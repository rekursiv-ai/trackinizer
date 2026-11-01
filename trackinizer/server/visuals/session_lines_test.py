"""What a session's records say of it, made of the rows the database answers.

The SQL that picks each row is run against PGlite by ``session_list_test.py``; these
tests hold what is made of a row: a delivered line unwrapped, one line cut short, and
who said the last.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import asyncio
import uuid

import pytest

from trackinizer.conftest import make_conn
from trackinizer.server.visuals.session_lines import (
    LINE_CHARS,
    LastLine,
    SessionLines,
    session_lines,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


_SESSION: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACCOUNT: Final = "ada@example.com"
_TRAILER: Final = "\nTrackinizer context (verify with trax): Issue#1"


def test_the_first_line_is_a_persons_words_unwrapped_from_their_delivery() -> None:
    said = _said(
        content=f"[chat-1] grace@example.com: \n  Fix the CI hook  \nthen ship{_TRAILER}",
        room="chat-1",
        source="grace@example.com",
    )

    assert said == SessionLines(first_line="Fix the CI hook")


def test_a_goal_and_a_first_line_keep_one_line_cut_short_and_none_is_none() -> None:
    assert _said(goal="x" * (LINE_CHARS + 1), content="y" * (LINE_CHARS + 1)) == (
        SessionLines(goal="x" * LINE_CHARS, first_line="y" * LINE_CHARS)
    )
    assert _said(goal="  \n ", content="") == SessionLines()


@pytest.mark.parametrize(
    ("last", "said"),
    [
        (
            {"last_kind": "AssistantMessage", "last_content": "Done:\n two  files."},
            LastLine(author=None, text="Done: two files."),
        ),
        (
            {
                "last_kind": "AgentToAgentMessage",
                "last_content": "Ship it",
                "last_sender": "grace@example.com",
                "last_source": "bob@example.com",
            },
            LastLine(author="grace@example.com", text="Ship it"),
        ),
        (
            {
                "last_kind": "UserMessage",
                "last_content": f"[c] bob@example.com: Run{_TRAILER}",
                "last_room": "c",
                "last_source": "bob@example.com",
            },
            LastLine(author="bob@example.com", text="Run"),
        ),
        (
            {"last_kind": "UserMessage", "last_content": "z" * (LINE_CHARS + 1)},
            LastLine(author=_ACCOUNT, text="z" * LINE_CHARS),
        ),
    ],
    ids=["answer", "sender", "delivered", "typed"],
)
def test_the_last_line_says_who_said_it_on_one_line(
    last: dict[str, object],
    said: LastLine,
) -> None:
    assert _said(**last) == SessionLines(last_line=said)


def _said(**columns: object) -> SessionLines:
    """Make what one session's row says of it, every column empty but ``columns``."""
    conn = make_conn()
    conn.fetch.return_value = [
        {
            "id": _SESSION,
            "content": None,
            "room": None,
            "source": None,
            "goal": None,
            "last_kind": None,
            "last_content": None,
            "last_sender": None,
            "last_room": None,
            "last_source": None,
        }
        | columns,
    ]
    said = asyncio.run(
        session_lines(cast("Conn", conn), session_ids=[_SESSION], account=_ACCOUNT),
    )
    assert conn.fetch.await_args is not None
    assert conn.fetch.await_args.args[1:] == ([_SESSION],)
    return said[_SESSION]


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
