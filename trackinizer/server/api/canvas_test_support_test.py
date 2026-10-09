"""Tests for the canvas and science chat route tests' seeding helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncio
import uuid

import httpx2
import pytest

from trackinizer.conftest import make_store
from trackinizer.lib.codec import from_plain, loads
from trackinizer.server.api import canvas_test_support
from trackinizer.server.api.canvas_test_support import (
    KB_EMAIL,
    KB_KEY_ID,
    KB_USER_ID,
    OTHER_EMAIL,
    OTHER_KEY_ID,
    OTHER_USER_ID,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    conversation_of,
    converse,
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
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
)
from trackinizer.server.auth import AuthIdentity


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[AuthIdentity]:
    """Collect the identities the helpers install, in order."""
    identities: list[AuthIdentity] = []
    monkeypatch.setattr(canvas_test_support, "install_identity", identities.append)
    return identities


def test_a_browser_acts_with_no_key(installed: list[AuthIdentity]) -> None:
    browser()
    browser(OTHER_USER_ID, email=OTHER_EMAIL)

    assert installed == [
        AuthIdentity(
            user_id=TEST_USER_ID,
            api_key_id=None,
            email=TEST_USER_EMAIL,
            role="writer",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=None,
            email=OTHER_EMAIL,
            role="writer",
        ),
    ]


def test_each_agent_acts_through_its_own_key_as_a_viewer_unless_told(
    installed: list[AuthIdentity],
) -> None:
    act_as_user_agent()
    act_as_other_agent()
    act_as_other_agent("writer")
    act_as_assistant()
    act_as_assistant("admin")

    assert installed == [
        AuthIdentity(
            user_id=TEST_USER_ID,
            api_key_id=TEST_API_KEY_ID,
            email=TEST_USER_EMAIL,
            role="viewer",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=OTHER_KEY_ID,
            email=OTHER_EMAIL,
            role="viewer",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=OTHER_KEY_ID,
            email=OTHER_EMAIL,
            role="writer",
        ),
        AuthIdentity(
            user_id=KB_USER_ID,
            api_key_id=KB_KEY_ID,
            email=KB_EMAIL,
            role="viewer",
        ),
        AuthIdentity(
            user_id=KB_USER_ID,
            api_key_id=KB_KEY_ID,
            email=KB_EMAIL,
            role="admin",
        ),
    ]


def test_seeding_makes_three_active_writers_each_with_one_writer_key() -> None:
    store, engine = make_store()
    users = (
        "INSERT INTO users (id, email, name, role, status, visual_workspace_enabled) "
        "VALUES ($1, $2, 'Test', 'writer', 'active', TRUE)"
    )
    keys = (
        "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role) "
        "VALUES ($1, $2, 'agent', 'test-hash', $3, 'writer')"
    )

    asyncio.run(seed_accounts(store))

    assert [each.args for each in engine.conn.execute.await_args_list] == [
        (users, TEST_USER_ID, TEST_USER_EMAIL),
        (keys, TEST_API_KEY_ID, TEST_USER_ID, f"trax_{TEST_API_KEY_ID.hex[:6]}"),
        (users, OTHER_USER_ID, OTHER_EMAIL),
        (keys, OTHER_KEY_ID, OTHER_USER_ID, f"trax_{OTHER_KEY_ID.hex[:6]}"),
        (users, KB_USER_ID, KB_EMAIL),
        (keys, KB_KEY_ID, KB_USER_ID, f"trax_{KB_KEY_ID.hex[:6]}"),
    ]


def test_a_started_session_is_polled_once_so_it_is_live() -> None:
    session_id = uuid.uuid4()
    server = _Server(
        answers={
            ("POST", "/api/sessions/start"): (201, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    assert _run(server, call=lambda client: start_session(client, actor="scout")) == (
        session_id
    )
    assert _run(server, call=start_session) == session_id

    started, polled, started_bare, _ = server.asked
    assert loads(started.content) == {
        "cli": "codex",
        "actor": "scout",
        "cli_session_id": None,
        "account": None,
    }
    assert loads(started_bare.content) == {
        "cli": "codex",
        "actor": None,
        "cli_session_id": None,
        "account": None,
    }
    assert (polled.method, polled.url.path) == (
        "GET",
        f"/api/sessions/{session_id}/inbound",
    )


@pytest.mark.parametrize(
    ("start", "poll", "refusal"),
    [(403, 200, "session start answered 403"), (201, 500, "inbound poll answered 500")],
)
def test_a_session_that_does_not_open_and_poll_is_an_error(
    start: int,
    poll: int,
    refusal: str,
) -> None:
    session_id = uuid.uuid4()
    server = _Server(
        answers={
            ("POST", "/api/sessions/start"): (start, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (poll, {}),
        },
    )

    with pytest.raises(ValueError, match=refusal):
        _run(server, call=start_session)


def test_a_canvas_is_opened_by_its_user_as_a_browser(
    installed: list[AuthIdentity],
) -> None:
    workspace_id = uuid.uuid4()
    server = _Server(
        answers={("POST", "/api/workspaces"): (200, {"id": str(workspace_id)})},
    )

    opened = _run(
        server,
        call=lambda client: open_workspace(
            client,
            user_id=OTHER_USER_ID,
            email=OTHER_EMAIL,
        ),
    )

    assert opened == workspace_id
    assert installed == [
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=None,
            email=OTHER_EMAIL,
            role="writer",
        ),
    ]
    server.answers[("POST", "/api/workspaces")] = (403, {})
    with pytest.raises(ValueError, match="workspace create answered 403"):
        _run(server, call=open_workspace)


def test_chat_is_shown_at_the_canvas_current_revision() -> None:
    workspace_id, page, chat = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    visuals = [
        {"id": str(page), "type": "trax.browse"},
        {"id": str(chat), "type": "trax.chat"},
    ]
    operations = ("POST", f"/api/workspaces/{workspace_id}/operations")
    server = _Server(
        answers={
            ("GET", f"/api/workspaces/{workspace_id}"): (200, {"revision": 7}),
            operations: (200, {"visuals": visuals}),
        },
    )

    assert (
        _run(server, call=lambda client: show_chat(client, workspace_id=workspace_id))
        == chat
    )

    shown = server.asked[1]
    assert loads(shown.content) == {
        "revision": 7,
        "operation": {"kind": "show", "visual_type": "trax.chat"},
    }
    _ = uuid.UUID(shown.headers["Idempotency-Key"])
    server.answers[operations] = (409, {})
    with pytest.raises(ValueError, match="show answered 409"):
        _run(server, call=lambda client: show_chat(client, workspace_id=workspace_id))


def test_a_posted_line_carries_its_canvas_conversation_and_key() -> None:
    workspace_id, chat, conversation, key = (uuid.uuid4() for _ in range(4))
    server = _Server(
        answers={
            ("POST", "/api/chats"): (200, {"conversation_id": str(conversation)}),
        },
    )

    sent = _run(
        server,
        call=lambda client: send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat,
            text="hi",
            conversation_id=conversation,
            key=key,
        ),
    )
    _ = _run(
        server,
        call=lambda client: send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat,
            text="new",
        ),
    )

    assert conversation_of(sent) == conversation
    continued, started = server.asked
    assert loads(continued.content) == {
        "workspace_id": str(workspace_id),
        "text": "hi",
        "chat_instance_id": str(chat),
        "expected_record_id": None,
        "conversation_id": str(conversation),
    }
    assert continued.headers["Idempotency-Key"] == str(key)
    assert (
        from_plain(loads(started.content), dict[str, object])["conversation_id"] is None
    )
    assert uuid.UUID(started.headers["Idempotency-Key"]) != key


def test_a_posted_line_names_the_line_it_forks_when_given_one() -> None:
    workspace_id, chat, session = (uuid.uuid4() for _ in range(3))
    server = _Server(
        answers={("POST", "/api/chats"): (200, {"conversation_id": str(chat)})},
    )
    fork = {"session_id": str(session), "part": 0, "idx": 3}

    _ = _run(
        server,
        call=lambda client: send_chat(
            client,
            workspace_id=workspace_id,
            chat_id=chat,
            text="hi",
            fork=fork,
        ),
    )

    [forked] = server.asked
    assert from_plain(loads(forked.content), dict[str, object])["fork"] == fork


def test_the_assistant_opens_a_conversation_as_its_science_chat() -> None:
    conversation, session_id = uuid.uuid4(), uuid.uuid4()
    store, engine = make_store()
    server = _Server(
        answers={
            ("POST", "/api/sessions/start"): (201, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    opened = _run(
        server,
        call=lambda client: open_science_chat(
            client,
            store,
            conversation_id=conversation,
            account=OTHER_EMAIL,
            posters=(TEST_USER_EMAIL,),
        ),
    )

    assert opened == session_id
    assert loads(server.asked[0].content) == {
        "cli": "codex",
        "actor": f"chat-{conversation.hex[:12]}",
        "cli_session_id": f"chat:{conversation}",
        "account": OTHER_EMAIL,
    }
    assert [each.args for each in engine.conn.execute.await_args_list] == [
        (
            "UPDATE inquiries SET labels = $2 WHERE id = $1",
            session_id,
            ["science-chat", f"poster:{TEST_USER_EMAIL}"],
        ),
    ]


def test_conversing_posts_a_line_then_has_the_assistant_open_its_session() -> None:
    workspace_id, chat, conversation, session_id = (uuid.uuid4() for _ in range(4))
    store, _ = make_store()
    server = _Server(
        answers={
            ("POST", "/api/chats"): (200, {"conversation_id": str(conversation)}),
            ("POST", "/api/sessions/start"): (201, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    opened = _run(
        server,
        call=lambda client: converse(
            client,
            store,
            workspace_id=workspace_id,
            chat_id=chat,
            text="hello there",
        ),
    )

    assert opened == session_id
    posted, started, _ = server.asked
    assert from_plain(loads(posted.content), dict[str, object])["text"] == "hello there"
    assert from_plain(loads(started.content), dict[str, object])["cli_session_id"] == (
        f"chat:{conversation}"
    )


def test_a_refused_send_names_no_conversation() -> None:
    with pytest.raises(ValueError, match="send answered 409"):
        conversation_of(httpx2.Response(409, json={}))


def test_the_revision_is_read_off_the_canvas() -> None:
    workspace_id = uuid.uuid4()
    server = _Server(
        answers={("GET", f"/api/workspaces/{workspace_id}"): (200, {"revision": 12})},
    )

    assert (
        _run(server, call=lambda client: revision_of(client, workspace_id=workspace_id))
        == 12
    )


def test_draining_reads_the_session_inbound_queue() -> None:
    session_id = uuid.uuid4()
    server = _Server(
        answers={
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    drained = _run(server, call=lambda client: drain(client, session_id=session_id))

    assert drained.status_code == 200
    assert [(each.method, each.url.path) for each in server.asked] == [
        ("GET", f"/api/sessions/{session_id}/inbound"),
    ]


@dataclass(slots=True, kw_only=True)
class _Server:
    """The app, as the helpers see it: canned answers by method and path."""

    answers: dict[tuple[str, str], tuple[int, object]]
    asked: list[httpx2.Request] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record ``request`` and answer it with its canned answer."""
        self.asked.append(request)
        status, body = self.answers[(request.method, request.url.path)]
        return httpx2.Response(status, json=body)


def _run[T](
    server: _Server,
    *,
    call: Callable[[httpx2.AsyncClient], Awaitable[T]],
) -> T:
    """Run one helper against ``server``."""

    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(server)) as client:
            return await call(client)

    return asyncio.run(run())


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
