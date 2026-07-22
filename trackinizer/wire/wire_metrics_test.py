"""Boundary-validation tests for the metrics wire bodies."""

from __future__ import annotations

from typing import Any, cast

import math

from pydantic import ValidationError

import pytest

from trackinizer.wire.wire_metrics import (
    _MAX_POINTS_PER_BATCH,
    LogMetricsRequest,
    MetricPoint,
)


class TestMetricPointValueDomain:
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_rejects_non_finite_value(self, bad: float) -> None:
        """NaN / ±Inf are valid Python floats but not valid JSON numbers.

        A persisted non-finite value makes the read endpoint's JSONResponse
        raise (``json.dumps`` with ``allow_nan=False``) and 500 the whole
        experiment's metric history, so it must be rejected at the boundary.
        """
        with pytest.raises(ValidationError):
            MetricPoint(key="loss", step=0, value=bad)

    def test_accepts_finite_value(self) -> None:
        assert MetricPoint(key="loss", step=0, value=-1.5).value == -1.5


class TestMetricPointKind:
    def test_rejects_non_scalar_kind(self) -> None:
        """``kind`` is closed to ``"scalar"``: readers assume a numeric scalar,
        so a non-scalar point would render wrong. Reject until media lands.
        """
        # Runtime-invalid on purpose: the Literal makes this a static error too,
        # so cast to feed the bad value past the type checker to the validator.
        with pytest.raises(ValidationError):
            MetricPoint(key="loss", step=0, value=1.0, kind=cast(Any, "histogram"))

    def test_defaults_to_scalar(self) -> None:
        assert MetricPoint(key="loss", step=0, value=1.0).kind == "scalar"


class TestMetricPointStep:
    def test_rejects_negative_step(self) -> None:
        with pytest.raises(ValidationError):
            MetricPoint(key="loss", step=-1, value=1.0)

    def test_rejects_step_over_bigint_max(self) -> None:
        # Python int is unbounded; without an upper bound a step past 2**63-1
        # overflows the BIGINT column on INSERT -> unmapped 500. Reject at the
        # boundary (422) instead.
        with pytest.raises(ValidationError):
            MetricPoint(key="loss", step=2**63, value=1.0)

    def test_accepts_bigint_max(self) -> None:
        assert MetricPoint(key="loss", step=2**63 - 1, value=1.0).step == 2**63 - 1


class TestLogMetricsRequestBatchSize:
    def test_rejects_empty_batch(self) -> None:
        with pytest.raises(ValidationError):
            LogMetricsRequest(points=[])

    def test_rejects_over_max_batch(self) -> None:
        # An unbounded batch is a memory/latency DoS (whole body parsed +
        # one mega-INSERT); the cap makes an over-large batch a clean 422.
        points = [
            MetricPoint(key="loss", step=i, value=1.0)
            for i in range(_MAX_POINTS_PER_BATCH + 1)
        ]
        with pytest.raises(ValidationError):
            LogMetricsRequest(points=points)

    def test_accepts_max_batch(self) -> None:
        points = [
            MetricPoint(key="loss", step=i, value=1.0)
            for i in range(_MAX_POINTS_PER_BATCH)
        ]
        assert len(LogMetricsRequest(points=points).points) == _MAX_POINTS_PER_BATCH
