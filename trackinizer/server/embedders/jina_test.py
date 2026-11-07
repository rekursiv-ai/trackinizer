"""Tests for the shared Jina v5 recipe: prompts, batching, lazy load, and loaders."""

from __future__ import annotations

from typing import cast, override

import asyncio
import threading

import huggingface_hub
import pytest
import torch

from trackinizer.server.embedders import jina
from trackinizer.server.embedders.jina import (
    JinaV5Embedder,
    jina_load,
    weights_cached,
)


_DIM = 4


class _FakeEncoder:
    """Returns a non-unit constant row per text, recording each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, str, bool]] = []

    def encode(
        self,
        texts: list[str],
        *,
        task: str,
        prompt_name: str,
        convert_to_tensor: bool,
    ) -> torch.Tensor:
        self.calls.append((list(texts), task, prompt_name, convert_to_tensor))
        return torch.full((len(texts), _DIM), 3.0)


class _TinyJina(JinaV5Embedder):
    """A Jina model whose load seam hands back one fake encoder, counting loads."""

    slug = "tiny-jina"
    default_dim = _DIM

    def __init__(self, *, batch_size: int = 16) -> None:
        super().__init__(device="cpu", batch_size=batch_size)
        self.encoder = _FakeEncoder()
        self.loads: list[str] = []
        self.release = threading.Event()
        """Held clear by a test to keep a load in flight; set, loads finish at once."""
        self.release.set()
        self.started = threading.Event()

    @override
    def _load_model(self, device: str) -> object:
        self.loads.append(device)
        self.started.set()
        _ = self.release.wait(timeout=5)
        return self.encoder


class _Bare(JinaV5Embedder):
    """A Jina model that never overrides the load seam."""

    slug = "bare"
    default_dim = 2


def test_the_name_carries_the_native_dim_unless_overridden() -> None:
    assert (_TinyJina().dim, _TinyJina().name) == (_DIM, "tiny-jina@4")
    assert (_Bare(dim=8).dim, _Bare(dim=8).name) == (8, "bare@8")


@pytest.mark.asyncio
async def test_documents_and_queries_use_their_own_prompts_unit_normed() -> None:
    embedder = _TinyJina()

    document = await embedder.embed("a line")
    query = await embedder.embed_query("a question")

    assert [(call[1], call[2], call[3]) for call in embedder.encoder.calls] == [
        ("retrieval", "document", True),
        ("retrieval", "query", True),
    ]
    for vector in (document, query):
        assert len(vector) == _DIM
        assert abs(cast(float, sum(v * v for v in vector) ** 0.5) - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_a_batch_splits_by_batch_size_and_keeps_order() -> None:
    embedder = _TinyJina(batch_size=2)

    vectors = await embedder.embed_batch(["a", "b", "c"])

    assert len(vectors) == 3
    assert [call[0] for call in embedder.encoder.calls] == [["a", "b"], ["c"]]


@pytest.mark.asyncio
async def test_an_empty_batch_never_loads() -> None:
    embedder = _TinyJina()

    assert await embedder.embed_batch([]) == []
    assert embedder.loads == []


@pytest.mark.asyncio
async def test_an_embed_waiting_on_an_inflight_load_reuses_its_model() -> None:
    embedder = _TinyJina()
    embedder.release.clear()
    first = asyncio.create_task(embedder.embed_query("a"))
    assert await asyncio.to_thread(embedder.started.wait, 5)
    second = asyncio.create_task(embedder.embed_query("b"))
    await asyncio.sleep(0)

    embedder.release.set()
    _ = await asyncio.gather(first, second)
    _ = await embedder.embed("c")

    assert embedder.loads == ["cpu"]
    assert len(embedder.encoder.calls) == 3


def test_the_base_class_has_no_model_to_load() -> None:
    with pytest.raises(NotImplementedError):
        _ = _Bare()._load_model("cpu")


def test_encoding_before_the_load_is_refused() -> None:
    with pytest.raises(ValueError, match="loaded before embedding"):
        _ = _TinyJina()._encode_sync(["a"], "query")


class _LoadedModel:
    def __init__(self) -> None:
        self.device = "unset"
        self.evaluated = False

    def to(self, device: str) -> _LoadedModel:
        self.device = device
        return self

    def eval(self) -> _LoadedModel:
        self.evaluated = True
        return self


class _AutoModel:
    def __init__(self) -> None:
        self.made = _LoadedModel()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def from_pretrained(self, model_id: str, **kwargs: object) -> _LoadedModel:
        self.calls.append((model_id, kwargs))
        return self.made


@pytest.mark.parametrize(
    ("device", "dtype"),
    [("cpu", torch.bfloat16), ("cuda:0", torch.float16)],
)
def test_jina_load_pins_the_revision_and_trusts_its_code(
    monkeypatch: pytest.MonkeyPatch,
    device: str,
    dtype: torch.dtype,
) -> None:
    auto = _AutoModel()
    monkeypatch.setattr(jina, "AutoModel", auto)

    model = jina_load("jinaai/x", "abc123", device)

    assert model is auto.made
    assert (auto.made.device, auto.made.evaluated) == (device, True)
    assert auto.calls == [
        ("jinaai/x", {"revision": "abc123", "trust_remote_code": True, "dtype": dtype}),
    ]


@pytest.mark.parametrize(
    ("cached", "present"),
    [("/hf/config.json", True), (None, False)],
)
def test_weights_cached_probes_the_pinned_config_without_network(
    monkeypatch: pytest.MonkeyPatch,
    cached: str | None,
    present: bool,
) -> None:
    probes: list[tuple[str, str, str]] = []

    def probe(repo: str, filename: str, *, revision: str) -> str | None:
        probes.append((repo, filename, revision))
        return cached

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", probe)

    assert weights_cached("jinaai/x", "abc123") is present
    assert probes == [("jinaai/x", "config.json", "abc123")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
