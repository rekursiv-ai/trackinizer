"""Wire contract for experiment-metric ingest.

Metrics logged against an :class:`Experiment` run flow to trackinizer
through two endpoints -- ``log`` (batch-append points) and ``read``
(paginated read-back). This module is the single source for the
request/response shapes and path templates; the server registers handlers
against them and the client builds requests from them, so neither can drift.

The domain type is :class:`ExperimentMetric` in
:mod:`types.experiment_metrics`; this module holds the wire bodies that
carry it. A point's ``value`` is a bare scalar and ``kind`` its shape
discriminator (``"scalar"`` today).

This package is part of the publishable client distribution, so it must
not import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
Mirrors ``wire_sessions`` -- the same start/events/end idiom, minus the
lifecycle (a metric stream has no open/close: the owning Experiment row's
status is its lifecycle).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


_MAX_KEY_CHARS = (
    512  # config-globals: ignore -- wire batch/size limit, protocol contract
)
"""Upper bound on a metric key. A key is matched/stored verbatim and indexed
in the primary key; an unbounded key would bloat the index for no gain."""

_BIGINT_MAX = 2**63 - 1  # config-globals: ignore -- int64 max, protocol bound
"""Max value of the ``step`` BIGINT storage column. Bounds the wire ``step`` so
an out-of-range value is a clean 422, not a bigint-overflow 500 at INSERT."""

_MAX_POINTS_PER_BATCH = (
    10_000  # config-globals: ignore -- wire batch/size limit, protocol contract
)
"""Upper bound on points in one ``log`` request. Bounds the memory + INSERT cost
of a single call (the whole batch is parsed into memory and sent as one
``unnest`` INSERT), so one writer cannot pin a backend with a giant body. Higher
than ``SubmitBatch``'s 1000 because a training run legitimately flushes many
points at once; a run logging more per flush pages into several requests."""


def _reject_blank_key(value: str) -> str:
    """Reject an empty-or-whitespace metric key.

    The key is a primary-key component matched verbatim, so a whitespace-only
    key can never be read back meaningfully and is almost certainly a client
    bug; reject it at the boundary. ``Field(min_length=1)`` alone admits
    ``"   "``, so this validator backs it -- mirroring ``wire_sessions``'
    ``_reject_blank`` rule for scalar identity fields.
    """
    if not value.strip():
        raise ValueError("metric key must be non-empty")
    return value


__all__ = [
    "EXPERIMENT_METRICS_PATH",
    "METRICS_API_PATHS",
    "LogMetricsRequest",
    "LogMetricsResponse",
    "MetricPoint",
    "ReadMetricsResponse",
    "experiment_metrics_path",
]


class MetricPoint(BaseModel):
    """One logged metric point, as sent over the wire.

    The wire carrier for an
    :class:`~types.experiment_metrics.ExperimentMetric`. ``(key, step)`` is
    the dedup key together with the owning ``experiment_id`` (supplied in the
    path, not the body): a repeated point is a no-op. ``value`` is the scalar;
    ``kind`` discriminates the shape (``"scalar"`` today).
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=_MAX_KEY_CHARS)
    """The metric name (``"loss"``, ``"val/acc"``, ``"gpu.0.mem"``)."""

    step: int = Field(ge=0, le=_BIGINT_MAX)
    """The x-axis ordinal, monotonic per key; part of the dedup key.

    Bounded to the ``BIGINT`` storage column: Python ``int`` is unbounded, so
    without ``le`` a ``step`` past ``2**63-1`` passes wire validation, then
    overflows the ``bigint`` INSERT and surfaces as an unmapped asyncpg
    ``DataError`` (500). The upper bound rejects it cleanly (422), matching the
    ``ge=0`` / finite-value boundary guards."""

    value: float = Field(allow_inf_nan=False)
    """The logged scalar at this ``(key, step)``.

    Must be finite: ``NaN`` / ``±Inf`` are rejected at the boundary (422). They
    are valid Python floats but not valid JSON numbers, so one persisted
    non-finite point would make the read endpoint's ``JSONResponse`` raise and
    500 the whole experiment's metric history. A diverged run logs a sentinel
    (or its last finite value), not a raw ``NaN``."""

    kind: Literal["scalar"] = "scalar"
    """The value shape. Closed to ``"scalar"`` today: every reader (SPA chart,
    CLI table) assumes a numeric scalar, so a non-scalar point would render
    wrong. Widen this literal (and teach the readers) when histogram / media
    payloads land -- until then a non-``scalar`` kind is a 422, not silent
    mis-rendered data."""

    timestamp: datetime | None = None
    """When the producer logged the point, on its own clock."""

    _reject_blank_key = field_validator("key", mode="after")(
        staticmethod(_reject_blank_key)
    )


class LogMetricsRequest(BaseModel):
    """Batch-append metric points to an experiment."""

    points: list[MetricPoint] = Field(min_length=1, max_length=_MAX_POINTS_PER_BATCH)


class LogMetricsResponse(BaseModel):
    """How many points the append actually persisted.

    ``logged`` counts rows newly written; ``skipped`` counts those that
    collided on ``(experiment_id, key, step)`` and were idempotently ignored,
    so a retried batch reports ``logged=0``. Mirrors
    :class:`~wire.wire_sessions.AppendEventsResponse`.
    """

    logged: int
    skipped: int


class ReadMetricsResponse(BaseModel):
    """One page of an experiment's metric points, in ``(key, step)`` order.

    Paginated (``limit`` / ``offset`` / ``key``) so a caller never pulls an
    arbitrarily large run into memory at once. ``key`` narrows to one metric.
    """

    points: list[MetricPoint]


EXPERIMENT_METRICS_PATH = "/api/experiments/{experiment_id}/metrics"  # config-globals: ignore -- API route (wire contract)


# Every non-field API path in the metrics family, as a FastAPI route template.
# Hand-registered (like the session family), so this registry is the single
# source the drift test checks against the live app -- the analogue of
# ``SESSION_API_PATHS``. A new metrics route belongs here so the same guard
# covers it.
METRICS_API_PATHS: tuple[str, ...] = (EXPERIMENT_METRICS_PATH,)


def experiment_metrics_path(experiment_id: uuid.UUID) -> str:
    """The metrics path (POST log, GET read) for one experiment."""
    return EXPERIMENT_METRICS_PATH.format(experiment_id=experiment_id)
