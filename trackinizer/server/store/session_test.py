"""Tests for ``Store`` session paths -- start/append/read/end session."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import json
import uuid

import asyncpg
import pytest

from trackinizer.conftest import (
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    set_field_row,
)
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.seq_ranges import SeqRange
from trackinizer.wire.wire_sessions import EventBody


class TestAppendEvents:
    @pytest.mark.asyncio
    async def test_append_events_notifies_on_real_append(self) -> None:
        """A successful append wakes subscribers via the session id."""
        conn = make_conn()
        store, engine = make_store(conn)
        session_id = uuid.uuid4()
        set_field_row(conn, {"kind": "AgentSession", "agentsession_ended": None})
        # ON CONFLICT DO NOTHING RETURNING yields the one newly-written seq.
        conn.fetch = AsyncMock(return_value=[{"seq": 0}])
        appended, skipped = await store.append_events(
            session_id, [EventBody(seq=0, kind="UserMessage")]
        )
        assert (appended, skipped) == (1, 0)
        assert engine.notify_calls == [
            (NOTIFY_CHANNEL, json.dumps({"id": str(session_id)}))
        ]

    @pytest.mark.asyncio
    async def test_append_events_fk_violation_raises_not_found(self) -> None:
        # The session passed the kind check but was purged in the race window
        # before the INSERT, so the ``session_id`` FK fails. The store maps
        # that to NotFoundError (-> 404), never a raw constraint-name 409
        # (REV-OPUS-03).
        conn = make_conn()
        store, _engine = make_store(conn)
        set_field_row(conn, {"kind": "AgentSession", "agentsession_ended": None})

        # Only the event INSERT trips the FK; ``tx()``'s rollback also runs over
        # ``fetch`` (extended protocol) and must succeed so the store maps the
        # violation cleanly rather than dying on the rollback.
        async def fetch(sql: str, *args: object) -> list[object]:
            del args
            if "INSERT INTO agent_session_events" in sql:
                raise asyncpg.ForeignKeyViolationError("session_id fkey")
            return []

        conn.fetch = AsyncMock(side_effect=fetch)
        with pytest.raises(NotFoundError, match="not found"):
            await store.append_events(
                uuid.uuid4(), [EventBody(seq=0, kind="UserMessage")]
            )

    @pytest.mark.asyncio
    async def test_append_events_count_is_exact_under_concurrent_appender(
        self,
    ) -> None:
        """A concurrent same-session commit must not corrupt the accounting.

        The count-delta approach (two whole-session ``count(*)`` reads around
        the insert) over-counts when another transaction commits between the
        reads: a one-row append could report appended=2 / skipped=-1. The
        ``RETURNING``-based count reports only the rows THIS call wrote.
        """
        conn = make_conn()
        store, _engine = make_store(conn)
        session_id = uuid.uuid4()
        set_field_row(conn, {"kind": "AgentSession", "agentsession_ended": None})
        # This call newly wrote exactly one row -> RETURNING yields one seq.
        conn.fetch = AsyncMock(return_value=[{"seq": 0}])
        appended, skipped = await store.append_events(
            session_id, [EventBody(seq=0, kind="UserMessage")]
        )
        assert appended == 1
        assert skipped == 0
        assert 0 <= appended <= 1
        assert skipped >= 0

    @pytest.mark.asyncio
    async def test_append_events_duplicate_batch_stays_silent(self) -> None:
        """A fully-duplicate retry (appended == 0) emits no notification."""
        conn = make_conn()
        store, engine = make_store(conn)
        session_id = uuid.uuid4()
        set_field_row(conn, {"kind": "AgentSession", "agentsession_ended": None})
        # Every row collided -> ON CONFLICT DO NOTHING RETURNING yields none.
        conn.fetch = AsyncMock(return_value=[])
        appended, skipped = await store.append_events(
            session_id, [EventBody(seq=0, kind="UserMessage")]
        )
        assert (appended, skipped) == (0, 1)
        assert engine.notify_calls == []

    @pytest.mark.asyncio
    async def test_append_events_rejects_ended_session(self) -> None:
        # Events may attach only to a LIVE session: an ended session's poller
        # is gone and will never inject them. Reject (no row written),
        # mirroring the /inbound enqueue route's ended-session 409.
        conn = make_conn()
        set_field_row(
            conn,
            {
                "kind": "AgentSession",
                "agentsession_ended": datetime(2025, 1, 1, tzinfo=UTC),
            },
        )
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="has ended"):
            await store.append_events(
                new_uuid(), [EventBody(seq=0, kind="UserMessage")]
            )
        # No row written: the INSERT (a ``conn.fetch`` RETURNING) never ran.
        assert not any(
            c.args and "INSERT INTO agent_session_events" in c.args[0]
            for c in conn.fetch.call_args_list
        )


class TestStartSession:
    @pytest.mark.asyncio
    async def test_start_session_exhausted_retries_raises_conflict(self) -> None:
        """Exhausting the reserve-retry budget surfaces a 409, not a raw leak.

        Every attempt's insert loses the live-owner race; after
        ``_MAX_ACTOR_RESERVE_ATTEMPTS`` the loop gives up with a clean
        ConflictError chaining the last asyncpg violation, not a bare loop-exit
        or a leaked ``UniqueViolationError``.
        """
        conn = make_conn()
        # No prior live owners, so reservation always returns the bare name.
        conn.fetch = AsyncMock(return_value=[])

        async def execute(sql: str, *args: object) -> str:
            del args
            if "INSERT INTO inquiries" in sql:
                exc = asyncpg.UniqueViolationError("dup live owner")
                exc.constraint_name = "uq_inquiries_live_session_owner"
                raise exc
            return "OK"

        conn.execute.side_effect = execute
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="could not reserve"):
            await store.start_session(
                SubmitAgentSession(
                    title="s", cli="codex", account="tester@example.com"
                ),
                requested_actor="alice",
            )

    @pytest.mark.asyncio
    async def test_start_session_returns_committed_owner_and_seq_not_reserved(
        self,
    ) -> None:
        """A replayed start echoes the COMMITTED owner/next_seq, not our reserve.

        Under a concurrent same-key race ``submit_agentsession`` may replay the
        winner's row instead of inserting ours. ``start_session`` must read back
        the authoritative owner + next event seq for the returned id, not assume
        the name WE reserved and an empty (seq 0) log -- otherwise the racing
        caller gets a receipt that mismatches the live session.
        """
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])  # reservation sees no live owners
        existing_id = new_uuid()

        async def submit(*_args: object, **_kwargs: object) -> object:
            # Simulate the idempotency-replay return: the WINNER's row id, whose
            # committed owner ("alice") differs from a racer's reserved "alice#2".
            return existing_id

        # The row's committed owner + a non-empty event log (max seq 4 -> next 5).
        async def fetchval(sql: str, *_args: object) -> object:
            if "owner" in sql:
                return "alice"
            if "max(seq)" in sql:
                return 4
            return None

        conn.fetchval.side_effect = fetchval
        store, _engine = make_store(conn)
        # Mock attribute patch: overwrite the bound method with a stub that
        # returns the replayed winner's id (the gather/idempotency race outcome).
        store.submit_agentsession = AsyncMock(side_effect=submit)
        sid, owner, next_seq = await store.start_session(
            SubmitAgentSession(title="s", cli="codex", account="tester@example.com"),
            requested_actor="alice",
        )
        assert sid == existing_id
        assert owner == "alice"
        assert next_seq == 5


class TestReadSessionEvents:
    @pytest.mark.asyncio
    async def test_read_session_events_seq_ranges_lower_to_one_or_group(self) -> None:
        """Event reads share the inquiry list's OR-of-intervals lowering.

        Same union, same single-query SQL shape -- the one
        ``_seq_range_clause`` helper backs both readers, so disjoint event
        windows never fan out per interval either.
        """
        conn = make_conn()
        conn.fetch.return_value = []
        store, _engine = make_store(conn)
        sid = new_uuid()
        await store.read_session_events(
            sid, seq_ranges=(SeqRange(start=0, stop=1), SeqRange(start=3))
        )
        sql, *params = conn.fetch.call_args_list[0].args
        assert "(seq >= $2 AND seq <= $3) OR (seq >= $4)" in sql
        assert params[:4] == [sid, 0, 1, 3]


class TestEndSession:
    """``end_session`` closes a session atomically (one tx, all-or-nothing)."""

    @staticmethod
    def _live_row(cli: str | None = None) -> dict[str, Any]:
        """A live ``AgentSession`` field-read row (``ended`` NULL)."""
        return {
            "kind": "AgentSession",
            "status": "active",
            "agentsession_ended": None,
            "agentsession_cli_session_id": cli,
        }

    @pytest.mark.asyncio
    async def test_ended_and_status_written_in_one_transaction(self) -> None:
        # The whole close must ride a single BEGIN/COMMIT so a partial
        # failure can't leave ``ended`` set while ``status`` stays active
        # (a zombie session invisible to messaging yet "active").
        conn = make_conn()
        set_field_row(conn, self._live_row())
        store, _engine = make_store(conn)
        await store.end_session(
            new_uuid(),
            ended=datetime(2026, 1, 1, tzinfo=UTC),
            cli_session_id=None,
            actor="user",
        )
        sqls = executed_sql(conn)
        # Exactly one transaction wraps every write.
        assert sqls.count("BEGIN") == 1
        assert sqls.count("COMMIT") == 1
        assert "ROLLBACK" not in sqls
        # ``ended`` and ``status`` move in ONE UPDATE so the lifecycle CHECK
        # never sees the (ended set, status active) intermediate desync.
        assert any(
            "UPDATE inquiries SET agentsession_ended = $1, status = $2" in s
            for s in sqls
        )

    @pytest.mark.asyncio
    async def test_status_failure_rolls_back_ended(self) -> None:
        # If the status write fails mid-close, the surrounding tx must
        # ROLLBACK so ``ended`` is not persisted -- the atomicity invariant
        # the zombie-session bug violated.
        conn = make_conn()
        set_field_row(conn, self._live_row())

        async def _execute(sql: str, *args: Any) -> str:
            del args
            # The close's combined ended+status UPDATE fails mid-flight.
            if "UPDATE inquiries SET agentsession_ended" in sql:
                raise asyncpg.PostgresError("close write boom")
            return "UPDATE 1"

        conn.execute = AsyncMock(side_effect=_execute)
        store, _engine = make_store(conn)
        with pytest.raises(asyncpg.PostgresError):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )
        sqls = executed_sql(conn)
        assert "ROLLBACK" in sqls
        assert "COMMIT" not in sqls

    @pytest.mark.asyncio
    async def test_already_ended_raises_conflict(self) -> None:
        # A second end on an already-closed session is a 409, mirroring the
        # enqueue-on-ended guard (API-07 idempotency).
        conn = make_conn()
        row = self._live_row()
        row["agentsession_ended"] = datetime(2025, 1, 1, tzinfo=UTC)
        set_field_row(conn, row)
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError, match="ended"):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )

    @pytest.mark.asyncio
    async def test_already_ended_same_key_replays_without_conflict(self) -> None:
        # A retry that reuses the original close's idempotency key is a replay:
        # the already-ended session returns the original success, NOT a 409.
        # The original end's ``agentsession_ended`` change_log row carries that
        # key as its id, so the probe finds it.
        conn = make_conn()
        row = self._live_row()
        row["agentsession_ended"] = datetime(2025, 1, 1, tzinfo=UTC)
        set_field_row(conn, row)
        replay_key = new_uuid()

        async def _fetchval(sql: str, *_args: object) -> object:
            # The replay probe finds K's change_log row for this session.
            if "FROM change_log" in sql:
                return 1
            return None

        conn.fetchval = AsyncMock(side_effect=_fetchval)
        store, _engine = make_store(conn)
        set_client_change_id(replay_key)
        # No raise: the same-key retry replays the original success.
        await store.end_session(
            new_uuid(),
            ended=datetime(2026, 1, 1, tzinfo=UTC),
            cli_session_id=None,
            actor="user",
        )
        # A replay writes no new close: no ``agentsession_ended`` UPDATE.
        sqls = executed_sql(conn)
        assert not any("UPDATE inquiries SET agentsession_ended" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_already_ended_same_key_replays_when_close_backfilled_cli(
        self,
    ) -> None:
        # When the original close also backfilled cli_session_id, that emit
        # consumed the client key first, so K landed on the
        # ``agentsession_cli_session_id`` change_log row -- NOT the
        # ``agentsession_ended`` one. The replay probe must still match by
        # (id, subject) alone, or an end-with-backfill retry falsely 409s.
        conn = make_conn()
        row = self._live_row()
        row["agentsession_ended"] = datetime(2025, 1, 1, tzinfo=UTC)
        set_field_row(conn, row)
        replay_key = new_uuid()

        async def _fetchval(sql: str, *_args: object) -> object:
            # K's change_log row exists for this session but its kind is
            # ``agentsession_cli_session_id`` (the backfill consumed K first).
            # The probe must not require kind='agentsession_ended'.
            if "FROM change_log" in sql and "agentsession_ended" in sql:
                return None
            if "FROM change_log" in sql:
                return 1
            return None

        conn.fetchval = AsyncMock(side_effect=_fetchval)
        store, _engine = make_store(conn)
        set_client_change_id(replay_key)
        # No raise: the same-key retry replays even though K is on the
        # cli_session_id row, not the ended row.
        await store.end_session(
            new_uuid(),
            ended=datetime(2026, 1, 1, tzinfo=UTC),
            cli_session_id="X",
            actor="user",
        )
        sqls = executed_sql(conn)
        assert not any("UPDATE inquiries SET agentsession_ended" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_already_ended_different_key_raises_conflict(self) -> None:
        # An already-ended session closed under a DIFFERENT idempotency key
        # (the probe finds no matching original) is a genuine second close: 409.
        conn = make_conn()
        row = self._live_row()
        row["agentsession_ended"] = datetime(2025, 1, 1, tzinfo=UTC)
        set_field_row(conn, row)
        conn.fetchval = AsyncMock(return_value=None)  # no matching original.
        store, _engine = make_store(conn)
        set_client_change_id(new_uuid())
        with pytest.raises(ConflictError, match="ended"):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )

    @pytest.mark.asyncio
    async def test_already_ended_conflict_raise_drains_slot(self) -> None:
        # A genuine second close (different key, already ended) raises
        # ConflictError -- but it must drain the externally-set slot FIRST,
        # exactly like the replay-success path and the submit F30 fix. Without
        # the unconditional drain, the leftover key leaks into the next
        # mutation in the same task and either collides on ``change_log.id`` or
        # mis-attributes that mutation to this failed close.
        conn = make_conn()
        row = self._live_row()
        row["agentsession_ended"] = datetime(2025, 1, 1, tzinfo=UTC)
        set_field_row(conn, row)
        conn.fetchval = AsyncMock(return_value=None)  # no matching original.
        store, _engine = make_store(conn)
        set_client_change_id(new_uuid())
        with pytest.raises(ConflictError, match="ended"):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )
        assert _peek_client_change_id() is None, (
            "end_session must drain the client_change_id slot before raising "
            "ConflictError; it currently leaks the externally-set key into the "
            "next mutation in the same task"
        )

    @pytest.mark.asyncio
    async def test_non_session_kind_raises_conflict(self) -> None:
        conn = make_conn()
        set_field_row(conn, {**self._live_row(), "kind": "Issue"})
        store, _engine = make_store(conn)
        with pytest.raises(ConflictError):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )

    @pytest.mark.asyncio
    async def test_missing_session_raises_not_found(self) -> None:
        conn = make_conn()
        set_field_row(conn, None)
        store, _engine = make_store(conn)
        with pytest.raises(NotFoundError):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
