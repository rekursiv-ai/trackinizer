"""The science chat reads: what they ask the database, and what they make of the answer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant, ChatOrgs
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.visuals.science_chats import (
    chat_openers,
    conversation_of_session,
    helper_began_chat,
    may_post,
    read_head,
    read_line_starter,
    read_shares,
    read_starter,
    science_chat_exists_at,
)
from trackinizer.types.errors import ChatDeletedError
from trackinizer.wire.audience import OPEN
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
    from trackinizer.server.auth import Role


_CONVERSATION: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORIGIN: Final = uuid.UUID("22222222-2222-2222-2222-222222222222")
_SESSION: Final = uuid.UUID("33333333-3333-3333-3333-333333333333")
_KEY: Final = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ASSISTANT: Final = Assistant(actor="scout", email="kb@example.com")
_FIRST_SESSION: Final = (
    "FROM inquiries AS sess JOIN api_keys AS credential "
    "ON credential.id = sess.agentsession_opened_by_api_key_id "
    "JOIN users AS opener ON opener.id = credential.user_id "
    "WHERE sess.kind = 'AgentSession' AND sess.agentsession_cli_session_id = $1 "
    "AND (opener.email = $2 OR lower(opener.email) = lower(sess.account)) "
    "ORDER BY sess.created, sess.id LIMIT 1"
)
_STARTER_SQL: Final = "SELECT sess.account " + _FIRST_SESSION
_TOMBSTONE_SQL: Final = (
    "SELECT EXISTS (SELECT 1 FROM science_chat_tombstones WHERE conversation_id = $1)"
)


def test_a_person_finds_the_assistants_chats_and_their_own() -> None:
    assert chat_openers(None, email="a@example.com") == ["a@example.com"]
    assert chat_openers(_ASSISTANT, email="a@example.com") == [
        "kb@example.com",
        "a@example.com",
    ]


def test_the_starter_is_the_first_session_opened_by_the_assistant_or_its_own_key() -> (
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


@pytest.mark.parametrize("helper", [True, False])
def test_a_chat_a_helper_began_is_asked_for_by_its_first_session(helper: bool) -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = helper

    began = asyncio.run(
        helper_began_chat(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            conversation_id=_CONVERSATION,
        ),
    )

    assert began is helper
    conn.fetchval.assert_awaited_once_with(
        "SELECT opener.email <> $2 " + _FIRST_SESSION,
        chat_session_id(_CONVERSATION),
        "kb@example.com",
    )


@pytest.mark.parametrize(
    ("named", "conversation"),
    [
        (chat_session_id(_CONVERSATION), _CONVERSATION),
        ("claude-abc", None),
        (None, None),
    ],
    ids=["a-chat", "another-session", "gone"],
)
def test_a_fork_points_session_names_the_chat_it_holds(
    named: str | None,
    conversation: uuid.UUID | None,
) -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = named

    found = asyncio.run(
        conversation_of_session(cast("Conn", conn), session_id=_SESSION),
    )

    assert found == conversation
    conn.fetchval.assert_awaited_once_with(
        "SELECT agentsession_cli_session_id FROM inquiries "
        "WHERE id = $1 AND kind = 'AgentSession'",
        _SESSION,
    )


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


# The head session's own account is someone else's, as a session a rotated assistant key
# opened for the first poster it heard would be; the starter wins.
def _head(
    engine: FakeEngine,
    *,
    forks: object,
    origin: object,
    starter: str,
    shares: tuple[str, ...] = (),
    opened: bool = False,
    others_posted: bool = False,
) -> None:
    """Script a head's reads: its starter, sessions, forks, origin and shares."""
    engine.conn.fetchrow.side_effect = None
    engine.conn.fetchrow.return_value = {
        "session_id": _SESSION,
        "cli_session_id": chat_session_id(_CONVERSATION),
        "title": "tails",
        "account": "first-heard@example.com",
        "modified": datetime(2026, 1, 1, tzinfo=UTC),
        "live": True,
        "sessions": [_SESSION],
        "opened": opened,
        "others_posted": others_posted,
    }
    engine.conn.fetchval.side_effect = [starter, forks, origin]
    engine.conn.fetch.return_value = [{"email": email} for email in shares]


def _read(
    engine: FakeEngine,
    *,
    email: str,
    orgs: ChatOrgs = "domain",
    role: Role = "writer",
    api_key_id: uuid.UUID | None = None,
) -> ChatHead | None:
    return asyncio.run(
        read_head(
            cast("DatabaseEngine", engine),
            assistant=_ASSISTANT,
            caller=AuthIdentity(
                user_id=uuid.uuid4(),
                api_key_id=api_key_id,
                email=email,
                role=role,
            ),
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

    head = _read(engine, email="bob@example.com", api_key_id=_KEY)

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
        "shared_with": [],
        "may_share": False,
        "may_delete": False,
    }
    openers = ["kb@example.com", "bob@example.com"]
    row_call = engine.conn.fetchrow.await_args
    assert row_call is not None
    assert row_call.args[1:] == (
        openers,
        SCIENCE_CHAT_LABEL,
        chat_session_id(_CONVERSATION),
        _KEY,
        "kb@example.com",
        "ada@example.com",
        [OPEN],
    )
    starter_call, count_call, origin_call = engine.conn.fetchval.await_args_list
    assert starter_call.args == (
        _STARTER_SQL,
        chat_session_id(_CONVERSATION),
        "kb@example.com",
    )
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
    shares_call = engine.conn.fetch.await_args
    assert shares_call is not None
    assert shares_call.args[1:] == (_CONVERSATION,)


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


def test_the_shares_are_the_starters_and_admins_to_see_and_let_their_people_type() -> (
    None
):
    shares = ("bob@gmail.com", "grace@example.com")
    heads = [
        _head_of("Ada@Gmail.com", shares=shares),
        _head_of("root@example.com", role="admin", shares=shares),
        _head_of("bob@gmail.com", shares=shares),
        _head_of("jan@other.org", shares=shares),
    ]

    assert [(h.shared_with, h.may_share, h.forks_on_typing) for h in heads] == [
        (list(shares), True, False),
        (list(shares), True, True),
        ([], False, False),
        ([], False, True),
    ]


def test_a_head_says_who_may_delete_the_chat() -> None:
    assert _head_of("Ada@Gmail.com").may_delete
    assert not _head_of("ada@gmail.com", others_posted=True).may_delete
    assert _head_of("root@example.com", role="admin", others_posted=True).may_delete
    assert _head_of("kb@example.com", opened=True, others_posted=True).may_delete
    assert not _head_of("bob@gmail.com").may_delete


def test_a_chat_no_session_carries_yet_has_no_head_and_reads_no_more() -> None:
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = [None, False]

    assert _read(engine, email="bob@example.com") is None
    assert engine.conn.fetchval.await_args is not None
    assert engine.conn.fetchval.await_args.args == (_TOMBSTONE_SQL, _CONVERSATION)
    engine.conn.fetch.assert_not_awaited()


def test_a_chat_whose_sessions_the_reader_may_not_find_has_no_head() -> None:
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = ["ada@example.com", False]
    engine.conn.fetchrow.side_effect = None
    engine.conn.fetchrow.return_value = None

    assert _read(engine, email="bob@example.com") is None
    engine.conn.fetch.assert_not_awaited()


def test_a_deleted_chat_has_no_head_but_a_refusal() -> None:
    engine = FakeEngine()
    engine.conn.fetchval.side_effect = [None, True]

    with pytest.raises(ChatDeletedError):
        _ = _read(engine, email="ada@example.com")


def test_a_new_lines_starter_is_the_databases_before_the_key_maps() -> None:
    inbound = InboundQueue()
    _ = inbound.send_once(
        _CONVERSATION,
        [(_SESSION, Inbound(text="plant", source="bob@example.com"))],
    )

    assert _line_starter("ada@example.com", inbound=inbound) == "ada@example.com"
    assert _line_starter(None, inbound=inbound) == "bob@example.com"


@pytest.mark.parametrize(
    ("stored", "shares", "poster", "allowed"),
    [
        (None, (), "bob@example.com", True),
        ("ada@example.com", (), "Ada@Example.com", True),
        ("ada@example.com", (), "bob@example.com", False),
        ("ada@example.com", ("bob@example.com",), "Bob@Example.com", True),
    ],
    ids=["nobody-began-it", "its-starter", "a-stranger", "shared-with"],
)
def test_who_may_post_into_an_individual_chat(
    stored: str | None,
    shares: tuple[str, ...],
    poster: str,
    allowed: bool,
) -> None:
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = stored
    conn.fetch.return_value = [{"email": email} for email in shares]

    may = asyncio.run(
        may_post(
            cast("Conn", conn),
            poster=poster,
            conversation_id=_CONVERSATION,
            assistant=_ASSISTANT,
            orgs="individual",
            inbound=InboundQueue(),
        ),
    )

    assert may is allowed
    assert conn.fetchval.await_args is not None
    assert conn.fetchval.await_args.args == (
        _STARTER_SQL,
        chat_session_id(_CONVERSATION),
        _ASSISTANT.email,
    )
    assert [call.args[1:] for call in conn.fetch.await_args_list] == (
        [] if stored is None else [(_CONVERSATION,)]
    )


def test_shares_are_read_oldest_first_for_one_conversation() -> None:
    conn: AsyncMock = make_conn()
    conn.fetch.return_value = [{"email": "bob@gmail.com"}, {"email": "a@example.com"}]

    shares = asyncio.run(
        read_shares(cast("Conn", conn), conversation_id=_CONVERSATION),
    )

    assert shares == ["bob@gmail.com", "a@example.com"]
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args == (
        (
            "SELECT email FROM science_chat_shares WHERE conversation_id = $1 "
            "ORDER BY shared_at, email"
        ),
        _CONVERSATION,
    )


def _line_starter(stored: str | None, *, inbound: InboundQueue) -> str | None:
    """Read a new line's starter when the database names ``stored``."""
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = stored
    return asyncio.run(
        read_line_starter(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            conversation_id=_CONVERSATION,
            inbound=inbound,
        ),
    )


def _head_of(
    email: str,
    *,
    role: Role = "writer",
    shares: tuple[str, ...] = (),
    opened: bool = False,
    others_posted: bool = False,
) -> ChatHead:
    """Read the head of Ada's chat on an ``individual`` server, as ``email``."""
    engine = FakeEngine()
    _head(
        engine,
        forks=None,
        origin=None,
        starter="ada@gmail.com",
        shares=shares,
        opened=opened,
        others_posted=others_posted,
    )
    head = _read(engine, email=email, orgs="individual", role=role)
    assert head is not None
    return head


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
