"""Qwen3-Embedding-8B unit tests via the fake ``_load`` seam (no weights).

The shared family recipe is covered in ``qwen3_4b_test``; this pins the 8B
specifics: truncation of the native 4096 to 1024, name, and unit norm after the
truncate-then-normalize slice.
"""

from __future__ import annotations

from typing import cast

import huggingface_hub
import pytest

from trackinizer.server.embedders import qwen3_8b
from trackinizer.server.embedders.qwen3_8b import (
    QWEN_MODEL_ID,
    QWEN_TRUNCATED_DIM,
    Qwen8BEmbedder,
)
from trackinizer.server.embedders.qwen_fakes import install_capturing
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.qwen3_8b"


def test_satisfies_the_query_embedder_protocol() -> None:
    """Qwen8BEmbedder is a ``QueryEmbedder`` (and thus an ``Embedder``)."""
    assert isinstance(Qwen8BEmbedder(), Embedder)
    assert isinstance(Qwen8BEmbedder(), QueryEmbedder)


def test_name_and_dim() -> None:
    """8B truncates to 1024, keeping the family storage width."""
    embedder = Qwen8BEmbedder()
    assert embedder.dim == QWEN_TRUNCATED_DIM == 1024
    assert embedder.name == "qwen3-embedding-8b@1024"


@pytest.mark.asyncio
async def test_truncates_to_unit_vector_of_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heavy-tail fake proves the slice precedes the normalize at 1024."""
    install_capturing(monkeypatch, _MODULE, truncated_dim=1024)
    vector = await Qwen8BEmbedder().embed("anything")
    assert len(vector) == QWEN_TRUNCATED_DIM
    assert abs(cast(float, sum(v * v for v in vector) ** 0.5) - 1.0) < 1e-4


def test_weights_present_probes_its_own_models_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[str, str]] = []

    def probe(repo: str, filename: str) -> str | None:
        probes.append((repo, filename))
        return "/hf/config.json"

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", probe)

    assert qwen3_8b.weights_present() is True
    assert probes == [(QWEN_MODEL_ID, "config.json")]


def test_load_builds_its_own_model_on_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str]] = []
    built = (object(), object())

    def hf_load(model_id: str, device: str) -> tuple[object, object]:
        loads.append((model_id, device))
        return built

    monkeypatch.setattr(qwen3_8b, "hf_load", hf_load)

    assert qwen3_8b._load("cuda:0") is built
    assert loads == [(QWEN_MODEL_ID, "cuda:0")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
