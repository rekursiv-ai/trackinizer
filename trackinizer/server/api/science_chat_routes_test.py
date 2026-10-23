"""Science chat over the whole app: posting a line, a person's history, a link's session."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final

import asyncio
import json
import uuid

from fastapi import Request
from treekle import from_plain, json_loads

import pytest

from trackinizer.lib.agent.types.sessions import AgentToAgentMessage
from trackinizer.server.api import science_chat_routes, workspace_routes
from trackinizer.server.api.app import app
from trackinizer.server.api.canvas_test_support import (
    ASSISTANT,
    ASSISTANT_CONFIG,
    KB_ACTOR,
    KB_EMAIL,
    KB_USER_ID,
    OTHER_EMAIL,
    OTHER_USER_ID,
    USER_AGENT,
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
    seed_science_chat,
    seed_session,
    send_chat,
    start_session,
)
from trackinizer.server.api.conftest import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    install_identity,
    make_test_identity,
)
from trackinizer.server.chat_hub import ChatHub, WorkspaceFrame
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.route_iter import iter_routes
from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ChatRefusedError,
    ForeignChatError,
)
from trackinizer.server.visuals.chat_shares import UnboundStation
from trackinizer.server.visuals.workspace_store import (
    AnswerNotOwnerError,
    CommandRefusedError,
)
from trackinizer.server.visuals.workspaces import WorkspaceState
from trackinizer.types.errors import ChatDeletedError
from trackinizer.types.session_records import SessionRecordRow
from trackinizer.wire.wire_science_chat import (
    SCIENCE_CHAT_LABEL,
    ChatSent,
    chat_session_id,
)
from trackinizer.wire.wire_session_ir import ManifestBody, RecordBody


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi.testclient import TestClient

    import httpx2

    from trackinizer.conftest import FakeEngine
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.store.core import Store
    from trackinizer.wire.wire_science_chat import ChatSend


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
) -> uuid.UUID:
    """Open a user's canvas; return its id."""
    if other:
        return await open_workspace(client, user_id=OTHER_USER_ID, email=OTHER_EMAIL)
    return await open_workspace(client)


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
        json_loads(response.content, dict[str, object])["messages"],
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
    workspace_id = await _canvas(client)
    key = uuid.uuid4()

    sent = await send_chat(
        client,
        workspace_id=workspace_id,
        text="what do we know about tails?",
        key=key,
    )

    assert sent.status_code == 200
    receipt = json_loads(sent.content, dict[str, object])
    # A new conversation is named by its key, and has no session until the
    # assistant opens it.
    assert receipt == {
        "conversation_id": str(key),
        "session_id": None,
        "workspace_id": str(workspace_id),
    }
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
    workspace_id = await _canvas(client)

    install_identity(make_test_identity(api_key_id=None, role="viewer"))
    refused = await send_chat(
        client,
        workspace_id=workspace_id,
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
    workspace_id = await _canvas(client)

    act_as_user_agent("writer")
    refused = await send_chat(
        client,
        workspace_id=workspace_id,
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
    workspace_id = await _canvas(client)
    browser()

    forged = await client.post(
        "/api/chats",
        json={
            "workspace_id": str(workspace_id),
            "text": "hi",
            "source": "ceo@example.com",
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    honest = await send_chat(
        client,
        workspace_id=workspace_id,
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
    workspace_id = await _canvas(client)
    key = uuid.uuid4()

    first = await send_chat(
        client,
        workspace_id=workspace_id,
        text="once",
        key=key,
    )
    retry = await send_chat(
        client,
        workspace_id=workspace_id,
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
    workspace_id = await _canvas(client)
    key = uuid.uuid4()
    first = await send_chat(
        client,
        workspace_id=workspace_id,
        text="first",
        key=key,
    )
    other = await send_chat(
        client,
        workspace_id=workspace_id,
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
    workspace_id = await _canvas(client)

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
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
    first_canvas = await _canvas(client)
    started = conversation_of(
        await send_chat(
            client,
            workspace_id=first_canvas,
            text="hello from ada",
        ),
    )
    chat = await open_science_chat(client, store, conversation_id=started)
    assert [m["text"] for m in await _drained(client, session_id=kb)] == [
        "hello from ada",
    ]

    browser(OTHER_USER_ID, email=OTHER_EMAIL)
    second_canvas = await _canvas(client, other=True)
    joined = await send_chat(
        client,
        workspace_id=second_canvas,
        text="and from grace",
        conversation_id=started,
    )

    assert joined.status_code == 200
    assert json_loads(joined.content, dict[str, object]) == {
        "conversation_id": str(started),
        "session_id": str(chat),
        "workspace_id": str(second_canvas),
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
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, text="a"),
    )
    chat = await seed_science_chat(store, conversation_id=started)
    act_as_assistant("writer")
    ended = await client.post(f"/api/sessions/{chat}/end", json={"status": "completed"})
    assert ended.status_code == 200
    await _drained(client, session_id=kb)

    browser()
    again = await send_chat(
        client,
        workspace_id=workspace_id,
        text="b",
        conversation_id=started,
    )

    assert again.status_code == 200
    assert json_loads(again.content, dict[str, object])["session_id"] is None
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
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, text="a"),
    )
    chat = await seed_science_chat(store, conversation_id=started)
    await _drained(client, session_id=kb)
    browser()
    joined = await send_chat(
        client,
        workspace_id=workspace_id,
        text="b",
        conversation_id=started,
    )
    assert json_loads(joined.content, dict[str, object])["session_id"] == str(
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
    _ = await _canvas(client)
    foreign = await _canvas(client, other=True)

    browser()
    refused = await send_chat(
        client,
        workspace_id=foreign,
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
    found = json_loads((await client.get(path)).content, dict[str, object])
    assert found["session_id"] == str(chat)
    assert (found["account"], found["live"]) == (TEST_USER_EMAIL, True)

    act_as_assistant("writer")
    await client.post(f"/api/sessions/{chat}/end", json={"status": "completed"})
    browser()
    closed = json_loads((await client.get(path)).content, dict[str, object])
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
    squatter = await start_unpolled(
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
    head = json_loads(
        (await client.get(f"/api/chats/{conversation}")).content,
        dict[str, object],
    )
    assert (head["session_id"], head["live"]) == (str(reopened), True)


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
    squatter = await start_unpolled(
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
    profile = json_loads(
        (await client.get("/api/me/profile")).content,
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
            for row in json_loads(found.content, list[dict[str, object]])
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

    assert (await client.get(f"/api/chats/{conversation}")).status_code == 404


def test_no_route_edits_a_chat_but_posting_sharing_and_deleting_it_whole() -> None:
    """A chat's lines are its posters': no route rewrites one."""
    chat_routes = [
        (path, methods)
        for path, methods in iter_routes(app)
        if path.startswith("/api/chats")
    ]
    assert sorted((path, sorted(methods)) for path, methods in chat_routes) == [
        ("/api/chats", ["POST"]),
        ("/api/chats/{conversation_id}", ["DELETE"]),
        ("/api/chats/{conversation_id}", ["GET"]),
        ("/api/chats/{conversation_id}/shares/{email}", ["DELETE"]),
        ("/api/chats/{conversation_id}/shares/{email}", ["PUT"]),
    ]


class TestShareAndDeleteAnswers:
    """What the share and delete routes answer, with the store beneath them faked."""

    def test_a_refused_line_answers_a_code_the_web_acts_on(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(api_key_id=None))
        monkeypatch.setattr(science_chat_routes, "send_chat_line", _refuse_the_line)

        refused = [
            client.post(
                "/api/chats",
                json={"workspace_id": str(uuid.uuid4()), "text": text},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            for text in ("may I?", "again?")
        ]

        assert [(r.status_code, json_loads(r.content)) for r in refused] == [
            (403, {"detail": {"code": "chat_not_shared", "message": "not yours"}}),
            (410, {"detail": {"code": "chat_deleted", "message": "deleted"}}),
        ]

    def test_a_refused_command_and_a_strangers_answer_answer_codes_chat_shows(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chat shows each message as its notice and keeps the draft."""
        client, _store, _engine = route_client
        install_identity(make_test_identity(api_key_id=None))
        monkeypatch.setattr(science_chat_routes, "send_chat_line", _refuse_the_post)

        refused = [
            client.post(
                "/api/chats",
                json={"workspace_id": str(uuid.uuid4()), "text": text},
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            for text in ("/model opus", "Yes")
        ]

        assert [(r.status_code, json_loads(r.content)) for r in refused] == [
            (422, {"detail": {"code": "command_refused", "message": "no menus"}}),
            (403, {"detail": {"code": "answer_not_owner", "message": "not your run"}}),
        ]

    def test_a_share_answers_the_list_and_a_refusal_its_code(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(api_key_id=None))
        monkeypatch.setattr(science_chat_routes, "set_share", _set_share_as_told)
        chat = uuid.uuid4()

        answers = [
            client.put(f"/api/chats/{chat}/shares/Ada@Example.com"),
            client.delete(f"/api/chats/{chat}/shares/ada@example.com"),
            client.put(f"/api/chats/{chat}/shares/{_REFUSED}"),
            client.delete(f"/api/chats/{chat}/shares/{_UNKNOWN}"),
            client.put(f"/api/chats/{chat}/shares/{_NOT_STAFF}"),
            client.put(f"/api/chats/{chat}/shares/not-an-email"),
        ]

        assert [r.status_code for r in answers] == [200, 200, 403, 404, 409, 422]
        assert [json_loads(r.content) for r in answers[:5]] == [
            [f"Ada@Example.com shared in {chat}"],
            [f"ada@example.com unshared in {chat}"],
            {"detail": {"code": "chat_not_starter"}},
            {"detail": "Conversation not found"},
            {"detail": {"code": "chat_staff_only"}},
        ]

    def test_a_delete_answers_204_and_tells_each_station_it_unbound(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, _engine = route_client
        install_identity(make_test_identity(api_key_id=None))
        hub = ChatHub()
        locked: list[list[uuid.UUID]] = []
        monkeypatch.setattr(app.state, "hub", hub, raising=False)
        monkeypatch.setattr(science_chat_routes, "read_conversation", _one_session)
        monkeypatch.setattr(
            science_chat_routes,
            "require_unlocked",
            partial(_record_lock, locked),
        )
        monkeypatch.setattr(science_chat_routes, "delete_chat", _delete_as_told)
        monkeypatch.setattr(science_chat_routes, "read_workspace", _station_state)

        with hub.subscribe(_STATION) as tab:
            deleted = client.delete(f"/api/chats/{_DELETED}")
            frame = tab.queue.get_nowait()
        refused = client.delete(f"/api/chats/{uuid.uuid4()}")
        unknown = client.delete(f"/api/chats/{_UNKNOWN_CHAT}")

        assert (deleted.status_code, deleted.content) == (204, b"")
        assert isinstance(frame, WorkspaceFrame)
        assert (frame.state.id, frame.state.revision) == (_STATION, 7)
        assert refused.status_code == 403
        assert json_loads(refused.content) == {
            "detail": {"code": "chat_has_other_posters"},
        }
        assert (unknown.status_code, json_loads(unknown.content)) == (
            404,
            {"detail": "Conversation not found"},
        )
        assert locked == [[_SESSION]] * 3

    def test_an_agents_key_shares_nothing_and_deletes_only_what_it_opened(
        self,
        route_client: tuple[TestClient, Store, FakeEngine],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client, _store, _engine = route_client
        monkeypatch.setattr(science_chat_routes, "set_share", _set_share_as_told)
        monkeypatch.setattr(science_chat_routes, "delete_chat", _delete_as_told)
        monkeypatch.setattr(science_chat_routes, "read_workspace", _station_state)
        monkeypatch.setattr(
            science_chat_routes,
            "require_unlocked",
            partial(_record_lock, []),
        )

        monkeypatch.setattr(science_chat_routes, "read_conversation", _one_session)
        share = client.put(f"/api/chats/{_DELETED}/shares/ada@example.com")
        foreign = client.delete(f"/api/chats/{_DELETED}")
        monkeypatch.setattr(
            science_chat_routes,
            "read_conversation",
            partial(_one_session, opened=True),
        )
        opened = client.delete(f"/api/chats/{_DELETED}")

        assert [(r.status_code, json_loads(r.content)) for r in (share, foreign)] == [
            (403, {"detail": "Browser session required"}),
        ] * 2
        assert opened.status_code == 204


class _Stream:
    """A user's events route, read frame by frame without ending it on a timeout."""

    def __init__(self, stream: AsyncIterator[str | bytes | memoryview]) -> None:
        self._stream = stream
        self._next: asyncio.Future[str | bytes | memoryview] | None = None

    async def opened(self) -> None:
        """Read past the opening frames, then let the stream start listening."""
        frame = await self._frame(timeout_sec=5.0)
        assert frame is not None
        assert frame["type"] == "workspace"
        # The stream subscribes to changes when it is asked for its next frame; the
        # request that makes one needs a database round trip, which comes after the
        # subscription.
        self._next = asyncio.ensure_future(anext(self._stream))
        await asyncio.sleep(0)

    async def has_frame(self, kind: str, *, id: str) -> bool:
        """Read frames until a ``kind`` frame for ``id`` arrives; False after 5 s."""
        return bool(await self.ids_until(kind, id=id))

    async def ids_until(self, kind: str, *, id: str) -> list[object]:
        """Read ``kind`` frames until one for ``id`` arrives; return their ids.

        The frames come in the order of the changes, so an id not returned did not
        precede ``id``. The list is empty when none for ``id`` arrives in 5 s.
        """
        deadline = asyncio.get_running_loop().time() + 5.0
        ids: list[object] = []
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            frame = await self._frame(timeout_sec=left)
            if frame is not None and frame["type"] == kind:
                ids.append(frame.get("id"))
                if ids[-1] == id:
                    return ids
        return []

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
                return json_loads(text.removeprefix("data: "), dict[str, object])

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
    audiences: frozenset[str] = frozenset({"open"}),
) -> _Stream:
    """Open a user's events route, as a reader of ``audiences``, past its opening frames."""
    request = Request({"type": "http", "app": app, "headers": [], "method": "GET"})
    response = await workspace_routes.workspace_events_route(
        workspace_id,
        request,
        replace(
            make_test_identity(user_id=user_id, api_key_id=None, email=email),
            read_audiences=audiences,
        ),
    )
    stream = _Stream(aiter(response.body_iterator))
    await stream.opened()
    return stream


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_record_appended_to_a_chat_reaches_every_viewers_stream(
    pglite_route_client: _Client,
) -> None:
    """The change is the session's id, on the stream of each user's canvas."""
    client, store = pglite_route_client
    await seed_accounts(store)
    first_canvas = await _canvas(client)
    second_canvas = await _canvas(client, other=True)
    chat = await seed_science_chat(store, conversation_id=uuid.uuid4())
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_record_appended_to_a_staff_chat_reaches_only_a_viewer_who_reads_staff(
    pglite_route_client: _Client,
) -> None:
    """A viewer of open hears of the open chat's line, and of nothing the staff chat got."""
    client, store = pglite_route_client
    await seed_accounts(store)
    first_canvas = await _canvas(client)
    second_canvas = await _canvas(client, other=True)
    staff_chat = await seed_science_chat(store, conversation_id=uuid.uuid4())
    open_chat = await seed_science_chat(store, conversation_id=uuid.uuid4())
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE inquiries SET agentsession_audience = 'staff' WHERE id = $1",
            staff_chat,
        )
    staff_reader = await _opened_stream(
        first_canvas,
        user_id=TEST_USER_ID,
        email=TEST_USER_EMAIL,
        audiences=frozenset({"open", "staff"}),
    )
    open_reader = await _opened_stream(
        second_canvas,
        user_id=OTHER_USER_ID,
        email=OTHER_EMAIL,
    )

    # The open chat's line comes last, so a ping for the staff chat that reached the
    # open reader would be among the frames that arrive before the open chat's own.
    act_as_assistant("writer")
    for chat in (staff_chat, open_chat):
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

    staff_heard = await staff_reader.ids_until("changed", id=str(open_chat))
    open_heard = await open_reader.ids_until("changed", id=str(open_chat))
    for stream in (staff_reader, open_reader):
        await stream.close()

    assert str(staff_chat) in staff_heard
    assert open_heard == [str(open_chat)]


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
    workspace_id = await _canvas(client)
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
    assert (visuals["trax.browse"], visuals["trax.subgraph"]) == (None, experiment)


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
    workspace_id = await _canvas(client)

    refused = await send_chat(
        client,
        workspace_id=workspace_id,
        text="hi",
        screen=screen,
    )

    assert refused.status_code == 422
    assert await _drained(client, session_id=kb) == []


async def _state(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
) -> dict[str, object]:
    response = await client.get(f"/api/workspaces/{workspace_id}")
    assert response.status_code == 200
    return json_loads(response.content, dict[str, object])


async def _choose(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    provider: str,
) -> httpx2.Response:
    """Send the choose-provider operation as the installed identity."""
    return await client.post(
        f"/api/workspaces/{workspace_id}/operations",
        json={
            "revision": await revision_of(client, workspace_id=workspace_id),
            "operation": {"kind": "provider", "provider": provider},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


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
        from_plain(
            json_loads(started.content, dict[str, object])["id"],
            str,
        ),
    )


async def open_helper_chat(
    client: httpx2.AsyncClient,
    *,
    store: Store,
    conversation_id: uuid.UUID,
) -> uuid.UUID:
    """Open a conversation's session under the owner's own key, not the assistant's.

    The session is started and labelled as a chat, but never polled, so a line for
    it stays queued.

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


async def seed_helper_chat(store: Store, *, conversation_id: uuid.UUID) -> uuid.UUID:
    """Open a conversation's session as ``open_helper_chat`` does, in the store.

    Args:
      store: The test database.
      conversation_id: The conversation.

    Returns:
      session_id: The conversation's session, opened under the owner's key, never polled.

    """
    return await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation_id.hex[:12]}",
        cli="sagent",
        cli_session_id=chat_session_id(conversation_id),
        account=TEST_USER_EMAIL,
        labels=[SCIENCE_CHAT_LABEL],
        polled=False,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_only_the_owners_browser_chooses_the_provider(
    pglite_route_client: _Client,
) -> None:
    """A partner's key may use the canvas, but may not switch who answers its Chat."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    started = conversation_of(
        await send_chat(client, workspace_id=workspace_id, text="a"),
    )
    _ = await seed_science_chat(store, conversation_id=started)
    act_as_assistant()

    refused = await _choose(client, workspace_id=workspace_id, provider="assistant")
    browser()
    retired = [
        (await _choose(client, workspace_id=workspace_id, provider=old)).status_code
        for old in ("shared", "local")
    ]

    # The partner's key is refused a valid choice; the retired partner choices are no
    # provider at all, even for the owner.
    assert (refused.status_code, retired) == (422, [422, 422])
    assert "Only the canvas's owner" in refused.text
    state = await _state(client, workspace_id=workspace_id)
    assert (state["provider"], state["revision"]) == ("assistant", 0)


async def _on_cli(
    store: Store,
    *,
    workspace_id: uuid.UUID,
    provider: str = "claude",
) -> uuid.UUID:
    """Put the station on ``provider`` with a chat of its own; return the chat."""
    conversation = uuid.uuid4()
    async with store.engine.acquire() as conn:
        await conn.execute(
            "UPDATE visual_workspaces SET state = state || jsonb_build_object("
            "'provider', $2::text), station = station || jsonb_build_object("
            "'conversation_id', $3::text) WHERE id = $1",
            workspace_id,
            provider,
            str(conversation),
        )
    return conversation


async def _post(
    client: httpx2.AsyncClient,
    *,
    workspace_id: uuid.UUID,
    text: str,
    kind: str,
    payload: dict[str, object] | None = None,
) -> httpx2.Response:
    """Post a browser line of ``kind`` to the station's chat."""
    browser()
    return await client.post(
        "/api/chats",
        json={
            "workspace_id": str(workspace_id),
            "text": text,
            "kind": kind,
            "payload": payload,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_post_to_a_claude_station_reaches_its_run_with_kind_and_payload(
    pglite_route_client: _Client,
) -> None:
    """The chat's session is the run's queue; only a line carries the screen."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id)
    run = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        drained_by="claude",
    )
    answer: dict[str, object] = {"prompt_id": "p1", "decision": "allow"}

    answered = await _post(
        client,
        workspace_id=workspace_id,
        text="Yes",
        kind="answer",
        payload=answer,
    )
    said = await _post(client, workspace_id=workspace_id, text="go on", kind="line")

    assert (answered.status_code, said.status_code) == (200, 200)
    sent = ChatSent.model_validate_json(answered.content)
    assert (sent.conversation_id, sent.session_id) == (conversation, run)
    act_as_user_agent("writer")
    taken = await drain(client, session_id=run, cli="claude")
    assert taken.status_code == 200
    messages = from_plain(
        json_loads(taken.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    assert [(m["text"], m["kind"], m["payload"]) for m in messages] == [
        ("Yes", "answer", answer),
        ("go on", "line", None),
    ]
    assert messages[0]["context"] is None
    context = from_plain(messages[1]["context"], dict[str, object])
    assert context["conversation_id"] == str(conversation)
    assert {m["source"] for m in messages} == {TEST_USER_EMAIL}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_claude_station_queues_goal_as_a_command_and_model_as_settings(
    pglite_route_client: _Client,
) -> None:
    """The browser posts a line; the server tells the command from it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id)
    run = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        drained_by="claude",
    )

    goal = await _post(
        client,
        workspace_id=workspace_id,
        text="/goal ship it",
        kind="line",
    )
    resume = await _post(
        client,
        workspace_id=workspace_id,
        text="/resume",
        kind="line",
    )
    model = await _post(
        client,
        workspace_id=workspace_id,
        text="/model opus",
        kind="line",
    )

    assert goal.status_code == 200, goal.text
    assert model.status_code == 200, model.text
    assert resume.status_code == 422
    assert json_loads(resume.content) == {
        "detail": {
            "code": "command_refused",
            "message": "Chat cannot run /resume: it opens a menu, changes settings or "
            "switches the session. Use it in the run's own terminal.",
        },
    }
    act_as_user_agent("writer")
    taken = await drain(client, session_id=run, cli="claude")
    assert taken.status_code == 200
    heard = from_plain(
        json_loads(taken.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    assert [(m["text"], m["kind"], m["payload"], m["context"]) for m in heard] == [
        ("/goal ship it", "command", None, None),
        ("/model opus", "settings", {"model": "opus", "effort": None}, None),
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_post_to_a_cli_station_with_no_run_is_refused_as_offline(
    pglite_route_client: _Client,
) -> None:
    """Nothing would drain the line, so the browser is told to pick a machine."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id, provider="codex")
    _ = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli_session_id=chat_session_id(conversation),
        polled=False,
    )

    refused = await _post(client, workspace_id=workspace_id, text="hi", kind="line")

    assert refused.status_code == 409
    detail = from_plain(
        json_loads(refused.content, dict[str, object])["detail"],
        dict[str, object],
    )
    assert detail == {
        "code": "provider_offline",
        "message": "No codex run holds this chat; pick a machine to start one",
    }


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_into_a_closed_cli_chat_is_refused_as_offline(
    pglite_route_client: _Client,
) -> None:
    """Closing ends the run's session; the chat takes no line until a machine is picked."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id)
    run = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        drained_by="claude",
    )
    assert (
        await _post(client, workspace_id=workspace_id, text="open", kind="line")
    ).status_code == 200
    act_as_user_agent("writer")
    closed = await client.post(f"/api/sessions/{run}/end", json={"actor": "scientist"})
    assert closed.status_code == 200, closed.text

    refused = await _post(client, workspace_id=workspace_id, text="still?", kind="line")

    assert refused.status_code == 409
    detail = from_plain(
        json_loads(refused.content, dict[str, object])["detail"],
        dict[str, object],
    )
    assert detail["code"] == "provider_offline"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_line_into_a_closed_assistant_chat_goes_to_the_assistants_service_session(
    pglite_route_client: _Client,
) -> None:
    """The assistant resumes a conversation from its service session, so a new line reopens it."""
    client, store = pglite_route_client
    await seed_accounts(store)
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    browser()
    conversation = conversation_of(
        await send_chat(client, workspace_id=workspace_id, text="hello"),
    )
    chat = await seed_science_chat(store, conversation_id=conversation)
    act_as_assistant()
    closed = await client.post(f"/api/sessions/{chat}/end", json={"actor": "scientist"})
    assert closed.status_code == 200, closed.text

    said = await _post(client, workspace_id=workspace_id, text="back", kind="line")

    assert said.status_code == 200, said.text
    assert ChatSent.model_validate_json(said.content).session_id != chat
    assert [m["text"] for m in await _drained(client, session_id=kb)][-1:] == ["back"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_assistant_takes_a_persons_line_or_an_answer_and_nothing_else(
    pglite_route_client: _Client,
) -> None:
    client, store = pglite_route_client
    await seed_accounts(store)
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)

    refused = await _post(
        client,
        workspace_id=workspace_id,
        text="/compact",
        kind="command",
    )

    assert refused.status_code == 422
    assert "The assistant takes a person's line or an answer, not a command" in (
        refused.text
    )
    assert await _drained(client, session_id=kb) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_to_the_assistants_card_reaches_the_chat_it_asked_in(
    pglite_route_client: _Client,
) -> None:
    """A choice on a question card is queued for the chat's own session, as posted."""
    client, store = pglite_route_client
    await seed_accounts(store)
    await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)
    browser()
    conversation = conversation_of(
        await send_chat(client, workspace_id=workspace_id, text="hello"),
    )
    chat = await seed_science_chat(store, conversation_id=conversation)
    answer: dict[str, object] = {"prompt_id": "q1", "option": "2"}

    answered = await _post(
        client,
        workspace_id=workspace_id,
        text="Blue",
        kind="answer",
        payload=answer,
    )

    assert answered.status_code == 200, answered.text
    assert ChatSent.model_validate_json(answered.content).session_id == chat
    heard = await _drained(client, session_id=chat)
    assert [(m["text"], m["kind"], m["payload"], m["source"]) for m in heard] == [
        ("Blue", "answer", answer, TEST_USER_EMAIL),
    ]
    # The pick is heard as the person's line, so the turn it starts reads the canvas
    # they picked on, as a typed line's does.
    context = heard[0]["context"]
    assert isinstance(context, dict)
    assert context["workspace_id"] == str(
        ChatSent.model_validate_json(answered.content).workspace_id,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_an_answer_to_a_chat_the_assistant_has_not_open_is_refused(
    pglite_route_client: _Client,
) -> None:
    """Its cards are released when it closes the chat, so there is nothing to answer."""
    client, store = pglite_route_client
    await seed_accounts(store)
    kb = await seed_session(store, ASSISTANT, actor=KB_ACTOR)
    workspace_id = await _canvas(client)

    refused = await _post(
        client,
        workspace_id=workspace_id,
        text="Blue",
        kind="answer",
        payload={"prompt_id": "q1", "option": "2"},
    )

    assert refused.status_code == 409, refused.text
    assert from_plain(
        json_loads(refused.content, dict[str, object])["detail"],
        dict[str, object],
    ) == {
        "code": "provider_offline",
        "message": "The assistant does not have this chat open",
    }
    assert await _drained(client, session_id=kb) == []


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_chat_taken_over_in_a_terminal_refuses_every_post(
    pglite_route_client: _Client,
) -> None:
    """Its headless run is stopped, so a post would be queued for no one."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id)
    run = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
    )
    inbound = app.state.inbound
    assert isinstance(inbound, InboundQueue)
    instance = "a" * 32
    inbound.mark_poller(run, cli="claude", instance=instance)
    assert inbound.report_state(run, instance=instance, state="taken_over")
    answer: dict[str, object] = {"prompt_id": "p1", "option": "allow_once"}

    refused = [
        await _post(client, workspace_id=workspace_id, text="go on", kind="line"),
        await _post(client, workspace_id=workspace_id, text="/goal x", kind="line"),
        await _post(
            client,
            workspace_id=workspace_id,
            text="Yes",
            kind="answer",
            payload=answer,
        ),
        await _post(client, workspace_id=workspace_id, text="stop", kind="interrupt"),
    ]

    for response in refused:
        assert response.status_code == 409, response.text
        assert from_plain(
            json_loads(response.content, dict[str, object])["detail"],
            dict[str, object],
        ) == {
            "code": "chat_taken_over",
            "message": "taken over in a terminal; reattach from Chat or Console",
        }
    assert inbound.pending(run) == 0
    assert inbound.report_state(run, instance=instance, state="idle")
    said = await _post(client, workspace_id=workspace_id, text="back", kind="line")
    assert said.status_code == 200, said.text
    assert inbound.pending(run) == 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_fork_of_a_claude_station_is_one_whose_line_waits_for_its_run(
    pglite_route_client: _Client,
) -> None:
    """No run holds a new chat, so its session is opened and the line queued there."""
    client, store = pglite_route_client
    await seed_accounts(store)
    workspace_id = await _canvas(client)
    conversation = await _on_cli(store, workspace_id=workspace_id)
    chat = await seed_session(
        store,
        USER_AGENT,
        actor=f"chat-{conversation.hex[:12]}",
        cli="claude",
        cli_session_id=chat_session_id(conversation),
        labels=[SCIENCE_CHAT_LABEL],
        drained_by="claude",
    )
    part = await store.upsert_session_manifest(
        chat,
        name="chat.jsonl",
        metadata={},
        ir_id=uuid.uuid4(),
        format="claude",
        records=1,
    )
    line = AgentToAgentMessage(
        sender=TEST_USER_EMAIL,
        content="q1",
        timestamp=datetime.now(UTC).isoformat(),
    )
    _ = await store.append_session_records(
        chat,
        [SessionRecordRow.of(session_id=chat, part=part, idx=0, record=line)],
    )
    key = uuid.uuid4()

    forked = await send_chat(
        client,
        workspace_id=workspace_id,
        text="what if?",
        key=key,
        fork={"session_id": str(chat), "part": part, "idx": 0},
    )

    assert forked.status_code == 200, forked.text
    sent = ChatSent.model_validate_json(forked.content)
    assert sent.conversation_id == key
    assert sent.session_id is not None
    assert sent.workspace_id != workspace_id
    station = await _state(client, workspace_id=sent.workspace_id)
    partner = from_plain(station["partner"], dict[str, object])
    assert (station["provider"], partner["session_id"], partner["status"]) == (
        "claude",
        str(sent.session_id),
        "offline",
    )
    async with store.engine.acquire() as conn:
        opened = await conn.fetchrow(
            "SELECT account, labels, agentsession_opened_by_api_key_id AS key "
            "FROM inquiries WHERE id = $1 AND agentsession_cli_session_id = $2",
            sent.session_id,
            chat_session_id(key),
        )
    assert opened is not None
    assert (opened["account"], opened["labels"], opened["key"]) == (
        TEST_USER_EMAIL,
        [SCIENCE_CHAT_LABEL],
        USER_AGENT.key_id,
    )
    act_as_user_agent("writer")
    taken = await drain(client, session_id=sent.session_id, cli="claude")
    assert taken.status_code == 200, taken.text
    [waiting] = from_plain(
        json_loads(taken.content, dict[str, object])["messages"],
        list[dict[str, object]],
    )
    assert (waiting["text"], waiting["kind"]) == ("what if?", "line")


_REFUSED: Final = "no@example.com"
_UNKNOWN: Final = "gone@example.com"
_NOT_STAFF: Final = "bob@example.com"
_DELETED: Final = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_UNKNOWN_CHAT: Final = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
_STATION: Final = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")
_SESSION: Final = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000004")
_OWNER: Final = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000005")


async def _refuse_the_line(
    engine: object,
    *,
    body: ChatSend,
    **line: object,
) -> ChatSent:
    """Refuse a line as an unshared chat does, or one asking again as a deleted one."""
    del engine, line
    if body.text == "again?":
        raise ChatDeletedError("deleted")
    raise ForeignChatError("not yours")


async def _refuse_the_post(
    engine: object,
    *,
    body: ChatSend,
    **line: object,
) -> ChatSent:
    """Refuse a slash command as a CLI's chat does, or an answer as a sharer's is."""
    del engine, line
    if body.text.startswith("/"):
        raise CommandRefusedError("no menus")
    raise AnswerNotOwnerError("not your run")


async def _set_share_as_told(
    engine: object,
    *,
    assistant: object,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    email: str,
    shared: bool,
    staff: Sequence[str],
) -> list[str] | None:
    """Share or unshare as the email says: refuse two, find no chat for another."""
    _check_served(engine, assistant=assistant, caller=caller)
    assert staff == ()
    if email == _REFUSED:
        raise ChatRefusedError("chat_not_starter")
    if email == _NOT_STAFF:
        raise ChatClosedError("chat_staff_only")
    done = "shared" if shared else "unshared"
    return None if email == _UNKNOWN else [f"{email} {done} in {conversation_id}"]


async def _one_session(
    conn: object,
    *,
    assistant: object,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    opened: bool = False,
) -> SimpleNamespace:
    """Find a chat of one session, whatever is asked."""
    del conversation_id
    _check_served(conn, assistant=assistant, caller=caller)
    return SimpleNamespace(sessions=[_SESSION], opened=opened)


async def _record_lock(
    locked: list[list[uuid.UUID]],
    request: object,
    identity: AuthIdentity,
    row_ids: list[uuid.UUID],
    *,
    include_peers: bool,
) -> None:
    """Note the rows a delete asks to be unlocked, with their peers."""
    assert request is not None
    assert include_peers
    assert identity.email == TEST_USER_EMAIL
    locked.append(list(row_ids))


async def _delete_as_told(
    store: object,
    *,
    assistant: object,
    caller: AuthIdentity,
    conversation_id: uuid.UUID,
    inbound: object,
) -> list[UnboundStation] | None:
    """Delete one chat, find no other, and refuse the rest."""
    _check_served(store, assistant=assistant, caller=caller)
    assert isinstance(inbound, InboundQueue)
    if conversation_id == _UNKNOWN_CHAT:
        return None
    if conversation_id != _DELETED:
        raise ChatRefusedError("chat_has_other_posters")
    return [UnboundStation(workspace_id=_STATION, user_id=_OWNER)]


async def _station_state(
    engine: object,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    inbound: object,
    assistant: object,
) -> WorkspaceState:
    """Read a station, for its owner, as it stands after its chat was deleted."""
    assert engine is not None
    assert isinstance(inbound, InboundQueue)
    assert (user_id, assistant) == (_OWNER, ASSISTANT_CONFIG.assistant)
    return WorkspaceState(
        id=workspace_id,
        revision=7,
        name="unnamed-workstream#1",
        conversation_id=None,
        page=None,
        visuals=[],
    )


def _check_served(source: object, *, assistant: object, caller: AuthIdentity) -> None:
    """Fail unless a fake got the database, the configured assistant and the caller."""
    assert source is not None
    assert (assistant, caller.email) == (ASSISTANT_CONFIG.assistant, TEST_USER_EMAIL)


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
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
