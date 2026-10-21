"""Science chat routes: post a line, find a conversation's session, share it, delete it.

A canvas Chat conversation is an AgentSession its provider answers in (see
:mod:`trackinizer.wire.wire_science_chat`). A line posted here is queued for the
provider, not stored: the provider records it as a record of the conversation's
session, and every viewer of that session sees it arrive as a change to the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response

from trackinizer.server.api._deps import (
    get_assistant,
    get_chat_orgs,
    get_hub,
    get_inbound,
    get_staff,
    get_store,
)
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.api.locks import require_unlocked
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.audience import members_reader
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.chat_hub import WorkspaceFrame
from trackinizer.server.inbound import IdempotencyReuseError
from trackinizer.server.visuals.chat_forks import (
    ChatClosedError,
    ChatRefusedError,
    ConversationTakenError,
    ForeignChatError,
)
from trackinizer.server.visuals.chat_shares import delete_chat, set_share
from trackinizer.server.visuals.science_chats import read_conversation, read_head
from trackinizer.server.visuals.workspace_store import (
    AnswerNotOwnerError,
    CommandRefusedError,
    PartnerBusyError,
    ProviderOfflineError,
    StationChatError,
    WorkspaceSessionUnavailableError,
    read_workspace,
    send_chat_line,
)
from trackinizer.wire.wire_science_chat import (
    CHAT_PATH,
    CHAT_SHARE_PATH,
    CHATS_PATH,
    ChatHead,
    ChatSend,
    ChatSent,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.server.visuals.chat_shares import UnboundStation


router = APIRouter()

ShareEmail = Annotated[str, Path(pattern=r"^[^@\s]+@[^@\s]+$", max_length=320)]


@router.post(CHATS_PATH, response_model=ChatSent)
async def post_chat_line_route(
    body: ChatSend,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
) -> ChatSent:
    """Post a line to a science chat, or start one, and queue it for the assistant.

    Whoever reads a chat posts into it: its starter, each person it is shared with
    and anyone the server's ``TRACKINIZER_CHAT_ORGS`` lets in, holding its audience;
    a viewer may not, and any other writer is refused with 403 ``chat_not_shared``,
    whether the chat is named by ``conversation_id`` or by the ``Idempotency-Key`` of
    a post that names none. A reader forks a chat from one of its lines (``fork``
    names it); a fork from a line of a chat the poster does not read is 422, since it
    would copy lines they may not read, a fork of a ``private`` or ``staff`` chat is
    409 ``chat_fork_closed``, since its copies would not keep the audience, a fork
    that the poster's organisation would read and the chat's readers do not
    include is 409 ``chat_fork_wider``, and a fork whose key names a conversation
    someone else began is refused with 409. A
    line under the id of a
    deleted chat, named or as a fork's key, or a fork from a line of one, is refused
    with 410 ``chat_deleted``: no chat starts under that id again. A station holds one
    chat: its first line binds the station to the line's conversation, and a line
    naming another conversation is refused with 409, as is one for a conversation
    another station of the poster holds. A fork goes to a new station, copied from
    the one it was sent from and named in the answer's ``workspace_id``. The line's
    poster is the attested identity, never anything in the body. The answer is the
    conversation id at once: the conversation's session appears when the assistant
    opens it, and ``GET /api/chats/{id}`` finds it. A retry under the same key
    queues nothing again.

    The poster's station's provider answers, or in a chat someone else started, the
    provider of its starter's station. ``assistant`` takes a ``line`` alone (422 for
    any other ``kind``). For ``claude`` or ``codex`` the post, with its ``kind`` and
    ``payload``, joins the queue of the chat's session that the CLI's run drains;
    with no such run live it is 409 ``provider_offline``. A fork from a ``claude`` or
    ``codex`` station is one too: its new session is opened at once and its line
    waits there for the run the person starts.

    A ``line`` that starts with a known slash command of claude or codex is judged
    by the chat's provider, not the poster's station. One the provider runs from
    Chat (``/goal``, and claude's ``/compact``) is queued as a ``command``, typed
    bare, with no page, trail or marks; any other is 422 ``command_refused`` with
    the reason in ``message``, and nothing is queued. Every other ``/`` line stays a
    line. An ``answer`` to a ``claude`` or ``codex`` chat is its starter's alone,
    the run's owner, since it runs a tool or grants trust on their machine: anyone
    else is 403 ``answer_not_owner``, and nothing is queued.

    Args:
      body: The line, the poster's station, and the conversation to post into or the
        line to fork; neither continues the station's chat, else starts one, named by
        ``key``.
      request: Request carrying the database engine and inbound queue.
      identity: The signed-in writer, who must own the canvas.
      key: Required retry-safe idempotency key.

    Returns:
      sent: The conversation, the station it went to, and its session when the line
        was queued on it.

    """
    require_browser(identity)
    assistant = get_assistant(request)
    try:
        sent = await send_chat_line(
            get_store(request),
            user_id=identity.user_id,
            body=body,
            key=key,
            source=identity.email,
            source_role=identity.role,
            inbound=get_inbound(request),
            assistant=assistant,
            reader=session_reader(request, identity),
            members=members_reader(
                identity.email,
                orgs=get_chat_orgs(request),
                assistant="" if assistant is None else assistant.email,
            ),
        )
    except ForeignChatError as error:
        raise HTTPException(
            status_code=403,
            detail={"code": "chat_not_shared", "message": str(error)},
        ) from error
    except AnswerNotOwnerError as error:
        raise HTTPException(
            status_code=403,
            detail={"code": "answer_not_owner", "message": str(error)},
        ) from error
    except CommandRefusedError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "command_refused", "message": str(error)},
        ) from error
    except ChatClosedError as error:
        raise HTTPException(status_code=409, detail={"code": error.code}) from error
    except ProviderOfflineError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "provider_offline", "message": str(error)},
        ) from error
    except (
        WorkspaceSessionUnavailableError,
        PartnerBusyError,
        IdempotencyReuseError,
        ConversationTakenError,
        StationChatError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if sent is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return sent


@router.get(CHAT_PATH, response_model=ChatHead)
async def read_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> ChatHead:
    """Find a conversation's session; 404 until the assistant has opened it.

    A chat that was deleted is 410 ``chat_deleted``.

    Args:
      conversation_id: The conversation.
      request: Request carrying the database engine.
      identity: The signed-in user; the head of every chat of the assistant is found
        for every user, and of a chat of a user's own helper for that user. Its
        transcript is read by the chat's members alone.

    Returns:
      head: The session, its starter, whether the assistant still has it open, how
        often it was forked, what it was forked from, its audience, whether the
        caller does not read it, who it is shared with, and whether the caller may
        hide, share or delete it.

    """
    head = await read_head(
        engine_of(request),
        assistant=get_assistant(request),
        caller=identity,
        conversation_id=conversation_id,
        reader=session_reader(request, identity),
    )
    if head is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return head


@router.put(CHAT_SHARE_PATH, response_model=list[str])
async def share_chat_route(
    conversation_id: uuid.UUID,
    email: ShareEmail,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> list[str]:
    """Share a chat with one person, who then reads it and posts as its starter does.

    Only the chat's starter shares it, from a browser: an API key is 403, and anyone
    else, an admin included, is 403 ``chat_not_starter``, since a share lets its
    person read the chat. A ``staff`` chat is shared only with a person who holds
    ``staff`` now, else 409 ``chat_staff_only``. Sharing again changes nothing.

    Args:
      conversation_id: The chat.
      email: The person's email; stored lowercase.
      request: Request carrying the database engine.
      identity: The signed-in writer.

    Returns:
      shared_with: The chat's shares, oldest first.

    """
    return await _set_share(
        request,
        identity=identity,
        conversation_id=conversation_id,
        email=email,
        shared=True,
    )


@router.delete(CHAT_SHARE_PATH, response_model=list[str])
async def unshare_chat_route(
    conversation_id: uuid.UUID,
    email: ShareEmail,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> list[str]:
    """Stop sharing a chat with one person: they read none of it, their past lines stay.

    Only the chat's starter or an admin unshares it, from a browser: an API key is
    403, and anyone else is 403 ``chat_not_starter``. Unsharing a person it is not
    shared with changes nothing.

    Args:
      conversation_id: The chat.
      email: The person's email.
      request: Request carrying the database engine.
      identity: The signed-in writer.

    Returns:
      shared_with: The chat's shares, oldest first.

    """
    return await _set_share(
        request,
        identity=identity,
        conversation_id=conversation_id,
        email=email,
        shared=False,
    )


@router.delete(CHAT_PATH, status_code=204)
async def delete_chat_route(
    conversation_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Delete a chat: every session of it with its records, its shares, its stations.

    An admin deletes any chat, and so does the key that opened every session of it;
    any other API key is 403, as only a person's browser deletes a chat otherwise.
    Its starter deletes it while every person's line in it is theirs, a line a fork
    copied counting as its original poster's and a line on its way in, queued or
    taken by the assistant and not yet recorded, as posted; once someone else has
    posted they are 403 ``chat_has_other_posters``, and anyone else is 403
    ``chat_not_starter``. A session that is locked, or linked to a locked row, needs
    an admin. Each station that held the chat holds none after, at its next
    revision, and its open tabs get a ``workspace`` frame saying so. The chat's
    forks stay, and so do its sessions' change log rows, without the titles and
    descriptions they held. Its queued lines go, and its id starts no chat again: a
    session, a line or a station under it is 410 ``chat_deleted``.

    Args:
      conversation_id: The chat.
      request: Request carrying the store, the inbound queue and the event hub.
      identity: The signed-in writer, or an API key.

    Returns:
      response: 204; 404 when the caller finds no such chat.

    """
    assistant = get_assistant(request)
    store = get_store(request)
    async with store.engine.acquire() as conn:
        found = await read_conversation(
            conn,
            assistant=assistant,
            caller=identity,
            conversation_id=conversation_id,
        )
    if found is not None:
        # An agent's key acts for its owner unseen, so only a person's browser deletes
        # a chat, save the assistant's own key deleting the chat it opened.
        if not found.opened:
            require_browser(identity)
        await require_unlocked(request, identity, found.sessions, include_peers=True)
    try:
        stations = await delete_chat(
            store,
            assistant=assistant,
            caller=identity,
            conversation_id=conversation_id,
            inbound=get_inbound(request),
        )
    except ChatRefusedError as error:
        raise HTTPException(status_code=403, detail={"code": error.code}) from error
    if stations is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await _publish_stations(request, stations=stations)
    return Response(status_code=204)


async def _set_share(
    request: Request,
    identity: AuthIdentity,
    *,
    conversation_id: uuid.UUID,
    email: str,
    shared: bool,
) -> list[str]:
    """Share a chat with ``email`` or stop, answering 403, 404 and 409 as routes do."""
    require_browser(identity)
    try:
        shared_with = await set_share(
            engine_of(request),
            assistant=get_assistant(request),
            caller=identity,
            conversation_id=conversation_id,
            email=email,
            shared=shared,
            staff=get_staff(request),
        )
    except ChatRefusedError as error:
        raise HTTPException(status_code=403, detail={"code": error.code}) from error
    except ChatClosedError as error:
        raise HTTPException(status_code=409, detail={"code": error.code}) from error
    if shared_with is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return shared_with


# Published after the delete commits, so a tab never reads a station still holding it.
async def _publish_stations(
    request: Request,
    stations: Sequence[UnboundStation],
) -> None:
    """Send each station's open tabs a ``workspace`` frame without the deleted chat."""
    hub = get_hub(request)
    for station in stations:
        state = await read_workspace(
            engine_of(request),
            user_id=station.user_id,
            workspace_id=station.workspace_id,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
        )
        if state is not None:
            hub.publish(station.workspace_id, frame=WorkspaceFrame(state=state))
