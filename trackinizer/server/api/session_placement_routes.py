"""Place a ``trax run`` session in a workstream and a station, and move it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from trackinizer.server.api._deps import get_hub, get_store
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.session_access import require_session
from trackinizer.server.api.visuals_routes import visual_catalog
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.session_placement import (
    move_session,
    place_session,
)
from trackinizer.wire.wire_sessions import (
    MoveSession,
    PlaceSession,
    SessionPlacement,
)


if TYPE_CHECKING:
    from trackinizer.types.inquiries import AgentSession


router = APIRouter()


@router.post("/api/sessions/{session_id}/place", response_model=SessionPlacement)
async def place_session_route(
    *,
    session_id: uuid.UUID,
    body: PlaceSession,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> SessionPlacement:
    """Place a session in the caller's workstream and a station; the server decides.

    The first rule that matches wins: the station the run started from, when it is the
    caller's; the placement the session already has; the workstream of the caller's
    newest earlier session on the same git branch, in a new station; else the caller's
    Scratch, in a new station. Placing a placed session returns its placement. A
    service account's session and a science chat are not placed: both ids are null.

    Args:
      session_id: The session, which the caller's own key opened.
      body: The station the run started from, and its git checkout.
      request: Request carrying the database engine and store.
      identity: The session's own account and key.

    Returns:
      placement: The workstream and station of the session.

    """
    session = await _own_session(request, identity=identity, session_id=session_id)
    placement = await place_session(
        engine_of(request),
        store=get_store(request),
        session=session,
        user_id=identity.user_id,
        email=identity.email,
        api_key_id=identity.api_key_id,
        body=body,
        catalog=visual_catalog(request).catalog(),
    )
    get_hub(request).nudge()
    return placement


@router.put("/api/sessions/{session_id}/workstream", status_code=204)
async def move_session_route(
    *,
    session_id: uuid.UUID,
    body: MoveSession,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> None:
    """Move a session to one of the caller's workstreams.

    The station made for the session moves with it when no other session sits on it.

    Args:
      session_id: The session, which the caller owns.
      body: The destination workstream Issue.
      request: Request carrying the database engine and store.
      identity: The session's account, as a browser or as its own key.

    """
    session = await _own_session(request, identity=identity, session_id=session_id)
    if not await move_session(
        engine_of(request),
        store=get_store(request),
        session=session,
        email=identity.email,
        api_key_id=identity.api_key_id,
        workstream_id=body.workstream_id,
    ):
        raise HTTPException(status_code=404, detail="Workstream not found")
    get_hub(request).nudge()


async def _own_session(
    request: Request,
    *,
    identity: AuthIdentity,
    session_id: uuid.UUID,
) -> AgentSession:
    """Return the caller's own session, else 404 or 403."""
    session = await require_session(get_store(request), session_id)
    if session.account != identity.email:
        raise HTTPException(status_code=403, detail="Own session required")
    if (
        identity.api_key_id is not None
        and identity.role == "viewer"
        and session.opened_by_api_key_id != identity.api_key_id
    ):
        raise HTTPException(status_code=403, detail="Own session key required")
    return session
