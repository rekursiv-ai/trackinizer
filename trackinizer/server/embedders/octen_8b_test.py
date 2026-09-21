"""Octen-Embedding-8B unit tests via the fake ``_load`` seam (no weights).

The shared family recipe (truncate-before-normalize, lazy load, batch order) is
covered in ``qwen3_4b_test``; this file pins Octen's DISTINGUISHING contract: the
card's ``"- "`` document prefix, the family ``Instruct:`` query prefix, its
name, and its dim.
"""

from __future__ import annotations

import pytest

from trackinizer.server.embedders.octen_8b import (
    OCTEN_TRUNCATED_DIM,
    OctenEmbedder,
)
from trackinizer.server.embedders.qwen_fakes import install_capturing
from trackinizer.server.embedders.qwen_family import QUERY_INSTRUCT
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.octen_8b"


def test_satisfies_the_query_embedder_protocol() -> None:
    """OctenEmbedder is a ``QueryEmbedder`` (and thus an ``Embedder``)."""
    assert isinstance(OctenEmbedder(), Embedder)
    assert isinstance(OctenEmbedder(), QueryEmbedder)


def test_name_and_dim() -> None:
    """Its stored name and dim are the frozen constants."""
    embedder = OctenEmbedder()
    assert embedder.dim == OCTEN_TRUNCATED_DIM == 1024
    assert embedder.name == "octen-embedding-8b@1024"


@pytest.mark.asyncio
async def test_document_carries_dash_prefix_query_carries_instruct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""Octen prepends ``"- "`` to DOCUMENTS and ``Instruct:`` to QUERIES.

    The ``"- "`` document prefix is the card's documented workaround for an
    upstream Qwen3-Embedding bug (embedding a bare document misbehaves); the
    query keeps the family ``Instruct: ...\nQuery:`` prompt. Dropping either
    would shift the vectors off the manifold the card measured.
    """
    tokenizer = install_capturing(monkeypatch, _MODULE, truncated_dim=1024)
    embedder = OctenEmbedder()
    _ = await embedder.embed("a stored session line")
    _ = await embedder.embed_query("a user question")

    document_text, query_text = tokenizer.seen[0][0], tokenizer.seen[1][0]
    assert document_text == "- a stored session line"  # Card's doc workaround.
    assert query_text.startswith("Instruct: ")
    assert query_text.endswith("Query:a user question")
    assert QUERY_INSTRUCT in query_text
    # The query side must NOT get the document "- " prefix.
    assert not query_text.startswith("- ")


@pytest.mark.asyncio
async def test_embed_returns_unit_vector_of_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document embed is unit-norm and ``dim`` long (slice precedes normalize)."""
    install_capturing(monkeypatch, _MODULE, truncated_dim=1024)
    vector = await OctenEmbedder().embed("anything")
    assert len(vector) == OCTEN_TRUNCATED_DIM
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-4


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
