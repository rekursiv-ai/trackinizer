from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import uuid

from trackinizer.types.columns import column_specs
from trackinizer.types.experiment_metrics import ExperimentMetric


def test_defaults_are_an_unpersisted_scalar() -> None:
    first, second = ExperimentMetric(), ExperimentMetric()
    assert (first.key, first.step, first.value, first.kind) == ("", 0, 0.0, "scalar")
    assert first.timestamp is None
    assert first.created is None
    assert first.experiment_id != second.experiment_id


def test_point_is_a_hashable_value() -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    eid = uuid.uuid4()
    point = ExperimentMetric(
        experiment_id=eid,
        key="loss",
        step=3,
        value=0.5,
        timestamp=when,
    )
    assert (point.experiment_id, point.key, point.step) == (eid, "loss", 3)
    assert point.timestamp == when
    # Only a frozen dataclass with ``eq`` is hashable.
    assert hash(point) == hash(replace(point))
    assert replace(point, step=4).step == 4


def test_column_specs_carry_the_sql_types() -> None:
    specs = column_specs(ExperimentMetric)
    assert {name: spec.sql_type for name, spec in specs.items()} == {
        "key": "TEXT",
        "step": "BIGINT",
        "value": "DOUBLE PRECISION",
        "kind": "TEXT",
        "timestamp": "TIMESTAMPTZ",
    }
    assert {name for name, spec in specs.items() if spec.required} == {
        "key",
        "step",
        "value",
        "kind",
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
