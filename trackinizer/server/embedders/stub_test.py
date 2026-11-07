"""Tests for ``StubEmbedder``, the deterministic hash embedder."""

from __future__ import annotations

from typing import cast

import pytest

from trackinizer.server.embedders.stub import StubEmbedder, _xorshift_floats
from trackinizer.types.embedder import Embedder, QueryEmbedder


def test_the_default_is_384_wide_and_named_stub() -> None:
    """384 MUST stay the literal ``"stub"``: it keys existing ``inquiry_embeddings`` rows."""
    embedder = StubEmbedder()

    assert (StubEmbedder.dim, embedder.dim, embedder.name) == (384, 384, "stub")
    assert StubEmbedder(dim=384).name == "stub"


def test_another_width_carries_it_in_the_name() -> None:
    """The config knob ``stub-1024`` and the DB identity agree; 384 and 1024 never mix."""
    embedder = StubEmbedder(dim=1024)

    assert (embedder.dim, embedder.name) == (1024, "stub-1024")


def test_xorshift_is_deterministic_per_seed() -> None:
    a, b, c = _xorshift_floats(42), _xorshift_floats(42), _xorshift_floats(43)

    first = [next(a) for _ in range(5)]

    assert first == [next(b) for _ in range(5)]
    assert first != [next(c) for _ in range(5)]


@pytest.mark.parametrize("seed", [0, 1, 1 << 64])
def test_xorshift_stays_in_the_unit_interval_for_any_seed(seed: int) -> None:
    rng = _xorshift_floats(seed)

    values = [next(rng) for _ in range(50)]

    assert all(-1.0 <= v < 1.0 for v in values)
    assert len(set(values)) == 50


def test_satisfies_the_query_embedder_protocol() -> None:
    assert isinstance(StubEmbedder(), Embedder)
    assert isinstance(StubEmbedder(), QueryEmbedder)


@pytest.mark.asyncio
async def test_a_text_embeds_to_the_same_unit_vector_every_time() -> None:
    embedder = StubEmbedder(dim=16)

    first = await embedder.embed("hello")
    again = await StubEmbedder(dim=16).embed("hello")
    other = await embedder.embed("goodbye")

    assert first == again
    assert first != other
    assert len(first) == 16
    assert all(-1.0 <= v <= 1.0 for v in first)
    assert abs(cast(float, sum(v * v for v in first) ** 0.5) - 1.0) < 1e-9


@pytest.mark.asyncio
async def test_a_query_embeds_as_its_document_does() -> None:
    embedder = StubEmbedder(dim=8)

    assert await embedder.embed_query("q") == await embedder.embed("q")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
