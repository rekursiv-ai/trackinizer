"""jina-embeddings-v5-text-small unit tests via the fake ``_load`` seam.

The Jina recipe and routing are exercised in ``jina_v5_text_nano_test``; this
pins the small variant's name and native 1024 dim.
"""

from __future__ import annotations

from typing import cast

import huggingface_hub
import pytest
import torch

from trackinizer.server.embedders import jina_v5_text_small
from trackinizer.server.embedders.jina_v5_text_small import (
    JINA_MODEL_ID,
    JINA_NATIVE_DIM,
    JinaV5SmallEmbedder,
)
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.jina_v5_text_small"


class _FakeEncoder:
    """Returns a fixed non-unit tensor, recording the prompt name per call."""

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
        return torch.full((len(texts), self._dim), 3.0)


def _patch_load(monkeypatch: pytest.MonkeyPatch, fake: _FakeEncoder) -> None:
    """Patch the module ``_load`` seam to return ``fake`` (no weights loaded)."""

    def fake_load(device: str) -> _FakeEncoder:
        del device
        return fake

    monkeypatch.setattr(f"{_MODULE}._load", fake_load)


def test_satisfies_the_query_embedder_protocol() -> None:
    """JinaV5SmallEmbedder is a ``QueryEmbedder`` (and thus an ``Embedder``)."""
    assert isinstance(JinaV5SmallEmbedder(), Embedder)
    assert isinstance(JinaV5SmallEmbedder(), QueryEmbedder)


def test_name_and_dim_are_native_1024() -> None:
    """Small stores at its native 1024 dim."""
    embedder = JinaV5SmallEmbedder()
    assert embedder.dim == JINA_NATIVE_DIM == 1024
    assert embedder.name == "jina-embeddings-v5-text-small@1024"


@pytest.mark.asyncio
async def test_output_is_unit_normed_to_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedder L2-normalizes ``encode``'s output to a unit vector of ``dim``."""
    fake = _FakeEncoder(JINA_NATIVE_DIM)
    _patch_load(monkeypatch, fake)
    vector = await JinaV5SmallEmbedder().embed("anything")
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

    assert jina_v5_text_small.weights_present() is True
    assert probes == [(JINA_MODEL_ID, "config.json", jina_v5_text_small._REVISION)]


def test_load_builds_its_pinned_model_on_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str, str]] = []
    built = object()

    def jina_load(model_id: str, revision: str, device: str) -> object:
        loads.append((model_id, revision, device))
        return built

    monkeypatch.setattr(jina_v5_text_small, "jina_load", jina_load)

    assert jina_v5_text_small._load("cuda:0") is built
    assert loads == [(JINA_MODEL_ID, jina_v5_text_small._REVISION, "cuda:0")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
