from __future__ import annotations

from trackinizer.addons.embedding.fakes import HashEmbedder
from trackinizer.addons.embedding.model import Embedder


class _QueriesOnly:
    dim = 3
    name = "queries-only"

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.0]


def test_a_model_embeds_batches_of_documents_and_queries() -> None:
    assert isinstance(HashEmbedder(dim=3), Embedder)
    assert not isinstance(_QueriesOnly(), Embedder)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
