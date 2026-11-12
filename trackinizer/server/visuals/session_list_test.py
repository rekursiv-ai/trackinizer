"""The caller's own sessions: which are listed, in what order, and what each says."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import AsyncMock, Mock

import uuid

from treekle import immutable

import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, RecordingPolicy
from trackinizer.sagent.session.types import (
    AgentToAgentMessage,
    AssistantMessage,
    ContextState,
    ToolCall,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.server.access_policy import Principal
from trackinizer.server.audience import SessionReader
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.visuals import session_list
from trackinizer.server.visuals.session_lines import (
    LINE_CHARS,
    LastLine,
    SessionLines,
)
from trackinizer.server.visuals.session_list import (
    SessionList,
    _placed,
    _Places,
    list_own_sessions,
)
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire import wire_science_chat


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import DatabaseEngine
    from trackinizer.sagent.session.types import SessionRecord
    from trackinizer.server.store.core import Store


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_EMAIL: Final = "ada@example.com"
_KEY: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OPEN: Final = SessionReader(audiences=frozenset({"open"}), email=_EMAIL)
_NOW: Final[datetime] = datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def store(store: Store) -> Store:
    """Return a fresh store with the caller's user row."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Ada', 'writer', 'active')",
            _USER,
            _EMAIL,
        )
        # A chat is read by its starter: the account whose key opened its first session.
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_222222', 'writer')",
            _KEY,
            _USER,
        )
    return store


async def _session(
    store: Store,
    *,
    account: str = _EMAIL,
    records: Sequence[SessionRecord] = (),
    live_records: int | None = None,
    created: datetime = _NOW,
    ended: bool = False,
    audience: str = "open",
    cli_session_id: str | None = None,
    labels: Sequence[str] = (),
) -> uuid.UUID:
    """Store a session holding ``records``; ``live_records`` bounds its manifest."""
    session_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await insert_inquiry(
            conn,
            session_id,
            "AgentSession",
            values={
                "title": "claude session",
                "account": account,
                "labels": sorted(labels),
                "created": created,
                "agentsession_audience": audience,
                "agentsession_cli": "claude",
                "agentsession_cli_session_id": cli_session_id,
                "agentsession_opened_by_api_key_id": _KEY,
            },
        )
    if records:
        await store.append_session_records(
            session_id,
            [
                SessionRecordRow.of(
                    session_id=session_id,
                    part=0,
                    idx=idx,
                    record=record,
                )
                for idx, record in enumerate(records)
            ],
        )
        _ = await store.upsert_session_manifest(
            session_id,
            name="s.jsonl",
            metadata=immutable({}),
            ir_id=uuid.uuid4(),
            format="claude",
            records=len(records) if live_records is None else live_records,
        )
    if ended:
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE inquiries SET status = 'complete', agentsession_ended = $2 "
                "WHERE id = $1",
                session_id,
                created,
            )
    return session_id


async def _issue(store: Store, *, title: str, labels: Sequence[str] = ()) -> uuid.UUID:
    """Store an Issue."""
    issue_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await insert_inquiry(
            conn,
            issue_id,
            "Issue",
            values={"title": title, "account": _EMAIL, "labels": sorted(labels)},
        )
    return issue_id


async def _edge(
    store: Store,
    *,
    child: uuid.UUID,
    kind: str,
    parent: uuid.UUID,
) -> None:
    """Link two rows directly, as an edge write leaves them."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "SELECT $1, f.kind, $2, t.kind, $3 FROM inquiries AS f, inquiries AS t "
            "WHERE f.id = $1 AND t.id = $2",
            child,
            parent,
            kind,
        )


async def _list(
    store: Store,
    *,
    inbound: InboundQueue | None = None,
    reader: SessionReader = _OPEN,
    days: int = 14,
    limit: int = 500,
) -> SessionList:
    """List the caller's sessions."""
    return await list_own_sessions(
        store.engine,
        email=_EMAIL,
        reader=reader,
        inbound=inbound or InboundQueue(),
        days=days,
        limit=limit,
    )


def _turn(at: datetime) -> AssistantMessage:
    """One answer written at ``at``."""
    return AssistantMessage(content="Done.", timestamp=at.isoformat())


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_recent_and_open_sessions_are_listed_by_last_turn_and_no_others(
    store: Store,
) -> None:
    month = _NOW - timedelta(days=30)
    recent = await _session(store, records=[_turn(_NOW - timedelta(hours=1))])
    stale_open = await _session(store, created=_NOW - timedelta(days=60))
    _ = await _session(store, records=[_turn(month)], created=month, ended=True)
    _ = await _session(store, account="grace@example.com", records=[_turn(_NOW)])
    old_but_active = await _session(
        store,
        created=_NOW - timedelta(days=90),
        ended=True,
        records=[_turn(_NOW - timedelta(minutes=5))],
    )

    listed = await _list(store)

    assert [each.id for each in listed.sessions] == [old_but_active, recent, stale_open]
    assert listed.sessions[1].last_activity == _NOW - timedelta(hours=1)
    assert listed.sessions[2].last_activity == _NOW - timedelta(days=60)
    assert not listed.more
    first = await _list(store, limit=1)
    assert ([each.id for each in first.sessions], first.more) == (
        [old_but_active],
        True,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_the_caller_does_not_read_is_left_out(store: Store) -> None:
    hidden = await _session(store, audience="staff")

    assert (await _list(store)).sessions == []
    staff = SessionReader(audiences=frozenset({"open", "staff"}), email=_EMAIL)
    assert [each.id for each in (await _list(store, reader=staff)).sessions] == [hidden]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_live_run_says_its_host_and_a_lapsed_one_where_it_last_ran(
    store: Store,
) -> None:
    now = [0.0]
    inbound = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
    live = await _session(store, records=[_turn(_NOW)])
    lapsed = await _session(store, records=[_turn(_NOW - timedelta(hours=1))])
    never = await _session(store, records=[_turn(_NOW - timedelta(hours=2))])
    inbound.mark_poller(lapsed, host="build-box", cli="codex")
    now[0] = 60.0
    inbound.mark_poller(live, host="workstation", cli="claude", instance="a" * 32)
    _ = inbound.report_state(live, instance="a" * 32, state="running")

    sessions = {
        each.id: each for each in (await _list(store, inbound=inbound)).sessions
    }

    assert (sessions[live].live, sessions[live].host, sessions[live].state) == (
        True,
        "workstation",
        "running",
    )
    assert (sessions[lapsed].live, sessions[lapsed].host) == (False, "build-box")
    assert (sessions[lapsed].state, sessions[never].host) == ("idle", None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_goal_is_the_last_one_logged_and_the_first_line_a_persons(
    store: Store,
) -> None:
    stamp = {"$inbound": {"room": "chat-1", "source": "ada@example.com"}}
    delivered = "[chat-1] ada@example.com: Fix the CI hook\nTrackinizer context (verify with trax): Issue#1"
    claude_goal = {"attachment": {"sentinel": True, "condition": "CI is green"}}
    codex_goal = {"goal": {"status": "active", "objective": "Ship the console"}}
    session = await _session(
        store,
        live_records=6,
        records=[
            UserMessage(content="<command-name>/clear</command-name>"),
            UserMessage(content="# AGENTS.md instructions for /repo"),
            UserMessage(content=delivered, extra=immutable(stamp)),
            ContextState(kind="goal_status", extra=immutable(claude_goal)),
            UncategorizedRecord(
                kind="event_msg/thread_goal_updated",
                payload=immutable(codex_goal),
            ),
            UserMessage(content="Second line"),
            # Past the manifest's live prefix: a compaction dropped it.
            ContextState(
                kind="goal_status",
                extra=immutable({"attachment": {"condition": "gone"}}),
            ),
        ],
    )
    quiet = await _session(store, records=[_turn(_NOW)])

    sessions = {each.id: each for each in (await _list(store)).sessions}

    assert (sessions[session].goal, sessions[session].first_line) == (
        "Ship the console",
        "Fix the CI hook",
    )
    assert (sessions[quiet].goal, sessions[quiet].first_line) == (None, None)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_last_line_is_the_last_said_on_one_line_by_whom_and_no_tool_output(
    store: Store,
) -> None:
    stamp = {"$inbound": {"room": "chat-1", "source": "grace@example.com"}}
    delivered = (
        "[chat-1] grace@example.com: Ship   it\n"
        "Trackinizer context (verify with trax): Issue#1"
    )
    answered = await _session(
        store,
        records=[
            UserMessage(content="Fix the hook"),
            AssistantMessage(content="Done:\n  two  files\n\nchanged."),
            ToolCall(call_id="c1", name="Bash", arguments=immutable({"cmd": "ls"})),
            AssistantMessage(content=""),
            ContextState(kind="goal_status", extra=immutable({"attachment": {}})),
        ],
    )
    typed = await _session(
        store,
        live_records=3,
        records=[
            AssistantMessage(content="Hello."),
            UserMessage(content="Run the suite"),
            UserMessage(content="<command-name>/clear</command-name>"),
            # Past the manifest's live prefix: a compaction dropped it.
            AssistantMessage(content="gone"),
        ],
    )
    shared = await _session(
        store,
        records=[
            AssistantMessage(content="Ready."),
            UserMessage(content=delivered, extra=immutable(stamp)),
        ],
    )
    asked = await _session(
        store,
        records=[AgentToAgentMessage(content="x" * 300, sender=_EMAIL)],
    )
    quiet = await _session(store)

    sessions = {each.id: each.last_line for each in (await _list(store)).sessions}

    assert sessions[answered] == LastLine(author=None, text="Done: two files changed.")
    assert sessions[typed] == LastLine(author=_EMAIL, text="Run the suite")
    assert sessions[shared] == LastLine(author="grace@example.com", text="Ship it")
    assert sessions[asked] == LastLine(author=_EMAIL, text="x" * LINE_CHARS)
    assert sessions[quiet] is None


def test_a_placed_session_sits_on_the_first_of_the_callers_canvases_it_names() -> None:
    mine, other = uuid.uuid4(), uuid.uuid4()
    places = _Places(workspace={mine: None})

    def placed(*labels: str) -> uuid.UUID | None:
        return _placed(cast("Record", {"labels": list(labels)}), places=places)

    assert placed("git:o@main", "canvas:not-a-uuid", f"canvas:{other}") is None
    assert placed(f"canvas:{other}", "canvas:bad", f"canvas:{mine}") == mine
    assert _placed(cast("Record", {"labels": None}), places=places) is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_campaign_sessions_name_their_issues_and_each_issue_its_parents(
    store: Store,
) -> None:
    workspace = await _issue(store, title="ARC3", labels=["workspace"])
    meta = await _issue(store, title="meta-013")
    campaign = await _issue(store, title="meta-013 E1")
    await _edge(store, child=campaign, kind="narrows", parent=meta)
    await _edge(store, child=campaign, kind="produced_by", parent=meta)
    await _edge(store, child=campaign, kind="produced_by", parent=workspace)
    lead = await _session(store)
    worker = await _session(store)
    await _edge(store, child=lead, kind="produced_by", parent=meta)
    await _edge(store, child=worker, kind="produced_by", parent=campaign)
    await _edge(store, child=worker, kind="produced_by", parent=workspace)

    listed = await _list(store)

    produced = {each.id: each.produced_by for each in listed.sessions}
    assert (produced[lead], produced[worker]) == ([meta], [campaign])
    assert [(each.id, each.parent_ids) for each in listed.issues] == [
        (meta, []),
        (campaign, [meta]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["TRUE", "FALSE"])
async def test_own_session_listing_joins_places_issues_and_live_state_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
    predicate: str,
) -> None:
    chat, placed, foreign, overflow = (uuid.uuid4() for _ in range(4))
    conversation, placed_canvas, issue, parent = (uuid.uuid4() for _ in range(4))
    engine = FakeEngine()
    rows = [
        _listing_row(chat, chat=wire_science_chat.chat_session_id(conversation)),
        _listing_row(
            placed,
            labels=[
                "git:main",
                "canvas:invalid",
                f"canvas:{uuid.uuid4()}",
                f"canvas:{placed_canvas}",
            ],
        ),
        _listing_row(foreign, labels=["canvas:invalid", f"canvas:{uuid.uuid4()}"]),
        _listing_row(overflow),
    ]
    engine.conn.fetch.side_effect = [
        rows,
        [{"from_id": chat, "to_id": issue}, {"from_id": placed, "to_id": issue}],
        [
            {
                "id": parent,
                "seq": 1,
                "title": "Meta",
                "status": "active",
                "parents": [],
            },
            {
                "id": issue,
                "seq": 2,
                "title": "Work",
                "status": "complete",
                "parents": [parent],
            },
        ],
    ]
    line = LastLine(author=None, text="Done")
    lines = AsyncMock(
        return_value={
            chat: SessionLines(
                goal="Ship",
                first_line="Please fix",
                last_line=line,
            ),
        },
    )
    states = AsyncMock(return_value={chat: "running"})
    monkeypatch.setattr(session_list, "session_lines", lines)
    monkeypatch.setattr(session_list, "live_run_states", states)
    policy = RecordingPolicy()
    monkeypatch.setattr(RecordingPolicy, "read_filter", Mock(return_value=predicate))
    inbound = InboundQueue()
    inbound.mark_poller(placed, host="old-box")
    inbound.forget_poller(placed)
    inbound.mark_poller(chat, host="live-box")
    before = datetime.now(UTC)

    listed = await list_own_sessions(
        cast("DatabaseEngine", engine),
        email=_EMAIL,
        reader=_OPEN,
        inbound=inbound,
        days=2,
        limit=3,
        policy=policy,
        principal=Principal(email=_EMAIL, role="viewer"),
    )

    assert listed.more
    assert [each.id for each in listed.sessions] == [chat, placed, foreign]
    first, second, third = listed.sessions
    assert first.conversation_id == conversation
    assert (first.goal, first.first_line, first.last_line) == (
        "Ship",
        "Please fix",
        line,
    )
    assert (first.live, first.state, first.host) == (True, "running", "live-box")
    assert second.produced_by == [issue]
    assert (second.live, second.state, second.host) == (False, "idle", "old-box")
    assert (third.canvas_id, third.workspace_id, third.host) == (None, None, None)
    assert (third.goal, third.first_line, third.last_line, third.produced_by) == (
        None,
        None,
        None,
        [],
    )
    assert [(each.id, each.parent_ids) for each in listed.issues] == [
        (parent, []),
        (issue, [parent]),
    ]
    lines.assert_awaited_once_with(
        engine.conn,
        session_ids=[chat, placed, foreign],
        account=_EMAIL,
    )
    states.assert_awaited_once_with(
        engine.conn,
        inbound=inbound,
        session_ids=[chat, placed, foreign],
    )
    calls = engine.conn.fetch.await_args_list
    recent = calls[0]
    produced, issues = calls[-2:]
    assert recent.args[1] == _EMAIL
    since = recent.args[2]
    assert isinstance(since, datetime)
    assert before - timedelta(days=2) <= since <= datetime.now(UTC) - timedelta(days=2)
    assert recent.args[3] == 4
    assert "ORDER BY active DESC, s.id DESC LIMIT $3" in str(recent.args[0])
    assert produced.args[1:] == ([chat, placed, foreign],)
    assert issues.args[1:] == ([issue],)
    for call in (produced, issues):
        assert (
            "EXISTS (SELECT 1 FROM inquiries readable_inquiry" in str(call.args[0])
        ) == (predicate == "FALSE")
        assert "ARRAY['workspace']" in str(call.args[0])


@pytest.mark.asyncio
async def test_no_matching_sessions_returns_empty_lists_without_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(session_list, "session_lines", AsyncMock(return_value={}))
    monkeypatch.setattr(session_list, "live_run_states", AsyncMock(return_value={}))
    listed = await list_own_sessions(
        cast("DatabaseEngine", engine),
        email=_EMAIL,
        reader=_OPEN,
        inbound=InboundQueue(),
        days=1,
        limit=1,
    )
    assert listed == SessionList(sessions=[], issues=[], more=False)


def _listing_row(
    session_id: uuid.UUID,
    *,
    chat: str | None = None,
    labels: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "id": session_id,
        "seq": 1,
        "title": "Session",
        "status": "active",
        "cli": "claude",
        "created": _NOW,
        "ended": None,
        "active": _NOW,
        "chat": chat,
        "labels": list(labels),
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
