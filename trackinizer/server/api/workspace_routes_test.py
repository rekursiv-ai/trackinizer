"""A canvas workspace persists and rejects stale concurrent operations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Final

import asyncio
import json
import uuid

from starlette.requests import Request

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api import workspace_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_KEY_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    Agent,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    open_canvas,
    open_workspace,
    revision_of,
    seed_accounts,
    seed_converse,
    seed_science_chat,
    seed_session,
    send_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.chat_hub import (
    ChatHub,
    HighlightFrame,
    NavigateFrame,
    WorkspaceFrame,
    iter_workspace_events,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.primitives import insert_edge, insert_inquiry
from trackinizer.server.visuals.catalog import StaticVisual, Workspace


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    import httpx2

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.chat_hub import Frame, Subscription
    from trackinizer.server.store.core import Store
    from trackinizer.types.inquiries import Inquiry


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_operations_are_shared_revisioned_and_idempotent(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Two readers see the same state and a stale write cannot erase it."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )

    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    assert created.status_code == 200
    initial = from_plain(loads(created.content), dict[str, object])
    workspace_id = from_plain(initial["id"], str)
    assert initial["revision"] == 0
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(initial["visuals"], list[object])
    ] == ["trax.browse", "trax.chat"]

    key = str(uuid.uuid4())
    operation = {
        "revision": 0,
        "operation": {"kind": "show", "visual_type": "trax.chat", "placement": "side"},
    }
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert shown.status_code == 200
    updated = from_plain(loads(shown.content), dict[str, object])
    assert updated["revision"] == 1
    assert {
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(updated["visuals"], list[object])
    } == {"trax.browse", "trax.chat"}

    replay = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    assert json.loads(replay.content) == json.loads(shown.content)
    mismatched_replay = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "placement": "main",
            },
        },
        headers={"Idempotency-Key": key},
    )
    assert mismatched_replay.status_code == 409
    mismatch = from_plain(loads(mismatched_replay.content), dict[str, object])
    assert from_plain(mismatch["current"], dict[str, object])["revision"] == 1
    same_dump_different_intent = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "placement": "side",
                "record_id": None,
                "params": {},
            },
        },
        headers={"Idempotency-Key": key},
    )
    assert same_dump_different_intent.status_code == 409
    stale = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=operation,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert stale.status_code == 409
    conflict = from_plain(loads(stale.content), dict[str, object])
    assert from_plain(conflict["current"], dict[str, object])["revision"] == 1

    response = await client.get(f"/api/workspaces/{workspace_id}")
    assert response.status_code == 200
    assert json.loads(response.content) == json.loads(shown.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_can_show_record_resolved_by_a_separate_read_profile(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A canvas stores a record reference even when its read graph is remote."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    workspace_id = from_plain(
        from_plain(loads(created.content), dict[str, object])["id"],
        str,
    )
    record_id = str(uuid.uuid4())
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
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
        from_plain(loads(shown.content), dict[str, object])["visuals"],
        list[object],
    )
    assert from_plain(visuals[-1], dict[str, object])["record_id"] == record_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_can_target_record_in_separate_read_profile(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send a persisted remote record ID without inventing local metadata."""
    client, store = pglite_route_client
    session_id = await _serve_the_assistant(
        client,
        store=store,
        monkeypatch=monkeypatch,
    )
    workspace_id = str(await open_workspace(client))
    remote_record_id = str(uuid.uuid4())
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": remote_record_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    visuals = from_plain(
        from_plain(loads(shown.content), dict[str, object])["visuals"],
        list[object],
    )
    chat_id = from_plain(from_plain(visuals[-1], dict[str, object])["id"], str)
    sent = await client.post(
        "/api/chats",
        json={
            "workspace_id": workspace_id,
            "text": "Put this session in context",
            "chat_instance_id": chat_id,
            "expected_record_id": remote_record_id,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert sent.status_code == 200
    act_as_assistant()
    drained = await drain(client, session_id=session_id)
    messages = from_plain(
        from_plain(loads(drained.content), dict[str, object])["messages"],
        list[object],
    )
    context = from_plain(
        from_plain(messages[0], dict[str, object])["context"],
        dict[str, object],
    )
    assert context["record_id"] == remote_record_id
    assert context["record"] is None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_preference_does_not_gate_owned_canvas(
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
    created = await client.post("/api/workspaces")
    assert created.status_code == 200
    workspace_id = from_plain(
        from_plain(loads(created.content), dict[str, object])["id"],
        str,
    )
    enabled = await client.put("/api/me/visual-workspace", json={"enabled": True})
    assert enabled.status_code == 200
    assert from_plain(loads(enabled.content), dict[str, object])["enabled"] is True
    disabled = await client.put("/api/me/visual-workspace", json={"enabled": False})
    assert disabled.status_code == 200
    still_owned = await client.get(f"/api/workspaces/{workspace_id}")
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
    assert (await client.post("/api/workspaces")).status_code == 403
    workspace_id = await open_workspace(client)
    act_as_user_agent()
    # A live session of the owner's own changes nothing.
    await start_session(client)
    read = await client.get(f"/api/workspaces/{workspace_id}")
    operated = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={"revision": 0, "operation": {"kind": "show", "visual_type": "trax.chat"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    for refused in (read, operated):
        assert refused.status_code == 403, refused.text
        assert "Only the canvas's Chat partner" in refused.text
    act_as_other_agent()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 404


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
    workspace_id = str(await open_workspace(client))
    act_as_assistant()
    started = await client.post(
        "/api/sessions/start",
        json={"cli": "codex", "actor": KB_ACTOR},
    )
    session_id = from_plain(
        from_plain(loads(started.content), dict[str, object])["id"],
        str,
    )
    browser()
    assert (await _partner(client, workspace_id=workspace_id))[
        "status"
    ] == "unavailable"

    act_as_assistant()
    assert (await drain(client, session_id=uuid.UUID(session_id))).status_code == 200
    browser()
    assert (await _partner(client, workspace_id=workspace_id))["status"] == "live"

    now[0] = 145.0
    assert (await _partner(client, workspace_id=workspace_id))[
        "status"
    ] == "unavailable"
    assert (
        await client.post(
            "/api/chats",
            json={
                "workspace_id": workspace_id,
                "text": "Cannot deliver to a dead poller",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409


@dataclass(frozen=True, slots=True, kw_only=True)
class _RecordCanvas:
    """A signed-in user's canvas, and the Experiment a Chat on it is about."""

    workspace_id: str
    record_id: uuid.UUID


async def _record_canvas(client: httpx2.AsyncClient, store: Store) -> _RecordCanvas:
    """Seed the accounts and an Experiment, and open the user's canvas."""
    record_id = uuid.uuid4()
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'Experiment', 987654, 'active', "
            "'test-user@example.com', 'Measured tails')",
            record_id,
        )
    return _RecordCanvas(
        workspace_id=str(await open_workspace(client)),
        record_id=record_id,
    )


async def _show_chat_on_record(
    client: httpx2.AsyncClient,
    canvas: _RecordCanvas,
    *,
    revision: int = 0,
) -> list[object]:
    """Show a Chat about the canvas's Experiment as the browser; return the visuals."""
    shown = await client.post(
        f"/api/workspaces/{canvas.workspace_id}/operations",
        json={
            "revision": revision,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": str(canvas.record_id),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    return from_plain(
        from_plain(loads(shown.content), dict[str, object])["visuals"],
        list[object],
    )


def _last_visual_id(visuals: list[object]) -> str:
    """Name the visual shown last."""
    return from_plain(from_plain(visuals[-1], dict[str, object])["id"], str)


def _chat_body(canvas: _RecordCanvas, *, chat_id: str, text: str) -> dict[str, str]:
    """Build the body of a line typed into the Chat about the Experiment."""
    return {
        "workspace_id": canvas.workspace_id,
        "text": text,
        "chat_instance_id": chat_id,
        "expected_record_id": str(canvas.record_id),
    }


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
        from_plain(loads(drained.content), dict[str, object])["messages"],
        list[dict[str, object]],
    )


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_reaches_the_assistant_with_persisted_canvas_context(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser message carries record details from persisted canvas state."""
    client, store = pglite_route_client
    canvas = await _record_canvas(client, store)
    workspace_id = canvas.workspace_id
    record_id = canvas.record_id
    message_path = "/api/chats"
    assert (
        await client.post(
            message_path,
            json={
                "workspace_id": workspace_id,
                "text": "Before the assistant runs",
                "expected_record_id": None,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    session_id = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    assert (
        await client.post(
            message_path,
            json={
                "workspace_id": workspace_id,
                "text": "Chat instance required",
                "expected_record_id": None,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    visuals = await _show_chat_on_record(client, canvas)
    chat_id = _last_visual_id(visuals)
    path = message_path
    body = _chat_body(canvas, chat_id=chat_id, text="What do the tails show?")
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    sent = await client.post(path, json=body, headers=headers)
    assert sent.status_code == 200
    receipt = from_plain(loads(sent.content), dict[str, object])
    # The assistant has not opened the conversation's session yet, and a new
    # conversation is named by the key that started it.
    assert receipt["session_id"] is None
    assert receipt["conversation_id"] == headers["Idempotency-Key"]
    replay = await client.post(path, json=body, headers=headers)
    assert loads(replay.content) == loads(sent.content)
    live = await _partner(client, workspace_id=workspace_id)
    assert live == {
        "session_id": str(session_id),
        "actor": KB_ACTOR,
        "cli": "codex",
        "status": "live",
        "kind": "shared",
    }
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET owner = NULL WHERE id = $1",
            session_id,
        )
    assert (await _partner(client, workspace_id=workspace_id))[
        "status"
    ] == "unavailable"
    assert (
        await client.post(
            path,
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
    assert context["workspace_id"] == workspace_id
    assert context["record_id"] == str(record_id)
    assert from_plain(context["record"], dict[str, object]) == {
        "id": str(record_id),
        "kind": "Experiment",
        "seq": 987_654,
        "title": "Measured tails",
    }
    visible = from_plain(context["visible_visuals"], list[object])
    assert {
        (
            from_plain(item, dict[str, object])["id"],
            from_plain(item, dict[str, object])["type"],
        )
        for item in visible
    } == {
        (
            from_plain(from_plain(item, dict[str, object])["id"], str),
            from_plain(from_plain(item, dict[str, object])["type"], str),
        )
        for item in visuals
    }


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_browser_line_about_a_long_record_is_bounded_and_a_forged_one_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A replay after the drain is the first receipt; a long title is cut to 512."""
    client, store = pglite_route_client
    canvas = await _record_canvas(client, store)
    session_id = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    visuals = await _show_chat_on_record(client, canvas)
    body = _chat_body(
        canvas,
        chat_id=_last_visual_id(visuals),
        text="What do the tails show?",
    )
    key = str(uuid.uuid4())
    sent = await _post_chat(client, body, key=key)
    assert sent.status_code == 200
    assert len(await _messages(client, session_id=session_id)) == 1

    browser()
    after_drain = await _post_chat(client, body, key=key)
    assert json.loads(after_drain.content) == json.loads(sent.content)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET title = $2 WHERE id = $1",
            canvas.record_id,
            "x" * 20_000,
        )
    bounded = await _post_chat(client, {**body, "text": "x"})
    assert bounded.status_code == 200
    [bounded_message] = await _messages(client, session_id=session_id)
    bounded_context = from_plain(bounded_message["context"], dict[str, object])
    bounded_record = from_plain(bounded_context["record"], dict[str, object])
    assert bounded_record["title"] == "x" * 512
    browser()
    wrong_visual = {
        **body,
        "text": "Wrong visual",
        "chat_instance_id": from_plain(visuals[0], dict[str, object])["id"],
    }
    assert (await _post_chat(client, wrong_visual)).status_code == 422
    assert (
        await _post_chat(client, {**body, "record_title": "forged"})
    ).status_code == 422
    unknown_chat = {**body, "chat_instance_id": str(uuid.uuid4())}
    assert (await _post_chat(client, unknown_chat)).status_code == 422


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_restarted_assistant_answers_from_its_newest_session_until_none_is_live(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A send replays whichever session answers; a retargeted Chat refuses a stale one."""
    client, store = pglite_route_client
    canvas = await _record_canvas(client, store)
    session_id = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    browser()
    visuals = await _show_chat_on_record(client, canvas)
    body = _chat_body(
        canvas,
        chat_id=_last_visual_id(visuals),
        text="What do the tails show?",
    )
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
    assert loads(replayed.content) == loads(sent.content)
    assert (await _post_chat(client, body)).status_code == 200
    assert len(await _messages(client, session_id=second_session_id)) == 1
    browser()
    retargeted = await client.post(
        f"/api/workspaces/{canvas.workspace_id}/operations",
        json={
            "revision": 1,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": None,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert retargeted.status_code == 200
    assert (await _post_chat(client, body)).status_code == 409
    assert await _messages(client, session_id=session_id) == []


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_refuses_a_line_once_every_session_of_the_assistant_has_ended(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An older session does not answer for a newer one that ended."""
    client, store = pglite_route_client
    canvas = await _record_canvas(client, store)
    sessions = [
        await seed_session(store, ASSISTANT, actor=actor)
        for actor in (KB_ACTOR, f"{KB_ACTOR}#2")
    ]
    browser()
    visuals = await _show_chat_on_record(client, canvas)
    body = _chat_body(
        canvas,
        chat_id=_last_visual_id(visuals),
        text="What do the tails show?",
    )
    assert (await _post_chat(client, body)).status_code == 200

    for ended in sessions:
        await store.end_session(
            ended,
            ended=datetime.now(UTC),
            api_key_id=KB_KEY_ID,
            actor=KB_EMAIL,
        )
    browser()
    assert (await _partner(client, workspace_id=canvas.workspace_id))[
        "status"
    ] == "unavailable"
    assert (await _post_chat(client, body)).status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_receipts_remain_bounded_without_losing_recent_replay(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Pruning old receipts preserves a new operation's retry result."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    assert created.status_code == 200
    workspace_id = uuid.UUID(
        from_plain(
            from_plain(loads(created.content), dict[str, object])["id"],
            str,
        ),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET revision = 15 WHERE id = $1",
            workspace_id,
        )
        for index in range(80):
            await conn.execute(
                "INSERT INTO visual_workspace_operations "
                "(workspace_id, key, request_hash, response) "
                "VALUES ($1, $2, $3, '{}'::jsonb)",
                workspace_id,
                uuid.uuid4(),
                str(index),
            )
    key = str(uuid.uuid4())
    operation = {
        "revision": 15,
        "operation": {"kind": "show", "visual_type": "trax.chat"},
    }
    path = f"/api/workspaces/{workspace_id}/operations"
    shown = await client.post(path, json=operation, headers={"Idempotency-Key": key})
    assert shown.status_code == 200
    async with store.engine.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM visual_workspace_operations WHERE workspace_id = $1",
            workspace_id,
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


@dataclass(frozen=True, slots=True, kw_only=True)
class _Evidence:
    """A structured report that cites a Paper's contrary evidence on a Belief."""

    report: _Report
    paper_id: uuid.UUID
    belief_id: uuid.UUID
    payload: dict[str, object]


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
            from_plain(loads(published.content), dict[str, object])["artifact_id"],
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


# The Issue, the Belief, the Paper and the edge are inserted straight into the database:
# the subject here is what a Chat line carries of the report, not how the evidence was
# filed.
async def _publish_evidence(client: httpx2.AsyncClient, store: Store) -> _Evidence:
    """Publish a structured report that cites a Paper's contrary evidence on a Belief."""
    issue_id, belief_id, paper_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        viewer_id, viewer = await _publisher_and_viewer(conn)
        rows: list[tuple[Inquiry.InquiryKind, uuid.UUID, str]] = [
            ("Issue", issue_id, "Report owner"),
            ("Belief", belief_id, "Scaling helps"),
            ("Paper", paper_id, "Measured result"),
        ]
        for kind, row_id, title in rows:
            await insert_inquiry(
                conn,
                row_id,
                kind,
                values={"title": title, "account": "test-user@example.com"},
            )
        await insert_edge(
            conn,
            from_id=paper_id,
            from_kind="Paper",
            to_id=belief_id,
            edge_kind="favors",
            valence=-0.75,
            note="Contrary held-out evidence",
        )
    payload: dict[str, object] = {
        "issue_id": str(issue_id),
        "title": "Evidence atlas",
        "summary": "Frozen result",
        "format": "structured",
        "citations": [{"record_id": str(issue_id)}],
        "sections": [
            {
                "title": "Scaling",
                "summary": "Measured direction",
                "details": "Matched comparison",
                "findings": [
                    {
                        "claim": "Scaling helps",
                        "outcome": {
                            "result": "3 wins",
                            "denominator": 8,
                            "split": "held-out",
                        },
                        "uncertainty": "Small sample",
                        "citations": [
                            {
                                "record_id": str(paper_id),
                                "claim_id": str(belief_id),
                                "edge_kind": "favors",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    return _Evidence(
        report=await _publish(
            client,
            store,
            payload=payload,
            viewer_id=viewer_id,
            viewer=viewer,
        ),
        paper_id=paper_id,
        belief_id=belief_id,
        payload=payload,
    )


async def _show(
    client: httpx2.AsyncClient,
    *,
    workspace_id: str,
    revision: int,
    visual_type: str,
    record_id: str,
) -> httpx2.Response:
    """Show ``visual_type`` about ``record_id`` as the installed browser."""
    return await client.post(
        f"/api/workspaces/{workspace_id}/operations",
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


async def _viewer_messages(
    client: httpx2.AsyncClient,
    report: _Report,
) -> list[dict[str, object]]:
    """Drain the viewer's agent's session as its own key; return what it heard."""
    _viewer_agent(report)
    drained = await client.get(f"/api/sessions/{report.session_id}/inbound")
    assert drained.status_code == 200, drained.text
    return from_plain(
        from_plain(loads(drained.content), dict[str, object])["messages"],
        list[dict[str, object]],
    )


def _frozen_valence(context: dict[str, object]) -> object:
    """Read the valence of the first citation of the first finding of the report."""
    report = from_plain(context["artifact_content"], dict[str, object])
    section = from_plain(report["sections"], list[dict[str, object]])[0]
    finding = from_plain(section["findings"], list[dict[str, object]])[0]
    return from_plain(finding["citations"], list[dict[str, object]])[0]["valence"]


async def _report_canvas(client: httpx2.AsyncClient, report: _Report) -> str:
    """Open the viewer's canvas as a browser; return its id."""
    _viewer_browser(report)
    created = await client.post("/api/workspaces")
    assert created.status_code == 200, created.text
    return from_plain(
        from_plain(loads(created.content), dict[str, object])["id"],
        str,
    )


async def _chat_on_report(
    client: httpx2.AsyncClient,
    report: _Report,
    *,
    workspace_id: str,
    revision: int,
) -> dict[str, str]:
    """Show a Chat about the report; return the body of a line typed into it."""
    shown_chat = await _show(
        client,
        workspace_id=workspace_id,
        revision=revision,
        visual_type="trax.chat",
        record_id=report.artifact_id,
    )
    assert shown_chat.status_code == 200, shown_chat.text
    chat_id = next(
        from_plain(visual["id"], str)
        for visual in from_plain(
            from_plain(loads(shown_chat.content), dict[str, object])["visuals"],
            list[dict[str, object]],
        )
        if visual["type"] == "trax.chat"
    )
    return {
        "workspace_id": workspace_id,
        "text": "What evidence supports this?",
        "chat_instance_id": chat_id,
        "expected_record_id": report.artifact_id,
    }


@pytest.mark.db_pglite
@pytest.mark.usefixtures("viewer_agent_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_writer_chats_about_frozen_report_revision_from_own_workspace(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A writer's message carries only server-read evidence from one revision.

    The writer's own agent is the assistant, as on a server of one's own.
    """
    client, store = pglite_route_client
    report = (await _publish_evidence(client, store)).report
    workspace_id = await _report_canvas(client, report)
    body = await _chat_on_report(
        client,
        report,
        workspace_id=workspace_id,
        revision=0,
    )
    sent = await _post_chat(client, body)
    assert sent.status_code == 200, sent.text
    [message] = await _viewer_messages(client, report)
    context = from_plain(message["context"], dict[str, object])
    assert context["record_id"] == report.artifact_id
    content = from_plain(context["artifact_content"], dict[str, object])
    assert content["revision"] == 1
    assert content["title"] == "Evidence atlas"
    assert from_plain(
        from_plain(content["citations"], list[object])[0],
        dict[str, object],
    )["record_id"] == str(report.issue_id)
    section = from_plain(content["sections"], list[dict[str, object]])[0]
    finding = from_plain(section["findings"], list[dict[str, object]])[0]
    citation = from_plain(finding["citations"], list[dict[str, object]])[0]
    assert citation["valence"] == -0.75
    assert citation["note"] == "Contrary held-out evidence"


@pytest.mark.db_pglite
@pytest.mark.usefixtures("viewer_agent_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_report_chat_refuses_forged_evidence_and_a_key_that_did_not_open_the_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The line cannot name its own citations; a second key of the viewer is refused."""
    client, store = pglite_route_client
    report = await _publish_page(client, store)
    _viewer_agent(report)
    assert (
        await client.get(f"/api/sessions/{report.session_id}/inbound")
    ).status_code == 200
    workspace_id = await _report_canvas(client, report)
    missing = await _show(
        client,
        workspace_id=workspace_id,
        revision=0,
        visual_type="trax.artifact",
        record_id=str(uuid.uuid4()),
    )
    assert missing.status_code == 422
    shown_report = await _show(
        client,
        workspace_id=workspace_id,
        revision=0,
        visual_type="trax.artifact",
        record_id=report.artifact_id,
    )
    assert shown_report.status_code == 200, shown_report.text
    body = await _chat_on_report(
        client,
        report,
        workspace_id=workspace_id,
        revision=1,
    )
    forged = await _post_chat(
        client,
        {**body, "citations": [{"record_id": str(uuid.uuid4())}]},
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
@pytest.mark.usefixtures("viewer_agent_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_keeps_the_evidence_of_its_revision_when_a_newer_one_and_the_graph_change(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A newer revision and a changed live edge do not reach a line about the older."""
    client, store = pglite_route_client
    evidence = await _publish_evidence(client, store)
    report = evidence.report
    install_identity(make_test_identity(api_key_id=None))
    newer = await client.post(
        "/api/artifacts/content",
        json={
            **evidence.payload,
            "previous_artifact_id": report.artifact_id,
            "summary": "Newer result",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert newer.status_code == 201, newer.text
    newer_artifact_id = from_plain(
        from_plain(loads(newer.content), dict[str, object])["artifact_id"],
        str,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE edges SET valence = 0.5, note = 'Changed live edge' "
            "WHERE from_id = $1 AND to_id = $2",
            evidence.paper_id,
            evidence.belief_id,
        )
    workspace_id = await _report_canvas(client, report)
    body = await _chat_on_report(
        client,
        report,
        workspace_id=workspace_id,
        revision=0,
    )
    same_revision = await _post_chat(client, body)
    assert same_revision.status_code == 200, same_revision.text
    [message] = await _viewer_messages(client, report)
    repeated = from_plain(message["context"], dict[str, object])
    repeated_report = from_plain(repeated["artifact_content"], dict[str, object])
    assert repeated_report["summary"] == "Frozen result"
    assert _frozen_valence(repeated) == -0.75
    _viewer_browser(report)
    retargeted = await _show(
        client,
        workspace_id=workspace_id,
        revision=1,
        visual_type="trax.chat",
        record_id=newer_artifact_id,
    )
    assert retargeted.status_code == 200, retargeted.text
    assert (await _post_chat(client, body)).status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_uses_the_deployments_configured_catalog(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new canvas opens on the configured default and rejects unlisted visuals."""
    client, store = pglite_route_client
    configured = Workspace.Config(
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
    created = await client.post("/api/workspaces")
    assert created.status_code == 200
    initial = from_plain(loads(created.content), dict[str, object])
    assert [
        from_plain(item, dict[str, object])["type"]
        for item in from_plain(initial["visuals"], list[object])
    ] == ["x.log"]
    shown = await client.post(
        f"/api/workspaces/{from_plain(initial['id'], str)}/operations",
        json={
            "revision": 0,
            "operation": {"kind": "show", "visual_type": "trax.chat"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 422
    assert "Unknown visual type" in shown.text


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
    workspace_id: uuid.UUID,
) -> dict[str, object]:
    return from_plain(
        loads((await client.get(f"/api/workspaces/{workspace_id}")).content),
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
    workspace_id, chat_id = await open_canvas(client)

    state = await _state(client, workspace_id=workspace_id)
    assert state["assistant"] == KB_ACTOR
    assert from_plain(state["partner"], dict[str, object]) == {
        "session_id": str(kb),
        "actor": KB_ACTOR,
        "cli": "codex",
        "status": "live",
        "kind": "shared",
    }
    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello scout",
    )
    assert sent.status_code == 200
    # The line waits on the assistant's own session until it opens the chat.
    assert from_plain(loads(sent.content), dict[str, object])["session_id"] is None
    act_as_assistant()
    messages = from_plain(
        from_plain(
            loads((await drain(client, session_id=kb)).content),
            dict[str, object],
        )["messages"],
        list[object],
    )
    assert from_plain(messages[0], dict[str, object])["text"] == "hello scout"
    assert squatter != kb


@dataclass(frozen=True, slots=True, kw_only=True)
class _Canvases:
    """The browser user's canvas and Chat, and another user's canvas."""

    workspace_id: uuid.UUID
    chat_id: uuid.UUID
    other_workspace_id: uuid.UUID


async def _two_canvases(client: httpx2.AsyncClient) -> _Canvases:
    """Open the browser user's canvas with a Chat, and another user's canvas."""
    workspace_id, chat_id = await open_canvas(client)
    return _Canvases(
        workspace_id=workspace_id,
        chat_id=chat_id,
        other_workspace_id=await open_workspace(
            client,
            user_id=OTHER_USER_ID,
            email=OTHER_EMAIL,
        ),
    )


async def _focus(
    client: httpx2.AsyncClient,
    workspace: uuid.UUID,
    *,
    chat_id: uuid.UUID,
    revision: int,
) -> httpx2.Response:
    """Focus the Chat on ``workspace`` as the installed identity."""
    return await client.post(
        f"/api/workspaces/{workspace}/operations",
        json={
            "revision": revision,
            "operation": {"kind": "focus", "instance_id": str(chat_id)},
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
    assert (
        await client.get(f"/api/workspaces/{canvases.workspace_id}")
    ).status_code == 404
    assert (
        await _focus(
            client,
            canvases.workspace_id,
            chat_id=canvases.chat_id,
            revision=1,
        )
    ).status_code == 404
    browser()
    sent = await send_chat(
        client,
        workspace_id=canvases.workspace_id,
        chat_id=canvases.chat_id,
        text="show me something",
    )
    assert sent.status_code == 200
    act_as_assistant()
    assert (
        await client.get(f"/api/workspaces/{canvases.workspace_id}")
    ).status_code == 404


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
    second_canvas = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, is_default, state) "
            "VALUES ($1, $2, FALSE, $3)",
            second_canvas,
            TEST_USER_ID,
            {"visuals": []},
        )
    await seed_science_chat(store, conversation_id=uuid.uuid4())

    act_as_assistant()
    assert (
        await client.get(f"/api/workspaces/{canvases.workspace_id}")
    ).status_code == 200
    assert (
        await _focus(
            client,
            canvases.workspace_id,
            chat_id=canvases.chat_id,
            revision=1,
        )
    ).status_code == 200
    assert (await client.get(f"/api/workspaces/{second_canvas}")).status_code == 200
    assert (
        await client.get(f"/api/workspaces/{canvases.other_workspace_id}")
    ).status_code == 404
    assert (
        await _focus(
            client,
            canvases.other_workspace_id,
            chat_id=canvases.chat_id,
            revision=0,
        )
    ).status_code == 404
    act_as_other_agent()
    assert (
        await client.get(f"/api/workspaces/{canvases.workspace_id}")
    ).status_code == 404


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
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="take me to the graph",
    )
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/operations"
    key = uuid.uuid4()
    body = {"revision": 0, "operation": {"kind": "navigate", "route": "#/graph"}}

    with assistant_served.subscribe(workspace_id) as stream:
        act_as_assistant()
        first = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert first.status_code == 200
        assert (
            from_plain(loads(first.content), dict[str, object])["revision"] == revision
        )
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
    assert await revision_of(client, workspace_id=workspace_id) == revision

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
                "instance_id": str(chat_id),
                "placement": "main",
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert placed.status_code == 200
    with assistant_served.subscribe(workspace_id) as stream:
        act_as_assistant()
        later = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert later.status_code == 200
        assert (
            from_plain(loads(later.content), dict[str, object])["revision"]
            == revision + 1
        )
        assert _frames(stream) == []
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            workspace_id,
            key,
        )
    assert stored in ({}, "{}")


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_show_names_what_it_brought_up_only_when_an_agent_made_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    assistant_served: ChatHub,
) -> None:
    """An agent's show says which instance it showed; the owner's own says none."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="show me the page",
    )

    with assistant_served.subscribe(workspace_id) as stream:
        for become in (act_as_assistant, browser):
            become()
            shown = await client.post(
                f"/api/workspaces/{workspace_id}/operations",
                json={
                    "revision": await revision_of(client, workspace_id=workspace_id),
                    "operation": {"kind": "show", "visual_type": "trax.browse"},
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            assert shown.status_code == 200
        frames = [f for f in _frames(stream) if isinstance(f, WorkspaceFrame)]
    by_agent, by_owner = frames
    assert by_agent.shown is not None
    assert by_agent.shown == by_agent.state.focused_instance
    assert by_owner.shown is None


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
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hi",
    )
    path = f"/api/workspaces/{workspace_id}/operations"

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
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hi",
    )
    revision = await revision_of(client, workspace_id=workspace_id)
    path = f"/api/workspaces/{workspace_id}/operations"
    ids = [uuid.uuid4(), uuid.uuid4()]
    key = uuid.uuid4()
    body = {
        "revision": 0,
        "operation": {"kind": "highlight", "ids": [str(i) for i in ids]},
    }

    with assistant_served.subscribe(workspace_id) as stream:
        act_as_assistant()
        first = await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(key)},
        )
        assert first.status_code == 200
        assert (
            from_plain(loads(first.content), dict[str, object])["revision"] == revision
        )
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
    assert await revision_of(client, workspace_id=workspace_id) == revision
    async with store.engine.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT response FROM visual_workspace_operations "
            "WHERE workspace_id = $1 AND key = $2",
            workspace_id,
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
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hi",
    )
    path = f"/api/workspaces/{workspace_id}/operations"

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
    workspace_id, chat_id = await open_canvas(client)
    body = {
        "revision": await revision_of(client, workspace_id=workspace_id),
        "operation": {
            "kind": "place",
            "instance_id": str(chat_id),
            "placement": "main",
        },
    }
    key = str(uuid.uuid4())
    path = f"/api/workspaces/{workspace_id}/operations"
    with assistant_served.subscribe(workspace_id) as stream:
        first = await client.post(path, json=body, headers={"Idempotency-Key": key})
        replay = await client.post(path, json=body, headers={"Idempotency-Key": key})
        frames = _frames(stream)
    assert (first.status_code, replay.status_code) == (200, 200)
    assert loads(first.content) == loads(replay.content)
    assert [f.type for f in frames if f is not None] == ["workspace"]


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_show_names_a_record_its_visual_can_draw(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The catalog's kinds are enforced: browse takes none, artifact only Artifacts."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await open_workspace(client)
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
            f"/api/workspaces/{workspace_id}/operations",
            json={
                "revision": await revision_of(client, workspace_id=workspace_id),
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
async def test_the_page_cannot_be_hidden_but_chat_can(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Browse is the page itself."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await open_workspace(client)
    visuals = [
        from_plain(v, dict[str, object])
        for v in from_plain(
            (await _state(client, workspace_id=workspace_id))["visuals"],
            list[object],
        )
    ]
    ids = {from_plain(v["type"], str): from_plain(v["id"], str) for v in visuals}

    async def hide(visual: str) -> int:
        response = await client.post(
            f"/api/workspaces/{workspace_id}/operations",
            json={
                "revision": await revision_of(client, workspace_id=workspace_id),
                "operation": {"kind": "hide", "instance_id": ids[visual]},
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        return response.status_code

    assert await hide("trax.browse") == 422
    assert await hide("trax.chat") == 200


# A keepalive is also when the stream asks whether its owner may still use the app, and
# re-reads the canvas; a test that does not wait for either asks for a long one.
async def _stream(
    workspace_id: uuid.UUID,
    *,
    monkeypatch: pytest.MonkeyPatch,
    keepalive_sec: float = 0.05,
) -> AsyncIterator[str | bytes | memoryview]:
    """Open the events route as the installed browser, with a short keepalive."""
    monkeypatch.setattr(
        workspace_routes,
        "iter_workspace_events",
        partial(iter_workspace_events, keepalive_sec=keepalive_sec),
    )
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
    response = await workspace_routes.workspace_events_route(
        workspace_id,
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
            return from_plain(loads(text.removeprefix("data: ")), dict[str, object])


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
    workspace_id, chat_id = await open_canvas(client)
    stream = await _stream(workspace_id, monkeypatch=monkeypatch, keepalive_sec=25.0)
    assert await anext(stream) == b": open\n\n"
    opened = await _next_frame(stream)
    assert opened["type"] == "workspace"
    assert from_plain(opened["state"], dict[str, object])["assistant"] == KB_ACTOR

    sent = await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello",
    )
    assert sent
    act_as_assistant()
    moved = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
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
    assert {frame["type"] for frame in seen} <= {"changed", "workspace", "navigate"}
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
    workspace_id = await open_workspace(client)
    stream = await _stream(workspace_id, monkeypatch=monkeypatch, keepalive_sec=25.0)
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
    assert changed["type"] == "workspace"
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
    workspace_id = await open_workspace(client)
    stream = await _stream(workspace_id, monkeypatch=monkeypatch)
    await anext(stream)
    await _next_frame(stream)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            TEST_USER_ID,
        )
    rest = [chunk async for chunk in stream]
    assert all(
        (chunk if isinstance(chunk, str) else bytes(chunk).decode()).startswith(":")
        for chunk in rest
    )


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
    workspace_id, chat_id = await open_canvas(client)
    await seed_converse(
        client,
        store,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello",
    )
    act_as_assistant("writer")
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 200
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            KB_KEY_ID,
        )
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 404


async def _partner(
    client: httpx2.AsyncClient,
    *,
    workspace_id: str,
) -> dict[str, object]:
    """Read the partner the canvas reports as the installed browser."""
    state = from_plain(
        loads((await client.get(f"/api/workspaces/{workspace_id}")).content),
        dict[str, object],
    )
    return from_plain(state["partner"], dict[str, object])


async def _serve_the_assistant(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> uuid.UUID:
    """Configure scout, seed the accounts and start it; return its session."""
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    await seed_accounts(store)
    act_as_assistant()
    session_id = await start_session(client, actor=KB_ACTOR)
    browser()
    return session_id


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
