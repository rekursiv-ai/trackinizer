"""Wire contract for the metric-grid mask query (read / bulk-write / rank).

The ``trax metric`` grammar (``trax/docs/metric-grammar.md``) is numpy
boolean-mask indexing over an experiment's ``(key, step) -> value`` grid: one
or more ``at <field> <op> <value>`` clauses AND into a mask, then ``to <value>``
assigns to the mask or a bare mask reads it. This module carries that mask, its
optional write value, and read ordering across the wire.

It is separate from :mod:`wire.wire_metrics` (the batch ``log`` ingest path):
that path is the append-only fast lane a training loop uses; this path is the
richer query/edit surface the CLI ``metric`` tail compiles to. Both live under
the ``/api/experiments`` metric family.

Kept import-pure (no ``server`` / ``trax`` / fastapi), like every wire module.
"""

from __future__ import annotations

from typing import Final, Literal, cast, get_args

import uuid

from pydantic import BaseModel, ConfigDict, Field

from trackinizer.wire.wire_metrics import MetricPoint


__all__ = [
    "EXPERIMENT_METRIC_QUERY_PATH",
    "EXPERIMENT_METRIC_WRITE_PATH",
    "METRICS_QUERY_API_PATHS",
    "METRIC_AXES",
    "METRIC_COMPARE_OPS",
    "METRIC_RANK_PATH",
    "MetricAxis",
    "MetricCompareOp",
    "MetricMaskClause",
    "MetricQueryRequest",
    "MetricQueryResponse",
    "MetricRankRequest",
    "MetricRankResponse",
    "MetricRankRow",
    "MetricReduce",
    "MetricWriteResponse",
    "experiment_metric_query_path",
    "experiment_metric_write_path",
]


type MetricAxis = Literal["key", "step", "value"]
"""A grid axis a mask clause constrains: the metric name, the step ordinal, or
the stored value."""

METRIC_AXES: Final[tuple[MetricAxis, ...]] = (
    "key",
    "step",
    "value",
)
"""Runtime tuple of every grid axis, for validation and iteration."""


type MetricCompareOp = Literal["is", "ne", "lt", "le", "gt", "ge"]
"""A metric mask comparator: equality / inequality / ordering on a grid axis.

The metric grid is neither text-regex-matchable nor nullable (every column is
NOT NULL under the PK / CHECK), so the ``re`` / ``nre`` and ``isnull`` /
``notnull`` ops of the inquiry ``FilterOp`` set are deliberately excluded. This
is the ONE definition of the metric comparator set: the CLI parser gates on it,
this wire model types ``op`` with it, and the store's operator map covers
exactly it -- so the three cannot drift into disagreement."""

METRIC_COMPARE_OPS: tuple[MetricCompareOp, ...] = cast(
    tuple[MetricCompareOp, ...], get_args(MetricCompareOp.__value__)
)
"""Runtime tuple of every metric comparator, derived from the type so the tuple
and the ``Literal`` never diverge."""


type MetricReduce = Literal["max", "min"]
"""A step-axis reduction: the highest (``max``) or lowest (``min``) step per
key. Used for "final" / "first" selections; carries no operand."""


class MetricMaskClause(BaseModel):
    """One ``at <axis> <op> <value>`` mask clause.

    ``op`` is either a :data:`MetricCompareOp` comparison (``is`` / ``ne`` /
    ``lt`` / ``le`` / ``gt`` / ``ge``), carrying a ``value``, or a step-axis
    reduction (``max`` / ``min``) with ``value`` empty. The server combines
    every clause with AND into one SQL predicate over ``experiment_metrics``.
    """

    model_config = ConfigDict(extra="forbid")

    axis: MetricAxis
    op: MetricCompareOp | MetricReduce
    value: str = ""


class MetricQueryRequest(BaseModel):
    """A mask over the metric grid plus one operation on the masked selection.

    ``masks`` AND together. ``write`` set means assign that value to every
    masked cell (a bulk upsert); ``write`` unset makes it a read, ordered by
    ``sort`` (over the value) and windowed by ``limit``. ``sort`` / ``limit``
    apply to reads only; the server rejects them alongside a ``write``.
    """

    model_config = ConfigDict(extra="forbid")

    masks: list[MetricMaskClause] = Field(default_factory=list)
    write: float | None = Field(default=None, allow_inf_nan=False)
    sort: Literal["asc", "desc"] | None = None
    limit: int | None = Field(default=None, ge=1)


class MetricQueryResponse(BaseModel):
    """The masked cells of a read, in ``(key, step)`` order."""

    points: list[MetricPoint]


class MetricWriteResponse(BaseModel):
    """How many cells a masked ``to`` write upserted.

    ``written`` counts cells newly inserted or updated by the assignment; a
    bulk write over a wide mask reports the full count so the caller sees the
    blast radius.
    """

    written: int


class MetricRankRequest(BaseModel):
    """A cross-experiment metric query: the same mask, over many experiments.

    ``experiment_ids`` are the experiments the caller preselected (from an
    inquiry list query); the ``mask`` reads/ranks their grids together. This is
    the leaderboard surface -- there is no stored ranking, only this query plus
    the ``sort`` / ``limit`` on the inner :class:`MetricQueryRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    experiment_ids: list[uuid.UUID] = Field(min_length=1)
    query: MetricQueryRequest


class MetricRankRow(BaseModel):
    """One cell of a cross-experiment read, tagged with its experiment."""

    experiment_id: uuid.UUID
    point: MetricPoint


class MetricRankResponse(BaseModel):
    """The cross-experiment masked cells, ordered by the query's sort."""

    rows: list[MetricRankRow]


EXPERIMENT_METRIC_QUERY_PATH: Final = "/api/experiments/{experiment_id}/metrics/query"
"""POST a :class:`MetricQueryRequest` to read one experiment's masked cells."""

EXPERIMENT_METRIC_WRITE_PATH: Final = "/api/experiments/{experiment_id}/metrics/write"
"""POST a :class:`MetricQueryRequest` (with ``write`` set) to assign the mask."""

METRIC_RANK_PATH: Final = "/api/metrics/rank"
"""POST a :class:`MetricRankRequest` for a cross-experiment masked read/rank."""


METRICS_QUERY_API_PATHS: tuple[str, ...] = (
    EXPERIMENT_METRIC_QUERY_PATH,
    EXPERIMENT_METRIC_WRITE_PATH,
    METRIC_RANK_PATH,
)
"""Every mask-query route template, for the drift guard (analogue of
``METRICS_API_PATHS``)."""


def experiment_metric_query_path(experiment_id: uuid.UUID) -> str:
    """The masked-read path for one experiment."""
    return EXPERIMENT_METRIC_QUERY_PATH.format(experiment_id=experiment_id)


def experiment_metric_write_path(experiment_id: uuid.UUID) -> str:
    """The masked-write path for one experiment."""
    return EXPERIMENT_METRIC_WRITE_PATH.format(experiment_id=experiment_id)
