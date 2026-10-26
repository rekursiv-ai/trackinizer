"""Tests for the asks open in a session's records, and releasing those nobody answers.

The records are the ones a run writes (:mod:`trackinizer.runs.asks`), stored
the way the append route stores them, so the SQL that finds an open ask reads what the
real capture path wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, override

import asyncio

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.runs.asks import opened_record, resolved_record
from trackinizer.sagent.session.types import AssistantMessage, SystemMessage
from trackinizer.server.asks import (
    ASKS_PART,
    _released,
    ask_release_loop,
    open_ask_ids,
    release_gone_runs,
    release_open_asks,
    release_orphaned_asks,
    waiting_sessions,
)
from trackinizer.server.asks_test_support import (
    command_ask,
    ended_ask,
    write_records,
)
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from uuid import UUID

    from treekle import PlainTree

    from trackinizer.lib.postgres import PGliteEngine


_AT = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_LATER = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a store on an empty, freshly bootstrapped database."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


class TestReleasedRecord:
    """The server writes a release as a run does, without importing the run's code."""

    def test_it_is_the_record_a_run_writes_for_a_release(self) -> None:
        released = _released("a", at=_AT)

        assert released == resolved_record(ended_ask("a", by="released"), at=_AT)
        assert released.extra["$interaction_resolved"] == _gone("a")
        assert (released.subtype, released.content) == (
            "interaction_resolved",
            "Released",
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestOpenAskIds:
    """An ask is open from its record to the record that resolves it."""

    async def test_an_ask_is_open_until_a_record_resolves_it(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(
            store,
            session,
            [
                opened_record(command_ask("a"), at=_AT),
                opened_record(command_ask("b"), at=_AT),
                resolved_record(ended_ask("a", by="chat"), at=_AT),
                opened_record(command_ask("c"), at=_AT),
            ],
        )

        assert await _open(store, session) == ["b", "c"]

    async def test_other_records_and_other_sessions_open_nothing(
        self,
        store: Store,
    ) -> None:
        session, other = await _session(store), await _session(store)
        await write_records(
            store,
            session,
            [
                SystemMessage(
                    subtype="init",
                    content="hello",
                    timestamp=_AT.isoformat(),
                ),
                AssistantMessage(content="ask?"),
            ],
        )
        await write_records(store, other, [opened_record(command_ask("x"), at=_AT)])

        assert await _open(store, session) == []
        assert await _open(store, other) == ["x"]

    async def test_a_record_past_the_part_s_live_prefix_is_not_read(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(
            store,
            session,
            [opened_record(command_ask("a"), at=_AT)],
            bound=0,
        )

        assert await _open(store, session) == []

    async def test_an_end_that_precedes_its_ask_in_part_order_still_ends_it(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(
            store,
            session,
            [resolved_record(ended_ask("a", by="run"), at=_AT)],
            name="first.jsonl",
        )
        await write_records(
            store,
            session,
            [opened_record(command_ask("a"), at=_AT)],
            name="second.jsonl",
        )

        assert await _open(store, session) == []

    async def test_which_of_some_sessions_wait_is_asked_in_one_read(
        self,
        store: Store,
    ) -> None:
        waits, done, quiet = [await _session(store) for _ in range(3)]
        await write_records(store, waits, [opened_record(command_ask("a"), at=_AT)])
        await write_records(
            store,
            done,
            [
                opened_record(command_ask("a"), at=_AT),
                resolved_record(ended_ask("a", by="chat"), at=_AT),
            ],
        )
        async with store.engine.acquire() as conn:
            found = await waiting_sessions(conn, session_ids=[waits, done, quiet])
            none = await waiting_sessions(conn, session_ids=[])

        assert (found, none) == ({waits}, set())

    async def test_an_ask_in_one_part_is_resolved_by_a_record_in_another(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(
            store,
            session,
            [opened_record(command_ask("a"), at=_AT)],
            name="run.jsonl",
        )
        await write_records(
            store,
            session,
            [resolved_record(ended_ask("a", by="run"), at=_AT)],
            name="more.jsonl",
        )

        assert await _open(store, session) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReleaseOpenAsks:
    """Asks nobody can answer any more are ended, as records the web reads."""

    async def test_each_open_ask_is_ended_as_the_run_would_have_released_it(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(
            store,
            session,
            [
                opened_record(command_ask("a"), at=_AT),
                opened_record(command_ask("b"), at=_AT),
                resolved_record(ended_ask("a", by="chat"), at=_AT),
            ],
        )

        released = await release_open_asks(store, session_id=session, at=_LATER)

        assert released == ["b"]
        assert await _open(store, session) == []
        assert await _stored(store, session, name=ASKS_PART) == [
            resolved_record(ended_ask("b", by="released"), at=_LATER),
        ]

    async def test_a_later_release_adds_to_the_same_part(self, store: Store) -> None:
        session = await _session(store)
        await write_records(store, session, [opened_record(command_ask("a"), at=_AT)])
        _ = await release_open_asks(store, session_id=session, at=_AT)
        await write_records(
            store,
            session,
            [
                opened_record(command_ask("b"), at=_AT),
                opened_record(command_ask("c"), at=_AT),
            ],
            name="later.jsonl",
        )

        released = await release_open_asks(store, session_id=session, at=_LATER)

        assert released == ["b", "c"]
        assert [
            record.content for record in await _stored(store, session, name=ASKS_PART)
        ] == ["Released"] * 3
        assert [
            record.extra["$interaction_resolved"]
            for record in await _stored(store, session, name=ASKS_PART)
        ] == [_gone(each) for each in "abc"]
        parts = [m.name for m in await store.read_session_manifests(session)]
        assert parts.count(ASKS_PART) == 1

    async def test_a_session_with_nothing_open_gets_no_part(self, store: Store) -> None:
        session = await _session(store)
        await write_records(store, session, [opened_record(command_ask("a"), at=_AT)])
        await write_records(
            store,
            session,
            [resolved_record(ended_ask("a", by="chat"), at=_AT)],
            name="more.jsonl",
        )

        assert await release_open_asks(store, session_id=session, at=_AT) == []
        names = [m.name for m in await store.read_session_manifests(session)]
        assert ASKS_PART not in names

    async def test_an_ended_session_takes_no_record(self, store: Store) -> None:
        session = await _session(store)
        await write_records(store, session, [opened_record(command_ask("a"), at=_AT)])
        _ = await store.end_session(
            session,
            ended=_AT,
            cli_session_id=None,
            api_key_id=None,
            actor="agent",
        )

        assert await release_open_asks(store, session_id=session, at=_LATER) == []

    async def test_the_part_the_server_writes_is_not_resumable(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(store, session, [opened_record(command_ask("a"), at=_AT)])

        _ = await release_open_asks(store, session_id=session, at=_AT)

        [mine] = [
            each
            for each in await store.read_session_manifests(session)
            if each.name == ASKS_PART
        ]
        assert (mine.format, mine.records, mine.opened_by_api_key_id) == ("", 1, None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReleaseGoneRuns:
    """The runs whose lease ended lose the asks they held."""

    async def test_a_run_whose_lease_was_revoked_releases_its_asks(
        self,
        store: Store,
    ) -> None:
        gone, live = await _session(store), await _session(store)
        for session in (gone, live):
            await write_records(
                store,
                session,
                [opened_record(command_ask("a"), at=_AT)],
            )
        inbound = InboundQueue()
        inbound.mark_poller(gone, cli="claude")
        inbound.mark_poller(live, cli="claude")
        inbound.forget_poller(gone)

        released = await release_gone_runs(store, inbound=inbound, at=_LATER)

        assert released == 1
        assert await _open(store, gone) == []
        assert await _open(store, live) == ["a"]

    async def test_a_session_that_takes_no_record_does_not_stop_the_pass(
        self,
        store: Store,
        pglite_engine: PGliteEngine,
    ) -> None:
        purged, gone = await _session(store), await _session(store)
        for session in (purged, gone):
            await write_records(
                store,
                session,
                [opened_record(command_ask("a"), at=_AT)],
            )
        inbound = InboundQueue()
        for session in (purged, gone):
            inbound.mark_poller(session, cli="claude")
            inbound.forget_poller(session)

        released = await release_gone_runs(
            _Purged(pglite_engine, embed=StubEmbedder(), purged=purged),
            inbound=inbound,
            at=_LATER,
        )

        assert released == 1
        assert await _open(store, gone) == []
        assert await _open(store, purged) == ["a"]

    async def test_the_loop_releases_a_run_that_went_offline(
        self,
        store: Store,
    ) -> None:
        session = await _session(store)
        await write_records(store, session, [opened_record(command_ask("a"), at=_AT)])
        inbound = InboundQueue()
        inbound.mark_poller(session, cli="claude")
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.01),
        )
        try:
            inbound.forget_poller(session)
            deadline = asyncio.get_running_loop().time() + 5.0
            while await _open(store, session):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)

        assert await _open(store, session) == []

    async def test_a_failed_pass_does_not_end_the_loop(
        self,
        store: Store,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        inbound = _FailsOnce()
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.01),
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while inbound.calls < 2:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)

        assert any("ask release pass failed" in r.getMessage() for r in caplog.records)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
class TestReleaseOrphanedAsks:
    """A run that died while the server was down left asks no lease will ever end."""

    async def test_a_live_cli_chat_with_no_run_behind_it_releases_its_asks(
        self,
        store: Store,
    ) -> None:
        orphan = await _session(store, cli="claude", cli_session_id="chat:1")
        await write_records(store, orphan, [opened_record(command_ask("a"), at=_AT)])

        released = await release_orphaned_asks(store, inbound=InboundQueue(), at=_LATER)

        assert released == 1
        assert await _open(store, orphan) == []

    async def test_only_a_cli_chat_with_no_lease_and_no_end_is_swept(
        self,
        store: Store,
    ) -> None:
        held = await _session(store, cli="codex", cli_session_id="chat:2")
        assistant = await _session(store, cli="sagent", cli_session_id="chat:3")
        plain = await _session(store, cli="claude", cli_session_id="abc")
        ended = await _session(store, cli="claude", cli_session_id="chat:4")
        for session in (held, assistant, plain, ended):
            await write_records(
                store,
                session,
                [opened_record(command_ask("a"), at=_AT)],
            )
        _ = await store.end_session(
            ended,
            ended=_AT,
            cli_session_id=None,
            api_key_id=None,
            actor="agent",
        )
        inbound = InboundQueue()
        inbound.mark_poller(held, cli="codex")

        released = await release_orphaned_asks(store, inbound=inbound, at=_LATER)

        assert released == 0
        for session in (held, assistant, plain):
            assert await _open(store, session) == ["a"]

    async def test_an_answered_chat_releases_nothing(self, store: Store) -> None:
        answered = await _session(store, cli="claude", cli_session_id="chat:5")
        await write_records(
            store,
            answered,
            [
                opened_record(command_ask("a"), at=_AT),
                resolved_record(ended_ask("a", by="chat"), at=_AT),
            ],
        )

        assert (
            await release_orphaned_asks(store, inbound=InboundQueue(), at=_LATER) == 0
        )
        names = [m.name for m in await store.read_session_manifests(answered)]
        assert ASKS_PART not in names

    async def test_the_loop_sweeps_once_when_a_lease_would_have_lapsed(
        self,
        store: Store,
    ) -> None:
        orphan = await _session(store, cli="claude", cli_session_id="chat:6")
        await write_records(store, orphan, [opened_record(command_ask("a"), at=_AT)])
        task = asyncio.create_task(
            ask_release_loop(
                store,
                inbound=InboundQueue(),
                interval_sec=0.01,
                sweep_after_sec=0.05,
            ),
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while await _open(store, orphan):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)

        assert await _open(store, orphan) == []

    async def test_the_loop_gives_a_run_its_lease_span_to_poll_before_it_sweeps(
        self,
        store: Store,
    ) -> None:
        live = await _session(store, cli="claude", cli_session_id="chat:7")
        await write_records(store, live, [opened_record(command_ask("a"), at=_AT)])
        inbound = _CountsPasses()
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.001),
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while inbound.calls < 5:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.001)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)

        assert await _open(store, live) == ["a"]


class _Purged(Store):
    """A store in which one session was purged after its asks were read."""

    def __init__(
        self,
        engine: PGliteEngine,
        *,
        embed: StubEmbedder,
        purged: UUID,
    ) -> None:
        super().__init__(engine, embed=embed)
        self._purged = purged

    @override
    async def upsert_session_manifest(
        self,
        session_id: UUID,
        *,
        name: str,
        metadata: Mapping[str, PlainTree],
        ir_id: UUID,
        format: str,
        records: int,
        api_key_id: UUID | None = None,
        exclusive: bool = False,
    ) -> int:
        if session_id == self._purged:
            raise NotFoundError(f"session {session_id} not found")
        return await super().upsert_session_manifest(
            session_id,
            name=name,
            metadata=metadata,
            ir_id=ir_id,
            format=format,
            records=records,
            api_key_id=api_key_id,
            exclusive=exclusive,
        )


class _CountsPasses(InboundQueue):
    """A queue that counts the loop's passes."""

    calls: int = 0

    @override
    def take_gone(self) -> list[UUID]:
        """Count a pass, answer nothing."""
        self.calls += 1
        return []


class _FailsOnce(InboundQueue):
    """A queue whose first ``take_gone`` raises, as a broken pass would."""

    calls: int = 0

    @override
    def take_gone(self) -> list[UUID]:
        """Fail the first time, answer nothing after."""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("blip")
        return []


async def _session(
    store: Store,
    *,
    cli: str = "claude",
    cli_session_id: str | None = None,
) -> UUID:
    """Open a session for a run to write into."""
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(
            title="asks",
            cli=cli,
            cli_session_id=cli_session_id,
            account="t@e",
        ),
        requested_actor="agent",
        audience="open",
    )
    return session_id


async def _open(store: Store, session_id: UUID) -> list[str]:
    """Return the ids of the asks open in a session."""
    async with store.engine.acquire() as conn:
        return await open_ask_ids(conn, session_id=session_id)


async def _stored(store: Store, session_id: UUID, *, name: str) -> list[SystemMessage]:
    """Read back the records of one part of a session."""
    manifest = await store.read_session_manifest(session_id, name=name)
    assert manifest is not None
    rows = await store.read_session_records(session_id, part=manifest.part)
    records = [row.record() for row in rows]
    assert all(isinstance(record, SystemMessage) for record in records)
    return [record for record in records if isinstance(record, SystemMessage)]


def _gone(ask_id: str) -> dict[str, str]:
    """Return what a released ask's ``$interaction_resolved`` says."""
    return {
        "id": ask_id,
        "option": "",
        "by": "released",
        "who": "",
        "decision": "released",
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
