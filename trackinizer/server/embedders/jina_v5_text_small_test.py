"""jina-embeddings-v5-text-small unit tests via the fake ``_load`` seam.

The Jina recipe and routing are exercised in ``jina_v5_text_nano_test``; this
pins the small variant's name and native 1024 dim.
"""

from __future__ import annotations

import pytest

from trackinizer.server.embedders._jina_fakes import install
from trackinizer.server.embedders.jina_v5_text_small import (
    JINA_NATIVE_DIM,
    JinaV5SmallEmbedder,
)
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.jina_v5_text_small"


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
    """The embedder L2-normalizes the pooled output to a unit vector of ``dim``."""
    install(monkeypatch, _MODULE, dim=JINA_NATIVE_DIM)
    vector = await JinaV5SmallEmbedder().embed("anything")
    assert len(vector) == JINA_NATIVE_DIM
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-4


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
