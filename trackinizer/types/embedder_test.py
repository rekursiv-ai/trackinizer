from __future__ import annotations

import pytest

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.types.embedder import Embedder, QueryEmbedder


class _DocumentOnly:
    dim = 3
    name = "doc-only"

    async def embed(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.0]


class _NoEmbed:
    dim = 3
    name = "nothing"


def test_runtime_check_narrows_embedders() -> None:
    assert isinstance(_DocumentOnly(), Embedder)
    assert isinstance(StubEmbedder(), Embedder)
    assert not isinstance(_NoEmbed(), Embedder)


def test_query_embedder_requires_embed_query() -> None:
    assert isinstance(StubEmbedder(), QueryEmbedder)
    assert not isinstance(_DocumentOnly(), QueryEmbedder)


@pytest.mark.asyncio
async def test_query_side_matches_dim() -> None:
    embedder: QueryEmbedder = StubEmbedder()
    assert len(await embedder.embed_query("q")) == embedder.dim
    assert len(await embedder.embed("d")) == embedder.dim


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
