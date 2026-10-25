"""The canvas store: the Chat provider is stored, and a replay never undoes it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import asyncio
import uuid

from treekle import from_plain, immutable, json_loads

import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, executed_sql, make_conn
from trackinizer.lib.agent.types.sessions import (
    AgentToAgentMessage,
    AssistantMessage,
    UserMessage,
)
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.audience import NOBODY, reader_for
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import (
    IdempotencyReuseError,
    Inbound,
    InboundQueue,
)
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.visuals import canvas_store
from trackinizer.server.visuals.canvas_store import (
    AnswerNotOwnerError,
    AppliedOperation,
    CommandRefusedError,
    ProviderUnavailableError,
    apply_canvas_operation,
    canvas_for_chat,
    clear_chat_canvas,
    name_unnamed_canvas,
    new_canvas,
    open_chat_session,
    open_latest_canvas,
    read_canvas,
    send_chat_line,
    set_canvas_flags,
    set_canvas_workspace,
    set_session_campaign,
    settings_refs_for_chat,
    suggest_workspace_for_chat,
)
from trackinizer.server.visuals.canvases import (
    ApplyCanvasOperation,
    CanvasData,
    CanvasLayout,
    CanvasMetadata,
    CanvasSettingsRefs,
    CanvasState,
    Mark,
    MarkCovers,
    MarkPoint,
    VisualInstance,
    WorkspaceSuggestion,
)
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.chat_forks import ChatClosedError
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.audience import OPEN, PRIVATE
from trackinizer.wire.bodies import SubmitAgentSession, SubmitArtifact
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatForkAt,
    ChatSend,
    chat_actor,
    chat_session_id,
    fork_point_label,
)
from trackinizer.wire.wire_sessions import CanvasRecordContext


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asyncpg import Record

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine
    from trackinizer.wire.wire_science_chat import (
        ChatLineKind,
        ChatProvider,
        ChatSent,
    )


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_EMAIL: Final = "user@example.com"
_CANVAS: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_OTHER_USER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SOURCE_SQL: Final = (
    "SELECT id, revision, state, metadata FROM visual_canvases "
    "WHERE id = $1 AND user_id = $2 FOR UPDATE"
)
_CLEARED_SQL: Final = (
    "SELECT id, revision, state, metadata FROM visual_canvases "
    "WHERE user_id = $1 AND metadata->>'clear_key' = $2 LIMIT 1"
)
_INSERT_SQL: Final = (
    "INSERT INTO visual_canvases (id, user_id, metadata, state) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
    "RETURNING id, revision, state, metadata"
)


def _flags_engine(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> tuple[FakeEngine, list[tuple[object, ...]]]:
    """Return a fast canvas-flag store with a recorded owner-scoped update."""
    conn: AsyncMock = make_conn()
    calls: list[tuple[object, ...]] = []

    async def fetchrow(sql: str, *args: object) -> object:
        del sql
        calls.append(args)
        if row is None:
            return None
        stored = cast("Mapping[str, object]", row)
        canvas = cast("Mapping[str, object]", stored["metadata"])
        changes = cast("Mapping[str, object]", args[2])
        updated = dict(stored)
        updated["metadata"] = dict(canvas) | dict(changes)
        return updated

    async def with_partner_fake(
        conn: object,
        *,
        row: object,
        **_kwargs: object,
    ) -> CanvasState | None:
        del conn
        if row is None:
            return None
        return canvas_store.state_from_row(cast("Mapping[str, object]", row))

    conn.fetchrow.side_effect = fetchrow
    monkeypatch.setattr(canvas_store, "_with_partner", with_partner_fake)
    return FakeEngine(conn), calls


def _set_flags(
    engine: FakeEngine,
    *,
    hidden: bool | None = None,
    pinned: bool | None = None,
) -> CanvasState | None:
    """Update canvas flags without opening PGlite."""
    return asyncio.run(
        set_canvas_flags(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            canvas_id=_CANVAS,
            hidden=hidden,
            pinned=pinned,
            inbound=InboundQueue(),
            assistant=None,
        ),
    )


async def _seed(engine: PGliteEngine) -> None:
    await reset_schema(engine)
    await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'T', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            _CANVAS,
            _USER,
            {"name": "unnamed-canvas#1"},
            {"visuals": []},
        )


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_canvas(pglite_engine: PGliteEngine) -> PGliteEngine:
    """Bootstrap an empty canvas before the measured test call starts."""
    await _seed(pglite_engine)
    return pglite_engine


@pytest_asyncio.fixture(loop_scope="session")
async def empty_canvas_schema(pglite_engine: PGliteEngine) -> PGliteEngine:
    """Bootstrap only the schema; focused tests insert their rows directly."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    return pglite_engine


async def _canvas_state_without_partner(
    conn: object,
    *,
    row: object,
    **_kwargs: object,
) -> CanvasState:
    """Skip unrelated partner lookups while retaining the database-returned state."""
    del conn
    return canvas_store.state_from_row(cast("Mapping[str, object]", row))


async def _insert_clear_source(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    canvas: Mapping[str, object],
    state: Mapping[str, object],
) -> None:
    """Insert the owner and one source row without API setup helpers."""
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, $2, 'T', 'writer', 'active')",
        user_id,
        f"{user_id}@example.com",
    )
    await conn.execute(
        "INSERT INTO visual_canvases (id, user_id, metadata, state) "
        "VALUES ($1, $2, $3, $4)",
        canvas_id,
        user_id,
        canvas,
        state,
    )


async def _apply(
    engine: PGliteEngine,
    body: ApplyCanvasOperation,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
) -> AppliedOperation | None:
    return await apply_canvas_operation(
        Store(engine, embed=StubEmbedder()),
        user_id=_USER,
        canvas_id=_CANVAS,
        key=key,
        body=body,
        catalog=default_catalog(),
        inbound=inbound,
        assistant=None,
    )


@pytest.mark.asyncio
async def test_a_session_canvas_stores_its_attachment_and_json_state() -> None:
    conn = make_conn()
    conn.fetchrow.return_value = None
    session = uuid.uuid4()
    continuation = uuid.uuid4()
    data = CanvasData(visuals=[], continuation_record_id=continuation)
    await canvas_store._insert_canvas(
        cast("Conn", conn),
        user_id=_USER,
        name="Run",
        data=data,
        session_id=session,
    )
    conn.fetchrow.assert_awaited_once_with(
        _INSERT_SQL,
        ANY,
        _USER,
        {"name": "Run", "hidden": False, "pinned": False, "session_id": str(session)},
        {
            "visuals": [],
            "focused_instance": None,
            "agent_instructions": None,
            "continuation_record_id": str(continuation),
            "provider": "assistant",
            "marks": [],
            "marks_seq": 0,
            "marks_sent": 0,
            "opens_as": "open",
        },
    )


@pytest.mark.parametrize(
    ("sent", "expected"),
    [(0, list(range(6, 26))), (15, list(range(16, 26))), (25, [])],
)
@pytest.mark.asyncio
async def test_a_line_carries_its_screen_and_only_the_latest_unsent_marks(
    sent: int,
    expected: list[int],
) -> None:
    conn = make_conn()
    record = CanvasRecordContext(id=uuid.uuid4(), kind="Issue", seq=7, title="A task")
    conn.fetch.return_value = [record.model_dump()]
    visual = VisualInstance(
        id=uuid.uuid4(),
        type="trax.subgraph",
        version=1,
        record_id=record.id,
        params={"hops": 2},
    )
    marks = [
        Mark(
            id=uuid.UUID(int=seq),
            seq=seq,
            author="ada@example.test",
            at=datetime(2026, 10, 23, tzinfo=UTC),
            points=[MarkPoint(x=0.1, y=0.2), MarkPoint(x=0.6, y=0.8)],
            covers=MarkCovers(records=[record.id], instances=[visual.id]),
        )
        for seq in range(1, 26)
    ]
    continuation = uuid.uuid4()
    state = CanvasState(
        id=_CANVAS,
        revision=2,
        name="Chat",
        conversation_id=None,
        page=None,
        visuals=[visual],
        agent_instructions="Inspect the selected task",
        continuation_record_id=continuation,
        marks=marks,
        marks_sent=sent,
        marks_seq=25,
    )
    fork = ChatForkAt(session_id=uuid.uuid4(), part=2, idx=4)
    body = ChatSend(
        canvas_id=_CANVAS,
        text="Check this",
        page="#/console",
        trail=["#/list/Issue"],
        fork=fork,
    )
    conversation = uuid.uuid4()
    context = await canvas_store._line_context(
        cast("Conn", conn),
        body=body,
        state=state,
        canvas_id=_CANVAS,
        conversation_id=conversation,
        reader=NOBODY,
    )
    assert context.canvas_id == _CANVAS
    assert context.conversation_id == conversation
    assert context.continuation_record_id == continuation
    assert context.agent_instructions == "Inspect the selected task"
    assert context.fork == fork
    assert context.page is not None
    assert context.page.route == "#/console"
    assert [page.route for page in context.trail] == ["#/list/Issue"]
    assert len(context.visible_visuals) == 1
    shown = context.visible_visuals[0]
    assert (shown.id, shown.type, shown.record, shown.params) == (
        visual.id,
        "trax.subgraph",
        record,
        {"hops": 2},
    )
    assert [mark.id.int for mark in context.marks] == expected
    for mark in context.marks:
        assert mark.records == [record]
        assert mark.instances == [shown]
        assert (mark.left, mark.top, mark.right, mark.bottom) == (0.1, 0.2, 0.6, 0.8)


@pytest.mark.asyncio(loop_scope="session")
async def test_chat_run_access_requires_the_starters_key_and_a_live_cli(
    pglite_engine: PGliteEngine,
) -> None:
    original = await _original(pglite_engine)
    session, key = original.session, original.own_key
    async with pglite_engine.acquire() as conn:
        conversation = await canvas_store.conversation_of_session(
            conn,
            session_id=session,
        )
    assert conversation is not None
    state = CanvasState(
        id=_CANVAS,
        revision=0,
        name="Chat",
        page=None,
        visuals=[],
        provider="claude",
        conversation_id=conversation,
    )
    inbound = InboundQueue()
    async with pglite_engine.acquire() as conn:

        async def allowed(
            given: CanvasState = state,
            *,
            owner: uuid.UUID = _USER,
            credential: uuid.UUID = key,
        ) -> bool:
            return await canvas_store._chat_run_key_may_use(
                conn,
                state=given,
                owner_id=owner,
                api_key_id=credential,
                inbound=inbound,
                assistant=_KB,
            )

        assert not await allowed()
        inbound.mark_poller(session, cli="claude")
        assert await allowed()
        assert not await allowed(owner=_OTHER_USER)
        assert not await allowed(owner=_OTHER_USER, credential=original.assistant_key)
        assert not await allowed(credential=uuid.uuid4())
        inbound.mark_poller(session, cli="assistant")
        assert not await allowed(state.model_copy(update={"provider": "assistant"}))
        inbound.mark_poller(session, cli="claude")
        assert not await allowed(state.model_copy(update={"conversation_id": None}))
        assert not await allowed(state.model_copy(update={"provider": "codex"}))
        inbound.mark_poller(session, cli="codex")
        assert await allowed(state.model_copy(update={"provider": "codex"}))
        inbound.mark_poller(session, cli="claude")
        await conn.execute("UPDATE api_keys SET revoked_at = now() WHERE id = $1", key)
        assert not await allowed()
        await conn.execute("UPDATE api_keys SET revoked_at = NULL WHERE id = $1", key)
        await conn.execute("UPDATE inquiries SET labels = '{}' WHERE id = $1", session)
        assert not await allowed()
        await conn.execute(
            "UPDATE inquiries SET labels = $2, status = 'complete', "
            "agentsession_ended = now() WHERE id = $1",
            session,
            [SCIENCE_CHAT_LABEL],
        )
        assert not await allowed()


def _choose(*, revision: int, provider: str) -> ApplyCanvasOperation:
    return ApplyCanvasOperation.model_validate(
        {"revision": revision, "operation": {"kind": "provider", "provider": provider}},
    )


async def _laptop(engine: PGliteEngine, *, clis: list[str]) -> None:
    """Connect a confirmed machine of the user's that lists ``clis``."""
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('laptop', "
            "'user@example.com', 'user@example.com', now(), gen_random_uuid(), "
            "now(), jsonb_build_object('clis', $1::jsonb))",
            clis,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_provider_is_stored_and_a_replay_answers_with_it(
    pglite_engine: PGliteEngine,
) -> None:
    """A retried choice returns its receipt, with the provider it stored."""
    await _seed(pglite_engine)
    await _laptop(pglite_engine, clis=["claude"])
    inbound = InboundQueue()
    key = uuid.uuid4()

    chosen = await _apply(
        pglite_engine,
        _choose(revision=0, provider="claude"),
        key=key,
        inbound=inbound,
    )
    assert chosen is not None
    assert chosen.state.provider == "claude"
    assert chosen.state.partner is not None
    assert (chosen.state.partner.status, chosen.state.partner.session_id) == (
        "offline",
        None,
    )
    assert chosen.state.revision == 1

    replay = await _apply(
        pglite_engine,
        _choose(revision=0, provider="claude"),
        key=key,
        inbound=inbound,
    )
    assert replay is not None
    assert replay.replayed
    assert replay.state.provider == "claude"

    read = await read_canvas(
        pglite_engine,
        user_id=_USER,
        canvas_id=_CANVAS,
        inbound=inbound,
        assistant=None,
    )
    assert read is not None
    assert (read.provider, read.revision) == ("claude", 1)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_provider_the_owner_cannot_run_is_refused_with_its_reason(
    pglite_engine: PGliteEngine,
) -> None:
    """Without a machine that lists codex, choosing it changes nothing."""
    await _seed(pglite_engine)
    await _laptop(pglite_engine, clis=["claude"])

    with pytest.raises(ProviderUnavailableError, match="install codex on it"):
        _ = await _apply(
            pglite_engine,
            _choose(revision=0, provider="codex"),
            key=uuid.uuid4(),
            inbound=InboundQueue(),
        )
    kept = await _apply(
        pglite_engine,
        _choose(revision=0, provider="assistant"),
        key=uuid.uuid4(),
        inbound=InboundQueue(),
    )
    assert kept is not None
    assert (kept.state.provider, kept.state.revision) == ("assistant", 1)


async def _owner_key(engine: PGliteEngine) -> uuid.UUID:
    """Give the user an API key, as their machine's login does."""
    key = uuid.uuid4()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'k', 'h', 'trax_k1', 'writer')",
            key,
            _USER,
        )
    return key


async def _set_provider(engine: PGliteEngine, provider: str) -> None:
    async with engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET state = state || "
            "jsonb_build_object('provider', $2::text) WHERE id = $1",
            _CANVAS,
            provider,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cli_canvas_gets_a_chat_and_a_session_before_any_line(
    pglite_engine: PGliteEngine,
) -> None:
    """The canvas's owner opens it under their key; a second call finds the same."""
    await _seed(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    assert (
        await open_chat_session(
            store,
            user_id=_USER,
            canvas_id=_CANVAS,
            assistant=None,
        )
        is None
    )
    await _set_provider(pglite_engine, "claude")
    assert (
        await open_chat_session(
            store,
            user_id=_USER,
            canvas_id=_CANVAS,
            assistant=None,
        )
        is None
    ), "an owner with no key has nothing to open the session with"
    key = await _owner_key(pglite_engine)

    opened = await open_chat_session(
        store,
        user_id=_USER,
        canvas_id=_CANVAS,
        assistant=None,
    )
    again = await open_chat_session(
        store,
        user_id=_USER,
        canvas_id=_CANVAS,
        assistant=None,
    )

    assert opened is not None
    assert again == opened
    read = await read_canvas(
        pglite_engine,
        user_id=_USER,
        canvas_id=_CANVAS,
        inbound=InboundQueue(),
        assistant=None,
    )
    assert read is not None
    assert read.conversation_id is not None
    assert read.partner is not None
    assert (read.partner.session_id, read.partner.status) == (opened, "offline")
    async with pglite_engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT account, title, labels, agentsession_cli AS cli, "
            "agentsession_cli_session_id AS chat, "
            "agentsession_opened_by_api_key_id AS key FROM inquiries WHERE id = $1",
            opened,
        )
        assert (
            await canvas_store.read_chat_provider(
                conn,
                assistant=None,
                conversation_id=read.conversation_id,
            )
            == "claude"
        )
        assert (
            await canvas_store.read_chat_provider(
                conn,
                assistant=None,
                conversation_id=uuid.uuid4(),
            )
            == "assistant"
        )
    assert row is not None
    assert dict(row) == {
        "account": "user@example.com",
        "title": "Science chat",
        "labels": ["science-chat"],
        "cli": "claude",
        "chat": f"chat:{read.conversation_id}",
        "key": key,
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cli_chats_first_session_opens_with_its_canvas_opens_as(
    pglite_engine: PGliteEngine,
) -> None:
    """A canvas that chose ``private`` opens its chat private; one that did not, open."""
    await _seed(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    _ = await _owner_key(pglite_engine)
    await _set_provider(pglite_engine, "claude")
    chosen = uuid.uuid4()
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            chosen,
            _USER,
            {"name": "unnamed-canvas#2"},
            {"visuals": [], "provider": "claude", "opens_as": "private"},
        )

    opened = {
        canvas_id: await open_chat_session(
            store,
            user_id=_USER,
            canvas_id=canvas_id,
            assistant=None,
        )
        for canvas_id in (_CANVAS, chosen)
    }

    async with pglite_engine.acquire() as conn:
        audiences = {
            canvas_id: await conn.fetchval(
                "SELECT agentsession_audience FROM inquiries WHERE id = $1",
                session_id,
            )
            for canvas_id, session_id in opened.items()
        }
    assert audiences == {_CANVAS: OPEN, chosen: PRIVATE}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_new_canvas_starts_on_its_owners_preferred_provider(
    pglite_engine: PGliteEngine,
) -> None:
    """Unless its maker names one: the named provider wins."""
    await _seed(pglite_engine)
    async with pglite_engine.acquire() as conn:
        await conn.execute(
            'UPDATE users SET preferences = \'{"chat_provider": "codex"}\' '
            "WHERE id = $1",
            _USER,
        )

    async def made(provider: ChatProvider | None) -> ChatProvider:
        state = await new_canvas(
            pglite_engine,
            user_id=_USER,
            catalog=default_catalog(),
            inbound=InboundQueue(),
            assistant=None,
            provider=provider,
        )
        return state.provider

    assert [await made(None), await made("claude"), await made("assistant")] == [
        "codex",
        "claude",
        "assistant",
    ]


def test_set_canvas_flags_hides_an_owned_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding writes only the owner's canvas flag."""
    row = _ROW | {"metadata": {"name": "tails", "hidden": True}}
    engine, calls = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, hidden=True)

    assert changed is not None
    assert changed.hidden is True
    assert calls == [(_CANVAS, _USER, {"hidden": True})]


def test_set_canvas_flags_unhides_without_changing_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhiding leaves the canvas's pin flag intact."""
    row = _ROW | {"metadata": {"name": "tails", "hidden": True, "pinned": True}}
    engine, _ = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, hidden=False)

    assert changed is not None
    assert (changed.hidden, changed.pinned) == (False, True)


def test_set_canvas_flags_pins_without_changing_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning leaves the canvas's hidden flag intact."""
    row = _ROW | {"metadata": {"name": "tails", "hidden": True}}
    engine, _ = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, pinned=True)

    assert changed is not None
    assert (changed.hidden, changed.pinned) == (True, True)


def test_set_canvas_flags_is_owner_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A canvas update matches both its id and owner."""
    engine, calls = _flags_engine(monkeypatch, None)

    changed = asyncio.run(
        set_canvas_flags(
            cast("DatabaseEngine", engine),
            user_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
            canvas_id=_CANVAS,
            hidden=True,
            inbound=InboundQueue(),
            assistant=None,
        ),
    )

    assert changed is None
    assert calls == [
        (
            _CANVAS,
            uuid.UUID("99999999-9999-9999-9999-999999999999"),
            {"hidden": True},
        ),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_operations_and_replays_keep_canvas_flags(
    seeded_canvas: PGliteEngine,
) -> None:
    """Operation responses retain flags stored in canvas JSON, including replays."""
    inbound = InboundQueue()
    changed = await set_canvas_flags(
        seeded_canvas,
        user_id=_USER,
        canvas_id=_CANVAS,
        hidden=True,
        pinned=True,
        inbound=inbound,
        assistant=None,
    )
    assert changed is not None
    body = _choose(revision=0, provider="assistant")
    key = uuid.uuid4()

    applied = await _apply(seeded_canvas, body, key=key, inbound=inbound)
    replayed = await _apply(seeded_canvas, body, key=key, inbound=inbound)

    assert applied is not None
    assert replayed is not None
    assert replayed.replayed
    assert (applied.state.hidden, applied.state.pinned) == (True, True)
    assert (replayed.state.hidden, replayed.state.pinned) == (True, True)


def test_open_latest_skips_hidden_canvases() -> None:
    """The latest lookup filters hidden canvases; opening one makes no default."""
    engine, asked = _scripted(_ROW)
    default = AsyncMock(return_value=_DEFAULT_WORKSPACE)

    state = _open(engine, default=default)

    assert state.id == _CANVAS
    assert asked == [(_LATEST, (_USER,))]
    default.assert_not_awaited()


def test_open_latest_makes_a_canvas_in_the_default_when_all_others_are_hidden() -> None:
    """With no visible row, the store inserts after listing existing names."""
    engine, asked = _scripted(None, None, _ROW)
    default = AsyncMock(return_value=_DEFAULT_WORKSPACE)

    async def names(sql: str, *args: object) -> list[dict[str, object]]:
        if "FROM machines" in sql:
            return []
        assert sql == (
            "SELECT metadata->>'name' AS name FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'name' LIKE $2"
        )
        assert args == (_USER, "unnamed-canvas#%")
        return [{"name": "unnamed-canvas#1"}]

    engine.conn.fetch.side_effect = names
    state = _open(engine, default=default)

    assert state.id == _CANVAS
    assert [sql for sql, _ in asked] == [
        _LATEST,
        _LATEST,
        "INSERT INTO visual_canvases (id, user_id, metadata, state) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING id, revision, state, metadata",
        _LATEST,
    ]
    assert asked[2][1][2] == {
        "name": "unnamed-canvas#2",
        "hidden": False,
        "pinned": False,
        "workspace_id": str(_DEFAULT_WORKSPACE),
    }
    default.assert_awaited_once()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opening_a_chat_unhides_its_canvas(
    seeded_canvas: PGliteEngine,
) -> None:
    """A chat lookup makes its matching canvas visible again."""
    conversation_id = uuid.uuid4()
    async with seeded_canvas.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text, 'hidden', TRUE) "
            "WHERE id = $1",
            _CANVAS,
            str(conversation_id),
        )

    opened = await canvas_for_chat(
        seeded_canvas,
        store=Store(seeded_canvas, embed=StubEmbedder()),
        user_id=_USER,
        email=_EMAIL,
        conversation_id=conversation_id,
        catalog=default_catalog(),
        inbound=InboundQueue(),
        assistant=None,
    )

    assert opened.id == _CANVAS
    assert opened.hidden is False
    async with seeded_canvas.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM inquiries") == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_copies_source_state_and_canvas_fields(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clear copies the canvas and canvas fields while resetting its identity flags."""
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    visual_id, focused_id = uuid.uuid4(), uuid.uuid4()
    workspace_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    layout = CanvasLayout.model_validate(
        {
            "tabs": [
                {"id": "graph", "name": "Graph", "visuals": [{"type": "trax.browse"}]},
                {
                    "id": "issue",
                    "name": "Issue #7",
                    "page": "#/issue/7",
                    "visuals": [{"type": "trax.search", "placement": "side"}],
                },
            ],
            "tab": "issue",
            "chat_ch": 96,
            "columns": {"left": 0.3},
        },
    ).model_dump(mode="json", exclude_none=True)
    source_state = CanvasData.model_validate(
        {
            "visuals": [
                {"id": str(visual_id), "type": "trax.browse", "version": 1},
                {"id": str(focused_id), "type": "trax.search", "version": 2},
            ],
            "focused_instance": str(focused_id),
        },
    ).model_dump(mode="json")
    source_canvas = {
        "name": "Source canvas",
        "conversation_id": str(conversation_id),
        "page": "#/source",
        "layout": layout,
        "hidden": True,
        "pinned": True,
        "workspace_id": str(workspace_id),
    }
    async with empty_canvas_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            canvas_id=_CANVAS,
            canvas=source_canvas,
            state=source_state,
        )

    key = uuid.uuid4()
    copied = await clear_chat_canvas(
        empty_canvas_schema,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    assert copied is not None

    async with empty_canvas_schema.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT metadata, state FROM visual_canvases WHERE id = $1",
            copied.id,
        )
    assert row is not None
    canvas = cast("Mapping[str, object]", row["metadata"])
    state = cast("Mapping[str, object]", row["state"])
    assert state == source_state
    assert canvas["layout"] == layout
    assert canvas["page"] == "#/source"
    assert canvas["workspace_id"] == str(workspace_id)
    assert "conversation_id" not in canvas
    assert canvas["hidden"] is False
    assert canvas["pinned"] is False
    assert canvas["name"] == "unnamed-canvas#1"
    assert canvas["clear_key"] == str(key)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_same_key_reuses_canvas_without_another_row(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated clear key returns the persisted canvas without inserting again."""
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    async with empty_canvas_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            canvas_id=_CANVAS,
            canvas={"name": "Source"},
            state={"visuals": []},
        )
    key = uuid.uuid4()
    first = await clear_chat_canvas(
        empty_canvas_schema,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    second = await clear_chat_canvas(
        empty_canvas_schema,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    assert first is not None
    assert second is not None
    assert second.id == first.id
    async with empty_canvas_schema.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_canvases WHERE user_id = $1",
            _USER,
        )
        canvas = await conn.fetchrow(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            first.id,
        )
    assert count == 2
    assert canvas is not None
    stored_canvas = cast("Mapping[str, object]", canvas["metadata"])
    assert stored_canvas["clear_key"] == str(key)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_different_keys_make_distinct_canvases(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new clear key creates a distinct canvas even for the same source."""
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    async with empty_canvas_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            canvas_id=_CANVAS,
            canvas={"name": "Source"},
            state={"visuals": []},
        )
    keys = (uuid.uuid4(), uuid.uuid4())
    copies = [
        await clear_chat_canvas(
            empty_canvas_schema,
            user_id=_USER,
            canvas_id=_CANVAS,
            key=key,
            inbound=InboundQueue(),
            assistant=None,
        )
        for key in keys
    ]
    assert copies[0] is not None
    assert copies[1] is not None
    assert copies[0].id != copies[1].id
    copy_ids = [copies[0].id, copies[1].id]
    async with empty_canvas_schema.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_canvases WHERE user_id = $1",
            _USER,
        )
        saved_keys = await conn.fetch(
            "SELECT metadata->>'clear_key' AS key FROM visual_canvases "
            "WHERE id = ANY($1::uuid[])",
            copy_ids,
        )
    assert count == 3
    assert {row["key"] for row in saved_keys} == {str(key) for key in keys}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_returns_none_for_another_users_source(
    empty_canvas_schema: PGliteEngine,
) -> None:
    """A user cannot clear another user's canvas by guessing its id."""
    async with empty_canvas_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'T', 'writer', 'active')",
            _USER,
            f"{_USER}@example.com",
        )
        await _insert_clear_source(
            conn,
            user_id=_OTHER_USER,
            canvas_id=_CANVAS,
            canvas={"name": "Foreign source"},
            state={"visuals": []},
        )
    copied = await clear_chat_canvas(
        empty_canvas_schema,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=uuid.uuid4(),
        inbound=InboundQueue(),
        assistant=None,
    )
    assert copied is None
    async with empty_canvas_schema.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM visual_canvases")
    assert count == 1


def test_a_clear_copies_the_canvas_without_its_marks_into_a_new_canvas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy keeps page, layout and workspace, takes the key, and drops marks."""
    workspace_id, key = uuid.uuid4(), uuid.uuid4()
    mark = {
        "points": [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}],
        "id": str(uuid.uuid4()),
        "seq": 3,
        "author": "user@example.com",
        "at": "2026-10-20T00:00:00Z",
    }
    source = _ROW | {
        "id": _CANVAS,
        "state": {"visuals": [], "marks": [mark], "marks_seq": 3},
        "metadata": {
            "name": "source",
            "conversation_id": str(uuid.uuid4()),
            "page": "#/source",
            "layout": {"tab": "graph", "chat_ch": 96},
            "hidden": True,
            "pinned": True,
            "workspace_id": str(workspace_id),
        },
    }
    engine, partner = _clear_store(monkeypatch, source, None, _ROW)
    inbound = InboundQueue()
    assistant = Assistant(actor="scout", email="kb@example.com")

    copied = _clear_scripted(engine, key=key, inbound=inbound, assistant=assistant)

    assert copied is not None
    assert copied.id == _CANVAS
    fetched = engine.conn.fetchrow.await_args_list
    assert fetched == [
        call(_SOURCE_SQL, _CANVAS, _USER),
        call(_CLEARED_SQL, _USER, str(key)),
        call(
            _INSERT_SQL,
            ANY,
            _USER,
            {
                "name": "unnamed-canvas#1",
                "page": "#/source",
                "layout": {"tab": "graph", "chat_ch": 96},
                "hidden": False,
                "pinned": False,
                "workspace_id": str(workspace_id),
                "clear_key": str(key),
            },
            ANY,
        ),
    ]
    copied_data = CanvasData.model_validate(fetched[2].args[4])
    assert (copied_data.marks, copied_data.marks_seq) == ([], 3)
    assert partner.await_args_list == [
        call(
            engine.conn,
            row=_ROW,
            user_id=_USER,
            inbound=inbound,
            assistant=assistant,
        ),
    ]


def _clear_store(
    monkeypatch: pytest.MonkeyPatch,
    *rows: object,
) -> tuple[FakeEngine, AsyncMock]:
    """Answer each fetchrow with the next of ``rows``; record every partner lookup."""
    conn: AsyncMock = make_conn()
    conn.fetchrow = AsyncMock(side_effect=list(rows))
    partner = AsyncMock(side_effect=_canvas_state_without_partner)
    monkeypatch.setattr(canvas_store, "_with_partner", partner)
    return FakeEngine(conn), partner


def _clear_scripted(
    engine: FakeEngine,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None = None,
) -> CanvasState | None:
    """Clear ``_CANVAS`` for ``_USER`` through the fast store."""
    return asyncio.run(
        clear_chat_canvas(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            canvas_id=_CANVAS,
            key=key,
            inbound=inbound,
            assistant=assistant,
        ),
    )


def test_a_retried_clear_returns_the_canvas_its_key_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key already used answers with its canvas, and inserts nothing."""
    key = uuid.uuid4()
    made = _ROW | {"metadata": {"name": "made", "clear_key": str(key)}}
    engine, partner = _clear_store(monkeypatch, _ROW | {"id": _CANVAS}, made)
    inbound = InboundQueue()
    assistant = Assistant(actor="scout", email="kb@example.com")

    copied = _clear_scripted(engine, key=key, inbound=inbound, assistant=assistant)

    assert copied is not None
    assert copied.name == "made"
    assert engine.conn.fetchrow.await_args_list == [
        call(_SOURCE_SQL, _CANVAS, _USER),
        call(_CLEARED_SQL, _USER, str(key)),
    ]
    assert partner.await_args_list == [
        call(
            engine.conn,
            row=made,
            user_id=_USER,
            inbound=inbound,
            assistant=assistant,
        ),
    ]


def test_a_clear_of_a_canvas_the_user_does_not_own_reads_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source the user does not own ends the clear after its one owner-scoped read."""
    engine, partner = _clear_store(monkeypatch, None)

    assert _clear_scripted(engine, key=uuid.uuid4(), inbound=InboundQueue()) is None
    assert engine.conn.fetchrow.await_args_list == [
        call(_SOURCE_SQL, _CANVAS, _USER),
    ]
    assert partner.await_args_list == []


def test_a_clear_whose_copy_never_lands_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An insert that no read ever sees raises instead of returning a missing canvas."""
    engine, _ = _clear_store(monkeypatch, _ROW | {"id": _CANVAS}, None, *[None] * 8)

    with pytest.raises(RuntimeError, match=r"^clear canvas insert was not visible$"):
        _ = _clear_scripted(engine, key=uuid.uuid4(), inbound=InboundQueue())


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_session_campaign_keeps_other_app_settings(
    empty_canvas_schema: PGliteEngine,
) -> None:
    """Campaign writes preserve notes and other keys in the session app."""
    session_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    async with empty_canvas_schema.acquire() as conn:
        await insert_inquiry(
            conn,
            session_id,
            "AgentSession",
            values={
                "title": "Chat",
                "owner": "user@example.com",
                "account": "user@example.com",
                "labels": ["science-chat", f"chat:{conversation_id}"],
                "subscribers": [],
                "app": {"settings": {"notes": "keep these"}},
                "agentsession_audience": "open",
                "agentsession_cli": "codex",
                "agentsession_cli_session_id": f"chat:{conversation_id}",
                "agentsession_rooms": [],
                "agentsession_opened_by_api_key_id": None,
            },
        )
    store = Store(empty_canvas_schema, embed=StubEmbedder())

    written = await set_session_campaign(
        store,
        session_id=session_id,
        email="user@example.com",
        campaign={"hours": 2.0, "scientists": 3},
    )

    async with empty_canvas_schema.acquire() as conn:
        app = await conn.fetchval("SELECT app FROM inquiries WHERE id = $1", session_id)
    assert written
    assert app == {
        "settings": {
            "notes": "keep these",
            "campaign": {"hours": 2.0, "scientists": 3},
        },
    }


def test_settings_refs_carry_the_stored_workspace_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suggestion the assistant stored reads back with the canvas's settings."""
    conversation_id, workspace_id, personal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    suggested = uuid.UUID("eca9d638-6a14-4f36-9037-4975511afc2f")
    conn: AsyncMock = make_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": _CANVAS,
            "metadata": {
                "name": "Charm",
                "conversation_id": str(conversation_id),
                "workspace_id": str(workspace_id),
                "suggestion": {
                    "title": "Charm ARC1/2",
                    "workspace_id": str(suggested),
                },
            },
        },
    )
    personal_settings = AsyncMock(return_value=personal)
    monkeypatch.setattr(canvas_store, "open_personal_settings", personal_settings)
    store = Store(cast("DatabaseEngine", FakeEngine(conn)), embed=StubEmbedder())

    refs = asyncio.run(
        settings_refs_for_chat(
            store,
            user_id=_USER,
            email="user@example.com",
            conversation_id=conversation_id,
            session_id=None,
        ),
    )

    assert refs == CanvasSettingsRefs(
        canvas_id=_CANVAS,
        workspace_issue_id=workspace_id,
        personal_artifact_id=personal,
        name="Charm",
        suggestion=WorkspaceSuggestion(title="Charm ARC1/2", workspace_id=suggested),
    )
    assert conn.fetchrow.await_args_list == [
        call(
            (
                "SELECT id, metadata FROM visual_canvases "
                "WHERE user_id = $1 AND metadata->>'conversation_id' = $2"
            ),
            _USER,
            str(conversation_id),
        ),
    ]
    assert personal_settings.await_args_list == [
        call(store, email="user@example.com"),
    ]


def test_settings_refs_drop_a_suggestion_of_the_workspace_the_chat_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat already in the suggested workspace reads back with no suggestion."""
    conversation_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    conn: AsyncMock = make_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": _CANVAS,
            "metadata": {
                "name": "Charm",
                "conversation_id": str(conversation_id),
                "workspace_id": str(workspace_id),
                "suggestion": {
                    "title": "Charm ARC1/2",
                    "workspace_id": str(workspace_id),
                },
            },
        },
    )
    monkeypatch.setattr(
        canvas_store,
        "open_personal_settings",
        AsyncMock(return_value=uuid.uuid4()),
    )
    store = Store(cast("DatabaseEngine", FakeEngine(conn)), embed=StubEmbedder())

    refs = asyncio.run(
        settings_refs_for_chat(
            store,
            user_id=_USER,
            email="user@example.com",
            conversation_id=conversation_id,
            session_id=None,
        ),
    )

    assert refs.workspace_issue_id == workspace_id
    assert refs.suggestion is None


def test_a_settings_artifact_is_submitted_as_its_owners_pinned_row() -> None:
    """The settings row is submitted owned by its user and labelled as settings."""
    conn = cast("Conn", object())
    store = MagicMock()
    store.submit_artifact = AsyncMock(return_value=_CANVAS)

    made = asyncio.run(
        canvas_store._make_settings_artifact(
            cast("Store", store),
            conn=conn,
            owner="user@example.com",
            title="Settings: user@example.com",
            description="Defaults.",
            app={"settings": {"notes": "kept"}},
        ),
    )

    assert made == _CANVAS
    assert store.submit_artifact.await_args == call(
        SubmitArtifact(
            title="Settings: user@example.com",
            description="Defaults.",
            owner="user@example.com",
            account="user@example.com",
            labels=["personal-settings"],
            app={"settings": {"notes": "kept"}},
        ),
        actor="user@example.com",
        conn=conn,
    )


def test_a_fork_copies_every_layout_tab_page_and_the_provider() -> None:
    """A fork writes the complete canvas layout, page and provider into its new row."""
    conversation_id = uuid.uuid4()
    layout = {
        "tabs": [
            {"id": "graph", "name": "Graph"},
            {"id": "issue", "name": "Issue#7", "page": "#/issue/7"},
        ],
        "tab": "issue",
        "chat_ch": 96,
    }
    source = _ROW | {
        "state": {"visuals": [], "provider": "claude"},
        "metadata": {
            "name": "source",
            "page": "#/source",
            "layout": layout,
            "workspace_id": str(uuid.uuid4()),
        },
    }
    engine = FakeEngine()
    inserted: list[tuple[object, ...]] = []

    async def insert(sql: str, *args: object) -> object:
        assert sql.startswith("INSERT INTO visual_canvases")
        inserted.append(args)
        return {"id": _CANVAS}

    engine.conn.fetchval.return_value = None
    engine.conn.fetch.return_value = list[dict[str, object]]()
    engine.conn.fetchrow.side_effect = insert

    made = asyncio.run(
        canvas_store._fork_canvas(
            cast("Conn", engine.conn),
            user_id=_USER,
            source=cast("Record", source),
            conversation_id=conversation_id,
        ),
    )

    assert made == _CANVAS
    copied = CanvasMetadata.model_validate(inserted[0][2])
    assert copied.conversation_id == conversation_id
    assert copied.page == "#/source"
    assert copied.layout is not None
    assert [tab.id for tab in copied.layout.tabs or []] == ["graph", "issue"]
    assert copied.layout.tab == "issue"
    assert copied.layout.chat_ch == 96
    source_canvas = from_plain(source["metadata"], dict[str, object])
    source_workspace = from_plain(source_canvas["workspace_id"], str)
    assert copied.workspace_id == uuid.UUID(source_workspace)
    # A fork of a claude canvas is a claude canvas.
    assert CanvasData.model_validate(inserted[0][3]).provider == "claude"


_KB: Final = Assistant(actor="scout", email="kb@example.com")
_ME: Final = "user@example.com"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Original:
    """A chat the assistant began for the user, its key, and the user's own key."""

    session: uuid.UUID
    assistant_key: uuid.UUID
    own_key: uuid.UUID


# The claude part was attached while the bot's answer to q1 was still being written, so
# in time order the lines run q1, q2, a1, a2, q3, a3; a fork at a2 holds the first four
# of them and neither q3 nor a3.
async def _original(engine: PGliteEngine, *, audience: str = OPEN) -> _Original:
    """Open a chat whose bot part and later claude part overlap in time."""
    await _seed(engine)
    own_key = await _owner_key(engine)
    assistant_key = uuid.uuid4()
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'KB', 'writer', 'active')",
            _OTHER_USER,
            _KB.email,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'kb', 'h', 'trax_kb', 'writer')",
            assistant_key,
            _OTHER_USER,
        )
    store = Store(engine, embed=StubEmbedder())
    session = await store.submit_agentsession(
        SubmitAgentSession(
            title="Science chat",
            owner="chat-original",
            account=_ME,
            cli="sagent",
            cli_session_id=chat_session_id(uuid.uuid4()),
            labels=[SCIENCE_CHAT_LABEL],
        ),
        audience=audience,
        api_key_id=assistant_key,
        actor=_KB.actor,
    )
    delivered = immutable({"$inbound": {"source": _ME, "room": ""}})
    parts: list[tuple[str, list[SessionRecord]]] = [
        (
            "sagent",
            [
                AgentToAgentMessage(sender=_ME, content="q1", timestamp=_minute(1)),
                AssistantMessage(content="a1", timestamp=_minute(3)),
            ],
        ),
        (
            "claude",
            [
                UserMessage(
                    content=f"{_ME}: q2",
                    timestamp=_minute(2),
                    extra=delivered,
                ),
                AssistantMessage(content="a2", timestamp=_minute(4)),
                UserMessage(
                    content=f"{_ME}: q3",
                    timestamp=_minute(5),
                    extra=delivered,
                ),
                AssistantMessage(content="a3", timestamp=_minute(6)),
            ],
        ),
    ]
    for part, (format_, records) in enumerate(parts):
        _ = await store.upsert_session_manifest(
            session,
            name=f"{format_}.jsonl",
            metadata=immutable({}),
            ir_id=uuid.uuid4(),
            format=format_,
            records=len(records),
            api_key_id=assistant_key,
        )
        _ = await store.append_session_records(
            session,
            [
                SessionRecordRow.of(session_id=session, part=part, idx=idx, record=each)
                for idx, each in enumerate(records)
            ],
        )
    return _Original(session=session, assistant_key=assistant_key, own_key=own_key)


def _minute(minute: int) -> str:
    return datetime(2026, 10, 20, 9, minute, tzinfo=UTC).isoformat()


async def _fork(
    engine: PGliteEngine,
    original: _Original,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
) -> ChatSent | None:
    """Fork the original at a2, its claude part's answer, from the user's canvas."""
    return await send_chat_line(
        Store(engine, embed=StubEmbedder()),
        user_id=_USER,
        body=ChatSend(
            canvas_id=_CANVAS,
            text="what if a2 were wrong?",
            fork=ChatForkAt(session_id=original.session, part=1, idx=1),
        ),
        key=key,
        source=_ME,
        source_role="writer",
        inbound=inbound,
        assistant=_KB,
        reader=reader_for(
            _ME,
            audiences=frozenset({OPEN, PRIVATE}),
            admin=False,
            orgs="individual",
            assistant=_KB.email,
            hosted_domain=None,
        ),
        members=None,
    )


async def _lines(engine: PGliteEngine, session: uuid.UUID) -> list[tuple[str, ...]]:
    """Return each part's format and what each of its records says, in part order."""
    store = Store(engine, embed=StubEmbedder())
    return [
        (
            manifest.format,
            *(
                from_plain(row.payload.get("content"), str, default="")
                for row in await store.read_session_records(
                    session,
                    part=manifest.part,
                    plaintext_only=True,
                )
            ),
        )
        for manifest in await store.read_session_manifests(session)
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_on_claude_opens_holding_the_lines_up_to_its_fork_point(
    pglite_engine: PGliteEngine,
) -> None:
    """Every part as far as the fork point, stamps kept, linked; a retry adds nothing."""
    original = await _original(pglite_engine)
    await _set_provider(pglite_engine, "claude")
    inbound, key = InboundQueue(), uuid.uuid4()

    sent = await _fork(pglite_engine, original, key=key, inbound=inbound)
    again = await _fork(pglite_engine, original, key=key, inbound=inbound)

    assert sent is not None
    assert again == sent
    fork = sent.session_id
    assert fork is not None
    assert await _lines(pglite_engine, fork) == [
        ("sagent", "q1", "a1"),
        ("claude", f"{_ME}: q2", "a2"),
    ]
    async with pglite_engine.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT account, agentsession_cli AS cli, agentsession_cli_session_id "
            "AS chat, agentsession_opened_by_api_key_id AS key FROM inquiries "
            "WHERE id = $1",
            fork,
        )
        copied = await conn.fetch(
            "SELECT part, idx, timestamp, payload::jsonb -> 'extra' AS extra "
            "FROM session_records WHERE session_id = $1 ORDER BY part, idx",
            fork,
        )
        openers = await conn.fetch(
            "SELECT opened_by_api_key_id AS key FROM session_manifests "
            "WHERE session_id = $1",
            fork,
        )
        edge = await conn.fetchrow(
            "SELECT link.edge_kind, link.labels, link.note, audit.api_key_id "
            "FROM edges AS link JOIN change_log AS audit ON audit.kind = 'edge_added' "
            "AND audit.subject_id = link.from_id AND audit.new_peer_id = link.to_id "
            "WHERE link.from_id = $1 AND link.to_id = $2",
            fork,
            original.session,
        )
        canvas = await conn.fetchrow(
            "SELECT state FROM visual_canvases WHERE metadata->>'conversation_id' = $1",
            str(sent.conversation_id),
        )
    # The forker's own run attaches to it, so it is theirs, opened for a claude run.
    assert session is not None
    assert dict(session) == {
        "account": _ME,
        "cli": "claude",
        "chat": chat_session_id(sent.conversation_id),
        "key": original.own_key,
    }
    assert [
        (row["part"], row["idx"], from_plain(row["timestamp"], datetime).minute)
        for row in copied
    ] == [(0, 0, 1), (0, 1, 3), (1, 0, 2), (1, 1, 4)]
    assert from_plain(copied[2]["extra"], dict[str, object]) == {
        "$inbound": {"source": _ME, "room": ""},
    }
    assert {row["key"] for row in openers} == {original.own_key}
    assert edge is not None
    assert (edge["edge_kind"], edge["labels"], edge["api_key_id"]) == (
        "produced_by",
        [FORK_EDGE_LABEL, fork_point_label(part=1, idx=1)],
        original.own_key,
    )
    assert canvas is not None
    assert CanvasData.model_validate(canvas["state"]).provider == "claude"
    # The line waits on the fork's own session for the run the forker starts.
    assert [each.text for each in inbound.drain(fork)] == ["what if a2 were wrong?"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_on_the_assistant_is_opened_under_its_key_for_it_to_resume(
    pglite_engine: PGliteEngine,
) -> None:
    """The line goes to its service session; the copies stay the forker's."""
    original = await _original(pglite_engine)
    service = await _service(pglite_engine, original)
    inbound = InboundQueue()
    inbound.mark_poller(service)

    sent = await _fork(pglite_engine, original, key=uuid.uuid4(), inbound=inbound)

    assert sent is not None
    assert sent.session_id is None
    [heard] = inbound.drain(service)
    assert heard.context is not None
    assert heard.context.conversation_id == sent.conversation_id
    async with pglite_engine.acquire() as conn:
        fork = await conn.fetchrow(
            "SELECT id, account, agentsession_cli AS cli, "
            "agentsession_opened_by_api_key_id AS key FROM inquiries "
            "WHERE agentsession_cli_session_id = $1",
            chat_session_id(sent.conversation_id),
        )
        assert fork is not None
        openers = await conn.fetch(
            "SELECT opened_by_api_key_id AS key FROM session_manifests "
            "WHERE session_id = $1",
            fork["id"],
        )
        # Only an edge its session's opener added counts as a fork.
        linked_by = await conn.fetchval(
            "SELECT audit.api_key_id FROM change_log AS audit "
            "WHERE audit.kind = 'edge_added' AND audit.subject_id = $1 "
            "AND audit.new_peer_id = $2",
            fork["id"],
            original.session,
        )
    assert linked_by == original.assistant_key
    assert (fork["account"], fork["cli"], fork["key"]) == (
        _ME,
        "sagent",
        original.assistant_key,
    )
    assert await _lines(pglite_engine, from_plain(fork["id"], uuid.UUID)) == [
        ("sagent", "q1", "a1"),
        ("claude", f"{_ME}: q2", "a2"),
    ]
    assert {row["key"] for row in openers} == {original.own_key}


# The assistant resumes the fork's session as soon as the line wakes it; were the fork
# still uncommitted, it would find no session and open a second, empty one.
@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistant_hears_a_fork_only_once_its_session_is_committed(
    pglite_engine: PGliteEngine,
) -> None:
    original = await _original(pglite_engine)
    service = await _service(pglite_engine, original)
    inbound = InboundQueue()
    inbound.mark_poller(service)
    # PGlite has one connection, so this is the one the post writes the fork on.
    async with pglite_engine.acquire() as conn:
        pass

    async def hear() -> bool:
        heard = await inbound.await_messages(service, timeout_sec=5)
        assert [each.text for each in heard] == ["what if a2 were wrong?"]
        return conn.is_in_transaction()

    hearing = asyncio.create_task(hear())
    sent = await _fork(pglite_engine, original, key=uuid.uuid4(), inbound=inbound)

    assert sent is not None
    assert not await hearing


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_under_a_used_key_is_refused_and_opens_nothing(
    pglite_engine: PGliteEngine,
) -> None:
    original = await _original(pglite_engine)
    service = await _service(pglite_engine, original)
    inbound, key = InboundQueue(), uuid.uuid4()
    inbound.mark_poller(service)
    _ = inbound.send_once(key, [(service, Inbound(text="hi"))], fingerprint="other")

    with pytest.raises(IdempotencyReuseError):
        _ = await _fork(pglite_engine, original, key=key, inbound=inbound)

    async with pglite_engine.acquire() as conn:
        opened = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE agentsession_cli_session_id = $1",
            chat_session_id(key),
        )
        canvases = await conn.fetchval("SELECT count(*) FROM visual_canvases")
    assert (opened, canvases) == (0, 1)
    assert [each.text for each in inbound.drain(service)] == ["hi"]


async def _service(engine: PGliteEngine, original: _Original) -> uuid.UUID:
    """Open the assistant's service session, under its own key."""
    return await Store(engine, embed=StubEmbedder()).submit_agentsession(
        SubmitAgentSession(title="scout", owner=_KB.actor, account=_KB.email),
        audience=OPEN,
        api_key_id=original.assistant_key,
        actor=_KB.actor,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_of_a_private_chat_is_refused_and_opens_nothing(
    pglite_engine: PGliteEngine,
) -> None:
    original = await _original(pglite_engine, audience=PRIVATE)
    await _set_provider(pglite_engine, "claude")
    key = uuid.uuid4()

    with pytest.raises(ChatClosedError, match="chat_fork_closed"):
        _ = await _fork(pglite_engine, original, key=key, inbound=InboundQueue())

    async with pglite_engine.acquire() as conn:
        opened = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE agentsession_cli_session_id = $1",
            chat_session_id(key),
        )
        canvases = await conn.fetchval("SELECT count(*) FROM visual_canvases")
    assert (opened, canvases) == (0, 1)


def test_moving_a_canvas_sets_workspace_and_clears_its_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid.uuid4()
    row = _ROW | {
        "metadata": {
            "name": "source",
            "suggestion": {"title": "Possible"},
        },
    }
    conn: AsyncMock = make_conn()
    updated = row | {
        "metadata": {"name": "source", "workspace_id": str(workspace_id)},
    }
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(sql: str, *args: object) -> object:
        calls.append((sql, args))
        return updated

    async def allowed(*_args: object, **_kwargs: object) -> bool:
        return True

    async def with_partner(
        conn: object,
        *,
        row: object,
        **_kwargs: object,
    ) -> CanvasState:
        del conn
        return canvas_store.state_from_row(cast("Mapping[str, object]", row))

    conn.fetchrow.side_effect = fetchrow
    monkeypatch.setattr(canvas_store, "check_workspace", allowed)
    monkeypatch.setattr(canvas_store, "_with_partner", with_partner)
    engine = FakeEngine(conn)
    state = asyncio.run(
        set_canvas_workspace(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            email="user@example.com",
            canvas_id=_CANVAS,
            workspace_id=workspace_id,
            inbound=InboundQueue(),
            assistant=None,
        ),
    )

    assert state is not None
    assert state.workspace_id == workspace_id
    assert state.suggestion is None
    assert len(calls) == 1
    assert "- 'suggestion'" in calls[0][0]
    assert calls[0][1] == (_CANVAS, _USER, str(workspace_id))


_CANVAS: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DEFAULT_WORKSPACE: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_LATEST: Final = (
    "SELECT id, revision, state, metadata FROM visual_canvases "
    "WHERE user_id = $1 AND coalesce((metadata->>'hidden')::boolean, FALSE) "
    "= FALSE AND NOT metadata ? 'session_id' "
    "ORDER BY modified_at DESC, id DESC LIMIT 1"
)
_ROW: Final[dict[str, object]] = {
    "id": _CANVAS,
    "revision": 4,
    "state": {"visuals": []},
    "metadata": {"name": "tails", "page": "#/issue/7"},
}


def _open(
    engine: FakeEngine,
    *,
    assistant: Assistant | None = None,
    default: AsyncMock | None = None,
) -> CanvasState:
    """Open the latest canvas through ``engine``; ``default`` opens the default."""
    opener = default or AsyncMock(return_value=_DEFAULT_WORKSPACE)
    database = cast("DatabaseEngine", engine)
    with patch.object(canvas_store, "open_default_workspace", opener):
        return asyncio.run(
            open_latest_canvas(
                database,
                store=Store(database, embed=StubEmbedder()),
                user_id=_USER,
                email=_EMAIL,
                catalog=default_catalog(),
                inbound=InboundQueue(),
                assistant=assistant,
            ),
        )


def _scripted(
    *latest: object,
) -> tuple[FakeEngine, list[tuple[str, tuple[object, ...]]]]:
    """Answer each read of the latest canvas in turn, and record every fetchrow."""
    conn: AsyncMock = make_conn()
    reads = list(latest)
    asked: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(sql: str, *args: object) -> object:
        asked.append((sql, args))
        if sql.startswith("INSERT"):
            return _ROW
        return reads.pop(0)

    async def fetch(*_args: object, **_kwargs: object) -> list[object]:
        return []

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = fetch
    return FakeEngine(conn), asked


def test_the_canvas_changed_last_is_read_and_nothing_is_written() -> None:
    engine, asked = _scripted(_ROW)

    state = _open(engine, assistant=Assistant(actor="scout", email="kb@example.com"))

    assert asked == [(_LATEST, (_USER,))]
    assert (state.id, state.revision, state.name, state.page) == (
        _CANVAS,
        4,
        "tails",
        "#/issue/7",
    )
    assert state.conversation_id is None
    assert state.assistant == "scout"
    assert state.partner is not None
    assert state.partner.status == "unavailable"
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


def test_a_user_with_no_canvas_gets_the_first_one_named_and_read_back() -> None:
    engine, asked = _scripted(None, None, _ROW)

    state = _open(engine)

    assert [sql for sql, _ in asked] == [
        _LATEST,
        _LATEST,
        (
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
            "RETURNING id, revision, state, metadata"
        ),
        _LATEST,
    ]
    _, (new_id, owner, canvas, data) = asked[2]
    assert isinstance(new_id, uuid.UUID)
    # A new canvas persists both flags, but has no chat or page yet.
    assert owner == _USER
    assert canvas == {
        "name": "unnamed-canvas#1",
        "hidden": False,
        "pinned": False,
        "workspace_id": str(_DEFAULT_WORKSPACE),
    }
    assert CanvasData.model_validate(data).visuals[0].type == "trax.browse"
    assert asked[3] == (_LATEST, (_USER,))
    assert state.id == _CANVAS
    assert state.assistant is None
    assert state.partner is None


def test_a_first_canvas_starts_on_its_owners_provider_and_reads_their_machines() -> (
    None
):
    """The preference read and the machines asked about are the owner's own."""
    engine, asked = _scripted(None, None, _ROW)

    async def fetchval(sql: str, *args: object) -> object:
        mine = "SELECT preferences FROM users" in sql and args == (_USER,)
        return {"chat_provider": "codex"} if mine else None

    async def fetch(sql: str, *args: object) -> list[dict[str, object]]:
        mine = "FROM machines" in sql and args[0] == _USER
        return [{"cli": "claude"}] if mine else []

    engine.conn.fetchval.side_effect = fetchval
    engine.conn.fetch.side_effect = fetch

    state = _open(engine)

    _, (_, _, _, data) = asked[2]
    assert CanvasData.model_validate(data).provider == "codex"
    assert (state.available.claude, state.available.codex) == (True, False)


def test_open_latest_reuses_a_canvas_that_appears_during_the_names_query() -> None:
    engine, asked = _scripted(None, _ROW)

    state = _open(engine)

    assert state.id == _CANVAS
    assert asked == [(_LATEST, (_USER,)), (_LATEST, (_USER,))]
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


@pytest.mark.parametrize("choice", ["shared", "local"])
def test_a_stored_partner_choice_reads_as_the_assistant_provider(choice: str) -> None:
    """A canvas stored before providers says ``partner_choice``, and no provider."""
    stored: dict[str, object] = {"visuals": [], "partner_choice": choice}
    engine, _ = _scripted(_ROW | {"state": stored})

    state = _open(engine)

    assert state.provider == "assistant"
    assert "partner_choice" not in canvas_store._data_of(state).model_dump()


def test_a_canvas_that_stays_invisible_after_its_insert_is_an_error() -> None:
    engine, _ = _scripted(None, None, None)

    with pytest.raises(RuntimeError, match=r"^first canvas insert was not visible$"):
        _ = _open(engine)


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_name_uses_the_next_free_suffix(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_canvas_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) VALUES "
            "($1, $2, $3, $4), ($5, $2, $3, $6), ($7, $2, $3, $8)",
            _CANVAS,
            _USER,
            {"visuals": []},
            {"name": "unnamed-canvas#1", "conversation_id": str(conversation_id)},
            uuid.uuid4(),
            {"name": "Research"},
            uuid.uuid4(),
            {"name": "Research (2)"},
        )

    _ = await name_unnamed_canvas(
        empty_canvas_schema,
        email="user@example.com",
        conversation_id=conversation_id,
        name="Research",
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_canvas_schema.acquire() as conn:
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
    assert CanvasMetadata.model_validate(canvas).name == "Research (3)"


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_name_leaves_an_already_named_canvas_unchanged(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_canvas_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) "
            "VALUES ($1, $2, $3, $4)",
            _CANVAS,
            _USER,
            {"visuals": []},
            {"name": "Research", "conversation_id": str(conversation_id)},
        )
        version_before = await conn.fetchval(
            "SELECT xmin::text FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )

    _ = await name_unnamed_canvas(
        empty_canvas_schema,
        email="user@example.com",
        conversation_id=conversation_id,
        name="Ignored",
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_canvas_schema.acquire() as conn:
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
        version_after = await conn.fetchval(
            "SELECT xmin::text FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
    assert CanvasMetadata.model_validate(canvas).name == "Research"
    assert version_after == version_before


@pytest.mark.asyncio(loop_scope="session")
async def test_suggestion_updates_chat_canvas_without_creating_an_issue(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_canvas_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) "
            "VALUES ($1, $2, $3, $4)",
            _CANVAS,
            _USER,
            {"visuals": []},
            {"name": "Research", "conversation_id": str(conversation_id)},
        )
        issues_before = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE kind = 'Issue'",
        )

    _ = await suggest_workspace_for_chat(
        empty_canvas_schema,
        user_id=_USER,
        email="user@example.com",
        conversation_id=conversation_id,
        suggestion=WorkspaceSuggestion(title="A possible stream"),
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_canvas_schema.acquire() as conn:
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
        issues_after = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE kind = 'Issue'",
        )
    assert CanvasMetadata.model_validate(canvas).suggestion == WorkspaceSuggestion(
        title="A possible stream",
    )
    assert issues_after == issues_before


@pytest.mark.asyncio(loop_scope="session")
async def test_a_suggestion_of_the_chats_own_workspace_reads_back_as_none(
    empty_canvas_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat sits in W: W is no suggestion; another workspace still is."""
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    monkeypatch.setattr(
        canvas_store,
        "check_workspace",
        AsyncMock(return_value=True),
    )
    conversation_id, workspace_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with empty_canvas_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) "
            "VALUES ($1, $2, $3, $4)",
            _CANVAS,
            _USER,
            {"visuals": []},
            {
                "name": "Research",
                "conversation_id": str(conversation_id),
                "workspace_id": str(workspace_id),
            },
        )

    async def suggest(target: uuid.UUID) -> CanvasState | None:
        return await suggest_workspace_for_chat(
            empty_canvas_schema,
            user_id=_USER,
            email="user@example.com",
            conversation_id=conversation_id,
            suggestion=WorkspaceSuggestion(workspace_id=target, title="Charm"),
            inbound=InboundQueue(),
            assistant=None,
        )

    own = await suggest(workspace_id)
    assert own is not None
    assert own.suggestion is None
    elsewhere = await suggest(other)
    assert elsewhere is not None
    assert elsewhere.suggestion == WorkspaceSuggestion(
        workspace_id=other,
        title="Charm",
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_boxes_are_stored_read_back_and_dropped_by_a_cleared_canvas(
    seeded_canvas: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mark survives the write and the read; a clear-chat copy starts with none."""
    monkeypatch.setattr(
        canvas_store,
        "_with_partner",
        _canvas_state_without_partner,
    )
    inbound = InboundQueue()
    drawn = await _apply(
        seeded_canvas,
        ApplyCanvasOperation.model_validate(
            {
                "revision": 0,
                "operation": {
                    "kind": "mark",
                    "mark": {
                        "points": [{"x": 0.5, "y": 0.5}, {"x": 0.7, "y": 0.8}],
                        "page": "#/graph",
                    },
                },
            },
        ),
        key=uuid.uuid4(),
        inbound=inbound,
    )
    assert drawn is not None
    read = await read_canvas(
        seeded_canvas,
        user_id=_USER,
        canvas_id=_CANVAS,
        inbound=inbound,
        assistant=None,
    )
    assert read is not None
    assert [(m.page, m.seq) for m in read.marks] == [("#/graph", 1)]
    assert [m.id for m in read.marks] == [m.id for m in drawn.state.marks]
    assert read.marks_seq == 1

    copied = await clear_chat_canvas(
        seeded_canvas,
        user_id=_USER,
        canvas_id=_CANVAS,
        key=uuid.uuid4(),
        inbound=inbound,
        assistant=None,
    )
    assert copied is not None
    assert (copied.marks, copied.marks_seq) == ([], 1)


_CLEAR_KEY: Final = uuid.UUID("77777777-7777-7777-7777-777777777777")
_CLEAR_INBOUND: Final = InboundQueue()
_SOURCE_STREAM: Final = uuid.UUID("88888888-8888-8888-8888-888888888888")
_SELECT_SOURCE: Final = (
    "SELECT id, revision, state, metadata FROM visual_canvases "
    "WHERE id = $1 AND user_id = $2 FOR UPDATE"
)
_SELECT_CLEARED: Final = (
    "SELECT id, revision, state, metadata FROM visual_canvases "
    "WHERE user_id = $1 AND metadata->>'clear_key' = $2 LIMIT 1"
)
_INSERT_CANVAS: Final = (
    "INSERT INTO visual_canvases (id, user_id, metadata, state) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
    "RETURNING id, revision, state, metadata"
)


def _clear_source() -> dict[str, object]:
    """Return a claude canvas's row with a chat, page, layout, mark and flags."""
    data = CanvasData.model_validate(
        {
            "visuals": [],
            "provider": "claude",
            "agent_instructions": "be brief",
            "marks": [
                {
                    "id": str(uuid.UUID(int=1)),
                    "seq": 2,
                    "points": [{"x": 0.1, "y": 0.1}, {"x": 0.2, "y": 0.2}],
                    "page": "#/graph",
                    "author": _ME,
                    "at": "2026-10-20T09:00:00+00:00",
                },
            ],
            "marks_seq": 2,
        },
    )
    return {
        "id": _CANVAS,
        "revision": 9,
        "state": data.model_dump(mode="json"),
        "metadata": {
            "name": "source",
            "conversation_id": str(uuid.UUID(int=2)),
            "page": "#/issue/7",
            "layout": {"tab": "issue", "chat_ch": 96},
            "hidden": True,
            "pinned": True,
            "workspace_id": str(_SOURCE_STREAM),
        },
    }


@dataclass(slots=True, kw_only=True)
class _Clearing:
    """What a clear asked the database and handed to the partner lookup."""

    asked: list[tuple[str, tuple[object, ...]]]
    partnered: list[dict[str, object]]


def _clear(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: object,
    cleared: object = None,
    inserted: bool = True,
) -> tuple[CanvasState | None, _Clearing]:
    """Clear the user's canvas against a scripted connection; return what it did."""
    conn: AsyncMock = make_conn()
    clearing = _Clearing(asked=[], partnered=[])

    async def fetchrow(sql: str, *args: object) -> object:
        clearing.asked.append((sql, args))
        if sql == _SELECT_SOURCE:
            return source
        if sql == _SELECT_CLEARED:
            return cleared
        if not inserted:
            return None
        return {"id": args[0], "revision": 0, "state": args[3], "metadata": args[2]}

    async def names(sql: str, *args: object) -> list[dict[str, object]]:
        clearing.asked.append((sql, args))
        return [{"name": "unnamed-canvas#1"}]

    async def partner(given: object, **found: object) -> CanvasState | None:
        clearing.partnered.append({"on_the_clears_conn": given is conn, **found})
        row = found["row"]
        return (
            None
            if row is None
            else canvas_store.state_from_row(cast("Mapping[str, object]", row))
        )

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = names
    monkeypatch.setattr(canvas_store, "_with_partner", partner)
    state = asyncio.run(
        clear_chat_canvas(
            cast("DatabaseEngine", FakeEngine(conn)),
            user_id=_USER,
            canvas_id=_CANVAS,
            key=_CLEAR_KEY,
            inbound=_CLEAR_INBOUND,
            assistant=_KB,
        ),
    )
    return state, clearing


def test_a_clear_copies_the_canvas_without_marks_chat_or_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy keeps the page, layout, workspace and provider, under the next name."""
    state, clearing = _clear(monkeypatch, source=_clear_source())

    assert [sql for sql, _ in clearing.asked] == [
        _SELECT_SOURCE,
        _SELECT_CLEARED,
        (
            "SELECT metadata->>'name' AS name FROM visual_canvases "
            "WHERE user_id = $1 AND metadata->>'name' LIKE $2"
        ),
        _INSERT_CANVAS,
    ]
    assert clearing.asked[0][1] == (_CANVAS, _USER)
    assert clearing.asked[1][1] == (_USER, str(_CLEAR_KEY))
    assert clearing.asked[2][1] == (_USER, "unnamed-canvas#%")
    new_id, owner, canvas, data = clearing.asked[3][1]
    assert isinstance(new_id, uuid.UUID)
    assert new_id != _CANVAS
    assert owner == _USER
    assert canvas == {
        "name": "unnamed-canvas#2",
        "page": "#/issue/7",
        "layout": {"tab": "issue", "chat_ch": 96},
        "hidden": False,
        "pinned": False,
        "workspace_id": str(_SOURCE_STREAM),
        "clear_key": str(_CLEAR_KEY),
    }
    copied = CanvasData.model_validate(data)
    assert (copied.provider, copied.agent_instructions) == ("claude", "be brief")
    assert (copied.marks, copied.marks_seq) == ([], 2)
    assert clearing.partnered == [
        {
            "on_the_clears_conn": True,
            "row": {"id": new_id, "revision": 0, "state": data, "metadata": canvas},
            "user_id": _USER,
            "inbound": _CLEAR_INBOUND,
            "assistant": _KB,
        },
    ]
    assert state is not None
    assert (state.id, state.conversation_id) == (new_id, None)


def test_a_clear_retried_under_its_key_returns_the_first_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _clear_source() | {"id": _CANVAS}

    state, clearing = _clear(monkeypatch, source=_clear_source(), cleared=first)

    assert [sql for sql, _ in clearing.asked] == [_SELECT_SOURCE, _SELECT_CLEARED]
    assert clearing.partnered == [
        {
            "on_the_clears_conn": True,
            "row": first,
            "user_id": _USER,
            "inbound": _CLEAR_INBOUND,
            "assistant": _KB,
        },
    ]
    assert state is not None
    assert state.id == _CANVAS


def test_a_clear_of_a_canvas_the_user_does_not_own_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, clearing = _clear(monkeypatch, source=None)

    assert state is None
    assert [sql for sql, _ in clearing.asked] == [_SELECT_SOURCE]
    assert clearing.partnered == []


def test_a_clear_whose_copy_never_appears_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match=r"^clear canvas insert was not visible$"):
        _ = _clear(monkeypatch, source=_clear_source(), inserted=False)


@dataclass(slots=True, kw_only=True)
class _Submitted:
    """A store that records each Artifact it is asked to submit."""

    calls: list[tuple[SubmitArtifact, dict[str, object]]]

    async def submit_artifact(self, body: SubmitArtifact, **how: object) -> uuid.UUID:
        self.calls.append((body, how))
        return _CANVAS


def test_a_settings_artifact_is_its_owners_with_the_settings_label() -> None:
    store = _Submitted(calls=[])
    conn = make_conn()
    app: dict[str, object] = {"campaign": {"hours": 2.0}}

    made = asyncio.run(
        canvas_store._make_settings_artifact(
            cast("Store", store),
            conn=cast("Conn", conn),
            owner=_ME,
            title="Settings: user@example.com",
            description="Your defaults.",
            app=app,
        ),
    )

    assert made == _CANVAS
    assert store.calls == [
        (
            SubmitArtifact(
                title="Settings: user@example.com",
                description="Your defaults.",
                owner=_ME,
                account=_ME,
                labels=["personal-settings"],
                app=app,
            ),
            {"actor": _ME, "conn": conn},
        ),
    ]


_SHARER: Final = "grace@example.com"
_SHARER_ID: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_SHARER_CANVAS: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")


# The sharer has a canvas of their own on the assistant, holding no chat.
async def _claude_chat(engine: PGliteEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """Put the user's canvas on claude, holding a chat their run drains, and share it."""
    await _seed(engine)
    own_key = await _owner_key(engine)
    conversation = uuid.uuid4()
    session = await Store(engine, embed=StubEmbedder()).submit_agentsession(
        SubmitAgentSession(
            title="Science chat",
            owner=chat_actor(conversation),
            account=_ME,
            cli="claude",
            cli_session_id=chat_session_id(conversation),
            labels=[SCIENCE_CHAT_LABEL],
        ),
        audience=OPEN,
        api_key_id=own_key,
        actor=_ME,
    )
    async with engine.acquire() as conn:
        # Three marks drawn and none sent yet: a line carries them, a command does not.
        await conn.execute(
            "UPDATE visual_canvases SET state = state || jsonb_build_object("
            "'provider', 'claude', 'marks_seq', 3), metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            _CANVAS,
            str(conversation),
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'G', 'writer', 'active')",
            _SHARER_ID,
            _SHARER,
        )
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            _SHARER_CANVAS,
            _SHARER_ID,
            {"name": "unnamed-canvas#1"},
            {"visuals": []},
        )
        await conn.execute(
            "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
            "VALUES ($1, $2, $3)",
            conversation,
            _SHARER,
            _ME,
        )
    return conversation, session


async def _post(
    engine: PGliteEngine,
    *,
    inbound: InboundQueue,
    text: str,
    conversation_id: uuid.UUID | None = None,
    kind: ChatLineKind = "line",
    payload: dict[str, object] | None = None,
    sharer: bool = False,
    key: uuid.UUID | None = None,
) -> ChatSent | None:
    """Post from the user's canvas, or the sharer's, as a browser does."""
    poster = _SHARER if sharer else _ME
    return await send_chat_line(
        Store(engine, embed=StubEmbedder()),
        user_id=_SHARER_ID if sharer else _USER,
        body=ChatSend(
            canvas_id=_SHARER_CANVAS if sharer else _CANVAS,
            text=text,
            kind=kind,
            payload=payload,
            conversation_id=conversation_id,
        ),
        key=key or uuid.uuid4(),
        source=poster,
        source_role="writer",
        inbound=inbound,
        assistant=None,
        reader=reader_for(
            poster,
            audiences=frozenset({OPEN}),
            admin=False,
            orgs="individual",
            assistant="",
            hosted_domain=None,
        ),
        members=None,
    )


async def _marks_sent(engine: PGliteEngine) -> int:
    async with engine.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
    return CanvasData.model_validate(state).marks_sent


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_claude_chat_queues_a_known_command_bare_and_a_slash_line_as_a_line(
    pglite_engine: PGliteEngine,
) -> None:
    """The command is typed as the CLI's own; the path stays a person's line."""
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    commanded = await _post(pglite_engine, inbound=inbound, text="/Goal ship it")
    marks_after_command = await _marks_sent(pglite_engine)
    said = await _post(pglite_engine, inbound=inbound, text="/etc/hosts is broken")

    assert commanded is not None
    assert said is not None
    assert commanded.session_id == said.session_id == session
    heard = inbound.drain(session)
    assert [(each.text, each.kind, each.source) for each in heard] == [
        ("/goal ship it", "command", _ME),
        ("/etc/hosts is broken", "line", _ME),
    ]
    assert heard[0].context is None
    assert heard[1].context is not None
    assert heard[1].context.conversation_id == conversation
    # The command sent no marks; the line after it sent all three.
    assert (marks_after_command, await _marks_sent(pglite_engine)) == (0, 3)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_model_or_effort_line_queues_a_settings_post_with_no_page_or_marks(
    pglite_engine: PGliteEngine,
) -> None:
    """The run applies a settings post; it carries no page, trail or marks."""
    _, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    _ = await _post(pglite_engine, inbound=inbound, text="/Model opus")
    _ = await _post(pglite_engine, inbound=inbound, text="/effort High")

    heard = inbound.drain(session)
    assert [(each.text, each.kind, each.payload, each.context) for each in heard] == [
        ("/model opus", "settings", {"model": "opus", "effort": None}, None),
        ("/effort high", "settings", {"model": None, "effort": "high"}, None),
    ]
    assert await _marks_sent(pglite_engine) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_bare_model_line_is_refused_with_what_works(
    pglite_engine: PGliteEngine,
) -> None:
    _, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    with pytest.raises(
        CommandRefusedError,
        match=r"^Chat can't open the model menu\. Type /model <name> ",
    ):
        _ = await _post(pglite_engine, inbound=inbound, text="/model")

    assert inbound.drain(session) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_command_chat_cannot_run_is_refused_and_queues_nothing(
    pglite_engine: PGliteEngine,
) -> None:
    _, session = await _claude_chat(pglite_engine)
    inbound, key = InboundQueue(), uuid.uuid4()
    inbound.mark_poller(session, cli="claude")

    with pytest.raises(CommandRefusedError, match=r"^Chat cannot run /resume: "):
        _ = await _post(pglite_engine, inbound=inbound, text="/resume", key=key)

    assert inbound.drain(session) == []
    assert inbound.sender_of(key) is None
    assert await _marks_sent(pglite_engine) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_canvas_refuses_a_command_before_it_binds_a_chat(
    seeded_canvas: PGliteEngine,
) -> None:
    """No assistant runs here: the refusal comes first, so nothing else is asked."""
    key = uuid.uuid4()

    with pytest.raises(CommandRefusedError) as refused:
        _ = await _post(
            seeded_canvas,
            inbound=InboundQueue(),
            text="/goal x",
            key=key,
        )

    assert str(refused.value) == (
        "The assistant runs no slash commands. /goal works in a chat on claude or "
        "codex: pick one in the provider card."
    )
    assert isinstance(refused.value, ValueError)
    async with seeded_canvas.acquire() as conn:
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            _CANVAS,
        )
    assert CanvasMetadata.model_validate(canvas).conversation_id is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_sharers_command_goes_by_the_chats_provider_not_their_canvas(
    pglite_engine: PGliteEngine,
) -> None:
    """The sharer's own canvas is the assistant's, which would refuse /goal."""
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    sent = await _post(
        pglite_engine,
        inbound=inbound,
        text="/goal ship it",
        conversation_id=conversation,
        sharer=True,
    )

    assert sent is not None
    assert sent.session_id == session
    [heard] = inbound.drain(session)
    assert (heard.text, heard.kind, heard.source, heard.context) == (
        "/goal ship it",
        "command",
        _SHARER,
        None,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_runs_owner_answers_its_prompts(
    pglite_engine: PGliteEngine,
) -> None:
    """An answer runs a tool on the starter's machine, so a sharer's is refused."""
    conversation, session = await _claude_chat(pglite_engine)
    inbound, key = InboundQueue(), uuid.uuid4()
    inbound.mark_poller(session, cli="claude")
    answer: dict[str, object] = {"prompt_id": "p1", "decision": "allow"}

    with pytest.raises(AnswerNotOwnerError) as refused:
        _ = await _post(
            pglite_engine,
            inbound=inbound,
            text="Yes",
            kind="answer",
            payload=answer,
            conversation_id=conversation,
            sharer=True,
            key=key,
        )
    assert inbound.drain(session) == []
    assert inbound.sender_of(key) is None
    answered = await _post(
        pglite_engine,
        inbound=inbound,
        text="Yes",
        kind="answer",
        payload=answer,
    )

    assert str(refused.value) == f"Only {_ME} can answer prompts from their machine."
    assert answered is not None
    [heard] = inbound.drain(session)
    assert (heard.text, heard.kind, heard.payload, heard.source) == (
        "Yes",
        "answer",
        answer,
        _ME,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_trust_answer_chat_posts_is_queued_from_the_runs_owner_alone(
    pglite_engine: PGliteEngine,
) -> None:
    """The answer Chat's trust card posts (``web/test/seams/trustAnswer.json``).

    The web's test posts that file's body from the card the agent runner writes, and the
    run's gate takes its payload; here only its starter's post of it is queued.
    """
    seam = _CWD.parents[1] / "web" / "test" / "seams" / "trustAnswer.json"
    posted = json_loads(seam.read_text(encoding="utf-8"), dict[str, object])
    text = from_plain(posted["text"], str)
    payload = from_plain(posted["payload"], dict[str, object])
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    with pytest.raises(AnswerNotOwnerError):
        _ = await _post(
            pglite_engine,
            inbound=inbound,
            text=text,
            kind="answer",
            payload=payload,
            conversation_id=conversation,
            sharer=True,
        )
    assert inbound.drain(session) == []
    answered = await _post(
        pglite_engine,
        inbound=inbound,
        text=text,
        kind="answer",
        payload=payload,
    )

    assert posted["kind"] == "answer"
    assert answered is not None
    [heard] = inbound.drain(session)
    assert (heard.kind, heard.text, heard.payload, heard.source) == (
        "answer",
        text,
        payload,
        _ME,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_post_of_another_kind_is_not_judged_as_a_command(
    pglite_engine: PGliteEngine,
) -> None:
    """Only a line is classified: an answer that reads like a menu verb is an answer."""
    _, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session, cli="claude")

    sent = await _post(
        pglite_engine,
        inbound=inbound,
        text="/model",
        kind="answer",
        payload={"prompt_id": "p1", "decision": "allow"},
    )

    assert sent is not None
    [heard] = inbound.drain(session)
    assert (heard.text, heard.kind) == ("/model", "answer")


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
