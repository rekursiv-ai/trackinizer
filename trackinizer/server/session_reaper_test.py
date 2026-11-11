"""Tests for closing sessions whose agents died, and reopening ones that return.

Most run the real SQL on PGlite: what a server restart keeps, and what a lock
rechecks, belong to the database, and a fake store would only assume them. The
unit tests pin what the reaper itself does with the store's answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, cast, override
from uuid import UUID, uuid4

import asyncio
import logging

import pytest

from trackinizer.server import session_reaper
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.session_reaper import (
    REAPER_ACTOR,
    STALE_AFTER,
    reap_silent_sessions,
    revive_if_reaped,
    session_reaper_loop,
)
from trackinizer.server.store.core import Store
from trackinizer.server.store.store_test_support import (
    open_agent_session,
    polled_session,
)
from trackinizer.types.inquiries import AgentSession, Inquiry


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from trackinizer.lib.postgres import PGliteEngine


# In the past, so a session reopened now is never silent as of ``_SILENT``.
_SEEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_SILENT = _SEEN + STALE_AFTER
"""The first time a session last heard from at ``_SEEN`` counts as gone."""


class TestReaperUnit:
    """What the reaper does with the store's answers."""

    @pytest.mark.asyncio
    async def test_a_pass_closes_each_silent_session_the_store_still_finds_silent(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        returned, dead = uuid4(), uuid4()
        store = _FakeStore(silent=[returned, dead], refuses=frozenset({returned}))
        inbound = InboundQueue()
        for session in (returned, dead):
            inbound.mark_poller(session)
            inbound.enqueue(session, Inbound(text="queued"))

        with caplog.at_level(logging.INFO):
            closed = await reap_silent_sessions(
                cast("Store", store),
                inbound=inbound,
                now=_SILENT,
            )

        assert closed == 1
        assert store.listed_before == [_SEEN]
        assert store.closed == [(dead, _SEEN, REAPER_ACTOR)]
        assert (inbound.pending(dead), inbound.has_poller(dead)) == (0, False)
        assert (inbound.pending(returned), inbound.has_poller(returned)) == (1, True)
        assert _reaper_messages(caplog) == [
            f"closed silent session {dead} (last seen {_SEEN})",
        ]

    @pytest.mark.asyncio
    async def test_a_revival_is_logged_only_when_it_reopened(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reaped, live = uuid4(), uuid4()
        store = _FakeStore(silent=[], reaped=frozenset({reaped}))

        with caplog.at_level(logging.INFO):
            revived = [
                await revive_if_reaped(cast("Store", store), session_id=session)
                for session in (reaped, live)
            ]

        assert revived == [True, False]
        assert store.reopened == [(reaped, REAPER_ACTOR), (live, REAPER_ACTOR)]
        assert _reaper_messages(caplog) == [
            f"reopened session {reaped}: its run came back",
        ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReap:
    """One pass over the sessions nothing has been heard from."""

    async def test_a_silent_session_is_closed_at_its_last_poll(
        self,
        store: Store,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = await polled_session(store, at=_SEEN)
        inbound = InboundQueue()
        inbound.mark_poller(session)
        inbound.enqueue(session, Inbound(text="undeliverable"))

        with caplog.at_level(logging.INFO):
            closed = await reap_silent_sessions(store, inbound=inbound, now=_SILENT)

        assert closed == 1
        row = await _row(store, session)
        assert (row.status, row.ended) == ("complete", _SEEN)
        assert await _audit_actors(store, session) == {REAPER_ACTOR}
        assert inbound.pending(session) == 0
        assert not inbound.has_poller(session)
        assert _reaper_messages(caplog) == [
            f"closed silent session {session} (last seen {_SEEN})",
        ]

    async def test_only_a_session_silent_for_the_whole_window_is_closed(
        self,
        store: Store,
    ) -> None:
        silent = await polled_session(store, at=_SEEN)
        recent = await polled_session(store, at=_SEEN + timedelta(seconds=1))

        assert (
            await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT) == 1
        )
        assert (await _row(store, silent)).status == "complete"
        assert (await _row(store, recent)).status == "active"

    async def test_a_session_that_never_polls_is_never_closed(
        self,
        store: Store,
    ) -> None:
        """A capture-only producer has no poller whose silence could mean anything."""
        session = await open_agent_session(store)
        await store.record_session_seen(session, at=_SEEN, polled=False)

        later = _SILENT + timedelta(days=1)
        assert await reap_silent_sessions(store, inbound=InboundQueue(), now=later) == 0
        assert (await _row(store, session)).status == "active"

    async def test_an_upload_keeps_a_polling_session_open(self, store: Store) -> None:
        """A bridge polls only when its agent is ready; its uploads say it is alive."""
        session = await polled_session(store, at=_SEEN)
        uploaded = _SEEN + timedelta(minutes=10)
        await store.record_session_seen(session, at=uploaded, polled=False)

        inbound = InboundQueue()
        assert await reap_silent_sessions(store, inbound=inbound, now=_SILENT) == 0
        later = uploaded + STALE_AFTER
        assert await reap_silent_sessions(store, inbound=inbound, now=later) == 1
        assert (await _row(store, session)).ended == uploaded

    async def test_a_session_heard_from_mid_pass_is_left_open(
        self,
        pglite_db: PGliteEngine,
        store: Store,
    ) -> None:
        """Each close rechecks under lock, so the list a pass reads can go stale."""
        returning = await polled_session(store, at=_SEEN)
        dead = await polled_session(store, at=_SEEN + timedelta(seconds=1))
        racing = _PollsMidPass(pglite_db, embed=StubEmbedder(), polls=returning)

        later = _SILENT + timedelta(seconds=1)
        assert (
            await reap_silent_sessions(racing, inbound=InboundQueue(), now=later) == 1
        )
        assert (await _row(store, returning)).status == "active"
        assert (await _row(store, dead)).status == "complete"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestRevive:
    """A run closed for silence that comes back gets its session back."""

    async def test_only_a_reaped_session_is_revived(
        self,
        store: Store,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reaped = await polled_session(store, at=_SEEN)
        ended_by_its_run = await polled_session(store, at=_SEEN)
        _ = await store.end_session(ended_by_its_run, ended=_SEEN, actor="agent")
        live = await polled_session(store, at=_SILENT)
        _ = await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT)

        with caplog.at_level(logging.INFO):
            revived = [
                await revive_if_reaped(store, session_id=session)
                for session in (reaped, ended_by_its_run, live, uuid4())
            ]

        assert revived == [True, False, False, False]
        assert [(await _row(store, s)).status for s in (reaped, ended_by_its_run)] == [
            "active",
            "complete",
        ]
        assert _reaper_messages(caplog) == [
            f"reopened session {reaped}: its run came back",
        ]

    async def test_a_revived_session_can_be_closed_again(self, store: Store) -> None:
        session = await polled_session(store, at=_SEEN)
        _ = await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT)
        assert await revive_if_reaped(store, session_id=session)

        # Heard from as of the revival, not its old silence.
        assert (
            await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT) == 0
        )
        assert await _closed_once_silent(store, session) == 1

    async def test_a_session_resumed_after_a_close_can_be_closed_again(
        self,
        store: Store,
    ) -> None:
        """A resume reopens through ``start_session``, not a revival."""
        session = await polled_session(store, at=_SEEN, cli_session_id="resumable")
        _ = await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT)
        resumed = await open_agent_session(store, cli_session_id="resumable")
        assert resumed == session

        assert (
            await reap_silent_sessions(store, inbound=InboundQueue(), now=_SILENT) == 0
        )
        assert await _closed_once_silent(store, session) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_restarted_server_still_closes_and_reopens(
    pglite_db: PGliteEngine,
    store: Store,
) -> None:
    """Nothing the reaper needs is held in the process that saw the session."""
    session = await polled_session(store, at=_SEEN)

    restarted = await _restart(pglite_db)
    assert (
        await reap_silent_sessions(restarted, inbound=InboundQueue(), now=_SILENT) == 1
    )
    again = await _restart(pglite_db)
    assert await revive_if_reaped(again, session_id=session)
    assert (await _row(again, session)).status == "active"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_loop_closes_a_silent_session(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await polled_session(store, at=datetime.now(UTC) - STALE_AFTER)
    reaped = asyncio.Event()
    reap = session_reaper.reap_silent_sessions

    monkeypatch.setattr(
        session_reaper,
        "reap_silent_sessions",
        partial(_reaped_then, reaped, reap),
    )
    loop = asyncio.create_task(
        session_reaper_loop(store, inbound=InboundQueue(), interval_sec=0.01),
    )
    try:
        await asyncio.wait_for(reaped.wait(), 5)
        assert (await _row(store, session)).status != "active"
    finally:
        _ = loop.cancel()


@pytest.mark.asyncio
async def test_the_loop_survives_a_failed_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = _BrokenStore()
    loop = asyncio.create_task(
        session_reaper_loop(
            cast("Store", broken),
            inbound=InboundQueue(),
            interval_sec=0.01,
        ),
    )
    # A second pass is the loop surviving the first one's failure.
    await asyncio.wait_for(broken.retried.wait(), timeout=5)
    running = not loop.done()
    _ = loop.cancel()

    assert running
    assert "session reaper pass failed" in caplog.text


class _PollsMidPass(Store):
    """A store on which one session polls between a pass's list and its close."""

    def __init__(
        self,
        engine: PGliteEngine,
        *,
        embed: StubEmbedder,
        polls: UUID,
    ) -> None:
        super().__init__(engine, embed=embed)
        self._polls = polls

    @override
    async def reap_session(
        self,
        session_id: UUID,
        *,
        before: datetime,
        actor: Inquiry.Actor,
    ) -> datetime | None:
        if session_id == self._polls:
            await self.record_session_seen(session_id, at=_SILENT, polled=True)
        return await super().reap_session(session_id, before=before, actor=actor)


class _FakeStore:
    """Lists fixed sessions as silent, and records each close and reopen asked of it."""

    def __init__(
        self,
        *,
        silent: list[UUID],
        refuses: frozenset[UUID] = frozenset(),
        reaped: frozenset[UUID] = frozenset(),
    ) -> None:
        self._silent = silent
        self._refuses = refuses
        self._reaped = reaped
        self.listed_before: list[datetime] = []
        self.closed: list[tuple[UUID, datetime, str]] = []
        self.reopened: list[tuple[UUID, str]] = []

    async def silent_sessions(self, *, before: datetime) -> list[UUID]:
        self.listed_before.append(before)
        return self._silent

    async def reap_session(
        self,
        session_id: UUID,
        *,
        before: datetime,
        actor: str,
    ) -> datetime | None:
        if session_id in self._refuses:
            return None
        self.closed.append((session_id, before, actor))
        return before

    async def revive_reaped_session(self, session_id: UUID, *, actor: str) -> bool:
        self.reopened.append((session_id, actor))
        return session_id in self._reaped


class _BrokenStore:
    """A store whose database is down; ``retried`` is set on its second pass."""

    def __init__(self) -> None:
        self.passes = 0
        self.retried = asyncio.Event()

    async def silent_sessions(self, *, before: datetime) -> list[UUID]:
        del before
        self.passes += 1
        if self.passes >= 2:
            self.retried.set()
        raise RuntimeError("database blip")


async def _closed_once_silent(store: Store, session_id: UUID) -> int:
    """Run a pass once ``session_id`` has been silent for the window, from now."""
    row = await _row(store, session_id)
    assert row.status == "active"
    later = datetime.now(UTC) + STALE_AFTER
    return await reap_silent_sessions(store, inbound=InboundQueue(), now=later)


async def _restart(engine: PGliteEngine) -> Store:
    """Return a store as a freshly started server builds it, on the same database."""
    restarted = Store(engine, embed=StubEmbedder())
    await restarted.bootstrap()
    return restarted


async def _row(store: Store, session_id: UUID) -> AgentSession:
    """Return the session's row."""
    row = await store.get_inquiry(session_id)
    assert isinstance(row, AgentSession)
    return row


def _reaper_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return what the reaper itself logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == session_reaper.__name__
    ]


async def _audit_actors(store: Store, session_id: UUID) -> set[str]:
    """Return who authored the session's end and status audit rows."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor FROM change_log WHERE subject_id = $1 "
            "AND kind IN ('agentsession_ended', 'status')",
            session_id,
        )
    return {str(row["actor"]) for row in rows}


async def _reaped_then(
    done: asyncio.Event,
    reap: Callable[..., Awaitable[int]],
    store: Store,
    *,
    inbound: InboundQueue,
    now: datetime,
) -> int:
    """Run ``reap``'s pass, then set ``done`` however it ended."""
    try:
        return await reap(store, inbound=inbound, now=now)
    finally:
        done.set()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
