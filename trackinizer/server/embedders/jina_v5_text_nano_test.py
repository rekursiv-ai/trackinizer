"""jina-embeddings-v5-text-nano unit tests via the fake ``_load`` seam.

Never loads weights or executes trust_remote_code: patches the module ``_load``
with a fake ``encode``, so the nano seam and unit-norm output are asserted
offline. The shared recipe (query/document prompt routing, batching, lazy load)
is exercised in ``jina_test``.
"""

from __future__ import annotations

from typing import cast

import huggingface_hub
import pytest
import torch

from trackinizer.server.embedders import jina_v5_text_nano
from trackinizer.server.embedders.jina_v5_text_nano import (
    JINA_MODEL_ID,
    JINA_NATIVE_DIM,
    JinaV5NanoEmbedder,
)
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.jina_v5_text_nano"


class _FakeEncoder:
    """Records ``prompt_name`` per call; returns a fixed non-unit tensor."""

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.prompts: list[str] = []

    def encode(
        self,
        texts: list[str],
        *,
        task: str,
        prompt_name: str,
        convert_to_tensor: bool,
    ) -> torch.Tensor:
        """Return a non-unit constant tensor, recording the prompt name."""
        del task, convert_to_tensor
        self.prompts.append(prompt_name)
        # Deliberately non-unit (all 3.0) so the embedder's own L2-normalize is
        # what makes the result unit -- proving we normalize defensively.
        return torch.full((len(texts), self._dim), 3.0)


def _patch_load(monkeypatch: pytest.MonkeyPatch, fake: _FakeEncoder) -> None:
    """Patch the module ``_load`` seam to return ``fake`` (no weights loaded)."""

    def fake_load(device: str) -> _FakeEncoder:
        del device
        return fake

    monkeypatch.setattr(f"{_MODULE}._load", fake_load)


def test_satisfies_the_query_embedder_protocol() -> None:
    """JinaV5NanoEmbedder is a ``QueryEmbedder`` (and thus an ``Embedder``)."""
    assert isinstance(JinaV5NanoEmbedder(), Embedder)
    assert isinstance(JinaV5NanoEmbedder(), QueryEmbedder)


def test_name_and_dim_are_native_768() -> None:
    """Nano stores at its native 768 dim (below the family's 1024)."""
    embedder = JinaV5NanoEmbedder()
    assert embedder.dim == JINA_NATIVE_DIM == 768
    assert embedder.name == "jina-embeddings-v5-text-nano@768"


@pytest.mark.asyncio
async def test_output_is_unit_normed_to_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedder L2-normalizes ``encode``'s output to a unit vector of ``dim``."""
    fake = _FakeEncoder(JINA_NATIVE_DIM)
    _patch_load(monkeypatch, fake)
    vector = await JinaV5NanoEmbedder().embed("anything")
    assert len(vector) == JINA_NATIVE_DIM
    assert abs(cast(float, sum(v * v for v in vector) ** 0.5) - 1.0) < 1e-4


def test_weights_present_probes_its_pinned_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[str, str, str]] = []

    def probe(repo: str, filename: str, *, revision: str) -> str | None:
        probes.append((repo, filename, revision))
        return "/hf/config.json"

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", probe)

    assert jina_v5_text_nano.weights_present() is True
    assert probes == [(JINA_MODEL_ID, "config.json", jina_v5_text_nano._REVISION)]


def test_load_builds_its_pinned_model_on_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str, str]] = []
    built = object()

    def jina_load(model_id: str, revision: str, device: str) -> object:
        loads.append((model_id, revision, device))
        return built

    monkeypatch.setattr(jina_v5_text_nano, "jina_load", jina_load)

    assert jina_v5_text_nano._load("cuda:0") is built
    assert loads == [(JINA_MODEL_ID, jina_v5_text_nano._REVISION, "cuda:0")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
