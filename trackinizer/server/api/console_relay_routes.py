"""Tell a canvas's chat what its owner sent agents directly from the Console."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from trackinizer.server.api._deps import get_assistant, get_inbound
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.visuals.console_relay import relay_console_line
from trackinizer.wire.wire_sessions import ConsoleRelay, ConsoleRelayReceipt


router = APIRouter()


@router.post("/api/canvases/{canvas_id}/relay", response_model=ConsoleRelayReceipt)
async def console_relay_route(
    canvas_id: uuid.UUID,
    body: ConsoleRelay,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> ConsoleRelayReceipt:
    """Tell the canvas's chat partner what the owner sent agents from the Console.

    The agents already have the message, sent to each by ``POST /api/messages``; this
    is the one request per Console line that follows them, naming the targets that
    accepted it. The partner records it in the canvas's chat as a line of the
    owner's that starts with ``→ `` and answers nothing, and reads it in its next
    prompt. A retry under the same key queues nothing again. A canvas without a
    chat, one whose chat the owner may not post into (one they do not read), or whose
    partner is not running or has too many unread lines, answers
    ``delivered: false``: the agents are not told otherwise, so it is no error.

    Args:
      canvas_id: The canvas the Console is in, which the caller must own.
      body: The agents the line went to, and its text.
      request: Request carrying the database engine and inbound queue.
      identity: The signed-in browser writer.
      key: Required retry-safe idempotency key.

    Returns:
      receipt: Whether the partner took the relay.

    """
    require_browser(identity)
    delivered = await relay_console_line(
        engine_of(request),
        user_id=identity.user_id,
        canvas_id=canvas_id,
        relay=body,
        key=key,
        source=identity.email,
        source_role=identity.role,
        inbound=get_inbound(request),
        assistant=get_assistant(request),
        reader=session_reader(request, identity),
    )
    if delivered is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return ConsoleRelayReceipt(delivered=delivered)
