"""The science chat reads: what they ask the database, and what they make of the answer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import FakeEngine, make_conn
from trackinizer.server.audience import NOBODY, SessionReader, visible_sql
from trackinizer.server.auth import AuthIdentity
from trackinizer.server.config import Assistant
from trackinizer.server.inbound import Inbound, InboundQueue
from trackinizer.server.visuals.science_chats import (
    _HEAD,
    chat_openers,
    conversation_of_session,
    may_post,
    read_fork_prefixes,
    read_head,
    read_line_starter,
    read_shares,
    read_starter,
    science_chat_exists_at,
)
from trackinizer.types.errors import ChatDeletedError
from trackinizer.wire.audience import OPEN, PRIVATE
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
    "FROM inquiries AS began JOIN api_keys AS began_key "
    "ON began_key.id = began.agentsession_opened_by_api_key_id "
    "JOIN users AS began_by ON began_by.id = began_key.user_id "
    "WHERE began.kind = 'AgentSession' AND (began_by.email = $2 "
    "OR lower(began_by.email) = lower(began.account)) "
    "AND began.agentsession_cli_session_id = $1 "
    "ORDER BY began.created, began.id LIMIT 1"
)
_STARTER_SQL: Final = "SELECT began.account " + _FIRST_SESSION
_BEGINNING_SQL: Final = "SELECT began.id " + _FIRST_SESSION
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


def test_a_user_turn_that_names_its_sender_is_a_persons_line() -> None:
    """A CLI user turn counts as a line only when its stamp or an author names a sender.

    The predicate is read from the SQL, which the database runs; the stamp is what
    ``trax run`` writes into ``extra`` for a line it delivered.
    """
    conn: AsyncMock = make_conn()
    conn.fetchval.return_value = True
    reader = SessionReader(
        audiences=frozenset({"open"}),
        email="a@example.com",
        org="example.com",
        assistant="kb@example.com",
    )

    asyncio.run(
        science_chat_exists_at(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            email="a@example.com",
            fork=ChatForkAt(session_id=_SESSION, part=2, idx=5),
            reader=reader,
        ),
    )

    assert conn.fetchval.await_args is not None
    sql = cast(str, conn.fetchval.await_args.args[0])
    assert (
        "'UserMessage' AND (line.payload -> 'extra' -> '$inbound' IS NOT NULL "
        "OR line.payload -> 'extra' ->> 'author' IS NOT NULL)" in sql
    )
    assert "line.kind = 'AssistantMessage' AND COALESCE(" in sql


def test_a_stamped_turn_from_another_sender_counts_as_another_poster() -> None:
    """A stamped user turn from another sender blocks a delete, as a line does.

    The rule is read from the head's SQL, which counts the posters of the session.
    """
    assert (
        "line.kind = 'UserMessage' AND (line.payload -> 'extra' -> '$inbound' IS NOT "
        "NULL OR line.payload -> 'extra' ->> 'author' IS NOT NULL)" in _HEAD
    )
    assert (
        "COALESCE(line.payload ->> 'sender', "
        "line.payload -> 'extra' -> '$inbound' ->> 'source', "
        "line.payload -> 'extra' ->> 'author', '')" in _HEAD
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

    reader = SessionReader(
        audiences=frozenset({"staff", "open"}),
        email="a@example.com",
        org="example.com",
        assistant="kb@example.com",
    )

    found = asyncio.run(
        science_chat_exists_at(
            cast("Conn", conn),
            assistant=_ASSISTANT,
            email="a@example.com",
            fork=fork,
            reader=reader,
        ),
    )

    assert found is True
    assert conn.fetchval.await_args is not None
    sql, *params = conn.fetchval.await_args.args
    assert isinstance(sql, str)
    assert params == [
        ["kb@example.com", "a@example.com"],
        SCIENCE_CHAT_LABEL,
        _SESSION,
        2,
        5,
        ["open", "staff"],
        "a@example.com",
        "example.com",
        False,
        False,
        False,
        True,
        "kb@example.com",
    ]
    # The line is looked for only in a session the reader may read.
    assert f"AS target WHERE target.id = $3 AND {_visible('target', reader)})" in sql
    conn.fetchval.return_value = None
    assert not asyncio.run(
        science_chat_exists_at(
            cast("Conn", conn),
            assistant=None,
            email="a@example.com",
            fork=fork,
            reader=NOBODY,
        ),
    )


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 20, 9, minute, tzinfo=UTC)


def _prefixes(
    rows: list[tuple[int, int, datetime | None]],
    *,
    part: int,
    idx: int,
) -> list[tuple[int, int]]:
    """Return the parts and record counts a fork at ``part``/``idx`` of ``rows`` copies."""
    conn: AsyncMock = make_conn()
    conn.fetch.return_value = [
        {"part": each, "idx": at, "timestamp": stamp} for each, at, stamp in rows
    ]
    fork = ChatForkAt(session_id=_SESSION, part=part, idx=idx)

    found = asyncio.run(read_fork_prefixes(cast("Conn", conn), fork=fork))

    assert conn.fetch.await_args is not None
    _, *params = conn.fetch.await_args.args
    assert params == [_SESSION, part]
    return [(prefix.part, prefix.records) for prefix in found]


def test_a_fork_copies_each_part_as_far_as_the_reader_puts_it_before_the_fork() -> None:
    """Two parts written at once interleave by time; a part begun after copies nothing."""
    rows: list[tuple[int, int, datetime | None]] = [
        (0, 0, _at(1)),
        (0, 1, _at(2)),
        (0, 2, _at(5)),
        (1, 0, _at(3)),
        (1, 1, _at(4)),
        (1, 2, _at(6)),
        (2, 0, _at(7)),
    ]

    assert _prefixes(rows, part=1, idx=1) == [(0, 2), (1, 2)]
    assert _prefixes(rows, part=0, idx=2) == [(0, 3), (1, 2)]
    assert _prefixes(rows, part=0, idx=0) == [(0, 1)]


def test_a_tie_with_the_fork_point_goes_to_the_lower_part_and_unstamped_take_the_last() -> (
    None
):
    """As the reader orders them: the stable sort keeps part order on equal moments."""
    rows: list[tuple[int, int, datetime | None]] = [
        (0, 0, None),
        (0, 1, _at(5)),
        (0, 2, None),
        (1, 0, _at(5)),
        (2, 0, None),
        (2, 1, _at(5)),
        (2, 2, _at(6)),
    ]

    # Part 0's first record takes its part's first stamp and its last the one before
    # it, so both tie with the fork point and come first; part 2's tie comes after.
    assert _prefixes(rows, part=1, idx=0) == [(0, 3), (1, 1)]


def test_a_fork_point_the_session_does_not_hold_copies_nothing() -> None:
    assert _prefixes([(0, 0, _at(1))], part=0, idx=4) == []
    assert _prefixes([], part=0, idx=0) == []


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
    audience: str = OPEN,
    reads: bool = True,
) -> None:
    """Script a head's reads: its starter, sessions, forks, origin, shares, a read."""
    engine.conn.fetchrow.side_effect = None
    engine.conn.fetchrow.return_value = {
        "session_id": _SESSION,
        "cli_session_id": chat_session_id(_CONVERSATION),
        "title": "tails",
        "account": "first-heard@example.com",
        "modified": datetime(2026, 1, 1, tzinfo=UTC),
        "live": True,
        "audience": audience,
        "sessions": [_SESSION],
        "opened": opened,
        "others_posted": others_posted,
    }
    engine.conn.fetchval.side_effect = [starter, forks, origin, reads]
    engine.conn.fetch.return_value = [{"email": email} for email in shares]


def _read(
    engine: FakeEngine,
    *,
    email: str,
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
            reader=SessionReader(email=email),
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
        "audience": "open",
        "may_hide": False,
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
        [OPEN, PRIVATE],
    )
    starter_call, count_call, origin_call, read_call = (
        engine.conn.fetchval.await_args_list
    )
    assert read_call.args[1] == _SESSION
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
    """A caller who does not read the chat is told so: their typing is no post."""
    engine = FakeEngine()
    _head(engine, forks=None, origin=None, starter="ada@gmail.com", reads=False)

    head = _read(engine, email="bob@gmail.com")

    assert head is not None
    assert (head.forks, head.forked_from, head.forks_on_typing) == (
        0,
        None,
        True,
    )


def test_the_starter_grants_shares_and_an_admin_sees_them_only_to_stop_them() -> None:
    shares = ("bob@gmail.com", "grace@example.com")
    heads = [
        _head_of("Ada@Gmail.com", shares=shares),
        _head_of("root@example.com", role="admin", shares=shares, reads=False),
        _head_of("bob@gmail.com", shares=shares),
        _head_of("jan@other.org", shares=shares, reads=False),
    ]

    assert [(h.shared_with, h.may_share, h.forks_on_typing) for h in heads] == [
        (list(shares), True, False),
        (list(shares), False, True),
        ([], False, False),
        ([], False, True),
    ]


@pytest.mark.parametrize(
    ("audience", "starter_hides", "sharee_hides"),
    [("open", True, False), ("private", True, False), ("staff", False, False)],
)
def test_the_head_says_audience_and_who_may_hide(
    audience: str,
    starter_hides: bool,
    sharee_hides: bool,
) -> None:
    """Its starter hides a chat and shows it again; nobody lowers a staff one."""
    starter = _head_of("Ada@Gmail.com", audience=audience)
    sharee = _head_of("bob@gmail.com", audience=audience, shares=("bob@gmail.com",))

    assert (starter.audience, sharee.audience) == (audience, audience)
    assert (starter.may_hide, sharee.may_hide) == (starter_hides, sharee_hides)


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
    ("answers", "sender", "allowed"),
    [
        ([None], None, True),
        ([None], "Bob@Example.com", True),
        ([None], "ada@example.com", False),
        ([_SESSION, True], "ada@example.com", True),
        ([_SESSION, False], None, False),
    ],
    ids=["nobody-began-it", "its-first-sender", "another-first-sender", "reads", "not"],
)
def test_a_post_is_a_read_of_the_session_that_began_the_chat(
    answers: list[object],
    sender: str | None,
    allowed: bool,
) -> None:
    """Whoever reads the chat's first session posts; before one, its first sender."""
    conn: AsyncMock = make_conn()
    conn.fetchval.side_effect = answers
    inbound = InboundQueue()
    if sender is not None:
        _ = inbound.send_once(
            _CONVERSATION,
            [(_SESSION, Inbound(text="first", source=sender))],
        )
    reader = SessionReader(email="bob@example.com")

    may = asyncio.run(
        may_post(
            cast("Conn", conn),
            poster="bob@example.com",
            reader=reader,
            conversation_id=_CONVERSATION,
            assistant=_ASSISTANT,
            inbound=inbound,
        ),
    )

    assert may is allowed
    began, *read = conn.fetchval.await_args_list
    assert began.args == (
        _BEGINNING_SQL,
        chat_session_id(_CONVERSATION),
        _ASSISTANT.email,
    )
    assert [call.args[1] for call in read] == [_SESSION] * len(read)


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
    audience: str = OPEN,
    reads: bool = True,
) -> ChatHead:
    """Read the head of Ada's chat as ``email``, who reads it when ``reads``."""
    engine = FakeEngine()
    _head(
        engine,
        forks=None,
        origin=None,
        starter="ada@gmail.com",
        shares=shares,
        opened=opened,
        others_posted=others_posted,
        audience=audience,
        reads=reads,
    )
    head = _read(engine, email=email, role=role)
    assert head is not None
    return head


def _visible(row: str, reader: SessionReader) -> str:
    """Return the transcript test of ``row``, bound after a fork point's five values."""
    return visible_sql(row, params=[None, None, None, None, None], reader=reader)


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
