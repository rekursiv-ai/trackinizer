"""Wire contract for science chat: canvas Chat conversations that are AgentSessions.

A conversation is one open AgentSession whose ``cli_session_id`` is
``chat:<conversation id>`` and whose label is :data:`SCIENCE_CHAT_LABEL`. Its records
are the people's lines (each from its poster), the assistant's tool calls and results,
and its answers. There is no second store: the browser reads the session's records,
its starter and the people they share it with post lines (and whoever else the
server's setting lets), and the assistant opens the session when it first hears the
conversation. A share names a person by email and follows the conversation, and the
starter deletes the conversation while every person's line in it is theirs.

A fork is a science chat of its own that starts from another's lines. It is a session
with a ``produced_by`` edge to the original, labelled :data:`FORK_EDGE_LABEL` and
:func:`fork_point_label`, and it opens with copies of the original's lines up to and
including the fork point, a record named by its part and position.

A line goes to ``POST /api/chats``. The server queues it for the conversation's own
session while the assistant keeps one open, and for the assistant's service session
otherwise, which opens or resumes the conversation's session before it answers. The
answer to a post is the conversation id at once; ``GET /api/chats/{id}`` answers 404
until the assistant has opened the session.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "CHATS_PATH",
    "CHAT_ACTOR_PREFIX",
    "CHAT_HELPER_CLI",
    "CHAT_PATH",
    "CHAT_SESSION_PREFIX",
    "CHAT_SHARE_PATH",
    "FORK_EDGE_LABEL",
    "FORK_POINT_PREFIX",
    "POSTER_LABEL_PREFIX",
    "SCIENCE_CHAT_LABEL",
    "ChatForkAt",
    "ChatHead",
    "ChatSend",
    "ChatSent",
    "chat_actor",
    "chat_conversation_id",
    "chat_session_id",
    "fork_point_label",
    "poster_label",
]


CHATS_PATH: Final = "/api/chats"
CHAT_PATH: Final = "/api/chats/{conversation_id}"
CHAT_SHARE_PATH: Final = "/api/chats/{conversation_id}/shares/{email}"

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

CHAT_HELPER_CLI: Final = "trax-helper"
"""The CLI a ``trax helper`` session opens with: it takes messages only through Chat."""

_ROUTE: Final = r"^#/[^\s\x00-\x1f\x7f]*$"
"""A `#/...` hash with no space or control character."""


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


class ChatSend(BaseModel):
    """One line a signed-in writer posts to a science chat from their canvas."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["science"] = "science"
    """What the conversation is; science chat is the only kind."""

    workspace_id: uuid.UUID
    """The poster's own canvas, which the line is sent from."""

    text: str = Field(pattern=r"\S", max_length=16_384)
    conversation_id: uuid.UUID | None = None
    """The conversation to post into; none starts one, named by the request's
    ``Idempotency-Key`` so a retry starts the same one."""

    fork: ChatForkAt | None = None
    """Start a new conversation from this line of another, and post there. It names
    the new conversation by the key, so it excludes ``conversation_id``."""

    page: str | None = Field(default=None, max_length=512, pattern=_ROUTE)
    """The `#/...` address the sender is on as they send."""

    trail: list[Annotated[str, Field(max_length=512, pattern=_ROUTE)]] = Field(
        default_factory=list,
        max_length=8,
    )
    """The addresses the sender came through before it, oldest first."""

    @model_validator(mode="after")
    def _forks_a_new_conversation(self) -> ChatSend:
        if self.fork is not None and self.conversation_id is not None:
            raise ValueError("a fork starts a new conversation; it cannot name one")
        return self


class ChatSent(BaseModel):
    """What a posted line was queued for."""

    conversation_id: uuid.UUID
    workspace_id: uuid.UUID
    """The station the line went to: the one it was sent from, or for a fork the new
    one made for it."""

    session_id: uuid.UUID | None
    """The conversation's session when the assistant had it open; otherwise ``None``,
    and the session appears when the assistant opens it."""


class ChatHead(BaseModel):
    """A science chat's session and starter, for opening it by link or after a post."""

    conversation_id: uuid.UUID
    session_id: uuid.UUID
    title: str
    account: str
    live: bool
    """Whether the assistant still has the session open."""

    forks: int
    """How many times the conversation has been forked."""

    forked_from: uuid.UUID | None
    """The conversation this one was forked from; ``None`` for one that was not."""

    forks_on_typing: bool
    """Whether a line the caller types starts a fork instead of joining the chat:
    the caller is not its starter, not one it is shared with, and not one the server's
    setting lets continue it."""

    shared_with: list[str]
    """The emails the chat is shared with, oldest share first: each posts into it as
    its starter does. Empty for anyone but the starter and an admin."""

    may_share: bool
    """Whether the caller may share the chat and stop sharing it: its starter, or an
    admin."""

    may_delete: bool
    """Whether the caller may delete the chat: an admin, the key that opened it, or
    its starter while every person's line in it is theirs."""
