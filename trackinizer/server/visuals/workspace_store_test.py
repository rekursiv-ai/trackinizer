"""The canvas store: the Chat partner choice is stored, and a replay never undoes it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

from treekle import from_plain

import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, executed_sql, make_conn
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.config import Assistant
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.visuals import workspace_store
from trackinizer.server.visuals.catalog import default_catalog
from trackinizer.server.visuals.workspace_store import (
    AppliedOperation,
    apply_workspace_operation,
    clear_chat_station,
    name_unnamed_station,
    open_latest_workspace,
    read_workspace,
    set_session_campaign,
    set_station_flags,
    set_station_workstream,
    suggest_workstream_for_chat,
    workspace_for_chat,
)
from trackinizer.server.visuals.workspaces import (
    ApplyWorkspaceOperation,
    StationData,
    StationLayout,
    WorkspaceData,
    WorkstreamSuggestion,
)


if TYPE_CHECKING:
    from collections.abc import Mapping
    from unittest.mock import AsyncMock

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine
    from trackinizer.server.visuals.workspaces import WorkspaceState


_USER: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_WORKSPACE: Final = uuid.UUID("55555555-5555-5555-5555-555555555555")
_OTHER_USER: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")


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


def _choose(*, revision: int, choice: str) -> ApplyWorkspaceOperation:
    return ApplyWorkspaceOperation.model_validate(
        {"revision": revision, "operation": {"kind": "partner", "choice": choice}},
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_choice_is_stored_and_a_replay_answers_with_the_current_one(
    pglite_engine: PGliteEngine,
) -> None:
    """A retried choice returns its receipt, but the partner follows what is chosen now."""
    await _seed(pglite_engine)
    inbound = InboundQueue()
    first_key, second_key = uuid.uuid4(), uuid.uuid4()

    local = await _apply(
        pglite_engine,
        _choose(revision=0, choice="local"),
        key=first_key,
        inbound=inbound,
    )
    assert local is not None
    assert local.state.partner_choice == "local"
    assert local.state.partner is not None
    assert local.state.partner.kind == "local"
    assert local.state.revision == 1

    shared = await _apply(
        pglite_engine,
        _choose(revision=1, choice="shared"),
        key=second_key,
        inbound=inbound,
    )
    assert shared is not None
    assert shared.state.partner_choice == "shared"
    assert shared.state.partner is None

    replay = await _apply(
        pglite_engine,
        _choose(revision=0, choice="local"),
        key=first_key,
        inbound=inbound,
    )
    assert replay is not None
    assert replay.replayed
    assert replay.state.partner_choice == "shared"

    read = await read_workspace(
        pglite_engine,
        user_id=_USER,
        workspace_id=_WORKSPACE,
        inbound=inbound,
        assistant=None,
    )
    assert read is not None
    assert (read.partner_choice, read.revision) == ("shared", 2)


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
    body = _choose(revision=0, choice="local")
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


def test_a_fork_copies_every_layout_tab_and_page() -> None:
    """A fork writes the complete station layout and page into its new row."""
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
    engine.conn.fetch.return_value = []
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
    "= FALSE ORDER BY modified_at DESC, id DESC LIMIT 1"
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


def test_open_latest_reuses_a_station_that_appears_during_the_names_query() -> None:
    engine, asked = _scripted(None, _ROW)

    state = _open(engine)

    assert state.id == _STATION
    assert asked == [(_LATEST, (_USER,)), (_LATEST, (_USER,))]
    assert not any(sql.startswith("INSERT") for sql in executed_sql(engine.conn))


def test_a_local_partner_is_looked_for_among_the_owners_own_helpers() -> None:
    local_state: dict[str, object] = {"visuals": [], "partner_choice": "local"}
    engine, _ = _scripted(_ROW | {"state": local_state})

    state = _open(engine)

    assert state.partner is not None
    assert (state.partner.kind, state.partner.status) == ("local", "unavailable")
    assert engine.conn.fetch.call_args.args[2] == _USER


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


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
