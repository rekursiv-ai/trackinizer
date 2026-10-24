"""List the caller's own sessions, newest activity first, each with its run and place."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from trackinizer.server.api._deps import get_inbound
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.session_list import (
    MAX_DAYS,
    MAX_SESSIONS,
    SessionList,
    list_own_sessions,
)
from trackinizer.wire.wire_sessions import SESSIONS_PATH


router = APIRouter()


@router.get(SESSIONS_PATH, response_model=SessionList)
async def list_sessions_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 14,
    limit: Annotated[int, Query(ge=1, le=MAX_SESSIONS)] = MAX_SESSIONS,
) -> SessionList:
    """List the caller's sessions active in the last ``days`` days or still open.

    Each says whether a run drains it now and on which machine (the run's lease,
    else where it last ran), when it last took a turn, its goal and first line, the
    canvas and workspace it sits in, and the Issues it was produced by; the Issues
    come once each, with the Issues they narrow or were produced by. A session the
    caller does not read is left out.

    Args:
      request: Request carrying the database engine and the poller leases.
      identity: The caller, whose account the sessions carry.
      days: How far back the last activity may be.
      limit: The most sessions to list, newest activity first.

    Returns:
      listed: The sessions and their Issues, and whether more matched.

    """
    return await list_own_sessions(
        engine_of(request),
        email=identity.email,
        user_id=identity.user_id,
        reader=session_reader(request, identity),
        inbound=get_inbound(request),
        days=days,
        limit=limit,
    )
