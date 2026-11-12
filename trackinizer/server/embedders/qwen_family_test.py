"""Tests for the shared Qwen3-Embedding recipe over a tiny fake model.

The fakes come from ``qwen_fakes``; nothing here loads weights or reaches the
network. Per-model tests pin each model's constants; this pins the recipe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, override

import asyncio
import threading

import huggingface_hub
import pytest
import torch

from trackinizer.server.embedders import qwen_family
from trackinizer.server.embedders.qwen_fakes import (
    CapturingTokenizer,
    FakeModel,
    FakeOutput,
)
from trackinizer.server.embedders.qwen_family import (
    QUERY_INSTRUCT,
    QwenFamilyEmbedder,
    _HfTokenBatcher,
    _last_token_pool,
    hf_load,
    weights_cached,
)
from trackinizer.server.tools import bucket_embed


if TYPE_CHECKING:
    from collections.abc import Sequence

    from transformers import PreTrainedTokenizerBase


_DIM = 4


class _TinyQwen(QwenFamilyEmbedder):
    """A family member whose load seam hands back fakes, counting loads."""

    slug = "tiny-qwen"
    default_dim = _DIM
    doc_prefix = "- "

    def __init__(
        self,
        *,
        tokenizer: object | None = None,
        batch_size: int = 16,
        device: str = "cpu",
        compile_forward: bool = False,
    ) -> None:
        super().__init__(
            device=device,
            batch_size=batch_size,
            compile_forward=compile_forward,
        )
        self.tokenizer = CapturingTokenizer() if tokenizer is None else tokenizer
        self.model = _RowCountingModel(truncated_dim=_DIM)
        self.loads: list[str] = []
        self.release = threading.Event()
        """Held clear by a test to keep a load in flight; set, loads finish at once."""
        self.release.set()
        self.started = threading.Event()

    @override
    def _load_model(
        self,
        device: str,
    ) -> tuple[PreTrainedTokenizerBase, torch.nn.Module]:
        self.loads.append(device)
        self.started.set()
        _ = self.release.wait(timeout=5)
        return cast(
            "tuple[PreTrainedTokenizerBase, torch.nn.Module]",
            (self.tokenizer, self.model),
        )


class _PlainQwen(_TinyQwen):
    """A family member with stock Qwen's bare document side."""

    doc_prefix = ""


class _Bare(QwenFamilyEmbedder):
    """A family member that never overrides the load seam."""

    slug = "bare"
    default_dim = 2


class _RowCountingModel(FakeModel):
    """The heavy-tail fake, recording how many rows each forward saw."""

    def __init__(self, *, truncated_dim: int) -> None:
        super().__init__(truncated_dim=truncated_dim)
        self.rows_seen: list[int] = []

    @override
    def __call__(self, **batch: torch.Tensor) -> FakeOutput:
        self.rows_seen.append(batch["attention_mask"].shape[0])
        return super().__call__(**batch)


class _InductorConfig:
    freezing = False


def _unit(vector: Sequence[float]) -> bool:
    return abs(cast(float, sum(v * v for v in vector) ** 0.5) - 1.0) < 1e-5


def test_the_name_follows_the_chosen_dim() -> None:
    assert (_TinyQwen().dim, _TinyQwen().name) == (_DIM, "tiny-qwen@4")
    assert (_Bare(dim=1).dim, _Bare(dim=1).name) == (1, "bare@1")


def test_constructing_does_not_load_the_model() -> None:
    """Lazy load: ``__init__`` costs no weights."""
    assert _TinyQwen().loads == []


@pytest.mark.asyncio
async def test_a_document_carries_the_prefix_and_a_query_the_instruction() -> None:
    r"""The card's corpus/query asymmetry: ``Instruct: ...\nQuery:`` on queries alone.

    The fake's tail past ``dim`` carries most of the mass, so a unit vector at
    ``dim`` proves the slice precedes the normalize.
    """
    embedder = _TinyQwen()
    tokenizer = cast("CapturingTokenizer", embedder.tokenizer)

    document = await embedder.embed("line")
    query = await embedder.embed_query("line")

    assert tokenizer.seen == [
        ["- line"],
        [f"Instruct: {QUERY_INSTRUCT}\nQuery:line"],
    ]
    assert (len(document), len(query)) == (_DIM, _DIM)
    assert _unit(document)
    assert _unit(query)


@pytest.mark.asyncio
async def test_a_batch_splits_by_batch_size_and_keeps_count() -> None:
    embedder = _TinyQwen(batch_size=2)

    vectors = await embedder.embed_batch(["a", "b", "c"])

    assert len(vectors) == 3
    assert embedder.model.rows_seen == [2, 1]
    assert all(_unit(vector) for vector in vectors)


@pytest.mark.asyncio
async def test_an_empty_batch_never_loads() -> None:
    embedder = _TinyQwen()

    assert await embedder.embed_batch([]) == []
    assert embedder.loads == []


@pytest.mark.asyncio
async def test_an_embed_waiting_on_an_inflight_load_reuses_its_model() -> None:
    embedder = _TinyQwen()
    embedder.release.clear()
    first = asyncio.create_task(embedder.embed_query("a"))
    assert await asyncio.to_thread(embedder.started.wait, 5)
    second = asyncio.create_task(embedder.embed_query("b"))
    await asyncio.sleep(0)

    embedder.release.set()
    _ = await asyncio.gather(first, second)
    _ = await embedder.embed("c")

    assert embedder.loads == ["cpu"]
    assert embedder.model.rows_seen == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("device", "frozen"), [("cpu", True), ("cuda:0", False)])
async def test_a_compiled_forward_is_dynamic_and_frozen_only_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
    device: str,
    frozen: bool,
) -> None:
    compiles: list[bool] = []
    config = _InductorConfig()

    def fake_compile(model: FakeModel, *, dynamic: bool) -> FakeModel:
        compiles.append(dynamic)
        return model

    monkeypatch.setattr(torch, "compile", fake_compile)
    monkeypatch.setattr(qwen_family, "_inductor_config", lambda: config)
    embedder = _TinyQwen(device=device, compile_forward=True)

    one = await embedder.embed("alone")

    assert compiles == [True]
    assert config.freezing is frozen
    # Inductor's CPU code fails on one row, so one text runs as two.
    assert embedder.model.rows_seen == [2]
    assert len(one) == _DIM


@pytest.mark.asyncio
async def test_an_eager_forward_runs_one_text_as_one_row() -> None:
    embedder = _TinyQwen()

    _ = await embedder.embed("alone")

    assert embedder.model.rows_seen == [1]


def test_the_inductor_config_is_torchs_own() -> None:
    assert isinstance(qwen_family._inductor_config().freezing, bool)


def test_the_base_class_has_no_model_to_load() -> None:
    with pytest.raises(NotImplementedError):
        _ = _Bare()._load_model("cpu")


def test_embedding_before_the_load_is_refused() -> None:
    with pytest.raises(ValueError, match="loaded before embedding"):
        _ = _TinyQwen()._embed_sync(["a"])


@pytest.mark.asyncio
async def test_bucket_queue_pins_recompile_limits_once_per_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned: list[tuple[int, ...]] = []

    def pin(edges: Sequence[int]) -> None:
        pinned.append(tuple(edges))

    monkeypatch.setattr(bucket_embed, "configure_recompile_limits", pin)
    embedder = _TinyQwen()

    first = await embedder.bucket_queue(edges=(8, 32), rows={8: 2, 32: 1})
    second = await embedder.bucket_queue(edges=(8, 32), rows={8: 2, 32: 1})

    assert pinned == [(8, 32)]
    assert first is not second


@pytest.mark.asyncio
async def test_bucket_queue_without_a_tokenizer_is_refused() -> None:
    embedder = _TinyQwen()
    embedder.tokenizer = None

    with pytest.raises(ValueError, match="loaded before embedding"):
        _ = await embedder.bucket_queue(edges=(8,), rows={8: 1})


@pytest.mark.parametrize(
    ("build", "seen"),
    [(_PlainQwen, ["doc", "doc"]), (_TinyQwen, ["- doc", "- doc"])],
    ids=["bare", "prefixed"],
)
@pytest.mark.asyncio
async def test_bucket_queue_routes_and_pads_each_document_with_its_prefix(
    build: type[_TinyQwen],
    seen: list[str],
) -> None:
    """The routing count and the forward batch both read the document prefix."""
    tokenizer = _CharTokenizer()
    queue = await build(tokenizer=tokenizer).bucket_queue(edges=(8,), rows={8: 2})

    ticket = queue.add(["doc"])
    await queue.run(hold=0)

    assert tokenizer.seen == seen
    assert ticket.done


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
    monkeypatch.setattr(qwen_family, "AutoModel", models)
    monkeypatch.setattr(qwen_family, "AutoTokenizer", tokenizers)

    tokenizer, loaded = hf_load("org/model", device)

    assert (tokenizer, loaded) == ("tokenizer", model)
    assert tokenizers.calls == [("org/model", {"padding_side": "left"})]
    assert models.calls == [("org/model", {"dtype": dtype})]
    assert model.config.use_cache is False
    assert (model.device, model.evaluated) == (device, True)


@pytest.mark.parametrize(
    ("cached", "present"),
    [("/hf/config.json", True), (None, False)],
)
def test_weights_cached_probes_the_config_without_network(
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    present: bool,
) -> None:
    probes: list[tuple[str, str]] = []

    def probe(repo: str, filename: str) -> str | None:
        probes.append((repo, filename))
        return cached

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", probe)

    assert weights_cached("org/model") is present
    assert probes == [("org/model", "config.json")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
