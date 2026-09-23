"""Unit tests for QwenEmbedder that never download the model.

These prove the wiring around the model -- lazy load, protocol shape, and the
truncate-then-normalize order -- against a fake tiny model injected at the
module's ``_load`` seam, so they run in milliseconds and need no network. The
family recipe on real weights is exercised in ``qwen3_0p6b_test``: the 4B
weights (~8 GB in bf16) do not fit a hosted CI runner's memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest
import torch

from trackinizer.server.embedders.octen_8b import OctenEmbedder
from trackinizer.server.embedders.qwen3_4b import (
    QWEN_TRUNCATED_DIM,
    QwenEmbedder,
)
from trackinizer.server.embedders.qwen_family import QUERY_INSTRUCT
from trackinizer.server.tools import bucket_embed
from trackinizer.types.embedder import Embedder


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class _FakeTokenizer:
    """Minimal left-padding tokenizer: maps texts to a fixed 2-token batch."""

    def __call__(
        self,
        texts: list[str],
        **_kwargs: object,
    ) -> _FakeBatch:
        rows = len(texts)
        return _FakeBatch(
            {
                "input_ids": torch.zeros((rows, 2), dtype=torch.long),
                "attention_mask": torch.ones((rows, 2), dtype=torch.long),
            },
        )


class _FakeBatch(dict[str, torch.Tensor]):
    """A ``dict``-like batch that supports ``.to(device)`` and ``**batch``."""

    def to(self, device: str) -> _FakeBatch:
        del device
        return self


class _FakeModel:
    """Returns a hidden state whose last-token vector has a heavy >1024 tail.

    The native pooled vector is ``[1]*1024`` in the prefix and ``[8]*(N-1024)``
    in the tail. Normalizing the full vector THEN slicing would leave the
    1024-prefix far short of unit norm (the tail carries most of the mass);
    slicing THEN normalizing yields a unit vector. The test asserts unit norm,
    so it fails unless the slice precedes the normalize.
    """

    native_dim = QWEN_TRUNCATED_DIM + 512

    def __init__(self) -> None:
        self.loaded_on = "unset"

    def __call__(self, **batch: torch.Tensor) -> _FakeOutput:

        rows = batch["attention_mask"].shape[0]
        prefix = torch.ones((rows, 2, QWEN_TRUNCATED_DIM))
        tail = torch.full((rows, 2, self.native_dim - QWEN_TRUNCATED_DIM), 8.0)
        return _FakeOutput(last_hidden_state=torch.cat([prefix, tail], dim=2))


class _FakeOutput:
    def __init__(self, *, last_hidden_state: torch.Tensor) -> None:
        self.last_hidden_state = last_hidden_state


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    """Patch the module ``_load`` seam to return the fake tokenizer+model."""
    model = _FakeModel()

    def fake_load(device: str) -> tuple[_FakeTokenizer, _FakeModel]:
        model.loaded_on = device
        return _FakeTokenizer(), model

    monkeypatch.setattr(
        "trackinizer.server.embedders.qwen3_4b._load",
        fake_load,
    )
    return model


def test_satisfies_the_embedder_protocol() -> None:
    """QwenEmbedder is an ``Embedder`` structurally: name, dim, embed."""
    assert isinstance(QwenEmbedder(), Embedder)


def test_name_and_dim_are_the_frozen_constants() -> None:
    embedder = QwenEmbedder()
    assert embedder.dim == QWEN_TRUNCATED_DIM == 1024
    # ``qwen3-embedding-4b``, not ``qwen3-4b`` (the chat LLM). This keys
    # session_embeddings rows, so the exact spelling is load-bearing.
    assert embedder.name == "qwen3-embedding-4b@1024"


def test_constructing_does_not_load_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lazy load: the shape's contract is that ``__init__`` costs no weights."""
    calls: list[str] = []

    def tripwire(device: str) -> tuple[_FakeTokenizer, _FakeModel]:
        calls.append(device)
        return _FakeTokenizer(), _FakeModel()

    monkeypatch.setattr("trackinizer.server.embedders.qwen3_4b._load", tripwire)
    _ = QwenEmbedder()
    assert calls == []  # No load at construction time.


@pytest.mark.asyncio
async def test_first_embed_loads_then_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model loads on first embed and is reused on the second."""
    calls: list[str] = []
    fake = _FakeModel()

    def counting_load(device: str) -> tuple[_FakeTokenizer, _FakeModel]:
        calls.append(device)
        return _FakeTokenizer(), fake

    monkeypatch.setattr(
        "trackinizer.server.embedders.qwen3_4b._load",
        counting_load,
    )
    embedder = QwenEmbedder(device="cpu")
    _ = await embedder.embed("first")
    _ = await embedder.embed("second")
    assert calls == ["cpu"]  # Loaded exactly once.


@pytest.mark.asyncio
async def test_truncates_before_normalizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The returned vector is unit-norm and 1024 -- slice precedes normalize.

    The fake's native vector puts heavy mass beyond 1024; only slicing first
    and normalizing the slice yields unit norm at 1024 dims.
    """
    _install_fake(monkeypatch)
    embedder = QwenEmbedder()
    vector = await embedder.embed("anything")
    assert len(vector) == QWEN_TRUNCATED_DIM
    norm = sum(v * v for v in vector) ** 0.5
    assert abs(norm - 1.0) < 1e-4


@pytest.mark.asyncio
async def test_embed_batch_preserves_order_and_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch spanning ``batch_size`` returns one vector per input, in order."""
    _install_fake(monkeypatch)
    embedder = QwenEmbedder(batch_size=2)
    vectors = await embedder.embed_batch(["a", "b", "c"])
    assert len(vectors) == 3
    assert all(len(v) == QWEN_TRUNCATED_DIM for v in vectors)


@pytest.mark.asyncio
async def test_embed_batch_empty_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No texts means no forward pass and no vectors."""
    model = _install_fake(monkeypatch)
    assert await QwenEmbedder().embed_batch([]) == []
    assert model.loaded_on == "unset"  # Empty input never even loads.


class _CapturingTokenizer(_FakeTokenizer):
    """A tokenizer that records the exact texts it was handed."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    @override
    def __call__(self, texts: list[str], **kwargs: object) -> _FakeBatch:
        self.seen.append(list(texts))
        return super().__call__(texts, **kwargs)


@pytest.mark.asyncio
async def test_query_side_prefixes_document_side_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""``embed_query`` prepends the instruct prompt; ``embed`` never does.

    The corpus/query asymmetry the model card documents: the query manifold
    carries ``Instruct: ...\\nQuery:<text>`` and the document side stays bare.
    Breaking it (prefixing documents, or not prefixing queries) would put the
    two on different manifolds and silently wreck retrieval.
    """
    tokenizer = _CapturingTokenizer()

    def fake_load(device: str) -> tuple[_CapturingTokenizer, _FakeModel]:
        del device
        return tokenizer, _FakeModel()

    monkeypatch.setattr("trackinizer.server.embedders.qwen3_4b._load", fake_load)
    embedder = QwenEmbedder()
    _ = await embedder.embed("a stored session line")
    _ = await embedder.embed_query("a stored session line")

    document_text, query_text = tokenizer.seen[0][0], tokenizer.seen[1][0]
    assert document_text == "a stored session line"  # Bare corpus side.
    assert query_text.startswith("Instruct: ")  # Prefixed query side.
    assert query_text.endswith("Query:a stored session line")
    assert QUERY_INSTRUCT in query_text


@pytest.mark.asyncio
async def test_embed_bucketed_batch_prefixes_and_configures_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucketed path applies doc_prefix and pins recompile limits ONCE.

    Glue over ``bucket_embed`` (whose routing/shape logic is tested there): this
    checks the embedder-side contract -- documents carry ``doc_prefix``, the
    static shape core is invoked with the model/dim/edges, and
    ``configure_recompile_limits`` runs exactly once across calls (re-pinning the
    global accumulated limit mid-run clipped warmup live).
    """
    _install_fake(monkeypatch)
    configures: list[tuple[int, ...]] = []
    seen_texts: list[list[str]] = []

    async def fake_embed_bucketed(
        model: bucket_embed.ForwardModel,
        tokenizer: bucket_embed.TokenBatcher,
        texts: list[str],
        *,
        edges: Sequence[int],
        rows: Mapping[int, int],
        dim: int,
    ) -> list[list[float]]:
        del model, tokenizer, edges, rows, dim
        seen_texts.append(list(texts))
        return [[0.0] for _ in texts]

    def record_configure(edges: Sequence[int]) -> None:
        configures.append(tuple(edges))

    monkeypatch.setattr(bucket_embed, "configure_recompile_limits", record_configure)
    monkeypatch.setattr(bucket_embed, "embed_bucketed", fake_embed_bucketed)

    embedder = QwenEmbedder()  # doc_prefix "" for stock Qwen.
    plan = {32: 4, 8192: 2}
    _ = await embedder.embed_bucketed_batch(["one", "two"], edges=(32, 8192), rows=plan)
    _ = await embedder.embed_bucketed_batch(["three"], edges=(32, 8192), rows=plan)

    assert configures == [(32, 8192)]  # Configured once, not per call.
    assert seen_texts == [["one", "two"], ["three"]]  # doc_prefix "" for Qwen.


@pytest.mark.asyncio
async def test_embed_bucketed_batch_applies_octen_doc_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doc_prefix model prepends it on the bucketed path too (Octen ``"- "``)."""

    def fake_load(device: str) -> tuple[_FakeTokenizer, _FakeModel]:
        del device
        return _FakeTokenizer(), _FakeModel()

    monkeypatch.setattr("trackinizer.server.embedders.octen_8b._load", fake_load)
    seen: list[list[str]] = []

    async def fake_embed_bucketed(
        model: bucket_embed.ForwardModel,
        tokenizer: bucket_embed.TokenBatcher,
        texts: list[str],
        *,
        edges: Sequence[int],
        rows: Mapping[int, int],
        dim: int,
    ) -> list[list[float]]:
        del model, tokenizer, edges, rows, dim
        seen.append(list(texts))
        return [[0.0] for _ in texts]

    def noop_configure(edges: Sequence[int]) -> None:
        del edges

    monkeypatch.setattr(bucket_embed, "configure_recompile_limits", noop_configure)
    monkeypatch.setattr(bucket_embed, "embed_bucketed", fake_embed_bucketed)

    _ = await OctenEmbedder().embed_bucketed_batch(
        ["doc"],
        edges=(32, 8192),
        rows={32: 4, 8192: 2},
    )
    assert seen == [["- doc"]]  # Octen's documented document prefix.


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
