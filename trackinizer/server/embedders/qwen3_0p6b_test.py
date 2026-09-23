"""Qwen3-Embedding-0.6B tests: fake ``_load`` seam, plus one real-weights test.

The shared family recipe is covered in ``qwen3_4b_test``; this pins the 0.6B
specifics: its native 1024 dim (no truncation), name, and that a document embed
is a unit vector with no ``"- "`` / instruction prefix. The family's only
real-weights test lives here, on the smallest member.
"""

from __future__ import annotations

import pytest

from trackinizer.server.embedders.qwen3_0p6b import (
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
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-4


@pytest.mark.network_huggingface
@pytest.mark.asyncio
async def test_real_model_embeds_meaningfully() -> None:
    """The real weights: unit norm, truncated dim, deterministic, semantics hold.

    Downloads Qwen3-Embedding-0.6B (~1.2 GB) through the provisioned HF cache.
    The ``network_huggingface`` resource marker rolls up to the ``integration``
    tier (skipped by default) via ``resource_markers.py``; never hand-write the
    rollup.
    """
    # The 0.6B, not the 4B: the 4B's bf16 weights are ~8 GB, which took down
    # the hosted CI runner whenever pytest-split placed this test late in a
    # shard. dim=512, not the native 1024: at 1024 the Matryoshka slice is a
    # no-op, and truncate-then-normalize is the step real weights must exercise.
    embedder = Qwen06BEmbedder(dim=512)
    cat_a = "The cat sat on the warm windowsill in the sun."
    cat_b = "A kitten napped on the sunny window ledge."
    finance = "Quarterly revenue exceeded analyst expectations."

    vectors = await embedder.embed_batch([cat_a, cat_b, finance])
    again = await embedder.embed(cat_a)

    assert all(len(v) == 512 for v in vectors)
    for v in vectors:
        norm = sum(x * x for x in v) ** 0.5
        # bf16 CPU inference (see qwen_family.hf_load): the vector is normalized
        # in bf16 then cast to fp32, so unit norm holds only to bf16 precision
        # (~3 sig figs). halfvec storage is itself fp16, so this is the real
        # precision the column keeps -- not a looser bar to pass.
        assert abs(norm - 1.0) < 5e-3
    # Determinism is bit-exact for an IDENTICAL call: same text, same batch
    # shape, same call reproduces the vector exactly. It is NOT asserted across
    # batch shapes: ``embed([cat_a])`` left-pads cat_a to its own length while
    # ``embed_batch([cat_a, cat_b, finance])`` pads it to the longest of three,
    # so cat_a's tokens sit at different positions over a different sequence
    # length. In bf16 the fused attention kernel then accumulates in a
    # host-dependent order, so the two shapes agree only to bf16 precision on
    # some CPUs and diverge (~2e-3) on others -- a false invariant that failed
    # on a host CI never minted against. The real, portable guarantee is
    # same-shape reproducibility.
    repeat = await embedder.embed(cat_a)
    assert repeat == again

    # Unit vectors, so each dot product is a cosine.
    near = sum(x * y for x, y in zip(vectors[0], vectors[1], strict=True))
    far = sum(x * y for x, y in zip(vectors[0], vectors[2], strict=True))
    assert near > far  # Two cat sentences beat cat-vs-finance.


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
