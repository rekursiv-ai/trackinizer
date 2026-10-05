"""Named canvas presets survive a new browser and exclude live session pairing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

import httpx2
import pytest

from trackinizer.lib.custom_json import convert, loads, parse
from trackinizer.server.api.app import app
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_save_on_one_client_and_open_on_another(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved workflow restores its visual state and guidance, not its session."""
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
    first = parse(
        (await client.post("/api/workspaces")).content,
        dict[str, object],
    )
    workspace_id = convert(first["id"], str)
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": first["revision"],
            "operation": {
                "kind": "show",
                "visual_type": "trax.chat",
                "placement": "floating",
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200
    current = parse(shown.content, dict[str, object])
    chat = next(
        convert(item, dict[str, object])
        for item in convert(current["visuals"], list[object])
        if convert(item, dict[str, object])["type"] == "trax.chat"
    )
    install_identity(make_test_identity())
    started = await client.post("/api/sessions/start", json={"cli": "codex"})
    assert started.status_code == 201
    session_id = convert(parse(started.content, dict[str, object])["id"], str)
    assert (await client.get(f"/api/sessions/{session_id}/inbound")).status_code == 200
    install_identity(make_test_identity(api_key_id=None))
    paired = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": current["revision"], "session_id": session_id},
    )
    assert paired.status_code == 200
    current = parse(paired.content, dict[str, object])
    assert current["connected_session_id"] == session_id
    save = await client.post(
        "/api/workspace-presets",
        json={
            "workspace_id": workspace_id,
            "revision": current["revision"],
            "name": "ARC3 investigation",
            "agent_instructions": "Trace evidence before proposing a change.",
            "continuation_record_id": str(uuid.uuid4()),
            "floating_rects": {
                convert(chat["id"], str): {
                    "left": 80,
                    "top": 100,
                    "width": 420,
                    "height": 500,
                },
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert save.status_code == 200
    preset = parse(save.content, dict[str, object])
    assert preset["name"] == "ARC3 investigation"
    assert preset["agent_instructions"] == "Trace evidence before proposing a change."
    assert "connected_session_id" not in convert(preset["state"], dict[str, object])
    visuals = convert(
        convert(preset["state"], dict[str, object])["visuals"],
        list[object],
    )
    assert convert(visuals[-1], dict[str, object])["floating_rect"] == {
        "left": 80,
        "top": 100,
        "width": 420,
        "height": 500,
    }

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://second-browser",
    ) as second_browser:
        listed = await second_browser.get("/api/workspace-presets")
        assert listed.status_code == 200
        assert [
            convert(row, dict[str, object])["id"]
            for row in parse(listed.content, list[object])
        ] == [preset["id"]]
        read_response = await second_browser.get(
            f"/api/workspace-presets/{preset['id']}",
        )
        assert read_response.status_code == 200
        assert loads(read_response.content) == loads(save.content)

    changed = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": current["revision"],
            "operation": {"kind": "hide", "instance_id": chat["id"]},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert changed.status_code == 200
    second = parse(changed.content, dict[str, object])
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://second-browser",
    ) as second_browser:
        opened = await second_browser.post(
            f"/api/workspace-presets/{preset['id']}/open",
            json={"workspace_id": workspace_id, "revision": second["revision"]},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    assert opened.status_code == 200
    restored = parse(opened.content, dict[str, object])
    assert restored["revision"] == convert(second["revision"], int) + 1
    assert restored["connected_session_id"] is None
    assert restored["agent_instructions"] == preset["agent_instructions"]
    assert restored["continuation_record_id"] == preset["continuation_record_id"]
    assert len(convert(restored["visuals"], list[object])) == 2
    resaved = await client.post(
        "/api/workspace-presets",
        json={
            "workspace_id": workspace_id,
            "revision": restored["revision"],
            "name": "ARC3 second pass",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert resaved.status_code == 200
    second_preset = parse(resaved.content, dict[str, object])
    assert second_preset["agent_instructions"] == preset["agent_instructions"]
    assert second_preset["continuation_record_id"] == preset["continuation_record_id"]
    focused = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": restored["revision"],
            "operation": {"kind": "focus", "instance_id": chat["id"]},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert focused.status_code == 200
    continued = parse(focused.content, dict[str, object])
    assert continued["agent_instructions"] == preset["agent_instructions"]
    assert continued["continuation_record_id"] == preset["continuation_record_id"]
    assert continued["connected_session_id"] is None
    paired_again = await client.put(
        f"/api/workspaces/{workspace_id}/connection",
        json={"revision": continued["revision"], "session_id": session_id},
    )
    assert paired_again.status_code == 200
    sent = await client.post(
        f"/api/workspaces/{workspace_id}/messages",
        json={
            "text": "Continue this investigation",
            "chat_instance_id": chat["id"],
            "expected_record_id": None,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert sent.status_code == 200
    install_identity(make_test_identity())
    drained = await client.get(f"/api/sessions/{session_id}/inbound")
    messages = convert(
        parse(drained.content, dict[str, object])["messages"],
        list[object],
    )
    context = convert(
        convert(messages[0], dict[str, object])["context"],
        dict[str, object],
    )
    assert context["agent_instructions"] == preset["agent_instructions"]
    assert context["continuation_record_id"] == preset["continuation_record_id"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_presets_enforce_revision_owner_and_floating_target(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A stale snapshot and foreign account cannot read or mutate a preset."""
    client, store = pglite_route_client
    foreign_id = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE), "
            "($2, 'foreign@example.com', 'Foreign', 'writer', 'active', TRUE)",
            TEST_USER_ID,
            foreign_id,
        )
    install_identity(make_test_identity(api_key_id=None))
    workspace = parse(
        (await client.post("/api/workspaces")).content,
        dict[str, object],
    )
    workspace_id = convert(workspace["id"], str)
    base = {"workspace_id": workspace_id, "revision": 0, "name": "Research"}
    assert (
        await client.post(
            "/api/workspace-presets",
            json={**base, "name": "   "},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/workspace-presets",
            json={**base, "revision": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    assert (
        await client.post(
            "/api/workspace-presets",
            json={
                **base,
                "floating_rects": {
                    str(uuid.uuid4()): {
                        "left": 0,
                        "top": 0,
                        "width": 320,
                        "height": 240,
                    },
                },
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    browse_id = convert(
        convert(convert(workspace["visuals"], list[object])[0], dict[str, object])[
            "id"
        ],
        str,
    )
    assert (
        await client.post(
            "/api/workspace-presets",
            json={
                **base,
                "floating_rects": {
                    browse_id: {"left": 0, "top": 0, "width": 320, "height": 240},
                },
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422
    saved = await client.post(
        "/api/workspace-presets",
        json=base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert saved.status_code == 200
    preset_id = convert(parse(saved.content, dict[str, object])["id"], str)
    assert (
        await client.put(f"/api/workspace-presets/{preset_id}", json={"name": "   "})
    ).status_code == 422
    install_identity(make_test_identity(user_id=foreign_id, api_key_id=None))
    assert (await client.get(f"/api/workspace-presets/{preset_id}")).status_code == 404
    assert (
        await client.put(f"/api/workspace-presets/{preset_id}", json={"name": "Stolen"})
    ).status_code == 404
    assert (
        await client.delete(f"/api/workspace-presets/{preset_id}")
    ).status_code == 404
    assert (
        await client.post(
            f"/api/workspace-presets/{preset_id}/open",
            json={"workspace_id": workspace_id, "revision": 0},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 404
    install_identity(make_test_identity(api_key_id=None))
    revised = await client.put(
        f"/api/workspace-presets/{preset_id}",
        json={
            "name": "Research revisited",
            "agent_instructions": "Continue carefully.",
        },
    )
    assert revised.status_code == 200
    assert parse(revised.content, dict[str, object])["name"] == "Research revisited"
    assert (
        await client.post(
            f"/api/workspace-presets/{preset_id}/open",
            json={"workspace_id": workspace_id, "revision": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 409
    assert (
        await client.delete(f"/api/workspace-presets/{preset_id}")
    ).status_code == 204
    assert (await client.get(f"/api/workspace-presets/{preset_id}")).status_code == 404
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspace_presets (id, user_id, name, state) "
            "SELECT gen_random_uuid(), $1, 'Existing', '{\"visuals\":[]}'::jsonb "
            "FROM generate_series(1, 100)",
            TEST_USER_ID,
        )
    assert (
        await client.post(
            "/api/workspace-presets",
            json=base,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    ).status_code == 422


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_preset_save_and_open_replay_once(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A lost response can be retried without duplicating a view or revision."""
    client, store = pglite_route_client
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
            "VALUES ($1, 'test-user@example.com', 'Test', 'writer', 'active', TRUE)",
            TEST_USER_ID,
        )
    install_identity(make_test_identity(api_key_id=None))
    workspace = parse(
        (await client.post("/api/workspaces")).content,
        dict[str, object],
    )
    workspace_id = convert(workspace["id"], str)
    save_body = {"workspace_id": workspace_id, "revision": 0, "name": "ARC3"}
    save_headers = {"Idempotency-Key": str(uuid.uuid4())}
    saved = await client.post(
        "/api/workspace-presets",
        json=save_body,
        headers=save_headers,
    )
    replayed_save = await client.post(
        "/api/workspace-presets",
        json=save_body,
        headers=save_headers,
    )
    assert saved.status_code == replayed_save.status_code == 200
    assert loads(saved.content) == loads(replayed_save.content)
    mismatched_save = await client.post(
        "/api/workspace-presets",
        json={**save_body, "name": "Other"},
        headers=save_headers,
    )
    assert mismatched_save.status_code == 409
    assert "Idempotency-Key" in convert(
        parse(mismatched_save.content, dict[str, object])["detail"],
        str,
    )
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM visual_workspace_presets WHERE user_id = $1",
                TEST_USER_ID,
            )
            == 1
        )
    preset_id = convert(parse(saved.content, dict[str, object])["id"], str)
    open_body = {"workspace_id": workspace_id, "revision": 0}
    open_headers = {"Idempotency-Key": str(uuid.uuid4())}
    opened = await client.post(
        f"/api/workspace-presets/{preset_id}/open",
        json=open_body,
        headers=open_headers,
    )
    replayed_open = await client.post(
        f"/api/workspace-presets/{preset_id}/open",
        json=open_body,
        headers=open_headers,
    )
    assert opened.status_code == replayed_open.status_code == 200
    assert loads(opened.content) == loads(replayed_open.content)
    current = parse(
        (await client.get(f"/api/workspaces/{workspace_id}")).content,
        dict[str, object],
    )
    assert current["revision"] == 1
    mismatched_open = await client.post(
        f"/api/workspace-presets/{preset_id}/open",
        json={**open_body, "revision": 1},
        headers=open_headers,
    )
    assert mismatched_open.status_code == 409
    assert "Idempotency-Key" in convert(
        parse(mismatched_open.content, dict[str, object])["detail"],
        str,
    )
    revision = 1
    latest_open_body = open_body
    latest_open_headers = open_headers
    latest_open = opened
    for _ in range(66):
        latest_open_body = {"workspace_id": workspace_id, "revision": revision}
        latest_open_headers = {"Idempotency-Key": str(uuid.uuid4())}
        latest_open = await client.post(
            f"/api/workspace-presets/{preset_id}/open",
            json=latest_open_body,
            headers=latest_open_headers,
        )
        assert latest_open.status_code == 200
        revision += 1
    async with store.engine.acquire() as conn:
        assert (
            convert(
                await conn.fetchval(
                    "SELECT count(*) FROM visual_workspace_operations WHERE workspace_id = $1",
                    uuid.UUID(workspace_id),
                ),
                int,
            )
            <= 64
        )
    replayed_latest = await client.post(
        f"/api/workspace-presets/{preset_id}/open",
        json=latest_open_body,
        headers=latest_open_headers,
    )
    assert loads(replayed_latest.content) == loads(latest_open.content)
    for _ in range(66):
        transient = await client.post(
            "/api/workspace-presets",
            json={
                "workspace_id": workspace_id,
                "revision": revision,
                "name": "Temporary",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert transient.status_code == 200
        transient_id = convert(
            parse(transient.content, dict[str, object])["id"],
            str,
        )
        assert (
            await client.delete(f"/api/workspace-presets/{transient_id}")
        ).status_code == 204
    async with store.engine.acquire() as conn:
        assert (
            convert(
                await conn.fetchval(
                    "SELECT count(*) FROM visual_workspace_operations WHERE workspace_id = $1",
                    uuid.UUID(workspace_id),
                ),
                int,
            )
            <= 64
        )


def test_preset_create_conflict_is_in_openapi() -> None:
    """Generated clients can decode the documented stale-revision response."""
    spec = convert(app.openapi(), dict[str, object])
    paths = convert(spec["paths"], dict[str, object])
    endpoint = convert(paths["/api/workspace-presets"], dict[str, object])
    operation = convert(endpoint["post"], dict[str, object])
    responses = convert(operation["responses"], dict[str, object])
    conflict = convert(responses["409"], dict[str, object])
    content = convert(conflict["content"], dict[str, object])
    media = convert(content["application/json"], dict[str, object])
    schema = convert(media["schema"], dict[str, object])
    assert convert(schema["$ref"], str).endswith(
        "/WorkspaceConflict",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
