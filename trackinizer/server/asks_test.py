"""Tests for the asks open in a session's records, and releasing those nobody answers.

The records are the ones a run writes (:mod:`trackinizer.runs.asks`), stored
the way the append route stores them, so the SQL that finds an open ask reads what the
real capture path wrote.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast, override
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import asyncio
import time

from treekle import immutable

import pytest

from trackinizer.conftest import make_conn, make_store
from trackinizer.runs.asks import opened_record, resolved_record
from trackinizer.sagent.session.types import AssistantMessage, SystemMessage
from trackinizer.server import asks
from trackinizer.server.asks import (
    ASKS_PART,
    _released,
    ask_asker,
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
from trackinizer.server.store.session_ir import SessionManifest
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.bodies import SubmitAgentSession


if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from treekle import PlainTree

    from trackinizer.lib.postgres import PGliteEngine
    from trackinizer.runs.asks import Ask


_AT = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_LATER = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)


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
class TestAskAsker:
    """A card names whom it asks; the newest live card of an id is the one read."""

    async def test_the_asker_is_the_latest_live_card_of_that_id(
        self,
        store: Store,
    ) -> None:
        session, other = await _session(store), await _session(store)
        await write_records(
            store,
            session,
            [
                opened_record(_asking("a", asker="old@x.y"), at=_AT),
                opened_record(_asking("a", asker="ada@x.y"), at=_LATER),
                opened_record(_asking("b", asker="bob@x.y"), at=_AT),
                opened_record(command_ask("c"), at=_AT),
            ],
        )
        await write_records(
            store,
            session,
            [opened_record(_asking("a", asker="hidden@x.y"), at=_LATER)],
            name="unbound.jsonl",
            bound=0,
        )
        await write_records(
            store,
            other,
            [opened_record(_asking("z", asker="zed@x.y"), at=_AT)],
        )

        async with store.engine.acquire() as conn:
            askers = [
                await ask_asker(conn, session_id=session, ask_id=ask_id)
                for ask_id in ("a", "b", "c", "missing", "z")
            ]

        assert askers == ["ada@x.y", "bob@x.y", "", "", ""]


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
        pglite_db: PGliteEngine,
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
            _Purged(pglite_db, embed=StubEmbedder(), purged=purged),
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
        inbound = _Passes()
        inbound.mark_poller(session, cli="claude")
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.01),
        )
        try:
            inbound.forget_poller(session)
            # The second pass begins only once the first is done.
            await inbound.passed(2)
        finally:
            _ = task.cancel()
            _ = await asyncio.gather(task, return_exceptions=True)

        assert await _open(store, session) == []

    async def test_a_failed_pass_does_not_end_the_loop(
        self,
        store: Store,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        inbound = _Passes(fail_first=True)
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.01),
        )
        try:
            await inbound.passed(2)
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
        assistant = await _session(store, cli="assistant", cli_session_id="chat:3")
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
        inbound = _Passes()
        # Due at once, so the first pass sweeps; when a lease lapses is the fake-clock
        # test's ``test_release_loop_sweeps_after_its_delay_...``.
        task = asyncio.create_task(
            ask_release_loop(
                store,
                inbound=inbound,
                interval_sec=0.01,
                sweep_after_sec=0.0,
            ),
        )
        try:
            await inbound.passed(2)
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
        inbound = _Passes()
        task = asyncio.create_task(
            ask_release_loop(store, inbound=inbound, interval_sec=0.001),
        )
        try:
            await inbound.passed(5)
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


class _Passes(InboundQueue):
    """A queue that counts the loop's passes, so a test waits for one to finish."""

    def __init__(self, *, fail_first: bool = False) -> None:
        super().__init__()
        self.calls = 0
        self._fail_first = fail_first
        self._passing = asyncio.Event()

    @override
    def take_gone(self) -> list[UUID]:
        """Count a pass; the first raises, as a broken pass would, if asked to."""
        self.calls += 1
        self._passing.set()
        if self._fail_first and self.calls == 1:
            raise RuntimeError("blip")
        return super().take_gone()

    async def passed(self, count: int) -> None:
        """Return once pass ``count`` has begun, and so every pass before it is done."""
        async with asyncio.timeout(5):
            while self.calls < count:
                self._passing.clear()
                _ = await self._passing.wait()


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


def _asking(ask_id: str, *, asker: str) -> Ask:
    """Return a command ask whose card names the person it asks."""
    return replace(command_ask(ask_id), input={"asker": asker})


def _gone(ask_id: str) -> dict[str, str]:
    """Return what a released ask's ``$interaction_resolved`` says."""
    return {
        "id": ask_id,
        "option": "",
        "by": "released",
        "who": "",
        "decision": "released",
    }


@pytest.mark.asyncio
async def test_open_asks_preserve_order_deduplicate_and_scope_closures() -> None:
    conn = make_conn()
    first, second = uuid4(), uuid4()
    conn.fetch.return_value = [
        {"session_id": first, "opened": "b", "closed": None},
        {"session_id": first, "opened": "a", "closed": None},
        {"session_id": first, "opened": "b", "closed": "a"},
        {"session_id": second, "opened": "a", "closed": "b"},
        {"session_id": second, "opened": "", "closed": None},
    ]
    assert await asks.open_ask_ids(conn, session_id=first) == ["b"]
    assert await asks.waiting_sessions(conn, session_ids=[first, second]) == {
        first,
        second,
    }
    conn.fetch.return_value = []
    assert await asks.open_ask_ids(conn, session_id=first) == []
    assert await asks.waiting_sessions(conn, session_ids=[]) == set()
    assert conn.fetch.call_args is not None
    sql = conn.fetch.call_args.args[0]
    assert isinstance(sql, str)
    assert "r.idx < m.records" in sql
    assert "ORDER BY r.session_id, r.part, r.idx" in sql
    assert conn.fetch.call_args.args[1:] == (
        [],
        ["extra", "$interaction", "id"],
        ["extra", "$interaction_resolved", "id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, "ada@example.com"])
async def test_ask_asker_reads_the_latest_live_card(answer: str | None) -> None:
    conn = make_conn()
    session = uuid4()
    conn.fetchval.return_value = answer
    assert await asks.ask_asker(conn, session_id=session, ask_id="a") == (answer or "")
    assert conn.fetchval.call_args is not None
    sql = conn.fetchval.call_args.args[0]
    assert isinstance(sql, str)
    assert "r.idx < m.records" in sql
    assert "ORDER BY r.part DESC, r.idx DESC LIMIT 1" in sql
    assert conn.fetchval.call_args.args[1:] == (
        session,
        "a",
        ["extra", "$interaction", "input", "asker"],
        ["extra", "$interaction", "id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_release_appends_typed_records_to_the_server_part(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    store, _ = make_store()
    session, ir = uuid4(), uuid4()
    manifest = (
        SessionManifest(
            part=7,
            name=ASKS_PART,
            metadata=immutable({}),
            ir_id=ir,
            format="",
            records=3,
        )
        if existing
        else None
    )
    read = AsyncMock(return_value=manifest)
    upsert = AsyncMock(return_value=7)
    append = AsyncMock(return_value=2)
    monkeypatch.setattr(asks, "open_ask_ids", AsyncMock(return_value=["a", "b"]))
    monkeypatch.setattr(store, "read_session_manifest", read)
    monkeypatch.setattr(store, "upsert_session_manifest", upsert)
    monkeypatch.setattr(store, "append_session_records", append)
    assert await asks.release_open_asks(store, session_id=session, at=_AT) == ["a", "b"]
    read.assert_awaited_once_with(session, name=ASKS_PART)
    assert upsert.call_args is not None
    assert append.call_args is not None
    assert upsert.call_args.kwargs["records"] == (5 if existing else 2)
    assert upsert.call_args.kwargs["format"] == ""
    assert upsert.call_args.kwargs["metadata"] == {}
    if existing:
        assert upsert.call_args.kwargs["ir_id"] == ir
    else:
        assert isinstance(upsert.call_args.kwargs["ir_id"], type(ir))
    rows = cast(list[SessionRecordRow], append.call_args.args[1])
    assert [(row.session_id, row.part, row.idx, row.record()) for row in rows] == [
        (session, 7, (3 if existing else 0) + index, _released(ask_id, at=_AT))
        for index, ask_id in enumerate(["a", "b"])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_release_writes_nothing_when_quiet_or_conflicted(
    monkeypatch: pytest.MonkeyPatch,
    conflict: bool,
) -> None:
    store, _ = make_store()
    read = AsyncMock(return_value=None)
    upsert = AsyncMock(side_effect=ConflictError("ended"))
    append = AsyncMock()
    monkeypatch.setattr(
        asks,
        "open_ask_ids",
        AsyncMock(return_value=["a"] if conflict else []),
    )
    monkeypatch.setattr(store, "read_session_manifest", read)
    monkeypatch.setattr(store, "upsert_session_manifest", upsert)
    monkeypatch.setattr(store, "append_session_records", append)
    assert await asks.release_open_asks(store, session_id=uuid4(), at=_AT) == []
    assert read.await_count == int(conflict)
    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_passes_count_only_released_asks_and_skip_live_pollers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    gone, quiet, live = uuid4(), uuid4(), uuid4()
    inbound = InboundQueue()
    for session in (gone, quiet):
        inbound.mark_poller(session, cli="claude")
        inbound.forget_poller(session)
    release = AsyncMock(side_effect=[["a", "b"], []])
    monkeypatch.setattr(asks, "release_open_asks", release)
    assert await asks.release_gone_runs(store, inbound=inbound, at=_AT) == 2
    assert await asks.release_gone_runs(store, inbound=inbound, at=_AT) == 0
    assert {call.kwargs["session_id"] for call in release.call_args_list} == {
        gone,
        quiet,
    }
    engine.conn.fetch.return_value = [{"id": live}, {"id": gone}]
    inbound.mark_poller(live, cli="codex")
    release.reset_mock(side_effect=True)
    release.return_value = ["c"]
    assert await asks.release_orphaned_asks(store, inbound=inbound, at=_AT) == 1
    release.assert_awaited_once_with(store, session_id=gone, at=_AT)
    assert engine.conn.fetch.call_args is not None
    assert engine.conn.fetch.call_args.args[1:] == (
        ["claude", "codex"],
        "chat:",
        ["extra", "$interaction", "id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep_after", [None, 0.0])
async def test_release_loop_retries_errors_and_sweeps_only_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sweep_after: float | None,
) -> None:
    store, _ = make_store()
    inbound = InboundQueue(poller_ttl_sec=0)
    sleep = AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()])
    gone = AsyncMock(side_effect=[RuntimeError("pass failed"), 0, 0])
    orphaned = AsyncMock(return_value=0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", Mock(return_value=1.0))
    monkeypatch.setattr(asks, "release_gone_runs", gone)
    monkeypatch.setattr(asks, "release_orphaned_asks", orphaned)
    with pytest.raises(asyncio.CancelledError):
        await asks.ask_release_loop(
            store,
            inbound=inbound,
            interval_sec=0.01,
            sweep_after_sec=sweep_after,
        )
    assert gone.await_count == 3
    assert orphaned.await_count == 1
    assert sleep.call_args is not None
    assert sleep.call_args.args == (0.01,)
    assert "ask release pass failed" in caplog.text


@pytest.mark.asyncio
async def test_release_loop_sweeps_after_its_delay_with_the_pass_s_store_and_time(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, _ = make_store()
    inbound = InboundQueue(poller_ttl_sec=0)
    passes: list[tuple[str, object, object, datetime]] = []

    async def gone(given: object, *, inbound: object, at: datetime) -> int:
        passes.append(("gone", given, inbound, at))
        if len(passes) == 1:
            raise RuntimeError("pass failed")
        return 0

    async def orphaned(given: object, *, inbound: object, at: datetime) -> int:
        passes.append(("orphaned", given, inbound, at))
        return 0

    monkeypatch.setattr(
        asyncio,
        "sleep",
        AsyncMock(side_effect=[None, None, None, None, asyncio.CancelledError()]),
    )
    # Started at 0 with a 5 s delay, the second pass (at 1) is too early and the
    # third (at 6) sweeps, and the fourth reads no clock: the sweep is done. The
    # failed first pass reads no clock either. Only ``asks``
    # sees these readings: the event loop reads the real clock too.
    monkeypatch.setattr(
        asks,
        "time",
        SimpleNamespace(monotonic=Mock(side_effect=[0.0, 1.0, 6.0])),
    )
    monkeypatch.setattr(asks, "release_gone_runs", gone)
    monkeypatch.setattr(asks, "release_orphaned_asks", orphaned)
    with pytest.raises(asyncio.CancelledError):
        await asks.ask_release_loop(
            store,
            inbound=inbound,
            interval_sec=0.01,
            sweep_after_sec=5.0,
        )
    assert [name for name, *_ in passes] == [
        "gone",
        "gone",
        "gone",
        "orphaned",
        "gone",
    ]
    assert all(given is store and queue is inbound for _, given, queue, _ in passes)
    assert all(at.tzinfo is UTC for *_, at in passes)
    assert passes[3][3] is passes[2][3]
    assert [r.getMessage() for r in caplog.records] == [
        "ask release pass failed; retrying next cycle",
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
