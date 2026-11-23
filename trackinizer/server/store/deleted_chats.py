"""A deleted science chat's id: the lock its delete holds, and the refusal to reopen it.

Deleting a chat leaves a row in ``science_chat_tombstones``, and nothing may start the
chat again under its id: not a session, not a line, not a station. Each of those, and
the delete, holds the conversation's lock for its whole transaction, so a start that
found no tombstone has written its row before the delete reads what it takes, and one
that comes after finds the tombstone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.types.errors import ChatDeletedError


if TYPE_CHECKING:
    import uuid

    from trackinizer.lib.postgres import Conn


__all__ = [
    "chat_was_deleted",
    "lock_chat_id",
    "refuse_a_deleted_chat",
    "session_was_purged",
]


async def lock_chat_id(conn: Conn, *, conversation_id: uuid.UUID) -> None:
    """Hold a conversation's id until ``conn``'s transaction ends.

    Args:
      conn: A connection inside a transaction.
      conversation_id: The conversation.

    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"trackinizer.science_chat.{conversation_id}",
    )


async def chat_was_deleted(conn: Conn, *, conversation_id: uuid.UUID) -> bool:
    """Say whether a conversation was deleted, so nothing may start it again.

    Args:
      conn: A database connection.
      conversation_id: The conversation.

    Returns:
      deleted: True when its tombstone stands.

    """
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM science_chat_tombstones "
            "WHERE conversation_id = $1)",
            conversation_id,
        ),
    )


async def refuse_a_deleted_chat(
    conn: Conn,
    *,
    conversation_id: uuid.UUID,
    forked_from: uuid.UUID | None = None,
) -> None:
    """Hold a conversation's id, and the chat's it forks, and refuse it if deleted.

    A fork holds its original too, so the original's delete commits before the fork
    is checked or after its line is queued, never between. Two ids are taken smaller
    first, as every taker of two does, so no two wait on each other.

    Args:
      conn: A connection inside the transaction that would start the chat again.
      conversation_id: The conversation.
      forked_from: The conversation a fork starts from, if this is one.

    Raises:
      ChatDeletedError: Its tombstone stands.

    """
    for held in sorted({conversation_id, forked_from or conversation_id}):
        await lock_chat_id(conn, conversation_id=held)
    if await chat_was_deleted(conn, conversation_id=conversation_id):
        raise ChatDeletedError("This chat was deleted; start a new one")


async def session_was_purged(conn: Conn, *, session_id: uuid.UUID) -> bool:
    """Say whether a session was purged, as a deleted chat's sessions are.

    Args:
      conn: A database connection.
      session_id: The session.

    Returns:
      purged: True when the change log holds its ``purged`` row.

    """
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM change_log "
            "WHERE subject_id = $1 AND kind = 'purged')",
            session_id,
        ),
    )
