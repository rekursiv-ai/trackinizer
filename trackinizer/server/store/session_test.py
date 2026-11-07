"""Tests for ``Store`` session paths -- start/append/read/end session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import re

from treekle import from_plain

import asyncpg
import pytest

from trackinizer.conftest import (
    RecordingPolicy,
    executed_sql,
    make_conn,
    make_store,
    new_uuid,
    set_field_row,
)
from trackinizer.mock_test_support import AsyncMockProtocol
from trackinizer.server.access_policy import (
    AccessDeniedError,
    Principal,
    Resource,
)
from trackinizer.server.store.change_id_slot import (
    _peek_client_change_id,
    set_client_change_id,
)
from trackinizer.server.store.session import (
    _LOCK_SESSION_SQL,
    PastRun,
    RunPlace,
    _optional_datetime,
    _optional_str,
)
from trackinizer.server.store.session_feed import CONVERSATION
from trackinizer.server.store.store_test_support import (
    EVERY_SESSION,
    append_user_message,
)
from trackinizer.types.change_log import Snapshot
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from collections.abc import Callable

    from trackinizer.lib.postgres import Conn
    from trackinizer.mock_test_support import MockConnection
    from trackinizer.server.store.core import Store


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
                    title="s",
                    cli="codex",
                    account="tester@example.com",
                ),
                requested_actor="alice",
                audience="open",
            )

    @pytest.mark.asyncio
    async def test_start_session_returns_the_committed_owner_not_our_reserve(
        self,
    ) -> None:
        """A replayed start echoes the COMMITTED owner, not the name we reserved.

        Under a concurrent same-key race ``submit_agentsession`` may replay the
        winner's row instead of inserting ours. ``start_session`` must read back
        the authoritative owner for the returned id -- otherwise the racing
        caller gets a receipt that mismatches the live session.

        ``next_seq`` is no longer part of that receipt: it continued the legacy
        event log, and a record's key is DERIVED from its position in its
        source file, so a resumed run re-derives the same keys rather than
        seeding a counter. It is always 0 now, asserted here so a reader does
        not mistake the field for live information.
        """
        conn = make_conn()
        conn.fetch = AsyncMock(return_value=[])  # Reservation sees no live owners.
        existing_id = new_uuid()

        async def submit(*_args: object, **_kwargs: object) -> object:
            # Simulate the idempotency-replay return: the WINNER's row id, whose
            # committed owner ("alice") differs from a racer's reserved "alice#2".
            return existing_id

        async def fetchval(sql: str, *_args: object) -> object:
            return "alice" if "owner" in sql else None

        conn.fetchval.side_effect = fetchval
        store, _engine = make_store(conn)
        # Mock attribute patch: overwrite the bound method with a stub that
        # returns the replayed winner's id (the gather/idempotency race outcome).
        with patch.object(
            store,
            "submit_agentsession",
            new=AsyncMock(side_effect=submit),
        ):
            sid, owner, next_seq = await store.start_session(
                SubmitAgentSession(
                    title="s",
                    cli="codex",
                    account="tester@example.com",
                ),
                requested_actor="alice",
                audience="open",
            )
        assert sid == existing_id
        assert owner == "alice"
        assert next_seq == 0

    @pytest.mark.asyncio
    async def test_another_unique_violation_is_raised_not_retried(self) -> None:
        """Only the live-owner race retries; any other duplicate is a real error."""
        store, engine = make_store()
        engine.conn.fetch = AsyncMock(return_value=[])
        exc = asyncpg.UniqueViolationError("dup")
        exc.constraint_name = "inquiries_pkey"
        submit = AsyncMock(side_effect=exc)

        with (
            patch.object(store, "submit_agentsession", new=submit),
            pytest.raises(asyncpg.UniqueViolationError) as raised,
        ):
            _ = await store.start_session(
                SubmitAgentSession(title="s", cli="codex", account="t@e"),
                requested_actor="alice",
                audience="open",
            )

        assert raised.value is exc
        assert submit.await_count == 1


@pytest.mark.asyncio
async def test_session_audiences_keep_nulls_and_omit_unknown_sessions() -> None:
    """A lookup returns stored audiences keyed by session, including NULL."""
    first, second, unknown = new_uuid(), new_uuid(), new_uuid()
    conn = make_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {"id": first, "agentsession_audience": "open"},
            {"id": second, "agentsession_audience": None},
        ],
    )
    store, _ = make_store(conn)

    assert await store.read_push_audiences([first, second, unknown]) == {
        first: "open",
        second: None,
    }
    conn.fetch.assert_awaited_once_with(
        "SELECT id, agentsession_audience FROM inquiries "
        "WHERE kind = 'AgentSession' AND id = ANY($1::uuid[]) "
        "AND COALESCE(agentsession_cli_session_id, '') NOT LIKE 'chat:%'",
        [first, second, unknown],
    )


@pytest.mark.asyncio
async def test_past_runs_read_a_machines_runs_for_one_account() -> None:
    """Each liveness row becomes a run, open or ended, in the order read.

    The first row's times arrive as ISO text, so each is read into a datetime.
    """
    ended, open_ = new_uuid(), new_uuid()
    seen = datetime(2026, 11, 2, 12, tzinfo=UTC)
    conn = make_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "session_id": ended,
                "run": "r1",
                "cli": "codex",
                "cwd": "/work",
                "last_seen": seen.isoformat(),
                "ended": (seen + timedelta(minutes=1)).isoformat(),
            },
            {
                "session_id": open_,
                "run": None,
                "cli": "claude",
                "cwd": None,
                "last_seen": seen - timedelta(hours=1),
                "ended": None,
            },
        ],
    )
    store, _ = make_store(conn)

    runs = await store.past_runs(machine="box", account="Ada@example.com", limit=2)

    assert runs == [
        PastRun(
            session_id=ended,
            cli="codex",
            run="r1",
            cwd="/work",
            last_seen=seen,
            ended=seen + timedelta(minutes=1),
        ),
        PastRun(
            session_id=open_,
            cli="claude",
            run=None,
            cwd=None,
            last_seen=seen - timedelta(hours=1),
            ended=None,
        ),
    ]
    conn.fetch.assert_awaited_once_with(
        "SELECT liveness.session_id, liveness.run, liveness.cli, liveness.cwd, "
        "liveness.last_seen, inquiries.agentsession_ended AS ended "
        "FROM session_liveness AS liveness "
        "JOIN inquiries ON inquiries.id = liveness.session_id "
        "WHERE liveness.machine = $1 AND liveness.cli IS NOT NULL "
        "AND lower(inquiries.account) = lower($2) "
        "ORDER BY liveness.last_seen DESC, liveness.session_id LIMIT $3",
        "box",
        "Ada@example.com",
        2,
    )


class TestSetSessionAudience:
    """``set_session_audience`` moves a session only for one of its owners."""

    _LOCK = (
        "SELECT id, account, agentsession_audience AS audience, "
        "agentsession_opened_by_api_key_id AS opener, "
        "agentsession_cli_session_id AS cli_session_id FROM inquiries "
        "WHERE kind = 'AgentSession' AND id = ANY($1::uuid[]) "
        "ORDER BY id FOR UPDATE"
    )

    @classmethod
    def _row(
        cls,
        *,
        audience: str = "open",
        opener: UUID | None = None,
        cli_session_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "id": new_uuid(),
            "account": "ada@example.com",
            "audience": audience,
            "opener": opener,
            "cli_session_id": cli_session_id,
        }

    @classmethod
    def _conn(cls, *rows: dict[str, object]) -> MockConnection:
        async def fetch(sql: str, *_args: object) -> list[dict[str, object]]:
            return list(rows) if sql == cls._LOCK else []

        conn = make_conn()
        conn.fetch = AsyncMock(side_effect=fetch)
        conn.fetchval = AsyncMock(return_value="bea@example.com")
        return conn

    @classmethod
    async def _hide(
        cls,
        store: Store,
        sessions: list[UUID],
        *,
        caller: str | None,
    ) -> list[UUID]:
        return await store.set_session_audience(
            sessions,
            audience="private",
            replacing=frozenset({"open"}),
            refusing=frozenset({"staff"}),
            api_key_id=None,
            actor="ada",
            caller=caller,
        )

    @pytest.mark.asyncio
    async def test_a_move_is_audited_and_heard_by_the_policy(self) -> None:
        """Each move updates the field, logs old and new, and tells the policy."""
        opener, key = new_uuid(), new_uuid()
        row = {**self._row(), "opener": str(opener)}
        session = from_plain(row["id"], UUID)
        conn = self._conn(row)
        store, _ = make_store(conn)
        prepare_audience_change = AsyncMock()
        on_audience = AsyncMock()
        policy = AsyncMock(
            prepare_audience_change=prepare_audience_change,
            on_audience=on_audience,
        )
        principal = Principal(email="ada@example.com", role="viewer")
        with (
            patch.object(store, "access_policy", new=policy),
            patch.object(store, "_update_field", new=AsyncMock()) as update,
            patch.object(store, "_emit_field_change", new=AsyncMock()) as emit,
        ):
            moved = await store.set_session_audience(
                [session],
                audience="private",
                replacing=frozenset({"open"}),
                refusing=frozenset({"staff"}),
                api_key_id=key,
                actor="ada",
                reason="hidden by its owner",
                principal=principal,
                caller="ada@example.com",
            )

        assert moved == [session]
        conn.fetch.assert_awaited_once_with(self._LOCK, [session])
        assert conn.fetchval.await_args is not None
        assert conn.fetchval.await_args.args[1:] == (opener,)
        prepare_audience_change.assert_awaited_once_with(conn)
        update.assert_awaited_once_with(
            conn,
            session,
            "agentsession_audience",
            "private",
        )
        emit.assert_awaited_once_with(
            conn,
            session,
            "AgentSession",
            "agentsession_audience",
            Snapshot(agentsession_audience="open"),
            new=Snapshot(agentsession_audience="private"),
            api_key_id=key,
            actor="ada",
            reason="hidden by its owner",
        )
        on_audience.assert_awaited_once_with(
            conn,
            principal=principal,
            resource=Resource(
                kind="session",
                id=str(session),
                account="ada@example.com",
            ),
        )

    @pytest.mark.asyncio
    async def test_a_keyless_session_is_hidden_by_its_account_with_no_reason(
        self,
    ) -> None:
        """No opening key names no second owner; the audit reason defaults empty."""
        row = self._row()
        conn = self._conn(row)
        store, _ = make_store(conn)
        session = from_plain(row["id"], UUID)
        emit = cast(AsyncMockProtocol[object], AsyncMock())
        with patch.object(store, "_emit_field_change", new=emit):
            assert await self._hide(store, [session], caller="ADA@example.com") == [
                session,
            ]

        conn.fetchval.assert_not_awaited()
        assert emit.await_args is not None
        assert emit.await_args.kwargs["reason"] == ""

    @pytest.mark.asyncio
    async def test_the_user_of_its_opening_key_hides_a_session(self) -> None:
        row = self._row(opener=new_uuid())
        store, _ = make_store(self._conn(row))
        session = from_plain(row["id"], UUID)

        assert await self._hide(store, [session], caller="bea@example.com") == [
            session,
        ]

    @pytest.mark.asyncio
    async def test_a_stranger_may_not_hide_a_session(self) -> None:
        row = self._row(opener=new_uuid())
        store, _ = make_store(self._conn(row))
        with (
            patch.object(store, "_update_field", new=AsyncMock()) as update,
            pytest.raises(AccessDeniedError, match=r"^session_not_owner$"),
        ):
            await self._hide(
                store,
                [from_plain(row["id"], UUID)],
                caller="cy@example.com",
            )
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_session_outside_replacing_stays_and_the_rest_move(self) -> None:
        held, moving = self._row(audience="private"), self._row()
        store, _ = make_store(self._conn(held, moving))
        sessions = [from_plain(row["id"], UUID) for row in (held, moving)]

        assert await self._hide(store, sessions, caller=None) == sessions[1:]

    @pytest.mark.asyncio
    async def test_a_refusing_audience_refuses_the_whole_call(self) -> None:
        staff, moving = self._row(audience="staff"), self._row()
        store, _ = make_store(self._conn(staff, moving))
        refused = from_plain(staff["id"], UUID)
        with (
            patch.object(store, "_update_field", new=AsyncMock()) as update,
            pytest.raises(
                ConflictError,
                match=re.escape(
                    f"sessions [{refused!r}] are in an audience that refuses 'private'",
                ),
            ),
        ):
            await self._hide(
                store,
                [refused, from_plain(moving["id"], UUID)],
                caller=None,
            )
        update.assert_not_awaited()


class TestEndSession:
    """``end_session`` closes a session atomically (one tx, all-or-nothing)."""

    @classmethod
    def _live_row(cls, cli: str | None = None) -> dict[str, object]:
        """Return a live ``AgentSession`` field-read row (``ended`` NULL)."""
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
    async def test_a_close_writes_and_audits_what_it_was_given(self) -> None:
        """Every input reaches the row or its audit, attributed to its caller."""
        conn = make_conn()
        set_field_row(conn, self._live_row())
        store, _engine = make_store(conn)
        session_id, key = new_uuid(), new_uuid()
        ended = datetime(2026, 1, 1, tzinfo=UTC)

        await store.end_session(
            session_id,
            ended=ended,
            cli_session_id="cli-1",
            api_key_id=key,
            actor="closer",
        )

        assert conn.fetchrow.await_args_list[0].args == (_LOCK_SESSION_SQL, session_id)
        writes = [call.args for call in conn.execute.await_args_list]
        assert [
            args[1:] for args in writes if "SET agentsession_ended" in str(args[0])
        ] == [(ended, "complete", session_id)]
        audits = [args for args in writes if "INSERT INTO change_log" in str(args[0])]
        kinds = ("agentsession_cli_session_id", "agentsession_ended", "status")
        assert [kind for args in audits for kind in kinds if kind in args] == list(
            kinds,
        )
        assert all(
            "closer" in args and key in args and session_id in args for args in audits
        )
        assert any("cli-1" in args for args in audits)

    @pytest.mark.asyncio
    async def test_status_failure_rolls_back_ended(self) -> None:
        # If the status write fails mid-close, the surrounding tx must
        # ROLLBACK so ``ended`` is not persisted -- the atomicity invariant
        # the zombie-session bug violated.
        conn = make_conn()
        set_field_row(conn, self._live_row())

        async def _execute(sql: str, *args: object) -> str:
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
        replay_key, session_id = new_uuid(), new_uuid()

        async def _fetchval(sql: str, *args: object) -> object:
            # The replay probe finds K's change_log row for this session, and
            # only when it asks for exactly that.
            if sql.startswith("SELECT 1 FROM change_log") and args == (
                replay_key,
                session_id,
            ):
                return 1
            return None

        conn.fetchval = AsyncMock(side_effect=_fetchval)
        store, _engine = make_store(conn)
        set_client_change_id(replay_key)
        # No raise: the same-key retry replays the original success.
        await store.end_session(
            session_id,
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
        conn.fetchval = AsyncMock(return_value=None)  # No matching original.
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
        conn.fetchval = AsyncMock(return_value=None)  # No matching original.
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
        with pytest.raises(
            ConflictError,
            match=r"is a Issue; only an AgentSession can be ended$",
        ):
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
        with pytest.raises(NotFoundError, match="not found"):
            await store.end_session(
                new_uuid(),
                ended=datetime(2026, 1, 1, tzinfo=UTC),
                cli_session_id=None,
                actor="user",
            )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestEndSessionOnADatabase:
    """What a close writes, read back from the tables rather than the SQL text."""

    async def test_a_close_stamps_the_session_and_audits_who_closed_it(
        self,
        store: Store,
    ) -> None:
        key = await _api_key(store)
        session_id = await _open(store)
        ended = datetime(2026, 1, 1, tzinfo=UTC)

        stamped = await store.end_session(
            session_id,
            ended=ended,
            cli_session_id="cli-1",
            api_key_id=key,
            actor="closer",
        )

        assert stamped == ended
        row = await store.get_inquiry(session_id)
        assert isinstance(row, AgentSession)
        assert (row.status, row.ended, row.cli_session_id) == (
            "complete",
            ended,
            "cli-1",
        )
        assert await _close_audit(store, session_id) == {
            ("agentsession_cli_session_id", "closer", key),
            ("agentsession_ended", "closer", key),
            ("status", "closer", key),
        }

    async def test_a_retry_under_the_same_key_replays_the_first_close(
        self,
        store: Store,
    ) -> None:
        session_id = await _open(store)
        first = datetime(2026, 1, 1, tzinfo=UTC)
        key = uuid4()
        set_client_change_id(key)
        _ = await store.end_session(session_id, ended=first, actor="closer")

        set_client_change_id(key)
        retried = await store.end_session(
            session_id,
            ended=first + timedelta(hours=1),
            actor="closer",
        )

        assert retried == first
        set_client_change_id(uuid4())
        with pytest.raises(ConflictError, match="already ended"):
            _ = await store.end_session(session_id, ended=first, actor="closer")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_live_session_in_no_room_resolves_with_none(store: Store) -> None:
    session_id = await _open(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_rooms = NULL WHERE id = $1",
            session_id,
        )

    assert await store.resolve_live_sessions("agent") == [(session_id, ())]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_attach_reopens_the_session_by_id_and_keeps_its_cli_id(
    store: Store,
) -> None:
    """Another key's run re-opens an ended chat by its id; its ``chat:`` id stays."""
    opener = await _api_key(store, email="opener@example.com")
    runner = await _api_key(store, email="runner@example.com")
    chat = f"chat:{uuid4()}"
    session_id, owner, _ = await store.start_session(
        SubmitAgentSession(
            title="s",
            cli="assistant",
            cli_session_id=chat,
            account="t@e",
        ),
        requested_actor="chat-1",
        audience="open",
        api_key_id=opener,
    )
    _ = await store.end_session(session_id, ended=datetime.now(UTC), actor="chat-1")

    attached = await store.start_session(
        SubmitAgentSession(title="ignored", cli="claude", account="t@e"),
        requested_actor="claude",
        audience="open",
        api_key_id=runner,
        attach=session_id,
    )

    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    assert attached == (session_id, owner, 0)
    assert (row.status, row.ended, row.cli_session_id, row.opened_by_api_key_id) == (
        "active",
        None,
        chat,
        opener,
    )
    with pytest.raises(NotFoundError):
        _ = await store.start_session(
            SubmitAgentSession(title="s", cli="claude", account="t@e"),
            requested_actor="claude",
            audience="open",
            attach=uuid4(),
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestPrivateResume:
    """A resume asked to be private hides the open session it re-attaches."""

    async def test_an_open_session_is_hidden_audited_and_told_to_the_policy(
        self,
        store: Store,
    ) -> None:
        policy = RecordingPolicy()
        store.access_policy = policy
        session_id = await _open(store, cli_session_id="cli-1")
        principal = Principal(email="t@e", role="writer")

        resumed = await _resume(store, "cli-1", audience="private", principal=principal)

        assert resumed == session_id
        row = await store.get_inquiry(session_id)
        assert isinstance(row, AgentSession)
        assert row.audience == "private"
        assert await _audits(store, session_id, "agentsession_audience") == [
            "hidden by its owner on resume",
        ]
        assert policy.of("prepare_audience_change") == [None]
        assert policy.of("on_audience") == [
            (principal, Resource(kind="session", id=str(session_id), account="t@e")),
        ]

    async def test_a_private_session_is_left_as_it_is(self, store: Store) -> None:
        policy = RecordingPolicy()
        store.access_policy = policy
        session_id = await _open(store, cli_session_id="cli-1")
        _ = await _resume(store, "cli-1", audience="private")

        assert await _resume(store, "cli-1", audience="private") == session_id

        assert len(policy.of("on_audience")) == 1
        assert len(await _audits(store, session_id, "agentsession_audience")) == 1

    async def test_a_staff_session_is_refused_and_stays_ended(
        self,
        store: Store,
    ) -> None:
        session_id = await _open(store, cli_session_id="cli-1")
        ended = datetime(2026, 1, 1, tzinfo=UTC)
        _ = await store.end_session(session_id, ended=ended, actor="agent")
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET agentsession_audience = 'staff' WHERE id = $1",
                session_id,
            )

        with pytest.raises(ConflictError, match="never hides"):
            _ = await _resume(store, "cli-1", audience="private")

        row = await store.get_inquiry(session_id)
        assert isinstance(row, AgentSession)
        assert (row.audience, row.ended) == ("staff", ended)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_room_resolves_only_the_sessions_joined_to_it(store: Store) -> None:
    joined = await _open(store, rooms=["lab"])
    _ = await _open(store, rooms=["other"])

    assert await store.resolve_live_sessions("agent", room="lab") == [
        (joined, ("lab",)),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_feed_keeps_a_window_and_its_conversation(store: Store) -> None:
    session_id = await _open(store)
    await append_user_message(store, session_id, idx=0, text="hi")
    now = datetime.now(UTC)
    before, after = now - timedelta(days=1), now + timedelta(days=1)

    (event,) = await store.read_feed(
        since=before,
        until=after,
        scope=EVERY_SESSION,
        conversation=True,
    )

    assert (event.session_id, event.text) == (session_id, "hi")
    assert await store.read_feed(since=after, scope=EVERY_SESSION) == []
    assert await store.read_feed(until=before, scope=EVERY_SESSION) == []


@pytest.mark.parametrize(
    ("read", "message"),
    [(_optional_datetime, "datetime"), (_optional_str, "str")],
)
def test_an_optional_column_of_another_type_is_refused(
    read: Callable[[object], object],
    message: str,
) -> None:
    assert read(None) is None
    with pytest.raises(ValueError, match=f"isinstance\\(value, {message}\\)"):
        _ = read(1)


async def _open(
    store: Store,
    *,
    cli_session_id: str | None = None,
    rooms: list[str] | None = None,
) -> UUID:
    """Open a live session."""
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(
            title="s",
            cli="claude",
            account="t@e",
            cli_session_id=cli_session_id,
            rooms=rooms,
        ),
        requested_actor="agent",
        audience="open",
    )
    return session_id


async def _resume(
    store: Store,
    cli_session_id: str,
    *,
    audience: str,
    principal: Principal | None = None,
) -> UUID:
    """Resume the session ``cli_session_id`` names, asking for ``audience``."""
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(
            title="s",
            cli="claude",
            account="t@e",
            cli_session_id=cli_session_id,
        ),
        requested_actor="agent",
        audience=audience,
        principal=principal,
    )
    return session_id


async def _audits(store: Store, session_id: UUID, kind: str) -> list[object]:
    """Return the reason of each ``kind`` audit row of ``session_id``."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT reason FROM change_log WHERE subject_id = $1 AND kind = $2",
            session_id,
            kind,
        )
    return [row["reason"] for row in rows]


async def _api_key(store: Store, *, email: str = "closer@example.com") -> UUID:
    """Return a stored API key, of a new user ``email``, that a write is attributed to."""
    user, key = uuid4(), uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Closer', 'writer', 'active')",
            user,
            email,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'key', 'hash', 'trax_', 'writer')",
            key,
            user,
        )
    return key


async def _close_audit(
    store: Store,
    session_id: UUID,
) -> set[tuple[object, object, object]]:
    """Return ``(kind, actor, api_key_id)`` for each audit row a close writes."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, actor, api_key_id FROM change_log WHERE subject_id = $1 "
            "AND kind IN ('agentsession_cli_session_id', 'agentsession_ended', "
            "'status')",
            session_id,
        )
    return {(row["kind"], row["actor"], row["api_key_id"]) for row in rows}


@pytest.mark.asyncio
async def test_actor_reservation_uses_the_first_gap_in_taken_suffixes() -> None:
    store, engine = make_store()
    engine.conn.fetch.return_value = [
        {"owner": owner} for owner in ("agent", "agent#2", "agent#4")
    ]
    assert await store.reserve_session_actor("agent") == "agent#3"
    assert engine.conn.fetch.await_args is not None
    assert engine.conn.fetch.await_args.args[1:] == ("agent",)


@pytest.mark.asyncio
@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("found", [False, True])
async def test_start_routes_attach_and_cli_resume_before_reserving(
    attach: bool,
    found: bool,
) -> None:
    store, _engine = make_store()
    session_id = uuid4()
    receipt = (session_id, "original", 0)
    resume = cast(
        AsyncMockProtocol[tuple[UUID, str, int] | None],
        AsyncMock(return_value=receipt if found else None),
    )
    reserve = cast(AsyncMockProtocol[str], AsyncMock(return_value="fresh"))
    body = SubmitAgentSession(title="s", cli_session_id="cli-1", account="t@e")
    with (
        patch.object(store, "_resume_session", new=resume),
        patch.object(store, "reserve_session_actor", new=reserve),
    ):
        if found:
            assert (
                await store.start_session(
                    body,
                    requested_actor="agent",
                    audience="private",
                    attach=session_id if attach else None,
                )
                == receipt
            )
        elif attach:
            with pytest.raises(NotFoundError):
                await store.start_session(
                    body,
                    requested_actor="agent",
                    audience="private",
                    attach=session_id,
                )
        else:
            with pytest.raises(ConflictError, match="after 0 attempts"):
                await store.start_session(
                    body,
                    requested_actor="agent",
                    audience="private",
                    max_reserve_attempts=0,
                )
    reserve.assert_not_awaited()
    assert resume.await_args is not None
    assert resume.await_args.kwargs["hide"] is True
    assert resume.await_args.kwargs["actor"] == "agent"
    assert resume.await_count == 1
    assert resume.await_args.args == (body,)
    assert resume.await_args.kwargs.get("attach") == (session_id if attach else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_key", [False, True])
async def test_start_replays_a_spent_key_without_reserving_and_drains_the_slot(
    body_key: bool,
) -> None:
    store, engine = make_store()
    session_id, key, header_key = uuid4(), uuid4(), uuid4()
    set_field_row(
        engine.conn,
        {
            "subject_id": session_id,
            "change_kind": "created",
            "subject_kind": "AgentSession",
        },
    )
    engine.conn.fetchval.return_value = "original"
    set_client_change_id(header_key if body_key else key)
    assert await store.start_session(
        SubmitAgentSession(title="s", idempotency_key=key if body_key else None),
        requested_actor="agent",
        audience="open",
    ) == (session_id, "original", 0)
    assert _peek_client_change_id() is None
    assert not any("INSERT INTO inquiries" in sql for sql in executed_sql(engine.conn))
    assert engine.conn.fetchrow.await_args is not None
    assert engine.conn.fetchrow.await_args.args[1:] == (key,)
    assert engine.conn.fetchval.await_args is not None
    assert engine.conn.fetchval.await_args.args[1:] == (session_id,)
    engine.conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("attach", [False, True])
@pytest.mark.parametrize("ended", [False, True])
async def test_resume_keeps_the_owner_hides_and_joins_requested_rooms(
    attach: bool,
    ended: bool,
) -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    set_field_row(
        engine.conn,
        {
            "id": session_id,
            "owner": "original",
            "account": "t@e",
            "status": "complete" if ended else "active",
            "agentsession_ended": at if ended else None,
            "agentsession_audience": "open",
        },
    )
    emit = cast(AsyncMockProtocol[object], AsyncMock())
    rooms = cast(AsyncMockProtocol[object], AsyncMock())
    policy = RecordingPolicy()
    store.access_policy = policy
    with (
        patch.object(store, "_emit_field_change", new=emit),
        patch.object(store, "_mutate_list_field_on_conn", new=rooms),
    ):
        receipt = await store._resume_session(
            SubmitAgentSession(title="ignored", cli_session_id="cli-1", rooms=["lab"]),
            api_key_id=None,
            actor="resumer",
            hide=True,
            attach=session_id if attach else None,
        )
    assert receipt == (session_id, "original", 0)
    assert policy.of("prepare_audience_change") == [None]
    assert len(policy.of("on_audience")) == 1
    assert policy.of("on_audience") == [
        (None, Resource(kind="session", id=str(session_id), account="t@e")),
    ]
    audience_change = emit.await_args_list[0]
    assert audience_change.args[4] == Snapshot(agentsession_audience="open")
    assert audience_change.kwargs["new"] == Snapshot(agentsession_audience="private")
    assert audience_change.kwargs["reason"] == "hidden by its owner on resume"
    assert all(call.kwargs["actor"] == "resumer" for call in emit.await_args_list)
    rooms.assert_awaited_once_with(
        engine.conn,
        session_id,
        "lab",
        column="agentsession_rooms",
        api_key_id=None,
        actor="resumer",
        include=True,
    )
    assert [call.args[3] for call in emit.await_args_list] == (
        ["agentsession_audience", "agentsession_ended", "status"]
        if ended
        else ["agentsession_audience"]
    )
    assert engine.conn.fetchrow.await_args is not None
    assert engine.conn.fetchrow.await_args.args[1:] == (
        (session_id,) if attach else ("cli-1", None)
    )


@pytest.mark.asyncio
async def test_a_resume_without_a_correlated_row_returns_none() -> None:
    store, _engine = make_store()
    assert (
        await store._resume_session(
            SubmitAgentSession(title="s", cli_session_id="absent"),
            api_key_id=None,
            actor="agent",
            hide=False,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["private", "staff"])
async def test_private_resume_never_rewrites_private_or_staff_audiences(
    held: str,
) -> None:
    store, engine = make_store()
    if held == "private":
        await store._hide_resumed(
            cast("Conn", engine.conn),
            session_id=uuid4(),
            account="t@e",
            held=held,
            api_key_id=None,
            actor="agent",
            principal=None,
        )
    else:
        with pytest.raises(ConflictError, match="never hides"):
            await store._hide_resumed(
                cast("Conn", engine.conn),
                session_id=uuid4(),
                account="t@e",
                held=held,
                api_key_id=None,
                actor="agent",
                principal=None,
            )
    engine.conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("polled", [False, True])
async def test_liveness_writes_the_poll_place_or_only_refreshes_a_watched_session(
    polled: bool,
) -> None:
    store, engine = make_store()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    session_id = uuid4()
    await store.record_session_seen(
        session_id,
        at=at,
        polled=polled,
        place=RunPlace(machine="box", run="r", cli="codex", cwd="/work"),
    )
    assert engine.conn.execute.await_args is not None
    sql, *args = engine.conn.execute.await_args.args
    assert args == [
        session_id,
        at,
        at - timedelta(minutes=1),
        *(["box", "r", "codex", "/work"] if polled else []),
    ]
    assert ("INSERT INTO session_liveness" in str(sql)) == polled


@pytest.mark.asyncio
async def test_silent_sessions_and_live_resolution_project_ids_and_rooms() -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    engine.conn.fetch.side_effect = [
        [{"session_id": session_id}],
        [{"id": session_id, "agentsession_rooms": ["lab"]}],
        [{"id": session_id, "agentsession_rooms": None}],
    ]
    assert await store.silent_sessions(before=at) == [session_id]
    assert await store.resolve_live_sessions("agent", room="lab") == [
        (session_id, ("lab",)),
    ]
    assert await store.resolve_live_sessions("agent") == [(session_id, ())]
    calls = engine.conn.fetch.await_args_list
    assert calls[0].args[1:] == (at,)
    assert "NOT liveness.reaped AND liveness.last_seen <= $1" in str(calls[0].args[0])
    assert "inquiries.agentsession_ended IS NULL" in str(calls[0].args[0])
    assert calls[1].args[1:] == ("agent", "lab")
    assert "$2 = ANY(agentsession_rooms)" in str(calls[1].args[0])
    assert calls[2].args[1:] == ("agent",)
    assert "ANY(agentsession_rooms)" not in str(calls[2].args[0])
    assert all("ORDER BY created, id" in str(call.args[0]) for call in calls[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [False, True])
async def test_reaper_ends_only_the_still_silent_session_and_marks_it_reaped(
    eligible: bool,
) -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    set_field_row(engine.conn, TestEndSession._live_row())
    engine.conn.fetchval.return_value = at if eligible else None
    emit = cast(AsyncMockProtocol[object], AsyncMock())
    with patch.object(store, "_emit_field_change", new=emit):
        assert await store.reap_session(session_id, before=at, actor="reaper") == (
            at if eligible else None
        )
    updates = [str(call.args[0]) for call in engine.conn.execute.await_args_list]
    assert any("SET reaped = TRUE" in sql for sql in updates) == eligible
    assert emit.await_count == (2 if eligible else 0)
    assert engine.conn.fetchrow.await_args is not None
    assert engine.conn.fetchrow.await_args.args == (_LOCK_SESSION_SQL, session_id)
    assert engine.conn.fetchval.await_args is not None
    assert engine.conn.fetchval.await_args.args[1:] == (session_id, at)
    if eligible:
        ended_change = emit.await_args_list[0]
        assert ended_change.args[3:] == (
            "agentsession_ended",
            Snapshot(agentsession_ended=None),
        )
        assert ended_change.kwargs["new"] == Snapshot(agentsession_ended=at)
        assert ended_change.kwargs["actor"] == "reaper"
        assert [
            call.args[1:]
            for call in engine.conn.execute.await_args_list
            if "SET agentsession_ended" in str(call.args[0])
        ] == [(at, "complete", session_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reaped", [False, True])
async def test_revival_reopens_only_a_reaped_ended_session(reaped: bool) -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    set_field_row(
        engine.conn,
        {
            **TestEndSession._live_row(),
            "agentsession_ended": at,
            "status": "complete",
        },
    )
    engine.conn.fetchval.return_value = reaped
    emit = cast(AsyncMockProtocol[object], AsyncMock())
    with patch.object(store, "_emit_field_change", new=emit):
        assert await store.revive_reaped_session(session_id, actor="agent") is reaped
    assert emit.await_count == (2 if reaped else 0)
    assert (
        any("SET reaped = FALSE" in sql for sql in executed_sql(engine.conn)) == reaped
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "Issue", "AgentSession"])
async def test_reopen_checks_the_locked_row_before_updating(kind: str | None) -> None:
    store, engine = make_store()
    if kind is not None:
        set_field_row(engine.conn, {**TestEndSession._live_row(), "kind": kind})
    if kind == "AgentSession":
        await store.reopen_session(uuid4(), conn=cast("Conn", engine.conn), actor="a")
    else:
        with pytest.raises(NotFoundError if kind is None else ConflictError):
            await store.reopen_session(
                uuid4(),
                conn=cast("Conn", engine.conn),
                actor="a",
            )
    engine.conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [False, True])
async def test_feed_binds_composite_bounds_and_returns_tail_in_display_order(
    tail: bool,
) -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    engine.conn.fetch.return_value = [
        {
            "session_id": session_id,
            "part": 2,
            "idx": idx,
            "kind": "UserMessage",
            "created": at,
            "timestamp": at,
            "model": "m",
            "payload": '{"content":"hi"}',
            "text": "hi",
            "owner": "agent",
            "agentsession_rooms": ["lab"],
            "agentsession_cli": "codex",
        }
        for idx in ((4, 3) if tail else (3, 4))
    ]
    cursor = (at, session_id, 2, 2)
    events = await store.read_feed(
        after=cursor,
        before=(at, session_id, 2, 5),
        since=at,
        until=at,
        scope=EVERY_SESSION,
        conversation=True,
        tail=tail,
        limit=2,
    )
    assert [
        (event.part, event.seq, event.actor, event.rooms, event.cli, event.text)
        for event in events
    ] == [
        (2, 3, "agent", ["lab"], "codex", "hi"),
        (2, 4, "agent", ["lab"], "codex", "hi"),
    ]
    assert events[0].message == {"content": "hi"}
    assert engine.conn.fetch.await_args is not None
    sql, *args = engine.conn.fetch.await_args.args
    assert args[:10] == [*cursor, at, session_id, 2, 5, at, at]
    assert " > ($1, $2, $3, $4)" in str(sql)
    assert " < ($5, $6, $7, $8)" in str(sql)
    assert f"(CASE WHEN {CONVERSATION} THEN true ELSE false END)" in str(sql)
    assert ("ORDER BY e.created DESC" in str(sql)) == tail
    assert "e.created >= $9" in str(sql)
    assert "e.created <= $10" in str(sql)
    assert "e.idx < (SELECT m.records FROM session_manifests m" in str(sql)
    assert args[-1] == 2
    assert (events[0].session_id, events[0].created, events[0].timestamp) == (
        session_id,
        at,
        at,
    )
    assert (events[0].kind, events[0].model) == ("UserMessage", "m")


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [False, True])
async def test_reaper_leaves_missing_and_already_ended_sessions_alone(
    ended: bool,
) -> None:
    store, engine = make_store()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    set_field_row(
        engine.conn,
        {**TestEndSession._live_row(), "agentsession_ended": at} if ended else None,
    )
    engine.conn.fetchval.return_value = at
    assert await store.reap_session(uuid4(), before=at, actor="reaper") is None
    assert executed_sql(engine.conn) == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [False, True])
async def test_revival_leaves_missing_and_live_sessions_alone(exists: bool) -> None:
    store, engine = make_store()
    set_field_row(engine.conn, TestEndSession._live_row() if exists else None)
    engine.conn.fetchval.return_value = True
    assert await store.revive_reaped_session(uuid4(), actor="agent") is False
    assert executed_sql(engine.conn) == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
async def test_reopen_clears_ended_and_restarts_liveness_with_audits() -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    set_field_row(
        engine.conn,
        {
            **TestEndSession._live_row(),
            "agentsession_ended": at,
            "status": "complete",
        },
    )
    emit = cast(AsyncMockProtocol[object], AsyncMock())
    with patch.object(store, "_emit_field_change", new=emit):
        await store.reopen_session(
            session_id,
            conn=cast("Conn", engine.conn),
            actor="resumer",
        )
    assert [call.args for call in engine.conn.execute.await_args_list] == [
        (
            (
                "UPDATE inquiries SET agentsession_ended = NULL, "
                "status = 'active', modified = clock_timestamp() WHERE id = $1"
            ),
            session_id,
        ),
        (
            (
                "UPDATE session_liveness SET reaped = FALSE, last_seen = clock_timestamp() "
                "WHERE session_id = $1"
            ),
            session_id,
        ),
    ]
    assert [(call.args[3:], call.kwargs["new"]) for call in emit.await_args_list] == [
        (
            ("agentsession_ended", Snapshot(agentsession_ended=at)),
            Snapshot(agentsession_ended=None),
        ),
        (("status", Snapshot(status="complete")), Snapshot(status="active")),
    ]
    assert all(call.kwargs["actor"] == "resumer" for call in emit.await_args_list)
    assert all(call.kwargs["api_key_id"] is None for call in emit.await_args_list)


@pytest.mark.asyncio
async def test_a_poll_without_place_stores_empty_location_fields() -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    await store.record_session_seen(session_id, at=at, polled=True)
    assert engine.conn.execute.await_args is not None
    assert engine.conn.execute.await_args.args[1:] == (
        session_id,
        at,
        at - timedelta(minutes=1),
        None,
        None,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_a_captured_resume_without_owner_keeps_its_empty_routing_name() -> None:
    store, engine = make_store()
    session_id = uuid4()
    set_field_row(
        engine.conn,
        {"id": session_id, "owner": None, "agentsession_ended": None},
    )
    assert await store._resume_session(
        SubmitAgentSession(title="s", cli_session_id="cli-1"),
        api_key_id=None,
        actor="agent",
        hide=False,
    ) == (session_id, "", 0)
    assert executed_sql(engine.conn) == ["BEGIN", "COMMIT"]


@pytest.mark.asyncio
async def test_unbounded_feed_maps_null_context_without_conversation_filter() -> None:
    store, engine = make_store()
    session_id = uuid4()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    engine.conn.fetch.return_value = [
        {
            "session_id": session_id,
            "part": 0,
            "idx": 0,
            "kind": "UserMessage",
            "created": at,
            "timestamp": None,
            "model": None,
            "payload": "{}",
            "text": "",
            "owner": None,
            "agentsession_rooms": None,
            "agentsession_cli": None,
        },
    ]
    (event,) = await store.read_feed(scope=EVERY_SESSION, limit=3)
    assert (event.actor, event.rooms, event.cli, event.model, event.timestamp) == (
        "",
        [],
        None,
        None,
        None,
    )
    assert (event.message, event.text, event.part, event.seq) == ({}, "", 0, 0)
    assert engine.conn.fetch.await_args is not None
    sql, *args = engine.conn.fetch.await_args.args
    assert f"(CASE WHEN {CONVERSATION} THEN true ELSE false END)" not in str(sql)
    assert "e.created >=" not in str(sql)
    assert "e.created <=" not in str(sql)
    assert "ORDER BY e.created ASC" in str(sql)
    assert args[-1] == 3


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
