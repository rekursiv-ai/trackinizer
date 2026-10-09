"""Named canvas presets survive a new browser and carry their guidance to Chat."""

from __future__ import annotations

from typing import TYPE_CHECKING

import json
import uuid

import httpx2
import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    act_as_assistant,
    drain,
    open_workspace,
    revision_of,
    seed_accounts,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.chat_hub import ChatHub, WorkspaceFrame
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    from trackinizer.server.chat_hub import Frame
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_save_on_one_client_and_open_on_another(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved workflow restores its visual state, and Chat hands on its guidance."""
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    await seed_accounts(store)
    act_as_assistant()
    session_id = await start_session(client, actor=KB_ACTOR)
    install_identity(make_test_identity(api_key_id=None))
    first = from_plain(
        loads((await client.post("/api/workspaces")).content),
        dict[str, object],
    )
    workspace_id = from_plain(first["id"], str)
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
    current = from_plain(loads(shown.content), dict[str, object])
    chat = next(
        from_plain(item, dict[str, object])
        for item in from_plain(current["visuals"], list[object])
        if from_plain(item, dict[str, object])["type"] == "trax.chat"
    )
    save = await client.post(
        "/api/workspace-presets",
        json={
            "workspace_id": workspace_id,
            "revision": current["revision"],
            "name": "ARC3 investigation",
            "agent_instructions": "Trace evidence before proposing a change.",
            "continuation_record_id": str(uuid.uuid4()),
            "floating_rects": {
                from_plain(chat["id"], str): {
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
    preset = from_plain(loads(save.content), dict[str, object])
    assert preset["name"] == "ARC3 investigation"
    assert preset["agent_instructions"] == "Trace evidence before proposing a change."
    assert from_plain(
        from_plain(
            from_plain(preset["state"], dict[str, object])["visuals"],
            list[object],
        )[-1],
        dict[str, object],
    )["floating_rect"] == {
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
            from_plain(row, dict[str, object])["id"]
            for row in from_plain(loads(listed.content), list[object])
        ] == [preset["id"]]
        read_response = await second_browser.get(
            f"/api/workspace-presets/{preset['id']}",
        )
        assert read_response.status_code == 200
        assert json.loads(read_response.content) == json.loads(save.content)

    changed = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": current["revision"],
            "operation": {"kind": "hide", "instance_id": chat["id"]},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert changed.status_code == 200
    second = from_plain(loads(changed.content), dict[str, object])
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
    restored = from_plain(loads(opened.content), dict[str, object])
    assert restored["revision"] == from_plain(second["revision"], int) + 1
    assert restored["agent_instructions"] == preset["agent_instructions"]
    assert restored["continuation_record_id"] == preset["continuation_record_id"]
    assert len(from_plain(restored["visuals"], list[object])) == 2
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
    second_preset = from_plain(loads(resaved.content), dict[str, object])
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
    continued = from_plain(loads(focused.content), dict[str, object])
    assert continued["agent_instructions"] == preset["agent_instructions"]
    assert continued["continuation_record_id"] == preset["continuation_record_id"]
    sent = await client.post(
        "/api/chats",
        json={
            "workspace_id": workspace_id,
            "text": "Continue this investigation",
            "chat_instance_id": chat["id"],
            "expected_record_id": None,
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
    workspace = from_plain(
        loads((await client.post("/api/workspaces")).content),
        dict[str, object],
    )
    workspace_id = from_plain(workspace["id"], str)
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
    browse_id = from_plain(
        from_plain(
            from_plain(workspace["visuals"], list[object])[0],
            dict[str, object],
        )["id"],
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
    preset_id = from_plain(
        from_plain(loads(saved.content), dict[str, object])["id"],
        str,
    )
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
    assert (
        from_plain(loads(revised.content), dict[str, object])["name"]
        == "Research revisited"
    )
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
    workspace = from_plain(
        loads((await client.post("/api/workspaces")).content),
        dict[str, object],
    )
    workspace_id = from_plain(workspace["id"], str)
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
    assert json.loads(saved.content) == json.loads(replayed_save.content)
    mismatched_save = await client.post(
        "/api/workspace-presets",
        json={**save_body, "name": "Other"},
        headers=save_headers,
    )
    assert mismatched_save.status_code == 409
    assert "Idempotency-Key" in from_plain(
        from_plain(loads(mismatched_save.content), dict[str, object])["detail"],
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
    preset_id = from_plain(
        from_plain(loads(saved.content), dict[str, object])["id"],
        str,
    )
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
    assert json.loads(opened.content) == json.loads(replayed_open.content)
    current = from_plain(
        loads((await client.get(f"/api/workspaces/{workspace_id}")).content),
        dict[str, object],
    )
    assert current["revision"] == 1
    mismatched_open = await client.post(
        f"/api/workspace-presets/{preset_id}/open",
        json={**open_body, "revision": 1},
        headers=open_headers,
    )
    assert mismatched_open.status_code == 409
    assert "Idempotency-Key" in from_plain(
        from_plain(loads(mismatched_open.content), dict[str, object])["detail"],
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
            from_plain(
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
    assert json.loads(replayed_latest.content) == json.loads(latest_open.content)
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
        transient_id = from_plain(
            from_plain(loads(transient.content), dict[str, object])["id"],
            str,
        )
        assert (
            await client.delete(f"/api/workspace-presets/{transient_id}")
        ).status_code == 204
    async with store.engine.acquire() as conn:
        assert (
            from_plain(
                await conn.fetchval(
                    "SELECT count(*) FROM visual_workspace_operations WHERE workspace_id = $1",
                    uuid.UUID(workspace_id),
                ),
                int,
            )
            <= 64
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_opening_a_preset_pushes_the_canvas_once_and_its_replay_pushes_nothing(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tab sees the restored canvas; a retried open is silent."""
    client, store = pglite_route_client
    hub = ChatHub()
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    await seed_accounts(store)
    workspace_id = await open_workspace(client)
    revision = await revision_of(client, workspace_id=workspace_id)
    saved = await client.post(
        "/api/workspace-presets",
        json={"workspace_id": str(workspace_id), "revision": revision, "name": "view"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert saved.status_code == 200
    preset_id = from_plain(
        from_plain(loads(saved.content), dict[str, object])["id"],
        str,
    )
    key = str(uuid.uuid4())
    body = {"workspace_id": str(workspace_id), "revision": revision}
    path = f"/api/workspace-presets/{preset_id}/open"
    with hub.subscribe(workspace_id) as stream:
        opened = await client.post(path, json=body, headers={"Idempotency-Key": key})
        replay = await client.post(path, json=body, headers={"Idempotency-Key": key})
        frames: list[Frame | None] = []
        while not stream.queue.empty():
            frames.append(stream.queue.get_nowait())
    assert (opened.status_code, replay.status_code) == (200, 200)
    assert loads(opened.content) == loads(replay.content)
    assert [f.type for f in frames if f is not None] == ["workspace"]
    frame = frames[0]
    assert isinstance(frame, WorkspaceFrame)
    assert frame.state.revision == revision + 1


def test_preset_create_conflict_is_in_openapi() -> None:
    """Generated clients can decode the documented stale-revision response."""
    spec = from_plain(app.openapi(), dict[str, object])
    paths = from_plain(spec["paths"], dict[str, object])
    endpoint = from_plain(paths["/api/workspace-presets"], dict[str, object])
    operation = from_plain(endpoint["post"], dict[str, object])
    responses = from_plain(operation["responses"], dict[str, object])
    conflict = from_plain(responses["409"], dict[str, object])
    content = from_plain(conflict["content"], dict[str, object])
    media = from_plain(content["application/json"], dict[str, object])
    schema = from_plain(media["schema"], dict[str, object])
    assert from_plain(schema["$ref"], str).endswith(
        "/WorkspaceConflict",
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
