"""Qwen3-Embedding-0.6B unit tests via the fake ``_load`` seam (no weights).

The shared family recipe is covered in ``qwen3_4b_test``; this pins the 0.6B
specifics: its native 1024 dim (no truncation), name, and that a document embed
is a unit vector with no ``"- "`` / instruction prefix.
"""

from __future__ import annotations

from typing import cast

import huggingface_hub
import pytest

from trackinizer.server.embedders import qwen3_0p6b
from trackinizer.server.embedders.qwen3_0p6b import (
    QWEN_MODEL_ID,
    QWEN_TRUNCATED_DIM,
    Qwen06BEmbedder,
)
from trackinizer.server.embedders.qwen_fakes import install_capturing
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.qwen3_0p6b"


def test_satisfies_the_query_embedder_protocol() -> None:
    """Qwen06BEmbedder is a ``QueryEmbedder`` (and thus an ``Embedder``)."""
    assert isinstance(Qwen06BEmbedder(), Embedder)
    assert isinstance(Qwen06BEmbedder(), QueryEmbedder)


def test_name_and_dim_are_native_1024() -> None:
    """0.6B stores at its native 1024 dim (no truncation)."""
    embedder = Qwen06BEmbedder()
    assert embedder.dim == QWEN_TRUNCATED_DIM == 1024
    assert embedder.name == "qwen3-embedding-0.6b@1024"


@pytest.mark.asyncio
async def test_document_is_bare_and_unit_norm(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document embed carries no prefix and is a unit vector of ``dim``."""
    tokenizer = install_capturing(monkeypatch, _MODULE, truncated_dim=1024)
    vector = await Qwen06BEmbedder().embed("a stored session line")
    assert tokenizer.seen[0][0] == "a stored session line"  # Bare corpus side.
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

    assert qwen3_0p6b.weights_present() is True
    assert probes == [(QWEN_MODEL_ID, "config.json")]


def test_load_builds_its_own_model_on_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str]] = []
    built = (object(), object())

    def hf_load(model_id: str, device: str) -> tuple[object, object]:
        loads.append((model_id, device))
        return built

    monkeypatch.setattr(qwen3_0p6b, "hf_load", hf_load)

    assert qwen3_0p6b._load("cuda:0") is built
    assert loads == [(QWEN_MODEL_ID, "cuda:0")]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
