"""Per-model bucket plans: shipped literals stay in step with the generator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import logging

import pytest

from trackinizer.server.embedders import registry
from trackinizer.server.tools import bucket_boundaries, model_buckets


if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def cold_edges() -> Generator[None]:
    """Run the generator even when another model or test populated its cache."""
    bucket_boundaries._cached_derive_edges.cache_clear()
    try:
        yield
    finally:
        bucket_boundaries._cached_derive_edges.cache_clear()


def _corpus() -> dict[int, int]:
    return bucket_boundaries.load_corpus_histogram(
        bucket_boundaries.CORPUS_HISTOGRAM_PATH,
    )


def test_every_registry_key_has_a_plan_or_an_explicit_fallback() -> None:
    """No registry model is silently missing from the bucket map.

    A model absent here would fall through to unknown-model handling; the map
    must name every key, either with a real plan or an explicit conservative
    marker, so a new embedder cannot ship without a deliberate bucket decision.
    """
    for name in registry.EMBEDDERS:
        assert name in model_buckets.MODEL_BUCKETS, name


@pytest.mark.parametrize(
    "name",
    [
        name
        for name, plan in model_buckets.MODEL_BUCKETS.items()
        if isinstance(plan, model_buckets.BucketPlan)
    ],
)
def test_shipped_edges_equal_the_generator_output(
    name: str,
    cold_edges: None,
) -> None:
    """The shipped edge tuples EQUAL the DP over the shipped histogram.

    The drift gate: regenerating the table is a conscious act. A hand-edited
    literal that no longer matches ``derive_edges`` fails here, so stale
    constants are impossible. Every model runs a cold generator independently.
    """
    del cold_edges
    plan = model_buckets.MODEL_BUCKETS[name]
    assert isinstance(plan, model_buckets.BucketPlan)
    expected = bucket_boundaries.derive_edges(
        model_buckets.SPEC_FOR_MODEL[name],
        _corpus(),
        min_buckets=model_buckets.DEFAULT_MIN_BUCKETS,
        max_buckets=model_buckets.DEFAULT_MAX_BUCKETS,
    )
    assert plan.edges == expected, (name, plan.edges, expected)


def test_shipped_reference_rows_equal_the_generator_at_32gb() -> None:
    """The shipped reference rows EQUAL ``derive_rows`` at the reference VRAM/dim."""
    for name, plan in model_buckets.MODEL_BUCKETS.items():
        if not isinstance(plan, model_buckets.BucketPlan):
            continue
        spec = model_buckets.SPEC_FOR_MODEL[name]
        for edge in plan.edges:
            expected = bucket_boundaries.derive_rows(
                spec,
                edge,
                vram_gb=model_buckets.REFERENCE_VRAM_GB,
                dim=plan.reference_dim,
            )
            assert plan.reference_rows[edge] == expected, (name, edge)


def test_the_jina_models_are_conservative_fallbacks() -> None:
    """Jina models have no plan: their custom encode path can't use buckets."""
    for name in (
        "jina-embeddings-v5-text-nano@768",
        "jina-embeddings-v5-text-small@1024",
    ):
        assert model_buckets.MODEL_BUCKETS[name] is model_buckets.CONSERVATIVE


def test_resolve_plan_returns_edges_and_live_rows_for_a_known_model() -> None:
    """A known model resolves to its edges and rows derived at the given VRAM."""
    edges, rows = model_buckets.resolve_plan(
        "qwen3-embedding-4b@1024",
        vram_gb=32,
        dim=1_024,
    )
    spec = model_buckets.SPEC_FOR_MODEL["qwen3-embedding-4b@1024"]
    assert edges == bucket_boundaries.derive_edges(
        spec,
        _corpus(),
        min_buckets=model_buckets.DEFAULT_MIN_BUCKETS,
        max_buckets=model_buckets.DEFAULT_MAX_BUCKETS,
    )
    assert rows == {
        e: bucket_boundaries.derive_rows(spec, e, vram_gb=32, dim=1_024) for e in edges
    }


def test_resolve_plan_scales_rows_with_vram() -> None:
    """More VRAM yields at least as many rows at every edge (monotone)."""
    _e, rows_24 = model_buckets.resolve_plan(
        "qwen3-embedding-8b@1024",
        vram_gb=24,
        dim=1_024,
    )
    _e, rows_32 = model_buckets.resolve_plan(
        "qwen3-embedding-8b@1024",
        vram_gb=32,
        dim=1_024,
    )
    _e, rows_80 = model_buckets.resolve_plan(
        "qwen3-embedding-8b@1024",
        vram_gb=80,
        dim=1_024,
    )
    for edge in rows_32:
        assert rows_24[edge] <= rows_32[edge] <= rows_80[edge], edge
    assert rows_24[160] < rows_80[160]


def test_resolve_plan_at_reference_vram_matches_the_shipped_rows() -> None:
    """Resolving at 32 GB / reference dim reproduces the shipped reference table."""
    plan = model_buckets.MODEL_BUCKETS["qwen3-embedding-4b@1024"]
    assert isinstance(plan, model_buckets.BucketPlan)
    _edges, rows = model_buckets.resolve_plan(
        "qwen3-embedding-4b@1024",
        vram_gb=model_buckets.REFERENCE_VRAM_GB,
        dim=plan.reference_dim,
    )
    assert rows == plan.reference_rows


def test_resolve_plan_defaults_dim_to_the_registered_identity() -> None:
    """Omitting dim uses the model's registered @dim (the reference dim)."""
    edges_default, rows_default = model_buckets.resolve_plan(
        "qwen3-embedding-4b@1024",
        vram_gb=32,
    )
    edges_explicit, rows_explicit = model_buckets.resolve_plan(
        "qwen3-embedding-4b@1024",
        vram_gb=32,
        dim=1_024,
    )
    assert edges_default == edges_explicit
    assert rows_default == rows_explicit


def test_resolve_plan_uses_nondefault_output_dimensions() -> None:
    """Explicit output widths change the head memory and derived row counts."""
    name = "qwen3-embedding-4b@1024"
    spec = model_buckets.SPEC_FOR_MODEL[name]
    default_edges, default_rows = model_buckets.resolve_plan(name, vram_gb=32)
    for dim in (spec.min_dim, spec.max_dim):
        edges, rows = model_buckets.resolve_plan(name, vram_gb=32, dim=dim)
        assert edges == default_edges
        assert rows == {
            edge: bucket_boundaries.derive_rows(spec, edge, vram_gb=32, dim=dim)
            for edge in edges
        }
        assert rows != default_rows


def test_resolve_plan_falls_back_conservatively_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown / fallback model logs WHY and returns a conservative plan.

    Mutation guard: the warning must name the cause (no bucketed batch method)
    so a future operator reads cause, not just absence.
    """
    with caplog.at_level(logging.WARNING):
        edges, rows = model_buckets.resolve_plan(
            "jina-embeddings-v5-text-nano@768",
            vram_gb=32,
        )
    assert edges == model_buckets.CONSERVATIVE_EDGES
    assert all(rows[e] <= model_buckets.CONSERVATIVE_MAX_ROWS for e in edges)
    message = caplog.text
    assert "jina-embeddings-v5-text-nano@768" in message
    assert "bucketed" in message.lower()


def test_conservative_fallback_rows_are_small() -> None:
    """The fallback caps rows low: no model's real tuning leaks to an unknown."""
    _edges, rows = model_buckets.resolve_plan("something-unregistered@1024", vram_gb=80)
    assert max(rows.values()) <= model_buckets.CONSERVATIVE_MAX_ROWS


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
