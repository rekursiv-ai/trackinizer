"""Routes for tool pipes: the assistant's leg, the host's wait, and the host's leg.

Only the configured assistant opens a pipe, for an attested writer or admin ``asker``
who owns the machine, whose host reports ``tools: true`` in its facts: the rule of
``tool_access``, which each sweep asks again. Its leg asks the hub
(``server/tool_pipes.py``) to queue an offer. The machine's host holds a ``GET`` on the
wait route, which returns the moment an offer is queued, and dials the host leg. The hub
relays between the two. A person's own browser, an API key of anyone else, a service
account and a server with authentication off open nothing here.

Every line the agent sends is read by a ``ToolLines`` before it is relayed, and each
``Call`` is logged with the gate's verdict, its arguments by their size alone. What the
tool did is in the session's record, which a call's session and id join. A pipe opening,
a refusal, a line that ends a pipe and a dropped line each log a line too, and the hub
logs each pipe's end.

A WebSocket cannot answer 404 or 410 after the handshake, so both legs accept, and then
refuse with a close code and a reason saying what happened (``wire_tools``).

``/exec/wait`` stays one release, for a host from before tool pipes: it answers ``null``,
so that host gets no error and no offer. A host that reports no ``tools`` fact is refused
at once with a reason saying to update its host, rather than left to miss the dial-back.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Annotated, Final, cast
from uuid import UUID

import logging

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket

from trackinizer.server import auth
from trackinizer.server.api._deps import (
    auth_is_disabled,
    get_assistant,
    get_pipe_hub,
    get_tool_line_bytes,
)
from trackinizer.server.api._routes_shared import (
    refusal,
    request_engine,
    require_instance,
)
from trackinizer.server.api.terminal_routes import Leg
from trackinizer.server.api.tool_access import tool_access
from trackinizer.server.machine_auth import (
    current_machine,
    machine_revoked,
    request_machine,
)
from trackinizer.server.tool_lines import GatedCall, ToolLines, authorize
from trackinizer.server.tool_pipes import (
    Inbound,
    Pipe,
    PipeBusyError,
    PipeEndError,
    PipeRevokedError,
    close_reason,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_exec, wire_terminals, wire_tools
from trackinizer.wire.wire_exec import MACHINE_EXEC_WAIT_PATH
from trackinizer.wire.wire_tools import (
    MACHINE_TOOLS_WAIT_PATH,
    MACHINE_TOOLS_WS_PATH,
    TOOLS_WS_PATH,
    ToolOffer,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.server.api.tool_access import Check


__all__ = [
    "exec_wait_route",
    "router",
    "tools_agent_socket",
    "tools_host_socket",
    "tools_wait_route",
]

router = APIRouter()

_logger = logging.getLogger(__name__)


@router.websocket(TOOLS_WS_PATH)
async def tools_agent_socket(
    websocket: WebSocket,
    name: str,
    asker: str | None = None,
) -> None:
    """Relay an agent's tool calls to the asker's machine, until either leg drops.

    Only the configured assistant's key opens this, naming an attested writer or admin
    ``asker`` who owns the machine, which must not be revoked, be confirmed by its owner,
    and report ``tools: true``; whether it is up is for its host's dial-back to say. A
    machine that does not is refused at once with a reason saying why. It holds a key
    and no cookie, so it needs no same-origin check.

    Each ``Call`` the agent sends is logged as the gate judges it, before it is
    forwarded; a line that cannot be read ends the pipe.

    Args:
      websocket: The agent's socket; it carries the assistant's key as a bearer.
      name: The machine's name.
      asker: The person the assistant acts for.

    """
    await websocket.accept()
    leg = Leg(websocket)
    try:
        identity = await auth.request_user(websocket)
    except HTTPException as error:
        await _refuse(
            leg,
            _close_code(error),
            _close_reason(error),
            leg_name="agent",
            machine=name,
        )
        return
    async with request_engine(websocket).acquire() as conn:
        access = await tool_access(
            conn,
            assistant=get_assistant(websocket),
            api_key_id=identity.api_key_id,
            asker=asker,
            machine=name,
        )
    if access.failed is not None:
        await _refuse(
            leg,
            wire_terminals.CLOSE_REFUSED,
            close_reason(_REFUSED.get(access.failed, _FORBIDDEN).format(name=name)),
            leg_name="agent",
            machine=name,
            check=access.failed,
        )
        return
    # The rule holds only of a key, an asker, and a machine of theirs.
    assert isinstance(access.machine_id, UUID)
    assert isinstance(identity.api_key_id, UUID)
    assert isinstance(asker, str)
    machine_id, owner, api_key_id = (
        access.machine_id,
        asker.lower(),
        identity.api_key_id,
    )
    hub = get_pipe_hub(websocket)
    try:
        pipe = hub.join(
            machine_id,
            agent=leg,
            asker=owner,
            api_key_id=api_key_id,
            inbound=_gated(
                asker=owner,
                machine=name,
                line_bytes=get_tool_line_bytes(websocket),
            ),
        )
    except PipeBusyError as busy:
        await _refuse(
            leg,
            wire_terminals.CLOSE_REFUSED,
            f"{name} holds its {busy.limit} pipes",
            leg_name="agent",
            machine=name,
        )
        return
    assistant = get_assistant(websocket)
    _logger.info(
        "tool pipe opened: pipe=%s machine=%s machine_id=%s asker=%s key=%s "
        "assistant=%s",
        pipe.id,
        name,
        machine_id,
        owner,
        api_key_id,
        "-" if assistant is None else assistant.actor,
    )
    await hub.serve_agent(pipe, partial(_read_bytes, leg))


# Unlike the terminal wait, it keeps no receipt: a response lost on its way loses its
# offer, the agent's pipe waits out its dial-back, and the agent dials again.
@router.get(
    MACHINE_TOOLS_WAIT_PATH,
    response_model_exclude_none=True,
    include_in_schema=False,
)
async def tools_wait_route(
    machine_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_tools.WAIT_MAX_SEC,
) -> ToolOffer | None:
    """Hold until an agent asks for this machine, then return its offer; else ``null``.

    The machine's own credential authenticates, as for a heartbeat. The wait is held
    at most 25 seconds.

    Args:
      machine_id: The machine.
      request: Request carrying the database engine and the pipe hub.
      instance: The host instance, which must be the machine's current one when the
        wait starts; its leg is refused if it is not by then.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      offer: The pipe to dial back for, or ``None`` when none came in time.

    Raises:
      HTTPException: 403 on a server with authentication off, or when another host
        took the machine over; 410 when the machine is revoked, including while the
        host waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_machine(request, machine_id)
    await require_instance(request, machine_id, instance=instance)
    try:
        return await get_pipe_hub(request).wait(
            machine_id,
            wait_sec=min(wait_sec, wire_tools.WAIT_MAX_SEC),
        )
    except PipeRevokedError as error:
        raise machine_revoked() from error


@router.websocket(MACHINE_TOOLS_WS_PATH)
async def tools_host_socket(
    websocket: WebSocket,
    machine_id: UUID,
    pipe_id: UUID,
    instance: UUID,
) -> None:
    """Relay a pipe's other end: the host dials here with its machine credential.

    Args:
      websocket: The host's socket; it carries the credential as a bearer.
      machine_id: The machine.
      pipe_id: The pipe the host was offered.
      instance: The host's instance, which must be the machine's current one.

    """
    await websocket.accept()
    leg = Leg(websocket)
    refuse = partial(_refuse, leg, leg_name="host", machine=str(machine_id))
    if auth_is_disabled(websocket):
        await refuse(wire_terminals.CLOSE_REFUSED, "tools are not available here")
        return
    try:
        _ = await request_machine(websocket, machine_id)
        await require_instance(websocket, machine_id, instance=instance)
    except (HTTPException, NotFoundError) as error:
        await refuse(_close_code(error), _close_reason(error))
        return
    hub = get_pipe_hub(websocket)
    pipe = hub.attach(machine_id, pipe_id, leg, instance=instance)
    if pipe is None:
        await refuse(
            wire_terminals.CLOSE_REFUSED,
            "no such pipe waits for this machine",
        )
        return
    await hub.serve_host(pipe, partial(_read_bytes, leg))


# Held and not answered at once: this host asks again the moment it hears back. It stops
# on the 410, as it always has.
@router.get(MACHINE_EXEC_WAIT_PATH, include_in_schema=False)
async def exec_wait_route(
    machine_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_exec.WAIT_MAX_SEC,
) -> None:
    """Hold a host from before tool pipes, and answer ``null``: it is offered nothing.

    Args:
      machine_id: The machine.
      request: Request carrying the database engine and the pipe hub.
      instance: The host instance, which must be the machine's current one.
      wait_sec: How long to hold, clamped to 25 seconds.

    Raises:
      HTTPException: 403 on a server with authentication off, or from a stale host;
        410 when the machine is revoked, including while the host waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_machine(request, machine_id)
    await require_instance(request, machine_id, instance=instance)
    try:
        await get_pipe_hub(request).hold(
            machine_id,
            wait_sec=min(wait_sec, wire_exec.WAIT_MAX_SEC),
        )
    except PipeRevokedError as error:
        raise machine_revoked() from error


# The assistant is trusted to say whom it acts for, as for a chat's settings, and the
# server reads that person's role itself. Nobody else may name an asker, and there is no
# route for a person's own session: a tool runs as the machine's user, which only the
# assistant, acting for that user, may ask for. A refusal of who is asking says no more
# than the forbidden one; one of the machine says what to do about it.
_FORBIDDEN: Final = "tools are not available here"
_REFUSED: Final[Mapping[Check, str]] = {
    "machine": "no machine {name} of yours",
    "credential": "machine {name} is revoked; connect it again",
    "confirmed": "machine {name} is not confirmed; confirm it first",
    "tools": "{name} serves no tools: update its host, or drop --no-exec",
}


# Never a line's text: an argument may hold a secret, and a log is read more widely
# than the session. A field the agent chose is quoted, so no line it sends can forge one.
def _gated(*, asker: str, machine: str, line_bytes: int) -> Inbound:
    """Return the check of one pipe's agent leg: read, log each call, then let through."""
    lines = ToolLines(
        asker=asker,
        machine=machine,
        authorize=authorize,
        line_bytes=line_bytes,
    )

    async def inbound(pipe: Pipe, frame: bytes) -> tuple[bytes, bytes]:
        try:
            routed, calls = lines.feed(frame)
        except ValueError as error:
            _logger.warning(
                "tool pipe line refused: pipe=%s failure=%s bytes=%d",
                pipe.id,
                type(error).__name__,
                len(frame),
            )
            raise PipeEndError(f"the server will not relay a line: {error}") from error
        for size in routed.dropped:
            _logger.warning(
                "tool pipe line dropped: pipe=%s failure=kind_not_offered bytes=%d",
                pipe.id,
                size,
            )
        for call in calls:
            _log_call(pipe, call)
        return routed.to_host, routed.to_agent

    return inbound


def _log_call(pipe: Pipe, call: GatedCall) -> None:
    """Log one call the gate judged, with no word of what its arguments say."""
    _logger.info(
        "tool call: pipe=%s asker=%s agent=%r session=%r machine=%s tool=%r "
        "call_id=%r argument_bytes=%d verdict=%s",
        pipe.id,
        call.asker,
        call.agent,
        call.session,
        call.machine,
        call.tool,
        call.call_id,
        call.argument_bytes,
        call.outcome,
    )


async def _refuse(
    leg: Leg,
    code: int,
    reason: str,
    *,
    leg_name: str,
    machine: str,
    check: Check | None = None,
) -> None:
    """Refuse a leg at its opening, saying so in the log, so no refusal is silent."""
    _logger.warning(
        "tool pipe refused: leg=%s machine=%s code=%d reason=%r check=%s",
        leg_name,
        machine,
        code,
        reason,
        check or "-",
    )
    await leg.close(code, reason)


async def _read_bytes(leg: Leg) -> bytes | None:
    """Read one frame; ``None`` when the peer left or sent anything but bytes."""
    message = await leg.receive()
    data = message.get("bytes")
    if message["type"] != "websocket.receive" or not isinstance(data, bytes):
        return None
    return data


# Nothing acts on which refusal it was, so one code says it, and the reason says which;
# each says no more than a refusal to the caller's face would. A revoked machine keeps
# the code a terminal hears too.
def _close_code(error: HTTPException | NotFoundError) -> int:
    """Return the close code of a leg refused with ``error``."""
    if isinstance(error, HTTPException) and error.status_code == 410:
        return wire_terminals.CLOSE_MACHINE_REVOKED
    return wire_terminals.CLOSE_REFUSED


# It says no more than the refusal would to the caller's face over HTTP.
def _close_reason(error: HTTPException | NotFoundError) -> str:
    """Return what a refused leg is told: the refusal's own message, cut to fit."""
    if isinstance(error, NotFoundError):
        return close_reason(str(error))
    detail = error.detail
    if isinstance(detail, dict):
        detail = cast(dict[str, object], detail).get("message", "")
    return close_reason(str(detail))


def _forbidden() -> HTTPException:
    """Return the one refusal of a caller that may not reach tools here."""
    return refusal(403, code="tools_forbidden", message=_FORBIDDEN)
