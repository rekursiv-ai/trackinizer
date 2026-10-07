"""Persistence for canvas Chat conversations and their lines.

A conversation belongs to one signed-in user and one canvas. Its lines are
numbered from 1 in the order stored, and every write here runs on the caller's
connection inside the caller's transaction, so a line is stored exactly when the
action that caused it commits. Publishing the line to open browsers is the
caller's job, after that commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, cast, get_args

import re
import uuid

from pydantic import BaseModel

from trackinizer.lib.codec import from_plain
from trackinizer.server.chat_hub import HighlightFrame, MessageFrame, StatusFrame
from trackinizer.server.notify import tx
from trackinizer.types.inquiries import Inquiry
from trackinizer.wire.wire_chats import (
    AwaitingChat,
    ChatMessage,
    ChatReply,
    ChatSummary,
    ChatThread,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn, DatabaseEngine
    from trackinizer.server.chat_hub import ChatHub


__all__ = [
    "NOT_DELIVERED",
    "ChatConversationNotFoundError",
    "ChatReplyForbiddenError",
    "ChatRequestConflictError",
    "SentMessage",
    "add_message",
    "awaiting_replies",
    "conversations_of",
    "delete_conversation",
    "list_conversations",
    "post_reply",
    "read_partner_thread",
    "read_thread",
    "release_session",
    "replay_of",
    "start_or_continue",
]


MAX_CONVERSATIONS: Final = 50
"""Conversations the history lists."""

MAX_THREAD_MESSAGES: Final = 500
"""Messages one thread read returns."""

TITLE_LENGTH: Final = 80
"""Characters of the first message kept as the conversation's title."""

NOT_DELIVERED: Final = (
    "Not delivered: the partner ended before it read your message. Send it again."
)
"""The status of a conversation whose waiting messages died with its session."""

_NAMED: Final = re.compile(
    r"(?<![\w#])("
    + "|".join(
        cast(tuple[str, ...], get_args(cast(object, Inquiry.InquiryKind.__value__))),
    )
    + r")#(\d+)\b",
)
"""A ``Kind#seq`` an answer names, matched as the web links one."""

_MOST_NAMED: Final = 50
"""The most records one answer highlights, as many as one highlight takes."""


class ChatConversationNotFoundError(Exception):
    """The conversation does not exist, belongs to another user, or another canvas."""


class ChatReplyForbiddenError(Exception):
    """The key did not open the conversation's live partner session."""


class ChatRequestConflictError(Exception):
    """An idempotency key was reused for a different send."""


class SentMessage(BaseModel):
    """A stored browser send: the line, its conversation and its partner."""

    conversation_id: uuid.UUID
    session_id: uuid.UUID | None
    message: ChatMessage


class _PartnerOf(BaseModel):
    """A conversation as its live partner session sees it."""

    workspace_id: uuid.UUID
    actor: str | None


async def start_or_continue(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    text: str,
    partner_session_id: uuid.UUID,
    partner_actor: str | None,
) -> uuid.UUID:
    """Open a conversation for a first message, or point an existing one at its partner.

    Args:
      conn: Connection inside the sender's transaction.
      user_id: The sender, who must own an existing conversation.
      workspace_id: Canvas the message is sent from; an existing conversation
        must belong to it.
      conversation_id: The conversation to continue; None starts one.
      text: The message, whose first words title a new conversation.
      partner_session_id: The session now answering.
      partner_actor: That session's actor.

    Returns:
      conversation_id: The conversation the message belongs to.

    Raises:
      ChatConversationNotFoundError: The conversation is not this user's on this canvas.

    """
    if conversation_id is None:
        conversation_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO chat_conversations "
            "(id, user_id, workspace_id, title, partner_session_id, partner_actor) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            conversation_id,
            user_id,
            workspace_id,
            " ".join(text.split())[:TITLE_LENGTH],
            partner_session_id,
            partner_actor,
        )
        return conversation_id
    updated = await conn.fetchval(
        "UPDATE chat_conversations SET partner_session_id = $4, partner_actor = $5, "
        "modified_at = clock_timestamp() "
        "WHERE id = $1 AND user_id = $2 AND workspace_id = $3 RETURNING id",
        conversation_id,
        user_id,
        workspace_id,
        partner_session_id,
        partner_actor,
    )
    if updated is None:
        raise ChatConversationNotFoundError("Conversation not found")
    return conversation_id


async def add_message(
    conn: Conn,
    *,
    conversation_id: uuid.UUID,
    role: Literal["user", "assistant"],
    author: str,
    text: str,
    request_key: uuid.UUID | None = None,
    request_hash: str | None = None,
) -> ChatMessage | None:
    """Store the next line of a conversation.

    The conversation row is locked first, so two concurrent lines take
    consecutive numbers instead of colliding.

    Args:
      conn: Connection inside a transaction.
      conversation_id: Conversation to extend.
      role: Who said it.
      author: The user's email, or the partner's actor.
      text: The line.
      request_key: The Idempotency-Key of the browser send that stored it; a key
        already stored stores nothing.
      request_hash: Hash of that request.

    Returns:
      message: The stored line, or None when the conversation is gone or the
        key is already stored.

    """
    locked = await conn.fetchval(
        "SELECT 1 FROM chat_conversations WHERE id = $1 FOR UPDATE",
        conversation_id,
    )
    if locked is None:
        return None
    row = await conn.fetchrow(
        "INSERT INTO chat_messages (id, conversation_id, seq, role, author, text, "
        "request_key, request_hash) "
        "SELECT $1::uuid, $2::uuid, COALESCE(MAX(seq), 0) + 1, $3::text, $4::text, "
        "$5::text, $6::uuid, $7::text "
        "FROM chat_messages WHERE conversation_id = $2 "
        "ON CONFLICT DO NOTHING "
        "RETURNING id, seq, role, author, text, created_at",
        uuid.uuid4(),
        conversation_id,
        role,
        author,
        text,
        request_key,
        request_hash,
    )
    if row is None:
        return None
    await conn.execute(
        "UPDATE chat_conversations SET modified_at = clock_timestamp() WHERE id = $1",
        conversation_id,
    )
    return _message(row)


async def replay_of(
    conn: Conn,
    *,
    user_id: uuid.UUID,
    request_key: uuid.UUID,
    request_hash: str,
) -> SentMessage | None:
    """Find the send an idempotency key already stored, for its replay.

    Args:
      conn: Database connection.
      user_id: The sender.
      request_key: The send's Idempotency-Key.
      request_hash: Hash of this request.

    Returns:
      sent: The original send, or None when the key was never stored.

    Raises:
      ChatRequestConflictError: The key stored a different request.

    """
    row = await conn.fetchrow(
        "SELECT message.id, message.seq, message.role, message.author, "
        "message.text, message.created_at, message.request_hash, "
        "conversation.id AS conversation_id, conversation.partner_session_id "
        "FROM chat_messages AS message "
        "JOIN chat_conversations AS conversation "
        "ON conversation.id = message.conversation_id "
        "WHERE message.request_key = $1 AND conversation.user_id = $2",
        request_key,
        user_id,
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise ChatRequestConflictError(
            "Idempotency-Key already used for another message",
        )
    return SentMessage.model_validate(
        {
            "conversation_id": row["conversation_id"],
            "session_id": row["partner_session_id"],
            "message": _message(row).model_dump(),
        },
    )


async def list_conversations(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
) -> list[ChatSummary]:
    """List a user's conversations, newest change first.

    Args:
      engine: Database connection source.
      user_id: The owner.

    Returns:
      chats: At most :data:`MAX_CONVERSATIONS` conversations.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, partner_actor, workspace_id, "
            "created_at AS created, modified_at AS modified "
            "FROM chat_conversations WHERE user_id = $1 "
            "ORDER BY modified_at DESC, id DESC LIMIT $2",
            user_id,
            MAX_CONVERSATIONS,
        )
    return [ChatSummary.model_validate(dict(row)) for row in rows]


async def conversations_of(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> list[uuid.UUID]:
    """List the ids of a user's conversations on one canvas.

    Args:
      engine: Database connection source.
      user_id: The owner.
      workspace_id: The canvas.

    Returns:
      ids: The conversation ids.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, workspace_id FROM chat_conversations "
            "WHERE user_id = $1 AND workspace_id = $2",
            user_id,
            workspace_id,
        )
    return [_Conversation.model_validate(dict(row)).id for row in rows]


async def awaiting_replies(
    engine: DatabaseEngine,
    *,
    api_key_id: uuid.UUID,
) -> list[AwaitingChat]:
    """List the conversations a live session of this key owes an answer.

    A partner that restarts has drained messages it never answered; this names
    them: the conversations whose partner is a live session this key opened and
    whose last line is the user's.

    Args:
      engine: Database connection source.
      api_key_id: The calling agent key.

    Returns:
      awaiting: Each conversation, oldest change first, with its last line's number.

    """
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT conversation.id AS conversation_id, conversation.workspace_id, "
            "last_line.seq "
            "FROM chat_conversations AS conversation "
            "JOIN inquiries AS sess ON sess.id = conversation.partner_session_id "
            "AND sess.kind = 'AgentSession' AND sess.status = 'active' "
            "AND sess.agentsession_ended IS NULL "
            "AND sess.agentsession_opened_by_api_key_id = $1 "
            "JOIN api_keys AS credential ON credential.id = $1 "
            "AND credential.revoked_at IS NULL "
            "JOIN LATERAL (SELECT seq, role FROM chat_messages "
            "WHERE conversation_id = conversation.id ORDER BY seq DESC LIMIT 1) "
            "AS last_line ON last_line.role = 'user' "
            "ORDER BY conversation.modified_at, conversation.id",
            api_key_id,
        )
    return [AwaitingChat.model_validate(dict(row)) for row in rows]


async def read_thread(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    after_seq: int | None,
) -> ChatThread | None:
    """Read a user's conversation with its newest lines, or the lines after one.

    Args:
      engine: Database connection source.
      user_id: The owner.
      conversation_id: Conversation to read.
      after_seq: Return up to 500 lines numbered above this; none returns the
        newest 500.

    Returns:
      thread: The conversation, or None when it is not this user's.

    """
    async with engine.acquire() as conn:
        head = await conn.fetchrow(
            "SELECT id, title, partner_actor, partner_session_id "
            "FROM chat_conversations WHERE id = $1 AND user_id = $2",
            conversation_id,
            user_id,
        )
        if head is None:
            return None
        return await _thread(conn, head=dict(head), after_seq=after_seq)


async def read_partner_thread(
    engine: DatabaseEngine,
    *,
    api_key_id: uuid.UUID,
    conversation_id: uuid.UUID,
    after_seq: int | None,
) -> ChatThread:
    """Read a conversation as its live partner session's key, to reseed it.

    Args:
      engine: Database connection source.
      api_key_id: The calling agent key.
      conversation_id: Conversation to read.
      after_seq: As for :func:`read_thread`.

    Returns:
      thread: The conversation.

    Raises:
      ChatConversationNotFoundError: No such conversation.
      ChatReplyForbiddenError: The key did not open its live partner session.

    """
    async with engine.acquire() as conn:
        await _partner_of(conn, conversation_id=conversation_id, api_key_id=api_key_id)
        head = await conn.fetchrow(
            "SELECT id, title, partner_actor, partner_session_id "
            "FROM chat_conversations WHERE id = $1",
            conversation_id,
        )
        if head is None:
            raise ChatConversationNotFoundError("Conversation not found")
        return await _thread(conn, head=dict(head), after_seq=after_seq)


async def delete_conversation(
    engine: DatabaseEngine,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    """Delete a user's conversation and its lines.

    Args:
      engine: Database connection source.
      user_id: The owner.
      conversation_id: Conversation to delete.

    Returns:
      workspace_id: The conversation's canvas, or None when it is not this
        user's.

    """
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM chat_conversations WHERE id = $1 AND user_id = $2 "
            "RETURNING workspace_id",
            conversation_id,
            user_id,
        )
    return None if row is None else _Located.model_validate(dict(row)).workspace_id


async def post_reply(
    engine: DatabaseEngine,
    *,
    conversation_id: uuid.UUID,
    api_key_id: uuid.UUID,
    reply: ChatReply,
    hub: ChatHub,
) -> ChatMessage | None:
    """Store an assistant's answer, or push its status, for one conversation.

    Only the key that opened the conversation's partner session may reply, and
    only while that session is live. A status is kept as the conversation's
    current one and never stored; an answer clears it. The records an answer
    names as ``Kind#seq`` are highlighted on the canvas after it, so pointing at
    what it talks about costs the partner no call.

    Args:
      engine: Database connection source.
      conversation_id: Conversation the reply is for.
      api_key_id: The calling agent key.
      reply: The answer or status.
      hub: Where the committed line is published.

    Returns:
      message: The stored answer; None for a status.

    Raises:
      ChatConversationNotFoundError: No such conversation, or the answer could
        not be stored because it is gone.
      ChatReplyForbiddenError: The key did not open its live partner session.

    """
    async with engine.acquire() as conn, tx(conn):
        partner = await _partner_of(
            conn,
            conversation_id=conversation_id,
            api_key_id=api_key_id,
        )
        if reply.kind == "status":
            hub.set_status(conversation_id, text=reply.text)
            hub.publish(
                partner.workspace_id,
                frame=StatusFrame(conversation_id=conversation_id, text=reply.text),
            )
            return None
        message = await add_message(
            conn,
            conversation_id=conversation_id,
            role="assistant",
            author=partner.actor or "",
            text=reply.text,
        )
        named = await _named_records(conn, text=reply.text)
    if message is None:
        raise ChatConversationNotFoundError("Conversation not found")
    hub.set_status(conversation_id, text="")
    hub.publish(
        partner.workspace_id,
        frame=MessageFrame(conversation_id=conversation_id, message=message),
    )
    if named:
        hub.publish(partner.workspace_id, frame=HighlightFrame(ids=named))
    return message


async def release_session(
    engine: DatabaseEngine,
    *,
    session_id: uuid.UUID,
    undelivered: Sequence[tuple[uuid.UUID, uuid.UUID]],
    hub: ChatHub,
) -> None:
    """Tell each Chat a session left what it was doing, once the session ends.

    A conversation with messages the session never drained gets a status saying
    so and to send again. Any other conversation the session was the partner of
    loses the status it had, since nothing is working on it now.

    Args:
      engine: Database connection source.
      session_id: The session that ended.
      undelivered: The ``(conversation, canvas)`` of each message it never drained.
      hub: Where each status is kept and published.

    """
    lost = dict.fromkeys(undelivered)
    for conversation_id, workspace_id in lost:
        hub.set_status(conversation_id, text=NOT_DELIVERED)
        hub.publish(
            workspace_id,
            frame=StatusFrame(conversation_id=conversation_id, text=NOT_DELIVERED),
        )
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, workspace_id FROM chat_conversations "
            "WHERE partner_session_id = $1",
            session_id,
        )
    for row in rows:
        conversation = _Conversation.model_validate(dict(row))
        if (conversation.id, conversation.workspace_id) in lost:
            continue
        if hub.status_of(conversation.id):
            hub.set_status(conversation.id, text="")
            hub.publish(
                conversation.workspace_id,
                frame=StatusFrame(conversation_id=conversation.id, text=""),
            )


async def _partner_of(
    conn: Conn,
    *,
    conversation_id: uuid.UUID,
    api_key_id: uuid.UUID,
) -> _PartnerOf:
    """Check the key opened the conversation's live partner session."""
    row = await conn.fetchrow(
        "SELECT conversation.workspace_id, sess.owner AS actor "
        "FROM chat_conversations AS conversation "
        "LEFT JOIN inquiries AS sess ON sess.id = conversation.partner_session_id "
        "AND sess.kind = 'AgentSession' AND sess.status = 'active' "
        "AND sess.agentsession_ended IS NULL "
        "AND sess.agentsession_opened_by_api_key_id = $2 "
        "AND EXISTS (SELECT 1 FROM api_keys WHERE id = $2 AND revoked_at IS NULL) "
        "WHERE conversation.id = $1",
        conversation_id,
        api_key_id,
    )
    if row is None:
        raise ChatConversationNotFoundError("Conversation not found")
    partner = _PartnerOf.model_validate(dict(row))
    if partner.actor is None:
        raise ChatReplyForbiddenError("Not this conversation's partner session")
    return partner


async def _named_records(conn: Conn, *, text: str) -> list[uuid.UUID]:
    """Return the records ``text`` names as ``Kind#seq`` that exist, in its order."""
    named = list(
        dict.fromkeys(
            (from_plain(match.group(1), str), int(match.group(2)))
            for match in _NAMED.finditer(text)
        ),
    )[:_MOST_NAMED]
    if not named:
        return []
    rows = await conn.fetch(
        "SELECT id, kind, seq FROM inquiries WHERE (kind, seq) IN "
        "(SELECT * FROM unnest($1::text[], $2::bigint[]))",
        [kind for kind, _seq in named],
        [seq for _kind, seq in named],
    )
    found = {
        (record.kind, record.seq): record.id
        for record in (_Named.model_validate(dict(row)) for row in rows)
    }
    return [found[ref] for ref in named if ref in found]


async def _thread(
    conn: Conn,
    *,
    head: dict[str, object],
    after_seq: int | None,
) -> ChatThread:
    """Read a conversation's lines: the newest 500, or up to 500 after a number."""
    conversation_id = head["id"]
    if after_seq is None:
        rows = list(
            reversed(
                await conn.fetch(
                    "SELECT id, seq, role, author, text, created_at "
                    "FROM chat_messages WHERE conversation_id = $1 "
                    "ORDER BY seq DESC LIMIT $2",
                    conversation_id,
                    MAX_THREAD_MESSAGES,
                ),
            ),
        )
    else:
        rows = await conn.fetch(
            "SELECT id, seq, role, author, text, created_at FROM chat_messages "
            "WHERE conversation_id = $1 AND seq > $2 ORDER BY seq LIMIT $3",
            conversation_id,
            after_seq,
            MAX_THREAD_MESSAGES,
        )
    earlier = bool(rows) and bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM chat_messages "
            "WHERE conversation_id = $1 AND seq < $2)",
            conversation_id,
            rows[0]["seq"],
        ),
    )
    return ChatThread.model_validate(
        {**head, "earlier": earlier, "messages": [_message(row) for row in rows]},
    )


def _message(row: Record) -> ChatMessage:
    """Build a wire line from a ``chat_messages`` row."""
    return ChatMessage.model_validate({**dict(row), "created": row["created_at"]})


class _Located(BaseModel):
    """A conversation's canvas."""

    workspace_id: uuid.UUID


class _Conversation(BaseModel):
    """A conversation and its canvas."""

    id: uuid.UUID
    workspace_id: uuid.UUID


class _Named(BaseModel):
    """A record an answer named, and its id."""

    id: uuid.UUID
    kind: str
    seq: int
