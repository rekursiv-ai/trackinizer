"""A canvas workspace persists and rejects stale concurrent operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

import pytest

from trackinizer.lib.custom_json import DictCodec, IntCodec, ListCodec, StrCodec, loads
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.visuals.catalog import StaticVisual, Workspace
from trackinizer.wire.bodies import (
    SubmitAgentSession,
    SubmitBelief,
    SubmitIssue,
    SubmitPaper,
)


if TYPE_CHECKING:
    import httpx2

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
    initial = DictCodec.coerce(loads(created.content))
    workspace_id = StrCodec.coerce(initial["id"])
    assert initial["revision"] == 0
    assert [
        DictCodec.coerce(item)["type"] for item in ListCodec.coerce(initial["visuals"])
    ] == ["trax.browse"]

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
    updated = DictCodec.coerce(loads(shown.content))
    assert updated["revision"] == 1
    assert {
        DictCodec.coerce(item)["type"] for item in ListCodec.coerce(updated["visuals"])
    } == {"trax.browse", "trax.chat"}

    replay = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    assert loads(replay.content) == loads(shown.content)
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
    mismatch = DictCodec.coerce(loads(mismatched_replay.content))
    assert DictCodec.coerce(mismatch["current"])["revision"] == 1
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
    conflict = DictCodec.coerce(loads(stale.content))
    assert DictCodec.coerce(conflict["current"])["revision"] == 1

    read = await client.get(f"/api/workspaces/{workspace_id}")
    assert read.status_code == 200
    assert loads(read.content) == loads(shown.content)


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
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
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
    visuals = ListCodec.coerce(DictCodec.coerce(loads(shown.content))["visuals"])
    assert DictCodec.coerce(visuals[-1])["record_id"] == record_id


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_can_target_record_in_separate_read_profile(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send a persisted remote record ID without inventing local metadata."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    session_id = StrCodec.coerce(DictCodec.coerce(loads(started.content))["id"])
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": session_id},
    )
    assert paired.status_code == 200
    remote_record_id = str(uuid.uuid4())
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 1,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": remote_record_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    visuals = ListCodec.coerce(DictCodec.coerce(loads(shown.content))["visuals"])
    chat_id = StrCodec.coerce(DictCodec.coerce(visuals[-1])["id"])
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
    install_identity(make_test_identity())
    drained = await client.get(f"/api/sessions/{session_id}/inbound")
    messages = ListCodec.coerce(DictCodec.coerce(loads(drained.content))["messages"])
    context = DictCodec.coerce(DictCodec.coerce(messages[0])["context"])
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
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
    enabled = await client.put("/api/me/visual-workspace", json={"enabled": True})
    assert enabled.status_code == 200
    assert DictCodec.coerce(loads(enabled.content))["enabled"] is True
    disabled = await client.put("/api/me/visual-workspace", json={"enabled": False})
    assert disabled.status_code == 200
    still_owned = await client.get(f"/api/workspaces/{workspace_id}")
    assert still_owned.status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_api_key_cannot_create_or_read_unpaired_workspace(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An API key cannot learn workspace state before explicit browser pairing."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity())
    assert (await client.post("/api/workspaces")).status_code == 403
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    assert created.status_code == 200
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
    install_identity(make_test_identity())
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_agent_operations_require_explicit_live_session_pairing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A key controls a canvas only while its live session is paired."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    workspace = await client.post("/api/workspaces")
    assert workspace.status_code == 200
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(workspace.content))["id"])

    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    assert started.status_code == 201
    session_id = StrCodec.coerce(DictCodec.coerce(loads(started.content))["id"])
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    operation = {
        "revision": 0,
        "operation": {"kind": "show", "visual_type": "trax.chat"},
    }
    path = f"/api/workspaces/{workspace_id}/operations"
    unpaired = await client.post(
        path,
        json=operation,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert unpaired.status_code == 403
    agent_pair = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": session_id},
    )
    assert agent_pair.status_code == 403

    install_identity(make_test_identity(api_key_id=None))
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": session_id},
    )
    assert paired.status_code == 200
    paired_state = DictCodec.coerce(loads(paired.content))
    assert paired_state["revision"] == 1
    assert paired_state["connected_session_id"] == session_id

    install_identity(make_test_identity())
    shown = await client.post(
        path,
        json={**operation, "revision": 1},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    assert DictCodec.coerce(loads(shown.content))["revision"] == 2

    await store.end_session(
        uuid.UUID(session_id),
        ended=datetime.now(UTC),
        api_key_id=TEST_API_KEY_ID,
        actor="test-user@example.com",
    )
    after_end = await client.post(
        path,
        json={
            "revision": 2,
            "operation": {"kind": "hide", "instance_id": str(uuid.uuid4())},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert after_end.status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_lists_only_its_connectable_live_sessions(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The picker omits revoked sessions and refuses API-key callers."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    assert started.status_code == 201
    session_id = StrCodec.coerce(DictCodec.coerce(loads(started.content))["id"])
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    path = "/api/workspaces/sessions/connectable"
    assert (await client.get(path)).status_code == 403

    install_identity(make_test_identity(api_key_id=None))
    listed = await client.get(path)
    assert listed.status_code == 200
    sessions = ListCodec.coerce(loads(listed.content))
    assert len(sessions) == 1
    session = DictCodec.coerce(sessions[0])
    assert session["id"] == session_id
    assert session["cli"] == "codex"

    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            TEST_API_KEY_ID,
        )
    assert loads((await client.get(path)).content) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_stale_poller_is_not_connectable_or_messageable(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB-active session without recent inbound polling is unavailable."""
    client, store = pglite_route_client
    now = [100.0]
    monkeypatch.setattr(
        app.state,
        "inbound",
        InboundQueue(poller_ttl_sec=45.0, _clock=lambda: now[0]),
        raising=False,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    session_id = StrCodec.coerce(DictCodec.coerce(loads(started.content))["id"])
    install_identity(make_test_identity(api_key_id=None))
    picker = "/api/workspaces/sessions/connectable"
    assert loads((await client.get(picker)).content) == []
    assert (
        await client.put(
            f"/api/workspaces/{workspace_id}/connection",
            json={"revision": 0, "session_id": session_id},
        )
    ).status_code == 422

    install_identity(make_test_identity())
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    assert len(ListCodec.coerce(loads((await client.get(picker)).content))) == 1
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": session_id},
    )
    assert paired.status_code == 200
    status_path = f"/api/workspaces/{workspace_id}/connection"
    assert (
        DictCodec.coerce(loads((await client.get(status_path)).content))["status"]
        == "live"
    )

    now[0] = 145.0
    assert loads((await client.get(picker)).content) == []
    assert (
        DictCodec.coerce(loads((await client.get(status_path)).content))["status"]
        == "unavailable"
    )
    assert (
        await client.post(
            f"/api/workspaces/{workspace_id}/messages",
            json={"text": "Cannot deliver to a dead poller"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    assert (
        await client.put(
            status_path,
            json={"revision": 1, "session_id": session_id},
        )
    ).status_code == 422


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_connectable_picker_finds_live_session_after_stale_page(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The picker cap applies to live sessions, not the first 100 DB rows."""
    client, store = pglite_route_client
    live_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
        for index in range(101):
            await conn.execute(
                "INSERT INTO inquiries "
                "(id, kind, seq, status, account, title, owner, "
                "agentsession_opened_by_api_key_id, agentsession_cli, created) "
                "VALUES ($1, 'AgentSession', $2, 'active', 'test-user@example.com', "
                "$3, $4, $5, 'codex', "
                "clock_timestamp() - ($2::int * interval '1 second'))",
                live_id if index == 100 else uuid.uuid4(),
                10_000 + index,
                f"Session {index}",
                f"agent-{index}",
                TEST_API_KEY_ID,
            )
    install_identity(make_test_identity())
    assert (await client.get(f"/api/sessions/{live_id}/inbound")).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    response = await client.get("/api/workspaces/sessions/connectable")
    assert response.status_code == 200
    assert [
        DictCodec.coerce(row)["id"] for row in ListCodec.coerce(loads(response.content))
    ] == [
        str(live_id),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_browser_chat_uses_paired_session_and_persisted_canvas_context(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser message carries record details from persisted canvas state."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    record_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'agent', 'test-hash', 'trax_test', 'writer')",
            TEST_API_KEY_ID,
            TEST_USER_ID,
        )
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'Experiment', 987654, 'active', "
            "'test-user@example.com', 'Measured tails')",
            record_id,
        )
    install_identity(make_test_identity(api_key_id=None))
    created = await client.post("/api/workspaces")
    workspace = DictCodec.coerce(loads(created.content))
    workspace_id = StrCodec.coerce(workspace["id"])
    message_path = f"/api/workspaces/{workspace_id}/messages"
    assert (
        await client.post(
            message_path,
            json={"text": "Before pairing", "expected_record_id": None},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    session_id = StrCodec.coerce(DictCodec.coerce(loads(started.content))["id"])
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": session_id},
    )
    assert paired.status_code == 200
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
            "revision": 1,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": str(record_id),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    visuals = ListCodec.coerce(DictCodec.coerce(loads(shown.content))["visuals"])
    chat_id = StrCodec.coerce(DictCodec.coerce(visuals[-1])["id"])
    path = message_path
    body = {
        "text": "What do the tails show?",
        "chat_instance_id": chat_id,
        "expected_record_id": str(record_id),
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    sent = await client.post(path, json=body, headers=headers)
    assert sent.status_code == 200
    receipt = DictCodec.coerce(loads(sent.content))
    assert receipt["session_id"] == session_id
    assert receipt["queued"] == 1
    replay = await client.post(path, json=body, headers=headers)
    assert loads(replay.content) == loads(sent.content)
    connection = await client.get(f"/api/workspaces/{workspace_id}/connection")
    assert connection.status_code == 200
    live = DictCodec.coerce(loads(connection.content))
    assert live["status"] == "live"
    assert live["session_id"] == session_id
    assert live["cli"] == "codex"
    assert live["actor"] == "test-user@example.com"
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET owner = NULL WHERE id = $1",
            uuid.UUID(session_id),
        )
    ownerless = await client.get(f"/api/workspaces/{workspace_id}/connection")
    assert DictCodec.coerce(loads(ownerless.content))["status"] == "unavailable"
    assert (
        await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET owner = 'test-user@example.com' WHERE id = $1",
            uuid.UUID(session_id),
        )

    install_identity(make_test_identity())
    drained = await client.get(f"/api/sessions/{session_id}/inbound")
    assert drained.status_code == 200
    messages = ListCodec.coerce(DictCodec.coerce(loads(drained.content))["messages"])
    assert len(messages) == 1
    message = DictCodec.coerce(messages[0])
    assert message["source"] == "test-user@example.com"
    context = DictCodec.coerce(message["context"])
    assert context["workspace_id"] == workspace_id
    assert context["record_id"] == str(record_id)
    assert DictCodec.coerce(context["record"]) == {
        "id": str(record_id),
        "kind": "Experiment",
        "seq": 987_654,
        "title": "Measured tails",
    }
    visible = ListCodec.coerce(context["visible_visuals"])
    assert {
        (DictCodec.coerce(item)["id"], DictCodec.coerce(item)["type"])
        for item in visible
    } == {
        (
            StrCodec.coerce(DictCodec.coerce(item)["id"]),
            StrCodec.coerce(DictCodec.coerce(item)["type"]),
        )
        for item in visuals
    }

    install_identity(make_test_identity(api_key_id=None))
    after_drain = await client.post(path, json=body, headers=headers)
    assert loads(after_drain.content) == loads(sent.content)
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
    install_identity(make_test_identity())
    bounded_drain = await client.get(f"/api/sessions/{session_id}/inbound")
    bounded_message = DictCodec.coerce(
        ListCodec.coerce(DictCodec.coerce(loads(bounded_drain.content))["messages"])[0],
    )
    bounded_context = DictCodec.coerce(bounded_message["context"])
    bounded_record = DictCodec.coerce(bounded_context["record"])
    assert bounded_record["title"] == "x" * 512
    install_identity(make_test_identity(api_key_id=None))
    assert (
        await client.post(
            path,
            json={
                "text": "Wrong visual",
                "chat_instance_id": DictCodec.coerce(visuals[0])["id"],
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
    install_identity(make_test_identity())
    second_started = await client.post("/api/sessions/start", json={"cli": "codex"})
    second_session_id = StrCodec.coerce(
        DictCodec.coerce(loads(second_started.content))["id"],
    )
    assert (
        await client.get(f"/api/sessions/{second_session_id}/inbound")
    ).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    second_pair = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 2, "session_id": second_session_id},
    )
    assert second_pair.status_code == 200
    assert (await client.post(path, json=body, headers=headers)).status_code == 409
    second_send = await client.post(
        path,
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert (
        DictCodec.coerce(loads(second_send.content))["session_id"] == second_session_id
    )
    install_identity(make_test_identity())
    second_drain = await client.get(f"/api/sessions/{second_session_id}/inbound")
    assert (
        len(ListCodec.coerce(DictCodec.coerce(loads(second_drain.content))["messages"]))
        == 1
    )
    install_identity(make_test_identity(api_key_id=None))
    retargeted = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 3,
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
    install_identity(make_test_identity())
    assert (
        ListCodec.coerce(
            DictCodec.coerce(
                loads(
                    (
                        await client.get(
                            f"/api/sessions/{session_id}/inbound",
                        )
                    ).content,
                ),
            )["messages"],
        )
        == []
    )
    install_identity(make_test_identity(api_key_id=None))
    session_id = second_session_id
    await store.end_session(
        uuid.UUID(session_id),
        ended=datetime.now(UTC),
        api_key_id=TEST_API_KEY_ID,
        actor="test-user@example.com",
    )
    ended = await client.get(f"/api/workspaces/{workspace_id}/connection")
    assert DictCodec.coerce(loads(ended.content))["status"] == "ended"
    assert (
        await client.post(
            path,
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    disconnected = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 4, "session_id": None},
    )
    assert disconnected.status_code == 200
    unavailable = await client.get(f"/api/workspaces/{workspace_id}/connection")
    assert DictCodec.coerce(loads(unavailable.content))["status"] == "unavailable"


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
        StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"]),
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
    assert IntCodec.coerce(count) <= 64
    replay = await client.post(path, json=operation, headers={"Idempotency-Key": key})
    assert replay.status_code == 200
    assert loads(replay.content) == loads(shown.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_viewer_chats_about_frozen_report_revision_from_own_workspace(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer's message carries only server-read evidence from one revision."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    viewer_id = uuid.uuid4()
    viewer_key = uuid.uuid4()
    viewer_email = "viewer@example.com"
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
    artifact_id = StrCodec.coerce(
        DictCodec.coerce(loads(published.content))["artifact_id"],
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
    workspace_id = StrCodec.coerce(DictCodec.coerce(loads(created.content))["id"])
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": 0, "session_id": str(session_id)},
    )
    assert paired.status_code == 200, paired.text
    path = f"/api/workspaces/{workspace_id}/operations"
    missing = await client.post(
        path,
        json={
            "revision": 1,
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
            "revision": 1,
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
            "revision": 2,
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "record_id": artifact_id,
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown_chat.status_code == 200, shown_chat.text
    chat_id = StrCodec.coerce(
        DictCodec.coerce(
            ListCodec.coerce(DictCodec.coerce(loads(shown_chat.content))["visuals"])[
                -1
            ],
        )["id"],
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
    messages = ListCodec.coerce(DictCodec.coerce(loads(drained.content))["messages"])
    context = DictCodec.coerce(DictCodec.coerce(messages[0])["context"])
    assert context["record_id"] == artifact_id
    report = DictCodec.coerce(context["artifact_content"])
    assert report["revision"] == 1
    assert report["title"] == "Evidence atlas"
    assert DictCodec.coerce(ListCodec.coerce(report["citations"])[0])[
        "record_id"
    ] == str(
        issue_id,
    )
    finding = DictCodec.coerce(
        ListCodec.coerce(
            DictCodec.coerce(ListCodec.coerce(report["sections"])[0])["findings"],
        )[0],
    )
    citation = DictCodec.coerce(ListCodec.coerce(finding["citations"])[0])
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
    newer_artifact_id = StrCodec.coerce(
        DictCodec.coerce(loads(newer.content))["artifact_id"],
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
    repeated = DictCodec.coerce(
        DictCodec.coerce(
            ListCodec.coerce(DictCodec.coerce(loads(again.content))["messages"])[0],
        )["context"],
    )
    repeated_report = DictCodec.coerce(repeated["artifact_content"])
    assert repeated_report["summary"] == "Frozen result"
    repeated_finding = DictCodec.coerce(
        ListCodec.coerce(
            DictCodec.coerce(ListCodec.coerce(repeated_report["sections"])[0])[
                "findings"
            ],
        )[0],
    )
    assert (
        DictCodec.coerce(ListCodec.coerce(repeated_finding["citations"])[0])["valence"]
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
            "revision": 3,
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
    initial = DictCodec.coerce(loads(created.content))
    assert [
        DictCodec.coerce(item)["type"] for item in ListCodec.coerce(initial["visuals"])
    ] == ["x.log"]
    shown = await client.post(
        f"/api/workspaces/{StrCodec.coerce(initial['id'])}/operations",
        json={
            "revision": 0,
            "operation": {"kind": "show", "visual_type": "trax.chat"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 422
    assert "Unknown visual type" in shown.text


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
