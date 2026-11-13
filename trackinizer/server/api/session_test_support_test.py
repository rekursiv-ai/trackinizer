"""Tests for the session route tests' seeding helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncio
import uuid

from treekle import json_loads

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import session_test_support
from trackinizer.server.api.conftest import (
    TEST_API_KEY_ID,
    TEST_USER_EMAIL,
    TEST_USER_ID,
    RecordingServer,
    run_on,
)
from trackinizer.server.api.session_test_support import (
    KB_EMAIL,
    KB_KEY_ID,
    KB_USER_ID,
    OTHER_AGENT,
    OTHER_EMAIL,
    OTHER_KEY_ID,
    OTHER_USER_ID,
    ServingRelay,
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    attached_process,
    browser,
    seed_accounts,
    seed_session,
    start_session,
)
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.session_relay import SessionRelay
from trackinizer.wire.wire_session_relay import NewInput


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[AuthIdentity]:
    """Collect the identities the helpers install, in order."""
    identities: list[AuthIdentity] = []
    monkeypatch.setattr(session_test_support, "install_identity", identities.append)
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
            hosted_domain="example.com",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=None,
            email=OTHER_EMAIL,
            role="writer",
            hosted_domain="example.com",
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
            hosted_domain="example.com",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=OTHER_KEY_ID,
            email=OTHER_EMAIL,
            role="viewer",
            hosted_domain="example.com",
        ),
        AuthIdentity(
            user_id=OTHER_USER_ID,
            api_key_id=OTHER_KEY_ID,
            email=OTHER_EMAIL,
            role="writer",
            hosted_domain="example.com",
        ),
        AuthIdentity(
            user_id=KB_USER_ID,
            api_key_id=KB_KEY_ID,
            email=KB_EMAIL,
            role="viewer",
            hosted_domain="example.com",
        ),
        AuthIdentity(
            user_id=KB_USER_ID,
            api_key_id=KB_KEY_ID,
            email=KB_EMAIL,
            role="admin",
            hosted_domain="example.com",
        ),
    ]


def test_seeding_makes_three_active_writers_each_with_one_writer_key() -> None:
    store, engine = make_store()
    users = (
        "INSERT INTO users (id, email, name, role, status, visual_canvas_enabled) "
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


def test_a_seeded_session_is_the_agents_live_and_watched_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    seen = AsyncMock()
    monkeypatch.setattr(store, "record_session_seen", seen)

    session_id = asyncio.run(
        seed_session(store, OTHER_AGENT, cli="claude"),
    )

    assert engine.conn.execute.await_args is not None
    inserted = engine.conn.execute.await_args.args
    assert inserted[1:3] == (session_id, "AgentSession")
    assert {"claude session", OTHER_EMAIL, OTHER_KEY_ID, "open", "claude"} <= {
        value for value in inserted if isinstance(value, str | uuid.UUID)
    }
    assert seen.await_args is not None
    assert seen.await_args.args == (session_id,)
    assert seen.await_args.kwargs["polled"] is True


def test_an_unpolled_seeded_session_takes_the_given_actor_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    seen = AsyncMock()
    monkeypatch.setattr(store, "record_session_seen", seen)

    asyncio.run(
        seed_session(
            store,
            OTHER_AGENT,
            actor="ranger",
            account="lead@example.com",
            polled=False,
        ),
    )

    assert engine.conn.execute.await_args is not None
    inserted = engine.conn.execute.await_args.args
    assert {"ranger", "lead@example.com"} <= set(inserted)
    assert OTHER_EMAIL not in inserted
    seen.assert_not_awaited()


def test_a_started_session_is_opened_through_the_route() -> None:
    session_id = uuid.uuid4()
    server = RecordingServer(
        answers={("POST", "/api/sessions/start"): (201, {"id": str(session_id)})},
    )

    assert run_on(server, call=lambda client: start_session(client, actor="scout")) == (
        session_id
    )
    assert run_on(server, call=start_session) == session_id

    started, started_bare = server.asked
    assert json_loads(started.content) == {
        "cli": "codex",
        "actor": "scout",
        "cli_session_id": None,
        "account": None,
    }
    assert json_loads(started_bare.content) == {
        "cli": "codex",
        "actor": None,
        "cli_session_id": None,
        "account": None,
    }


def test_a_session_that_does_not_open_is_an_error() -> None:
    server = RecordingServer(
        answers={("POST", "/api/sessions/start"): (403, {"id": str(uuid.uuid4())})},
    )

    with pytest.raises(ValueError, match="session start answered 403"):
        run_on(server, call=start_session)


@pytest.mark.asyncio
async def test_an_attached_process_serves_its_session_for_the_block_alone() -> None:
    relay = SessionRelay()
    session = uuid.uuid4()

    async with attached_process(relay, session, host="lab") as process:
        during = relay.process(session)

    after = relay.process(session)
    assert during == process
    assert (process.session_id, process.state, process.host) == (
        session,
        "attached",
        "lab",
    )
    assert after is not None
    assert (after.process_id, after.state, after.reason) == (
        process.process_id,
        "detached",
        "exited",
    )


@pytest.mark.asyncio
async def test_an_open_socket_takes_what_it_is_sent_and_closes_once() -> None:
    """The process the support attaches is let in by a rule that always holds."""
    assert await session_test_support._always()
    socket = session_test_support._OpenSocket()

    await socket.send_text("ignored")
    await socket.close(1001)
    await socket.close(1000)

    assert await socket.receive() == 1001


def test_a_serving_relay_serves_every_session_and_notes_each_send_in_order() -> None:
    relay = ServingRelay()
    first, second = uuid.UUID(int=7), uuid.UUID(int=8)
    line = NewInput(kind="UserMessage", payload={"content": "hi"})

    sent = [
        relay.send_input(first, line, author="ann@example.com"),
        relay.send_input(second, line, author="lead#4", recipient="x", booked="ask"),
    ]
    served = [relay.process(each) for each in (first, second)]

    assert sent == [line.id, line.id]
    assert relay.sent == [
        (first, {"content": "hi"}, "ann@example.com"),
        (second, {"content": "hi"}, "lead#4"),
    ]
    assert [(each.session_id, each.state) for each in served if each is not None] == [
        (first, "attached"),
        (second, "attached"),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
