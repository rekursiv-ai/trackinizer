"""A canvas's own URL shows it, read only, to whom its chat lets in; others learn whom to ask."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, json_loads

import pytest

from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    OTHER_EMAIL,
    OTHER_USER_ID,
    browser,
    seed_accounts,
    seed_science_chat,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.store.core import Store


async def _chat_canvas(
    client: httpx2.AsyncClient,
    store: Store,
    *,
    audience: str = "open",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Make the test user's canvas for a new chat of theirs, in ``audience``."""
    conversation_id = uuid.uuid4()
    session_id = await seed_science_chat(store, conversation_id=conversation_id)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = $2 WHERE id = $1",
            session_id,
            audience,
        )
    made = await client.post(f"/api/canvases/for-chat/{conversation_id}")
    assert made.status_code == 200, made.text
    return (
        uuid.UUID(from_plain(json_loads(made.content, dict[str, object])["id"], str)),
        session_id,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_reader_of_the_canvas_chat_views_the_canvas_and_learns_its_owner(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    canvas_id, _ = await _chat_canvas(client, store)
    for user_id, email in ((None, TEST_USER_EMAIL), (OTHER_USER_ID, OTHER_EMAIL)):
        if user_id is None:
            browser()
        else:
            browser(user_id, email=email)
        viewed = await client.get(f"/api/canvases/{canvas_id}/view")
        assert viewed.status_code == 200, viewed.text
        body = json_loads(viewed.content, dict[str, object])
        assert body["owner"] == TEST_USER_EMAIL
        assert from_plain(body["canvas"], dict[str, object])["id"] == str(canvas_id)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("audience", ["private", "staff"])
async def test_one_the_chat_does_not_let_in_learns_whom_to_ask_and_nothing_else(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    canvas_id, _ = await _chat_canvas(client, store, audience=audience)
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    refused = await client.get(f"/api/canvases/{canvas_id}/view")
    assert refused.status_code == 403, refused.text
    assert json_loads(refused.content, dict[str, object]) == {
        "detail": f"Ask {TEST_USER_EMAIL} for access to this canvas.",
        "owner": TEST_USER_EMAIL,
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_canvas_with_no_chat_shows_to_its_owner_alone(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    browser()
    made = await client.post("/api/canvases/new")
    canvas_id = from_plain(json_loads(made.content, dict[str, object])["id"], str)
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    refused = await client.get(f"/api/canvases/{canvas_id}/view")
    assert refused.status_code == 403, refused.text
    unknown = await client.get(f"/api/canvases/{uuid.uuid4()}/view")
    assert unknown.status_code == 404, unknown.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_viewer_changes_nothing_on_the_canvas(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    await seed_accounts(store)
    canvas_id, _ = await _chat_canvas(client, store)
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    viewed = json_loads(
        (await client.get(f"/api/canvases/{canvas_id}/view")).content,
        dict[str, object],
    )
    revision = from_plain(
        from_plain(viewed["canvas"], dict[str, object])["revision"],
        int,
    )
    moved = await client.post(
        f"/api/canvases/{canvas_id}/operations",
        json={
            "revision": revision,
            "operation": {"kind": "show", "visual_type": "trax.subgraph"},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert moved.status_code == 404, moved.text
    paged = await client.put(
        f"/api/canvases/{canvas_id}/page",
        json={"route": "#/settings"},
    )
    assert paged.status_code == 404, paged.text


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
