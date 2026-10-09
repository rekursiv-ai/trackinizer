"""Science chat over the whole app: posting a line, a person's history, a link's session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncio
import json
import uuid

from fastapi import Request

import pytest

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage
from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api import workspace_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_USER_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    conversation_of,
    drain,
    open_science_chat,
    open_workspace,
    revision_of,
    seed_accounts,
    send_chat,
    show_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.route_iter import iter_routes
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import (
    CHAT_HELPER_CLI,
    SCIENCE_CHAT_LABEL,
    chat_session_id,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx2

    from trackinizer.server.store.core import Store


type _Client = tuple[httpx2.AsyncClient, Store]


@pytest.fixture(autouse=True)
def assistant_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve with scout configured and a fresh poller table."""
    monkeypatch.setattr(app.state, "config", ASSISTANT_CONFIG, raising=False)
    monkeypatch.setattr(app.state, "inbound", InboundQueue(), raising=False)


async def _canvas(
    client: httpx2.AsyncClient,
    *,
    other: bool = False,
) -> tuple[
    uuid.UUID,
    uuid.UUID,
]:
    """Open a user's canvas with its Chat visual; return both ids."""
    if other:
        workspace_id = await open_workspace(
            client,
            user_id=OTHER_USER_ID,
            email=OTHER_EMAIL,
        )
    else:
        workspace_id = await open_workspace(client)
    return workspace_id, await show_chat(client, workspace_id=workspace_id)


async def _drained(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> list[dict[str, object]]:
    """Drain a session as the assistant; return its messages."""
    act_as_assistant()
    response = await drain(client, session_id=session_id)
    assert response.status_code == 200
    return from_plain(
        from_plain(loads(response.content), dict[str, object])["messages"],
        list[dict[str, object]],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_writer_starts_a_conversation_and_the_assistant_hears_the_line(
    pglite_route_client: _Client,
) -> None:
    """The answer is the conversation id at once; the sender is the identity."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    key = uuid.uuid4()

    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="what do we know about tails?",
        key=key,
    )

    assert sent.status_code == 200
    receipt = from_plain(loads(sent.content), dict[str, object])
    # A new conversation is named by its key, and has no session until the
    # assistant opens it.
    assert receipt == {"conversation_id": str(key), "session_id": None}
    [heard] = await _drained(client, session_id=kb)
    assert heard["text"] == "what do we know about tails?"
    assert (heard["source"], heard["source_role"]) == (TEST_USER_EMAIL, "writer")
    context = from_plain(heard["context"], dict[str, object])
    assert (context["conversation_id"], context["workspace_id"]) == (
        str(key),
        str(workspace_id),
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_viewer_cannot_post_and_nothing_is_queued(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)

    install_identity(make_test_identity(api_key_id=None, role="viewer"))
    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="let me in",
    )

    assert refused.status_code == 403
    assert await _drained(client, session_id=kb) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_cannot_post_through_a_canvas(
    pglite_route_client: _Client,
) -> None:
    """Only a signed-in browser posts; an agent key is not a poster."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)

    act_as_user_agent("writer")
    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="as a key",
    )

    assert refused.status_code == 403
    assert await _drained(client, session_id=kb) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_poster_is_the_identity_and_the_body_cannot_name_one(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    browser()

    forged = await client.post(
        "/api/chats",
        json={
            "workspace_id": str(workspace_id),
            "text": "hi",
            "chat_instance_id": str(chat_id),
            "source": "ceo@example.com",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    honest = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hi",
    )

    assert forged.status_code == 422
    assert honest.status_code == 200
    [heard] = await _drained(client, session_id=kb)
    assert heard["source"] == TEST_USER_EMAIL


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_retry_under_one_key_queues_the_line_once(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    key = uuid.uuid4()

    first = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="once",
        key=key,
    )
    retry = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="once",
        key=key,
    )

    assert (first.status_code, retry.status_code) == (200, 200)
    assert conversation_of(first) == conversation_of(retry) == key
    assert len(await _drained(client, session_id=kb)) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_reused_for_another_line_is_refused_and_queues_nothing(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    key = uuid.uuid4()
    first = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="first",
        key=key,
    )
    other = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="a different line",
        key=key,
    )

    assert (first.status_code, other.status_code) == (200, 409)
    assert [m["text"] for m in await _drained(client, session_id=kb)] == ["first"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_post_with_no_assistant_running_is_refused(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id, chat_id = await _canvas(client)

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="anyone there",
    )

    assert refused.status_code == 409


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_writer_posts_into_the_first_ones_open_chat(
    pglite_route_client: _Client,
) -> None:
    """The line joins the chat's own queue, attested to the second writer."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    first_canvas, first_chat = await _canvas(client)
    started = conversation_of(
        await send_chat(
            client,
            workspace_id=first_canvas,
            chat_id=first_chat,
            text="hello from ada",
        ),
    )
    chat = await open_science_chat(client, store, conversation_id=started)
    assert [m["text"] for m in await _drained(client, session_id=kb)] == [
        "hello from ada",
    ]

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    second_canvas, second_chat = await _canvas(client, other=True)
    joined = await send_chat(
        client,
        workspace_id=second_canvas,
        chat_id=second_chat,
        text="and from grace",
        conversation_id=started,
    )

    assert joined.status_code == 200
    assert from_plain(loads(joined.content), dict[str, object]) == {
        "conversation_id": str(started),
        "session_id": str(chat),
    }
    assert await _drained(client, session_id=kb) == []
    [heard] = await _drained(client, session_id=chat)
    assert (heard["text"], heard["source"]) == ("and from grace", OTHER_EMAIL)
    context = from_plain(heard["context"], dict[str, object])
    # The line carries its poster's own canvas, not the starter's.
    assert context["workspace_id"] == str(second_canvas)
    assert context["conversation_id"] == str(started)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_the_assistant_closed_is_reopened_through_its_service_session(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="a"),
    )
    chat = await open_science_chat(client, store, conversation_id=started)
    act_as_assistant("writer")
    ended = await client.post(f"/api/sessions/{chat}/end", json={"status": "completed"})
    assert ended.status_code == 200
    await _drained(client, session_id=kb)

    browser()
    again = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="b",
        conversation_id=started,
    )

    assert again.status_code == 200
    assert from_plain(loads(again.content), dict[str, object])["session_id"] is None
    [heard] = await _drained(client, session_id=kb)
    assert heard["text"] == "b"
    context = from_plain(heard["context"], dict[str, object])
    assert context["conversation_id"] == str(started)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_lines_still_queued_when_a_chat_is_closed_go_to_the_assistants_service_session(
    pglite_route_client: _Client,
) -> None:
    """A post answered 200 is not lost to the close that raced it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="a"),
    )
    chat = await open_science_chat(client, store, conversation_id=started)
    await _drained(client, session_id=kb)
    browser()
    joined = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="b",
        conversation_id=started,
    )
    assert from_plain(loads(joined.content), dict[str, object])["session_id"] == str(
        chat,
    )

    act_as_assistant("writer")
    ended = await client.post(f"/api/sessions/{chat}/end", json={"status": "completed"})
    assert ended.status_code == 200

    [heard] = await _drained(client, session_id=kb)
    assert (heard["text"], heard["source"]) == ("b", TEST_USER_EMAIL)
    context = from_plain(heard["context"], dict[str, object])
    assert context["conversation_id"] == str(started)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_canvas_is_not_found(pglite_route_client: _Client) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    _, chat_id = await _canvas(client)
    foreign, _ = await _canvas(client, other=True)

    browser()
    refused = await send_chat(
        client,
        workspace_id=foreign,
        chat_id=chat_id,
        text="in your canvas",
    )

    assert refused.status_code == 404


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_conversation_is_found_by_its_id_once_the_assistant_opened_it(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    browser()
    path = f"/api/chats/{conversation}"
    assert (await client.get(path)).status_code == 404

    chat = await open_science_chat(client, store, conversation_id=conversation)
    # Every signed-in user reads any science chat's head, a viewer included.
    install_identity(
        make_test_identity(
            user_id=OTHER_USER_ID,
            api_key_id=None,
            email=OTHER_EMAIL,
            role="viewer",
        ),
    )
    found = from_plain(loads((await client.get(path)).content), dict[str, object])
    assert found["session_id"] == str(chat)
    assert (found["account"], found["live"]) == (TEST_USER_EMAIL, True)

    act_as_assistant("writer")
    await client.post(f"/api/sessions/{chat}/end", json={"status": "completed"})
    browser()
    closed = from_plain(loads((await client.get(path)).content), dict[str, object])
    assert closed["live"] is False


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_session_that_only_carries_the_id_is_not_the_conversation(
    pglite_route_client: _Client,
) -> None:
    """Another account's session under `chat:<id>` is nobody's chat."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    act_as_other_agent("writer")
    squatter = await start_session(
        client,
        actor="squat",
        cli_session_id=chat_session_id(conversation),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = ARRAY['science-chat'] WHERE id = $1",
            squatter,
        )
    browser()

    assert (await client.get(f"/api/chats/{conversation}")).status_code == 404
    listed = await client.get("/api/chats")
    assert from_plain(loads(listed.content), list[object]) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_history_is_the_chats_i_started_or_posted_in(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    mine, joined, theirs = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mine_session = await open_science_chat(client, store, conversation_id=mine)
    joined_session = await open_science_chat(
        client,
        store,
        conversation_id=joined,
        account=OTHER_EMAIL,
        posters=(TEST_USER_EMAIL,),
    )
    await open_science_chat(
        client,
        store,
        conversation_id=theirs,
        account=OTHER_EMAIL,
    )
    act_as_assistant("writer")
    other = await start_session(client, actor="not-a-chat")
    assert other

    browser()
    mine_list = from_plain(
        loads((await client.get("/api/chats")).content),
        list[dict[str, object]],
    )
    assert {each["conversation_id"] for each in mine_list} == {str(mine), str(joined)}
    by_id = {each["conversation_id"]: each for each in mine_list}
    assert by_id[str(mine)]["session_id"] == str(mine_session)
    assert by_id[str(joined)]["session_id"] == str(joined_session)
    assert by_id[str(joined)]["account"] == OTHER_EMAIL

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    others_list = from_plain(
        loads((await client.get("/api/chats")).content),
        list[dict[str, object]],
    )
    assert {each["conversation_id"] for each in others_list} == {
        str(joined),
        str(theirs),
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_rotated_assistant_key_leaves_one_chat_read_from_its_newest_session(
    pglite_route_client: _Client,
) -> None:
    """Resume matches the opening key, so a new key opens a second session."""
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    old = await open_science_chat(client, store, conversation_id=conversation)
    act_as_assistant("writer")
    ended = await client.post(f"/api/sessions/{old}/end", json={"status": "done"})
    assert ended.status_code == 200
    new_key = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
            "VALUES ($1, $2, 'rotated', 'test-hash', 'trax_rotate', 'writer')",
            new_key,
            KB_USER_ID,
        )
    install_identity(
        make_test_identity(
            user_id=KB_USER_ID,
            api_key_id=new_key,
            email=KB_EMAIL,
            role="writer",
        ),
    )
    reopened = await start_session(
        client,
        actor=f"chat-{conversation.hex[:12]}",
        cli_session_id=chat_session_id(conversation),
        account=TEST_USER_EMAIL,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = ARRAY['science-chat'] WHERE id = $1",
            reopened,
        )
    browser()

    assert reopened != old
    head = from_plain(
        loads((await client.get(f"/api/chats/{conversation}")).content),
        dict[str, object],
    )
    assert (head["session_id"], head["live"]) == (str(reopened), True)
    listed = from_plain(
        loads((await client.get("/api/chats")).content),
        list[dict[str, object]],
    )
    assert [(each["conversation_id"], each["session_id"]) for each in listed] == [
        (str(conversation), str(reopened)),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_listing_a_restart_reads_selects_the_assistants_own_recent_chats(
    pglite_route_client: _Client,
) -> None:
    """The filters the assistant's recovery sends reach the columns they name."""
    client, store = pglite_route_client
    await seed_accounts(store)
    own = await open_science_chat(client, store, conversation_id=uuid.uuid4())
    act_as_user_agent("writer")
    squatter = await start_session(
        client,
        actor="squat",
        cli_session_id=chat_session_id(uuid.uuid4()),
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = ARRAY['science-chat'] WHERE id = $1",
            squatter,
        )
    act_as_assistant("writer")
    profile = from_plain(
        loads((await client.get("/api/me/profile")).content),
        dict[str, object],
    )
    key = from_plain(profile["api_key_id"], str)

    async def listed(since: datetime) -> list[str]:
        found = await client.get(
            "/api/inquiries",
            params={
                "kind": "AgentSession",
                "limit": 100,
                "offset": 0,
                "filter": [
                    json.dumps(
                        {"field": "cli_session_id", "op": "re", "value": "^chat:"},
                    ),
                    json.dumps(
                        {"field": "labels", "op": "is", "value": "science-chat"},
                    ),
                    json.dumps({"field": "modified", "op": "ge", "value": str(since)}),
                    json.dumps(
                        {"field": "opened_by_api_key_id", "op": "is", "value": key},
                    ),
                ],
            },
        )
        assert found.status_code == 200, found.text
        return [
            from_plain(row["id"], str)
            for row in from_plain(loads(found.content), list[dict[str, object]])
        ]

    assert await listed(datetime.now(UTC) - timedelta(days=7)) == [str(own)]
    assert await listed(datetime.now(UTC) + timedelta(days=1)) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_without_an_assistant_there_are_no_chats(
    pglite_route_client: _Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    conversation = uuid.uuid4()
    await open_science_chat(client, store, conversation_id=conversation)
    monkeypatch.setattr(app.state, "config", None, raising=False)
    browser()

    assert (
        from_plain(loads((await client.get("/api/chats")).content), list[object]) == []
    )
    assert (await client.get(f"/api/chats/{conversation}")).status_code == 404


def test_no_route_deletes_or_edits_a_chat() -> None:
    """Science chat is public and permanent: the routes only read and post."""
    chat_routes = [
        (path, methods)
        for path, methods in iter_routes(app)
        if path.startswith("/api/chats")
    ]
    assert sorted((path, sorted(methods)) for path, methods in chat_routes) == [
        ("/api/chats", ["GET"]),
        ("/api/chats", ["POST"]),
        ("/api/chats/{conversation_id}", ["GET"]),
    ]


class _Stream:
    """A user's events route, read frame by frame without ending it on a timeout."""

    def __init__(self, stream: AsyncIterator[str | bytes | memoryview]) -> None:
        self._stream = stream
        self._next: asyncio.Future[str | bytes | memoryview] | None = None

    async def frames(self, *, quiet_sec: float) -> list[dict[str, object]]:
        """Return the data frames that arrive before ``quiet_sec`` pass without one."""
        frames: list[dict[str, object]] = []
        while (frame := await self._frame(timeout_sec=quiet_sec)) is not None:
            frames.append(frame)
        return frames

    async def has_frame(self, kind: str, *, id: str) -> bool:
        """Read frames until a ``kind`` frame for ``id`` arrives; False after 5 s."""
        deadline = asyncio.get_running_loop().time() + 5.0
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            frame = await self._frame(timeout_sec=left)
            if frame is not None and (frame["type"], frame.get("id")) == (kind, id):
                return True
        return False

    async def _frame(self, *, timeout_sec: float) -> dict[str, object] | None:
        """Return the next data frame, or None when none arrives in time."""
        while True:
            if self._next is None:
                self._next = asyncio.ensure_future(anext(self._stream))
            done, _ = await asyncio.wait({self._next}, timeout=timeout_sec)
            if not done:
                return None
            chunk = self._next.result()
            self._next = None
            text = chunk if isinstance(chunk, str) else bytes(chunk).decode()
            if text.startswith("data: "):
                return from_plain(
                    loads(text.removeprefix("data: ")),
                    dict[str, object],
                )

    async def close(self) -> None:
        """Stop reading."""
        if self._next is not None:
            _ = self._next.cancel()
            _ = await asyncio.gather(self._next, return_exceptions=True)


async def _opened_stream(
    workspace_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    email: str,
) -> _Stream:
    """Open a user's events route and read past its opening frames."""
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
    response = await workspace_routes.workspace_events_route(
        workspace_id,
        request,
        make_test_identity(user_id=user_id, api_key_id=None, email=email),
    )
    stream = _Stream(aiter(response.body_iterator))
    # The stream starts listening for changes while this quiet period passes.
    opened = await stream.frames(quiet_sec=0.05)
    assert [frame["type"] for frame in opened][:1] == ["workspace"]
    return stream


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_record_appended_to_a_chat_reaches_every_viewers_stream(
    pglite_route_client: _Client,
) -> None:
    """The change is the session's id, on the stream of each user's canvas."""
    client, store = pglite_route_client
    await seed_accounts(store)
    first_canvas, _ = await _canvas(client)
    second_canvas, _ = await _canvas(client, other=True)
    chat = await open_science_chat(client, store, conversation_id=uuid.uuid4())
    first = await _opened_stream(
        first_canvas,
        user_id=TEST_USER_ID,
        email=TEST_USER_EMAIL,
    )
    second = await _opened_stream(
        second_canvas,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )
    act_as_assistant("writer")
    appended = await client.post(
        f"/api/sessions/{chat}/records",
        json=_append(
            AgentToAgentMessage(
                sender=OTHER_EMAIL,
                content="a line",
                timestamp=datetime.now(UTC).isoformat(),
            ),
        ),
    )
    assert appended.status_code == 200, appended.text

    for stream in (first, second):
        assert await stream.has_frame("changed", id=str(chat))
        await stream.close()


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
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_carries_the_records_of_the_page_trail_and_visuals(
    pglite_route_client: _Client,
) -> None:
    """A post resolves each route and visual to its record; a miss keeps its route."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    experiment_id, issue_id = await _seed_screen_records(store)
    missing = uuid.uuid4()
    workspace_id, chat_id = await _canvas(client)
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

    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="what about this experiment in context?",
        screen={"page": "#/ref/Experiment/987654", "trail": trail},
    )

    assert sent.status_code == 200
    [heard] = await _drained(client, session_id=kb)
    context = from_plain(heard["context"], dict[str, object])
    experiment = {
        "id": str(experiment_id),
        "kind": "Experiment",
        "seq": 987_654,
        "title": "Measured tails",
    }
    issue = {"id": str(issue_id), "kind": "Issue", "seq": 11, "title": "t" * 512}
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
async def test_a_line_with_a_bad_route_or_a_long_trail_is_refused(
    pglite_route_client: _Client,
    screen: dict[str, object],
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hi",
        screen=screen,
    )

    assert refused.status_code == 422
    assert await _drained(client, session_id=kb) == []


def _partner(state: dict[str, object]) -> dict[str, object]:
    return from_plain(state["partner"], dict[str, object])


async def _state(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
) -> dict[str, object]:
    response = await client.get(f"/api/workspaces/{workspace_id}")
    assert response.status_code == 200
    return from_plain(loads(response.content), dict[str, object])


async def _choose(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    choice: str,
) -> httpx2.Response:
    """Send the choose-partner operation as the installed identity."""
    return await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": await revision_of(client, workspace_id=workspace_id),
            "operation": {"kind": "partner", "choice": choice},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_local_choice_makes_the_owners_helper_the_partner(
    pglite_route_client: _Client,
) -> None:
    """The owner's `trax helper` serves their chat through its own key, end to end."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    scout = await start_session(client, actor=KB_ACTOR)
    act_as_user_agent()
    helper = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)
    workspace_id, chat_id = await _canvas(client)
    shared = await _state(client, workspace_id=workspace_id)
    assert shared["partner_choice"] == "shared"
    assert _partner(shared)["session_id"] == str(scout)
    assert _partner(shared)["kind"] == "shared"

    chosen = await _choose(client, workspace_id=workspace_id, choice="local")
    assert chosen.status_code == 200
    assert _partner(from_plain(loads(chosen.content), dict[str, object])) == {
        "session_id": str(helper),
        "actor": "helper",
        "cli": CHAT_HELPER_CLI,
        "status": "live",
        "kind": "local",
    }
    assert (await _state(client, workspace_id=workspace_id))[
        "partner_choice"
    ] == "local"

    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello",
    )
    conversation = conversation_of(sent)
    assert from_plain(loads(sent.content), dict[str, object])["session_id"] is None
    act_as_user_agent()
    [heard] = from_plain(
        from_plain(
            loads((await drain(client, session_id=helper)).content),
            dict[str, object],
        )["messages"],
        list[dict[str, object]],
    )
    assert (heard["text"], heard["source"]) == ("hello", TEST_USER_EMAIL)
    assert await _drained(client, session_id=scout) == []

    # The helper opens the chat; the owner's key now passes, the assistant's does not.
    chat = await open_helper_chat(client, store=store, conversation_id=conversation)
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 200
    act_as_assistant()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 404

    # Its next line still goes to the helper's service session, which is what the
    # helper drains; its history finds the chat.
    browser()
    joined = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="and then?",
        conversation_id=conversation,
    )
    assert from_plain(loads(joined.content), dict[str, object])["session_id"] is None
    act_as_user_agent()
    [again] = from_plain(
        from_plain(
            loads((await drain(client, session_id=helper)).content),
            dict[str, object],
        )["messages"],
        list[dict[str, object]],
    )
    assert again["text"] == "and then?"
    assert await _drained_by_owner(client, session_id=chat) == []
    listed = from_plain(loads((await client.get("/api/chats")).content), list[object])
    assert [
        from_plain(each, dict[str, object])["conversation_id"] for each in listed
    ] == [
        str(conversation),
    ]
    head = await client.get(f"/api/chats/{conversation}")
    assert head.status_code == 200


async def start_unpolled(
    client: httpx2.AsyncClient,
    *,
    actor: str,
    cli: str = "codex",
    cli_session_id: str | None = None,
    account: str | None = None,
) -> uuid.UUID:
    """Open a session as the installed agent identity without draining its queue.

    Args:
      client: The test client.
      actor: The routing name to ask for.
      cli: The CLI the session says it runs.
      cli_session_id: The id the session is known by across restarts.
      account: The person the session is attributed to; the key's owner by default.

    Returns:
      session_id: The session, opened or resumed.

    """
    started = await client.post(
        "/api/sessions/start",
        json={
            "cli": cli,
            "actor": actor,
            "cli_session_id": cli_session_id,
            "account": account,
        },
    )
    assert started.status_code == 201, started.text
    return uuid.UUID(
        from_plain(from_plain(loads(started.content), dict[str, object])["id"], str),
    )


async def open_helper_chat(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Open a conversation's session as the owner's own `trax helper` does.

    The helper starts the session and records in it, but never polls it: it drains
    only its service session.

    Args:
      client: The test client.
      store: The test database.
      conversation_id: The conversation.

    Returns:
      session_id: The conversation's session, opened under the owner's key.

    """
    act_as_user_agent("writer")
    session_id = await start_unpolled(
        client,
        actor=f"chat-{conversation_id.hex[:12]}",
        cli="sagent",
        cli_session_id=chat_session_id(conversation_id),
        account=TEST_USER_EMAIL,
    )
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET labels = $2 WHERE id = $1",
            session_id,
            [SCIENCE_CHAT_LABEL],
        )
    return session_id


async def _drained_by_owner(
    client: httpx2.AsyncClient,
    *,
    session_id: uuid.UUID,
) -> list[dict[str, object]]:
    """Drain a session as the owner's own key; return its messages."""
    act_as_user_agent("writer")
    response = await drain(client, session_id=session_id)
    assert response.status_code == 200
    return from_plain(
        from_plain(loads(response.content), dict[str, object])["messages"],
        list[dict[str, object]],
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_writer_cannot_poll_a_helper_chat_to_divert_the_owners_lines(
    pglite_route_client: _Client,
) -> None:
    """The owner's helper hears every line on its service session.

    A poll by a key that did not open the chat is refused, and a poll by the opener
    does not make the chat a destination: the helper never drains it.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_user_agent()
    helper = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)
    workspace_id, chat_id = await _canvas(client)
    assert (
        await _choose(client, workspace_id=workspace_id, choice="local")
    ).status_code == 200
    conversation = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="1"),
    )
    chat = await open_helper_chat(client, store=store, conversation_id=conversation)
    assert [m["text"] for m in await _drained_by_owner(client, session_id=helper)] == [
        "1",
    ]

    act_as_other_agent("writer")
    assert (await drain(client, session_id=chat)).status_code == 403
    # Even the opening key polling the chat does not make it the destination.
    assert await _drained_by_owner(client, session_id=chat) == []
    browser()
    second = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="2",
        conversation_id=conversation,
    )

    assert from_plain(loads(second.content), dict[str, object])["session_id"] is None
    act_as_other_agent("writer")
    assert (await drain(client, session_id=chat)).status_code == 403
    assert [m["text"] for m in await _drained_by_owner(client, session_id=helper)] == [
        "2",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_local_canvas_cannot_post_into_a_chat_the_assistant_holds(
    pglite_route_client: _Client,
) -> None:
    """The helper would open a private session under the id and hide the public one.

    History and the link read the live session first, so the person would see the
    helper's answers and lose the conversation everyone else reads.
    """
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    _ = await start_session(client, actor=KB_ACTOR)
    act_as_user_agent()
    helper = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)
    workspace_id, chat_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="a"),
    )
    _ = await open_science_chat(client, store, conversation_id=started)
    browser()
    assert (
        await _choose(client, workspace_id=workspace_id, choice="local")
    ).status_code == 200

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="b",
        conversation_id=started,
    )

    assert refused.status_code == 409
    assert "shared assistant" in refused.text
    assert await _drained_by_owner(client, session_id=helper) == []
    # A conversation of its own is still the helper's.
    browser()
    fresh = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="c",
    )
    assert fresh.status_code == 200
    assert [m["text"] for m in await _drained_by_owner(client, session_id=helper)] == [
        "c",
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_local_choice_with_no_helper_running_refuses_a_post(
    pglite_route_client: _Client,
) -> None:
    """The shared assistant never fills in for a missing helper."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    kb = await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)

    chosen = await _choose(client, workspace_id=workspace_id, choice="local")
    assert chosen.status_code == 200
    assert _partner(from_plain(loads(chosen.content), dict[str, object])) == {
        "session_id": None,
        "actor": None,
        "cli": None,
        "status": "unavailable",
        "kind": "local",
    }
    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="anyone?",
    )
    assert refused.status_code == 409
    assert "local helper" in refused.text
    assert await _drained(client, session_id=kb) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_another_users_helper_never_becomes_this_owners_partner(
    pglite_route_client: _Client,
) -> None:
    """A helper is its opener's own: the owner's Chat reaches nobody else's."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    scout = await start_session(client, actor=KB_ACTOR)
    act_as_other_agent()
    foreign = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)
    workspace_id, chat_id = await _canvas(client)
    await _choose(client, workspace_id=workspace_id, choice="local")
    state = await _state(client, workspace_id=workspace_id)
    assert state["partner_choice"] == "local"
    assert _partner(state)["kind"] == "local"
    assert _partner(state)["status"] == "unavailable"
    assert _partner(state)["session_id"] is None

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        chat_id=chat_id,
        text="hello",
    )
    assert refused.status_code == 409
    act_as_other_agent()
    drained = await drain(client, session_id=foreign)
    assert from_plain(loads(drained.content), dict[str, object])["messages"] == []
    assert await _drained(client, session_id=scout) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_shared_canvas_still_refuses_its_owners_own_key(
    pglite_route_client: _Client,
) -> None:
    """The owner's helper key does not pass while the partner is the assistant."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="a"),
    )
    _ = await open_science_chat(client, store, conversation_id=started)
    act_as_user_agent()
    _ = await start_session(client, actor="helper", cli=CHAT_HELPER_CLI)

    refused = await client.get(f"/api/workspaces/{workspace_id}")

    assert refused.status_code == 403
    assert "Chat partner" in refused.text
    act_as_assistant()
    assert (await client.get(f"/api/workspaces/{workspace_id}")).status_code == 200


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_owners_browser_chooses_the_partner(
    pglite_route_client: _Client,
) -> None:
    """A partner's key may use the canvas, but may not switch who its partner is."""
    client, store = pglite_route_client
    await seed_accounts(store)
    act_as_assistant()
    await start_session(client, actor=KB_ACTOR)
    workspace_id, chat_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, chat_id=chat_id, text="a"),
    )
    _ = await open_science_chat(client, store, conversation_id=started)
    act_as_assistant()

    refused = await _choose(client, workspace_id=workspace_id, choice="local")

    assert refused.status_code == 422
    browser()
    assert (await _state(client, workspace_id=workspace_id))[
        "partner_choice"
    ] == "shared"


def _append(record: AgentToAgentMessage) -> dict[str, object]:
    """Return the body that appends one record as the first of a part."""
    body = RecordBody.of(
        SessionRecordRow.of(session_id=uuid.UUID(int=0), part=0, idx=0, record=record),
    )
    manifest = ManifestBody(
        name="chat.jsonl",
        metadata={},
        ir_id=uuid.uuid4(),
        format="sagent",
        records=1,
    )
    return {
        "name": "chat.jsonl",
        "manifest": manifest.model_dump(mode="json"),
        "records": [body.model_dump(mode="json")],
    }


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
