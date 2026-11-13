"""Who opened a session: the account that owns the key that opened it."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast


if TYPE_CHECKING:
    import uuid

    from trackinizer.lib.postgres import Conn


__all__ = ["opener_email"]


async def opener_email(conn: Conn, *, api_key_id: uuid.UUID | None) -> str | None:
    """Return the email of the account that owns an API key.

    Args:
      conn: Database connection.
      api_key_id: The key that opened a session, if known.

    Returns:
      email: The owning account's email, or None for no or an unknown key.

    """
    if api_key_id is None:
        return None
    return cast(
        str | None,
        await conn.fetchval(
            "SELECT member.email FROM api_keys AS credential "
            "JOIN users AS member ON member.id = credential.user_id "
            "WHERE credential.id = $1",
            api_key_id,
        ),
    )
