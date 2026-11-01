"""A canvas's own URL shows it, read only, to whom its chat lets in; others learn whom to ask."""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from treekle import from_plain, immutable, json_loads

import pytest

from trackinizer.sagent.session.types import ContextState, UserMessage
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
    browser,
    chat_labels,
    seed_accounts,
    seed_science_chat,
    seed_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL, patch_state
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import chat_session_id


if TYPE_CHECKING:
    import httpx2

    from trackinizer.sagent.session.types import SessionRecord
    from trackinizer.server.api.canvas_test_support import Agent
    from trackinizer.server.store.core import Store


async def _record(store: Store, session_id: uuid.UUID, *records: SessionRecord) -> None:
    """Record ``records`` as the first part of ``session_id``'s transcript."""
    part = await store.upsert_session_manifest(
        session_id,
        name="chat.jsonl",
        metadata={},
        ir_id=uuid.uuid4(),
        format="claude",
        records=len(records),
    )
    _ = await store.append_session_records(
        session_id,
        [
            SessionRecordRow.of(
                session_id=session_id,
                part=part,
                idx=idx,
                record=record,
            )
            for idx, record in enumerate(records)
        ],
    )


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
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
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
@pytest.mark.parametrize(
    "opener",
    [ASSISTANT, USER_AGENT],
    ids=["assistant", "own_cli"],
)
async def test_a_viewer_reads_the_canvas_chat_whoever_opened_its_session(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    opener: Agent,
) -> None:
    """The chat that lets a viewer see the canvas is the one Chat finds and reads.

    A claude or codex run on the starter's machine opens its chat's session under the
    starter's own key, not the assistant's; the viewer's Chat finds it all the same.
    """
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    await seed_accounts(store)
    conversation_id = uuid.uuid4()
    session_id = await seed_session(
        store,
        opener,
        actor=f"chat-{conversation_id.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation_id),
        account=TEST_USER_EMAIL,
        labels=chat_labels(),
    )
    browser()
    made = await client.post(f"/api/canvases/for-chat/{conversation_id}")
    assert made.status_code == 200, made.text
    canvas_id = from_plain(json_loads(made.content, dict[str, object])["id"], str)

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    viewed = await client.get(f"/api/canvases/{canvas_id}/view")
    head = await client.get(f"/api/chats/{conversation_id}")
    records = await client.get(f"/api/sessions/{session_id}/records")

    assert viewed.status_code == 200, viewed.text
    assert head.status_code == 200, head.text
    body = json_loads(head.content, dict[str, object])
    assert (body["session_id"], body["account"], body["forks_on_typing"]) == (
        str(session_id),
        TEST_USER_EMAIL,
        False,
    )
    assert records.status_code == 200, records.text


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("audience", ["open", "private"])
async def test_a_viewer_names_the_chat_by_what_its_owners_list_reads(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
) -> None:
    """The head carries what the owner's session list names the chat by, to its readers.

    A canvas nobody named goes by its chat's goal, else its first line, which the
    viewer's own list cannot carry: the session is not theirs. One who does not read
    the chat learns none of its words.
    """
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
    await seed_accounts(store)
    conversation_id = uuid.uuid4()
    session_id = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation_id.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation_id),
        account=TEST_USER_EMAIL,
        labels=chat_labels(),
    )
    await _record(
        store,
        session_id,
        UserMessage(
            content=f"[chat] {TEST_USER_EMAIL}: Hi\nTrackinizer context (verify with trax): x",
            extra=immutable({"$inbound": {"room": "chat", "source": TEST_USER_EMAIL}}),
        ),
        ContextState(
            kind="goal_status",
            extra=immutable({"attachment": {"condition": "CI is green"}}),
        ),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = $2 WHERE id = $1",
            session_id,
            audience,
        )
    browser()
    listed = json_loads((await client.get("/api/sessions")).content, dict[str, object])
    [row] = [
        each
        for each in from_plain(listed["sessions"], list[dict[str, object]])
        if each["id"] == str(session_id)
    ]

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    head = json_loads(
        (await client.get(f"/api/chats/{conversation_id}")).content,
        dict[str, object],
    )

    said = ("title", "cli", "goal", "first_line")
    assert (row["goal"], row["first_line"]) == ("CI is green", "Hi")
    assert {key: head.get(key) for key in said} == (
        {key: row[key] for key in said}
        if audience == "open"
        else {"title": row["title"], "cli": "claude", "goal": None, "first_line": None}
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("audience", ["private", "staff"])
async def test_one_the_chat_does_not_let_in_learns_whom_to_ask_and_nothing_else(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
    audience: str,
) -> None:
    client, store = pglite_route_client
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
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
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
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
    patch_state(monkeypatch, "config", ASSISTANT_CONFIG)
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
