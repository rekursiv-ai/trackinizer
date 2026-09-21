"""Optimal padding-bucket boundaries for length-bucketed batch embedding.

Given a ``{length: count}`` distribution and a bucket budget ``k``, place
bucket edges minimizing total padded cost: every item pads to its bucket's
edge, so a bucket ending at edge ``e`` costs ``count * cost(e)`` summed over
its items. Optimal edges over a sorted 1-D distribution with additive
per-segment cost is a classic segmentation DP (the same family as optimal
histogram binning), NOT an assignment problem: items are forced to the
smallest edge >= their length, so the only free choice is where segments end.

``sweep`` runs the DP for k = 1..max_k and ``knee`` picks the k where the
marginal saving flattens (largest-triangle rule), for callers that want the
bucket count chosen from the data too.

The default ``cost`` is the padded length itself (compute proportional to
tokens); pass a measured per-vector cost curve (e.g. superlinear attention
cost, or ``f(e, B*(e)) / B*(e)`` from a GPU grid measurement) to reweight.

This cost model deliberately EXCLUDES compilation cost: it is per-shape and so
grows with the bucket COUNT, not with edge placement, and is bucketing-invariant
for a warm cache. A restart-prone one-shot job (static ``torch.compile`` cold
each process) pays per-shape warmup that can dominate, so it prefers FEWER
buckets than this DP's compute-only optimum -- that trade is folded back in at
the runner (see ``server/tools/bucket_embed.py``'s edge-set choice), not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


__all__ = [
    "CORPUS_HISTOGRAM_PATH",
    "OCTEN_8B",
    "QWEN3_0P6B",
    "QWEN3_4B",
    "QWEN3_8B",
    "SweepPoint",
    "TransformerSpec",
    "bucketed_cost",
    "derive_edges",
    "derive_rows",
    "dim_in_range",
    "knee",
    "load_corpus_histogram",
    "optimal_boundaries",
    "param_count",
    "sweep",
    "transformer_cost",
]

# Batch size does NOT belong in this module, non-obviously: with a
# memory-bound batch ``B(e) = max_tokens/e``, a per-forward overhead
# amortizes to ``overhead*e/max_tokens`` per item -- proportional to ``e``,
# so it rescales every candidate bucketing identically and never moves an
# edge (and under a row cap the term goes constant, which is
# bucketing-invariant too). Batch size governs the RUNNER's per-bucket rows;
# edge placement reacts only to a genuinely non-linear per-item cost --
# asymptotic shape only, since on fixed hardware constant factors also
# cancel from the argmin. For a transformer that shape is
# :func:`transformer_cost`.


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformerSpec:
    """A model's forward-FLOP shape AND its output-dimension contract.

    The FLOP-shape fields drive :func:`transformer_cost` and :func:`derive_rows`;
    the dim fields are the SINGLE source for ``--dim`` validation (there is no
    separate supported-dims declaration). A Matryoshka model accepts any dim in
    ``[min_dim, max_dim]``; a fixed model sets ``min_dim == max_dim ==
    native_dim`` so only its native dim validates.

    Attributes:
      hidden_dim: Residual-stream width ``d``.
      intermediate_dim: Gated-MLP inner width ``d_ff``.
      num_layers: Transformer blocks.
      num_heads: Query heads.
      num_kv_heads: Key/value heads (GQA; equals ``num_heads`` for MHA).
      head_dim: Per-head width (``q`` projects to ``num_heads * head_dim``).
      native_dim: The model's full output width (used for the head-slice term).
      min_dim: Smallest valid ``--dim`` (a Matryoshka floor; ``native_dim`` when
        fixed).
      max_dim: Largest valid ``--dim`` (``native_dim`` for both MRL and fixed).

    """

    hidden_dim: int
    intermediate_dim: int
    num_layers: int
    num_heads: int
    num_kv_heads: int
    head_dim: int
    native_dim: int
    min_dim: int
    max_dim: int


QWEN3_0P6B = TransformerSpec(
    hidden_dim=1_024,
    intermediate_dim=3_072,
    num_layers=28,
    num_heads=16,
    num_kv_heads=8,
    head_dim=128,
    native_dim=1_024,
    min_dim=32,
    max_dim=1_024,
)
"""Qwen/Qwen3-Embedding-0.6B config.json (2026-09-20); MRL 32..1024 per the card
("user-defined output dimensions ranging from 32 to 1024")."""


QWEN3_4B = TransformerSpec(
    hidden_dim=2_560,
    intermediate_dim=9_728,
    num_layers=36,
    num_heads=32,
    num_kv_heads=8,
    head_dim=128,
    native_dim=2_560,
    min_dim=32,
    max_dim=2_560,
)
"""Qwen/Qwen3-Embedding-4B config.json (2026-09-20); MRL 32..2560 per the card
("user-defined output dimensions ranging from 32 to 2560")."""


QWEN3_8B = TransformerSpec(
    hidden_dim=4_096,
    intermediate_dim=12_288,
    num_layers=36,
    num_heads=32,
    num_kv_heads=8,
    head_dim=128,
    native_dim=4_096,
    min_dim=32,
    max_dim=4_096,
)
"""Qwen/Qwen3-Embedding-8B config.json (2026-09-20); MRL 32..4096 per the card
("user-defined output dimensions ranging from 32 to 4096")."""


OCTEN_8B = TransformerSpec(
    hidden_dim=4_096,
    intermediate_dim=12_288,
    num_layers=36,
    num_heads=32,
    num_kv_heads=8,
    head_dim=128,
    native_dim=4_096,
    min_dim=32,
    max_dim=4_096,
)
"""Octen/Octen-Embedding-8B config.json (2026-09-20): a Qwen3-Embedding-8B
fine-tune, identical dims (model_type ``qwen3``, architectures ``Qwen3Model``).
MRL 32..4096 inherited from Qwen3-Embedding-8B; the Octen card does not restate
the range, but the fine-tune preserves the nesting property and the repo already
ships the @1024 truncation."""


# The vocabulary size enters ONLY the weight-memory term (tied input/output
# embeddings, so counted once), not the per-token FLOP shape. Kept off
# ``TransformerSpec`` -- which is the FLOP-shape contract -- and carried here per
# model, cited to the same config.json.
_VOCAB_SIZE: Final = {
    QWEN3_0P6B: 151_669,
    QWEN3_4B: 151_669,
    QWEN3_8B: 151_665,
    OCTEN_8B: 151_665,
}


def dim_in_range(spec: TransformerSpec, dim: int) -> bool:
    """Whether ``dim`` is a valid output width for ``spec``.

    The spec is the single dim-contract source: a Matryoshka model accepts any
    ``dim`` in ``[min_dim, max_dim]``; a fixed model (``min_dim == max_dim``)
    accepts only its native dim.

    Args:
      spec: The model's spec, e.g. :data:`QWEN3_4B`.
      dim: The requested output dimension.

    Returns:
      valid: ``True`` when ``dim`` is within the spec's supported range.

    """
    return spec.min_dim <= dim <= spec.max_dim


def param_count(spec: TransformerSpec) -> int:
    """Return the model's parameter count (for the weight-memory term).

    Counts per-layer attention projections (q/k/v/o) and the gated MLP's three
    matrices, plus one tied token-embedding table (Qwen3-Embedding ties input and
    output embeddings). Used only to size ``params * dtype_bytes`` weight memory
    in :func:`derive_rows`; the FLOP shape is :func:`transformer_cost`.

    Args:
      spec: The model's shape parameters, e.g. :data:`QWEN3_4B`.

    Returns:
      params: Total parameter count.

    """
    d = spec.hidden_dim
    attn_width = spec.num_heads * spec.head_dim
    kv_width = spec.num_kv_heads * spec.head_dim
    per_layer = (
        d * attn_width  # Q.
        + 2 * d * kv_width  # `k`, v.
        + attn_width * d  # O.
        + 3 * d * spec.intermediate_dim  # Gated MLP: gate, up, down.
    )
    return spec.num_layers * per_layer + _VOCAB_SIZE[spec] * d


def transformer_cost(spec: TransformerSpec) -> Callable[[int], float]:
    """Return per-item forward FLOPs at padded length ``e`` for the DP.

    Exact dense-forward accounting per layer, per token (multiply-accumulate
    counted as 2 FLOPs):

    - projections: ``q`` ``2*d*(n_h*h)``, ``k``/``v`` ``2*d*(n_kv*h)`` each,
      ``o`` ``2*(n_h*h)*d``;
    - attention: scores + weighted values, ``2 * 2 * e * (n_h*h)`` --
      the only term carrying an extra factor of ``e``;
    - gated MLP: three ``d x d_ff`` matmuls, ``3 * 2 * d * d_ff``.

    Absolute scale cancels from the DP's argmin on fixed hardware; what moves
    edges is the RATIO of the attention term to the rest, which this counts
    exactly rather than approximating (a loose ``e + e^2/d`` shape misplaces
    the linear-to-quadratic crossover for GQA models, where attention width
    ``n_h*h`` differs from ``d``).

    Args:
      spec: The model's shape parameters, e.g. :data:`QWEN3_4B`.

    Returns:
      cost: Per-item FLOPs at padded length ``e``.

    """
    d = spec.hidden_dim
    attn_width = spec.num_heads * spec.head_dim
    kv_width = spec.num_kv_heads * spec.head_dim
    per_token_dense = (
        2 * d * attn_width  # Q.
        + 2 * 2 * d * kv_width  # `k`, v.
        + 2 * attn_width * d  # O.
        + 3 * 2 * d * spec.intermediate_dim  # Gated MLP.
    )
    per_token_attn = 2 * 2 * attn_width  # Scores + weighted values, per kv pos.

    def cost(edge: int) -> float:
        return spec.num_layers * edge * (per_token_dense + per_token_attn * edge)

    return cost


# The fraction of the non-weight VRAM the derivation may fill. The ONE named
# tolerance (not a fitted constant): it covers the CUDA context, the compile /
# inductor workspace, the SDPA kernel scratch, and allocator fragmentation --
# the overhead around the otherwise-exact per-row byte count. Sized by
# MEASUREMENT, not guess: at 0.9 a live 4B run held 28.65 GiB on a 32 GiB card at
# the derived rows and OOM'd on a 6.6 GiB allocation, implying ~3.5 GiB of
# unmodeled overhead (~11% of the card). 0.8 budgets ~6.4 GiB of the 32 GiB to
# that overhead -- comfortably over the measured ~3.5 GiB -- so rows drop ~11%
# and clear the ceiling (measured 2026-09-20). Rows scale linearly with this.
_VRAM_MARGIN: Final = 0.8


def derive_rows(
    spec: TransformerSpec,
    edge: int,
    *,
    vram_gb: float,
    dim: int,
    dtype_bytes: int = 2,
) -> int:
    """Return the safe batch row count for ``spec`` at bucket ``edge``.

    Pure arithmetic, no fitted constants: weight memory is ``param_count(spec) *
    dtype_bytes``; the rest of the card, times :data:`_VRAM_MARGIN`, is the
    activation budget; per-row cost is the exact peak-layer byte count
    (:func:`_bytes_per_row`) plus the pooled ``dim``-wide head slice. A bigger
    model (more weights) or a smaller card shrinks the budget; a longer edge
    raises the per-row cost. Never returns below one.

    Args:
      spec: The model's shape parameters, e.g. :data:`QWEN3_4B`.
      edge: The bucket's padded sequence length.
      vram_gb: The card's total memory in GiB-scale gigabytes (``32`` for a 5090).
      dim: The (truncated) output width; sizes the pooled head slice per row.
      dtype_bytes: Bytes per element (fp16 == 2), for both weights and activation.

    Returns:
      rows: Rows per forward for this ``(spec, edge, card)``.

    """
    weight_bytes = param_count(spec) * dtype_bytes
    budget = (vram_gb * 1e9 - weight_bytes) * _VRAM_MARGIN
    per_row = _bytes_per_row(spec, edge, dtype_bytes) + dim * dtype_bytes
    return max(1, int(budget / per_row))


_CWD: Final = Path(__file__).resolve().parent

# The exact per-length corpus token histogram shipped as a data fixture (a small
# deterministic golden, well under the 750 KiB source-control bound). The loader
# groups lengths to the pad multiple and clamps the tail; see
# :func:`load_corpus_histogram`.
CORPUS_HISTOGRAM_PATH: Final = _CWD / "testdata" / "corpus_token_freq_2026-09-20.txt"

# The runtime routes up to this tail edge; the corpus is clamped into it (a
# truncation edge, not data loss -- the runner truncates every text at 8192).
_TAIL_EDGE: Final = 8_192

# The DP's edge granularity, matching the runner's compile-shape rounding. Also
# the grouping granularity: two lengths sharing a pad-ceiling are indistinguishable
# to the objective (both pad to the same edge), so grouping is lossless for edge
# placement and collapses ~5.6k distinct lengths to ~256 for a tractable DP.
_EDGE_PAD_MULTIPLE: Final = 32


def load_corpus_histogram(path: Path) -> dict[int, int]:
    """Load a ``length|count`` corpus file into a DP-ready histogram.

    Reads the exact per-length token counts, clamps every length beyond
    :data:`_TAIL_EDGE` into that edge (the runner truncates there, so those texts
    genuinely pad to the tail bucket -- not data loss), and groups lengths to
    their :data:`_EDGE_PAD_MULTIPLE` pad-ceiling. Grouping is LOSSLESS for edge
    placement: lengths sharing a pad-ceiling always route to the same edge and
    pad identically, so the DP objective cannot tell them apart, while ``n``
    drops from thousands of distinct lengths to hundreds (a tractable DP).

    Args:
      path: A file of ``#``-comment lines then ``length|count`` rows.

    Returns:
      histogram: Pad-ceiling length -> summed count, tail-clamped.

    """
    histogram: dict[int, int] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        length_text, count_text = line.split("|")
        length = min(int(length_text), _TAIL_EDGE)
        grouped = -(-length // _EDGE_PAD_MULTIPLE) * _EDGE_PAD_MULTIPLE
        histogram[grouped] = histogram.get(grouped, 0) + int(count_text)
    return histogram


def derive_edges(
    spec: TransformerSpec,
    histogram: Mapping[int, int],
    *,
    min_buckets: int,
    max_buckets: int,
) -> tuple[int, ...]:
    """Return the per-model default bucket edges over ``histogram``.

    Runs the segmentation DP under ``spec``'s exact forward-cost curve, sweeping
    bucket counts ``1..max_buckets``; the chosen count is the knee (where added
    buckets stop paying) clamped into ``[min_buckets, max_buckets]``. Appends the
    :data:`_TAIL_EDGE` safety bucket when the DP does not already end there. The
    low edges are shared across the Qwen family (short-mass buckets); the long
    edges diverge by arch (the attention/dense crossover differs per model).

    Args:
      spec: The model's shape parameters, e.g. :data:`QWEN3_4B`.
      histogram: Token-length -> count (see :func:`load_corpus_histogram`).
      min_buckets: Lower clamp on the bucket count (finer than the knee).
      max_buckets: Upper clamp / sweep ceiling.

    Returns:
      edges: Ascending bucket edges, ending at :data:`_TAIL_EDGE`.

    """
    cost = transformer_cost(spec)
    points = sweep(
        histogram,
        max_k=max_buckets,
        cost=cost,
        pad_multiple=_EDGE_PAD_MULTIPLE,
    )
    chosen_k = min(max_buckets, max(min_buckets, knee(points)))
    edges = optimal_boundaries(
        histogram,
        chosen_k,
        cost=cost,
        pad_multiple=_EDGE_PAD_MULTIPLE,
    )
    if edges[-1] < _TAIL_EDGE:
        edges.append(_TAIL_EDGE)
    return tuple(edges)


def optimal_boundaries(
    freq: Mapping[int, int],
    k: int,
    *,
    cost: Callable[[int], float] = float,
    pad_multiple: int = 1,
) -> list[int]:
    """Return at most ``k`` bucket edges minimizing total padded cost.

    Args:
      freq: Item length -> occurrence count. Lengths and counts positive.
      k: Maximum number of buckets; fewer are returned when the distribution
        has fewer distinct lengths.
      cost: Per-item cost of padding to an edge. Defaults to the edge itself
        (compute linear in padded tokens); pass a measured curve to reweight.
      pad_multiple: Round each edge up to this multiple (compile-shape
        granularity). ``1`` keeps exact lengths.

    Returns:
      edges: Ascending bucket edges; the last covers the maximum length.

    Raises:
      ValueError: ``freq`` is empty, or any length/count/k is non-positive.

    """
    if not freq:
        raise ValueError("freq is empty")
    if k < 1:
        raise ValueError(f"k must be positive, got {k}")
    for length, count in freq.items():
        if length < 1:
            raise ValueError(f"length must be positive, got {length}")
        if count < 1:
            raise ValueError(f"count must be positive, got {count}")
    lengths = sorted(freq)
    counts = [freq[length] for length in lengths]
    n = len(lengths)
    k = min(k, n)
    # prefix[i] = total count of the first i lengths.
    prefix = [0] * (n + 1)
    for i, count in enumerate(counts):
        prefix[i + 1] = prefix[i] + count

    def edge(j: int) -> int:
        """Return the padded edge for a segment ending at sorted index ``j``."""
        return -(-lengths[j] // pad_multiple) * pad_multiple

    def segment_cost(i: int, j: int) -> float:
        """Cost of lengths i..j (inclusive) padded to ``edge(j)``."""
        return (prefix[j + 1] - prefix[i]) * cost(edge(j))

    # dp[b][j] = min cost of covering the first j+1 lengths with b+1 buckets.
    unset = float("inf")
    dp = [[unset] * n for _ in range(k)]
    cut = [[0] * n for _ in range(k)]
    for j in range(n):
        dp[0][j] = segment_cost(0, j)
    for b in range(1, k):
        for j in range(b, n):
            for i in range(b - 1, j):
                candidate = dp[b - 1][i] + segment_cost(i + 1, j)
                if candidate < dp[b][j]:
                    dp[b][j] = candidate
                    cut[b][j] = i
    # Fewer buckets can never help (a split is free at worst), so take the
    # best over <= k buckets and reconstruct.
    best_b = min(range(k), key=lambda b: dp[b][n - 1])
    edges: list[int] = []
    j = n - 1
    for b in range(best_b, 0, -1):
        edges.append(edge(j))
        j = cut[b][j]
    edges.append(edge(j))
    return sorted(set(edges))


def bucketed_cost(
    freq: Mapping[int, int],
    edges: list[int],
    *,
    cost: Callable[[int], float] = float,
) -> float:
    """Return the total padded cost of ``freq`` under ``edges``.

    Args:
      freq: Item length -> occurrence count.
      edges: Ascending bucket edges; the last must cover the max length.
      cost: Per-item cost of padding to an edge (see
        :func:`optimal_boundaries`).

    Returns:
      total: Sum over items of ``cost(smallest edge >= length)``.

    Raises:
      ValueError: A length exceeds the last edge.

    """
    ordered = sorted(edges)
    total = 0.0
    for length, count in freq.items():
        target = next((e for e in ordered if e >= length), None)
        if target is None:
            raise ValueError(f"length {length} exceeds the last edge {ordered[-1]}")
        total += count * cost(target)
    return total


@dataclass(frozen=True, slots=True, kw_only=True)
class SweepPoint:
    """One bucket-count candidate: its optimal edges and their total cost."""

    k: int
    edges: list[int]
    cost: float


def sweep(
    freq: Mapping[int, int],
    *,
    max_k: int,
    cost: Callable[[int], float] = float,
    pad_multiple: int = 1,
) -> list[SweepPoint]:
    """Return the optimal solution for every bucket count 1..max_k.

    Args:
      freq: Item length -> occurrence count.
      max_k: Largest bucket count to evaluate.
      cost: Per-item cost of padding to an edge (see
        :func:`optimal_boundaries`).
      pad_multiple: Edge granularity (see :func:`optimal_boundaries`).

    Returns:
      points: One :class:`SweepPoint` per k, ascending; costs non-increasing.

    """
    # One DP fill to ``max_k`` buckets, read off per k -- NOT ``max_k`` calls to
    # ``optimal_boundaries``, each of which refills the whole table from scratch
    # (O(max_k) redundant work). ``dp[b][j]`` is the min cost of the first j+1
    # lengths in b+1 buckets; the same table serves every k, so the sweep costs
    # one fill. Output is identical to the per-call form (asserted in the test).
    if not freq:
        raise ValueError("freq is empty")
    if max_k < 1:
        raise ValueError(f"max_k must be positive, got {max_k}")
    for length, count in freq.items():
        if length < 1:
            raise ValueError(f"length must be positive, got {length}")
        if count < 1:
            raise ValueError(f"count must be positive, got {count}")
    lengths = sorted(freq)
    counts = [freq[length] for length in lengths]
    n = len(lengths)
    top = min(max_k, n)
    prefix = [0] * (n + 1)
    for i, count in enumerate(counts):
        prefix[i + 1] = prefix[i] + count

    def edge(j: int) -> int:
        return -(-lengths[j] // pad_multiple) * pad_multiple

    def segment_cost(i: int, j: int) -> float:
        return (prefix[j + 1] - prefix[i]) * cost(edge(j))

    unset = float("inf")
    dp = [[unset] * n for _ in range(top)]
    cut = [[0] * n for _ in range(top)]
    for j in range(n):
        dp[0][j] = segment_cost(0, j)
    for b in range(1, top):
        for j in range(b, n):
            for i in range(b - 1, j):
                candidate = dp[b - 1][i] + segment_cost(i + 1, j)
                if candidate < dp[b][j]:
                    dp[b][j] = candidate
                    cut[b][j] = i

    edges_by_index = [edge(j) for j in range(n)]
    points: list[SweepPoint] = []
    for k in range(1, max_k + 1):
        # Fewer buckets never cost more, so k's solution is the best over <= k
        # (matching ``optimal_boundaries`` exactly). Beyond ``n`` distinct
        # lengths, extra buckets add nothing: the row saturates at ``top``.
        reach = min(k, top)
        best_b = min(range(reach), key=lambda b: dp[b][n - 1])
        edges = _reconstruct_edges(cut, edges_by_index, n, best_b)
        points.append(
            SweepPoint(k=k, edges=edges, cost=bucketed_cost(freq, edges, cost=cost)),
        )
    return points


def knee(points: list[SweepPoint]) -> int:
    """Return the k at the sweep's knee (largest-triangle rule).

    The knee is the point with maximum perpendicular distance from the line
    joining the sweep's endpoints in (k, cost) space -- where adding buckets
    stops paying. With fewer than three points the largest k wins trivially.

    Args:
      points: A :func:`sweep` result (ascending k).

    Returns:
      k: The chosen bucket count.

    """
    if len(points) < 3:
        return points[-1].k
    first, last = points[0], points[-1]
    dk = last.k - first.k
    dc = last.cost - first.cost
    norm = (dk * dk + dc * dc) ** 0.5
    if norm == 0:
        return first.k
    best = max(
        points,
        key=lambda p: abs(dk * (first.cost - p.cost) - dc * (first.k - p.k)) / norm,
    )
    return best.k


# Exact tensor accounting for one Qwen3 decoder layer under ``no_grad`` (mirrors
# :func:`transformer_cost`'s FLOP accounting), from the real ``transformers`` forward:
#
# - Residual + current hidden stream: ``2 * e * hidden`` (the residual persists across
# the executing sublayer). - The layer's PEAK sublayer temporaries -- ``max`` of: -
# Attention: q/k/v pre-SDPA (``e * (num_heads + 2*num_kv_heads) * head_dim``) plus the
# ``e * num_heads * head_dim`` attention output, PLUS the ``e**2 * num_heads`` scores
# matrix. The bucket runner passes a left-padding ``attention_mask``
# (``_HfTokenBatcher.pad_batch``); a non-None additive mask drives F.sdpa to the math
# backend, which MATERIALIZES the ``(rows, num_heads, e, e)`` scores. Counting it is
# both correct for this path and the safe upper bound (the fused kernels use less), so
# it never under-budgets. - Gated MLP: ``act_fn(gate_proj(x))`` and ``up_proj(x)`` --
# two ``e * intermediate_dim`` tensors coexist for the elementwise multiply.
#
# Under ``no_grad`` only ONE layer's temporaries are live at once (sequential layers, no
# saved-for-backward tape), so peak is a per-layer max, not a sum over layers. The
# pooled head slice (``rows * native_dim``) is added ONCE by the caller -- it is ~4 MB
# at the busiest bucket, <0.02% of budget -- so it is folded into :func:`derive_rows`'s
# budget, not this per-layer term.
def _bytes_per_row(spec: TransformerSpec, edge: int, dtype_bytes: int) -> int:
    """Return the PEAK activation bytes one row occupies in a layer's forward."""
    # TODO(gpu-probe): when a GPU is free, measure true OOM rows at 8192 with an
    # is_causal FUSED masked kernel (no e**2 materialization). If fused proves
    # out, a documented fused branch (drop the ``scores`` term) can replace this
    # dense bound and buy back the tail rows the safe over-count currently costs.
    attn_width = spec.num_heads * spec.head_dim
    kv_width = spec.num_kv_heads * spec.head_dim
    resid_io = 2 * edge * spec.hidden_dim
    scores = edge * edge * spec.num_heads  # (num_heads, e, e) math-backend scores.
    attn_tmp = edge * (attn_width + 2 * kv_width) + edge * attn_width + scores
    mlp_tmp = 2 * edge * spec.intermediate_dim
    return (resid_io + max(attn_tmp, mlp_tmp)) * dtype_bytes


def _reconstruct_edges(
    cut: list[list[int]],
    edges_by_index: list[int],
    n: int,
    best_b: int,
) -> list[int]:
    """Walk the DP ``cut`` table back to the ascending edge set for ``best_b``."""
    edges: list[int] = []
    j = n - 1
    for b in range(best_b, 0, -1):
        edges.append(edges_by_index[j])
        j = cut[b][j]
    edges.append(edges_by_index[j])
    return sorted(set(edges))
