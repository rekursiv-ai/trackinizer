"""Chat uploads: a page asks the assistant about files in its box; the assistant answers.

A page asks for upload links, for a download link to a file it uploaded, or for that
file's removal. The bytes go between the page and the box, never through here (see
:mod:`trackinizer.wire.wire_chat_uploads`). This module only says who may ask,
by the rule a line is posted by, and carries the ask to the assistant and its answer
back through :class:`~trackinizer.server.upload_relay.UploadRelay`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, Literal

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from trackinizer.server.api._deps import (
    get_assistant,
    get_inbound,
    get_store,
    get_uploads,
)
from trackinizer.server.api._routes_shared import require_browser
from trackinizer.server.api.session_access import session_reader
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.upload_relay import UploadsBusyError
from trackinizer.server.visuals.canvas_store import (
    CanvasChatError,
    UploadsUnsupportedError,
    check_chat_upload,
)
from trackinizer.server.visuals.chat_forks import ForeignChatError
from trackinizer.wire.wire_chat_uploads import (
    CHAT_FILE_LINK_PATH,
    CHAT_FILE_REMOVE_PATH,
    CHAT_UPLOAD_ANSWER_PATH,
    CHAT_UPLOADS_PATH,
    CHAT_UPLOADS_WAIT_PATH,
    UPLOADS_WAIT_MAX_SEC,
    ChatFileAsk,
    ChatFileLink,
    ChatUploadAsk,
    ChatUploadGrant,
    UploadAnswer,
    UploadAsk,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.wire.wire_chat_uploads import ChatUploadFile


router = APIRouter()

_ASK_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    409: {
        "description": "The chat's provider takes no files, or the assistant "
        "refused (code ``uploads_unsupported`` or ``upload_refused``).",
    },
    503: {
        "description": "No assistant answered, or too many asks are waiting "
        "(code ``uploads_unavailable`` or ``uploads_busy``).",
    },
}


@router.post(
    CHAT_UPLOADS_PATH,
    response_model=ChatUploadGrant,
    responses=_ASK_RESPONSES,
)
async def ask_chat_uploads_route(
    body: ChatUploadAsk,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> ChatUploadGrant:
    """Ask the assistant for a link per file to its box, for a line of this chat.

    Whoever may post a line to the chat from this canvas may ask, and no one else:
    a foreign canvas is 404, a chat not the poster's to post into is 403
    ``chat_not_shared``, a deleted one 410, a chat other than the one the canvas
    holds 409, and a chat whose provider is not the assistant 409
    ``uploads_unsupported``. The ask waits for the assistant, which
    signs one link per file for the poster's box: an assistant that does not
    answer in time is 503 ``uploads_unavailable``, and its refusal is 409
    ``upload_refused`` with its reason in ``message``. While as many asks as the
    relay holds are waiting, another is 503 ``uploads_busy`` at once. Nothing is
    stored.

    Args:
      body: The canvas, the chat, and each file's name and size.
      request: Request carrying the store, the inbound queue and the relay.
      identity: The signed-in writer, who must own the canvas.

    Returns:
      grant: A box path and a link per file, in the order asked.

    """
    answer = await _relayed(
        request,
        identity,
        canvas_id=body.canvas_id,
        conversation_id=body.conversation_id,
        action="upload",
        files=body.files,
    )
    if answer.files is None or [each.name for each in answer.files] != [
        each.name for each in body.files
    ]:
        raise _bad_answer("other files than those asked for")
    return ChatUploadGrant(files=answer.files)


@router.post(
    CHAT_FILE_LINK_PATH,
    response_model=ChatFileLink,
    responses=_ASK_RESPONSES,
)
async def link_chat_file_route(
    body: ChatFileAsk,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> ChatFileLink:
    """Ask the assistant for a download link to a file the poster uploaded.

    Asked as an upload is, by the same rule and with the same refusals; the assistant
    signs a link for the poster's own box alone, so only they download the file, and
    refuses a path that is not one of their uploads or datasets.

    Args:
      body: The canvas, the chat, and the file's box path.
      request: Request carrying the store, the inbound queue and the relay.
      identity: The signed-in writer, who must own the canvas.

    Returns:
      link: The download link, signed for the path alone.

    """
    answer = await _relayed(
        request,
        identity,
        canvas_id=body.canvas_id,
        conversation_id=body.conversation_id,
        action="link",
        path=body.path,
    )
    if answer.url is None:
        raise _bad_answer("no link")
    return ChatFileLink(url=answer.url)


@router.post(CHAT_FILE_REMOVE_PATH, status_code=204, responses=_ASK_RESPONSES)
async def remove_chat_file_route(
    body: ChatFileAsk,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Ask the assistant to remove a file the poster uploaded, as a cancel does.

    Asked as an upload is, by the same rule and with the same refusals; the assistant
    removes only a file under the poster's own uploads or datasets.

    Args:
      body: The canvas, the chat, and the file's box path.
      request: Request carrying the store, the inbound queue and the relay.
      identity: The signed-in writer, who must own the canvas.

    Returns:
      response: Empty, once the file is gone.

    """
    answer = await _relayed(
        request,
        identity,
        canvas_id=body.canvas_id,
        conversation_id=body.conversation_id,
        action="remove",
        path=body.path,
    )
    if answer.removed is None:
        raise _bad_answer("no removal")
    return Response(status_code=204)


@router.get(CHAT_UPLOADS_WAIT_PATH, response_model=UploadAsk | None)
async def wait_chat_upload_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
    wait_sec: Annotated[float, Query(ge=0, le=UPLOADS_WAIT_MAX_SEC)] = 20,
) -> UploadAsk | None:
    """Take the oldest ask for upload links, holding the request until one comes.

    The configured assistant's key alone, since it alone signs links to the boxes;
    anyone else is 403.

    Args:
      request: Request carrying the relay.
      identity: The assistant's key.
      wait_sec: The longest to hold the request.

    Returns:
      ask: Who asks, from which canvas and chat, for which files; ``None`` when
        none came in time.

    """
    _require_assistant_key(request, identity)
    return await get_uploads(request).wait(wait_sec=wait_sec)


@router.post(CHAT_UPLOAD_ANSWER_PATH, status_code=204)
async def answer_chat_upload_route(
    request_id: uuid.UUID,
    body: UploadAnswer,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Answer an ask the assistant was handed: a slot per file, or why there are none.

    The assistant's key alone (403 otherwise). The first answer to an ask is taken;
    a later one, or one for an ask whose page gave up, is 404.

    Args:
      request_id: The ask's id, as the wait handed it.
      body: The slots, or the reason.
      request: Request carrying the relay.
      identity: The assistant's key.

    Returns:
      response: Empty.

    """
    _require_assistant_key(request, identity)
    if not get_uploads(request).answer(request_id, body):
        raise HTTPException(status_code=404, detail="No such upload ask waiting")
    return Response(status_code=204)


# Its key only: a browser session of the assistant's account would pass no origin
# check here, and only the assistant reaches the boxes the links are signed for.
def _require_assistant_key(request: Request, identity: AuthIdentity) -> None:
    """Refuse every caller but the configured assistant's key."""
    assistant = get_assistant(request)
    if (
        identity.api_key_id is None
        or assistant is None
        or identity.email.lower() != assistant.email.lower()
    ):
        raise HTTPException(status_code=403, detail="The assistant's key only")


# Every refusal is raised as the HTTP error the routes document, the assistant's own
# included, so the answer returned holds what its action asked for.
async def _relayed(
    request: Request,
    identity: AuthIdentity,
    *,
    canvas_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    action: Literal["upload", "link", "remove"],
    files: Sequence[ChatUploadFile] = (),
    path: str | None = None,
) -> UploadAnswer:
    """Check the poster may post to the chat, relay the ask, and return its answer."""
    require_browser(identity)
    try:
        target = await check_chat_upload(
            get_store(request),
            user_id=identity.user_id,
            canvas_id=canvas_id,
            conversation_id=conversation_id,
            source=identity.email,
            assistant=get_assistant(request),
            inbound=get_inbound(request),
            reader=session_reader(request, identity),
        )
    except ForeignChatError as error:
        raise HTTPException(
            status_code=403,
            detail={"code": "chat_not_shared", "message": str(error)},
        ) from error
    except UploadsUnsupportedError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "uploads_unsupported", "message": str(error)},
        ) from error
    except CanvasChatError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if target is None:
        raise HTTPException(status_code=404, detail="Canvas not found")
    try:
        answer = await get_uploads(request).ask(
            canvas_id=canvas_id,
            conversation_id=target.conversation_id,
            workspace_id=target.workspace_id,
            asker=identity.email,
            asker_role=identity.role,
            action=action,
            files=files,
            path=path,
        )
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "uploads_unavailable",
                "message": "The assistant did not answer; try again.",
            },
        ) from error
    except UploadsBusyError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "uploads_busy",
                "message": "Too many uploads are waiting; try again shortly.",
            },
        ) from error
    if answer.error is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_refused", "message": answer.error},
        )
    return answer


def _bad_answer(what: str) -> HTTPException:
    """Return the 502 for an assistant that answered an ask with the wrong thing."""
    return HTTPException(
        status_code=502,
        detail=f"The assistant answered with {what}.",
    )
