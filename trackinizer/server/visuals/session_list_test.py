"""The caller's own sessions: which are listed, in what order, and what each says."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import uuid

from treekle import immutable

import pytest
import pytest_asyncio

from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    ContextState,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.audience import SessionReader
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.visuals.session_list import SessionList, list_own_sessions
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.lib.postgres import PGliteEngine


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_EMAIL: Final = "ada@example.com"
_KEY: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OPEN: Final = SessionReader(audiences=frozenset({"open"}), email=_EMAIL)
_NOW: Final = datetime.now(UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a fresh store with the caller's user row."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    async with pglite_engine.acquire() as conn:
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
    yield built


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


async def _canvas(
    store: Store,
    *,
    user_id: uuid.UUID = _USER,
    conversation_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Store a canvas of ``user_id`` holding a chat, in a workspace or Scratch."""
    canvas_id = uuid.uuid4()
    metadata = {
        "name": f"canvas {canvas_id}",
        "conversation_id": None if conversation_id is None else str(conversation_id),
        "workspace_id": None if workspace_id is None else str(workspace_id),
    }
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) "
            "VALUES ($1, $2, '{}'::jsonb, $3)",
            canvas_id,
            user_id,
            {key: value for key, value in metadata.items() if value is not None},
        )
    return canvas_id


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
        user_id=_USER,
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
async def test_a_live_run_says_its_machine_and_a_lapsed_one_where_it_last_ran(
    store: Store,
) -> None:
    now = [0.0]
    inbound = InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0])
    live = await _session(store, records=[_turn(_NOW)])
    lapsed = await _session(store, records=[_turn(_NOW - timedelta(hours=1))])
    never = await _session(store, records=[_turn(_NOW - timedelta(hours=2))])
    inbound.mark_poller(lapsed, machine="build-box", cli="codex")
    now[0] = 60.0
    inbound.mark_poller(live, machine="workstation", cli="claude", instance="a" * 32)
    _ = inbound.report_state(live, instance="a" * 32, state="running")

    sessions = {
        each.id: each for each in (await _list(store, inbound=inbound)).sessions
    }

    assert (sessions[live].live, sessions[live].machine, sessions[live].state) == (
        True,
        "workstation",
        "running",
    )
    assert (sessions[lapsed].live, sessions[lapsed].machine) == (False, "build-box")
    assert (sessions[lapsed].state, sessions[never].machine) == ("idle", None)


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
async def test_a_chat_sits_on_its_canvas_and_a_placed_session_on_its_labelled_one(
    store: Store,
) -> None:
    workspace = await _issue(store, title="Console", labels=["workspace"])
    conversation = uuid.uuid4()
    chat_canvas = await _canvas(
        store,
        conversation_id=conversation,
        workspace_id=workspace,
    )
    scratch_canvas = await _canvas(store)
    chat = await _session(store, cli_session_id=chat_session_id(conversation))
    placed = await _session(store, labels=["git:o@main", f"canvas:{scratch_canvas}"])
    foreign = await _session(
        store,
        labels=[f"canvas:{uuid.uuid4()}", "canvas:not-a-uuid"],
    )

    sessions = {each.id: each for each in (await _list(store)).sessions}

    assert (sessions[chat].conversation_id, sessions[chat].canvas_id) == (
        conversation,
        chat_canvas,
    )
    assert sessions[chat].workspace_id == workspace
    assert (sessions[placed].canvas_id, sessions[placed].workspace_id) == (
        scratch_canvas,
        None,
    )
    assert (sessions[foreign].canvas_id, sessions[foreign].conversation_id) == (
        None,
        None,
    )


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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
