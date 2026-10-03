"""Unit tests for the ``Store`` metrics seam -- log, read, mask query, and write.

Fast (no Postgres): the mocked ``conn`` drives the kind-guard, operand-contract,
and ``ON CONFLICT DO NOTHING RETURNING`` accounting branches. The full DB-backed
round-trip (real dedup, real FK, real purge race) lives in ``integration_test.py``;
this pins the error-mapping and count logic the integration tier cannot exercise
cheaply.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import uuid

import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    set_field_row,
)
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.routes import MAX_LIST_LIMIT
from trackinizer.wire.wire_metrics import MetricPoint
from trackinizer.wire.wire_metrics_query import (
    MetricAxis,
    MetricCompareOp,
    MetricMaskClause,
    MetricReduce,
)


class TestLogMetrics:
    @pytest.mark.asyncio
    async def test_log_metrics_reports_newly_written_count(self) -> None:
        """``(logged, skipped)`` counts the RETURNING rows, not the batch size."""
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        # Two of three (key, step) rows were newly inserted; the third collided.
        conn.fetch = AsyncMock(return_value=[{"step": 0}, {"step": 1}])
        logged, skipped = await store.log_metrics(
            uuid.uuid4(),
            [
                MetricPoint(key="loss", step=0, value=0.9),
                MetricPoint(key="loss", step=1, value=0.5),
                MetricPoint(key="loss", step=2, value=0.3),
            ],
        )
        assert (logged, skipped) == (2, 1)

    @pytest.mark.asyncio
    async def test_log_metrics_empty_batch_is_noop(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        assert await store.log_metrics(uuid.uuid4(), []) == (0, 0)

    @pytest.mark.asyncio
    async def test_log_metrics_rejects_non_experiment(self) -> None:
        """A metric may only attach to an Experiment; other kinds 409."""
        conn = make_conn()
        set_field_row(conn, {"kind": "Belief"})
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="not an Experiment"):
            await store.log_metrics(
                uuid.uuid4(),
                [MetricPoint(key="loss", step=0, value=1.0)],
            )

    @pytest.mark.asyncio
    async def test_log_metrics_missing_experiment_raises_not_found(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)  # Fetchrow returns no row.
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="not found"):
            await store.log_metrics(
                uuid.uuid4(),
                [MetricPoint(key="loss", step=0, value=1.0)],
            )

    @pytest.mark.asyncio
    async def test_log_metrics_locks_the_experiment_row(self) -> None:
        """The kind check holds the row, so a purge cannot race the INSERT.

        The store has no foreign-key handler because this lock makes the FK
        unfailable; ``integration_test``'s purge test exercises the real race.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        conn.fetch = AsyncMock(return_value=[{"step": 0}])
        await store.log_metrics(
            uuid.uuid4(),
            [MetricPoint(key="loss", step=0, value=1.0)],
        )
        call_args = conn.fetchrow.call_args
        assert call_args is not None
        sql = call_args.args[0]
        assert isinstance(sql, str)
        assert sql.endswith("FOR UPDATE")


class TestReadMetrics:
    @pytest.mark.asyncio
    async def test_read_metrics_shapes_rows_into_points(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"kind": "Experiment"})
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "key": "loss",
                    "step": 0,
                    "value": 0.9,
                    "kind": "scalar",
                    "timestamp": None,
                },
            ],
        )
        points = await store.read_metrics(uuid.uuid4())
        assert [(p.key, p.step, p.value, p.kind) for p in points] == [
            ("loss", 0, 0.9, "scalar"),
        ]

    @pytest.mark.asyncio
    async def test_read_metrics_key_filter_binds_param(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"kind": "Experiment"})
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.read_metrics(uuid.uuid4(), key="loss")
        call_args = conn.fetch.call_args
        assert call_args is not None
        args = call_args.args
        sql = args[0]
        assert isinstance(sql, str)
        assert "key = $2" in sql
        assert "loss" in args

    @pytest.mark.asyncio
    async def test_read_metrics_missing_experiment_raises_not_found(self) -> None:
        """An unknown id is a 404, not an empty page indistinguishable from no data."""
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="not found"):
            await store.read_metrics(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_read_metrics_rejects_non_experiment(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"kind": "Issue"})
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="not an Experiment"):
            await store.read_metrics(uuid.uuid4())


# Operands no stored cell could hold: past the ``bigint`` column, a negative step, a
# non-finite value, and keys the wire contract refuses (blank, NUL).
_BAD_OPERANDS: tuple[tuple[MetricAxis, str], ...] = (
    ("step", str(2**63)),
    ("step", "-1"),
    ("value", "NaN"),
    ("value", "inf"),
    ("key", "\t"),
    ("key", "lo\x00ss"),
)


def _row(**over: object) -> dict[str, object]:
    """Return a default ``experiment_metrics`` result row, overridable per test."""
    return {
        "experiment_id": uuid.uuid4(),
        "key": "loss",
        "step": 0,
        "value": 0.9,
        "kind": "scalar",
        "timestamp": None,
    } | over


class TestQueryMetrics:
    @pytest.mark.asyncio
    async def test_experiment_ids_bound_first_for_index(self) -> None:
        """The PK-leading ``experiment_id = ANY($1)`` constraint is always $1."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        eids = [uuid.uuid4(), uuid.uuid4()]
        await store.query_metrics(eids, masks=[])
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql_arg = call_args.args[0]
        assert isinstance(sql_arg, str)
        sql = sql_arg
        assert "experiment_id = ANY($1::uuid[])" in sql
        assert conn.fetch.call_args[0][1] == eids

    @pytest.mark.asyncio
    async def test_comparator_op_maps_to_sql(self) -> None:
        """Each metric comparator maps to its SQL operator on the right axis."""
        cases: dict[MetricCompareOp, str] = {
            "is": "=",
            "ne": "<>",
            "lt": "<",
            "le": "<=",
            "gt": ">",
            "ge": ">=",
        }
        for op, sql_op in cases.items():
            conn = make_conn()
            store, _engine = make_store(conn)
            conn.fetch = AsyncMock(return_value=[])
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis="step", op=op, value="3")],
            )
            call_args = conn.fetch.call_args
            assert call_args is not None
            sql_arg = call_args.args[0]
            assert isinstance(sql_arg, str)
            sql = sql_arg
            assert f"step {sql_op} $2::bigint" in sql, op

    @pytest.mark.asyncio
    async def test_axis_column_and_cast(self) -> None:
        """``key`` casts text, ``value`` float8, ``step`` bigint."""
        cases: tuple[tuple[MetricAxis, str, str], ...] = (
            ("key", "loss", "$2::text"),
            ("value", "0.5", "$2::float8"),
            ("step", "3", "$2::bigint"),
        )
        for axis, val, cast_sql in cases:
            conn = make_conn()
            store, _engine = make_store(conn)
            conn.fetch = AsyncMock(return_value=[])
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis=axis, op="is", value=val)],
            )
            call_args = conn.fetch.call_args
            assert call_args is not None
            sql_arg = call_args.args[0]
            assert isinstance(sql_arg, str)
            sql = sql_arg
            assert f"{axis} = {cast_sql}" in sql, axis

    @pytest.mark.asyncio
    async def test_unsupported_op_rejected_at_store(self) -> None:
        """The store rejects an unsupported op too -- defense in depth.

        The wire model is the primary gate (``re`` / ``isnull`` fail to
        construct; see ``wire_metrics_query_test.py``). This pins the store's own
        last-line defense against a ``model_construct`` bypass.

        A direct caller could bypass the wire validation with ``model_construct``;
        the store's ``_OP_TO_SQL`` membership check is the last line before SQL, so
        a bad op 409s rather than mistranslating.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        bad = MetricMaskClause.model_construct(axis="key", op="re", value="x")
        with pytest.raises(ConflictError, match="not supported on metric axis"):
            await store.query_metrics([uuid.uuid4()], masks=[bad])

    @pytest.mark.asyncio
    async def test_reduction_requires_step_axis(self) -> None:
        """``max`` / ``min`` are step-axis reductions; reject on key / value."""
        conn = make_conn()
        store, _engine = make_store(conn)
        axes: tuple[MetricAxis, ...] = ("key", "value")
        ops: tuple[MetricReduce, ...] = ("max", "min")
        for axis in axes:
            for op in ops:
                with pytest.raises(ConflictError, match="reduction"):
                    await store.query_metrics(
                        [uuid.uuid4()],
                        masks=[MetricMaskClause(axis=axis, op=op)],
                    )

    @pytest.mark.asyncio
    async def test_step_max_uses_distinct_on_desc(self) -> None:
        """``step max`` selects the highest step per (experiment_id, key)."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics(
            [uuid.uuid4()],
            masks=[MetricMaskClause(axis="step", op="max")],
        )
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql_arg = call_args.args[0]
        assert isinstance(sql_arg, str)
        sql = sql_arg
        assert "DISTINCT ON (experiment_id, key)" in sql
        assert "ORDER BY experiment_id, key, step DESC" in sql

    @pytest.mark.asyncio
    async def test_step_min_uses_distinct_on_asc(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics(
            [uuid.uuid4()],
            masks=[MetricMaskClause(axis="step", op="min")],
        )
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql_arg = call_args.args[0]
        assert isinstance(sql_arg, str)
        sql = sql_arg
        assert "ORDER BY experiment_id, key, step ASC" in sql

    @pytest.mark.asyncio
    async def test_no_reduction_orders_by_key_step(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics([uuid.uuid4()], masks=[])
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql_arg = call_args.args[0]
        assert isinstance(sql_arg, str)
        sql = sql_arg
        assert "DISTINCT ON" not in sql
        assert "ORDER BY experiment_id, key, step" in sql

    @pytest.mark.asyncio
    async def test_non_numeric_step_mask_rejected(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="not a valid metric step"):
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis="step", op="gt", value="abc")],
            )

    @pytest.mark.asyncio
    async def test_non_numeric_value_mask_rejected(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="not a valid metric value"):
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis="value", op="gt", value="hi")],
            )

    @pytest.mark.parametrize(("axis", "operand"), _BAD_OPERANDS)
    @pytest.mark.asyncio
    async def test_operand_outside_the_cell_contract_rejected(
        self,
        axis: MetricAxis,
        operand: str,
    ) -> None:
        """An operand is held to the field contract of the cell it compares."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(ConflictError, match=f"not a valid metric {axis}"):
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis=axis, op="lt", value=operand)],
            )
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_second_reduction_rejected(self) -> None:
        """Two reductions conflict; taking the last made clause order matter."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(ConflictError, match="one reduction"):
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[
                    MetricMaskClause(axis="step", op="max"),
                    MetricMaskClause(axis="step", op="min"),
                ],
            )

    @pytest.mark.asyncio
    async def test_reduction_operand_rejected(self) -> None:
        """``step max`` takes no operand; one supplied was silently dropped."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        with pytest.raises(ConflictError, match="takes no operand"):
            await store.query_metrics(
                [uuid.uuid4()],
                masks=[MetricMaskClause(axis="step", op="max", value="1")],
            )

    @pytest.mark.asyncio
    async def test_result_shaped_as_experiment_id_point_tuples(self) -> None:
        """Rows become ``(experiment_id, MetricPoint)`` for cross-experiment use."""
        eid = uuid.uuid4()
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(
            return_value=[_row(experiment_id=eid, key="loss", step=7, value=0.3)],
        )
        result = await store.query_metrics([eid], masks=[])
        assert len(result) == 1
        got_eid, point = result[0]
        assert got_eid == eid
        assert (point.key, point.step, point.value, point.kind) == (
            "loss",
            7,
            0.3,
            "scalar",
        )

    @pytest.mark.asyncio
    async def test_sort_orders_by_value_and_limit_windows(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics([uuid.uuid4()], masks=[], sort="desc", limit=5)
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql_arg = call_args.args[0]
        assert isinstance(sql_arg, str)
        sql = sql_arg
        assert "ORDER BY value DESC" in sql
        assert "LIMIT $" in sql
        assert 5 in conn.fetch.call_args[0]

    @pytest.mark.asyncio
    async def test_sort_breaks_value_ties_by_cell(self) -> None:
        """Equal values order by cell, so a ``limit`` cut is deterministic."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics([uuid.uuid4()], masks=[], sort="asc", limit=5)
        call_args = conn.fetch.call_args
        assert call_args is not None
        sql = call_args.args[0]
        assert isinstance(sql, str)
        assert "ORDER BY value ASC, experiment_id, key, step LIMIT" in sql

    @pytest.mark.asyncio
    async def test_omitted_limit_is_the_list_cap(self) -> None:
        """An omitted ``limit`` reads up to ``MAX_LIST_LIMIT``, not a 50-row page."""
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetch = AsyncMock(return_value=[])
        await store.query_metrics([uuid.uuid4()], masks=[])
        call_args = conn.fetch.call_args
        assert call_args is not None
        assert call_args.args[-1] == MAX_LIST_LIMIT


class TestWriteMetricsMasked:
    @classmethod
    def _pinned(cls, *, key: str = "loss", step: str = "3") -> list[MetricMaskClause]:
        return [
            MetricMaskClause(axis="key", op="is", value=key),
            MetricMaskClause(axis="step", op="is", value=step),
        ]

    @pytest.mark.asyncio
    async def test_single_cell_upserts_via_on_conflict(self) -> None:
        """A key+step pinned by ``is`` inserts-or-overwrites the one cell."""
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        conn.execute = AsyncMock(return_value="INSERT 0 1")
        written = await store.write_metrics_masked(
            uuid.uuid4(),
            masks=self._pinned(),
            value=0.5,
        )
        assert written == 1
        upsert = next(
            s for s in executed_sql(conn) if "INSERT INTO experiment_metrics" in s
        )
        assert "ON CONFLICT (experiment_id, key, step) DO UPDATE" in upsert
        assert "value=EXCLUDED.value" in upsert.replace(" ", "")

    @pytest.mark.asyncio
    async def test_bulk_write_updates_existing_only(self) -> None:
        """A comparator mask UPDATEs matching cells; never invents rows."""
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        conn.execute = AsyncMock(return_value="UPDATE 4")
        written = await store.write_metrics_masked(
            uuid.uuid4(),
            masks=[
                MetricMaskClause(axis="key", op="is", value="loss"),
                MetricMaskClause(axis="step", op="gt", value="3"),
            ],
            value=0.5,
        )
        assert written == 4
        update = next(
            s for s in executed_sql(conn) if s.startswith("UPDATE experiment_metrics")
        )
        assert "step > $" in update
        assert "INSERT INTO experiment_metrics" not in "".join(executed_sql(conn))

    @pytest.mark.parametrize(("axis", "operand"), _BAD_OPERANDS)
    @pytest.mark.asyncio
    async def test_operand_outside_the_cell_contract_rejected(
        self,
        axis: MetricAxis,
        operand: str,
    ) -> None:
        """No write path stores, or compares against, a cell the read would refuse."""
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        if axis == "key":
            masks = self._pinned(key=operand)
        elif axis == "step":
            masks = self._pinned(step=operand)
        else:
            masks = [
                MetricMaskClause(axis="step", op="ge", value="0"),
                MetricMaskClause(axis="value", op="lt", value=operand),
            ]
        with pytest.raises(ConflictError, match=f"not a valid metric {axis}"):
            await store.write_metrics_masked(uuid.uuid4(), masks=masks, value=0.5)
        assert not [s for s in executed_sql(conn) if "experiment_metrics" in s]

    @pytest.mark.asyncio
    async def test_write_without_step_mask_rejected(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        with pytest.raises(ConflictError, match="write requires a step mask"):
            await store.write_metrics_masked(
                uuid.uuid4(),
                masks=[MetricMaskClause(axis="key", op="is", value="loss")],
                value=0.5,
            )

    @pytest.mark.asyncio
    async def test_non_finite_value_rejected(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ConflictError, match="finite"):
                await store.write_metrics_masked(
                    uuid.uuid4(),
                    masks=self._pinned(),
                    value=bad,
                )

    @pytest.mark.asyncio
    async def test_unsupported_op_rejected_at_store(self) -> None:
        """A ``model_construct``-bypassed bad op still 409s at the store (defense)."""
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "Experiment"})
        with pytest.raises(ConflictError, match="not supported on metric axis"):
            await store.write_metrics_masked(
                uuid.uuid4(),
                masks=[
                    MetricMaskClause(axis="step", op="is", value="3"),
                    MetricMaskClause.model_construct(axis="key", op="isnull", value=""),
                ],
                value=0.5,
            )

    @pytest.mark.asyncio
    async def test_rejects_non_experiment(self) -> None:
        conn = make_conn()
        set_field_row(conn, {"kind": "Belief"})
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="not an Experiment"):
            await store.write_metrics_masked(
                uuid.uuid4(),
                masks=self._pinned(),
                value=0.5,
            )

    @pytest.mark.asyncio
    async def test_missing_experiment_raises_not_found(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError, match="not found"):
            await store.write_metrics_masked(
                uuid.uuid4(),
                masks=self._pinned(),
                value=0.5,
            )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
