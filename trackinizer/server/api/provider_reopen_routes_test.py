"""A provider switch resumes a closed chat; a drain alone never does."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    USER_AGENT,
    act_as_assistant,
    act_as_user_agent,
    browser,
    canvas_revision,
    drain,
    open_canvas,
    seed_accounts,
    seed_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL, patch_state
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("provider", ["claude", "codex", "assistant"])
async def test_switching_a_closed_chat_reopens_it_for_the_new_provider(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    canvas, conversation, session = await _closed_chat(
        client,
        store=store,
        provider="codex" if provider == "claude" else "claude",
        account=TEST_USER_EMAIL.upper() if provider == "claude" else TEST_USER_EMAIL,
    )
    revision = await canvas_revision(client, canvas_id=canvas)
    key = str(uuid.uuid4())
    operation = {
        "revision": revision,
        "operation": {"kind": "provider", "provider": provider},
    }
    chosen = await client.post(
        f"/api/canvases/{canvas}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert chosen.status_code == 200, chosen.text
    state = json_loads(chosen.content, dict[str, object])
    assert state["conversation_id"] == str(conversation)
    head = await client.get(f"/api/chats/{conversation}")
    assert head.status_code == 200, head.text
    assert json_loads(head.content, dict[str, object])["live"] is True
    assert json_loads(head.content, dict[str, object])["session_id"] == str(session)

    if provider == "assistant":
        act_as_assistant("writer")
    else:
        act_as_user_agent("writer")
    accepted = await drain(client, session_id=session, cli=provider)
    assert accepted.status_code == 200, accepted.text
    async with store.engine.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM inquiries WHERE agentsession_cli_session_id = $1",
                chat_session_id(conversation),
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT status FROM inquiries WHERE id = $1",
                session,
            )
            == "active"
        )

    browser()
    ended = await client.post(f"/api/sessions/{session}/end", json={})
    assert ended.status_code == 200, ended.text
    replay = await client.post(
        f"/api/canvases/{canvas}/operations",
        json=operation,
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200, replay.text
    head = await client.get(f"/api/chats/{conversation}")
    assert json_loads(head.content, dict[str, object])["live"] is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_closed_chat_without_a_switch_still_refuses_its_run(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    _, _, session = await _closed_chat(client, store=store)
    act_as_user_agent("writer")
    refused = await drain(client, session_id=session, cli="claude")
    assert refused.status_code == 409, refused.text
    detail = from_plain(
        json_loads(refused.content, dict[str, object])["detail"],
        dict[str, object],
    )
    assert detail["code"] == "session_ended"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("stale", [False, True])
async def test_a_noop_or_refused_switch_leaves_the_chat_closed(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    stale: bool,
) -> None:
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    canvas, conversation, _ = await _closed_chat(client, store=store)
    revision = await canvas_revision(client, canvas_id=canvas)
    chosen = await client.post(
        f"/api/canvases/{canvas}/operations",
        json={
            "revision": revision + int(stale),
            "operation": {
                "kind": "provider",
                "provider": "codex" if stale else "claude",
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert chosen.status_code == (409 if stale else 200), chosen.text
    head = await client.get(f"/api/chats/{conversation}")
    assert json_loads(head.content, dict[str, object])["live"] is False


async def _closed_chat(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    provider: str = "claude",
    account: str = TEST_USER_EMAIL,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed an assistant-origin chat and close it through the browser."""
    await seed_accounts(store)
    canvas = await open_canvas(client)
    conversation = uuid.uuid4()
    session = await seed_session(
        store,
        ASSISTANT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="assistant",
        cli_session_id=chat_session_id(conversation),
        account=account,
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_canvases SET metadata = metadata || "
            "jsonb_build_object('conversation_id', $2::text), state = state || "
            "jsonb_build_object('provider', $3::text) WHERE id = $1",
            canvas,
            str(conversation),
            provider,
        )
        await conn.execute(
            "INSERT INTO machines (name, updated_by, owner, confirmed_at, "
            "host_instance, last_heartbeat, facts) VALUES ('callisto', $1, $1, now(), "
            'gen_random_uuid(), now(), \'{"clis": ["claude", "codex"]}\')',
            USER_AGENT.email,
        )
    closed = await client.post(f"/api/sessions/{session}/end", json={})
    assert closed.status_code == 200, closed.text
    return canvas, conversation, session


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
