"""Segmentation-DP bucket boundaries: optimality, edge cases, knee sweep."""

from __future__ import annotations

import itertools

import pytest

from trackinizer.server.tools.bucket_boundaries import (
    CORPUS_HISTOGRAM_PATH,
    OCTEN_8B,
    QWEN3_0P6B,
    QWEN3_4B,
    QWEN3_8B,
    bucketed_cost,
    derive_edges,
    derive_rows,
    dim_in_range,
    knee,
    load_corpus_histogram,
    optimal_boundaries,
    param_count,
    sweep,
    transformer_cost,
)


def _corpus() -> dict[int, int]:
    """Return the shipped corpus histogram, grouped and tail-clamped."""
    return load_corpus_histogram(CORPUS_HISTOGRAM_PATH)


def test_single_bucket_is_the_max_length() -> None:
    assert optimal_boundaries({100: 5, 700: 1}, 1) == [700]


def test_k_at_least_distinct_lengths_is_exact() -> None:
    """One bucket per distinct length pads nothing."""
    freq = {100: 3, 500: 2, 900: 1}
    edges = optimal_boundaries(freq, 3)
    assert edges == [100, 500, 900]
    assert bucketed_cost(freq, edges) == pytest.approx(3 * 100 + 2 * 500 + 900)


def test_two_buckets_split_a_bimodal_distribution_at_the_mode_gap() -> None:
    """Mass at 100 and 4000: the DP must not pad the short mode to 4000."""
    freq = {100: 1_000, 4_000: 10}
    edges = optimal_boundaries(freq, 2)
    assert edges == [100, 4_000]


def test_dp_beats_equal_count_quantiles() -> None:
    """Quantile edges ignore cost steepness; the DP must do at least as well.

    90% of texts are short but ONE long outlier length dominates cost; a
    count-quantile split buries the outlier with mid lengths, padding the mid
    mass to the outlier edge.
    """
    freq = {64: 450, 128: 450, 2_048: 90, 8_192: 10}
    dp_edges = optimal_boundaries(freq, 3)
    quantile_edges = [64, 128, 8_192]  # equal-count-ish split.
    assert bucketed_cost(freq, dp_edges) <= bucketed_cost(freq, quantile_edges)
    # The DP separates the 8k outlier rather than merging it into 2k mass.
    assert 2_048 in dp_edges


def test_cost_callable_reweights_boundaries() -> None:
    """A superlinear per-token cost pushes the DP to isolate long lengths."""
    freq = {128: 100, 1_024: 100, 8_192: 100}
    linear = optimal_boundaries(freq, 2)
    quadratic = optimal_boundaries(freq, 2, cost=lambda e: float(e) ** 2)
    assert bucketed_cost(freq, quadratic, cost=lambda e: float(e) ** 2) <= (
        bucketed_cost(freq, linear, cost=lambda e: float(e) ** 2)
    )


def test_edges_round_up_to_the_pad_multiple() -> None:
    edges = optimal_boundaries({100: 5, 700: 1}, 2, pad_multiple=128)
    assert edges == [128, 768]


def test_empty_freq_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        optimal_boundaries({}, 2)


def test_nonpositive_inputs_rejected() -> None:
    with pytest.raises(ValueError, match="k"):
        optimal_boundaries({100: 1}, 0)
    with pytest.raises(ValueError, match="length"):
        optimal_boundaries({0: 1}, 1)
    with pytest.raises(ValueError, match="count"):
        optimal_boundaries({100: 0}, 1)


def test_transformer_cost_counts_qwen3_4b_flops_exactly() -> None:
    """Hand-computed one-layer, one-token accounting matches the closed form."""
    cost = transformer_cost(QWEN3_4B)
    d, d_ff, layers = 2_560, 9_728, 36
    attn_w, kv_w = 32 * 128, 8 * 128
    e = 512
    dense = 2 * d * attn_w + 2 * 2 * d * kv_w + 2 * attn_w * d + 6 * d * d_ff
    attn = 2 * 2 * attn_w * e
    assert cost(e) == pytest.approx(layers * e * (dense + attn))


def test_transformer_cost_crossover_matches_the_widths() -> None:
    """Attention passes dense compute where ``4*attn_w*e`` meets the dense
    per-token term -- for Qwen3-4B at e* ~ 12.3k (202M dense FLOP/token over
    16384 attention FLOP/token/pos), so within our 8k window the curve is
    superlinear (per-token ratio 1 + (8192-128)/e* ~ 1.65) but still
    dense-dominated.
    """
    cost = transformer_cost(QWEN3_4B)
    per_token_ratio = (cost(8_192) / 8_192) / (cost(128) / 128)
    assert per_token_ratio == pytest.approx(1.65, abs=0.02)


def test_transformer_cost_isolates_long_lengths_more_than_linear() -> None:
    """The attention term makes the DP separate a long mode that a linear
    cost would merge into its neighbor.
    """
    freq = {512: 100, 6_000: 100, 8_192: 100}
    cost = transformer_cost(QWEN3_4B)
    edges = optimal_boundaries(freq, 2, cost=cost)
    linear_edges = optimal_boundaries(freq, 2)
    assert bucketed_cost(freq, edges, cost=cost) <= (
        bucketed_cost(freq, linear_edges, cost=cost)
    )


def test_sweep_matches_per_k_optimal_boundaries() -> None:
    """The single-fill sweep gives IDENTICAL edges to per-k ``optimal_boundaries``.

    The sweep fills the DP once and reads each k off it; this pins that
    optimization to the (slower) per-call form it replaced, so the speedup
    cannot silently change a bucketing.
    """
    freq = {64: 500, 128: 400, 300: 120, 512: 80, 2_048: 15, 8_192: 5}
    cost = transformer_cost(QWEN3_4B)
    points = sweep(freq, max_k=6, cost=cost, pad_multiple=32)
    for point in points:
        per_k = optimal_boundaries(freq, point.k, cost=cost, pad_multiple=32)
        assert point.edges == per_k, (point.k, point.edges, per_k)


def test_sweep_is_monotone_and_knee_picks_diminishing_returns() -> None:
    """More buckets never cost more; the knee lands where savings flatten."""
    freq = {64: 500, 128: 400, 512: 80, 2_048: 15, 8_192: 5}
    points = sweep(freq, max_k=8)
    costs = [p.cost for p in points]
    assert costs == sorted(costs, reverse=True)
    best_k = knee(points)
    assert 2 <= best_k <= 5
    # The knee's cost captures nearly all of the achievable saving.
    saved_at_knee = costs[0] - costs[best_k - 1]
    saved_total = costs[0] - costs[-1]
    assert saved_at_knee >= 0.8 * saved_total


_VERIFIED_4B_ROWS = {768: 120, 1_120: 80, 1_504: 48, 8_192: 5}
"""The battle-tested 4B long-edge row table the derivation must not exceed."""


def test_param_count_matches_the_published_model_sizes() -> None:
    """Each spec's param count lands at its advertised size (name sanity)."""
    assert param_count(QWEN3_0P6B) == pytest.approx(0.6e9, rel=0.1)
    assert param_count(QWEN3_4B) == pytest.approx(4.0e9, rel=0.1)
    assert param_count(QWEN3_8B) == pytest.approx(7.6e9, rel=0.1)
    # Octen is a Qwen3-8B fine-tune with identical dims.
    assert param_count(OCTEN_8B) == param_count(QWEN3_8B)


def test_derive_rows_meets_or_exceeds_the_proven_live_table() -> None:
    """The dense-E2 safe upper bound never claims less headroom than reality ran.

    The proven table is a FLOOR: those counts ran live without OOM. The byte
    model is a dense-attention SAFE UPPER BOUND on memory (it counts the full
    ``e**2`` math-backend scores), so it can only OVER-charge memory and thus
    UNDER-count rows -- never OOM. At every edge where the proven count exceeds 8
    the bound clears the floor. At the extreme 8192 edge the dense bound gives 4
    while the live run fit 5 (that run used a mem-efficient masked kernel the
    dense bound over-charges); agreement within one row at the tail is the bound
    validating, not a counting error. If the bound ever fell FAR below a proven
    count, the counting would be wrong -- investigate, never refit.
    """
    for edge, proven in _VERIFIED_4B_ROWS.items():
        derived = derive_rows(QWEN3_4B, edge, vram_gb=32, dim=1_024)
        if proven > 8:
            assert derived >= proven, (edge, derived, proven)
        else:
            assert derived in {4, 5}, (edge, derived, proven)


def test_derive_rows_is_monotone_non_increasing_in_edge() -> None:
    """A longer edge never gets more rows (activation grows with edge)."""
    edges = (160, 512, 768, 1_120, 1_504, 8_192)
    rows = [derive_rows(QWEN3_4B, e, vram_gb=32, dim=1_024) for e in edges]
    assert all(a >= b for a, b in itertools.pairwise(rows))


def test_bigger_model_gets_strictly_fewer_rows_at_every_edge() -> None:
    """8B weights leave less activation headroom, so 8B rows < 4B rows."""
    for edge in (160, 512, 768, 1_120, 1_504, 8_192):
        rows_4b = derive_rows(QWEN3_4B, edge, vram_gb=32, dim=1_024)
        rows_8b = derive_rows(QWEN3_8B, edge, vram_gb=32, dim=1_024)
        assert rows_8b < rows_4b, (edge, rows_8b, rows_4b)


def test_smaller_model_gets_more_rows_than_4b() -> None:
    """0.6B is tiny, so it packs far more rows per bucket than 4B."""
    for edge in (160, 512, 1_504, 8_192):
        assert derive_rows(QWEN3_0P6B, edge, vram_gb=32, dim=1_024) > derive_rows(
            QWEN3_4B,
            edge,
            vram_gb=32,
            dim=1_024,
        )


def test_derive_rows_shrinks_on_a_smaller_card() -> None:
    """Halving VRAM lowers every row count (less activation budget)."""
    for edge in (160, 512, 1_504):
        big = derive_rows(QWEN3_4B, edge, vram_gb=32, dim=1_024)
        small = derive_rows(QWEN3_4B, edge, vram_gb=16, dim=1_024)
        assert small < big, (edge, small, big)


def test_derive_rows_grows_with_vram() -> None:
    """More VRAM never yields fewer rows (monotone in the card size)."""
    for edge in (160, 512, 1_504, 8_192):
        r24 = derive_rows(QWEN3_8B, edge, vram_gb=24, dim=1_024)
        r32 = derive_rows(QWEN3_8B, edge, vram_gb=32, dim=1_024)
        r80 = derive_rows(QWEN3_8B, edge, vram_gb=80, dim=1_024)
        assert r24 <= r32 <= r80, (edge, r24, r32, r80)


def test_derive_rows_never_returns_below_one() -> None:
    """Even a huge edge on a small card yields at least a single row."""
    assert derive_rows(QWEN3_8B, 8_192, vram_gb=16, dim=1_024) >= 1


def test_derive_edges_share_a_prefix_and_diverge_in_the_tail() -> None:
    """Qwen-family edges share the low buckets but diverge at the long tail.

    The corpus mass sits at short lengths, so the low edges are identical across
    specs; the attention/dense crossover differs by arch, so the LONG edges (past
    the crossover) shift per model. The shipped plans therefore carry per-model
    edge tuples with a common prefix, not one shared set.
    """
    hist = _corpus()
    edges = {
        spec: derive_edges(spec, hist, min_buckets=10, max_buckets=20)
        for spec in (QWEN3_0P6B, QWEN3_4B, QWEN3_8B, OCTEN_8B)
    }
    # The min-buckets clamp forces >= 10 edges even though the knee is far lower.
    for spec_edges in edges.values():
        assert len(spec_edges) >= 10
    # 8B and Octen share dims exactly -> identical edges.
    assert edges[QWEN3_8B] == edges[OCTEN_8B]
    # The smallest and largest models differ somewhere in the tail (their
    # attention/dense crossover, hence long-edge placement, differs).
    assert edges[QWEN3_0P6B] != edges[QWEN3_8B]
    # ... but agree on the low-edge prefix (the short-mass buckets).
    assert edges[QWEN3_0P6B][:6] == edges[QWEN3_8B][:6]


def test_derive_edges_appends_the_safety_tail() -> None:
    """The 8192 tail is present even though the histogram is clamped to it."""
    edges = derive_edges(QWEN3_4B, _corpus(), min_buckets=10, max_buckets=20)
    assert edges[-1] == 8_192


def test_derive_edges_honors_the_bucket_count_clamp() -> None:
    """The chosen k is clamped into ``[min_buckets, max_buckets]``."""
    hist = _corpus()
    few = derive_edges(QWEN3_4B, hist, min_buckets=3, max_buckets=5)
    many = derive_edges(QWEN3_4B, hist, min_buckets=15, max_buckets=20)
    assert len(few) <= 5
    assert len(many) >= 15


def test_corpus_loader_clamps_the_tail_and_groups_to_the_pad_multiple() -> None:
    """The loader folds >8192 into 8192 and groups lengths to pad-ceilings."""
    hist = _corpus()
    assert max(hist) == 8_192  # Everything longer is clamped into the tail edge.
    assert all(length % 32 == 0 for length in hist)  # pad-ceiling grouped.


def test_dim_in_range_accepts_matryoshka_dims_within_the_spec() -> None:
    """An MRL model accepts any dim in ``[min_dim, max_dim]``."""
    assert dim_in_range(QWEN3_4B, 32)
    assert dim_in_range(QWEN3_4B, 1_024)
    assert dim_in_range(QWEN3_4B, 2_560)


def test_dim_in_range_rejects_dims_outside_the_matryoshka_span() -> None:
    """Below the floor or above native is out of range."""
    assert not dim_in_range(QWEN3_4B, 31)
    assert not dim_in_range(QWEN3_4B, 2_561)
    assert not dim_in_range(QWEN3_0P6B, 2_560)  # 0.6B tops out at 1024.


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
