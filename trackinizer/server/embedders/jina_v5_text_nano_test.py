"""jina-embeddings-v5-text-nano unit tests via the fake ``_load`` seam.

Never loads weights or reaches the network: patches the module ``_load`` with a
fake ONNX session and a capturing tokenizer, so the query/document prompt
asymmetry and unit-norm output are asserted offline. The shared Jina recipe is
identical for the small variant.
"""

from __future__ import annotations

import pytest

from trackinizer.server.embedders._jina_fakes import install
from trackinizer.server.embedders.jina_v5_text_nano import (
    JINA_NATIVE_DIM,
    JinaV5NanoEmbedder,
)
from trackinizer.types.embedder import Embedder, QueryEmbedder


_MODULE = "trackinizer.server.embedders.jina_v5_text_nano"


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
async def test_query_and_document_route_distinct_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``embed`` applies the document prompt; ``embed_query`` the query prompt.

    Jina v5 retrieval carries the corpus/query asymmetry as a prompt prefix;
    routing a document as a query (or vice versa) puts the two on different
    manifolds and wrecks retrieval.
    """
    tokenizer = install(monkeypatch, _MODULE, dim=JINA_NATIVE_DIM)
    embedder = JinaV5NanoEmbedder()
    _ = await embedder.embed("a stored session line")
    _ = await embedder.embed_query("a user question")

    document_text, query_text = tokenizer.seen[0][0], tokenizer.seen[1][0]
    assert document_text.endswith("a stored session line")
    assert query_text.endswith("a user question")
    assert document_text != "a stored session line"  # A document prompt is applied.
    assert query_text != "a user question"  # A distinct query prompt is applied.
    assert document_text.split("a stored")[0] != query_text.split("a user")[0]


@pytest.mark.asyncio
async def test_output_is_unit_normed_to_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedder L2-normalizes the pooled output to a unit vector of ``dim``."""
    install(monkeypatch, _MODULE, dim=JINA_NATIVE_DIM)
    vector = await JinaV5NanoEmbedder().embed("anything")
    assert len(vector) == JINA_NATIVE_DIM
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-4


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
