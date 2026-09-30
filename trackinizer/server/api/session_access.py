"""Authorize a viewer to mutate only the AgentSession opened by its API key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException


if TYPE_CHECKING:
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.types.inquiries import AgentSession


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
