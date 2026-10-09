"""Science chat routes: post a line, list a person's chats, find a conversation's session.

A canvas Chat conversation is an AgentSession the assistant opens (see
:mod:`trackinizer.wire.wire_science_chat`). A line posted here is queued for the
assistant, not stored: the assistant records it as a record of the conversation's
session, and every viewer of that session sees it arrive as a change to the session.
"""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from trackinizer.server.api._deps import get_assistant, get_chat_orgs, get_inbound
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.inbound import IdempotencyReuseError
from trackinizer.server.visuals.chat_forks import (
    ConversationTakenError,
    ForeignChatError,
)
from trackinizer.server.visuals.science_chats import list_chats, read_head
from trackinizer.server.visuals.workspace_store import (
    PartnerBusyError,
    WorkspaceContextChangedError,
    WorkspaceSessionUnavailableError,
    send_chat_line,
)
from trackinizer.wire.wire_science_chat import (
    CHAT_PATH,
    CHATS_PATH,
    ChatHead,
    ChatSend,
    ChatSent,
    ChatSummary,
)


router = APIRouter()


@router.post(CHATS_PATH, response_model=ChatSent)
async def post_chat_line_route(
    body: ChatSend,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> ChatSent:
    """Post a line to a science chat, or start one, and queue it for the assistant.

    Any signed-in writer in a chat's organisation may post into it; a viewer may not,
    and a writer outside it is refused with 403 and forks it instead (``fork`` names the
    line to start from), whether the chat is named by ``conversation_id`` or by the
    ``Idempotency-Key`` of a post that names none; a fork whose key names a
    conversation someone else began is refused with 409. The line's poster is the
    attested identity, never anything in the body. The answer is
    the conversation id at once: the conversation's session appears when the
    assistant opens it, and ``GET /api/chats/{id}`` finds it. A retry under the same
    key queues nothing again.

    Args:
      body: The line, the poster's canvas, and the conversation to post into or the
        line to fork; neither starts one, named by ``key``.
      request: Request carrying the database engine and inbound queue.
      identity: The signed-in writer, who must own the canvas.
      key: Required retry-safe idempotency key.

    Returns:
      sent: The conversation, and its session when the assistant has it open.

    """
    require_browser(identity)
    try:
        sent = await send_chat_line(
            engine_of(request),
            user_id=identity.user_id,
            body=body,
            key=key,
            source=identity.email,
            source_role=identity.role,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
            orgs=get_chat_orgs(request),
        )
    except ForeignChatError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (
        WorkspaceSessionUnavailableError,
        PartnerBusyError,
        WorkspaceContextChangedError,
        IdempotencyReuseError,
        ConversationTakenError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if sent is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sent


@router.get(CHATS_PATH, response_model=list[ChatSummary])
async def list_chats_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[ChatSummary]:
    """List the science chats the caller started or posted in, newest first, at most 50.

    Args:
      request: Request carrying the database engine.
      identity: The signed-in user.

    Returns:
      chats: The caller's history. Any other science chat opens by its link.

    """
    return await list_chats(
        engine_of(request),
        assistant=get_assistant(request),
        email=identity.email,
    )


@router.get(CHAT_PATH, response_model=ChatHead)
async def read_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> ChatHead:
    """Find a conversation's session; 404 until the assistant has opened it.

    Args:
      conversation_id: The conversation.
      request: Request carrying the database engine.
      identity: The signed-in user; every chat of the assistant is open to every
        user, and a chat of a user's own helper to that user.

    Returns:
      head: The session, its starter, whether the assistant still has it open, how
        often it was forked, what it was forked from, and whether the caller's typing
        forks it.

    """
    head = await read_head(
        engine_of(request),
        assistant=get_assistant(request),
        email=identity.email,
        conversation_id=conversation_id,
        orgs=get_chat_orgs(request),
    )
    if head is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return head
