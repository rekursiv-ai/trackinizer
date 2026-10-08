"""A canvas workspace persists and rejects stale concurrent operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

import asyncio
import json
import uuid

from starlette.requests import Request

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api import workspace_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.chat_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_KEY_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    open_workspace,
    revision_of,
    seed_accounts,
    send_chat,
    show_chat,
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
    iter_workspace_events,
)
from trackinizer.server.config import Assistant, Config
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.catalog import StaticVisual, Workspace
from trackinizer.wire.bodies import (
    SubmitAgentSession,
    SubmitBelief,
    SubmitIssue,
    SubmitPaper,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2

    from trackinizer.server.chat_hub import Frame, Subscription
    from trackinizer.server.store.core import Store


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
        f"/api/workspaces/{workspace_id}/messages",
        json={
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
            f"/api/workspaces/{workspace_id}/messages",
            json={"text": "Cannot deliver to a dead poller"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_reaches_the_assistant_with_persisted_canvas_context(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser message carries record details from persisted canvas state."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    record_id = uuid.uuid4()
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'Experiment', 987654, 'active', "
            "'test-user@example.com', 'Measured tails')",
            record_id,
        )
    workspace_id = str(await open_workspace(client))
    message_path = f"/api/workspaces/{workspace_id}/messages"
    assert (
        await client.post(
            message_path,
            json={"text": "Before the assistant runs", "expected_record_id": None},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    act_as_assistant()
    session_id = await start_session(client, actor=KB_ACTOR)
    browser()
    assert (
        await client.post(
            message_path,
            json={"text": "Chat instance required", "expected_record_id": None},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": str(record_id),
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
    path = message_path
    body = {
        "text": "What do the tails show?",
        "chat_instance_id": chat_id,
        "expected_record_id": str(record_id),
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    sent = await client.post(path, json=body, headers=headers)
    assert sent.status_code == 200
    receipt = from_plain(loads(sent.content), dict[str, object])
    assert receipt["session_id"] == str(session_id)
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

    act_as_assistant()
    drained = await drain(client, session_id=session_id)
    assert drained.status_code == 200
    messages = from_plain(
        from_plain(loads(drained.content), dict[str, object])["messages"],
        list[object],
    )
    assert len(messages) == 1
    message = from_plain(messages[0], dict[str, object])
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

    browser()
    after_drain = await client.post(path, json=body, headers=headers)
    assert json.loads(after_drain.content) == json.loads(sent.content)
    changed_text = await client.post(
        path,
        json={**body, "text": "Changed question"},
        headers=headers,
    )
    assert changed_text.status_code == 409
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET title = $2 WHERE id = $1",
            record_id,
            "x" * 20_000,
        )
    bounded = await client.post(
        path,
        json={
            "text": "x",
            "chat_instance_id": chat_id,
            "expected_record_id": str(record_id),
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert bounded.status_code == 200
    act_as_assistant()
    bounded_drain = await drain(client, session_id=session_id)
    bounded_message = from_plain(
        from_plain(
            from_plain(loads(bounded_drain.content), dict[str, object])["messages"],
            list[object],
        )[0],
        dict[str, object],
    )
    bounded_context = from_plain(bounded_message["context"], dict[str, object])
    bounded_record = from_plain(bounded_context["record"], dict[str, object])
    assert bounded_record["title"] == "x" * 512
    browser()
    assert (
        await client.post(
            path,
            json={
                "text": "Wrong visual",
                "chat_instance_id": from_plain(visuals[0], dict[str, object])["id"],
                "expected_record_id": str(record_id),
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    assert (
        await client.post(
            path,
            json={**body, "record_title": "forged"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    assert (
        await client.post(
            path,
            json={**body, "chat_instance_id": str(uuid.uuid4())},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    # The assistant restarts: its newest live session answers from now on.
    act_as_assistant()
    second_session_id = await start_session(client, actor=KB_ACTOR)
    browser()
    # The same send is a replay of the first, whichever session now answers.
    replayed = await client.post(path, json=body, headers=headers)
    assert replayed.status_code == 200
    assert from_plain(loads(replayed.content), dict[str, object])["session_id"] == str(
        session_id,
    )
    assert (
        await client.post(path, json={**body, "text": "Another"}, headers=headers)
    ).status_code == 409
    second_send = await client.post(
        path,
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert from_plain(loads(second_send.content), dict[str, object])[
        "session_id"
    ] == str(
        second_session_id,
    )
    act_as_assistant()
    second_drain = await drain(client, session_id=second_session_id)
    assert (
        len(
            from_plain(
                from_plain(loads(second_drain.content), dict[str, object])["messages"],
                list[object],
            ),
        )
        == 1
    )
    browser()
    retargeted = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
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
    stale_context = await client.post(
        path,
        json={**body, "expected_record_id": str(record_id)},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert stale_context.status_code == 409
    act_as_assistant()
    first_drain = await drain(client, session_id=session_id)
    assert from_plain(loads(first_drain.content), dict[str, object])["messages"] == []
    for ended in (session_id, second_session_id):
        await store.end_session(
            ended,
            ended=datetime.now(UTC),
            api_key_id=KB_KEY_ID,
            actor=KB_EMAIL,
        )
    browser()
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_viewer_chats_about_frozen_report_revision_from_own_workspace(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer's message carries only server-read evidence from one revision.

    The viewer's own agent is the assistant, as a `trax helper` on a server of
    one's own is.
    """
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    viewer_id = uuid.uuid4()
    viewer_key = uuid.uuid4()
    viewer_email = "viewer@example.com"
    monkeypatch.setattr(
        app.state,
        "config",
        replace(
            Config(),
            assistant=Assistant(actor="viewer-agent", email=viewer_email),
        ),
        raising=False,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) VALUES "
            "($1, 'test-user@example.com', 'Publisher', 'writer', 'active'), "
            "($2, $3, 'Viewer', 'viewer', 'active')",
            TEST_USER_ID,
            viewer_id,
            viewer_email,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'viewer-agent', 'test-hash', 'trax_view', 'viewer')",
            viewer_key,
            viewer_id,
        )
    issue_id = await store.submit_issue(
        SubmitIssue(title="Report owner", account="test-user@example.com"),
        actor="test-user@example.com",
    )
    belief_id = await store.submit_belief(
        SubmitBelief(title="Scaling helps", account="test-user@example.com"),
        actor="test-user@example.com",
    )
    paper_id = await store.submit_paper(
        SubmitPaper(title="Measured result", account="test-user@example.com"),
        actor="test-user@example.com",
    )
    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=-0.75,
        note="Contrary held-out evidence",
        actor="test-user@example.com",
    )
    install_identity(make_test_identity(api_key_id=None))
    payload = {
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
    published = await client.post(
        "/api/artifacts/content",
        json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert published.status_code == 201, published.text
    artifact_id = from_plain(
        from_plain(loads(published.content), dict[str, object])["artifact_id"],
        str,
    )
    session_id, _, _ = await store.start_session(
        SubmitAgentSession(title="Viewer's agent", cli="codex", account=viewer_email),
        requested_actor="viewer-agent",
        api_key_id=viewer_key,
    )
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=viewer_key,
            email=viewer_email,
            role="viewer",
        ),
    )
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=None,
            email=viewer_email,
            role="viewer",
        ),
    )
    created = await client.post("/api/workspaces")
    assert created.status_code == 200, created.text
    workspace_id = from_plain(
        from_plain(loads(created.content), dict[str, object])["id"],
        str,
    )
    path = f"/api/workspaces/{workspace_id}/operations"
    missing = await client.post(
        path,
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.artifact",
                "record_id": str(uuid.uuid4()),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert missing.status_code == 422
    shown_report = await client.post(
        path,
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.artifact",
                "record_id": artifact_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown_report.status_code == 200, shown_report.text
    shown_chat = await client.post(
        path,
        json={
            "revision": 1,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": artifact_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
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
    message_path = f"/api/workspaces/{workspace_id}/messages"
    body = {
        "text": "What evidence supports this?",
        "chat_instance_id": chat_id,
        "expected_record_id": artifact_id,
    }
    forged = await client.post(
        message_path,
        json={**body, "citations": [{"record_id": str(uuid.uuid4())}]},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert forged.status_code == 422
    sent = await client.post(
        message_path,
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert sent.status_code == 200, sent.text
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=viewer_key,
            email=viewer_email,
            role="viewer",
        ),
    )
    drained = await client.get(f"/api/sessions/{session_id}/inbound")
    assert drained.status_code == 200, drained.text
    messages = from_plain(
        from_plain(loads(drained.content), dict[str, object])["messages"],
        list[object],
    )
    context = from_plain(
        from_plain(messages[0], dict[str, object])["context"],
        dict[str, object],
    )
    assert context["record_id"] == artifact_id
    report = from_plain(context["artifact_content"], dict[str, object])
    assert report["revision"] == 1
    assert report["title"] == "Evidence atlas"
    assert from_plain(
        from_plain(report["citations"], list[object])[0],
        dict[str, object],
    )["record_id"] == str(
        issue_id,
    )
    finding = from_plain(
        from_plain(
            from_plain(
                from_plain(report["sections"], list[object])[0],
                dict[str, object],
            )["findings"],
            list[object],
        )[0],
        dict[str, object],
    )
    citation = from_plain(
        from_plain(finding["citations"], list[object])[0],
        dict[str, object],
    )
    assert citation["valence"] == -0.75
    assert citation["note"] == "Contrary held-out evidence"
    other_key = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'other-agent', 'test-hash', 'trax_othe', 'viewer')",
            other_key,
            viewer_id,
        )
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=other_key,
            email=viewer_email,
            role="viewer",
        ),
    )
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 403
    install_identity(make_test_identity(api_key_id=None))
    newer = await client.post(
        "/api/artifacts/content",
        json={
            **payload,
            "previous_artifact_id": artifact_id,
            "summary": "Newer result",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert newer.status_code == 201, newer.text
    newer_artifact_id = from_plain(
        from_plain(loads(newer.content), dict[str, object])["artifact_id"],
        str,
    )
    await store.add_edge(
        from_id=paper_id,
        to_id=belief_id,
        edge_kind="favors",
        valence=0.5,
        note="Changed live edge",
        actor="test-user@example.com",
    )
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=None,
            email=viewer_email,
            role="viewer",
        ),
    )
    same_revision = await client.post(
        message_path,
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert same_revision.status_code == 200, same_revision.text
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=viewer_key,
            email=viewer_email,
            role="viewer",
        ),
    )
    again = await client.get(f"/api/sessions/{session_id}/inbound")
    repeated = from_plain(
        from_plain(
            from_plain(
                from_plain(loads(again.content), dict[str, object])["messages"],
                list[object],
            )[0],
            dict[str, object],
        )["context"],
        dict[str, object],
    )
    repeated_report = from_plain(repeated["artifact_content"], dict[str, object])
    assert repeated_report["summary"] == "Frozen result"
    repeated_finding = from_plain(
        from_plain(
            from_plain(
                from_plain(repeated_report["sections"], list[object])[0],
                dict[str, object],
            )["findings"],
            list[object],
        )[0],
        dict[str, object],
    )
    assert (
        from_plain(
            from_plain(repeated_finding["citations"], list[object])[0],
            dict[str, object],
        )["valence"]
        == -0.75
    )
    install_identity(
        make_test_identity(
            user_id=viewer_id,
            api_key_id=None,
            email=viewer_email,
            role="viewer",
        ),
    )
    retargeted = await client.post(
        path,
        json={
            "revision": 2,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": newer_artifact_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert retargeted.status_code == 200, retargeted.text
    stale = await client.post(
        message_path,
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert stale.status_code == 409


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
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)

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
    assert from_plain(loads(sent.content), dict[str, object])["session_id"] == str(kb)
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


async def _seed_screen_records(store: Store) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert an Experiment and an Issue; return their ids."""
    experiment_id, issue_id = uuid.uuid4(), uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) VALUES "
            "($1, 'Experiment', 987654, 'active', 'test-user@example.com', "
            "'Measured tails'), "
            "($2, 'Issue', 11, 'active', 'test-user@example.com', $3)",
            experiment_id,
            issue_id,
            "t" * 20_000,
        )
    return experiment_id, issue_id


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_a_message_carries_the_records_of_the_page_trail_and_visuals(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A send resolves each route and visual to its record; a miss keeps its route."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    session_id = await start_session(client, actor=KB_ACTOR)
    experiment_id, issue_id = await _seed_screen_records(store)
    missing = uuid.uuid4()
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": await revision_of(client, workspace_id=workspace_id),
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": str(experiment_id),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    trail = [
        f"#/lookup/{issue_id}",
        "#/list/Issue",
        f"#/inquiry/{missing}",
        "#/graph?focus=issue/11&hops=2",
        "#/ref/Issue/12",
    ]

    sent = await client.post(
        f"/api/workspaces/{workspace_id}/messages",
        json={
            "text": "what about this experiment in context?",
            "chat_instance_id": str(chat_id),
            "expected_record_id": None,
            "page": "#/ref/Experiment/987654",
            "trail": trail,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert sent.status_code == 200
    act_as_assistant()
    drained = from_plain(
        loads((await drain(client, session_id=session_id)).content),
        dict[str, object],
    )
    message = from_plain(
        from_plain(drained["messages"], list[object])[0],
        dict[str, object],
    )
    context = from_plain(message["context"], dict[str, object])
    experiment = {
        "id": str(experiment_id),
        "kind": "Experiment",
        "seq": 987_654,
        "title": "Measured tails",
    }
    issue = {
        "id": str(issue_id),
        "kind": "Issue",
        "seq": 11,
        "title": "t" * 512,
    }
    assert context["page"] == {"route": "#/ref/Experiment/987654", "record": experiment}
    assert context["trail"] == [
        {"route": trail[0], "record": issue},
        {"route": trail[1], "record": None},
        {"route": trail[2], "record": None},
        {"route": trail[3], "record": issue},
        {"route": trail[4], "record": None},
    ]
    visuals = {
        from_plain(from_plain(item, dict[str, object])["type"], str): from_plain(
            item,
            dict[str, object],
        ).get("record")
        for item in from_plain(context["visible_visuals"], list[object])
    }
    assert (visuals["trax.chat"], visuals["trax.subgraph"]) == (None, experiment)


@pytest.mark.db_pglite
@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "screen",
    [
        {"page": "not a route"},
        {"page": "#/lookup/with space"},
        {"trail": ["#/graph", "bad"]},
        {"trail": ["#/graph"] * 9},
    ],
)
async def test_a_message_with_a_bad_route_or_a_long_trail_is_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    screen: dict[str, object],
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    _ = await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)

    refused = await client.post(
        f"/api/workspaces/{workspace_id}/messages",
        json={
            "text": "hi",
            "chat_instance_id": str(chat_id),
            "expected_record_id": None,
            **screen,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    assert refused.status_code == 422


@pytest.mark.usefixtures("assistant_served")
@pytest.mark.asyncio(loop_scope="session")
async def test_assistant_key_operates_a_canvas_only_after_a_conversation_on_it(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The assistant gets nothing from a user who never talked to it on that canvas."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    other_workspace = await open_workspace(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    second_canvas = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, is_default, state) "
            "VALUES ($1, $2, FALSE, $3)",
            second_canvas,
            TEST_USER_ID,
            {"visuals": []},
        )

    async def operate(workspace: uuid.UUID, *, revision: int) -> httpx2.Response:
        return await client.post(
            f"/api/workspaces/{workspace}/operations",
            json={
                "revision": revision,
                "operation": {"kind": "focus", "instance_id": str(chat_id)},
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    act_as_assistant()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 404
    assert (await operate(workspace_id, revision=1)).status_code == 404

    browser()
    assert (
        await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="show me something",
        )
    ).status_code == 200

    act_as_assistant()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 200
    assert (await operate(workspace_id, revision=1)).status_code == 200
    assert (await client.get(f"/api/workspaces/{second_canvas}")).status_code == 404
    assert (await operate(second_canvas, revision=0)).status_code == 404
    assert (await client.get(f"/api/workspaces/{other_workspace}")).status_code == 404
    assert (await operate(other_workspace, revision=0)).status_code == 404
    act_as_other_agent()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 404


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
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    await send_chat(
        client,
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
async def test_only_an_agent_may_navigate_and_the_route_must_be_clean(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser changes the hash itself; a route with a space is refused."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="hi")
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
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="hi")
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
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="hi")
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
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
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


async def _stream(
    workspace_id: uuid.UUID,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[str | bytes | memoryview]:
    """Open the events route as the installed browser, with a short keepalive."""
    monkeypatch.setattr(
        workspace_routes,
        "iter_workspace_events",
        partial(iter_workspace_events, keepalive_sec=0.05),
    )
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
    response = await workspace_routes.workspace_events_route(
        workspace_id,
        request,
        make_test_identity(api_key_id=None),
    )
    return aiter(response.body_iterator)


async def _end(
    store: Store,
    *,
    stream: AsyncIterator[str | bytes | memoryview],
) -> None:
    """End an event stream the way an owner's removal does, and let it finish."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            TEST_USER_ID,
        )
    async for _ in stream:
        pass


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
    """Over the route itself: open, a message, delivery, a status, a page move."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    stream = await _stream(workspace_id, monkeypatch=monkeypatch)
    assert await anext(stream) == b": open\n\n"
    opened = await _next_frame(stream)
    assert opened["type"] == "workspace"
    assert from_plain(opened["state"], dict[str, object])["assistant"] == KB_ACTOR

    browser()
    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello",
    )
    conversation_id = from_plain(
        from_plain(loads(sent.content), dict[str, object])["conversation_id"],
        str,
    )
    message = await _next_frame(stream)
    assert (message["type"], message["conversation_id"]) == ("message", conversation_id)
    act_as_assistant()
    await drain(client, session_id=kb)
    delivered = await _next_frame(stream)
    assert (delivered["type"], delivered["seq"]) == ("delivered", 1)
    status = await client.post(
        f"/api/chats/{conversation_id}/messages",
        json={"text": "reading", "kind": "status"},
    )
    assert status.status_code == 200
    assert (await _next_frame(stream))["text"] == "reading"
    moved = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={"revision": 0, "operation": {"kind": "navigate", "route": "#/graph"}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert moved.status_code == 200
    navigate = await _next_frame(stream)
    assert (navigate["type"], navigate["route"]) == ("navigate", "#/graph")
    assert all(isinstance(f["t"], int) for f in (opened, message, delivered, navigate))
    await _end(store, stream=stream)


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
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    stream = await _stream(workspace_id, monkeypatch=monkeypatch)
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
    await _end(store, stream=stream)


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
    """Revocation, not just a missing conversation, closes the assistant's access."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="hello")
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
