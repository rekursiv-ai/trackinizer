"""Routes of a live session: a viewer's stream, a person's inputs, and its process.

Declared ahead of the relay behind them, so the API's schema names every body and a
client can be written against it. Each route answers 501 ``not_built`` until the relay
is; ``wire_session_relay`` holds the paths and the bodies.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from trackinizer.server.api._routes_shared import refusal
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.wire.wire_session_relay import (
    SESSION_INPUTS_PATH,
    SESSION_LINES_PATH,
    SESSION_PROCESS_PATH,
    AgentProcess,
    InputSent,
    NewInput,
    ProcessSpec,
    ViewerFrame,
)


__all__ = [
    "read_process_route",
    "router",
    "send_input_route",
    "session_lines_route",
    "start_process_route",
    "stop_process_route",
]


router = APIRouter()


@router.get(
    SESSION_LINES_PATH,
    response_class=StreamingResponse,
    responses={200: {"model": ViewerFrame}},
)
async def session_lines_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> StreamingResponse:
    """Stream a session's lines to a viewer as server-sent frames.

    Each frame is ``data: <json>``, one :data:`ViewerFrame` told apart by its
    ``type``: an ``Opened`` first, then each line as it arrives.

    Args:
      session_id: The session to watch.
      request: Request carrying the relay.
      identity: A viewer who may read the session.

    Returns:
      stream: Server-sent frames.

    Raises:
      HTTPException: 501 until the relay is built.

    """
    del session_id, request, identity
    raise _not_built()


@router.post(SESSION_INPUTS_PATH, response_model=InputSent)
async def send_input_route(
    session_id: UUID,
    body: NewInput,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> InputSent:
    """Send a session's root agent a line, a stop or a slash line.

    Args:
      session_id: The session whose agent process takes it.
      body: What to send.
      request: Request carrying the relay.
      identity: Whoever may send the session an input.

    Returns:
      sent: The id the input's effect is told back under.

    Raises:
      HTTPException: 501 until the relay is built.

    """
    del session_id, body, request, identity
    raise _not_built()


@router.put(SESSION_PROCESS_PATH, response_model=AgentProcess)
async def start_process_route(
    session_id: UUID,
    body: ProcessSpec,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> AgentProcess:
    """Bring up a session's agent process: on any host, with any host's login.

    Args:
      session_id: The session to serve.
      body: The agent's host, its login's host, and the agent's flags.
      request: Request carrying the relay.
      identity: The session's account.

    Returns:
      process: The process, ``starting``; the one already serving when there is one.

    Raises:
      HTTPException: 501 until the relay is built.

    """
    del session_id, body, request, identity
    raise _not_built()


@router.get(SESSION_PROCESS_PATH, response_model=AgentProcess | None)
async def read_process_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> AgentProcess | None:
    """Return the agent process serving a session, or the last that did.

    Args:
      session_id: The session.
      request: Request carrying the relay.
      identity: A viewer who may read the session.

    Returns:
      process: The process; ``None`` for a session that has had none.

    Raises:
      HTTPException: 501 until the relay is built.

    """
    del session_id, request, identity
    raise _not_built()


@router.delete(SESSION_PROCESS_PATH, response_model=AgentProcess | None)
async def stop_process_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> AgentProcess | None:
    """Stop a session's agent process.

    Args:
      session_id: The session.
      request: Request carrying the relay.
      identity: The session's account.

    Returns:
      process: The process, ``detached``; ``None`` for a session that has had none.

    Raises:
      HTTPException: 501 until the relay is built.

    """
    del session_id, request, identity
    raise _not_built()


def _not_built() -> HTTPException:
    """Return the refusal every route here answers until the relay is built."""
    return refusal(
        501,
        code="not_built",
        message="The session relay is not built yet.",
    )
