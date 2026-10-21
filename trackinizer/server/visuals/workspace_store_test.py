"""The canvas store: the Chat provider is stored, and a replay never undoes it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call

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
from trackinizer.server.audience import reader_for
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import (
    IdempotencyReuseError,
    Inbound,
    InboundQueue,
)
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.visuals import workspace_store
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.chat_forks import ChatClosedError
from trackinizer.server.visuals.workspace_store import (
    AnswerNotOwnerError,
    AppliedOperation,
    CommandRefusedError,
    ProviderUnavailableError,
    apply_workspace_operation,
    clear_chat_station,
    name_unnamed_station,
    new_workspace,
    open_chat_session,
    open_latest_workspace,
    read_workspace,
    send_chat_line,
    set_session_campaign,
    set_station_flags,
    set_station_workstream,
    settings_refs_for_chat,
    suggest_workstream_for_chat,
    workspace_for_chat,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    StationData,
    StationLayout,
    WorkspaceData,
    WorkspaceSettingsRefs,
    WorkstreamSuggestion,
)
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


_CWD: Final = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asyncpg import Record

    from trackinizer.lib.agent.types.sessions import SessionRecord
    from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine
    from trackinizer.server.visuals.workspaces import WorkspaceState
    from trackinizer.wire.wire_science_chat import (
        ChatLineKind,
        ChatProvider,
        ChatSent,
    )


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_OTHER_USER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SOURCE_SQL: Final = (
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE id = $1 AND user_id = $2 FOR UPDATE"
)
_CLEARED_SQL: Final = (
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE user_id = $1 AND station->>'clear_key' = $2 LIMIT 1"
)
_INSERT_SQL: Final = (
    "INSERT INTO visual_workspaces (id, user_id, station, state) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
    "RETURNING id, revision, state, station"
)


def _flags_engine(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> tuple[FakeEngine, list[tuple[object, ...]]]:
    """Return a fast station-flag store with a recorded owner-scoped update."""
    conn: AsyncMock = make_conn()
    calls: list[tuple[object, ...]] = []

    async def fetchrow(sql: str, *args: object) -> object:
        del sql
        calls.append(args)
        if row is None:
            return None
        stored = cast("Mapping[str, object]", row)
        station = cast("Mapping[str, object]", stored["station"])
        changes = cast("Mapping[str, object]", args[2])
        updated = dict(stored)
        updated["station"] = dict(station) | dict(changes)
        return updated

    async def with_partner_fake(
        conn: object,
        *,
        row: object,
        **_kwargs: object,
    ) -> WorkspaceState | None:
        del conn
        if row is None:
            return None
        return workspace_store.state_from_row(cast("Mapping[str, object]", row))

    conn.fetchrow.side_effect = fetchrow
    monkeypatch.setattr(workspace_store, "_with_partner", with_partner_fake)
    return FakeEngine(conn), calls


def _set_flags(
    engine: FakeEngine,
    *,
    hidden: bool | None = None,
    pinned: bool | None = None,
) -> WorkspaceState | None:
    """Update station flags without opening PGlite."""
    return asyncio.run(
        set_station_flags(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            workspace_id=_WORKSPACE,
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
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4)",
            _WORKSPACE,
            _USER,
            {"name": "unnamed-workstream#1"},
            {"visuals": []},
        )


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_workspace(pglite_engine: PGliteEngine) -> PGliteEngine:
    """Bootstrap an empty station before the measured test call starts."""
    await _seed(pglite_engine)
    return pglite_engine


@pytest_asyncio.fixture(loop_scope="session")
async def empty_workspace_schema(pglite_engine: PGliteEngine) -> PGliteEngine:
    """Bootstrap only the schema; focused tests insert their rows directly."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    return pglite_engine


async def _station_state_without_partner(
    conn: object,
    *,
    row: object,
    **_kwargs: object,
) -> WorkspaceState:
    """Skip unrelated partner lookups while retaining the database-returned state."""
    del conn
    return workspace_store.state_from_row(cast("Mapping[str, object]", row))


async def _insert_clear_source(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    station: Mapping[str, object],
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
        "INSERT INTO visual_workspaces (id, user_id, station, state) "
        "VALUES ($1, $2, $3, $4)",
        workspace_id,
        user_id,
        station,
        state,
    )


async def _apply(
    engine: PGliteEngine,
    body: ApplyWorkspaceOperation,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
) -> AppliedOperation | None:
    return await apply_workspace_operation(
        engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=key,
        body=body,
        catalog=default_catalog(),
        inbound=inbound,
        assistant=None,
    )


def _choose(*, revision: int, provider: str) -> ApplyWorkspaceOperation:
    return ApplyWorkspaceOperation.model_validate(
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

    read = await read_workspace(
        pglite_engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
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
            "UPDATE visual_workspaces SET state = state || "
            "jsonb_build_object('provider', $2::text) WHERE id = $1",
            _WORKSPACE,
            provider,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_cli_station_gets_a_chat_and_a_session_before_any_line(
    pglite_engine: PGliteEngine,
) -> None:
    """The station's owner opens it under their key; a second call finds the same."""
    await _seed(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    assert (
        await open_chat_session(
            store,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            assistant=None,
        )
        is None
    )
    await _set_provider(pglite_engine, "claude")
    assert (
        await open_chat_session(
            store,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            assistant=None,
        )
        is None
    ), "an owner with no key has nothing to open the session with"
    key = await _owner_key(pglite_engine)

    opened = await open_chat_session(
        store,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        assistant=None,
    )
    again = await open_chat_session(
        store,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        assistant=None,
    )

    assert opened is not None
    assert again == opened
    read = await read_workspace(
        pglite_engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
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
            await workspace_store.read_chat_provider(
                conn,
                assistant=None,
                conversation_id=read.conversation_id,
            )
            == "claude"
        )
        assert (
            await workspace_store.read_chat_provider(
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
async def test_a_new_station_starts_on_its_owners_preferred_provider(
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
        state = await new_workspace(
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


def test_set_station_flags_hides_an_owned_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding writes only the owner's station flag."""
    row = _ROW | {"station": {"name": "tails", "hidden": True}}
    engine, calls = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, hidden=True)

    assert changed is not None
    assert changed.hidden is True
    assert calls == [(_WORKSPACE, _USER, {"hidden": True})]


def test_set_station_flags_unhides_without_changing_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhiding leaves the station's pin flag intact."""
    row = _ROW | {"station": {"name": "tails", "hidden": True, "pinned": True}}
    engine, _ = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, hidden=False)

    assert changed is not None
    assert (changed.hidden, changed.pinned) == (False, True)


def test_set_station_flags_pins_without_changing_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning leaves the station's hidden flag intact."""
    row = _ROW | {"station": {"name": "tails", "hidden": True}}
    engine, _ = _flags_engine(monkeypatch, row)

    changed = _set_flags(engine, pinned=True)

    assert changed is not None
    assert (changed.hidden, changed.pinned) == (True, True)


def test_set_station_flags_is_owner_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A station update matches both its id and owner."""
    engine, calls = _flags_engine(monkeypatch, None)

    changed = asyncio.run(
        set_station_flags(
            cast("DatabaseEngine", engine),
            user_id=uuid.UUID("99999999-9999-9999-9999-999999999999"),
            workspace_id=_WORKSPACE,
            hidden=True,
            inbound=InboundQueue(),
            assistant=None,
        ),
    )

    assert changed is None
    assert calls == [
        (
            _WORKSPACE,
            uuid.UUID("99999999-9999-9999-9999-999999999999"),
            {"hidden": True},
        ),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_operations_and_replays_keep_station_flags(
    seeded_workspace: PGliteEngine,
) -> None:
    """Operation responses retain flags stored in station JSON, including replays."""
    inbound = InboundQueue()
    changed = await set_station_flags(
        seeded_workspace,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        hidden=True,
        pinned=True,
        inbound=inbound,
        assistant=None,
    )
    assert changed is not None
    body = _choose(revision=0, provider="assistant")
    key = uuid.uuid4()

    applied = await _apply(seeded_workspace, body, key=key, inbound=inbound)
    replayed = await _apply(seeded_workspace, body, key=key, inbound=inbound)

    assert applied is not None
    assert replayed is not None
    assert replayed.replayed
    assert (applied.state.hidden, applied.state.pinned) == (True, True)
    assert (replayed.state.hidden, replayed.state.pinned) == (True, True)


def test_open_latest_skips_hidden_stations() -> None:
    """The latest lookup filters hidden stations before choosing a row."""
    engine, asked = _scripted(_ROW)

    state = _open(engine)

    assert state.id == _STATION
    assert asked == [(_LATEST, (_USER,))]


def test_open_latest_creates_a_scratch_station_when_all_existing_stations_are_hidden() -> (
    None
):
    """With no visible row, the store inserts after listing existing names."""
    engine, asked = _scripted(None, None, _ROW)
    workstream_id = uuid.uuid4()

    async def names(sql: str, *args: object) -> list[dict[str, object]]:
        if "FROM machines" in sql:
            return []
        assert sql == (
            "SELECT station->>'name' AS name FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'name' LIKE $2"
        )
        assert args == (_USER, "unnamed-workstream#%")
        return [{"name": "unnamed-workstream#1"}]

    engine.conn.fetch.side_effect = names
    state = _open(engine, workstream_id=workstream_id)

    assert state.id == _STATION
    assert [sql for sql, _ in asked] == [
        _LATEST,
        _LATEST,
        "INSERT INTO visual_workspaces (id, user_id, station, state) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING id, revision, state, station",
        _LATEST,
    ]
    assert asked[2][1][2] == {
        "name": "unnamed-workstream#2",
        "hidden": False,
        "pinned": False,
        "workstream_id": str(workstream_id),
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opening_a_chat_unhides_its_station(
    seeded_workspace: PGliteEngine,
) -> None:
    """A chat lookup makes its matching station visible again."""
    conversation_id = uuid.uuid4()
    async with seeded_workspace.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET station = station || "
            "jsonb_build_object('conversation_id', $2::text, 'hidden', TRUE) "
            "WHERE id = $1",
            _WORKSPACE,
            str(conversation_id),
        )

    opened = await workspace_for_chat(
        seeded_workspace,
        user_id=_USER,
        conversation_id=conversation_id,
        catalog=default_catalog(),
        inbound=InboundQueue(),
        assistant=None,
    )

    assert opened.id == _WORKSPACE
    assert opened.hidden is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_copies_source_state_and_station_fields(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clear copies the canvas and station fields while resetting its identity flags."""
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    visual_id, focused_id = uuid.uuid4(), uuid.uuid4()
    workstream_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    layout = StationLayout.model_validate(
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
    source_state = WorkspaceData.model_validate(
        {
            "visuals": [
                {"id": str(visual_id), "type": "trax.browse", "version": 1},
                {"id": str(focused_id), "type": "trax.search", "version": 2},
            ],
            "focused_instance": str(focused_id),
        },
    ).model_dump(mode="json")
    source_station = {
        "name": "Source station",
        "conversation_id": str(conversation_id),
        "page": "#/source",
        "layout": layout,
        "hidden": True,
        "pinned": True,
        "workstream_id": str(workstream_id),
    }
    async with empty_workspace_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            station=source_station,
            state=source_state,
        )

    key = uuid.uuid4()
    copied = await clear_chat_station(
        empty_workspace_schema,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    assert copied is not None

    async with empty_workspace_schema.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT station, state FROM visual_workspaces WHERE id = $1",
            copied.id,
        )
    assert row is not None
    station = cast("Mapping[str, object]", row["station"])
    state = cast("Mapping[str, object]", row["state"])
    assert state == source_state
    assert station["layout"] == layout
    assert station["page"] == "#/source"
    assert station["workstream_id"] == str(workstream_id)
    assert "conversation_id" not in station
    assert station["hidden"] is False
    assert station["pinned"] is False
    assert station["name"] == "unnamed-workstream#1"
    assert station["clear_key"] == str(key)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_same_key_reuses_station_without_another_row(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated clear key returns the persisted station without inserting again."""
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    async with empty_workspace_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            station={"name": "Source"},
            state={"visuals": []},
        )
    key = uuid.uuid4()
    first = await clear_chat_station(
        empty_workspace_schema,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    second = await clear_chat_station(
        empty_workspace_schema,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=key,
        inbound=InboundQueue(),
        assistant=None,
    )
    assert first is not None
    assert second is not None
    assert second.id == first.id
    async with empty_workspace_schema.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_workspaces WHERE user_id = $1",
            _USER,
        )
        station = await conn.fetchrow(
            "SELECT station FROM visual_workspaces WHERE id = $1",
            first.id,
        )
    assert count == 2
    assert station is not None
    stored_station = cast("Mapping[str, object]", station["station"])
    assert stored_station["clear_key"] == str(key)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_different_keys_make_distinct_stations(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new clear key creates a distinct station even for the same source."""
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    async with empty_workspace_schema.acquire() as conn:
        await _insert_clear_source(
            conn,
            user_id=_USER,
            workspace_id=_WORKSPACE,
            station={"name": "Source"},
            state={"visuals": []},
        )
    keys = (uuid.uuid4(), uuid.uuid4())
    copies = [
        await clear_chat_station(
            empty_workspace_schema,
            user_id=_USER,
            workspace_id=_WORKSPACE,
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
    async with empty_workspace_schema.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_workspaces WHERE user_id = $1",
            _USER,
        )
        saved_keys = await conn.fetch(
            "SELECT station->>'clear_key' AS key FROM visual_workspaces "
            "WHERE id = ANY($1::uuid[])",
            copy_ids,
        )
    assert count == 3
    assert {row["key"] for row in saved_keys} == {str(key) for key in keys}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_chat_returns_none_for_another_users_source(
    empty_workspace_schema: PGliteEngine,
) -> None:
    """A user cannot clear another user's station by guessing its id."""
    async with empty_workspace_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'T', 'writer', 'active')",
            _USER,
            f"{_USER}@example.com",
        )
        await _insert_clear_source(
            conn,
            user_id=_OTHER_USER,
            workspace_id=_WORKSPACE,
            station={"name": "Foreign source"},
            state={"visuals": []},
        )
    copied = await clear_chat_station(
        empty_workspace_schema,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        key=uuid.uuid4(),
        inbound=InboundQueue(),
        assistant=None,
    )
    assert copied is None
    async with empty_workspace_schema.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM visual_workspaces")
    assert count == 1


def test_a_clear_copies_the_canvas_without_its_marks_into_a_new_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy keeps page, layout and workstream, takes the key, and drops marks."""
    workstream_id, key = uuid.uuid4(), uuid.uuid4()
    mark = {
        "points": [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}],
        "id": str(uuid.uuid4()),
        "seq": 3,
        "author": "user@example.com",
        "at": "2026-10-20T00:00:00Z",
    }
    source = _ROW | {
        "id": _WORKSPACE,
        "state": {"visuals": [], "marks": [mark], "marks_seq": 3},
        "station": {
            "name": "source",
            "conversation_id": str(uuid.uuid4()),
            "page": "#/source",
            "layout": {"tab": "graph", "chat_ch": 96},
            "hidden": True,
            "pinned": True,
            "workstream_id": str(workstream_id),
        },
    }
    engine, partner = _clear_store(monkeypatch, source, None, _ROW)
    inbound = InboundQueue()
    assistant = Assistant(actor="scout", email="kb@example.com")

    copied = _clear_scripted(engine, key=key, inbound=inbound, assistant=assistant)

    assert copied is not None
    assert copied.id == _STATION
    fetched = engine.conn.fetchrow.await_args_list
    assert fetched == [
        call(_SOURCE_SQL, _WORKSPACE, _USER),
        call(_CLEARED_SQL, _USER, str(key)),
        call(
            _INSERT_SQL,
            ANY,
            _USER,
            {
                "name": "unnamed-workstream#1",
                "page": "#/source",
                "layout": {"tab": "graph", "chat_ch": 96},
                "hidden": False,
                "pinned": False,
                "workstream_id": str(workstream_id),
                "clear_key": str(key),
            },
            ANY,
        ),
    ]
    copied_data = WorkspaceData.model_validate(fetched[2].args[4])
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
    partner = AsyncMock(side_effect=_station_state_without_partner)
    monkeypatch.setattr(workspace_store, "_with_partner", partner)
    return FakeEngine(conn), partner


def _clear_scripted(
    engine: FakeEngine,
    *,
    key: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None = None,
) -> WorkspaceState | None:
    """Clear ``_WORKSPACE`` for ``_USER`` through the fast store."""
    return asyncio.run(
        clear_chat_station(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            workspace_id=_WORKSPACE,
            key=key,
            inbound=inbound,
            assistant=assistant,
        ),
    )


def test_a_retried_clear_returns_the_station_its_key_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key already used answers with its station, and inserts nothing."""
    key = uuid.uuid4()
    made = _ROW | {"station": {"name": "made", "clear_key": str(key)}}
    engine, partner = _clear_store(monkeypatch, _ROW | {"id": _WORKSPACE}, made)
    inbound = InboundQueue()
    assistant = Assistant(actor="scout", email="kb@example.com")

    copied = _clear_scripted(engine, key=key, inbound=inbound, assistant=assistant)

    assert copied is not None
    assert copied.name == "made"
    assert engine.conn.fetchrow.await_args_list == [
        call(_SOURCE_SQL, _WORKSPACE, _USER),
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


def test_a_clear_of_a_station_the_user_does_not_own_reads_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source the user does not own ends the clear after its one owner-scoped read."""
    engine, partner = _clear_store(monkeypatch, None)

    assert _clear_scripted(engine, key=uuid.uuid4(), inbound=InboundQueue()) is None
    assert engine.conn.fetchrow.await_args_list == [
        call(_SOURCE_SQL, _WORKSPACE, _USER),
    ]
    assert partner.await_args_list == []


def test_a_clear_whose_copy_never_lands_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An insert that no read ever sees raises instead of returning a missing station."""
    engine, _ = _clear_store(monkeypatch, _ROW | {"id": _WORKSPACE}, None, *[None] * 8)

    with pytest.raises(RuntimeError, match=r"^clear station insert was not visible$"):
        _ = _clear_scripted(engine, key=uuid.uuid4(), inbound=InboundQueue())


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_session_campaign_keeps_other_app_settings(
    empty_workspace_schema: PGliteEngine,
) -> None:
    """Campaign writes preserve notes and other keys in the session app."""
    session_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    async with empty_workspace_schema.acquire() as conn:
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
    store = Store(empty_workspace_schema, embed=StubEmbedder())

    written = await set_session_campaign(
        store,
        session_id=session_id,
        email="user@example.com",
        campaign={"hours": 2.0, "scientists": 3},
    )

    async with empty_workspace_schema.acquire() as conn:
        app = await conn.fetchval("SELECT app FROM inquiries WHERE id = $1", session_id)
    assert written
    assert app == {
        "settings": {
            "notes": "keep these",
            "campaign": {"hours": 2.0, "scientists": 3},
        },
    }


def test_settings_refs_carry_the_stored_workstream_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suggestion the assistant stored reads back with the station's settings."""
    conversation_id, workstream_id, personal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    suggested = uuid.UUID("eca9d638-6a14-4f36-9037-4975511afc2f")
    conn: AsyncMock = make_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": _WORKSPACE,
            "station": {
                "name": "Charm",
                "conversation_id": str(conversation_id),
                "workstream_id": str(workstream_id),
                "suggestion": {
                    "title": "Charm ARC1/2",
                    "workstream_id": str(suggested),
                },
            },
        },
    )
    personal_settings = AsyncMock(return_value=personal)
    monkeypatch.setattr(workspace_store, "open_personal_settings", personal_settings)
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

    assert refs == WorkspaceSettingsRefs(
        station_id=_WORKSPACE,
        workstream_issue_id=workstream_id,
        personal_artifact_id=personal,
        name="Charm",
        suggestion=WorkstreamSuggestion(title="Charm ARC1/2", workstream_id=suggested),
    )
    assert conn.fetchrow.await_args_list == [
        call(
            (
                "SELECT id, station FROM visual_workspaces "
                "WHERE user_id = $1 AND station->>'conversation_id' = $2"
            ),
            _USER,
            str(conversation_id),
        ),
    ]
    assert personal_settings.await_args_list == [
        call(store, email="user@example.com"),
    ]


def test_settings_refs_drop_a_suggestion_of_the_workstream_the_chat_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat already in the suggested workstream reads back with no suggestion."""
    conversation_id, workstream_id = uuid.uuid4(), uuid.uuid4()
    conn: AsyncMock = make_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": _WORKSPACE,
            "station": {
                "name": "Charm",
                "conversation_id": str(conversation_id),
                "workstream_id": str(workstream_id),
                "suggestion": {
                    "title": "Charm ARC1/2",
                    "workstream_id": str(workstream_id),
                },
            },
        },
    )
    monkeypatch.setattr(
        workspace_store,
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

    assert refs.workstream_issue_id == workstream_id
    assert refs.suggestion is None


def test_a_settings_artifact_is_submitted_as_its_owners_pinned_row() -> None:
    """The settings row is submitted owned by its user and labelled as settings."""
    conn = cast("Conn", object())
    store = MagicMock()
    store.submit_artifact = AsyncMock(return_value=_STATION)

    made = asyncio.run(
        workspace_store._make_settings_artifact(
            cast("Store", store),
            conn=conn,
            owner="user@example.com",
            title="Settings: user@example.com",
            description="Defaults.",
            app={"settings": {"notes": "kept"}},
        ),
    )

    assert made == _STATION
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
    """A fork writes the complete station layout, page and provider into its new row."""
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
        "station": {
            "name": "source",
            "page": "#/source",
            "layout": layout,
            "workstream_id": str(uuid.uuid4()),
        },
    }
    engine = FakeEngine()
    inserted: list[tuple[object, ...]] = []

    async def insert(sql: str, *args: object) -> object:
        assert sql.startswith("INSERT INTO visual_workspaces")
        inserted.append(args)
        return {"id": _STATION}

    engine.conn.fetchval.return_value = None
    engine.conn.fetch.return_value = list[dict[str, object]]()
    engine.conn.fetchrow.side_effect = insert

    made = asyncio.run(
        workspace_store._fork_station(
            cast("Conn", engine.conn),
            user_id=_USER,
            source=cast("Record", source),
            conversation_id=conversation_id,
        ),
    )

    assert made == _STATION
    copied = StationData.model_validate(inserted[0][2])
    assert copied.conversation_id == conversation_id
    assert copied.page == "#/source"
    assert copied.layout is not None
    assert [tab.id for tab in copied.layout.tabs or []] == ["graph", "issue"]
    assert copied.layout.tab == "issue"
    assert copied.layout.chat_ch == 96
    source_station = from_plain(source["station"], dict[str, object])
    source_workstream = from_plain(source_station["workstream_id"], str)
    assert copied.workstream_id == uuid.UUID(source_workstream)
    # A fork of a claude station is a claude station.
    assert WorkspaceData.model_validate(inserted[0][3]).provider == "claude"


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
    """Fork the original at a2, its claude part's answer, from the user's station."""
    return await send_chat_line(
        Store(engine, embed=StubEmbedder()),
        user_id=_USER,
        body=ChatSend(
            workspace_id=_WORKSPACE,
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
        station = await conn.fetchrow(
            "SELECT state FROM visual_workspaces WHERE station->>'conversation_id' = $1",
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
    assert station is not None
    assert WorkspaceData.model_validate(station["state"]).provider == "claude"
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
        stations = await conn.fetchval("SELECT count(*) FROM visual_workspaces")
    assert (opened, stations) == (0, 1)
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
        stations = await conn.fetchval("SELECT count(*) FROM visual_workspaces")
    assert (opened, stations) == (0, 1)


def test_moving_a_station_sets_workstream_and_clears_its_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workstream_id = uuid.uuid4()
    row = _ROW | {
        "station": {
            "name": "source",
            "suggestion": {"title": "Possible"},
        },
    }
    conn: AsyncMock = make_conn()
    updated = row | {
        "station": {"name": "source", "workstream_id": str(workstream_id)},
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
    ) -> WorkspaceState:
        del conn
        return workspace_store.state_from_row(cast("Mapping[str, object]", row))

    conn.fetchrow.side_effect = fetchrow
    monkeypatch.setattr(workspace_store, "check_workstream", allowed)
    monkeypatch.setattr(workspace_store, "_with_partner", with_partner)
    engine = FakeEngine(conn)
    state = asyncio.run(
        set_station_workstream(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            email="user@example.com",
            workspace_id=_WORKSPACE,
            workstream_id=workstream_id,
            inbound=InboundQueue(),
            assistant=None,
        ),
    )

    assert state is not None
    assert state.workstream_id == workstream_id
    assert state.suggestion is None
    assert len(calls) == 1
    assert "- 'suggestion'" in calls[0][0]
    assert calls[0][1] == (_WORKSPACE, _USER, str(workstream_id))


_STATION: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_LATEST: Final = (
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE user_id = $1 AND coalesce((station->>'hidden')::boolean, FALSE) "
    "= FALSE AND NOT station ? 'session_id' "
    "ORDER BY modified_at DESC, id DESC LIMIT 1"
)
_ROW: Final[dict[str, object]] = {
    "id": _STATION,
    "revision": 4,
    "state": {"visuals": []},
    "station": {"name": "tails", "page": "#/issue/7"},
}


def _open(
    engine: FakeEngine,
    *,
    assistant: Assistant | None = None,
    workstream_id: uuid.UUID | None = None,
) -> WorkspaceState:
    """Open the latest station through ``engine``."""
    return asyncio.run(
        open_latest_workspace(
            cast("DatabaseEngine", engine),
            user_id=_USER,
            catalog=default_catalog(),
            inbound=InboundQueue(),
            assistant=assistant,
            workstream_id=workstream_id,
        ),
    )


def _scripted(
    *latest: object,
) -> tuple[FakeEngine, list[tuple[str, tuple[object, ...]]]]:
    """Answer each read of the latest station in turn, and record every fetchrow."""
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


def test_the_station_changed_last_is_read_and_nothing_is_written() -> None:
    engine, asked = _scripted(_ROW)

    state = _open(engine, assistant=Assistant(actor="scout", email="kb@example.com"))

    assert asked == [(_LATEST, (_USER,))]
    assert (state.id, state.revision, state.name, state.page) == (
        _STATION,
        4,
        "tails",
        "#/issue/7",
    )
    assert state.conversation_id is None
    assert state.assistant == "scout"
    assert state.partner is not None
    assert state.partner.status == "unavailable"
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


def test_a_user_with_no_station_gets_the_first_one_named_and_read_back() -> None:
    engine, asked = _scripted(None, None, _ROW)

    state = _open(engine)

    assert [sql for sql, _ in asked] == [
        _LATEST,
        _LATEST,
        (
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
            "RETURNING id, revision, state, station"
        ),
        _LATEST,
    ]
    _, (new_id, owner, station, data) = asked[2]
    assert isinstance(new_id, uuid.UUID)
    # A new station persists both flags, but has no chat or page yet.
    assert owner == _USER
    assert station == {
        "name": "unnamed-workstream#1",
        "hidden": False,
        "pinned": False,
    }
    assert WorkspaceData.model_validate(data).visuals[0].type == "trax.browse"
    assert asked[3] == (_LATEST, (_USER,))
    assert state.id == _STATION
    assert state.assistant is None
    assert state.partner is None


def test_a_first_station_starts_on_its_owners_provider_and_reads_their_machines() -> (
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
    assert WorkspaceData.model_validate(data).provider == "codex"
    assert (state.available.claude, state.available.codex) == (True, False)


def test_open_latest_reuses_a_station_that_appears_during_the_names_query() -> None:
    engine, asked = _scripted(None, _ROW)

    state = _open(engine)

    assert state.id == _STATION
    assert asked == [(_LATEST, (_USER,)), (_LATEST, (_USER,))]
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


@pytest.mark.parametrize("choice", ["shared", "local"])
def test_a_stored_partner_choice_reads_as_the_assistant_provider(choice: str) -> None:
    """A canvas stored before providers says ``partner_choice``, and no provider."""
    stored: dict[str, object] = {"visuals": [], "partner_choice": choice}
    engine, _ = _scripted(_ROW | {"state": stored})

    state = _open(engine)

    assert state.provider == "assistant"
    assert "partner_choice" not in workspace_store._data_of(state).model_dump()


def test_a_station_that_stays_invisible_after_its_insert_is_an_error() -> None:
    engine, _ = _scripted(None, None, None)

    with pytest.raises(RuntimeError, match=r"^first workspace insert was not visible$"):
        _ = _open(engine)


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_name_uses_the_next_free_suffix(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_workspace_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state, station) VALUES "
            "($1, $2, $3, $4), ($5, $2, $3, $6), ($7, $2, $3, $8)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
            {"name": "unnamed-workstream#1", "conversation_id": str(conversation_id)},
            uuid.uuid4(),
            {"name": "Research"},
            uuid.uuid4(),
            {"name": "Research (2)"},
        )

    _ = await name_unnamed_station(
        empty_workspace_schema,
        email="user@example.com",
        conversation_id=conversation_id,
        name="Research",
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_workspace_schema.acquire() as conn:
        station = await conn.fetchval(
            "SELECT station FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
    assert StationData.model_validate(station).name == "Research (3)"


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_name_leaves_an_already_named_station_unchanged(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_workspace_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state, station) "
            "VALUES ($1, $2, $3, $4)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
            {"name": "Research", "conversation_id": str(conversation_id)},
        )
        version_before = await conn.fetchval(
            "SELECT xmin::text FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )

    _ = await name_unnamed_station(
        empty_workspace_schema,
        email="user@example.com",
        conversation_id=conversation_id,
        name="Ignored",
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_workspace_schema.acquire() as conn:
        station = await conn.fetchval(
            "SELECT station FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
        version_after = await conn.fetchval(
            "SELECT xmin::text FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
    assert StationData.model_validate(station).name == "Research"
    assert version_after == version_before


@pytest.mark.asyncio(loop_scope="session")
async def test_suggestion_updates_chat_station_without_creating_an_issue(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    conversation_id = uuid.uuid4()
    async with empty_workspace_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state, station) "
            "VALUES ($1, $2, $3, $4)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
            {"name": "Research", "conversation_id": str(conversation_id)},
        )
        issues_before = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE kind = 'Issue'",
        )

    _ = await suggest_workstream_for_chat(
        empty_workspace_schema,
        user_id=_USER,
        email="user@example.com",
        conversation_id=conversation_id,
        suggestion=WorkstreamSuggestion(title="A possible stream"),
        inbound=InboundQueue(),
        assistant=None,
    )
    async with empty_workspace_schema.acquire() as conn:
        station = await conn.fetchval(
            "SELECT station FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
        issues_after = await conn.fetchval(
            "SELECT count(*) FROM inquiries WHERE kind = 'Issue'",
        )
    assert StationData.model_validate(station).suggestion == WorkstreamSuggestion(
        title="A possible stream",
    )
    assert issues_after == issues_before


@pytest.mark.asyncio(loop_scope="session")
async def test_a_suggestion_of_the_chats_own_workstream_reads_back_as_none(
    empty_workspace_schema: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat sits in W: W is no suggestion; another workstream still is."""
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    monkeypatch.setattr(
        workspace_store,
        "check_workstream",
        AsyncMock(return_value=True),
    )
    conversation_id, workstream_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with empty_workspace_schema.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'user@example.com', 'Test', 'writer', 'active')",
            _USER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, state, station) "
            "VALUES ($1, $2, $3, $4)",
            _WORKSPACE,
            _USER,
            {"visuals": []},
            {
                "name": "Research",
                "conversation_id": str(conversation_id),
                "workstream_id": str(workstream_id),
            },
        )

    async def suggest(target: uuid.UUID) -> WorkspaceState | None:
        return await suggest_workstream_for_chat(
            empty_workspace_schema,
            user_id=_USER,
            email="user@example.com",
            conversation_id=conversation_id,
            suggestion=WorkstreamSuggestion(workstream_id=target, title="Charm"),
            inbound=InboundQueue(),
            assistant=None,
        )

    own = await suggest(workstream_id)
    assert own is not None
    assert own.suggestion is None
    elsewhere = await suggest(other)
    assert elsewhere is not None
    assert elsewhere.suggestion == WorkstreamSuggestion(
        workstream_id=other,
        title="Charm",
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_boxes_are_stored_read_back_and_dropped_by_a_cleared_station(
    seeded_workspace: PGliteEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mark survives the write and the read; a clear-chat copy starts with none."""
    monkeypatch.setattr(
        workspace_store,
        "_with_partner",
        _station_state_without_partner,
    )
    inbound = InboundQueue()
    drawn = await _apply(
        seeded_workspace,
        ApplyWorkspaceOperation.model_validate(
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
    read = await read_workspace(
        seeded_workspace,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        inbound=inbound,
        assistant=None,
    )
    assert read is not None
    assert [(m.page, m.seq) for m in read.marks] == [("#/graph", 1)]
    assert [m.id for m in read.marks] == [m.id for m in drawn.state.marks]
    assert read.marks_seq == 1

    copied = await clear_chat_station(
        seeded_workspace,
        user_id=_USER,
        workspace_id=_WORKSPACE,
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
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE id = $1 AND user_id = $2 FOR UPDATE"
)
_SELECT_CLEARED: Final = (
    "SELECT id, revision, state, station FROM visual_workspaces "
    "WHERE user_id = $1 AND station->>'clear_key' = $2 LIMIT 1"
)
_INSERT_STATION: Final = (
    "INSERT INTO visual_workspaces (id, user_id, station, state) "
    "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING "
    "RETURNING id, revision, state, station"
)


def _clear_source() -> dict[str, object]:
    """Return a claude station's row with a chat, page, layout, mark and flags."""
    data = WorkspaceData.model_validate(
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
        "id": _WORKSPACE,
        "revision": 9,
        "state": data.model_dump(mode="json"),
        "station": {
            "name": "source",
            "conversation_id": str(uuid.UUID(int=2)),
            "page": "#/issue/7",
            "layout": {"tab": "issue", "chat_ch": 96},
            "hidden": True,
            "pinned": True,
            "workstream_id": str(_SOURCE_STREAM),
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
) -> tuple[WorkspaceState | None, _Clearing]:
    """Clear the user's station against a scripted connection; return what it did."""
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
        return {"id": args[0], "revision": 0, "state": args[3], "station": args[2]}

    async def names(sql: str, *args: object) -> list[dict[str, object]]:
        clearing.asked.append((sql, args))
        return [{"name": "unnamed-workstream#1"}]

    async def partner(given: object, **found: object) -> WorkspaceState | None:
        clearing.partnered.append({"on_the_clears_conn": given is conn, **found})
        row = found["row"]
        return (
            None
            if row is None
            else workspace_store.state_from_row(cast("Mapping[str, object]", row))
        )

    conn.fetchrow.side_effect = fetchrow
    conn.fetch.side_effect = names
    monkeypatch.setattr(workspace_store, "_with_partner", partner)
    state = asyncio.run(
        clear_chat_station(
            cast("DatabaseEngine", FakeEngine(conn)),
            user_id=_USER,
            workspace_id=_WORKSPACE,
            key=_CLEAR_KEY,
            inbound=_CLEAR_INBOUND,
            assistant=_KB,
        ),
    )
    return state, clearing


def test_a_clear_copies_the_canvas_without_marks_chat_or_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy keeps the page, layout, workstream and provider, under the next name."""
    state, clearing = _clear(monkeypatch, source=_clear_source())

    assert [sql for sql, _ in clearing.asked] == [
        _SELECT_SOURCE,
        _SELECT_CLEARED,
        (
            "SELECT station->>'name' AS name FROM visual_workspaces "
            "WHERE user_id = $1 AND station->>'name' LIKE $2"
        ),
        _INSERT_STATION,
    ]
    assert clearing.asked[0][1] == (_WORKSPACE, _USER)
    assert clearing.asked[1][1] == (_USER, str(_CLEAR_KEY))
    assert clearing.asked[2][1] == (_USER, "unnamed-workstream#%")
    new_id, owner, station, data = clearing.asked[3][1]
    assert isinstance(new_id, uuid.UUID)
    assert new_id != _WORKSPACE
    assert owner == _USER
    assert station == {
        "name": "unnamed-workstream#2",
        "page": "#/issue/7",
        "layout": {"tab": "issue", "chat_ch": 96},
        "hidden": False,
        "pinned": False,
        "workstream_id": str(_SOURCE_STREAM),
        "clear_key": str(_CLEAR_KEY),
    }
    copied = WorkspaceData.model_validate(data)
    assert (copied.provider, copied.agent_instructions) == ("claude", "be brief")
    assert (copied.marks, copied.marks_seq) == ([], 2)
    assert clearing.partnered == [
        {
            "on_the_clears_conn": True,
            "row": {"id": new_id, "revision": 0, "state": data, "station": station},
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
    first = _clear_source() | {"id": _STATION}

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
    assert state.id == _STATION


def test_a_clear_of_a_station_the_user_does_not_own_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, clearing = _clear(monkeypatch, source=None)

    assert state is None
    assert [sql for sql, _ in clearing.asked] == [_SELECT_SOURCE]
    assert clearing.partnered == []


def test_a_clear_whose_copy_never_appears_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match=r"^clear station insert was not visible$"):
        _ = _clear(monkeypatch, source=_clear_source(), inserted=False)


@dataclass(slots=True, kw_only=True)
class _Submitted:
    """A store that records each Artifact it is asked to submit."""

    calls: list[tuple[SubmitArtifact, dict[str, object]]]

    async def submit_artifact(self, body: SubmitArtifact, **how: object) -> uuid.UUID:
        self.calls.append((body, how))
        return _STATION


def test_a_settings_artifact_is_its_owners_with_the_settings_label() -> None:
    store = _Submitted(calls=[])
    conn = make_conn()
    app: dict[str, object] = {"campaign": {"hours": 2.0}}

    made = asyncio.run(
        workspace_store._make_settings_artifact(
            cast("Store", store),
            conn=cast("Conn", conn),
            owner=_ME,
            title="Settings: user@example.com",
            description="Your defaults.",
            app=app,
        ),
    )

    assert made == _STATION
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
_SHARER_STATION: Final = uuid.UUID("66666666-6666-6666-6666-666666666666")


# The sharer has a station of their own on the assistant, holding no chat.
async def _claude_chat(engine: PGliteEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """Put the user's station on claude, holding a chat their run drains, and share it."""
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
            "UPDATE visual_workspaces SET state = state || jsonb_build_object("
            "'provider', 'claude', 'marks_seq', 3), station = station || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            _WORKSPACE,
            str(conversation),
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'G', 'writer', 'active')",
            _SHARER_ID,
            _SHARER,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, station, state) "
            "VALUES ($1, $2, $3, $4)",
            _SHARER_STATION,
            _SHARER_ID,
            {"name": "unnamed-workstream#1"},
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
    """Post from the user's station, or the sharer's, as a browser does."""
    poster = _SHARER if sharer else _ME
    return await send_chat_line(
        Store(engine, embed=StubEmbedder()),
        user_id=_SHARER_ID if sharer else _USER,
        body=ChatSend(
            workspace_id=_SHARER_STATION if sharer else _WORKSPACE,
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
            "SELECT state FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
    return WorkspaceData.model_validate(state).marks_sent


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_claude_chat_queues_a_known_command_bare_and_a_slash_line_as_a_line(
    pglite_engine: PGliteEngine,
) -> None:
    """The command is typed as the CLI's own; the path stays a person's line."""
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session)

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
async def test_a_command_chat_cannot_run_is_refused_and_queues_nothing(
    pglite_engine: PGliteEngine,
) -> None:
    _, session = await _claude_chat(pglite_engine)
    inbound, key = InboundQueue(), uuid.uuid4()
    inbound.mark_poller(session)

    with pytest.raises(CommandRefusedError, match=r"^Chat cannot run /model: "):
        _ = await _post(pglite_engine, inbound=inbound, text="/model opus", key=key)

    assert inbound.drain(session) == []
    assert inbound.sender_of(key) is None
    assert await _marks_sent(pglite_engine) == 0


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_station_refuses_a_command_before_it_binds_a_chat(
    seeded_workspace: PGliteEngine,
) -> None:
    """No assistant runs here: the refusal comes first, so nothing else is asked."""
    key = uuid.uuid4()

    with pytest.raises(CommandRefusedError) as refused:
        _ = await _post(
            seeded_workspace,
            inbound=InboundQueue(),
            text="/goal x",
            key=key,
        )

    assert str(refused.value) == (
        "The assistant runs no slash commands. /goal works in a chat on claude or "
        "codex: pick one in the provider card."
    )
    assert isinstance(refused.value, ValueError)
    async with seeded_workspace.acquire() as conn:
        station = await conn.fetchval(
            "SELECT station FROM visual_workspaces WHERE id = $1",
            _WORKSPACE,
        )
    assert StationData.model_validate(station).conversation_id is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_sharers_command_goes_by_the_chats_provider_not_their_station(
    pglite_engine: PGliteEngine,
) -> None:
    """The sharer's own station is the assistant's, which would refuse /goal."""
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session)

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
    inbound.mark_poller(session)
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

    The web's test posts that file's body from the card ``trax run`` writes, and the
    run's gate takes its payload; here only its starter's post of it is queued.
    """
    seam = _CWD.parents[1] / "web" / "test" / "seams" / "trustAnswer.json"
    posted = json_loads(seam.read_text(encoding="utf-8"), dict[str, object])
    text = from_plain(posted["text"], str)
    payload = from_plain(posted["payload"], dict[str, object])
    conversation, session = await _claude_chat(pglite_engine)
    inbound = InboundQueue()
    inbound.mark_poller(session)

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
    inbound.mark_poller(session)

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
