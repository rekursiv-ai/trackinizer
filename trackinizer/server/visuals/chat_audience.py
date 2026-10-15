"""Make a science chat staff-only before its assistant reads a staff session into it.

The configured assistant reads a session for the person it answers (the ``asker`` of a
read by id) into the chat it answers them in (its ``chat``). What it reads lands in
the chat's session, which every member of the chat reads. So before it reads a
``staff`` session, every session under the chat's id becomes ``staff``: from then on
only the members who hold ``staff`` read the chat, post in it or are shared it, and
no one forks it. A ``private`` chat is never made ``staff``: one audience per session,
and ``staff`` would open a chat its starter hid to every member who holds it. Nothing
lowers a ``staff`` chat again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trackinizer.server.visuals.science_chats import read_chat_sessions
from trackinizer.wire.audience import OPEN, PRIVATE, STAFF


if TYPE_CHECKING:
    import uuid

    from trackinizer.server.store.core import Store


__all__ = ["raise_chat_audience"]


async def raise_chat_audience(
    store: Store,
    *,
    conversation_id: uuid.UUID,
    api_key_id: uuid.UUID | None,
    actor: str,
    reason: str,
) -> list[uuid.UUID]:
    """Make every ``open`` session under a chat's id ``staff``; refuse a private chat.

    Args:
      store: The store.
      conversation_id: The chat.
      api_key_id: The key the assistant reads with, for the audit.
      actor: Who reads, for the audit.
      reason: Why, for the audit: what was read and for whom.

    Returns:
      moved: The sessions made ``staff``; empty when the chat already was.

    Raises:
      ConflictError: A session under the chat's id is ``private``.

    """
    async with store.engine.acquire() as conn:
        sessions = await read_chat_sessions(conn, conversation_id=conversation_id)
    return await store.set_session_audience(
        sessions,
        audience=STAFF,
        replacing=frozenset({OPEN}),
        refusing=frozenset({PRIVATE}),
        api_key_id=api_key_id,
        actor=actor,
        reason=reason,
    )
