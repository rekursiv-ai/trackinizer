"""Tests for ``Store`` read paths -- list/get/cost/changes queries."""

from __future__ import annotations

from datetime import UTC, datetime
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
from trackinizer.types.cost import Cost
from trackinizer.types.errors import NotFoundError
from trackinizer.types.inquiries import Inquiry
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


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
