"""The science chat reads: what they ask the database, and what they make of the answer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.config import Assistant, ChatOrgs
from trackinizer.server.visuals.science_chats import (
    chat_openers,
    read_head,
    read_starter,
    science_chat_exists_at,
)
from trackinizer.wire.wire_science_chat import (
    FORK_EDGE_LABEL,
    SCIENCE_CHAT_LABEL,
    ChatForkAt,
    ChatHead,
    chat_session_id,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from trackinizer.lib.postgres import Conn, DatabaseEngine


_CONVERSATION: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORIGIN: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SESSION: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ASSISTANT: Final = Assistant(actor="scout", email="kb@example.com")
_STARTER_SQL: Final = (
    "SELECT sess.account FROM inquiries AS sess "
    "LEFT JOIN api_keys AS credential "
    "ON credential.id = sess.agentsession_opened_by_api_key_id "
    "LEFT JOIN users AS opener ON opener.id = credential.user_id "
    "WHERE sess.kind = 'AgentSession' AND sess.agentsession_cli_session_id = $1 "
    "ORDER BY COALESCE(opener.email = $2, FALSE) DESC, sess.created, sess.id "
    "LIMIT 1"
)


def test_a_person_finds_the_assistants_chats_and_their_own() -> None:
    assert chat_openers(None, email="a@example.com") == ["a@example.com"]
    assert chat_openers(_ASSISTANT, email="a@example.com") == [
        "kb@example.com",
        "a@example.com",
    ]


def test_the_starter_is_asked_for_by_the_conversations_session_id_with_the_assistant_first() -> (
    None
):
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = "ada@example.com"

    starter = asyncio.run(
        read_starter(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            conversation_id=_CONVERSATION,
        ),
    )

    assert starter == "ada@example.com"
    conn.fetchval.assert_awaited_once_with(
        _STARTER_SQL,
        chat_session_id(_CONVERSATION),
        "kb@example.com",
    )


def test_a_conversation_nobody_carries_has_no_starter_and_no_assistant_prefers_nobody() -> (
    None
):
    conn: AsyncMock = make_conn()

    starter = asyncio.run(
        read_starter(cast("Conn", conn), assistant=None, conversation_id=_CONVERSATION),
    )

    assert starter is None
    assert conn.fetchval.await_args is not None
    assert conn.fetchval.await_args.args[1:] == (chat_session_id(_CONVERSATION), None)


def test_a_line_is_looked_for_in_the_session_part_and_position_the_fork_names() -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = True
    fork = ChatForkAt(session_id=_SESSION, part=2, idx=5)

    found = asyncio.run(
        science_chat_exists_at(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            email="a@example.com",
            fork=fork,
            audiences=frozenset({"staff", "open"}),
        ),
    )

    assert found is True
    assert conn.fetchval.await_args is not None
    assert conn.fetchval.await_args.args[1:] == (
        ["kb@example.com", "a@example.com"],
        SCIENCE_CHAT_LABEL,
        _SESSION,
        2,
        5,
        ["open", "staff"],
    )
    conn.fetchval.return_value = None
    assert not asyncio.run(
        science_chat_exists_at(
            cast("Conn", conn),
            assistant=None,
            email="a@example.com",
            fork=fork,
            audiences=frozenset({"open"}),
        ),
    )


def _head(engine: FakeEngine, *, forks: object, origin: object, starter: str) -> None:
    """Script the three reads of a head: the session row, the fork count, the origin."""
    engine.conn.fetchrow.side_effect = None
    engine.conn.fetchrow.return_value = {
        "session_id": _SESSION,
        "cli_session_id": chat_session_id(_CONVERSATION),
        "title": "tails",
        "account": starter,
        "modified": datetime(2026, 1, 1, tzinfo=UTC),
        "live": True,
    }
    engine.conn.fetchval.side_effect = [forks, origin]


def _read(
    engine: FakeEngine,
    *,
    email: str,
    orgs: ChatOrgs = "domain",
) -> ChatHead | None:
    return asyncio.run(
        read_head(
            cast("DatabaseEngine", engine),
            assistant=_ASSISTANT,
            email=email,
            conversation_id=_CONVERSATION,
            orgs=orgs,
        ),
    )


def test_a_head_names_its_session_forks_and_origin() -> None:
    engine = FakeEngine()
    _head(
        engine,
        forks=3,
        origin=chat_session_id(_ORIGIN),
        starter="ada@example.com",
    )

    head = _read(engine, email="bob@example.com")

    assert head is not None
    assert head.model_dump() == {
        "conversation_id": _CONVERSATION,
        "session_id": _SESSION,
        "title": "tails",
        "account": "ada@example.com",
        "live": True,
        "forks": 3,
        "forked_from": _ORIGIN,
        "forks_on_typing": False,
    }
    openers = ["kb@example.com", "bob@example.com"]
    row_call = engine.conn.fetchrow.await_args
    assert row_call is not None
    assert row_call.args[1:] == (
        openers,
        SCIENCE_CHAT_LABEL,
        chat_session_id(_CONVERSATION),
    )
    count_call, origin_call = engine.conn.fetchval.await_args_list
    assert count_call.args[1:] == (
        openers,
        SCIENCE_CHAT_LABEL,
        FORK_EDGE_LABEL,
        chat_session_id(_CONVERSATION),
    )
    assert origin_call.args[1:] == (
        openers,
        SCIENCE_CHAT_LABEL,
        FORK_EDGE_LABEL,
        _SESSION,
    )


def test_a_head_with_no_forks_or_origin_and_a_stranger_forking_on_typing() -> None:
    engine = FakeEngine()
    _head(engine, forks=None, origin=None, starter="ada@gmail.com")

    head = _read(engine, email="bob@gmail.com")

    assert head is not None
    assert (head.forks, head.forked_from, head.forks_on_typing) == (
        0,
        None,
        True,
    )


def test_a_chat_the_partner_has_not_opened_has_no_head() -> None:
    engine = FakeEngine()
    engine.conn.fetchrow.side_effect = None
    engine.conn.fetchrow.return_value = None

    assert _read(engine, email="bob@example.com") is None
    engine.conn.fetchval.assert_not_awaited()


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
