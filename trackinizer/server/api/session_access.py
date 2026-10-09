"""Authorize who may mutate an AgentSession.

A viewer key mutates only the session it opened. A science chat is stricter: it is
public to every signed-in user, so any writer may read it, but only the key that
opened it writes it. Its lines name their poster, and its labels and account say who
started it and who posted, so a second writer's write would forge a poster or hide the
chat from History. A chat is known by its ``chat:`` id alone, whoever opened it and
whether or not an assistant is configured: a user's own ``trax helper`` opens chats
under that user's key, and answers the last line of one with that user's local CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid

from fastapi import HTTPException

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._deps import get_store
from trackinizer.wire.wire_science_chat import CHAT_SESSION_PREFIX


if TYPE_CHECKING:
    from fastapi import Request

    from trackinizer.server.auth import AuthIdentity
    from trackinizer.types.inquiries import AgentSession


__all__ = [
    "is_a_science_chat",
    "require_chat_opener",
    "require_chat_opener_of",
    "require_session_write_access",
]


def require_session_write_access(identity: AuthIdentity, session: AgentSession) -> None:
    """Keep viewer capture scoped to the credential that opened the session.

    Args:
      identity: Authenticated caller and its API key.
      session: Target session with its opening key attribution.

    Raises:
      HTTPException: A viewer did not open this session with its own key.

    """
    if identity.role == "viewer" and (
        identity.api_key_id is None
        or session.opened_by_api_key_id != identity.api_key_id
    ):
        raise HTTPException(status_code=403, detail="Own session key required")


def is_a_science_chat(session: AgentSession) -> bool:
    """Say whether ``session`` is a science chat: one under a ``chat:`` id.

    Args:
      session: The session.

    Returns:
      chat: Whether its ``cli_session_id`` starts with ``chat:``.

    """
    return (session.cli_session_id or "").startswith(CHAT_SESSION_PREFIX)


def require_chat_opener(identity: AuthIdentity, *, session: AgentSession) -> None:
    """Refuse a write to a science chat from any key but the one that opened it.

    Args:
      identity: Authenticated caller and its API key.
      session: Target session.

    Raises:
      HTTPException: ``session`` is a science chat and the caller did not open it.

    """
    _refuse_a_stranger(
        identity,
        cli_session_id=session.cli_session_id,
        opener=session.opened_by_api_key_id,
    )


async def require_chat_opener_of(
    request: Request,
    identity: AuthIdentity,
    target_id: uuid.UUID,
) -> None:
    """Apply :func:`require_chat_opener` to any inquiry id; other rows pass.

    Args:
      request: The request, which carries the store.
      identity: Authenticated caller and its API key.
      target_id: The row a route is about to change.

    Raises:
      HTTPException: ``target_id`` is a science chat the caller did not open.

    """
    async with get_store(request).engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agentsession_cli_session_id AS cli_session_id, "
            "agentsession_opened_by_api_key_id AS opener FROM inquiries "
            "WHERE id = $1 AND kind = 'AgentSession'",
            target_id,
        )
    if row is not None:
        _refuse_a_stranger(
            identity,
            cli_session_id=from_plain(row.get("cli_session_id"), str, default=""),
            opener=from_plain(row.get("opener"), uuid.UUID, default=None),
        )


def _refuse_a_stranger(
    identity: AuthIdentity,
    *,
    cli_session_id: str | None,
    opener: uuid.UUID | None,
) -> None:
    """Refuse the write to a science chat that ``identity`` did not open."""
    if identity.api_key_id != opener and (cli_session_id or "").startswith(
        CHAT_SESSION_PREFIX,
    ):
        raise HTTPException(
            status_code=403,
            detail="A science chat is written by the key that opened it",
        )
