"""Unit tests for QwenEmbedder that never download the model.

These prove the wiring around the model -- lazy load, protocol shape, and the
truncate-then-normalize order -- against a fake tiny model injected at the
module's ``_load`` seam, so they run in milliseconds and need no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import pytest
import torch

from trackinizer.server.embedders import qwen_family, registry
from trackinizer.server.embedders.octen_8b import OctenEmbedder
from trackinizer.server.embedders.qwen3_4b import (
    QWEN_TRUNCATED_DIM,
    QwenEmbedder,
)
from trackinizer.server.embedders.qwen_family import (
    QUERY_INSTRUCT,
    QwenFamilyEmbedder,
)
from trackinizer.server.tools import bucket_embed
from trackinizer.types.embedder import Embedder


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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
        self.rows_seen: list[int] = []

    def __call__(self, **batch: torch.Tensor) -> _FakeOutput:

        rows = batch["attention_mask"].shape[0]
        self.rows_seen.append(rows)
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


class _FakeInductorConfig:
    freezing = False


def _uncompiled(model: _FakeModel, *, dynamic: bool) -> _FakeModel:
    """Stand in for ``torch.compile``: the model itself, so its calls stay visible."""
    del dynamic
    return model


class _LoadedConfig:
    use_cache = True


class _LoadedModel:
    """What ``AutoModel.from_pretrained`` returns, recording what ``hf_load`` does."""

    def __init__(self) -> None:
        self.config = _LoadedConfig()
        self.device = "unset"
        self.evaluated = False

    def to(self, device: str) -> _LoadedModel:
        self.device = device
        return self

    def eval(self) -> _LoadedModel:
        self.evaluated = True
        return self


class _Pretrained:
    """Stands in for an ``Auto*`` class: ``from_pretrained`` returns ``made``."""

    def __init__(self, made: object) -> None:
        self.made = made
        self.calls: list[tuple[str, dict[str, object]]] = []

    def from_pretrained(self, model_id: str, **kwargs: object) -> object:
        self.calls.append((model_id, kwargs))
        return self.made


def test_hf_load_turns_the_kv_cache_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """An embedder never reads the KV cache, which held 5-6x the forward on a 5090."""
    model = _LoadedModel()
    models = _Pretrained(model)
    monkeypatch.setattr(qwen_family, "AutoModel", models)
    monkeypatch.setattr(qwen_family, "AutoTokenizer", _Pretrained("tokenizer"))

    tokenizer, loaded = qwen_family.hf_load("org/model", "cuda:0")

    assert (tokenizer, loaded) == ("tokenizer", model)
    assert model.config.use_cache is False
    assert (model.device, model.evaluated) == ("cuda:0", True)
    assert models.calls == [("org/model", {"dtype": torch.float16})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("throughput", "device", "compiled", "frozen"),
    [
        (True, "cpu", True, True),
        (True, "cuda:0", True, False),
        (False, "cpu", False, False),
    ],
    ids=["follow on CPU", "GPU backfill", "server query path"],
)
async def test_a_throughput_embedder_compiles_and_freezes_only_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
    *,
    throughput: bool,
    device: str,
    compiled: bool,
    frozen: bool,
) -> None:
    """The backfill builder compiles on every device; only a CPU compile freezes.

    Freezing folds the weights into the graph so Inductor prepacks them for the
    CPU's matmul; the server's query embedder stays eager, so it pays no compile.
    """
    _install_fake(monkeypatch)
    compiles: list[bool] = []
    config = _FakeInductorConfig()

    def fake_compile(model: _FakeModel, *, dynamic: bool) -> _FakeModel:
        compiles.append(dynamic)
        return model

    monkeypatch.setattr(torch, "compile", fake_compile)
    monkeypatch.setattr(qwen_family, "_inductor_config", lambda: config)
    embedder = (
        registry.build_backfill_embedder(
            "qwen3-embedding-4b@1024",
            device=device,
            batch_size=8,
        )
        if throughput
        else registry.build_session_embedder("qwen3-embedding-4b@1024")
    )
    assert embedder is not None

    _ = await embedder.embed("compile me")

    assert compiles == ([True] if compiled else [])
    assert config.freezing is frozen


@pytest.mark.asyncio
@pytest.mark.parametrize(("throughput", "rows"), [(True, [2, 3]), (False, [1, 3])])
async def test_a_compiled_forward_never_sees_a_batch_of_one(
    monkeypatch: pytest.MonkeyPatch,
    *,
    throughput: bool,
    rows: list[int],
) -> None:
    """One text runs as two rows when compiled; Inductor's CPU code fails on one.

    The vectors are still one per text, and an eager forward is left alone.
    """
    model = _install_fake(monkeypatch)
    monkeypatch.setattr(torch, "compile", _uncompiled)
    monkeypatch.setattr(qwen_family, "_inductor_config", _FakeInductorConfig)
    embedder = (
        registry.build_backfill_embedder(
            "qwen3-embedding-4b@1024",
            device="cpu",
            batch_size=8,
        )
        if throughput
        else registry.build_session_embedder("qwen3-embedding-4b@1024")
    )
    assert isinstance(embedder, qwen_family.QwenFamilyEmbedder)

    one = await embedder.embed("alone")
    three = await embedder.embed_batch(["a", "b", "c"])

    assert model.rows_seen == rows
    assert len(one) == QWEN_TRUNCATED_DIM
    assert len(three) == 3


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
async def test_bucket_queue_configures_recompile_limits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recompile limits are pinned once per embedder, not per queue.

    Re-pinning the global accumulated limit mid-run clipped warmup live.
    """
    _install_fake(monkeypatch)
    configures: list[tuple[int, ...]] = []

    def record_configure(edges: Sequence[int]) -> None:
        configures.append(tuple(edges))

    monkeypatch.setattr(bucket_embed, "configure_recompile_limits", record_configure)
    embedder = QwenEmbedder()
    plan = {32: 4, 8192: 2}

    first = await embedder.bucket_queue(edges=(32, 8192), rows=plan)
    second = await embedder.bucket_queue(edges=(32, 8192), rows=plan)

    assert configures == [(32, 8192)]
    assert first is not second


class _PrefixProbe:
    """Records each text tokenized; one token per character, padded to the edge."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(
        self,
        texts: str | list[str],
        *,
        max_length: int,
        truncation: bool,
        padding: str = "do_not_pad",
        return_tensors: str | None = None,
    ) -> dict[str, object]:
        del truncation, padding
        if isinstance(texts, str):
            self.seen.append(texts)
            return {"input_ids": [0] * len(texts)}
        self.seen.extend(texts)
        assert return_tensors == "pt"
        shape = (len(texts), max_length)
        return {
            "input_ids": torch.zeros(shape, dtype=torch.long),
            "attention_mask": torch.ones(shape, dtype=torch.long),
        }


@pytest.mark.parametrize(
    ("module", "build", "seen"),
    [
        ("qwen3_4b", QwenEmbedder, ["doc", "doc"]),
        ("octen_8b", OctenEmbedder, ["- doc", "- doc"]),
    ],
)
@pytest.mark.asyncio
async def test_bucket_queue_routes_and_pads_each_document_with_its_prefix(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    build: Callable[[], QwenFamilyEmbedder],
    seen: list[str],
) -> None:
    """The routing count and the forward batch both read Octen's ``"- "``; Qwen's bare."""
    probe = _PrefixProbe()

    def fake_load(device: str) -> tuple[_PrefixProbe, _FakeModel]:
        del device
        return probe, _FakeModel()

    monkeypatch.setattr(f"trackinizer.server.embedders.{module}._load", fake_load)
    queue = await build().bucket_queue(edges=(8,), rows={8: 2})

    ticket = queue.add(["doc"])
    await queue.run(hold=0)

    assert probe.seen == seen
    assert ticket.done


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
