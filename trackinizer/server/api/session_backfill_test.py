"""Historical session imports preserve science-chat ownership and privacy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    USER_AGENT,
    act_as_other_agent,
    seed_accounts,
    seed_session,
)
from trackinizer.server.visuals.science_chats import read_starter
from trackinizer.wire.wire_science_chat import SCIENCE_CHAT_LABEL, chat_session_id


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "created",
    [
        "0001-01-01T00:00:00Z",
        "0001-01-01T01:00:00+01:00",
        "0001-01-01T00:00:00+01:00",
    ],
)
async def test_unrepresentable_backfill_is_rejected_before_writing(
    pglite_route_client: _Client,
    created: str,
    batch: bool,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    async with store.engine.acquire() as conn:
        before_rows = await conn.fetch("SELECT * FROM inquiries ORDER BY id")
        before_changes = await conn.fetch("SELECT * FROM change_log ORDER BY id")
    item = {
        "kind": "Issue",
        "title": "Unrepresentable history",
        "created": created,
        "idempotency_key": str(uuid.uuid4()),
    }
    if batch:
        response = await client.post(
            "/api/inquiries/batch",
            json={
                "items": [
                    {
                        "kind": "Issue",
                        "title": "Must not be partially created",
                        "idempotency_key": str(uuid.uuid4()),
                    },
                    item,
                ],
            },
        )
    else:
        response = await client.post("/api/inquiries/issue", json=item)

    assert response.status_code == 422, response.text
    async with store.engine.acquire() as conn:
        assert await conn.fetch("SELECT * FROM inquiries ORDER BY id") == before_rows
        assert (
            await conn.fetch("SELECT * FROM change_log ORDER BY id") == before_changes
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "created",
    [
        "0001-01-01T00:00:00.000001Z",
        "0001-01-01T01:00:00.000001+01:00",
        "2020-01-01T00:00:00Z",
    ],
)
async def test_finite_historical_dates_preserve_normal_edge_creation(
    pglite_route_client: _Client,
    created: str,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    ids: list[uuid.UUID] = []
    for title, instant in (("Historical", created), ("Current", None)):
        response = await client.post(
            "/api/inquiries/issue",
            json={"title": title, "created": instant},
        )
        assert response.status_code == 201, response.text
        ids.append(
            uuid.UUID(
                from_plain(json_loads(response.content, dict[str, object])["id"], str),
            ),
        )

    linked = await client.post(f"/api/edges/{ids[1]}/requires/{ids[0]}", json={})

    assert linked.status_code == 200, linked.text
    historical = await store.get_inquiry(ids[0])
    current = await store.get_inquiry(ids[1])
    assert historical is not None
    assert current is not None
    assert historical.created == datetime.fromisoformat(created)
    assert historical.created.tzinfo is not None
    assert [edge.id for edge in current.produced_by] == [historical.id]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("historical", [False, True])
async def test_backfilled_chat_creation_preserves_the_original_private_chat_authority(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
    historical: bool,
) -> None:
    """Later session records never change who owns an existing private chat."""
    client, store = pglite_route_client
    original, conversation = await _private_chat(store, monkeypatch=monkeypatch)
    act_as_other_agent("writer")
    before = await client.get(f"/api/sessions/{original}/records")
    assert before.status_code == 403, before.text
    payload = {
        "title": "Another account's session",
        "cli": "codex",
        "cli_session_id": chat_session_id(conversation),
    }
    if historical:
        payload["created"] = "2020-01-01T00:00:00Z"

    created = await client.post("/api/inquiries/agentsession", json=payload)
    after = await client.get(f"/api/sessions/{original}/records")
    async with store.engine.acquire() as conn:
        starter = await read_starter(
            conn,
            assistant=ASSISTANT_CONFIG.assistant,
            conversation_id=conversation,
        )

    observed = (
        f"{historical=}, create_status={created.status_code}, "
        f"private_read_before={before.status_code}, private_read_after={after.status_code}, "
        f"{starter=}"
    )
    assert created.status_code == (422 if historical else 201), observed
    assert after.status_code == 403, observed
    assert starter == USER_AGENT.email, observed


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_ending_an_ordinary_historical_session_cannot_move_it_under_a_chat(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing a historical session preserves the reserved chat-id boundary."""
    client, store = pglite_route_client
    original, conversation = await _private_chat(store, monkeypatch=monkeypatch)
    act_as_other_agent("writer")
    created = await client.post(
        "/api/inquiries/agentsession",
        json={
            "title": "Ordinary historical import",
            "cli": "codex",
            "created": "2020-01-01T00:00:00Z",
        },
    )
    assert created.status_code == 201, created.text
    session_id = from_plain(json_loads(created.content, dict[str, object])["id"], str)
    before = await client.get(f"/api/sessions/{original}/records")
    assert before.status_code == 403, before.text
    generic = await client.put(
        f"/api/agentsession/{session_id}/cli_session_id",
        json={"value": chat_session_id(conversation)},
    )
    assert generic.status_code == 403, generic.text

    ended = await client.post(
        f"/api/sessions/{session_id}/end",
        json={"cli_session_id": chat_session_id(conversation)},
    )
    after = await client.get(f"/api/sessions/{original}/records")
    async with store.engine.acquire() as conn:
        starter = await read_starter(
            conn,
            assistant=ASSISTANT_CONFIG.assistant,
            conversation_id=conversation,
        )

    observed = (
        f"generic_edit_status={generic.status_code}, end_status={ended.status_code}, "
        f"private_read_before={before.status_code}, private_read_after={after.status_code}, "
        f"{starter=}"
    )
    assert ended.status_code == 403, observed
    assert after.status_code == 403, observed
    assert starter == USER_AGENT.email, observed


async def _private_chat(
    store: Store,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create another account's private helper chat in an isolated test database."""
    monkeypatch.setattr(
        app.state,
        "config",
        replace(ASSISTANT_CONFIG, chat_orgs="individual"),
        raising=False,
    )
    await seed_accounts(store)
    conversation = uuid.uuid4()
    original = await seed_session(
        store,
        USER_AGENT,
        actor="original-chat",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'private' WHERE id = $1",
            original,
        )
    return original, conversation


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
