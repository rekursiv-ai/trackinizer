"""An owner's science chat can use its canvas without ordinary run placement."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    OTHER_AGENT,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    act_as_other_agent,
    act_as_user_agent,
    open_workspace,
    seed_accounts,
    seed_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


async def _bind_chat(
    store: Store,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    cli: str = "codex",
) -> None:
    """Bind an existing station to its chat, as the browser's provider choice does."""
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET station = station || "
            "jsonb_build_object('conversation_id', $2::text), state = state || "
            "jsonb_build_object('provider', $3::text) WHERE id = $1",
            workspace_id,
            str(conversation_id),
            cli,
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("cli", ["claude", "codex"])
@pytest.mark.parametrize("opener", ["run-key", "assistant", "other-owner-key"])
async def test_an_owners_science_chat_reads_and_operates_its_canvas_without_placement(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    cli: str,
    opener: str,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    workspace_id = await open_workspace(client)
    conversation_id = uuid.uuid4()
    await _bind_chat(
        store,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        cli=cli,
    )
    session_id = await seed_session(
        store,
        ASSISTANT if opener == "assistant" else USER_AGENT,
        cli=cli,
        cli_session_id=chat_session_id(conversation_id),
        account=TEST_USER_EMAIL,
        labels=[SCIENCE_CHAT_LABEL],
        drained_by=cli,
    )

    act_as_user_agent()
    if opener == "other-owner-key":
        key_id = uuid.uuid4()
        async with store.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
                "VALUES ($1, $2, 'other key', 'hash', 'other', 'viewer')",
                key_id,
                TEST_USER_ID,
            )
        install_identity(make_test_identity(api_key_id=key_id, role="viewer"))
    read = await client.get(f"/api/workspaces/{workspace_id}")
    assert read.status_code == 200, read.text
    state = json_loads(read.content, dict[str, object])
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": from_plain(state["revision"], int),
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": str(session_id),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert shown.status_code == 200, shown.text
    visuals = from_plain(
        json_loads(shown.content, dict[str, object])["visuals"],
        list[dict[str, object]],
    )
    assert any(visual["type"] == "trax.subgraph" for visual in visuals)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "case",
    [
        "another-chat",
        "no-chat",
        "foreign-key",
        "foreign-station",
        "unlabelled",
        "ended",
        "revoked",
        "no-poller",
        "wrong-cli",
    ],
)
async def test_a_science_chat_gives_no_canvas_access_without_its_owned_live_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    case: str,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await open_workspace(
        client,
        user_id=OTHER_USER_ID if case == "foreign-station" else TEST_USER_ID,
        email=OTHER_EMAIL if case == "foreign-station" else TEST_USER_EMAIL,
    )
    conversation_id = uuid.uuid4()
    if case != "no-chat":
        await _bind_chat(
            store,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        )
    session_id = await seed_session(
        store,
        OTHER_AGENT if case == "foreign-key" else USER_AGENT,
        cli_session_id=chat_session_id(
            uuid.uuid4() if case == "another-chat" else conversation_id,
        ),
        labels=[] if case == "unlabelled" else [SCIENCE_CHAT_LABEL],
        drained_by="claude" if case == "wrong-cli" else "codex",
    )
    if case == "no-poller":
        inbound = app.state.inbound
        assert isinstance(inbound, InboundQueue)
        inbound.forget_poller(session_id)
    async with store.engine.acquire() as conn:
        if case == "ended":
            await conn.execute(
                "UPDATE inquiries SET status = 'complete', agentsession_ended = now() "
                "WHERE id = $1",
                session_id,
            )
        elif case == "revoked":
            await conn.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = $1",
                USER_AGENT.key_id,
            )

    if case == "foreign-key":
        act_as_other_agent()
    else:
        act_as_user_agent()
    read = await client.get(f"/api/workspaces/{workspace_id}")
    shown = await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": 0,
            "operation": {
                "kind": "show",
                "visual_type": "trax.subgraph",
                "record_id": str(session_id),
            },
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    expected = 404 if case in {"foreign-key", "foreign-station"} else 403
    assert read.status_code == expected, read.text
    assert shown.status_code == expected, shown.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
