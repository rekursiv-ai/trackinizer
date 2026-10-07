"""Wire contract for canvas Chat conversations.

A conversation belongs to one signed-in user and one canvas. Its lines are the
user's messages and its partner's answers. An assistant, which holds many
conversations in one session, posts each answer to the conversation it is for
with :data:`CHAT_MESSAGES_PATH`. The server attributes the answers of a user's
own ``trax run`` session, which holds one conversation at a time, so that client
posts nothing here. The browser reads conversations and receives their lines on
the canvas's event stream.

This package is part of the publishable client distribution, so it must not
import ``server`` / ``trax`` / fastapi (see ``import_purity_test``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal, Self

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


__all__ = [
    "CHATS_AWAITING_PATH",
    "CHATS_PATH",
    "CHAT_HELPER_CLI",
    "CHAT_MESSAGES_PATH",
    "CHAT_PATH",
    "AwaitingChat",
    "ChatMessage",
    "ChatReply",
    "ChatSummary",
    "ChatThread",
]


CHATS_PATH: Final = "/api/chats"
CHATS_AWAITING_PATH: Final = "/api/chats/awaiting"
CHAT_PATH: Final = "/api/chats/{conversation_id}"
CHAT_MESSAGES_PATH: Final = "/api/chats/{conversation_id}/messages"

CHAT_HELPER_CLI: Final = "trax-helper"
"""The CLI a ``trax helper`` session opens with: it takes messages only through Chat."""


class ChatMessage(BaseModel):
    """One stored line of a conversation, numbered from 1 in the order stored."""

    id: uuid.UUID
    seq: int = Field(ge=1)
    role: Literal["user", "assistant"]
    author: str
    """The user's email, or the partner session's actor."""

    text: str
    created: datetime


class ChatReply(BaseModel):
    """An assistant's line for one conversation: an answer, or its status.

    An answer is stored and shown as the partner's message. A status is shown
    under the conversation until the next status or answer and is never stored;
    an empty status clears it.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=65_536)
    kind: Literal["answer", "status"]

    @model_validator(mode="after")
    def _text_has_content(self) -> Self:
        """Refuse text of only whitespace; only a status may be empty, to clear."""
        if (self.kind == "answer" or self.text) and not self.text.strip():
            raise ValueError("A reply needs text; only an empty status clears.")
        return self


class ChatSummary(BaseModel):
    """One conversation in its owner's history."""

    id: uuid.UUID
    title: str
    """The first message, cut to 80 characters."""

    partner_actor: str | None
    workspace_id: uuid.UUID
    created: datetime
    modified: datetime


class ChatThread(BaseModel):
    """A conversation and its messages: the newest 500, or up to 500 after a ``seq``."""

    id: uuid.UUID
    title: str
    partner_actor: str | None
    partner_session_id: uuid.UUID | None
    earlier: bool
    """Older messages exist than the first one returned."""

    messages: list[ChatMessage]


class AwaitingChat(BaseModel):
    """A conversation whose last line is the user's, waiting on its partner."""

    conversation_id: uuid.UUID
    workspace_id: uuid.UUID
    seq: int = Field(ge=1)
    """The number of the user's last line, which the partner has yet to answer."""
