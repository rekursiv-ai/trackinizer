"""Tests for the session route tests' seeding helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncio
import uuid

from treekle import json_loads

import pytest

from trackinizer.conftest import make_store
from trackinizer.server.api import session_test_support
from trackinizer.server.api.app import app
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
    act_as_assistant,
    act_as_other_agent,
    act_as_user_agent,
    browser,
    drain,
    seed_accounts,
    seed_session,
    start_session,
)
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.inbound import InboundQueue


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


def test_a_seeded_session_is_the_agents_live_and_polled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    seen = AsyncMock()
    monkeypatch.setattr(store, "record_session_seen", seen)
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound, raising=False)

    session_id = asyncio.run(
        seed_session(store, OTHER_AGENT, cli="claude", drained_by="claude"),
    )

    assert engine.conn.execute.await_args is not None
    inserted = engine.conn.execute.await_args.args
    assert inserted[1:3] == (session_id, "AgentSession")
    assert {"claude session", OTHER_EMAIL, OTHER_KEY_ID, "open", "claude"} <= {
        value for value in inserted if isinstance(value, str | uuid.UUID)
    }
    assert inbound.has_poller(session_id)
    assert seen.await_args is not None
    assert seen.await_args.args == (session_id,)
    assert seen.await_args.kwargs["polled"] is True


def test_an_unpolled_seeded_session_takes_the_given_actor_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, engine = make_store()
    seen = AsyncMock()
    monkeypatch.setattr(store, "record_session_seen", seen)
    inbound = InboundQueue()
    monkeypatch.setattr(app.state, "inbound", inbound, raising=False)

    session_id = asyncio.run(
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
    assert not inbound.has_poller(session_id)
    seen.assert_not_awaited()


def test_a_started_session_is_polled_once_so_it_is_live() -> None:
    session_id = uuid.uuid4()
    server = RecordingServer(
        answers={
            ("POST", "/api/sessions/start"): (201, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    assert run_on(server, call=lambda client: start_session(client, actor="scout")) == (
        session_id
    )
    assert run_on(server, call=start_session) == session_id

    started, polled, started_bare, _ = server.asked
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
    server = RecordingServer(
        answers={
            ("POST", "/api/sessions/start"): (start, {"id": str(session_id)}),
            ("GET", f"/api/sessions/{session_id}/inbound"): (poll, {}),
        },
    )

    with pytest.raises(ValueError, match=refusal):
        run_on(server, call=start_session)


def test_draining_reads_the_session_inbound_queue() -> None:
    session_id = uuid.uuid4()
    server = RecordingServer(
        answers={
            ("GET", f"/api/sessions/{session_id}/inbound"): (200, {"messages": []}),
        },
    )

    drained = run_on(server, call=lambda client: drain(client, session_id=session_id))

    assert drained.status_code == 200
    assert [(each.method, each.url.path) for each in server.asked] == [
        ("GET", f"/api/sessions/{session_id}/inbound"),
    ]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
