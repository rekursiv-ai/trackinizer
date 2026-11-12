"""Tests for how ``Store`` watches a session's liveness and closes and reopens it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest

from trackinizer.server.notify import notify_after_commit, tx
from trackinizer.server.store.store_test_support import (
    last_seen,
    open_agent_session,
    polled_session,
)
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import AgentSession
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.store.core import Store


_SEEN: Final = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_LATER: Final = _SEEN + timedelta(hours=1)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestSeen:
    """How a sign of life is recorded."""

    async def test_a_sighting_is_written_only_once_a_minute_has_passed(
        self,
        store: Store,
    ) -> None:
        """A run polls continuously; writing each poll would cost a row per cycle."""
        session = await polled_session(store, at=_SEEN)
        minute = _SEEN + timedelta(minutes=1)

        await store.record_session_seen(session, at=minute, polled=True)
        await store.record_session_seen(session, at=minute, polled=False)
        assert await last_seen(store, session) == _SEEN
        later = minute + timedelta(seconds=1)
        await store.record_session_seen(session, at=later, polled=False)
        assert await last_seen(store, session) == later
        latest = later + timedelta(minutes=2)
        await store.record_session_seen(session, at=latest, polled=True)
        assert await last_seen(store, session) == latest

    async def test_a_session_that_never_polled_is_never_watched(
        self,
        store: Store,
    ) -> None:
        session = await open_agent_session(store)

        await store.record_session_seen(session, at=_SEEN, polled=False)

        assert await store.silent_sessions(before=_LATER) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestSilentSessions:
    async def test_lists_live_sessions_silent_since_before_longest_silent_first(
        self,
        store: Store,
    ) -> None:
        newer = await polled_session(store, at=_SEEN + timedelta(minutes=5))
        older = await polled_session(store, at=_SEEN)
        _ = await polled_session(store, at=_LATER + timedelta(seconds=1))
        ended = await polled_session(store, at=_SEEN)
        _ = await store.end_session(ended, ended=_SEEN, actor="agent")

        assert await store.silent_sessions(before=_LATER) == [older, newer]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReapSession:
    async def test_a_silent_session_ends_when_last_seen_and_is_not_listed_again(
        self,
        store: Store,
    ) -> None:
        session = await polled_session(store, at=_SEEN)

        assert await store.reap_session(session, before=_LATER, actor="reaper") == _SEEN

        row = await store.get_inquiry(session)
        assert isinstance(row, AgentSession)
        assert (row.status, row.ended) == ("complete", _SEEN)
        assert await store.silent_sessions(before=_LATER) == []
        assert await store.reap_session(session, before=_LATER, actor="reaper") is None

    async def test_a_session_heard_from_since_is_left_alone(
        self,
        store: Store,
    ) -> None:
        session = await polled_session(store, at=_SEEN)
        await store.record_session_seen(session, at=_LATER, polled=False)

        assert await store.reap_session(session, before=_SEEN, actor="reaper") is None
        assert await _status(store, session) == ("active", None)

    async def test_a_session_its_run_ended_is_left_alone(
        self,
        store: Store,
    ) -> None:
        session = await polled_session(store, at=_SEEN)
        _ = await store.end_session(session, ended=_LATER, actor="agent")

        assert await store.reap_session(session, before=_LATER, actor="reaper") is None
        assert await _status(store, session) == ("complete", _LATER)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReviveReapedSession:
    async def test_a_reaped_session_is_live_again(self, store: Store) -> None:
        session = await polled_session(store, at=_SEEN)
        _ = await store.reap_session(session, before=_LATER, actor="reaper")

        assert await store.revive_reaped_session(session, actor="reaper")

        assert await _status(store, session) == ("active", None)
        assert not await store.revive_reaped_session(session, actor="reaper")

    async def test_a_session_its_run_ended_stays_ended(self, store: Store) -> None:
        session = await polled_session(store, at=_SEEN)
        _ = await store.end_session(session, ended=_LATER, actor="agent")

        assert not await store.revive_reaped_session(session, actor="reaper")
        assert await _status(store, session) == ("complete", _LATER)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReopenSession:
    async def test_an_ended_session_reopens_and_a_live_one_is_unchanged(
        self,
        store: Store,
    ) -> None:
        ended = await polled_session(store, at=_SEEN)
        _ = await store.end_session(ended, ended=_LATER, actor="agent")
        live = await polled_session(store, at=_SEEN)

        await _reopen(store, ended)
        await _reopen(store, live)

        assert await _status(store, ended) == ("active", None)
        assert await _status(store, live) == ("active", None)
        assert await last_seen(store, live) == _SEEN

    async def test_an_unknown_id_is_not_found(self, store: Store) -> None:
        with pytest.raises(NotFoundError):
            await _reopen(store, uuid4())

    async def test_another_kind_is_refused(self, store: Store) -> None:
        issue = await store.submit_issue(SubmitIssue(title="t", account="t@e"))

        with pytest.raises(ConflictError, match="Only an AgentSession"):
            await _reopen(store, issue)


async def _reopen(store: Store, session_id: UUID) -> None:
    """Reopen ``session_id`` inside a transaction, as an authorized route does."""
    async with notify_after_commit(), store.engine.acquire() as conn, tx(conn):
        await store.reopen_session(session_id, conn=conn, actor="user")


async def _status(store: Store, session_id: UUID) -> tuple[str, datetime | None]:
    """Return the session's ``(status, ended)``."""
    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    return row.status, row.ended


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
