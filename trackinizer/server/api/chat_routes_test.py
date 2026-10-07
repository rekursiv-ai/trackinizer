"""Conversations over HTTP: owner-scoped history, sending, replies, and push."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncio
import uuid

import pytest

from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api.app import app
from trackinizer.server.api.chat_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_KEY_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    conversation_of,
    drain,
    open_workspace,
    revision_of,
    seed_accounts,
    send_chat,
    show_chat,
    start_session,
)
from trackinizer.server.api.conftest import TEST_USER_EMAIL, TEST_USER_ID
from trackinizer.server.chat_hub import (
    ChatHub,
    DeletedFrame,
    DeliveredFrame,
    MessageFrame,
    StatusFrame,
    WorkspaceFrame,
)
from trackinizer.server.inbound import InboundQueue


if TYPE_CHECKING:
    import httpx2

    from trackinizer.server.chat_hub import Frame
    from trackinizer.server.store.core import Store


@pytest.fixture(autouse=True)
def serving_hub(monkeypatch: pytest.MonkeyPatch) -> ChatHub:
    """Serve with scout configured, and a fresh poller table and event hub."""
    hub = ChatHub()
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)
    monkeypatch.setattr(app.state, "hub", hub, raising=False)
    return hub


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_conversation_is_created_continued_listed_read_and_deleted(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A null conversation_id starts one; a given one continues it."""
    client, store = pglite_route_client
    kb, workspace_id, chat_id, conversation_id = await _talk(
        client,
        store=store,
        text="  Show   me\n the   timeline " + "z" * 100,
    )
    text = "  Show   me\n the   timeline " + "z" * 100
    second = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="and the graph",
        conversation_id=conversation_id,
    )
    assert conversation_of(second) == conversation_id
    receipt = from_plain(loads(second.content), dict[str, object])
    assert set(receipt) == {"session_id", "conversation_id", "message"}
    message = from_plain(receipt["message"], dict[str, object])
    assert (message["seq"], message["role"], message["author"]) == (
        2,
        "user",
        TEST_USER_EMAIL,
    )

    listed = from_plain(loads((await client.get("/api/chats")).content), list[object])
    assert len(listed) == 1
    summary = from_plain(listed[0], dict[str, object])
    assert summary["id"] == str(conversation_id)
    assert summary["title"] == ("Show me the timeline " + "z" * 100)[:80]
    assert summary["partner_actor"] == KB_ACTOR
    assert summary["workspace_id"] == str(workspace_id)

    thread = await _thread(client, conversation_id=conversation_id)
    assert thread["partner_session_id"] == str(kb)
    assert thread["earlier"] is False
    assert _texts(thread) == [text, "and the graph"]
    assert _texts(
        await _thread(client, conversation_id=conversation_id, after_seq=1),
    ) == [
        "and the graph",
    ]

    assert (await client.delete(f"/api/chats/{conversation_id}")).status_code == 204
    assert (await client.get(f"/api/chats/{conversation_id}")).status_code == 404
    assert loads((await client.get("/api/chats")).content) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_conversation_is_404_and_api_keys_are_refused(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Ownership and principal checks hold on every browser route."""
    client, store = pglite_route_client
    _, workspace_id, _, conversation_id = await _talk(client, store=store)
    path = f"/api/chats/{conversation_id}"

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    assert (await client.get(path)).status_code == 404
    assert (await client.delete(path)).status_code == 404
    assert loads((await client.get("/api/chats")).content) == []
    other_workspace = await open_workspace(
        client,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    other_chat = await show_chat(client, workspace_id=other_workspace)
    foreign = await send_chat(
        client,
        workspace_id=other_workspace,
        chat_id=other_chat,
        text="mine now",
        conversation_id=conversation_id,
    )
    assert foreign.status_code == 404
    assert (
        await client.get(f"/api/workspaces/{workspace_id}/events")
    ).status_code == 404

    browser()
    second_canvas = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, is_default, state) "
            "VALUES ($1, $2, FALSE, '{\"visuals\": []}')",
            second_canvas,
            TEST_USER_ID,
        )
    wrong_canvas = await client.post(
        f"/api/workspaces/{second_canvas}/messages",
        json={"text": "x", "conversation_id": str(conversation_id)},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert wrong_canvas.status_code == 404

    act_as_user_agent()
    assert (await client.get("/api/chats")).status_code == 403
    assert (await client.get(path)).status_code == 403
    assert (await client.delete(path)).status_code == 403
    assert (
        await client.get(f"/api/workspaces/{workspace_id}/events")
    ).status_code == 403
    browser()
    assert (await client.get(path)).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_send_replay_stores_the_message_once_and_queues_it_once(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A retried key and request returns the original receipt and nothing more."""
    client, store = pglite_route_client
    kb, workspace_id, chat_id, conversation_id = await _talk(
        client,
        store=store,
        text="once",
    )
    act_as_assistant()
    assert _drained(await drain(client, session_id=kb)) == ["once"]
    browser()
    key = uuid.uuid4()
    first = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="retry me",
        key=key,
    )
    again = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="retry me",
        key=key,
    )
    assert again.status_code == 200
    assert loads(again.content) == loads(first.content)
    assert _texts(await _thread(client, conversation_id=conversation_of(first))) == [
        "retry me",
    ]
    act_as_assistant()
    assert _drained(await drain(client, session_id=kb)) == ["retry me"]
    browser()
    different = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="different",
        key=key,
    )
    assert different.status_code == 409
    assert conversation_of(first) != conversation_id
    other_conversation = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="retry me",
        conversation_id=conversation_id,
        key=key,
    )
    assert other_conversation.status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_replay_answers_after_the_partner_went_away(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The user's receipt does not depend on the partner still being there."""
    client, store = pglite_route_client
    kb, workspace_id, chat_id, _ = await _talk(client, store=store)
    key = uuid.uuid4()
    first = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="before it left",
        key=key,
    )
    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    browser()
    assert (
        await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="after it left",
        )
    ).status_code == 409
    replay = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="before it left",
        key=key,
    )
    assert replay.status_code == 200
    assert loads(replay.content) == loads(first.content)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_sends_with_one_key_store_one_message_and_replay_one(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Both callers get the same receipt; one line is stored and one queued."""
    client, store = pglite_route_client
    kb, workspace_id, chat_id, talk_conversation = await _talk(client, store=store)
    act_as_assistant()
    await drain(client, session_id=kb)
    browser()
    key = uuid.uuid4()
    first, second = await asyncio.gather(
        send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="race",
            key=key,
        ),
        send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="race",
            key=key,
        ),
    )
    assert (first.status_code, second.status_code) == (200, 200)
    assert loads(first.content) == loads(second.content)
    assert sorted(_ids(await client.get("/api/chats"))) == sorted(
        [str(conversation_of(first)), str(talk_conversation)],
    )
    assert _texts(await _thread(client, conversation_id=conversation_of(first))) == [
        "race",
    ]
    act_as_assistant()
    assert _drained(await drain(client, session_id=kb)) == ["race"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_message_needs_a_non_space_character_and_at_most_16384(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Whitespace alone and an oversize message are refused before anything is stored."""
    client, store = pglite_route_client
    _, workspace_id, chat_id, talk_conversation = await _talk(client, store=store)
    for text in ("", "   ", "\n\t", "x" * 16_385):
        refused = await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text=text,
        )
        assert refused.status_code == 422
    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="x" * 16_384,
    )
    assert sent.status_code == 200
    assert sorted(_ids(await client.get("/api/chats"))) == sorted(
        [str(conversation_of(sent)), str(talk_conversation)],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_reply_is_accepted_only_from_the_partner_sessions_key(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An answer is stored once for its author; every other caller is refused."""
    client, store = pglite_route_client
    kb, _, _, conversation_id = await _talk(client, store=store)
    path = f"/api/chats/{conversation_id}/messages"
    answer = {"text": "Here is Issue#1.", "kind": "answer"}

    for refused in (act_as_other_agent, act_as_user_agent, browser):
        refused()
        assert (await client.post(path, json=answer)).status_code == 403
    act_as_assistant()
    unknown = f"/api/chats/{uuid.uuid4()}/messages"
    assert (await client.post(unknown, json=answer)).status_code == 404
    assert (
        await client.post(path, json={"text": " ", "kind": "answer"})
    ).status_code == 422

    posted = await client.post(path, json=answer)
    assert posted.status_code == 200
    stored = from_plain(loads(posted.content), dict[str, object])
    assert (stored["seq"], stored["role"], stored["author"]) == (
        2,
        "assistant",
        KB_ACTOR,
    )
    browser()
    assert _texts(await _thread(client, conversation_id=conversation_id)) == [
        "hello",
        "Here is Issue#1.",
    ]

    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    assert (await client.post(path, json=answer)).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_status_is_pushed_and_never_stored(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    serving_hub: ChatHub,
) -> None:
    """Statuses reach the stream, are kept for a reload, and leave the thread alone."""
    client, store = pglite_route_client
    _, workspace_id, _, conversation_id = await _talk(client, store=store)
    path = f"/api/chats/{conversation_id}/messages"
    with serving_hub.subscribe(workspace_id) as stream:
        act_as_assistant()
        status = await client.post(path, json={"text": "thinking", "kind": "status"})
        assert status.status_code == 200
        assert loads(status.content) is None
        assert serving_hub.status_of(conversation_id) == "thinking"
        assert (
            await client.post(path, json={"text": "", "kind": "status"})
        ).status_code == 200
        assert serving_hub.status_of(conversation_id) == ""
        frames = [stream.queue.get_nowait() for _ in range(2)]
        assert stream.queue.empty()
    assert [
        (frame.conversation_id, frame.text)
        for frame in frames
        if isinstance(frame, StatusFrame)
    ] == [(conversation_id, "thinking"), (conversation_id, "")]
    browser()
    assert _texts(await _thread(client, conversation_id=conversation_id)) == ["hello"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_stream_carries_workspace_message_and_delivered_frames(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    serving_hub: ChatHub,
) -> None:
    """Every change reaches the owner's stream after it commits; delivered has seq."""
    client, store = pglite_route_client
    kb, workspace_id, chat_id, conversation_id = await _talk(client, store=store)
    act_as_assistant()
    await drain(client, session_id=kb)
    with serving_hub.subscribe(workspace_id) as stream:
        browser()
        focused = await client.post(
            f"/api/workspaces/{workspace_id}/operations",
            json={
                "revision": await revision_of(client, workspace_id=workspace_id),
                "operation": {"kind": "focus", "instance_id": str(chat_id)},
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert focused.status_code == 200
        await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="next",
            conversation_id=conversation_id,
        )
        act_as_assistant()
        await drain(client, session_id=kb)
        await client.post(
            f"/api/chats/{conversation_id}/messages",
            json={"text": "done", "kind": "answer"},
        )
        frames: list[Frame | None] = []
        while not stream.queue.empty():
            frames.append(stream.queue.get_nowait())
    assert [frame.type for frame in frames if frame is not None] == [
        "workspace",
        "message",
        "delivered",
        "message",
    ]
    workspace_frame = frames[0]
    assert isinstance(workspace_frame, WorkspaceFrame)
    assert workspace_frame.state.partner is not None
    assert workspace_frame.state.partner.actor == KB_ACTOR
    assert workspace_frame.state.assistant == KB_ACTOR
    delivered = frames[2]
    sent = frames[1]
    assert isinstance(delivered, DeliveredFrame)
    assert isinstance(sent, MessageFrame)
    assert delivered.seq == sent.message.seq == 2
    assert serving_hub.delivered_of(conversation_id) == 2
    assert all(frame is not None and frame.t > 0 for frame in frames)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_thread_without_a_cursor_reads_the_newest_lines(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """The default read is the tail, so a long chat opens at its end."""
    client, store = pglite_route_client
    _, _, _, conversation_id = await _talk(client, store=store)
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_messages (id, conversation_id, seq, role, author, text) "
            "SELECT gen_random_uuid(), $1, n, 'assistant', 'a', 'line ' || n "
            "FROM generate_series(2, 502) AS n",
            conversation_id,
        )
    thread = await _thread(client, conversation_id=conversation_id)
    messages = [
        from_plain(m, dict[str, object])
        for m in from_plain(thread["messages"], list[object])
    ]
    assert [m["seq"] for m in messages][:2] == [3, 4]
    assert len(messages) == 500
    assert thread["earlier"] is True
    after = await _thread(client, conversation_id=conversation_id, after_seq=500)
    assert [
        from_plain(m, dict[str, object])["seq"]
        for m in from_plain(after["messages"], list[object])
    ] == [
        501,
        502,
    ]
    assert after["earlier"] is True


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_sessions_key_reads_its_conversation_and_no_other_key_does(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """An assistant that lost its memory of a thread reseeds from the stored one."""
    client, store = pglite_route_client
    kb, _, chat_id, conversation_id = await _talk(client, store=store)
    workspace_id = await _thread_workspace(client, conversation_id=conversation_id)
    path = f"/api/chats/{conversation_id}"
    await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="second",
        conversation_id=conversation_id,
    )

    for role in ("viewer", "writer"):
        act_as_assistant(role)
        thread = await client.get(path)
        assert thread.status_code == 200
        body = from_plain(loads(thread.content), dict[str, object])
        assert _texts(body) == ["hello", "second"]
        assert body["partner_session_id"] == str(kb)
        after = await client.get(path, params={"after_seq": 1})
        assert _texts(from_plain(loads(after.content), dict[str, object])) == ["second"]
        assert (await client.get(f"/api/chats/{uuid.uuid4()}")).status_code == 404
        for refused in (act_as_other_agent, act_as_user_agent):
            refused(role)
            assert (await client.get(path)).status_code == 403
    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    assert (await client.get(path)).status_code == 404
    browser()
    assert (await client.get(path)).status_code == 200

    act_as_assistant()
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    assert (await client.get(path)).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_revoked_partner_key_can_no_longer_read_the_conversation(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """Revoking the assistant's key ends its access to every user's chat."""
    client, store = pglite_route_client
    _, _, _, conversation_id = await _talk(client, store=store)
    act_as_assistant()
    assert (await client.get(f"/api/chats/{conversation_id}")).status_code == 200
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            KB_KEY_ID,
        )
    assert (await client.get(f"/api/chats/{conversation_id}")).status_code == 403


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_full_partner_queue_refuses_a_message_instead_of_dropping_one(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-pressure, not loss: the sender is told, and nothing is stored."""
    client, store = pglite_route_client
    monkeypatch.setattr(
        app.state,
        "inbound",
        InboundQueue(max_per_session=2),
        raising=False,
    )
    kb, workspace_id, chat_id, conversation_id = await _talk(
        client,
        store=store,
        text="one",
    )
    await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="two",
        conversation_id=conversation_id,
    )
    busy = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="three",
        conversation_id=conversation_id,
    )
    assert busy.status_code == 409
    assert "busy" in busy.text
    assert _texts(await _thread(client, conversation_id=conversation_id)) == [
        "one",
        "two",
    ]
    act_as_assistant()
    assert _drained(await drain(client, session_id=kb)) == ["one", "two"]
    browser()
    assert (
        await send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat_id,
            text="three",
            conversation_id=conversation_id,
        )
    ).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_a_conversation_tells_the_canvas_and_forgets_what_it_kept(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
    serving_hub: ChatHub,
) -> None:
    """A deleted conversation leaves every open tab."""
    client, store = pglite_route_client
    _, workspace_id, _, conversation_id = await _talk(client, store=store)
    serving_hub.set_status(conversation_id, text="thinking")
    serving_hub.set_delivered(conversation_id, seq=1)
    with serving_hub.subscribe(workspace_id) as stream:
        deleted = await client.delete(f"/api/chats/{conversation_id}")
        assert deleted.status_code == 204
        frames: list[Frame | None] = []
        while not stream.queue.empty():
            frames.append(stream.queue.get_nowait())
    assert [
        (frame.type, frame.conversation_id)
        for frame in frames
        if isinstance(frame, DeletedFrame)
    ] == [("deleted", conversation_id)]
    assert len(frames) == 1
    assert serving_hub.status_of(conversation_id) == ""
    assert serving_hub.delivered_of(conversation_id) == 0
    assert (await client.delete(f"/api/chats/{conversation_id}")).status_code == 404


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_partner_key_learns_which_conversations_it_owes_an_answer(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """After a restart: the live partner's conversations whose last line is the user's."""
    client, store = pglite_route_client
    _, workspace_id, chat_id, first = await _talk(client, store=store, text="one")
    second = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="two"),
    )
    browser()
    await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="one again",
        conversation_id=first,
    )

    act_as_assistant()
    owed = await _awaiting(client)
    assert [
        (row["conversation_id"], row["workspace_id"], row["seq"]) for row in owed
    ] == [
        (str(second), str(workspace_id), 1),
        (str(first), str(workspace_id), 2),
    ]

    answered = await client.post(
        f"/api/chats/{first}/messages",
        json={"text": "done", "kind": "answer"},
    )
    assert answered.status_code == 200
    assert [row["conversation_id"] for row in await _awaiting(client)] == [str(second)]
    status = await client.post(
        f"/api/chats/{second}/messages",
        json={"text": "thinking", "kind": "status"},
    )
    assert status.status_code == 200
    assert [row["conversation_id"] for row in await _awaiting(client)] == [str(second)]
    await client.post(
        f"/api/chats/{second}/messages",
        json={"text": "done too", "kind": "answer"},
    )
    assert await _awaiting(client) == []
    browser()
    await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="more",
        conversation_id=second,
    )
    act_as_assistant()
    assert [
        (row["conversation_id"], row["seq"]) for row in await _awaiting(client)
    ] == [
        (str(second), 3),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_a_live_partner_key_is_told_of_owed_answers(
    pglite_route_client: tuple[httpx2.AsyncClient, Store],
) -> None:
    """A browser is refused; another key, a revoked one, and an ended session get none."""
    client, store = pglite_route_client
    kb, _, _, conversation_id = await _talk(client, store=store)
    browser()
    assert (await client.get("/api/chats/awaiting")).status_code == 403
    for role in ("viewer", "writer"):
        act_as_other_agent(role)
        assert await _awaiting(client) == []
        act_as_user_agent(role)
        assert await _awaiting(client) == []
    act_as_assistant("writer")
    assert [row["conversation_id"] for row in await _awaiting(client)] == [
        str(conversation_id),
    ]
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() WHERE id = $1",
            KB_KEY_ID,
        )
    assert await _awaiting(client) == []
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE api_keys SET revoked_at = NULL WHERE id = $1",
            KB_KEY_ID,
        )
    ended = await client.post(f"/api/sessions/{kb}/end", json={"status": "completed"})
    assert ended.status_code == 200
    assert await _awaiting(client) == []


async def _thread(
    client: httpx2.AsyncClient,
    *,
    conversation_id: uuid.UUID,
    after_seq: int | None = None,
) -> dict[str, object]:
    response = await client.get(
        f"/api/chats/{conversation_id}",
        params={} if after_seq is None else {"after_seq": after_seq},
    )
    assert response.status_code == 200
    return from_plain(loads(response.content), dict[str, object])


def _texts(thread: dict[str, object]) -> list[str]:
    return [
        from_plain(from_plain(message, dict[str, object])["text"], str)
        for message in from_plain(thread["messages"], list[object])
    ]


def _drained(response: httpx2.Response) -> list[str]:
    body = from_plain(loads(response.content), dict[str, object])
    return [
        from_plain(from_plain(message, dict[str, object])["text"], str)
        for message in from_plain(body["messages"], list[object])
    ]


def _ids(response: httpx2.Response) -> list[str]:
    return [
        from_plain(from_plain(row, dict[str, object])["id"], str)
        for row in from_plain(loads(response.content), list[object])
    ]


async def _talk(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    text: str = "hello",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed accounts, a live scout, and one conversation on the user's canvas."""
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id = await open_workspace(client)
    chat_id = await show_chat(client, workspace_id=workspace_id)
    conversation_id = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text=text),
    )
    return kb, workspace_id, chat_id, conversation_id


async def _thread_workspace(
    client: httpx2.AsyncClient,
    *,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Name the canvas a conversation of the installed browser is on."""
    listed = from_plain(loads((await client.get("/api/chats")).content), list[object])
    row = next(
        from_plain(item, dict[str, object])
        for item in listed
        if from_plain(item, dict[str, object])["id"] == str(conversation_id)
    )
    return uuid.UUID(from_plain(row["workspace_id"], str))


async def _awaiting(client: httpx2.AsyncClient) -> list[dict[str, object]]:
    response = await client.get("/api/chats/awaiting")
    assert response.status_code == 200
    return [
        from_plain(row, dict[str, object])
        for row in from_plain(loads(response.content), list[object])
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
