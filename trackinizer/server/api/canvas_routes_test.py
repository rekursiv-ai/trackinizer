"""A canvas canvas persists and rejects stale concurrent operations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final, cast
from unittest.mock import AsyncMock, MagicMock

import asyncio
import json
import uuid

from fastapi import HTTPException
from starlette.requests import Request
from treekle import from_plain, json_loads

import httpx2
import pytest
import pytest_asyncio

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.api import canvas_routes
from trackinizer.server.api._deps import terminal_hub
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_routes import _current_state
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_KEY_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    Agent,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    open_canvas,
    revision_of,
    seed_accounts,
    seed_converse,
    seed_science_chat,
    seed_session,
    send_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.api.session_access import Asker
from trackinizer.server.asks import open_ask_ids, release_gone_runs
from trackinizer.server.asks_test_support import open_ask
from trackinizer.server.audience import SessionReader
from trackinizer.server.chat_hub import (
    ChatHub,
    HighlightFrame,
    NavigateFrame,
    iter_canvas_events,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.notify import SSE_PING
from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.core import Store
from trackinizer.server.terminals import Opening
from trackinizer.server.visuals.canvas_store import (
    open_personal_settings,
    settings_refs_for_chat,
)
from trackinizer.server.visuals.canvases import (
    NO_MACHINE_REASON,
    CampaignSetup,
    CanvasSettingsRefs,
    CanvasState,
    NameChat,
    NewCanvas,
    SessionCampaign,
)
from trackinizer.server.visuals.catalog import (
    RECORD_VISUAL,
    Canvas,
    StaticVisual,
    default_canvas,
    offer_record_visual,
)
from trackinizer.server.visuals.workspaces import (
    SetCanvasWorkspace,
    open_scratch,
)
from trackinizer.wire.wire_science_chat import (
    SCIENCE_CHAT_LABEL,
    ChatHead,
    chat_session_id,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from trackinizer.lib.postgres import Conn, DatabaseEngine, PGliteEngine
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.chat_hub import Frame, Subscription
    from trackinizer.server.terminals import Terminal
    from trackinizer.server.visuals.catalog import VisualCatalogBody
    from trackinizer.wire.wire_science_chat import ChatProvider
    from trackinizer.wire.wire_terminals import TerminalCommand


async def _clear_chat_fake(
    engine: object,
    *,
    user_id: uuid.UUID,
    canvas_id: uuid.UUID,
    key: uuid.UUID,
    inbound: InboundQueue,
    assistant: Assistant | None,
    seen: list[tuple[object, dict[str, object]]],
    results: list[CanvasState | None],
) -> CanvasState | None:
    seen.append(
        (
            engine,
            {
                "user_id": user_id,
                "canvas_id": canvas_id,
                "key": key,
                "inbound": inbound,
                "assistant": assistant,
            },
        ),
    )
    return results.pop(0)


async def _open_scratch_fake(
    engine: DatabaseEngine,
    *,
    store: Store,
    user_id: uuid.UUID,
    email: str,
    calls: list[tuple[str, object]],
    scratch_id: uuid.UUID,
) -> uuid.UUID:
    del engine, store, user_id, email
    calls.append(("scratch", None))
    return scratch_id


async def _check_workspace_fake(
    engine: DatabaseEngine,
    *,
    email: str,
    workspace_id: uuid.UUID,
    calls: list[tuple[str, object]],
    result: bool = True,
) -> bool:
    del engine, email, workspace_id
    calls.append(("check", None))
    return result


async def _new_canvas_fake(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    catalog: VisualCatalogBody,
    inbound: InboundQueue,
    assistant: Assistant | None,
    workspace_id: uuid.UUID | None = None,
    provider: ChatProvider | None = None,
    calls: list[tuple[str, object]],
    state: CanvasState,
) -> CanvasState:
    del engine, user_id, catalog, inbound, assistant
    calls.append(("new", workspace_id))
    if provider is not None:
        calls.append(("provider", provider))
    return state


async def _name_canvas_fake(
    engine: DatabaseEngine,
    *,
    email: str,
    conversation_id: uuid.UUID,
    name: str,
    inbound: InboundQueue,
    assistant: Assistant | None,
    sent: dict[str, object],
    state: CanvasState,
) -> CanvasState:
    del engine, inbound, assistant
    sent.update(email=email, conversation_id=conversation_id, name=name)
    return state


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_operations_are_shared_revisioned_and_idempotent(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Two readers see the same state and a stale write cannot erase it."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_canvas_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )

    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/canvases")
    assert created.status_code == 200
    initial = json_loads(created.content, dict[str, object])
    canvas_id = from_plain(initial["id"], str)
    assert initial["revision"] == 0
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(initial["visuals"], list[object])
    ] == ["trax.browse"]

    record_id = str(uuid.uuid4())
    key = str(uuid.uuid4())
    operation = {
        "revision": 0,
        "operation": {
            "kind": "show",
            "visual_type": "trax.subgraph",
            "record_id": record_id,
            "placement": "side",
        },
    }
    shown = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert shown.status_code == 200
    updated = json_loads(shown.content, dict[str, object])
    assert updated["revision"] == 1
    assert {
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(updated["visuals"], list[object])
    } == {"trax.browse", "trax.subgraph"}

    replay = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    assert json.loads(replay.content) == json.loads(shown.content)
    mismatched_replay = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": record_id,
                "placement": "main",
            },
        },
        headers={"Idempotency-Key": key},
    )
    assert mismatched_replay.status_code == 409
    mismatch = json_loads(mismatched_replay.content, dict[str, object])
    assert from_plain(mismatch["current"], dict[str, object])["revision"] == 1
    same_dump_different_intent = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "placement": "side",
                "record_id": record_id,
                "params": {},
            },
        },
        headers={"Idempotency-Key": key},
    )
    assert same_dump_different_intent.status_code == 409
    stale = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json=operation,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert stale.status_code == 409
    conflict = json_loads(stale.content, dict[str, object])
    assert from_plain(conflict["current"], dict[str, object])["revision"] == 1

    response = await client.get(f"/api/canvases/{canvas_id}")
    assert response.status_code == 200
    assert json.loads(response.content) == json.loads(shown.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_can_show_record_resolved_by_a_separate_read_profile(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A canvas stores a record reference even when its read graph is remote."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_canvas_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/canvases")
    canvas_id = from_plain(
        json_loads(created.content, dict[str, object])["id"],
        str,
    )
    record_id = str(uuid.uuid4())
    shown = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": record_id,
                "placement": "side",
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    visuals = from_plain(
        json_loads(shown.content, dict[str, object])["visuals"],
        list[object],
    )
    assert from_plain(visuals[-1], dict[str, object])["record_id"] == record_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_preference_does_not_gate_owned_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The UI preference does not prevent an owned report-chat canvas."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active')",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/canvases")
    assert created.status_code == 200
    canvas_id = from_plain(
        json_loads(created.content, dict[str, object])["id"],
        str,
    )
    enabled = await client.put("/api/me/visual-canvas", json={"enabled": True})
    assert enabled.status_code == 200
    assert json_loads(enabled.content, dict[str, object])["enabled"] is True
    disabled = await client.put("/api/me/visual-canvas", json={"enabled": False})
    assert disabled.status_code == 200
    still_owned = await client.get(f"/api/canvases/{canvas_id}")
    assert still_owned.status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_owners_own_key_cannot_create_read_or_operate_a_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Chat's assistant uses a canvas with a key; the owner's own is told so."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    await seed_accounts(store)
    act_as_user_agent()
    assert (await client.post("/api/canvases")).status_code == 403
    canvas_id = await open_canvas(client)
    act_as_user_agent()
    # A live session of the owner's own changes nothing.
    await start_session(client)
    read = await client.get(f"/api/canvases/{canvas_id}")
    operated = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": 0,
            "operation": {"kind": "show", "visual_type": "trax.browse"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    for refused in (read, operated):
        assert refused.status_code == 403, refused.text
        assert "Only the canvas's Chat partner" in refused.text
    act_as_other_agent()
    assert (await client.get(f"/api/canvases/{canvas_id}")).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_that_stops_polling_is_not_messageable(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB-active session without recent inbound polling is unavailable."""
    client, store = pglite_route_client
    now = [100.0]
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(
        app.state,
        "inbound",
        InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0]),
        raising=False,
    )
    await seed_accounts(store)
    canvas_id = str(await open_canvas(client))
    act_as_assistant()
    started = await client.post(
        "/api/sessions/start",
        json={"cli": "codex", "actor": KB_ACTOR},
    )
    session_id = from_plain(
        json_loads(started.content, dict[str, object])["id"],
        str,
    )
    browser()
    assert (await _partner(client, canvas_id=canvas_id))["status"] == "unavailable"

    act_as_assistant()
    assert (await drain(client, session_id=uuid.UUID(session_id))).status_code == 200
    browser()
    assert (await _partner(client, canvas_id=canvas_id))["status"] == "live"

    now[0] = 145.0
    assert (await _partner(client, canvas_id=canvas_id))["status"] == "unavailable"
    assert (
        await client.post(
            "/api/chats",
            json={
                "canvas_id": canvas_id,
                "text": "Cannot deliver to a dead poller",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409


def test_current_state_reads_the_canvas_with_its_ids_or_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fast read of ``_current_state``: it passes the owner and ids through, or 404s.

    No marker, so this runs under the mutation gate, where the route tests (db_pglite)
    are deselected.
    """
    engine, inbound, assistant, state = object(), object(), object(), object()
    owner, found, missing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    request = cast(Request, object())
    seen: list[tuple[object, dict[str, object]]] = []

    # Each reader is passed the request, so a mutant that drops or swaps that argument
    # is caught here rather than slipping through a fake that ignores it.
    def give_engine(got: Request) -> object:
        assert got is request
        return engine

    def give_inbound(got: Request) -> object:
        assert got is request
        return inbound

    def give_assistant(got: Request) -> object:
        assert got is request
        return assistant

    async def read(engine_arg: object, **kwargs: object) -> object:
        seen.append((engine_arg, kwargs))
        return state if kwargs["canvas_id"] == found else None

    monkeypatch.setattr(canvas_routes, "engine_of", give_engine)
    monkeypatch.setattr(canvas_routes, "get_inbound", give_inbound)
    monkeypatch.setattr(canvas_routes, "get_assistant", give_assistant)
    monkeypatch.setattr(canvas_routes, "read_canvas", read)

    reader = SessionReader(email="owner@example.com")
    got = asyncio.run(
        _current_state(request, user_id=owner, canvas_id=found, reader=reader),
    )
    assert got is state
    assert seen[-1] == (
        engine,
        {
            "user_id": owner,
            "canvas_id": found,
            "inbound": inbound,
            "assistant": assistant,
            "reader": reader,
        },
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            _current_state(request, user_id=owner, canvas_id=missing, reader=reader),
        )
    assert caught.value.status_code == 404
    assert caught.value.detail == "Canvas not found"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_patch_hides_pins_and_updates_atomically_with_its_name(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Flags update with a rename; a duplicate name leaves both flags unchanged."""
    client, store = pglite_route_client
    canvas_id = await _canvas(client, store)
    browser()

    second_id = await open_canvas(client, new=True)
    second = await client.patch(
        f"/api/canvases/{second_id}",
        json={"hidden": True, "pinned": True},
    )
    assert second.status_code == 200
    second_state = json_loads(second.content, dict[str, object])
    assert (second_state["hidden"], second_state["pinned"]) == (True, True)

    duplicate = await client.patch(
        f"/api/canvases/{canvas_id}",
        json={"name": second_state["name"], "hidden": True, "pinned": True},
    )
    assert duplicate.status_code == 409
    unchanged = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    assert (unchanged["hidden"], unchanged["pinned"]) == (False, False)

    unhidden = await client.patch(
        f"/api/canvases/{second_id}",
        json={"hidden": False},
    )
    assert unhidden.status_code == 200
    pinned = json_loads(unhidden.content, dict[str, object])
    assert (pinned["hidden"], pinned["pinned"]) == (False, True)
    assert (
        await client.patch(f"/api/canvases/{canvas_id}", json={})
    ).status_code == 422

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    foreign = await client.patch(
        f"/api/canvases/{second_id}",
        json={"hidden": True},
    )
    assert foreign.status_code == 404


def test_clear_chat_route_passes_owner_and_key_and_returns_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route forwards its owner and key, and maps a missing canvas to 404."""
    request = cast(Request, object())
    identity = make_test_identity(api_key_id=None)
    canvas_id, key = uuid.uuid4(), uuid.uuid4()
    state = CanvasState(
        id=canvas_id,
        revision=0,
        name="unnamed-canvas#1",
        conversation_id=None,
        page=None,
        visuals=[],
    )
    engine, inbound, assistant = object(), object(), object()
    seen: list[tuple[object, dict[str, object]]] = []
    results: list[CanvasState | None] = [state, None]

    def require_browser(identity: object) -> None:
        del identity

    def give_engine(got: Request) -> object:
        assert got is request
        return engine

    def give_inbound(got: Request) -> object:
        assert got is request
        return inbound

    def give_assistant(got: Request) -> object:
        assert got is request
        return assistant

    monkeypatch.setattr(canvas_routes, "require_browser", require_browser)
    monkeypatch.setattr(canvas_routes, "engine_of", give_engine)
    monkeypatch.setattr(canvas_routes, "get_inbound", give_inbound)
    monkeypatch.setattr(canvas_routes, "get_assistant", give_assistant)
    monkeypatch.setattr(
        canvas_routes,
        "clear_chat_canvas",
        partial(_clear_chat_fake, seen=seen, results=results),
    )

    opened = asyncio.run(
        canvas_routes.clear_chat_route(
            canvas_id=canvas_id,
            request=request,
            identity=identity,
            key=key,
        ),
    )
    assert opened is state
    assert seen[0] == (
        engine,
        {
            "user_id": identity.user_id,
            "canvas_id": canvas_id,
            "key": key,
            "inbound": inbound,
            "assistant": assistant,
        },
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            canvas_routes.clear_chat_route(
                canvas_id=canvas_id,
                request=request,
                identity=identity,
                key=key,
            ),
        )
    assert caught.value.status_code == 404


async def _canvas(client: httpx2.AsyncClient, store: Store) -> str:
    """Seed the accounts and open the user's canvas; return its id."""
    await seed_accounts(store)
    return str(await open_canvas(client))


async def _canvas_with_graph(
    client: httpx2.AsyncClient,
    *,
    user_id: uuid.UUID = TEST_USER_ID,
    email: str = TEST_USER_EMAIL,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Open a user's canvas and show a context graph on it; return both ids."""
    canvas_id = await open_canvas(client, user_id=user_id, email=email)
    shown = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": str(uuid.uuid4()),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200, shown.text
    graph = next(
        from_plain(visual["id"], str)
        for visual in from_plain(
            json_loads(shown.content, dict[str, object])["visuals"],
            list[dict[str, object]],
        )
        if visual["type"] == "trax.subgraph"
    )
    return canvas_id, uuid.UUID(graph)


async def _post_chat(
    client: httpx2.AsyncClient,
    body: Mapping[str, object],
    *,
    key: str | None = None,
) -> httpx2.Response:
    """Post a line under ``key``, or a fresh one."""
    return await client.post(
        "/api/chats",
        json=body,
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


async def _messages(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> list[dict[str, object]]:
    """Drain a session as the assistant; return what it heard."""
    act_as_assistant()
    drained = await drain(client, session_id=session_id)
    assert drained.status_code == 200
    return from_plain(
        json_loads(drained.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_reaches_the_assistant_with_the_page_and_no_pinned_record(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser line carries its canvas, page and trail; the canvas pins no record."""
    client, store = pglite_route_client
    canvas_id = await _canvas(client, store)
    record_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'Experiment', 987654, 'active', "
            "'test-user@example.com', 'Measured tails')",
            record_id,
        )
    message_path = "/api/chats"
    assert (
        await client.post(
            message_path,
            json={
                "canvas_id": canvas_id,
                "text": "Before the assistant runs",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    session_id = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    body = {
        "canvas_id": canvas_id,
        "text": "What do the tails show?",
        "page": f"#/lookup/{record_id}",
        "trail": ["#/graph"],
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    sent = await client.post(message_path, json=body, headers=headers)
    assert sent.status_code == 200
    receipt = json_loads(sent.content, dict[str, object])
    # The assistant has not opened the conversation's session yet, and a new
    # conversation is named by the key that started it.
    assert receipt["session_id"] is None
    assert receipt["conversation_id"] == headers["Idempotency-Key"]
    replay = await client.post(message_path, json=body, headers=headers)
    assert json_loads(replay.content) == json_loads(sent.content)
    live = await _partner(client, canvas_id=canvas_id)
    assert live == {
        "session_id": str(session_id),
        "actor": KB_ACTOR,
        "cli": "codex",
        "status": "live",
        "provider": "assistant",
        "state": "idle",
        "machine": None,
        "run_name": None,
        "cwd": None,
        "label": KB_ACTOR,
        "refused": "",
        "refused_at": None,
    }
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET owner = NULL WHERE id = $1",
            session_id,
        )
    assert (await _partner(client, canvas_id=canvas_id))["status"] == "unavailable"
    assert (
        await client.post(
            message_path,
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET owner = $2 WHERE id = $1",
            session_id,
            KB_ACTOR,
        )

    messages = await _messages(client, session_id=session_id)
    assert len(messages) == 1
    message = messages[0]
    assert message["source"] == "test-user@example.com"
    context = from_plain(message["context"], dict[str, object])
    assert context["canvas_id"] == canvas_id
    # The page is the screen's record; nothing else pins one to the line.
    assert {"record_id", "record", "artifact_content"}.isdisjoint(context)
    assert from_plain(context["page"], dict[str, object]) == {
        "route": f"#/lookup/{record_id}",
        "record": {
            "id": str(record_id),
            "kind": "Experiment",
            "seq": 987_654,
            "title": "Measured tails",
        },
    }
    assert context["trail"] == [{"route": "#/graph", "record": None}]
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(context["visible_visuals"], list[object])
    ] == ["trax.browse"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_naming_a_chat_visual_or_a_pinned_record_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Chat is no visual and pins no record: a line that names either is malformed."""
    client, store = pglite_route_client
    canvas_id = await _canvas(client, store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    body = {"canvas_id": canvas_id, "text": "What do the tails show?"}
    assert (await _post_chat(client, body)).status_code == 200
    for removed in (
        {"chat_instance_id": str(uuid.uuid4())},
        {"expected_record_id": str(uuid.uuid4())},
        {"record_title": "forged"},
    ):
        assert (await _post_chat(client, {**body, **removed})).status_code == 422


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_restarted_assistant_answers_from_its_newest_session_until_none_is_live(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A send replays whichever session answers."""
    client, store = pglite_route_client
    canvas_id = await _canvas(client, store)
    session_id = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    body = {"canvas_id": canvas_id, "text": "What do the tails show?"}
    key = str(uuid.uuid4())
    sent = await _post_chat(client, body, key=key)
    assert sent.status_code == 200
    assert len(await _messages(client, session_id=session_id)) == 1

    # The assistant restarts: its newest live session answers from now on.
    second_session_id = await seed_session(store, ASSISTANT, actor=f"{KB_ACTOR}#2")
    browser()
    # The same send is a replay of the first, whichever session now answers.
    replayed = await _post_chat(client, body, key=key)
    assert replayed.status_code == 200
    assert json_loads(replayed.content) == json_loads(sent.content)
    assert (await _post_chat(client, body)).status_code == 200
    assert len(await _messages(client, session_id=second_session_id)) == 1
    assert await _messages(client, session_id=session_id) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_refuses_a_line_once_every_session_of_the_assistant_has_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An older session does not answer for a newer one that ended."""
    client, store = pglite_route_client
    canvas_id = await _canvas(client, store)
    sessions = [
        await seed_session(store, ASSISTANT, actor=actor)
        for actor in (KB_ACTOR, f"{KB_ACTOR}#2")
    ]
    browser()
    body = {"canvas_id": canvas_id, "text": "What do the tails show?"}
    assert (await _post_chat(client, body)).status_code == 200

    for ended in sessions:
        await store.end_session(
            ended,
            ended=datetime.now(UTC),
            api_key_id=KB_KEY_ID,
            actor=KB_EMAIL,
        )
    browser()
    assert (await _partner(client, canvas_id=canvas_id))["status"] == "unavailable"
    assert (await _post_chat(client, body)).status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_receipts_remain_bounded_without_losing_recent_replay(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Pruning old receipts preserves a new operation's retry result."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_canvas_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/canvases")
    assert created.status_code == 200
    canvas_id = uuid.UUID(
        from_plain(
            json_loads(created.content, dict[str, object])["id"],
            str,
        ),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET revision = 15 WHERE id = $1",
            canvas_id,
        )
        for index in range(80):
            await conn.execute(
                "INSERT INTO visual_canvas_operations "
                "(canvas_id, key, request_hash, response) "
                "VALUES ($1, $2, $3, '{}'::jsonb)",
                canvas_id,
                uuid.uuid4(),
                str(index),
            )
    key = str(uuid.uuid4())
    operation = {
        "revision": 15,
        "operation": {
            "kind": "show",
            "visual_type": "trax.subgraph",
            "record_id": str(uuid.uuid4()),
        },
    }
    path = f"/api/canvases/{canvas_id}/operations"
    shown = await client.post(path, json=operation, headers={"Idempotency-Key": key})
    assert shown.status_code == 200
    async with store.engine.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_canvas_operations WHERE canvas_id = $1",
            canvas_id,
        )
    assert from_plain(count, int) <= 64
    replay = await client.post(path, json=operation, headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert json.loads(replay.content) == json.loads(shown.content)


_VIEWER_EMAIL: Final = "viewer@example.com"


@pytest.fixture
def viewer_agent_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with the viewer's own agent as the assistant, as on a server of one's own."""
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(
        app.state,
        "config",
        replace(
            Config(),
            assistant=Assistant(actor="viewer-agent", email=_VIEWER_EMAIL),
        ),
        raising=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Report:
    """A published report, and the viewer whose agent serves a Chat about it."""

    issue_id: uuid.UUID
    artifact_id: str
    viewer_id: uuid.UUID
    viewer: Agent
    session_id: uuid.UUID
    """The viewer's agent's session, which has polled."""


def _viewer_agent(report: _Report, *, key_id: uuid.UUID | None = None) -> None:
    """Act as the viewer's agent holding ``key_id``, its own key by default."""
    install_identity(
        make_test_identity(
            user_id=report.viewer_id,
            api_key_id=key_id or report.viewer.key_id,
            email=_VIEWER_EMAIL,
            role="viewer",
        ),
    )


def _viewer_browser(report: _Report) -> None:
    """Act as the viewer in a browser."""
    browser(report.viewer_id, email=_VIEWER_EMAIL)


async def _publisher_and_viewer(
    conn: Conn,
) -> tuple[uuid.UUID, Agent]:
    """Create the publisher and the viewer, who holds a key; return both."""
    viewer_id, viewer_key = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) VALUES "
        "($1, 'test-user@example.com', 'Publisher', 'writer', 'active'), "
        "($2, $3, 'Viewer', 'writer', 'active')",
        TEST_USER_ID,
        viewer_id,
        _VIEWER_EMAIL,
    )
    await conn.execute(
        "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
        "VALUES ($1, $2, 'viewer-agent', 'test-hash', 'trax_view', 'viewer')",
        viewer_key,
        viewer_id,
    )
    return viewer_id, Agent(key_id=viewer_key, email=_VIEWER_EMAIL)


async def _publish(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    payload: dict[str, object],
    viewer_id: uuid.UUID,
    viewer: Agent,
) -> _Report:
    """Publish ``payload`` as the publisher; start the viewer's agent's session."""
    install_identity(make_test_identity(api_key_id=None))
    published = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert published.status_code == 201, published.text
    return _Report(
        issue_id=uuid.UUID(from_plain(payload["issue_id"], str)),
        artifact_id=from_plain(
            json_loads(published.content, dict[str, object])["artifact_id"],
            str,
        ),
        viewer_id=viewer_id,
        viewer=viewer,
        session_id=await seed_session(
            store,
            viewer,
            actor="viewer-agent",
            account=_VIEWER_EMAIL,
        ),
    )


async def _publish_page(client: httpx2.AsyncClient, store: Store) -> _Report:
    """Publish a page of HTML on an Issue, for a test whose subject is not evidence."""
    issue_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        viewer_id, viewer = await _publisher_and_viewer(conn)
        await insert_inquiry(
            conn,
            issue_id,
            "Issue",
            values={"title": "Report owner", "account": "test-user@example.com"},
        )
    return await _publish(
        client,
        store,
        payload={
            "issue_id": str(issue_id),
            "title": "Evidence atlas",
            "summary": "Frozen result",
            "format": "html",
            "html": "<p>report</p>",
        },
        viewer_id=viewer_id,
        viewer=viewer,
    )


async def _show(
    client: httpx2.AsyncClient,
    *,
    canvas_id: str,
    revision: int,
    visual_type: str,
    record_id: str,
) -> httpx2.Response:
    """Show ``visual_type`` about ``record_id`` as the installed browser."""
    return await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": revision,
            "operation": {
                "kind": "show",
                "visual_type": visual_type,
                "record_id": record_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


async def _report_canvas(client: httpx2.AsyncClient, report: _Report) -> str:
    """Open the viewer's canvas as a browser; return its id."""
    _viewer_browser(report)
    created = await client.post("/api/canvases")
    assert created.status_code == 200, created.text
    return from_plain(
        json_loads(created.content, dict[str, object])["id"],
        str,
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("viewer_agent_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_cannot_name_its_own_citations_nor_use_a_second_viewer_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The line cannot name its own citations; a second key of the viewer is refused."""
    client, store = pglite_route_client
    report = await _publish_page(client, store)
    _viewer_agent(report)
    assert (
        await client.get(f"/api/sessions/{report.session_id}/inbound")
    ).status_code == 200
    canvas_id = await _report_canvas(client, report)
    missing = await _show(
        client,
        canvas_id=canvas_id,
        revision=0,
        visual_type="trax.artifact",
        record_id=str(uuid.uuid4()),
    )
    assert missing.status_code == 422
    shown_report = await _show(
        client,
        canvas_id=canvas_id,
        revision=0,
        visual_type="trax.artifact",
        record_id=report.artifact_id,
    )
    assert shown_report.status_code == 200, shown_report.text
    forged = await _post_chat(
        client,
        {
            "canvas_id": canvas_id,
            "text": "What evidence supports this?",
            "citations": [{"record_id": str(uuid.uuid4())}],
        },
    )
    assert forged.status_code == 422
    other_key = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'other-agent', 'test-hash', 'trax_othe', 'viewer')",
            other_key,
            report.viewer_id,
        )
    _viewer_agent(report, key_id=other_key)
    assert (
        await client.get(f"/api/sessions/{report.session_id}/inbound")
    ).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_canvas_uses_the_deployments_configured_catalog(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new canvas opens on the configured default and rejects unlisted visuals."""
    client, store = pglite_route_client
    configured = Canvas.Config(
        visuals=[
            StaticVisual.Config(type="x.notes", title="Notes", description="Notes."),
            StaticVisual.Config(type="x.log", title="Log", description="Log."),
        ],
        default_visual="x.log",
    ).make()
    monkeypatch.setattr(app.state, "visual_catalog", configured, raising=False)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active')",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/canvases")
    assert created.status_code == 200
    initial = json_loads(created.content, dict[str, object])
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(initial["visuals"], list[object])
    ] == ["x.log"]
    shown = await client.post(
        f"/api/canvases/{from_plain(initial['id'], str)}/operations",
        json={
            "revision": 0,
            "operation": {"kind": "show", "visual_type": "trax.chat"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 422
    assert "Unknown visual type" in shown.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_stored_canvas_that_holds_chat_opens_without_it_and_refuses_it_again(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A canvas saved with Chat as a visual reads without it; writes keep it gone."""
    client, store = pglite_route_client
    await seed_accounts(store)
    chat = uuid.uuid4()

    def visual(visual_id: uuid.UUID, kind: str, placement: str) -> dict[str, object]:
        return {
            "id": str(visual_id),
            "type": kind,
            "version": 1,
            "placement": placement,
            "record_id": None,
            "params": {},
        }

    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, revision, state) "
            'VALUES ($1, $2, \'{"name": "unnamed-canvas#1"}\', 4, $3)',
            uuid.uuid4(),
            TEST_USER_ID,
            {
                "visuals": [
                    visual(uuid.uuid4(), "trax.browse", "main"),
                    visual(chat, "trax.chat", "side"),
                ],
                "focused_instance": str(chat),
            },
        )
    browser()
    opened = json_loads(
        (await client.post("/api/canvases")).content,
        dict[str, object],
    )
    assert opened["revision"] == 4
    assert opened["focused_instance"] is None
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(opened["visuals"], list[object])
    ] == ["trax.browse"]

    path = f"/api/canvases/{from_plain(opened['id'], str)}/operations"
    refused = await client.post(
        path,
        json={"revision": 4, "operation": {"kind": "show", "visual_type": "trax.chat"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert refused.status_code == 422
    assert "Unknown visual type 'trax.chat'" in refused.text
    shown = await client.post(
        path,
        json={
            "revision": 4,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": str(uuid.uuid4()),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200, shown.text
    async with store.engine.acquire() as conn:
        stored = from_plain(
            await conn.fetchval(
                "SELECT state FROM visual_canvases WHERE user_id = $1",
                TEST_USER_ID,
            ),
            dict[str, object],
        )
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(stored["visuals"], list[object])
    ] == ["trax.browse", "trax.subgraph"]


@pytest.fixture
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, and a fresh poller table and event hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


async def _state(
    client: httpx2.AsyncClient,
    *,
    canvas_id: uuid.UUID,
) -> dict[str, object]:
    return json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )


def _frames(stream: Subscription) -> list[Frame | None]:
    frames: list[Frame | None] = []
    while not stream.queue.empty():
        frames.append(stream.queue.get_nowait())
    return frames


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvas_talks_to_the_assistant_however_its_session_is_named(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The partner is scout, by its configured name, even as `scout#2`."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_other_agent()
    squatter = await start_session(client, actor=KB_ACTOR)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    act_as_user_agent()
    canvas_id = await open_canvas(client)

    state = await _state(client, canvas_id=canvas_id)
    assert state["assistant"] == KB_ACTOR
    assert from_plain(state["partner"], dict[str, object]) == {
        "session_id": str(kb),
        "actor": KB_ACTOR,
        "cli": "codex",
        "status": "live",
        "provider": "assistant",
        "state": "idle",
        "machine": None,
        "run_name": None,
        "cwd": None,
        "label": KB_ACTOR,
        "refused": "",
        "refused_at": None,
    }
    sent = await send_chat(
        client,
        canvas_id=canvas_id,
        text="hello scout",
    )
    assert sent.status_code == 200
    # The line waits on the assistant's own session until it opens the chat.
    assert json_loads(sent.content, dict[str, object])["session_id"] is None
    act_as_assistant()
    messages = from_plain(
        json_loads((await drain(client, session_id=kb)).content, dict[str, object])[
            "messages"
        ],
        list[object],
    )
    assert from_plain(messages[0], dict[str, object])["text"] == "hello scout"
    assert squatter != kb


@dataclass(frozen=True, slots=True, kw_only=True)
class _Canvases:
    """The browser user's canvas and its context graph, and another user's canvas."""

    canvas_id: uuid.UUID
    graph_id: uuid.UUID
    other_canvas_id: uuid.UUID


async def _two_canvases(client: httpx2.AsyncClient) -> _Canvases:
    """Open the browser user's canvas with a graph, and another user's canvas."""
    canvas_id, graph_id = await _canvas_with_graph(client)
    return _Canvases(
        canvas_id=canvas_id,
        graph_id=graph_id,
        other_canvas_id=await open_canvas(
            client,
            user_id=OTHER_USER_ID,
            email=OTHER_EMAIL,
        ),
    )


async def _focus(
    client: httpx2.AsyncClient,
    canvas: uuid.UUID,
    *,
    visual_id: uuid.UUID,
    revision: int,
) -> httpx2.Response:
    """Focus a visual on ``canvas`` as the installed identity."""
    return await client.post(
        f"/api/canvases/{canvas}/operations",
        json={
            "revision": revision,
            "operation": {"kind": "focus", "instance_id": str(visual_id)},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_key_gets_no_canvas_of_a_user_whose_line_it_has_not_opened(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A line posted is not yet a chat the assistant holds."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvases = await _two_canvases(client)

    act_as_assistant()
    assert (await client.get(f"/api/canvases/{canvases.canvas_id}")).status_code == 404
    assert (
        await _focus(
            client,
            canvases.canvas_id,
            visual_id=canvases.graph_id,
            revision=1,
        )
    ).status_code == 404
    browser()
    sent = await send_chat(
        client,
        canvas_id=canvases.canvas_id,
        text="show me something",
    )
    assert sent.status_code == 200
    act_as_assistant()
    assert (await client.get(f"/api/canvases/{canvases.canvas_id}")).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_key_operates_a_canvas_only_of_a_poster_in_a_chat_it_has_open(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The assistant gets nothing from a user who never posted in a chat it holds."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvases = await _two_canvases(client)
    conversation = uuid.uuid4()
    browser()
    sent = await send_chat(
        client,
        canvas_id=canvases.canvas_id,
        text="show me something",
        conversation_id=conversation,
    )
    assert sent.status_code == 200
    second_canvas = await open_canvas(client, new=True)
    await seed_science_chat(store, conversation_id=conversation)

    act_as_assistant()
    assert (await client.get(f"/api/canvases/{canvases.canvas_id}")).status_code == 200
    assert (
        await _focus(
            client,
            canvases.canvas_id,
            visual_id=canvases.graph_id,
            revision=1,
        )
    ).status_code == 200
    # A canvas without a chat takes no agent, whoever posted in the user's others.
    assert (await client.get(f"/api/canvases/{second_canvas}")).status_code == 404
    assert (
        await client.get(f"/api/canvases/{canvases.other_canvas_id}")
    ).status_code == 404
    assert (
        await _focus(
            client,
            canvases.other_canvas_id,
            visual_id=canvases.graph_id,
            revision=0,
        )
    ).status_code == 404
    act_as_other_agent()
    assert (await client.get(f"/api/canvases/{canvases.canvas_id}")).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_navigate_moves_the_page_without_changing_the_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """An agent's navigate pushes one frame, keeps the revision, and replays silently."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id, graph_id = await _canvas_with_graph(client)
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="take me to the graph",
    )
    revision = await revision_of(client, canvas_id=canvas_id)
    path = f"/api/canvases/{canvas_id}/operations"
    key = uuid.uuid4()
    body = {"revision": 0, "operation": {"kind": "navigate", "route": "#/graph"}}

    with assistant_served.subscribe(canvas_id) as stream:
        act_as_assistant()
        first = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert first.status_code == 200
        assert json_loads(first.content, dict[str, object])["revision"] == revision
        replay = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert replay.status_code == 200
        frames = _frames(stream)
    assert [(f.type, f.route) for f in frames if isinstance(f, NavigateFrame)] == [
        ("navigate", "#/graph"),
    ]
    assert len(frames) == 1
    browser()
    assert await revision_of(client, canvas_id=canvas_id) == revision

    different = await client.post(
        path,
        json={**body, "operation": {"kind": "navigate", "route": "#/activity"}},
        headers={"Idempotency-Key": str(key)},
    )
    assert different.status_code == 409

    # A retry after the canvas moved on returns the canvas as it is now, silently.
    act_as_user_agent()
    browser()
    placed = await client.post(
        path,
        json={
            "revision": revision,
            "operation": {
                "kind": "place",
                "instance_id": str(graph_id),
                "placement": "main",
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert placed.status_code == 200
    with assistant_served.subscribe(canvas_id) as stream:
        act_as_assistant()
        later = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert later.status_code == 200
        assert json_loads(later.content, dict[str, object])["revision"] == revision + 1
        assert _frames(stream) == []
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT response FROM visual_canvas_operations "
            "WHERE canvas_id = $1 AND key = $2",
            canvas_id,
            key,
        )
    assert stored in ({}, "{}")


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_an_agents_record_takes_the_lone_pages_place_and_a_browsers_keeps_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent's record at main replaces the lone page; a browser's keeps it."""
    client, store = pglite_route_client
    visuals = Canvas.Config()
    offer_record_visual(visuals)
    monkeypatch.setattr(app.state, "visual_catalog", visuals.make(), raising=False)
    await seed_accounts(store)
    await seed_session(store, agent=ASSISTANT, actor=KB_ACTOR)
    by_agent = await open_canvas(client)
    await seed_converse(
        client,
        store=store,
        canvas_id=by_agent,
        text="show me the issue",
    )
    by_browser = await open_canvas(client, new=True)
    standing: list[list[tuple[str, str]]] = []

    for canvas_id, act in ((by_agent, act_as_assistant), (by_browser, browser)):
        browser()
        revision = await revision_of(client, canvas_id=canvas_id)
        act()
        shown = await client.post(
            f"/api/canvases/{canvas_id}/operations",
            json={
                "revision": revision,
                "operation": {
                    "kind": "show",
                    "visual_type": RECORD_VISUAL,
                    "record_id": str(uuid.uuid4()),
                    "placement": "main",
                    "params": {"kind": "Issue"},
                },
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert shown.status_code == 200, shown.text
        state = json_loads(shown.content, dict[str, object])
        standing.append(
            [
                (str(visual["type"]), str(visual["placement"]))
                for visual in from_plain(state["visuals"], list[dict[str, object]])
            ],
        )

    assert standing == [
        [(RECORD_VISUAL, "main")],
        [("trax.browse", "main"), (RECORD_VISUAL, "main")],
    ]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_an_agent_may_navigate_and_the_route_must_be_clean(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser changes the hash itself; a route with a space is refused."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="hi",
    )
    path = f"/api/canvases/{canvas_id}/operations"

    def navigate(route: str) -> dict[str, object]:
        return {"revision": 0, "operation": {"kind": "navigate", "route": route}}

    browser()
    from_browser = await client.post(
        path,
        json=navigate("#/graph"),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert from_browser.status_code == 422
    act_as_assistant()
    for route in ("#/a b", "graph", "#/" + "x" * 511, "#/a\u0007"):
        refused = await client.post(
            path,
            json=navigate(route),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert refused.status_code == 422


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_highlight_points_at_inquiries_without_changing_the_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """An agent's highlight pushes one frame, keeps the revision, and replays silently."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="hi",
    )
    revision = await revision_of(client, canvas_id=canvas_id)
    path = f"/api/canvases/{canvas_id}/operations"
    ids = [uuid.uuid4(), uuid.uuid4()]
    key = uuid.uuid4()
    body = {
        "revision": 0,
        "operation": {"kind": "highlight", "ids": [str(i) for i in ids]},
    }

    with assistant_served.subscribe(canvas_id) as stream:
        act_as_assistant()
        first = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert first.status_code == 200
        assert json_loads(first.content, dict[str, object])["revision"] == revision
        replay = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert replay.status_code == 200
        frames = _frames(stream)
        cleared = await client.post(
            path,
            json={"revision": 0, "operation": {"kind": "highlight", "ids": []}},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert cleared.status_code == 200
        after = _frames(stream)
    assert [(f.type, f.ids) for f in frames if isinstance(f, HighlightFrame)] == [
        ("highlight", ids),
    ]
    assert len(frames) == 1
    assert [f.ids for f in after if isinstance(f, HighlightFrame)] == [[]]
    browser()
    assert await revision_of(client, canvas_id=canvas_id) == revision
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT response FROM visual_canvas_operations "
            "WHERE canvas_id = $1 AND key = $2",
            canvas_id,
            key,
        )
    assert stored in ({}, "{}")


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_only_an_agent_may_highlight_and_ids_must_be_uuids(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser cannot highlight; more than 50 ids or a non-UUID is refused."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="hi",
    )
    path = f"/api/canvases/{canvas_id}/operations"

    def highlight(ids: list[str]) -> dict[str, object]:
        return {"revision": 0, "operation": {"kind": "highlight", "ids": ids}}

    browser()
    from_browser = await client.post(
        path,
        json=highlight([str(uuid.uuid4())]),
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert from_browser.status_code == 422
    act_as_assistant()
    for ids in (["Issue#1"], [str(uuid.uuid4()) for _ in range(51)]):
        refused = await client.post(
            path,
            json=highlight(ids),
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert refused.status_code == 422


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_replayed_operation_publishes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """The first application pushes the canvas; its retry returns it silently."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id, graph_id = await _canvas_with_graph(client)
    body = {
        "revision": await revision_of(client, canvas_id=canvas_id),
        "operation": {
            "kind": "place",
            "instance_id": str(graph_id),
            "placement": "main",
        },
    }
    key = str(uuid.uuid4())
    path = f"/api/canvases/{canvas_id}/operations"
    with assistant_served.subscribe(canvas_id) as stream:
        first = await client.post(path, json=body, headers={"Idempotency-Key": key})
        replay = await client.post(path, json=body, headers={"Idempotency-Key": key})
        frames = _frames(stream)
    assert (first.status_code, replay.status_code) == (200, 200)
    assert json_loads(first.content) == json_loads(replay.content)
    assert [f.type for f in frames if f is not None] == ["canvas"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_show_names_a_record_its_visual_can_draw(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The catalog's kinds are enforced: browse takes none, artifact only Artifacts."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    kinds = {name: uuid.uuid4() for name in ("Issue", "Experiment", "Paper", "Belief")}
    async with store.engine.acquire() as conn:
        for seq, (kind, record_id) in enumerate(kinds.items(), start=1):
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title) "
                "VALUES ($1, $2, $3, 'active', 'test-user@example.com', 'T')",
                record_id,
                kind,
                700_000 + seq,
            )

    async def show(visual: str, *, record: uuid.UUID | None) -> int:
        response = await client.post(
            f"/api/canvases/{canvas_id}/operations",
            json={
                "revision": await revision_of(client, canvas_id=canvas_id),
                "operation": {
                    "kind": "show",
                    "visual_type": visual,
                    "record_id": None if record is None else str(record),
                },
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        return response.status_code

    assert await show("trax.timeline", record=kinds["Issue"]) == 200
    assert await show("trax.timeline", record=kinds["Experiment"]) == 200
    assert await show("trax.timeline", record=kinds["Paper"]) == 200
    assert await show("trax.timeline", record=kinds["Belief"]) == 200
    assert await show("trax.artifact", record=kinds["Issue"]) == 422
    assert await show("trax.subgraph", record=kinds["Paper"]) == 200
    assert await show("trax.browse", record=kinds["Issue"]) == 422
    assert await show("trax.browse", record=None) == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_page_can_be_hidden_like_another_visual(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Browse is the default visual a canvas opens with, and closes like the rest."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id, _ = await _canvas_with_graph(client)
    visuals = [
        from_plain(v, dict[str, object])
        for v in from_plain(
            (await _state(client, canvas_id=canvas_id))["visuals"],
            list[object],
        )
    ]
    ids = {from_plain(v["type"], str): from_plain(v["id"], str) for v in visuals}

    async def hide(visual: str) -> int:
        response = await client.post(
            f"/api/canvases/{canvas_id}/operations",
            json={
                "revision": await revision_of(client, canvas_id=canvas_id),
                "operation": {"kind": "hide", "instance_id": ids[visual]},
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        return response.status_code

    assert await hide("trax.browse") == 200
    assert await hide("trax.subgraph") == 200


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_navigation_with_no_page_shows_it_in_the_same_revision(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """An agent's navigate never lands nowhere: the page returns, then the route."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id, _ = await _canvas_with_graph(client)
    page = next(
        from_plain(v["id"], str)
        for v in from_plain(
            (await _state(client, canvas_id=canvas_id))["visuals"],
            list[dict[str, object]],
        )
        if v["type"] == "trax.browse"
    )
    closed = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": await revision_of(client, canvas_id=canvas_id),
            "operation": {"kind": "hide", "instance_id": page},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert closed.status_code == 200, closed.text
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="go to the graph",
    )
    revision = await revision_of(client, canvas_id=canvas_id)
    path = f"/api/canvases/{canvas_id}/operations"
    body = {"revision": 0, "operation": {"kind": "navigate", "route": "#/graph"}}
    key = uuid.uuid4()

    with assistant_served.subscribe(canvas_id) as stream:
        act_as_assistant()
        moved = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert moved.status_code == 200, moved.text
        replay = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert replay.status_code == 200
        frames = _frames(stream)
    state = json_loads(moved.content, dict[str, object])
    assert state["revision"] == revision + 1
    assert [
        (v["type"], v["placement"])
        for v in from_plain(state["visuals"], list[dict[str, object]])
    ] == [("trax.subgraph", "main"), ("trax.browse", "main")]
    assert [f.type for f in frames if f is not None] == ["canvas", "navigate"]
    browser()
    assert await revision_of(client, canvas_id=canvas_id) == revision + 1


# A keepalive is also when the stream asks whether its owner may still use the app, and
# re-reads the canvas; a test that does not wait for either asks for a long one.
async def _stream(
    canvas_id: uuid.UUID,
    *,
    monkeypatch: pytest.MonkeyPatch,
    keepalive_sec: float = 0.05,
) -> AsyncIterator[str | bytes | memoryview]:
    """Open the events route as the installed browser, with a short keepalive."""
    monkeypatch.setattr(
        canvas_routes,
        "iter_canvas_events",
        partial(iter_canvas_events, keepalive_sec=keepalive_sec),
    )
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
    response = await canvas_routes.canvas_events_route(
        canvas_id,
        request,
        make_test_identity(api_key_id=None),
    )
    return aiter(response.body_iterator)


async def _close(stream: AsyncIterator[str | bytes | memoryview]) -> None:
    """Stop reading an event stream, and let it release what it holds."""
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()


async def _next_frame(
    stream: AsyncIterator[str | bytes | memoryview],
) -> dict[str, object]:
    """Read the next data frame of an event stream, skipping its comments."""
    while True:
        chunk = await asyncio.wait_for(anext(stream), 5)
        text = chunk if isinstance(chunk, str) else bytes(chunk).decode()
        if text.startswith("data: "):
            return json_loads(text.removeprefix("data: "), dict[str, object])


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_events_route_streams_the_canvas_and_what_happens_to_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the route itself: open, the canvas, a page move; no frame is Chat's own."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    stream = await _stream(canvas_id, monkeypatch=monkeypatch, keepalive_sec=25.0)
    assert await anext(stream) == b": open\n\n"
    opened = await _next_frame(stream)
    assert opened["type"] == "canvas"
    assert from_plain(opened["state"], dict[str, object])["assistant"] == KB_ACTOR

    sent = await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="hello",
    )
    assert sent
    act_as_assistant()
    moved = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={"revision": 0, "operation": {"kind": "navigate", "route": "#/graph"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert moved.status_code == 200
    navigate = await _next_frame(stream)
    # The session the assistant opened is a change like any other inquiry's.
    seen = [navigate]
    while navigate["type"] != "navigate":
        navigate = await _next_frame(stream)
        seen.append(navigate)
    assert {frame["type"] for frame in seen} <= {"changed", "canvas", "navigate"}
    assert navigate["route"] == "#/graph"
    assert all(isinstance(f["t"], int) for f in (opened, navigate))
    await _close(stream)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_events_route_tells_the_canvas_when_its_partner_goes_away(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partner ending reaches an open tab as a canvas frame, unprompted."""
    client, store = pglite_route_client
    await seed_accounts(store)
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    stream = await _stream(canvas_id, monkeypatch=monkeypatch, keepalive_sec=25.0)
    await anext(stream)
    opened = await _next_frame(stream)
    assert (
        from_plain(
            from_plain(opened["state"], dict[str, object])["partner"],
            dict[str, object],
        )["status"]
        == "live"
    )
    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    changed = await _next_frame(stream)
    assert changed["type"] == "canvas"
    partner = from_plain(
        from_plain(changed["state"], dict[str, object])["partner"],
        dict[str, object],
    )
    assert partner["status"] == "unavailable"
    assert partner["session_id"] is None
    await _close(stream)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_the_events_stream_ends_when_its_owner_is_disabled(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked account does not keep a stream open."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    stream = await _stream(canvas_id, monkeypatch=monkeypatch)
    await anext(stream)
    await _next_frame(stream)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            TEST_USER_ID,
        )
    rest = [chunk async for chunk in stream]
    texts = [
        chunk if isinstance(chunk, str) else bytes(chunk).decode() for chunk in rest
    ]
    assert all(text.startswith(":") or text == SSE_PING.decode() for text in texts)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_revoked_assistant_key_loses_the_canvas_at_once(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Revocation, not just a closed chat, closes the assistant's access."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    canvas_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        canvas_id=canvas_id,
        text="hello",
    )
    act_as_assistant("writer")
    assert (await client.get(f"/api/canvases/{canvas_id}")).status_code == 200
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            KB_KEY_ID,
        )
    assert (await client.get(f"/api/canvases/{canvas_id}")).status_code == 404


async def _partner(
    client: httpx2.AsyncClient,
    *,
    canvas_id: str,
) -> dict[str, object]:
    """Read the partner the canvas reports as the installed browser."""
    state = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}")).content,
        dict[str, object],
    )
    return from_plain(state["partner"], dict[str, object])


@dataclass(frozen=True, slots=True, kw_only=True)
class _SettingsChat:
    client: httpx2.AsyncClient
    store: Store
    conversation_id: uuid.UUID
    session_id: uuid.UUID
    canvas_id: uuid.UUID


async def _seed_settings_chat(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> _SettingsChat:
    """Prepare a chat with its Scratch and personal settings rows already pinned."""
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    conversation_id = uuid.uuid4()
    session_id = await seed_science_chat(store, conversation_id=conversation_id)
    canvas_id = await _bind_chat_canvas(
        store,
        user_id=TEST_USER_ID,
        conversation_id=conversation_id,
    )
    await open_scratch(
        store.engine,
        store=store,
        user_id=TEST_USER_ID,
        email=TEST_USER_EMAIL,
    )
    await open_personal_settings(store, email=TEST_USER_EMAIL)
    if legacy:
        async with store.engine.acquire() as conn:
            await conn.execute(
                "UPDATE visual_canvases SET metadata = metadata || "
                "jsonb_build_object('campaign', jsonb_build_object('hours', 2)) "
                "WHERE id = $1",
                canvas_id,
            )
    return _SettingsChat(
        client=client,
        store=store,
        conversation_id=conversation_id,
        session_id=session_id,
        canvas_id=canvas_id,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def settings_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> _SettingsChat:
    client, store = pglite_route_client
    return await _seed_settings_chat(
        client,
        store=store,
        monkeypatch=monkeypatch,
        legacy=False,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def legacy_settings_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> _SettingsChat:
    client, store = pglite_route_client
    chat = await _seed_settings_chat(
        client,
        store=store,
        monkeypatch=monkeypatch,
        legacy=True,
    )
    act_as_assistant("writer")
    response = await client.get(
        f"/api/canvases/for-chat/{chat.conversation_id}/settings",
        params={"asker": TEST_USER_EMAIL},
    )
    assert response.status_code == 200
    return chat


@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_owner_writes_campaign_on_the_head_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's route writes the exact campaign to the resolved head session."""
    conn = make_conn()
    engine = FakeEngine(conn)
    conversation_id, session_id, canvas_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    identity = make_test_identity(api_key_id=None)
    body = SessionCampaign(campaign=CampaignSetup(hours=2, scientists=3))
    store = Store(cast("DatabaseEngine", engine), embed=StubEmbedder())
    conn.fetchval.return_value = str(conversation_id)
    head = ChatHead(
        conversation_id=conversation_id,
        session_id=session_id,
        title="Chat",
        account=TEST_USER_EMAIL,
        live=True,
        forks=0,
        forked_from=None,
        forks_on_typing=False,
        audience="open",
        may_hide=True,
        shared_with=[],
        may_share=True,
        may_delete=True,
    )
    request = MagicMock()
    request.app.state.config = ASSISTANT_CONFIG
    campaign_writer = AsyncMock(return_value=True)
    monkeypatch.setattr(
        canvas_routes,
        "engine_of",
        MagicMock(return_value=cast("DatabaseEngine", engine)),
    )
    monkeypatch.setattr(
        canvas_routes,
        "get_assistant",
        MagicMock(return_value=ASSISTANT_CONFIG.assistant),
    )
    monkeypatch.setattr(canvas_routes, "get_store", MagicMock(return_value=store))
    monkeypatch.setattr(canvas_routes, "read_head", AsyncMock(return_value=head))
    monkeypatch.setattr(canvas_routes, "set_session_campaign", campaign_writer)

    stored = await canvas_routes.set_session_settings_route(
        canvas_id=canvas_id,
        body=body,
        request=cast("Request", request),
        identity=identity,
    )

    assert stored == body
    assert campaign_writer.await_args is not None
    assert campaign_writer.await_args.args == (store,)
    assert campaign_writer.await_args.kwargs == {
        "session_id": session_id,
        "email": identity.email,
        "campaign": {"hours": 2.0, "scientists": 3},
    }
    assert conn.fetchval.await_args is not None
    query = from_plain(conn.fetchval.await_args.args[0], str)
    assert "WHERE id = $1 AND user_id = $2" in query
    assert conn.fetchval.await_args.args[1:] == (canvas_id, identity.user_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_another_user_cannot_write_campaign_to_the_owner_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canvas lookup scopes session-settings writes to its owner."""
    conn = make_conn()
    engine = FakeEngine(conn)
    canvas_id = uuid.uuid4()
    identity = make_test_identity(user_id=OTHER_USER_ID, api_key_id=None)
    conn.fetchval.return_value = None
    request = MagicMock()
    request.app.state.config = ASSISTANT_CONFIG
    monkeypatch.setattr(
        canvas_routes,
        "engine_of",
        MagicMock(return_value=cast("DatabaseEngine", engine)),
    )

    with pytest.raises(HTTPException) as error:
        await canvas_routes.set_session_settings_route(
            canvas_id=canvas_id,
            body=SessionCampaign(campaign=CampaignSetup(hours=9)),
            request=cast("Request", request),
            identity=identity,
        )

    assert error.value.status_code == 404
    assert conn.fetchval.await_args is not None
    query = from_plain(conn.fetchval.await_args.args[0], str)
    assert "WHERE id = $1 AND user_id = $2" in query
    assert conn.fetchval.await_args.args[1:] == (canvas_id, OTHER_USER_ID)


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_settings_refs_include_the_head_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route resolves the asker's session and returns the settings refs."""
    request = MagicMock()
    request.app.state.config = ASSISTANT_CONFIG
    identity = make_test_identity(api_key_id=KB_KEY_ID)
    engine = FakeEngine(make_conn())
    store = cast("Store", object())
    conversation_id, session_id = uuid.uuid4(), uuid.uuid4()
    asker = Asker(
        email=TEST_USER_EMAIL,
        role="writer",
        read_audiences=frozenset({"open", "private"}),
    )
    head = ChatHead(
        conversation_id=conversation_id,
        session_id=session_id,
        title="Chat",
        account=TEST_USER_EMAIL,
        live=True,
        forks=0,
        forked_from=None,
        forks_on_typing=False,
        audience="open",
        may_hide=True,
        shared_with=[],
        may_share=True,
        may_delete=True,
    )
    refs = CanvasSettingsRefs(
        workspace_issue_id=uuid.uuid4(),
        session_id=session_id,
        personal_artifact_id=uuid.uuid4(),
    )
    resolve = AsyncMock(return_value=asker)
    head_reader = AsyncMock(return_value=head)
    user_lookup = AsyncMock(return_value=TEST_USER_ID)
    settings_lookup = AsyncMock(return_value=refs)
    monkeypatch.setattr(
        canvas_routes,
        "engine_of",
        MagicMock(return_value=cast("DatabaseEngine", engine)),
    )
    monkeypatch.setattr(canvas_routes, "get_store", MagicMock(return_value=store))
    monkeypatch.setattr(canvas_routes, "resolve_asker", resolve)
    monkeypatch.setattr(canvas_routes, "read_head", head_reader)
    monkeypatch.setattr(canvas_routes, "_user_id_for_email", user_lookup)
    monkeypatch.setattr(canvas_routes, "settings_refs_for_chat", settings_lookup)

    response = await canvas_routes.settings_refs_for_chat_route(
        conversation_id=conversation_id,
        request=cast("Request", request),
        identity=identity,
        asker=TEST_USER_EMAIL,
    )

    assert response == refs
    assert resolve.await_args is not None
    assert resolve.await_args.kwargs == {"identity": identity, "email": TEST_USER_EMAIL}
    assert head_reader.await_args is not None
    assert head_reader.await_args.kwargs["conversation_id"] == conversation_id
    assert user_lookup.await_args is not None
    assert user_lookup.await_args.kwargs == {"email": TEST_USER_EMAIL}
    assert settings_lookup.await_args is not None
    assert settings_lookup.await_args.args == (store,)
    assert settings_lookup.await_args.kwargs == {
        "user_id": TEST_USER_ID,
        "email": TEST_USER_EMAIL,
        "conversation_id": conversation_id,
        "session_id": session_id,
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_settings_refs_for_chat_return_the_pinned_rows(
    settings_chat: _SettingsChat,
) -> None:
    """The store resolves Scratch, the chat session, and personal settings."""
    refs = await settings_refs_for_chat(
        settings_chat.store,
        user_id=TEST_USER_ID,
        email=TEST_USER_EMAIL,
        conversation_id=settings_chat.conversation_id,
        session_id=settings_chat.session_id,
    )

    assert refs.workspace_issue_id is not None
    assert refs.session_id == settings_chat.session_id
    assert refs.personal_artifact_id is not None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_reads_settings_of_a_canvas_holding_a_suggestion(
    settings_chat: _SettingsChat,
) -> None:
    """A stored workspace suggestion comes back with the settings, not as a 500."""
    stored = {
        "title": "Charm ARC1/2",
        "workspace_id": "eca9d638-6a14-4f36-9037-4975511afc2f",
    }
    async with settings_chat.store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('suggestion', $2::jsonb) WHERE id = $1",
            settings_chat.canvas_id,
            stored,
        )
    act_as_assistant("writer")

    response = await settings_chat.client.get(
        f"/api/canvases/for-chat/{settings_chat.conversation_id}/settings",
        params={"asker": TEST_USER_EMAIL},
    )

    assert response.status_code == 200, response.text
    assert json_loads(response.content, dict[str, object])["suggestion"] == stored


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_settings_refs_reject_another_asker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assistant cannot request settings for a different person."""
    request = MagicMock()
    identity = make_test_identity(api_key_id=KB_KEY_ID)
    resolve = AsyncMock(return_value=None)
    monkeypatch.setattr(canvas_routes, "resolve_asker", resolve)

    with pytest.raises(HTTPException) as error:
        await canvas_routes.settings_refs_for_chat_route(
            conversation_id=uuid.uuid4(),
            request=cast("Request", request),
            identity=identity,
            asker=OTHER_EMAIL,
        )

    assert error.value.status_code == 403
    assert resolve.await_args is not None
    assert resolve.await_args.kwargs == {"identity": identity, "email": OTHER_EMAIL}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_canvas_campaign_moves_to_the_head_session_once(
    legacy_settings_chat: _SettingsChat,
) -> None:
    """A legacy campaign moves once and is removed from canvas JSON."""
    client, store = legacy_settings_chat.client, legacy_settings_chat.store
    act_as_assistant("writer")
    route = f"/api/canvases/for-chat/{legacy_settings_chat.conversation_id}/settings"
    response = await client.get(route, params={"asker": TEST_USER_EMAIL})

    assert response.status_code == 200
    async with store.engine.acquire() as conn:
        app_value = await conn.fetchval(
            "SELECT app FROM inquiries WHERE id = $1",
            legacy_settings_chat.session_id,
        )
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            legacy_settings_chat.canvas_id,
        )
    settings = from_plain(
        from_plain(app_value, dict[str, object])["settings"],
        dict[str, object],
    )
    assert settings["campaign"] == {"hours": 2.0}
    assert "campaign" not in from_plain(canvas, dict[str, object])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_browser_gets_one_pinned_personal_settings_artifact(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The settings artifact route returns the same owner-pinned row each time."""
    client, store = pglite_route_client
    await seed_accounts(store)
    browser()

    first = await client.post("/api/me/settings-artifact")
    again = await client.post("/api/me/settings-artifact")

    assert first.status_code == 200
    assert (
        json_loads(first.content, dict[str, object])["artifact_id"]
        == json_loads(
            again.content,
            dict[str, object],
        )["artifact_id"]
    )


async def _bind_chat_canvas(
    store: Store,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a lean owner canvas bound to ``conversation_id``."""
    canvas_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) VALUES ("
            "$1, $2, '{}'::jsonb, jsonb_build_object('name', $3::text, "
            "'conversation_id', $4::text))",
            canvas_id,
            user_id,
            f"canvas-{canvas_id.hex[:6]}",
            str(conversation_id),
        )
    return canvas_id


def _patch_canvas_route_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: PGliteEngine,
    store: Store,
) -> None:
    def require_browser(identity: AuthIdentity) -> None:
        del identity

    def engine_of(request: Request) -> DatabaseEngine:
        del request
        return engine

    def get_store(request: Request) -> Store:
        del request
        return store

    def get_inbound(request: Request) -> InboundQueue:
        del request
        return InboundQueue()

    def get_assistant(request: Request) -> None:
        del request

    monkeypatch.setattr(canvas_routes, "require_browser", require_browser)
    monkeypatch.setattr(canvas_routes, "engine_of", engine_of)
    monkeypatch.setattr(canvas_routes, "get_store", get_store)
    monkeypatch.setattr(canvas_routes, "get_inbound", get_inbound)
    monkeypatch.setattr(canvas_routes, "get_assistant", get_assistant)
    monkeypatch.setattr(canvas_routes, "visual_catalog", _fake_visual_catalog)


def _fake_visual_catalog(request: Request) -> Canvas:
    """Return the default visual catalog for a direct route call."""
    del request
    return default_canvas()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_new_canvas_selects_a_workspace_after_opening_scratch(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    calls: list[tuple[str, object]] = []
    scratch_id, chosen_id = uuid.uuid4(), uuid.uuid4()
    store = Store(pglite_engine, embed=StubEmbedder())
    _patch_canvas_route_dependencies(
        monkeypatch,
        engine=pglite_engine,
        store=store,
    )
    state = CanvasState(
        id=uuid.uuid4(),
        revision=0,
        name="unnamed-canvas#1",
        conversation_id=None,
        page=None,
        visuals=[],
    )

    monkeypatch.setattr(
        canvas_routes,
        "open_scratch",
        partial(_open_scratch_fake, calls=calls, scratch_id=scratch_id),
    )
    monkeypatch.setattr(
        canvas_routes,
        "check_workspace",
        partial(_check_workspace_fake, calls=calls),
    )
    monkeypatch.setattr(
        canvas_routes,
        "new_canvas",
        partial(_new_canvas_fake, calls=calls, state=state),
    )

    made = await canvas_routes.new_canvas_route(
        request=cast("Request", MagicMock()),
        identity=make_test_identity(api_key_id=None),
        body=NewCanvas(workspace_id=chosen_id, provider="codex"),
    )

    assert made == state
    assert calls == [
        ("scratch", None),
        ("check", None),
        ("new", chosen_id),
        ("provider", "codex"),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_new_canvas_defaults_to_the_scratch_workspace(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    calls: list[tuple[str, object]] = []
    scratch_id = uuid.uuid4()
    store = Store(pglite_engine, embed=StubEmbedder())
    _patch_canvas_route_dependencies(
        monkeypatch,
        engine=pglite_engine,
        store=store,
    )

    state = CanvasState(
        id=uuid.uuid4(),
        revision=0,
        name="unnamed-canvas#1",
        conversation_id=None,
        page=None,
        visuals=[],
    )

    monkeypatch.setattr(
        canvas_routes,
        "open_scratch",
        partial(_open_scratch_fake, calls=[], scratch_id=scratch_id),
    )
    monkeypatch.setattr(
        canvas_routes,
        "new_canvas",
        partial(_new_canvas_fake, calls=calls, state=state),
    )

    await canvas_routes.new_canvas_route(
        request=cast("Request", MagicMock()),
        identity=make_test_identity(api_key_id=None),
    )

    assert calls == [("new", scratch_id)]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_moving_a_canvas_to_an_unknown_workspace_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
    pglite_engine: PGliteEngine,
) -> None:
    store = Store(pglite_engine, embed=StubEmbedder())
    _patch_canvas_route_dependencies(
        monkeypatch,
        engine=pglite_engine,
        store=store,
    )

    async def missing(
        engine: DatabaseEngine,
        *,
        user_id: uuid.UUID,
        email: str,
        canvas_id: uuid.UUID,
        workspace_id: uuid.UUID,
        inbound: InboundQueue,
        assistant: Assistant | None,
    ) -> CanvasState | None:
        del engine, user_id, email, canvas_id, workspace_id, inbound, assistant
        return None

    monkeypatch.setattr(canvas_routes, "set_canvas_workspace", missing)
    with pytest.raises(HTTPException) as error:
        await canvas_routes.set_canvas_workspace_route(
            canvas_id=uuid.uuid4(),
            body=SetCanvasWorkspace(workspace_id=uuid.uuid4()),
            request=cast("Request", MagicMock()),
            identity=make_test_identity(api_key_id=None),
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_name_route_forwards_suggestion_to_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = uuid.uuid4()
    canvas_id = uuid.uuid4()
    request = cast("Request", MagicMock())
    identity = make_test_identity(api_key_id=uuid.uuid4())
    state = CanvasState(
        id=canvas_id,
        revision=0,
        name="Chosen by the user",
        conversation_id=conversation_id,
        page=None,
        visuals=[],
    )
    hub = MagicMock()

    async def resolve(
        request: Request,
        identity: AuthIdentity,
        *,
        email: str | None,
    ) -> Asker | None:
        del request, identity
        assert email == TEST_USER_EMAIL
        return Asker(email=TEST_USER_EMAIL, role="writer", read_audiences=frozenset())

    sent: dict[str, object] = {}

    def engine_for(request: Request) -> DatabaseEngine:
        del request
        return cast("DatabaseEngine", object())

    def inbound_for(request: Request) -> InboundQueue:
        del request
        return InboundQueue()

    def assistant_for(request: Request) -> Assistant | None:
        del request
        return None

    def hub_for(request: Request) -> ChatHub:
        del request
        return cast("ChatHub", hub)

    monkeypatch.setattr(canvas_routes, "resolve_asker", resolve)
    monkeypatch.setattr(canvas_routes, "engine_of", engine_for)
    monkeypatch.setattr(canvas_routes, "get_inbound", inbound_for)
    monkeypatch.setattr(canvas_routes, "get_assistant", assistant_for)
    monkeypatch.setattr(canvas_routes, "get_hub", hub_for)
    monkeypatch.setattr(
        canvas_routes,
        "name_unnamed_canvas",
        partial(_name_canvas_fake, sent=sent, state=state),
    )

    _ = await canvas_routes.name_for_chat_route(
        conversation_id=conversation_id,
        body=NameChat(name="Assistant's idea"),
        request=request,
        identity=identity,
        asker=TEST_USER_EMAIL,
    )

    assert sent == {
        "email": TEST_USER_EMAIL,
        "conversation_id": conversation_id,
        "name": "Assistant's idea",
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_suggestion_updates_canvas_without_creating_an_issue(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation_id = uuid.uuid4()
    canvas_id = await _bind_canvas(
        store,
        user_id=TEST_USER_ID,
        conversation_id=conversation_id,
        name="Current name",
    )
    async with store.engine.acquire() as conn:
        issue_ids_before = {
            from_plain(row["id"], uuid.UUID)
            for row in await conn.fetch("SELECT id FROM inquiries WHERE kind = 'Issue'")
        }
    act_as_assistant("writer")

    response = await client.post(
        f"/api/canvases/for-chat/{conversation_id}/suggestion",
        params={"asker": TEST_USER_EMAIL},
        json={"title": "Possible group"},
    )

    assert response.status_code == 200, response.text
    result = json_loads(response.content, dict[str, object])
    assert result["suggestion"] == {
        "workspace_id": None,
        "title": "Possible group",
    }
    assert (await _canvas_json(store, canvas_id))["suggestion"] == result["suggestion"]
    async with store.engine.acquire() as conn:
        issue_ids_after = {
            from_plain(row["id"], uuid.UUID)
            for row in await conn.fetch("SELECT id FROM inquiries WHERE kind = 'Issue'")
        }
    assert issue_ids_after == issue_ids_before
    _ = assistant_served


async def _bind_canvas(
    store: Store,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Insert a canvas bound to a chat and return its id."""
    canvas_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, state, metadata) VALUES ("
            '$1, $2, \'{"visuals": [], "focused_instance": null}\'::jsonb, '
            "jsonb_build_object('name', $3::text, 'conversation_id', $4::text))",
            canvas_id,
            user_id,
            name,
            str(conversation_id),
        )
    return canvas_id


async def _canvas_json(store: Store, canvas_id: uuid.UUID) -> dict[str, object]:
    """Read a canvas JSON object for a route assertion."""
    async with store.engine.acquire() as conn:
        canvas = await conn.fetchval(
            "SELECT metadata FROM visual_canvases WHERE id = $1",
            canvas_id,
        )
    return from_plain(canvas, dict[str, object])


async def _new_canvas(
    client: httpx2.AsyncClient,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    """Make a canvas as the browser user; return its state."""
    browser()
    made = await client.post("/api/canvases/new", json=body)
    assert made.status_code == 200, made.text
    return json_loads(made.content, dict[str, object])


async def _chat_sessions(store: Store, *, conversation_id: object) -> list[object]:
    """Return the ids of the sessions under a chat's id."""
    async with store.engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM inquiries WHERE agentsession_cli_session_id = $1",
            f"chat:{conversation_id}",
        )
    return [str(row["id"]) for row in rows]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_new_chat_starts_on_the_provider_named_else_on_the_preference(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A CLI canvas is taken without a machine, with a session to start a run on."""
    client, store = pglite_route_client
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            'UPDATE users SET preferences = \'{"chat_provider": "codex"}\' '
            "WHERE id = $1",
            TEST_USER_ID,
        )

    preferred = await _new_canvas(client)
    named = await _new_canvas(client, {"provider": "assistant"})
    unrunnable = await _new_canvas(client, {"provider": "claude"})

    assert (preferred["provider"], named["provider"], unrunnable["provider"]) == (
        "codex",
        "assistant",
        "claude",
    )
    assert named["conversation_id"] is None
    for state in (preferred, unrunnable):
        partner = from_plain(state["partner"], dict[str, object])
        assert partner["status"] == "offline"
        assert await _chat_sessions(
            store,
            conversation_id=state["conversation_id"],
        ) == [
            partner["session_id"],
        ]
    assert from_plain(unrunnable["reasons"], dict[str, object])["claude"] == (
        NO_MACHINE_REASON.format(cli="claude")
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_choosing_a_cli_needs_a_machine_and_gives_the_chat_its_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The picker always has a session to attach a run to, and a retry makes no other."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)

    async def choose(provider: str) -> httpx2.Response:
        return await client.post(
            f"/api/canvases/{canvas_id}/operations",
            json={
                "revision": await revision_of(client, canvas_id=canvas_id),
                "operation": {"kind": "provider", "provider": provider},
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    refused = await choose("claude")
    assert refused.status_code == 409
    assert from_plain(
        json_loads(refused.content, dict[str, object])["detail"],
        dict[str, object],
    ) == {
        "code": "provider_unavailable",
        "message": NO_MACHINE_REASON.format(cli="claude"),
    }
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('laptop', $1, $1, now(), "
            'gen_random_uuid(), now(), \'{"clis": ["claude"]}\')',
            TEST_USER_EMAIL,
        )

    chosen = await choose("claude")
    again = await choose("claude")

    assert (chosen.status_code, again.status_code) == (200, 200)
    state = json_loads(chosen.content, dict[str, object])
    partner = from_plain(state["partner"], dict[str, object])
    assert (state["provider"], partner["status"], partner["label"]) == (
        "claude",
        "offline",
        "claude: no run",
    )
    assert from_plain(state["available"], dict[str, object])["claude"] is True
    assert await _chat_sessions(store, conversation_id=state["conversation_id"]) == [
        partner["session_id"],
    ]
    assert json_loads(again.content, dict[str, object])["partner"] == state["partner"]
    back = await choose("assistant")
    assert json_loads(back.content, dict[str, object])["provider"] == "assistant"


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_choosing_a_cli_drops_the_assistants_hold_on_the_chat(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The assistant's poll of the chat would read as the run's until its lease ran out."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            canvas_id,
            str(conversation),
        )
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('laptop', $1, $1, now(), "
            'gen_random_uuid(), now(), \'{"clis": ["claude"]}\')',
            TEST_USER_EMAIL,
        )
    held = await seed_session(
        store,
        ASSISTANT,
        actor=f"chat-{conversation.hex[:12]}",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
        labels=[SCIENCE_CHAT_LABEL],
    )

    chosen = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": await revision_of(client, canvas_id=canvas_id),
            "operation": {"kind": "provider", "provider": "claude"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert chosen.status_code == 200, chosen.text
    partner = from_plain(
        json_loads(chosen.content, dict[str, object])["partner"],
        dict[str, object],
    )
    assert (partner["status"], partner["session_id"]) == ("offline", str(held))
    inbound = cast("InboundQueue", app.state.inbound)
    assert not inbound.has_poller(held)


async def _choose_provider(
    client: httpx2.AsyncClient,
    *,
    canvas_id: uuid.UUID,
    provider: str,
) -> httpx2.Response:
    """Choose ``provider`` for the canvas as the installed browser."""
    return await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": await revision_of(client, canvas_id=canvas_id),
            "operation": {"kind": "provider", "provider": provider},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_choosing_another_provider_ends_the_chats_run_of_the_one_left(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """One run of a chat is live at a time: a switch stops the old run first."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            canvas_id,
            str(conversation),
        )
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('laptop', $1, $1, now(), "
            'gen_random_uuid(), now(), \'{"clis": ["claude", "codex"]}\')',
            TEST_USER_EMAIL,
        )
    chat = await seed_session(
        store,
        USER_AGENT,
        actor="chat-1",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        polled=False,
    )
    inbound = cast("InboundQueue", app.state.inbound)
    hub = terminal_hub(app)

    def start(command: TerminalCommand, *, user: str = TEST_USER_EMAIL) -> Terminal:
        return hub.open(
            Opening(
                user=user,
                machine_id=uuid.uuid4(),
                machine="laptop",
                rows=24,
                cols=80,
                canvas=canvas_id,
                command=command,
                attach=chat,
            ),
        )

    async def choose(provider: str) -> httpx2.Response:
        return await _choose_provider(
            client,
            canvas_id=canvas_id,
            provider=provider,
        )

    assert (await choose("claude")).status_code == 200
    claude = start("claude")
    # Another person's terminal on the session is not the owner's run to stop.
    foreign = start("claude", user=OTHER_EMAIL)
    inbound.mark_poller(chat, cli="claude", instance="a" * 32)

    assert (await choose("claude")).status_code == 200
    assert (claude.stopping, inbound.has_poller(chat)) == (False, True)

    switched = await choose("codex")
    assert switched.status_code == 200, switched.text
    assert (claude.stopping, claude.ended) == (True, True)
    assert (foreign.stopping, foreign.ended) == (False, False)
    assert not inbound.has_poller(chat)
    assert (await _partner(client, canvas_id=str(canvas_id)))["status"] == ("offline")

    codex = start("codex")
    inbound.mark_poller(chat, cli="codex", instance="b" * 32)
    live = await _partner(client, canvas_id=str(canvas_id))
    assert (live["status"], live["label"]) == ("live", "codex")
    assert hub.attached_to(chat) == [foreign, codex]

    back = await choose("assistant")
    assert back.status_code == 200, back.text
    assert (codex.stopping, inbound.has_poller(chat)) == (True, False)
    assert hub.attached_to(chat) == [foreign]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_choosing_another_provider_releases_the_asks_of_the_run_left(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The old run is told to stop, so a card it left open can never be answered."""
    client, store = pglite_route_client
    await seed_accounts(store)
    canvas_id = await open_canvas(client)
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text) WHERE id = $1",
            canvas_id,
            str(conversation),
        )
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('laptop', $1, $1, now(), "
            'gen_random_uuid(), now(), \'{"clis": ["codex"]}\')',
            TEST_USER_EMAIL,
        )
    chat = await seed_session(
        store,
        USER_AGENT,
        actor="chat-1",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        polled=False,
    )
    inbound = cast("InboundQueue", app.state.inbound)
    inbound.mark_poller(chat, cli="claude", instance="a" * 32)
    await open_ask(store, chat, ask_id="a1")
    assert inbound.take_gone() == []

    switched = await _choose_provider(
        client,
        canvas_id=canvas_id,
        provider="codex",
    )
    released = await release_gone_runs(store, inbound=inbound, at=datetime.now(UTC))

    assert switched.status_code == 200, switched.text
    assert released == 1
    async with store.engine.acquire() as conn:
        assert await open_ask_ids(conn, session_id=chat) == []


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
