"""Wire contract for science chat: canvas Chat conversations that are AgentSessions.

A conversation is one open AgentSession whose ``cli_session_id`` is
``chat:<conversation id>`` and whose label is :data:`SCIENCE_CHAT_LABEL`. Its records
are the people's lines (each from its poster), the assistant's tool calls and results,
and its answers. There is no second store: its members read the session's records and
post lines (its starter, the people they share it with, and whoever else the server's
setting lets), and its starter opens the session when they start it. The session's row, which every key reads, says only who started it and
who posted: its title is :data:`CHAT_TITLE`. A share names a person by email and
follows the conversation, and the starter deletes the conversation while every
person's line in it is theirs.

A fork is a science chat of its own that starts from another's lines. It is a session
with a ``produced_by`` edge to the original, labelled :data:`FORK_EDGE_LABEL` and
:func:`fork_point_label`, and it opens with copies of the original's lines up to and
including the fork point, a record named by its part and position.

A chat starts with ``POST /api/chats`` (:class:`ChatStart`), which binds a new
conversation to the canvas, or forks one onto a new canvas, and opens its session at
once under its starter's key, as a ``sagent`` session; the answer (:class:`ChatSent`)
names it. Its agent is the session's agent process, which the starter brings up on
``PUT /api/sessions/{id}/process``, and every line goes to that agent as an input on
``POST /api/sessions/{id}/inputs`` (``wire_session_relay``).

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Final, Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "CHATS_PATH",
    "CHAT_ACTOR_PREFIX",
    "CHAT_PATH",
    "CHAT_SESSION_PREFIX",
    "CHAT_SHARE_PATH",
    "CHAT_TITLE",
    "FORK_EDGE_LABEL",
    "FORK_POINT_PREFIX",
    "NO_KEY",
    "POSTER_LABEL_PREFIX",
    "SCIENCE_CHAT_LABEL",
    "AssistantPrompt",
    "ChatForkAt",
    "ChatHead",
    "ChatSent",
    "ChatStart",
    "chat_actor",
    "chat_conversation_id",
    "chat_session_id",
    "fork_point_label",
    "poster_label",
]


CHATS_PATH: Final = "/api/chats"
CHAT_PATH: Final = "/api/chats/{conversation_id}"
CHAT_SHARE_PATH: Final = "/api/chats/{conversation_id}/shares/{email}"

NO_KEY: Final = "no_key"
"""The ``code`` of a 409 for a chat started by someone with no live API key: its
session is opened under one."""

SCIENCE_CHAT_LABEL: Final = "science-chat"
"""The label every science chat session carries."""

CHAT_SESSION_PREFIX: Final = "chat:"
"""The start of a science chat session's ``cli_session_id``."""

CHAT_ACTOR_PREFIX: Final = "chat-"
"""The start of the routing name a science chat's session is opened under."""

POSTER_LABEL_PREFIX: Final = "poster:"
"""The start of the label that names a person who posted in a science chat."""

FORK_EDGE_LABEL: Final = "chat-fork"
"""The label on the edge from a fork's session to the session it was forked from."""

FORK_POINT_PREFIX: Final = "fork-at:"
"""The start of the edge label that names the record a fork starts after."""

CHAT_TITLE: Final = "Science chat"
"""The title of every science chat's session, whatever its opener asks for. Every key
reads a session's row, while a chat's lines are its members' alone."""

type AssistantPrompt = Literal["lead", "sagent"]
"""The assistant's system prompt in a conversation, as its poster chose it.

Values:
  lead: The assistant's own lead prompt.
  sagent: sagent's system prompt, followed by a note of the canvas like the one a
    claude or codex run is given, in place of the lead prompt.
"""


def chat_session_id(conversation_id: uuid.UUID) -> str:
    """Return the ``cli_session_id`` of a conversation's session.

    Args:
      conversation_id: The conversation.

    Returns:
      cli_session_id: ``chat:<conversation id>``.

    """
    return f"{CHAT_SESSION_PREFIX}{conversation_id}"


def chat_conversation_id(cli_session_id: str | None) -> uuid.UUID | None:
    """Read the conversation out of a science chat session's ``cli_session_id``.

    Args:
      cli_session_id: A session's ``cli_session_id``, if it has one.

    Returns:
      conversation_id: The ``<id>`` of ``chat:<id>``; None for any other.

    """
    if cli_session_id is None or not cli_session_id.startswith(CHAT_SESSION_PREFIX):
        return None
    try:
        return uuid.UUID(cli_session_id.removeprefix(CHAT_SESSION_PREFIX))
    except ValueError:
        return None


def chat_actor(conversation_id: uuid.UUID) -> str:
    """Return the routing name of a conversation's session.

    It is short and its own, so it never takes the assistant's name: a line meant for
    the assistant's service session is never routed to a chat, and the Console knows a
    science chat by it.

    Args:
      conversation_id: The conversation.

    Returns:
      actor: ``chat-`` and the first twelve hex digits of the id.

    """
    return f"{CHAT_ACTOR_PREFIX}{conversation_id.hex[:12]}"


def poster_label(email: str) -> str:
    """Return the label that records ``email`` as a poster of a science chat.

    Args:
      email: The attested email of the person who posted.

    Returns:
      label: ``poster:<email>``.

    """
    return f"{POSTER_LABEL_PREFIX}{email}"


def fork_point_label(*, part: int, idx: int) -> str:
    """Return the edge label that names the record a fork starts after.

    Args:
      part: The original session's part holding the record.
      idx: The record's position in that part.

    Returns:
      label: ``fork-at:<part>:<idx>``.

    """
    return f"{FORK_POINT_PREFIX}{part}:{idx}"


class ChatForkAt(BaseModel):
    """A line of a science chat's session, which a fork starts from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: uuid.UUID
    """The original's session, whose records hold the line."""

    part: int = Field(ge=0)
    idx: int = Field(ge=0)
    """The line's record: its part and its position in it. The fork holds the lines
    of the session up to and including this one."""


class ChatStart(BaseModel):
    """A chat to start on a canvas, or to fork from a line of another onto a new one.

    The request's ``Idempotency-Key`` names a new conversation, so a retry starts the
    same one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canvas_id: uuid.UUID
    """The starter's own canvas: it holds the new chat, or a fork is copied from it."""

    fork: ChatForkAt | None = None
    """The line of another chat the new one starts from; ``None`` starts the canvas's
    own chat, or answers the one it holds."""


class ChatSent(BaseModel):
    """A chat started, or the one its canvas already held: where it is and its session."""

    conversation_id: uuid.UUID
    canvas_id: uuid.UUID
    """The canvas that holds it: the one it was started from, or for a fork the new
    one made for it."""

    session_id: uuid.UUID
    """The conversation's session, opened by the start."""


class ChatHead(BaseModel):
    """A science chat's session and starter, for opening it by link or after a post."""

    conversation_id: uuid.UUID
    session_id: uuid.UUID
    title: str
    cli: str | None
    """The CLI the session says it runs, which a title its start minted names."""

    goal: str | None
    """The goal its CLI last reported, as its starter's session list reads it; None
    for a caller who does not read the chat."""

    first_line: str | None
    """The first line a person wrote in it, as its starter's session list reads it;
    None for a caller who does not read the chat."""

    account: str
    live: bool
    """Whether the session is open: not ended."""

    forks: int
    """How many times the conversation has been forked."""

    forked_from: uuid.UUID | None
    """The conversation this one was forked from; ``None`` for one that was not."""

    forks_on_typing: bool
    """Whether the caller does not read the chat: not its starter, not one it is
    shared with, and not one the server's setting lets in, or it is a ``private`` or
    ``staff`` chat they may not read. They read none of its lines, and a line they
    type is refused with 403 ``chat_not_shared``; they cannot fork it either, since a
    fork copies lines its poster reads."""

    audience: str
    """Who reads the chat: ``open`` (its members), ``private`` (its starter and the
    people it is shared with) or ``staff`` (its members who hold ``staff``, once its
    assistant has read a staff session for one of them)."""

    may_post: bool
    """Whether the caller posts into the chat: its starter, a person it is shared
    with or, on a ``domain`` server, a member of the starter's organisation, who
    reads it. Any other reader's line is refused with 403 ``chat_not_shared``, and
    they fork an ``open`` chat to continue instead."""

    may_hide: bool
    """Whether the caller may make the chat ``private`` or ``open`` again: its starter,
    while it is not ``staff``."""

    shared_with: list[str]
    """The emails the chat is shared with, oldest share first: each reads it and posts
    into it as its starter does. Empty for anyone but the starter and an admin."""

    may_share: bool
    """Whether the caller may share the chat and stop sharing it: its starter alone.
    An admin may only stop sharing it, since a share lets its person read it."""

    may_delete: bool
    """Whether the caller may delete the chat: an admin, the key that opened it, or
    its starter while every person's line in it is theirs."""
