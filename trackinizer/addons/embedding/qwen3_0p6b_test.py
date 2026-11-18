"""Tests for the Qwen3-Embedding-0.6B recipe over a tiny fake model.

The fakes come from ``fakes``; nothing here loads weights or reaches the
network. Each test patches the module's ``_load`` seam.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, ClassVar, cast, override

import asyncio
import os
import threading

import pytest
import torch

from trackinizer.addons.embedding import bucket_embed, qwen3_0p6b
from trackinizer.addons.embedding.fakes import (
    CapturingTokenizer,
    FakeModel,
    FakeOutput,
)
from trackinizer.addons.embedding.qwen3_0p6b import (
    QUERY_INSTRUCT,
    QWEN_MODEL_ID,
    Qwen06BEmbedder,
    _HfTokenBatcher,
    _last_token_pool,
    hf_load,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from transformers import PreTrainedTokenizerBase


_DIM = 1_024


class _TinyQwen(Qwen06BEmbedder):
    """The model with a document prefix, its load seam handing back fakes, counted."""

    name = "tiny-qwen@1024"
    doc_prefix: ClassVar[str] = "- "

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        tokenizer: object | None = None,
        batch_size: int = 16,
        device: str = "cpu",
    ) -> None:
        config = Qwen06BEmbedder.Config()
        config.device = device
        config.threads = 2
        config.batch_size = batch_size
        super().__init__(config)
        self.tokenizer = CapturingTokenizer() if tokenizer is None else tokenizer
        self.model = _RowCountingModel(truncated_dim=_DIM)
        self.loads: list[str] = []
        self.release = threading.Event()
        """Held clear by a test to keep a load in flight; set, loads finish at once."""
        self.release.set()
        self.started = threading.Event()
        monkeypatch.setattr(qwen3_0p6b, "_load", self._load_model)

    def _load_model(
        self,
        device: str,
        *,
        threads: int,
    ) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
        self.loads.append(f"{device}/{threads}")
        self.started.set()
        _ = self.release.wait(timeout=5)
        return cast(
            "tuple[PreTrainedTokenizerBase, torch.nn.Module]",
            (self.tokenizer, self.model),
        )


class _PlainQwen(_TinyQwen):
    """A family member with stock Qwen's bare document side."""

    doc_prefix: ClassVar[str] = ""


class _RowCountingModel(FakeModel):
    """The heavy-tail fake, recording how many rows each forward saw."""

    def __init__(self, *, truncated_dim: int) -> None:
        super().__init__(truncated_dim=truncated_dim)
        self.rows_seen: list[int] = []
        self.shapes: list[tuple[int, int]] = []

    @override
    def __call__(self, **batch: torch.Tensor) -> FakeOutput:
        self.rows_seen.append(batch["attention_mask"].shape[0])
        rows, length = batch["attention_mask"].shape
        self.shapes.append((rows, length))
        return super().__call__(**batch)


def _unit(vector: Sequence[float]) -> bool:
    return abs(cast(float, sum(v * v for v in vector) ** 0.5) - 1.0) < 1e-5


def test_the_name_and_dim_are_the_native_1024() -> None:
    model = Qwen06BEmbedder(Qwen06BEmbedder.Config())

    assert (model.dim, model.name) == (_DIM, "qwen3-embedding-0.6b@1024")


@pytest.mark.parametrize(("cpus", "threads"), [(None, 1), (4, 1), (40, 10), (128, 16)])
def test_threads_default_to_a_quarter_of_the_cpus_from_1_to_16(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int | None,
    threads: int,
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    config = Qwen06BEmbedder.Config()

    assert Qwen06BEmbedder(config)._threads == threads
    config.threads = 3
    assert Qwen06BEmbedder(config)._threads == 3


def test_constructing_does_not_load_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lazy load: ``__init__`` costs no weights."""
    assert _TinyQwen(monkeypatch).loads == []


@pytest.mark.asyncio
async def test_documents_are_bare_and_queries_carry_the_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""The card's corpus/query asymmetry: ``Instruct: ...\nQuery:`` on queries alone.

    The fake's tail past ``dim`` carries most of the mass, so a unit vector at
    ``dim`` proves the slice precedes the normalize. A subclass's prefix reaches
    its documents and no query.
    """
    embedder = _PlainQwen(monkeypatch)
    tokenizer = cast("CapturingTokenizer", embedder.tokenizer)

    [document] = await embedder.embed_batch(["line"])
    query = await embedder.embed_query("line")
    prefixed = _TinyQwen(monkeypatch)
    _ = await prefixed.embed_batch(["line"])
    _ = await prefixed.embed_query("line")

    assert tokenizer.seen == [
        ["line"],
        [f"Instruct: {QUERY_INSTRUCT}\nQuery:line"],
    ]
    assert cast("CapturingTokenizer", prefixed.tokenizer).seen == [
        ["- line"],
        [f"Instruct: {QUERY_INSTRUCT}\nQuery:line"],
    ]
    assert (len(document), len(query)) == (_DIM, _DIM)
    assert _unit(document)
    assert _unit(query)


@pytest.mark.asyncio
async def test_a_batch_runs_shortest_first_and_returns_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = _PlainQwen(monkeypatch, batch_size=3)
    tokenizer = cast("CapturingTokenizer", embedder.tokenizer)
    texts = ["three", "x", "fourteen chars", "ab"]

    vectors = await embedder.embed_batch(texts)
    monkeypatch.setattr(embedder, "_embed_sync", _lengths)
    lengths = await embedder.embed_batch(texts)

    assert tokenizer.seen == [["x", "ab", "three"], ["fourteen chars"]]
    assert embedder.model.rows_seen == [3, 1]
    assert all(_unit(vector) for vector in vectors)
    assert lengths == [[5.0], [1.0], [14.0], [2.0]]


@pytest.mark.asyncio
async def test_an_empty_batch_never_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = _TinyQwen(monkeypatch)

    assert await embedder.embed_batch([]) == []
    assert embedder.loads == []


@pytest.mark.asyncio
async def test_an_embed_waiting_on_an_inflight_load_reuses_its_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = _TinyQwen(monkeypatch)
    embedder.release.clear()
    first = asyncio.create_task(embedder.embed_query("a"))
    assert await asyncio.to_thread(embedder.started.wait, 5)
    second = asyncio.create_task(embedder.embed_query("b"))
    await asyncio.sleep(0)

    embedder.release.set()
    _ = await asyncio.gather(first, second)
    _ = await embedder.embed_batch(["c"])

    assert embedder.loads == ["cpu/2"]
    assert embedder.model.rows_seen == [1, 1, 1]


@pytest.mark.asyncio
async def test_a_cuda_batch_runs_through_the_bucket_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off the CPU every forward has a planned shape: its bucket's rows and length.

    The fake card holds 1.25 GB, so the plan's rows are small; ``meta`` stands in for
    a GPU device, which a host without one cannot place tensors on. The forward is
    compiled, and the recompile limits pinned, once.
    """
    pinned: list[tuple[int, ...]] = []
    compiles: list[bool] = []

    def pin(edges: Sequence[int]) -> None:
        pinned.append(tuple(edges))

    def fake_compile(model: FakeModel, *, dynamic: bool) -> FakeModel:
        compiles.append(dynamic)
        return model

    monkeypatch.setattr(bucket_embed, "configure_recompile_limits", pin)
    monkeypatch.setattr(torch, "compile", fake_compile)
    monkeypatch.setattr(torch.cuda, "get_device_properties", _card)
    embedder = _TinyQwen(monkeypatch, tokenizer=_CharTokenizer(), device="meta")

    vectors = await embedder.embed_batch(["abc", "x" * 100, "de"])
    _ = await embedder.embed_batch(["f"])

    rows = {
        edge: bucket_embed.derive_rows(qwen3_0p6b._SHAPE, edge, vram_gb=1.25, dim=_DIM)
        for edge in (64, 160)
    }
    assert embedder.model.shapes == [(rows[64], 64), (rows[160], 160), (rows[64], 64)]
    assert rows[64] > 3
    assert (pinned, compiles) == ([qwen3_0p6b._EDGES], [True])
    assert len(vectors) == 3
    assert embedder.loads == ["meta/2"]
    # Routing and the forward both read the document prefix.
    assert cast("_CharTokenizer", embedder.tokenizer).seen[:3] == [
        "- abc",
        "- " + "x" * 100,
        "- de",
    ]


@pytest.mark.asyncio
async def test_an_eager_forward_runs_one_text_as_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = _TinyQwen(monkeypatch)

    _ = await embedder.embed_batch(["alone"])

    assert embedder.model.rows_seen == [1]


def test_embedding_before_the_load_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="loaded before embedding"):
        _ = _TinyQwen(monkeypatch)._embed_sync(["a"])


def test_load_builds_the_model_on_the_device_with_its_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str]] = []
    threads: list[int] = []
    built = (object(), object())

    def fake_hf_load(model_id: str, device: str) -> tuple[object, object]:
        loads.append((model_id, device))
        return built

    monkeypatch.setattr(qwen3_0p6b, "hf_load", fake_hf_load)
    monkeypatch.setattr(torch, "set_num_threads", threads.append)

    assert qwen3_0p6b._load("cuda:0", threads=5) is built
    assert (loads, threads) == ([(QWEN_MODEL_ID, "cuda:0")], [5])


def test_a_left_padded_batch_pools_the_last_position() -> None:
    hidden = torch.arange(12.0).reshape(2, 3, 2)
    mask = torch.tensor([[0, 1, 1], [1, 1, 1]])

    assert torch.equal(_last_token_pool(hidden, mask), hidden[:, -1])


def test_a_right_padded_batch_pools_each_rows_last_real_token() -> None:
    hidden = torch.arange(12.0).reshape(2, 3, 2)
    mask = torch.tensor([[1, 1, 0], [1, 0, 0]])

    assert torch.equal(
        _last_token_pool(hidden, mask),
        torch.stack([hidden[0, 1], hidden[1, 0]]),
    )


class _CharTokenizer:
    """One token per character; pads a list to ``max_length`` as torch tensors."""

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
        del truncation, padding, return_tensors
        if isinstance(texts, str):
            self.seen.append(texts)
            return {"input_ids": list(range(min(len(texts), max_length)))}
        self.seen.extend(texts)
        shape = (len(texts), max_length)
        return {
            "input_ids": torch.ones(shape, dtype=torch.long),
            "attention_mask": torch.ones(shape, dtype=torch.long),
        }


def _batcher() -> _HfTokenBatcher:
    return _HfTokenBatcher(
        cast("PreTrainedTokenizerBase", _CharTokenizer()),
        device="cpu",
        prefix="- ",
    )


def _lengths(texts: list[str]) -> list[list[float]]:
    """Stand in for a forward: each text's vector is its length."""
    return [[float(len(text))] for text in texts]


def _card(device: str) -> SimpleNamespace:
    """Stand in for the device's properties: a card of 1.25 GB."""
    return SimpleNamespace(total_memory=1_250_000_000, device=device)


def test_the_token_count_reads_the_prefixed_text() -> None:
    assert _batcher().count("abc") == len("- abc")


def test_a_short_bucket_pads_its_tail_rows_fully_masked() -> None:
    batch = _batcher().pad_batch(["a"], rows=3, seq=4)

    assert batch["input_ids"].shape == batch["attention_mask"].shape == (3, 4)
    assert batch["attention_mask"].sum(dim=1).tolist() == [4, 0, 0]
    assert batch["input_ids"][1:].sum().item() == 0


def test_a_full_bucket_adds_no_rows() -> None:
    batch = _batcher().pad_batch(["a", "b"], rows=2, seq=4)

    assert batch["attention_mask"].tolist() == [[1] * 4] * 2


class _LoadedConfig:
    use_cache = True


class _LoadedModel:
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
    def __init__(self, made: object) -> None:
        self.made = made
        self.calls: list[tuple[str, dict[str, object]]] = []

    def from_pretrained(self, model_id: str, **kwargs: object) -> object:
        self.calls.append((model_id, kwargs))
        return self.made


@pytest.mark.parametrize(
    ("device", "dtype"),
    [("cpu", torch.bfloat16), ("cuda:0", torch.float16)],
)
def test_hf_load_left_pads_halves_and_turns_the_kv_cache_off(
    monkeypatch: pytest.MonkeyPatch,
    device: str,
    dtype: torch.dtype,
) -> None:
    model = _LoadedModel()
    models, tokenizers = _Pretrained(model), _Pretrained("tokenizer")
    monkeypatch.setattr(qwen3_0p6b, "AutoModel", models)
    monkeypatch.setattr(qwen3_0p6b, "AutoTokenizer", tokenizers)

    tokenizer, loaded = hf_load("org/model", device)

    assert (tokenizer, loaded) == ("tokenizer", model)
    assert tokenizers.calls == [("org/model", {"padding_side": "left"})]
    assert models.calls == [("org/model", {"dtype": dtype})]
    assert model.config.use_cache is False
    assert (model.device, model.evaluated) == (device, True)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
