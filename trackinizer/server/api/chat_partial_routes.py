"""The answer a science chat's assistant is writing: put by it, read by the chat's readers.

While a turn runs, the assistant puts the answer so far a few times a second, and
each tab that shows the chat while it owes an answer holds a read open until the
partial changes. The partial lives in this process's memory alone
(:class:`~trackinizer.server.chat_partials.ChatPartials`): no write reaches the
database, the change log or NOTIFY, and the stored answer, which lands as every
record does, replaces it.

Only the configured assistant writes a partial, through the key that opened the
chat's session. A read takes the same test as a read of the session's records, and
a read that was held takes it again before it answers with what changed: a reader
the chat was unshared from, or raised to ``staff`` above, while the read was held is
refused rather than shown what the assistant wrote since.

The server runs one process (``server.py`` takes no ``--workers``), as the inbound
queue and the Chat hub need too. Several would each hold their own partials and
seqs: a reader whose reads alternate between them would be answered at once each
time, a request loop under a flickering bubble. They would need each session pinned
to one process, or a channel they share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import math

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trackinizer.server.api._deps import (
    get_assistant,
    get_chat_streaming,
    get_partials,
    get_store,
)
from trackinizer.server.api.session_access import (
    is_a_science_chat,
    require_session,
    require_session_access,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.wire.wire_science_chat import (
    CHAT_PARTIAL_PATH,
    CHAT_PARTIAL_TURN_PATH,
    CHAT_PARTIAL_WAIT_SEC,
    ChatPartial,
    ChatPartialState,
)


if TYPE_CHECKING:
    from trackinizer.types.inquiries import AgentSession


router = APIRouter()


@router.put(CHAT_PARTIAL_PATH, status_code=204)
async def put_chat_partial_route(
    session_id: UUID,
    body: ChatPartial,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Hold the answer a chat's assistant is writing, in place of the one held.

    Refused in this order: 404 ``chat_partials_off`` when the server streams no
    answers; 403 ``not_the_assistant`` from anyone but the configured assistant; 404
    for no session; 404 ``not_a_chat`` for a session that is no science chat; 403
    from a key that did not open the session; 409 ``session_ended`` once it ended.

    Args:
      session_id: The chat's session.
      body: The answer so far, its turn, and whether it is whole.
      request: Request carrying the store and the partials.
      identity: The assistant, by the key that opened the session.

    Returns:
      response: Empty.

    """
    session = await _require_writer(request, identity, session_id=session_id)
    if session.status != "active" or session.ended is not None:
        raise HTTPException(status_code=409, detail={"code": "session_ended"})
    _ = get_partials(request).put(session_id, body)
    return Response(status_code=204)


@router.delete(CHAT_PARTIAL_TURN_PATH, status_code=204)
async def delete_chat_partial_route(
    session_id: UUID,
    turn: str,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Drop the chat's partial if it is of ``turn``; a later turn's stays.

    Refused as a put is, but for an ended session, whose partial may still go.

    Args:
      session_id: The chat's session.
      turn: The turn whose partial ends.
      request: Request carrying the store and the partials.
      identity: The assistant, by the key that opened the session.

    Returns:
      response: Empty, whether or not a partial was dropped.

    """
    _ = await _require_writer(request, identity, session_id=session_id)
    get_partials(request).clear(session_id, turn=turn)
    return Response(status_code=204)


@router.get(CHAT_PARTIAL_PATH)
async def read_chat_partial_route(
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    *,
    after: int = 0,
    wait_sec: float = 0.0,
) -> ChatPartialState:
    """Read the answer the chat's assistant is writing, once it is not the one seen.

    Answers at once when the held partial's ``seq`` is not ``after``; else when it
    changes, or when it expires; else after ``wait_sec`` with it unchanged. 404
    ``chat_partials_off`` when the server streams no answers, 404 for no session,
    and 403 ``session_audience`` to a caller who does not read the session's records,
    asked again before a changed partial is answered.

    Args:
      session_id: The chat's session.
      request: Request carrying the store and the partials.
      identity: A reader of the chat.
      after: The ``seq`` the caller last saw; 0 for none.
      wait_sec: Seconds to hold the request for a change, clamped to [0, 25].

    Returns:
      state: The partial held, and its ``seq``; ``seq`` 0 and none when none is.

    """
    _require_streaming(request)
    await _require_reader(request, identity, session_id=session_id)
    state = await get_partials(request).wait(
        session_id,
        after=after,
        # The query parses "nan", which every bound below would let through.
        wait_sec=0.0
        if math.isnan(wait_sec)
        else min(max(wait_sec, 0.0), CHAT_PARTIAL_WAIT_SEC),
    )
    if state.seq != after:
        # The hold outlasts the test: the chat may have been unshared from the
        # reader, or raised to staff for the read whose answer is now streaming.
        await _require_reader(request, identity, session_id=session_id)
    return state


async def _require_reader(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> None:
    """Refuse a read of a partial from whoever does not read the session's records."""
    await require_session_access(
        request,
        identity,
        session=await require_session(get_store(request), session_id),
        mode="read",
    )


async def _require_writer(
    request: Request,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> AgentSession:
    """Refuse a write of a partial from anyone but the assistant's opening key."""
    _require_streaming(request)
    assistant = get_assistant(request)
    if assistant is None or identity.email != assistant.email:
        raise HTTPException(status_code=403, detail={"code": "not_the_assistant"})
    session = await require_session(get_store(request), session_id)
    if not is_a_science_chat(session):
        raise HTTPException(status_code=404, detail={"code": "not_a_chat"})
    if identity.api_key_id is None or identity.api_key_id != (
        session.opened_by_api_key_id
    ):
        raise HTTPException(
            status_code=403,
            detail="A science chat is written by the key that opened it",
        )
    return session


def _require_streaming(request: Request) -> None:
    """Refuse every partial route on a server that streams no answers."""
    if not get_chat_streaming(request):
        raise HTTPException(status_code=404, detail={"code": "chat_partials_off"})
