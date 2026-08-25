"""Tests for ``Store`` read paths -- list/get/cost/changes queries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import asyncio
import uuid

import pytest

from trackinizer.conftest import (
    make_conn,
    make_store,
    new_uuid,
    queue_field_rows,
)
from trackinizer.server.store import read
from trackinizer.types.cost import Cost
from trackinizer.types.errors import NotFoundError, ValidationError
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.filters import Filter
from trackinizer.wire.seq_ranges import SeqRange


K1: uuid.UUID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class TestStoreReads:
    @pytest.mark.asyncio
    async def test_list_changes_after_id_uses_composite_created_id_cursor(
        self,
    ) -> None:
        """Pagination cursor must compare ``(created, id)``, not bare ``id``.

        The result orders ``created DESC, id DESC`` but the old cursor was
        ``id < after_id`` alone; since UUID order is unrelated to ``created``,
        page 2 skipped older rows with larger UUIDs and duped newer rows with
        smaller UUIDs. The cursor must be the same ``(created, id)`` tuple the
        ORDER BY uses.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        cursor_id = new_uuid()
        cursor_created = datetime(2026, 1, 1, tzinfo=UTC)
        # The cursor lookup resolves after_id's ``created``.
        conn.fetchval = AsyncMock(return_value=cursor_created)
        conn.fetch = AsyncMock(return_value=[])
        await store.list_changes(after_id=cursor_id)
        main = next(
            c
            for c in conn.fetch.call_args_list
            if c.args and "FROM change_log" in c.args[0]
        )
        sql = main.args[0]
        # A composite row comparison, not a bare ``id <``.
        assert "(created, id) <" in sql
        assert cursor_created in main.args
        assert cursor_id in main.args

    @pytest.mark.asyncio
    async def test_list_changes_unknown_after_id_raises_not_found(self) -> None:
        # A stale/unknown after_id resolves to cursor_created=None, making
        # ``(created, id) < (NULL, after_id)`` NULL for every row -> a silent
        # empty page. Surface NotFoundError (404) instead, matching the
        # codebase's not-found semantics, so a pager can't mistake a bad
        # cursor for "pagination ended".
        conn = make_conn()
        store, _engine = make_store(conn)
        conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(NotFoundError, match="not found"):
            await store.list_changes(after_id=new_uuid())

    @pytest.mark.asyncio
    async def test_get_inquiry_missing_returns_none(self) -> None:
        conn = make_conn()
        store, _engine = make_store(conn)
        assert await store.get_inquiry(new_uuid()) is None

    def test_what_changed_for_me_rejects_invalid_limit(self) -> None:
        store, _engine = make_store()
        with pytest.raises(ValueError, match="limit must be in"):
            asyncio.run(store.what_changed_for_me("alice", datetime.now(UTC), limit=0))

    @pytest.mark.asyncio
    async def test_what_changed_for_anyone_filters_and_pages_ascending(self) -> None:
        """The push-task query: any-subscriber rows past a (created, id) cursor.

        Same tuple-cursor shape as ``what_changed_for_me`` (ascending, ties
        included via the min-UUID sentinel) with the single-agent filter
        replaced by "has any subscriber at all" -- the push task routes each
        row to every actor in its snapshot, so the SQL must not name one.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        since = datetime(2026, 1, 1, tzinfo=UTC)
        after = new_uuid()
        conn.fetch = AsyncMock(return_value=[])
        await store.what_changed_for_anyone(since, after_id=after)
        sql = conn.fetch.call_args.args[0]
        assert "(c.created, c.id) > ($1, $2)" in sql
        assert "subscribers_snapshot != '{}'" in sql
        assert "ORDER BY c.created, c.id" in sql
        assert conn.fetch.call_args.args[1] == since
        assert conn.fetch.call_args.args[2] == after

    def test_what_changed_for_anyone_rejects_invalid_limit(self) -> None:
        store, _engine = make_store()
        with pytest.raises(ValueError, match="limit must be in"):
            asyncio.run(store.what_changed_for_anyone(datetime.now(UTC), limit=0))

    def test_what_changed_for_anyone_predicate_has_a_serving_index(self) -> None:
        """The push query's filter must be backed by a matching partial index.

        Measured (EXPLAIN on PGlite): the GIN over ``subscribers_snapshot``
        serves ``@>``/``&&``, not ``!= '{}'`` -- and ``cardinality(...) > 0``
        is no better (both plan as Seq Scan). This query runs on every push
        doorbell against append-only ``change_log``, so it needs a partial
        btree whose WHERE clause matches the query's predicate verbatim and
        whose key matches the ORDER BY (Bitmap Index Scan, cost 130 -> 8.5
        at 5k rows).
        """
        schema = (
            Path(__file__).resolve().parents[1] / "assets" / "schema.sql"
        ).read_text()
        assert "ON change_log (created, id)" in schema, (
            "no partial index serves what_changed_for_anyone's per-doorbell scan"
        )
        assert "WHERE subscribers_snapshot != '{}'" in schema


class TestCoverageStoreReads:
    def _row(
        self, *, kind: Inquiry.InquiryKind = "Issue", **extra: object
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        row: dict[str, object] = {
            "id": new_uuid(),
            "kind": kind,
            "seq": 1,
            "owner": "alice",
            "account": "alice",
            "status": "active",
            "title": "title",
            "description": "description",
            "labels": [],
            "subscribers": [],
            "created": now,
            "modified": now,
            "marginal_cost_agent_usd": 0.0,
            "marginal_cost_resource_usd": 0.0,
        }
        row.update(extra)
        return row

    @pytest.mark.asyncio
    async def test_list_next_cost_evidence_and_changes(self) -> None:
        issue_id = new_uuid()
        change_id = new_uuid()
        conn = make_conn()
        conn.fetch.side_effect = [
            # list_kind: main select, then bulk-edges outbound + inbound.
            [self._row(kind="Issue", priority=20)],
            [],
            [],
            # next_issue: edge_fetch outbound + inbound for the returned row.
            [],
            [],
            # proves_belief: main select, then bulk-edges outbound + inbound.
            [self._row(kind="Experiment", codechanges=[], outcome="ok")],
            [],
            [],
            # what_changed_for_me: change_log rows.
            [
                {
                    "id": change_id,
                    "created": datetime.now(UTC),
                    "actor": "alice",
                    "api_key_id": None,
                    "subject_id": issue_id,
                    "subject_kind": "Issue",
                    "kind": "created",
                    "caused_by": None,
                    "reason": "",
                    "old_marginal_cost_agent_usd": 0.0,
                    "old_marginal_cost_resource_usd": 0.0,
                    "new_marginal_cost_agent_usd": 0.0,
                    "new_marginal_cost_resource_usd": 0.0,
                }
            ],
        ]
        queue_field_rows(
            conn,
            # next_issue SELECT * FROM next_issue.sql.
            self._row(kind="Issue", id=issue_id, priority=20),
            # cost_for(deep=True): cost_subtree row after existence check.
            {"agent_usd": 1.0, "resource_usd": 2.0},
        )
        # cost_for existence check (fetchval); deep call exists, shallow doesn't.
        conn.fetchval.side_effect = [1, None]
        store, _engine = make_store(conn)
        assert len(await store.list_kind("Issue", status="active")) == 1
        assert await store.next_issue() is not None
        assert await store.cost_for(issue_id, deep=True) == Cost(
            agent_usd=1.0,
            resource_usd=2.0,
        )
        assert await store.cost_for(issue_id) is None
        assert len(await store.proves_belief(issue_id)) == 1
        assert len(await store.what_changed_for_me("alice", datetime.now(UTC))) == 1

    @pytest.mark.asyncio
    async def test_list_kind_seq_ranges_lower_to_one_or_group(self) -> None:
        """Disjoint intervals become one parenthesized OR group in the SQL.

        The union must be a single indexed query, not one fetch per
        interval, so the SELECT carries an ``(... OR ...)`` clause whose
        bounds bind every interval's present sides.
        """
        conn = make_conn()
        conn.fetch.side_effect = [[], [], []]  # main select + bulk-edges.
        store, _engine = make_store(conn)
        await store.list_kind(
            "Issue",
            seq_ranges=(SeqRange(start=222, stop=260), SeqRange(start=279)),
        )
        sql, *params = conn.fetch.call_args_list[0].args
        assert "(seq >= $2 AND seq <= $3) OR (seq >= $4)" in sql
        # The seq bounds bind $2..$4; LIMIT/OFFSET trail as the last two.
        assert params[:4] == ["Issue", 222, 260, 279]


class TestListKindFilterLowering:
    """Filters belong in the WHERE clause wherever SQL can express them.

    The post-filter path drops ``LIMIT`` from the SQL and materializes EVERY
    row of the kind before testing the predicate in Python -- correct, but the
    cost is the whole table regardless of how few rows match. Measured against
    a live server, ``status`` as a filter took 75.9ms where the same predicate
    as a native param took 18.8ms for byte-identical output, and a filtered
    ``limit=1`` cost 56ms because the scan is paid before the window.

    These pin the SQL SHAPE. That the shape selects the same rows as the
    Python predicate is proven separately, against a real engine, in
    ``read_lowering_pglite_test``.
    """

    @staticmethod
    async def sql_for(*filters: Filter, limit: int = 5) -> str:
        """The SELECT ``list_kind`` issues for ``filters``."""
        conn = make_conn()
        conn.fetch.side_effect = [[], []]
        store, _engine = make_store(conn)
        await store.list_kind("Issue", limit=limit, filters=filters)
        sql, *_params = conn.fetch.call_args_list[0].args
        return str(sql)

    @pytest.mark.asyncio
    async def test_lowers_an_equality_filter_into_sql(self) -> None:
        conn = make_conn()
        conn.fetch.side_effect = [[], []]
        store, _engine = make_store(conn)

        await store.list_kind(
            "Issue", filters=(Filter(field="account", op="is", value="josh"),)
        )

        sql, *params = conn.fetch.call_args_list[0].args
        assert "account = $2" in sql, (
            f"equality filter stayed in Python; SQL was:\n{sql}"
        )
        assert "josh" in params

    @pytest.mark.asyncio
    async def test_a_lowered_filter_keeps_limit_in_sql(self) -> None:
        """With the predicate in SQL, the window is honest again.

        ``LIMIT`` can only ride along once the filter runs before it; that is
        the whole reason the post-filter path exists (Issue#256).
        """
        conn = make_conn()
        conn.fetch.side_effect = [[], []]
        store, _engine = make_store(conn)

        await store.list_kind(
            "Issue",
            limit=5,
            filters=(Filter(field="account", op="is", value="josh"),),
        )

        sql, *params = conn.fetch.call_args_list[0].args
        assert "LIMIT" in sql, f"lowered filter still fetched unbounded:\n{sql}"
        assert 5 in params

    @pytest.mark.asyncio
    async def test_lowers_a_regex_to_the_sql_operator(self) -> None:
        """``re`` becomes Postgres' ``~``, so the scan never leaves the DB."""
        sql = await self.sql_for(Filter(field="title", op="re", value="bug"))

        assert "title ~ $2" in sql
        assert "LIMIT" in sql

    @pytest.mark.asyncio
    async def test_lowers_text_array_membership_to_gin_containment(self) -> None:
        """``label is x`` uses containment so the GIN index can serve it."""
        sql = await self.sql_for(Filter(field="labels", op="is", value="bug"))

        assert "labels @> ARRAY[$2]::text[]" in sql
        assert "LIMIT" in sql

    @pytest.mark.asyncio
    async def test_lowers_presence_ops_without_binding_an_operand(self) -> None:
        """``isnull`` carries no value, so it must not consume a placeholder."""
        conn = make_conn()
        conn.fetch.side_effect = [[], []]
        store, _engine = make_store(conn)

        await store.list_kind(
            "Issue", limit=5, filters=(Filter(field="owner", op="isnull", value=""),)
        )

        sql, *params = conn.fetch.call_args_list[0].args
        assert "owner IS NULL" in sql
        # kind, limit, offset -- no operand for the presence test.
        assert params == ["Issue", 5, 0]

    @pytest.mark.asyncio
    async def test_lowers_an_order_op_on_a_numeric_column(self) -> None:
        """Both evaluators compare a numeric column numerically."""
        sql = await self.sql_for(Filter(field="issue_priority", op="lt", value="20"))

        assert "issue_priority < $2::numeric" in sql
        assert "LIMIT" in sql

    @pytest.mark.asyncio
    async def test_refuses_an_order_op_on_a_text_column(self) -> None:
        """``match_filter`` compares numeric-looking TEXT as numbers.

        Python reads ``"10" < "9"`` as ``10 < 9`` (False); SQL compares the
        same values lexically (True). Measured against a live engine, so
        neither evaluator may run it: lowering would change the rows, and
        keeping it in Python returns rows no lowered query could.
        """
        with pytest.raises(ValidationError, match="title"):
            await self.sql_for(Filter(field="title", op="lt", value="9"))

    @pytest.mark.asyncio
    async def test_an_unknown_field_is_refused(self) -> None:
        """A field no column answers is a typo, not a Python-side filter.

        It used to fall through to the post-filter path, where ``row.get``
        reads the missing key as NULL -- so ``ne`` kept every row while SQL
        would have errored on the column name.
        """
        with pytest.raises(ValidationError, match="unknown filter field"):
            await self.sql_for(Filter(field="nonesuch", op="is", value="x"))

    @pytest.mark.asyncio
    async def test_a_mixed_filter_set_keeps_the_python_path(self) -> None:
        """One un-lowerable clause forces the whole query back to post-filter.

        The window must follow EVERY predicate, so a single Python-evaluated
        clause costs the query its SQL ``LIMIT`` (Issue#256). ``config`` is
        JSONB, which has no SQL rendering of ``str(dict)`` to lower to.
        """
        sql = await self.sql_for(
            Filter(field="account", op="is", value="josh"),
            Filter(field="experiment_config", op="is", value="x"),
        )

        assert "LIMIT" not in sql


class TestUnboundableRegexRefusal:
    """A regex is refused by COLUMN, never by evaluation mode.

    Python's backtracking engine has no deadline, so a regex that cannot lower
    into SQL is refused. What makes it unboundable is the column -- a JSONB
    payload the SQL renderer declines -- and that does not change with the
    mode. Refusing on the mode instead disabled every regex in the SQL/Python
    parity suite, which runs each filter both ways and so could no longer
    compare the two engines' translation at all.
    """

    @pytest.mark.parametrize("lowering", [True, False])
    def test_a_json_column_is_refused_in_either_mode(self, lowering: bool) -> None:
        with pytest.raises(ValidationError, match="experiment_config"):
            read._partition_filters(
                (Filter(field="experiment_config", op="re", value="x"),),
                [],
                lowering=lowering,
            )

    @pytest.mark.parametrize("lowering", [True, False])
    def test_a_lowerable_column_is_allowed_in_either_mode(self, lowering: bool) -> None:
        read._partition_filters(
            (Filter(field="title", op="re", value="^a"),), [], lowering=lowering
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
