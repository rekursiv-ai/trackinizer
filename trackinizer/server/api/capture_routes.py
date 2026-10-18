"""A picture of the canvas, asked for by its agent and taken by its owner's page."""

from __future__ import annotations

from typing import Annotated, Final

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from trackinizer.server.api._deps import (
    get_assistant,
    get_hub,
    get_inbound,
    get_relay,
)
from trackinizer.server.api._routes_shared import engine_of, require_browser
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.capture_relay import (
    MAX_ANSWER_BYTES,
    CaptureAnswer,
    CaptureImage,
)
from trackinizer.server.chat_hub import CaptureFrame
from trackinizer.server.visuals.workspace_store import (
    WorkspaceKeyRefusedError,
    agent_may_operate,
    owns_workspace,
)


router = APIRouter()


@router.post(
    "/api/workspaces/{workspace_id}/captures",
    response_model=CaptureImage,
    responses={424: {"description": "No open page of the canvas gave a picture."}},
)
async def request_capture_route(
    workspace_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> CaptureImage:
    """Ask the owner's open page for a picture of the canvas, and wait for it.

    Only the canvas's Chat partner, as for every other operation on it. The page
    that answers first wins; the picture is passed on and never stored. When no
    page is open, none answers within the wait, or the page has no canvas to
    draw, the answer is 424 with the reason.

    Args:
      workspace_id: Canvas to look at.
      request: Request carrying the database engine, the hub and the relay.
      identity: The assistant's agent key.

    Returns:
      image: The canvas as the person sees it, with the boxes on it.

    """
    if identity.api_key_id is None:
        raise HTTPException(status_code=403, detail="Agent key required")
    try:
        allowed = await agent_may_operate(
            engine_of(request),
            user_id=identity.user_id,
            workspace_id=workspace_id,
            agent_api_key_id=identity.api_key_id,
            inbound=get_inbound(request),
            assistant=get_assistant(request),
        )
    except WorkspaceKeyRefusedError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    if not allowed:
        raise HTTPException(status_code=404, detail="Workspace not found")
    hub = get_hub(request)
    if not hub.listening(workspace_id):
        raise HTTPException(status_code=424, detail="No open page answered.")
    try:
        answer = await get_relay(request).ask(
            workspace_id,
            publish=lambda asked: hub.publish(
                workspace_id,
                frame=CaptureFrame(request=asked),
            ),
        )
    except TimeoutError as error:
        raise HTTPException(status_code=424, detail="No open page answered.") from error
    if answer.image is None:
        raise HTTPException(status_code=424, detail=answer.error)
    return answer.image


# The body is read here, in chunks, so one over the cap is refused before it is all
# held and a 422 never echoes the picture; FastAPI does neither for a declared body.
# The schema is stated by hand for the web app's typed client, and its test compares
# it with ``CaptureAnswer``.
_ANSWER_BODY: Final = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "image": {"$ref": "#/components/schemas/CaptureImage"},
                    "error": {"type": "string"},
                },
            },
        },
    },
}


@router.put(
    "/api/workspaces/{workspace_id}/captures/{capture_id}",
    status_code=204,
    openapi_extra={"requestBody": _ANSWER_BODY},
)
async def answer_capture_route(
    workspace_id: uuid.UUID,
    capture_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("viewer"))],
) -> Response:
    """Answer an agent's request for a picture, from the owner's open page.

    The body is a ``CaptureAnswer`` of at most 2.2 MB. The first answer to a
    request is taken; a later one, or one for a request that is over, is 404.

    Args:
      workspace_id: The canvas the page shows.
      capture_id: The request id the page was sent.
      request: Request carrying the body.
      identity: Authenticated browser account, which must own the canvas.

    Returns:
      response: Empty.

    """
    require_browser(identity)
    if not await owns_workspace(
        engine_of(request),
        user_id=identity.user_id,
        workspace_id=workspace_id,
    ):
        raise HTTPException(status_code=404, detail="Workspace not found")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_ANSWER_BYTES:
            raise HTTPException(status_code=413, detail="Capture answer too large")
    try:
        answer = CaptureAnswer.model_validate_json(bytes(body))
    except ValidationError as error:
        # The input is left out of the detail: it is the picture.
        raise HTTPException(
            status_code=422,
            detail=[each["msg"] for each in error.errors(include_input=False)],
        ) from error
    if not get_relay(request).answer(
        workspace_id,
        request=capture_id,
        answer=answer,
    ):
        raise HTTPException(status_code=404, detail="No such capture request")
    return Response(status_code=204)
