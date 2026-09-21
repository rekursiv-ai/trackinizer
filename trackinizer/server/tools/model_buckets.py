"""Precomputed per-model bucket plans for the GPU backfill runner.

Setting ``--model X`` picks up that model's own bucket tuning; this module ships
the plans as literal constants (:data:`MODEL_BUCKETS`) so the reference table is
a single auditable artifact. The derivation in :mod:`bucket_boundaries` is the
GENERATOR: ``model_buckets_test.py`` asserts the shipped literals EQUAL what the
generator produces from the shipped inputs (the corpus histogram + each model's
:class:`~bucket_boundaries.TransformerSpec`), so regenerating the table is a
conscious act and a silently stale constant is impossible.

Edges are corpus + architecture driven and VRAM-INDEPENDENT: the DP over the
corpus token histogram under each spec's forward-cost curve, with the bucket
count clamped into ``[DEFAULT_MIN_BUCKETS, DEFAULT_MAX_BUCKETS]``. The low edges
are shared across the Qwen family (short-mass buckets); the long edges diverge by
arch (the attention/dense crossover differs per model), so plans carry per-model
edge tuples.

Rows ARE VRAM- and dim-dependent. The shipped ``reference_rows`` are the exact
byte-accounting derivation at :data:`REFERENCE_VRAM_GB` (a 5090's 32 GB) and each
model's registered ``reference_dim``, the documented default for tests and for
planning contexts with no CUDA. The runner recomputes rows LIVE from the measured
card and the chosen ``--dim`` via :func:`resolve_plan`, so a 24 GB or 80 GB card
(or a Matryoshka dim below native) gets correct counts, not 5090/1024 constants.

Jina models carry no plan (:data:`CONSERVATIVE`): their custom ``encode`` path
does not route through ``embed_bucketed_batch``, so a precomputed table would be
dead config. If a Jina model ever gains the bucketed method, adding a spec here
is a one-liner (a ``TransformerSpec`` in ``bucket_boundaries`` + an entry below).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import logging

from trackinizer.server.embedders import registry
from trackinizer.server.tools import bucket_boundaries


if TYPE_CHECKING:
    from trackinizer.server.tools.bucket_boundaries import TransformerSpec


__all__ = [
    "CONSERVATIVE",
    "CONSERVATIVE_EDGES",
    "CONSERVATIVE_MAX_ROWS",
    "DEFAULT_MAX_BUCKETS",
    "DEFAULT_MIN_BUCKETS",
    "MODEL_BUCKETS",
    "REFERENCE_VRAM_GB",
    "SPEC_FOR_MODEL",
    "BucketPlan",
    "resolve_plan",
]


_LOGGER: Final = logging.getLogger(__name__)

# The card the shipped ``reference_rows`` are derived for (a 5090). The runner
# overrides this with the measured device memory; this is the no-CUDA default.
REFERENCE_VRAM_GB: Final = 32.0

# The bucket-count clamp for the edge DP: the knee is far lower than 10 on this
# corpus (short-mass dominated), so the min forces a finer, more useful set; the
# max caps compile-shape count (each shape is a separate static compile).
DEFAULT_MIN_BUCKETS: Final = 10
DEFAULT_MAX_BUCKETS: Final = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class BucketPlan:
    """A model's precomputed bucket edges and reference row counts.

    Attributes:
      edges: Ascending static bucket edges (corpus + arch driven, VRAM-free).
      reference_dim: The registered ``@dim`` the reference rows were derived at.
      reference_rows: ``edge -> rows`` at :data:`REFERENCE_VRAM_GB` and
        ``reference_dim``; the runner recomputes rows for the live card/dim but
        ships these as the default.

    """

    edges: tuple[int, ...]
    reference_dim: int
    reference_rows: Mapping[int, int]


# The registry keys whose backfill path routes through the bucketed core, mapped
# to the spec the generator derives from. DERIVED from the registry's ``_MODELS``
# (via :func:`registry.bucket_specs`) so there is ONE source of model metadata --
# no second spec table to drift. Jina keys are absent (fixed-dim, ``spec is
# None``); they carry no bucket plan and resolve to CONSERVATIVE.
SPEC_FOR_MODEL: Final[Mapping[str, TransformerSpec]] = registry.bucket_specs()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Conservative:
    """Marker: this model has no bucket plan; use the conservative fallback."""


# The sentinel a registry key maps to when it deliberately has no plan.
CONSERVATIVE: Final = _Conservative()

# The fallback edges: the same corpus-driven set, so an unknown model still
# routes sanely; only the rows are capped low.
CONSERVATIVE_EDGES: Final[tuple[int, ...]] = (160, 512, 1504, 8192)

# The fallback row cap. Small on purpose: an unknown model must NEVER inherit a
# tuned model's aggressive counts and OOM, so every fallback bucket stays tiny.
CONSERVATIVE_MAX_ROWS: Final = 8


# Every registry key names a deliberate bucket decision: a precomputed plan, or
# CONSERVATIVE. The plans below are LITERALS; ``model_buckets_test`` proves each
# equals the generator's output from the shipped inputs (the drift gate).
MODEL_BUCKETS: Final[Mapping[str, BucketPlan | _Conservative]] = {
    "qwen3-embedding-0.6b@1024": BucketPlan(
        edges=(64, 160, 288, 512, 704, 992, 1792, 3584, 5376, 8192),
        reference_dim=1024,
        reference_rows={
            64: 20_857,
            160: 7159,
            288: 3342,
            512: 1468,
            704: 899,
            992: 516,
            1792: 186,
            3584: 52,
            5376: 24,
            8192: 10,
        },
    ),
    "qwen3-embedding-4b@1024": BucketPlan(
        edges=(64, 160, 288, 512, 704, 992, 1760, 3584, 5376, 8192),
        reference_dim=1024,
        reference_rows={
            64: 6088,
            160: 2436,
            288: 1353,
            512: 589,
            704: 359,
            992: 205,
            1760: 75,
            3584: 20,
            5376: 9,
            8192: 4,
        },
    ),
    "qwen3-embedding-8b@1024": BucketPlan(
        edges=(64, 160, 288, 512, 704, 992, 1760, 3584, 5376, 8192),
        reference_dim=1024,
        reference_rows={
            64: 3215,
            160: 1286,
            288: 714,
            512: 378,
            704: 233,
            992: 135,
            1760: 51,
            3584: 14,
            5376: 6,
            8192: 2,
        },
    ),
    "octen-embedding-8b@1024": BucketPlan(
        edges=(64, 160, 288, 512, 704, 992, 1760, 3584, 5376, 8192),
        reference_dim=1024,
        reference_rows={
            64: 3215,
            160: 1286,
            288: 714,
            512: 378,
            704: 233,
            992: 135,
            1760: 51,
            3584: 14,
            5376: 6,
            8192: 2,
        },
    ),
    "jina-embeddings-v5-text-nano@768": CONSERVATIVE,
    "jina-embeddings-v5-text-small@1024": CONSERVATIVE,
}


def resolve_plan(
    name: str,
    *,
    vram_gb: float,
    dim: int | None = None,
) -> tuple[tuple[int, ...], dict[int, int]]:
    """Return ``(edges, rows)`` for ``name`` on a card of ``vram_gb`` at ``dim``.

    A model with a precomputed plan gets its shipped edges and rows derived LIVE
    for ``vram_gb`` and ``dim`` (so a non-5090 card or a sub-native dim is
    correct). ``dim`` defaults to the plan's registered ``reference_dim``. A model
    marked :data:`CONSERVATIVE`, or one absent from the map, gets the small
    fallback and a WARNING naming the cause -- so an operator reads why buckets do
    not apply, not just that they are missing.

    Args:
      name: A registry key (a model's stored identity).
      vram_gb: The target card's total memory in GiB-scale gigabytes.
      dim: The output width for the head-slice term; defaults to the registered
        ``reference_dim``.

    Returns:
      edges: Ascending static bucket edges.
      rows: ``edge -> rows per forward`` for this ``(model, card, dim)``.

    """
    plan = MODEL_BUCKETS.get(name)
    if isinstance(plan, BucketPlan):
        spec = SPEC_FOR_MODEL[name]
        chosen_dim = plan.reference_dim if dim is None else dim
        rows = {
            edge: bucket_boundaries.derive_rows(
                spec,
                edge,
                vram_gb=vram_gb,
                dim=chosen_dim,
            )
            for edge in plan.edges
        }
        return plan.edges, rows
    _LOGGER.warning(
        "Model %r has no bucket plan (its custom encode path exposes no "
        "embed_bucketed_batch), so the backfill uses the conservative fallback "
        "(<=%d rows/bucket), never another model's tuning. Add a TransformerSpec "
        "and a MODEL_BUCKETS entry if it gains the bucketed method.",
        name,
        CONSERVATIVE_MAX_ROWS,
    )
    fallback_rows: dict[int, int] = dict.fromkeys(
        CONSERVATIVE_EDGES,
        CONSERVATIVE_MAX_ROWS,
    )
    return CONSERVATIVE_EDGES, fallback_rows
