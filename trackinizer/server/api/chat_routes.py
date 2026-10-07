"""Canvas Chat conversations: history for the browser, replies from the partner."""

from __future__ import annotations

from typing import Annotated

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trackinizer.server.api._deps import get_hub
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import DeletedFrame
from trackinizer.server.visuals.chats import (
    ChatConversationNotFoundError,
    ChatReplyForbiddenError,
    awaiting_replies,
    delete_conversation,
    list_conversations,
    post_reply,
    read_partner_thread,
    read_thread,
)
from trackinizer.wire.wire_chats import (
    CHAT_MESSAGES_PATH,
    CHAT_PATH,
    CHATS_AWAITING_PATH,
    CHATS_PATH,
    AwaitingChat,
    ChatMessage,
    ChatReply,
    ChatSummary,
    ChatThread,
)


router = APIRouter()


@router.get(CHATS_PATH, response_model=list[ChatSummary])
async def list_chats_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[ChatSummary]:
    """List the signed-in user's conversations, newest change first, at most 50.

    Args:
      request: Request carrying the database engine.
      identity: Authenticated browser account.

    Returns:
      chats: The user's conversations.

    """
    require_browser(identity)
    return await list_conversations(engine_of(request), user_id=identity.user_id)


@router.get(CHATS_AWAITING_PATH, response_model=list[AwaitingChat])
async def awaiting_chats_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> list[AwaitingChat]:
    """List the conversations the caller's live session has yet to answer.

    For the agent key of a partner session. After a restart it names the user
    lines the partner drained but never answered: conversations whose partner is a
    live session the key opened and whose last line is the user's.

    Args:
      request: Request carrying the database engine.
      identity: The partner session's agent key.

    Returns:
      awaiting: Each conversation with its workspace and the user's last seq.

    """
    if identity.api_key_id is None:
        raise HTTPException(status_code=403, detail="Agent key required")
    return await awaiting_replies(engine_of(request), api_key_id=identity.api_key_id)


@router.get(CHAT_PATH, response_model=ChatThread)
async def read_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    after_seq: Annotated[int | None, Query(ge=0)] = None,
) -> ChatThread:
    """Read one conversation: its newest 500 lines, or those after a number.

    The owner's browser reads it, and so does the agent key of its live partner
    session, which is how an assistant that lost its memory of the thread reseeds
    itself. No one else may.

    Args:
      conversation_id: Conversation to read.
      request: Request carrying the database engine.
      identity: The owner's browser, or the partner session's agent key.
      after_seq: Return up to 500 lines numbered above this; without it, the newest 500.

    Returns:
      thread: The conversation and its lines.

    """
    if identity.api_key_id is not None:
        try:
            return await read_partner_thread(
                engine_of(request),
                api_key_id=identity.api_key_id,
                conversation_id=conversation_id,
                after_seq=after_seq,
            )
        except ChatConversationNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ChatReplyForbiddenError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
    thread = await read_thread(
        engine_of(request),
        user_id=identity.user_id,
        conversation_id=conversation_id,
        after_seq=after_seq,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return thread


@router.delete(CHAT_PATH, status_code=204)
async def delete_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> None:
    """Delete one of the user's conversations and its lines.

    The canvas's stream is told with a ``deleted`` frame.

    Args:
      conversation_id: Conversation to delete.
      request: Request carrying the database engine.
      identity: Authenticated browser account, which must own the conversation.

    """
    require_browser(identity)
    workspace_id = await delete_conversation(
        engine_of(request),
        user_id=identity.user_id,
        conversation_id=conversation_id,
    )
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    hub = get_hub(request)
    hub.forget(conversation_id)
    hub.publish(workspace_id, frame=DeletedFrame(conversation_id=conversation_id))


@router.post(CHAT_MESSAGES_PATH, response_model=ChatMessage | None)
async def reply_to_chat_route(
    conversation_id: uuid.UUID,
    body: ChatReply,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> ChatMessage | None:
    """Post an assistant's answer or status to a conversation.

    Only the API key that opened the conversation's live partner session may
    call this. An answer is stored as the partner's line and pushed; a status is
    pushed and never stored, and an empty one clears it.

    Args:
      conversation_id: Conversation the reply is for.
      body: The text and whether it is an answer or a status.
      request: Request carrying the database engine and the event hub.
      identity: Authenticated agent key.

    Returns:
      message: The stored answer; null for a status.

    """
    if identity.api_key_id is None:
        raise HTTPException(status_code=403, detail="Agent key required")
    try:
        return await post_reply(
            engine_of(request),
            conversation_id=conversation_id,
            api_key_id=identity.api_key_id,
            reply=body,
            hub=get_hub(request),
        )
    except ChatConversationNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ChatReplyForbiddenError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
