"""Tests for the metric-grid mask query wire bodies."""

from __future__ import annotations

import uuid

from pydantic import ValidationError

import pytest

from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_metrics_query import (
    MetricMaskClause,
    MetricQueryRequest,
    MetricRankRequest,
)


class TestMetricMaskClause:
    def test_comparison_clause_round_trips(self) -> None:
        clause = MetricMaskClause(axis="step", op="gt", value="3")
        assert clause.model_dump() == {"axis": "step", "op": "gt", "value": "3"}

    def test_reduction_clause_has_empty_value(self) -> None:
        clause = MetricMaskClause(axis="step", op="max")
        assert clause.value == ""

    def test_rejects_unknown_axis(self) -> None:
        # A bad literal is the point (runtime validation), so go through
        # ``model_validate`` -- which takes ``Any`` -- rather than the typed
        # constructor, mirroring ``test_rejects_extra_field`` below.
        with pytest.raises(ValidationError):
            MetricMaskClause.model_validate(
                {"axis": "bogus", "op": "is", "value": "loss"},
            )

    def test_rejects_unknown_op(self) -> None:
        with pytest.raises(ValidationError):
            MetricMaskClause.model_validate(
                {"axis": "step", "op": "bogus", "value": "3"},
            )

    @pytest.mark.parametrize("op", ["re", "nre", "isnull", "notnull"])
    def test_rejects_filter_only_ops(self, op: str) -> None:
        # A metric grid is neither text-regex-matchable nor nullable, so the
        # regex (``re`` / ``nre``) and presence (``isnull`` / ``notnull``) ops
        # from the inquiry-filter set have no meaning here. The wire op type is
        # the narrow metric op set, so they are rejected at validation -- not
        # only later at the store.
        with pytest.raises(ValidationError):
            MetricMaskClause.model_validate({"axis": "value", "op": op, "value": "0.9"})

    def test_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            MetricMaskClause.model_validate(
                {"axis": "step", "op": "is", "value": "3", "extra": 1},
            )


class TestMetricQueryRequest:
    def test_read_request_defaults(self) -> None:
        req = MetricQueryRequest(
            masks=[MetricMaskClause(axis="key", op="is", value="loss")],
        )
        assert req.write is None
        assert req.sort is None
        assert req.limit is None

    def test_write_request_carries_float(self) -> None:
        req = MetricQueryRequest(
            masks=[
                MetricMaskClause(axis="key", op="is", value="loss"),
                MetricMaskClause(axis="step", op="is", value="3"),
            ],
            write=0.5,
        )
        assert req.write == 0.5

    def test_read_with_sort_and_limit(self) -> None:
        req = MetricQueryRequest(
            masks=[MetricMaskClause(axis="key", op="is", value="loss")],
            sort="desc",
            limit=5,
        )
        assert (req.sort, req.limit) == ("desc", 5)

    def test_empty_masks_is_whole_grid(self) -> None:
        req = MetricQueryRequest()
        assert req.masks == []

    def test_rejects_zero_limit(self) -> None:
        with pytest.raises(ValidationError):
            MetricQueryRequest(masks=[], limit=0)

    def test_rejects_limit_over_max(self) -> None:
        """An over-cap limit is refused, never silently clamped to the cap."""
        assert MetricQueryRequest(limit=MAX_LIST_LIMIT).limit == MAX_LIST_LIMIT
        with pytest.raises(ValidationError):
            MetricQueryRequest(limit=MAX_LIST_LIMIT + 1)

    @pytest.mark.parametrize(
        "read_control",
        [{"sort": "desc"}, {"limit": 1}],
    )
    def test_rejects_read_controls_on_write(
        self,
        read_control: dict[str, object],
    ) -> None:
        """``sort`` / ``limit`` window a read; on a write they would be ignored."""
        with pytest.raises(ValidationError, match="reads only"):
            MetricQueryRequest.model_validate({"write": 0.5} | read_control)

    def test_write_tolerates_null_read_controls(self) -> None:
        """A client dumping the model sends ``null`` for unset fields."""
        req = MetricQueryRequest.model_validate(
            {"masks": [], "write": 0.5, "sort": None, "limit": None},
        )
        assert req.write == 0.5

    def test_rejects_non_finite_write(self) -> None:
        # A non-finite write value would 500 the read on serialization, the
        # same failure class the log path's allow_inf_nan=False closes.
        with pytest.raises(ValidationError):
            MetricQueryRequest(masks=[], write=float("nan"))

    def test_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            MetricQueryRequest.model_validate({"masks": [], "bogus": 1})


class TestMetricRankRequest:
    def test_caps_experiment_ids(self) -> None:
        """The id cap is a wire bound (422), not a store-side 409."""
        ids = [uuid.uuid4() for _ in range(MAX_LIST_LIMIT + 1)]
        assert MetricRankRequest(experiment_ids=ids[:-1], query=MetricQueryRequest())
        with pytest.raises(ValidationError):
            MetricRankRequest(experiment_ids=ids, query=MetricQueryRequest())


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
