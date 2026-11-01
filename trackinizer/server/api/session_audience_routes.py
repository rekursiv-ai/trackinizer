"""Hide a session from everyone but its owner and those who oversee, or show it again.

A session's owner is its account, or the person whose key opened it. A ``private``
session's transcript is read by its owner and by a person who holds ``staff`` or is
an admin. Only the
transcript is hidden: the row, its title, actor and cost, stays open, as a ``staff``
session's does. A ``staff`` session is never lowered, by its owner or anyone.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from trackinizer.server.api._deps import (
    get_store,
)
from trackinizer.server.api.session_access import require_session
from trackinizer.server.audience import may_hide
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.session_runs import opener_email
from trackinizer.types.errors import ConflictError
from trackinizer.wire.audience import OPEN, PRIVATE, STAFF
from trackinizer.wire.wire_sessions import (
    SESSION_AUDIENCE_PATH,
    SessionAudienceChange,
    SessionAudienceResponse,
)


router = APIRouter()


@router.post(SESSION_AUDIENCE_PATH)
async def set_session_audience_route(
    session_id: UUID,
    body: SessionAudienceChange,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionAudienceResponse:
    """Make a session ``private``, or ``open`` again.

    Only its owner does, from a browser or by key: anyone else, an admin included,
    is 403 ``session_not_owner``. A ``staff`` session is 409 ``session_staff``. Each
    session that moves is audited in the change log; asking for the audience it has
    moves nothing.

    Args:
      session_id: The session.
      body: The audience it takes.
      request: Request carrying the store.
      identity: The caller.

    Returns:
      response: The session, its audience, and every session that moved.

    """
    store = get_store(request)
    session = await require_session(store, session_id)
    async with store.engine.acquire() as conn:
        owners = (
            session.account,
            await opener_email(conn, api_key_id=session.opened_by_api_key_id) or "",
        )
        sessions = [session.id]
    if session.audience == STAFF:
        raise _staff()
    if not may_hide(identity.email, owners=owners, audience=session.audience):
        raise HTTPException(status_code=403, detail={"code": "session_not_owner"})
    try:
        moved = await store.set_session_audience(
            sessions,
            audience=body.audience,
            replacing=frozenset({OPEN, PRIVATE}) - {body.audience},
            refusing=frozenset({STAFF}),
            api_key_id=identity.api_key_id,
            actor=identity.email,
            reason="hidden by its owner" if body.audience == PRIVATE else "shown again",
        )
    except ConflictError as error:
        raise _staff() from error
    return SessionAudienceResponse(
        session_id=session_id,
        audience=body.audience,
        sessions=moved,
    )


def _staff() -> HTTPException:
    """Build the refusal to lower a ``staff`` session."""
    return HTTPException(status_code=409, detail={"code": "session_staff"})
