"""Wire contract for science chat: canvas Chat conversations that are AgentSessions.

A conversation is one open AgentSession whose ``cli_session_id`` is
``chat:<conversation id>`` and whose label is :data:`SCIENCE_CHAT_LABEL`. Its records
are the people's lines (each from its poster), the assistant's tool calls and results,
and its answers. There is no second store: its members read the session's records and
post lines (its starter, the people they share it with, and whoever else the server's
setting lets), and the assistant opens the session when it first hears the
conversation. The session's row, which every key reads, says only who started it and
who posted: its title is :data:`CHAT_TITLE`. A share names a person by email and
follows the conversation, and the starter deletes the conversation while every
person's line in it is theirs.

A fork is a science chat of its own that starts from another's lines. It is a session
with a ``produced_by`` edge to the original, labelled :data:`FORK_EDGE_LABEL` and
:func:`fork_point_label`, and it opens with copies of the original's lines up to and
including the fork point, a record named by its part and position.

A line goes to ``POST /api/chats``. Who answers it is the canvas's provider
(:data:`ChatProvider`). For ``assistant`` the server queues it for the
conversation's own session while the assistant keeps one open, and for the
assistant's service session otherwise, which opens or resumes the conversation's
session before it answers. For ``claude`` or ``codex`` it queues it for the
conversation's session, which the person's own run on a machine of theirs drains.
The answer to a post is the conversation id at once; ``GET /api/chats/{id}`` answers
404 until some session carries the conversation.

While a turn runs, the assistant puts the answer it is writing at
:data:`CHAT_PARTIAL_PATH`, where the chat's readers wait for it to change. The server
keeps it in memory alone, for a minute after its last change: it is no record, and
the stored answer replaces it.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)


__all__ = [
    "CHATS_PATH",
    "CHAT_ACTOR_PREFIX",
    "CHAT_PARTIAL_CHARS",
    "CHAT_PARTIAL_PATH",
    "CHAT_PARTIAL_TURN_PATH",
    "CHAT_PARTIAL_WAIT_SEC",
    "CHAT_PATH",
    "CHAT_SESSION_PREFIX",
    "CHAT_SHARE_PATH",
    "CHAT_TITLE",
    "FORK_EDGE_LABEL",
    "FORK_POINT_PREFIX",
    "POSTER_LABEL_PREFIX",
    "SCIENCE_CHAT_LABEL",
    "ChatAnswer",
    "ChatForkAt",
    "ChatHead",
    "ChatLineKind",
    "ChatPartial",
    "ChatPartialState",
    "ChatProvider",
    "ChatSend",
    "ChatSent",
    "ChatSettings",
    "chat_actor",
    "chat_conversation_id",
    "chat_session_id",
    "fork_point_label",
    "poster_label",
]


CHATS_PATH: Final = "/api/chats"
CHAT_PATH: Final = "/api/chats/{conversation_id}"
CHAT_SHARE_PATH: Final = "/api/chats/{conversation_id}/shares/{email}"
CHAT_PARTIAL_PATH: Final = "/api/sessions/{session_id}/partial"
CHAT_PARTIAL_TURN_PATH: Final = "/api/sessions/{session_id}/partial/{turn}"

CHAT_PARTIAL_CHARS: Final = 16_384
"""The most text a partial carries; the assistant clips what it has written to it."""

CHAT_PARTIAL_WAIT_SEC: Final = 25.0
"""The longest a read of a partial is held for a change, under the edge's idle cutoff."""

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

_ROUTE: Final = r"^#/[^\s\x00-\x1f\x7f]*$"
"""A `#/...` hash with no space or control character."""


type ChatProvider = Literal["assistant", "claude", "codex"]
"""Who answers a canvas's Chat.

Values:
  assistant: The server's shared assistant.
  claude: The person's own claude agent run on a machine of theirs, attached to
    the chat's session.
  codex: The same with codex.
"""


# ``wire_sessions`` imports this module for ``ChatForkAt``, so this one cannot import
# its ``InboundKind`` back; ``wire_science_chat_test`` holds the two equal.
type ChatLineKind = Literal["line", "command", "answer", "settings", "interrupt"]
"""What a Chat post is: the values of ``wire_sessions.InboundKind``, which a run
delivers each its own way. Only ``line`` is a person's chat line."""


class ChatAnswer(BaseModel):
    """The payload of an ``answer`` post: a choice on an ask the run is showing.

    It names the choice by ``option``, the id of one of the ask's own options, or in
    the older form by ``decision``, which the run reads as the option of that kind.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_id: str = Field(min_length=1, max_length=200)
    option: str = Field(default="", max_length=200)
    """The id of the option taken, or a kind such as ``allow_once``; empty when
    ``decision`` names the choice."""

    decision: Literal["allow", "deny"] | None = None
    """The older form of ``option``: ``allow`` is ``allow_once``, ``deny`` is ``deny``."""

    updated_input: JsonValue = None
    """The tool input the prompt's answer replaces, such as the answers to a
    question; ``None`` leaves it as asked."""

    message: str = Field(default="", max_length=16_384)
    """Why, for a deny; the run passes it to the CLI."""

    @model_validator(mode="after")
    def _names_one_choice(self) -> Self:
        if bool(self.option) == (self.decision is not None):
            raise ValueError("an answer names exactly one of an option and a decision")
        return self


class ChatSettings(BaseModel):
    """The payload of a ``settings`` post: a model or effort change, or both."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=120)
    effort: str | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.model is None and self.effort is None:
            raise ValueError("a settings post names a model, an effort, or both")
        return self


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

    kind: ChatLineKind = "line"
    """What the post is; a person's line unless the client names another kind. Only a
    ``claude`` or ``codex`` canvas takes a kind other than ``line``."""

    payload: dict[str, object] | None = None
    """The kind's structured data: a :class:`ChatAnswer` for ``answer``, a
    :class:`ChatSettings` for ``settings``, ``None`` for the other kinds."""

    canvas_id: uuid.UUID
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

    # The web sent ``"science"`` here, the conversation's kind, until the field named
    # the post's; a browser not yet reloaded still does, and its line is a line.
    @field_validator("kind", mode="before")
    @classmethod
    def _read_science_as_line(cls, value: object) -> object:
        """Read the retired ``science`` kind as ``line``."""
        return "line" if value == "science" else value

    @model_validator(mode="after")
    def _forks_a_new_conversation(self) -> ChatSend:
        if self.fork is not None and self.conversation_id is not None:
            raise ValueError("a fork starts a new conversation; it cannot name one")
        if self.fork is not None and self.kind != "line":
            raise ValueError("a fork starts from a person's line")
        return self

    # The payload's own error is raised as a ValueError: a ValidationError raised in
    # a validator is not folded into the body's, so the route would answer 500.
    @model_validator(mode="after")
    def _payload_fits_the_kind(self) -> ChatSend:
        shape = {"answer": ChatAnswer, "settings": ChatSettings}.get(self.kind)
        if shape is None and self.payload is not None:
            raise ValueError(f"a {self.kind} post carries no payload")
        try:
            _ = None if shape is None else shape.model_validate(self.payload)
        except ValidationError as error:
            raise ValueError(f"the {self.kind} payload: {error}") from error
        return self


class ChatPartial(BaseModel):
    """The answer a chat's assistant is writing, as far as it has written it."""

    model_config = ConfigDict(extra="forbid")

    turn: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    """The delivery the answer is for, new for each: a reader that saw its answer
    stored shows no partial of it again."""

    text: str = Field(max_length=CHAT_PARTIAL_CHARS)
    """The answer so far, as a reader is shown it: vetted for secrets, without the
    notes the model keeps, and clipped."""

    done: bool = False
    """Whether ``text`` is the whole answer, which the assistant is now storing."""


class ChatPartialState(BaseModel):
    """What the server holds of a chat's answer in writing, and how new it is."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    """Names the partial held: drawn at random, below 2**53, when the chat held none,
    and one more with each put after it, so a put always names a new one and says
    nothing of other chats; 0 when the server holds none for the chat. A reader
    passes the last it saw to wait for a change, and compares it for equality only."""

    partial: ChatPartial | None


class ChatSent(BaseModel):
    """What a posted line was queued for."""

    conversation_id: uuid.UUID
    canvas_id: uuid.UUID
    """The canvas the line went to: the one it was sent from, or for a fork the new
    one made for it."""

    session_id: uuid.UUID | None
    """The conversation's session when the line was queued on it: the assistant had it
    open, or the canvas's provider is a CLI; otherwise ``None``, and the session
    appears when the assistant opens it."""


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
