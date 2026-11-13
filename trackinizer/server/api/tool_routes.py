"""Routes for tool connections: the agent's socket, the daemon's wait, and the daemon's socket.

A writer's or admin's key opens a tool connection to a host of its own account, such as
the key minted for an agent process; the configured assistant opens one for an
attested writer or admin ``asker`` who owns the host; and a writer's or admin's own
browser, signed in from this server's pages, opens one to a host of their own, as a file
explorer does. Either way the host reports ``tools: true`` in its facts: the rule
of ``tool_access``, which each sweep asks again. Its socket asks the relay
(``server/tool_relay.py``) to queue an offer. The daemon holds a ``GET`` on the wait
route, which returns the moment an offer is queued, and dials the daemon socket. The
relay relays between the two. A browser from another page or for another account, a key
naming another account, and a server with authentication off open nothing here.

Every line the agent sends is read by a ``ToolLines`` before it is relayed, and each
``Call`` is logged with the gate's verdict, its arguments by their size alone. What the
tool did is in the session's record, which a call's session and id join. A tool connection opening,
a refusal, a line that ends a tool connection and a dropped line each log a line too, and the relay
logs each tool connection's end.

A WebSocket cannot answer 404 or 410 after the handshake, so both sockets accept, and then
refuse with a close code and a reason saying what happened (``wire_tools``).

``/exec/wait`` stays one release, for a daemon from before tool connections: it answers ``null``,
so that daemon gets no error and no offer. A daemon that reports no ``tools`` fact is refused
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
    get_tool_line_bytes,
    get_tool_relay,
)
from trackinizer.server.api._origin import is_strictly_same_origin
from trackinizer.server.api._routes_shared import (
    refusal,
    request_engine,
    require_instance,
)
from trackinizer.server.api.terminal_routes import StarletteSocket
from trackinizer.server.api.tool_access import tool_access
from trackinizer.server.host_auth import (
    current_host,
    host_revoked,
    request_host,
)
from trackinizer.server.tool_lines import GatedCall, ToolLines, authorize
from trackinizer.server.tool_relay import (
    Inbound,
    ToolConnection,
    ToolConnectionBusyError,
    ToolConnectionEndError,
    ToolConnectionRevokedError,
    ToolRelay,
    close_reason,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_exec, wire_terminals, wire_tools
from trackinizer.wire.wire_exec import HOST_EXEC_WAIT_PATH
from trackinizer.wire.wire_service_accounts import is_service_address
from trackinizer.wire.wire_tools import (
    HOST_TOOLS_WAIT_PATH,
    HOST_TOOLS_WS_PATH,
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
    "tools_daemon_socket",
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
    """Relay an agent's tool calls to the asker's host, until either socket drops.

    A writer's or admin's key opens this for a host of its own account; the
    configured assistant's names an attested writer or admin ``asker`` who owns the
    host. A person's own browser opens it for a host of their own: signed in, from
    this server's pages, as no service account, naming no other ``asker``. The host
    must not be revoked, be confirmed by its owner, and report ``tools: true``; whether
    it is up is for its daemon's dial-back to say. A host that does not is refused at
    once with a reason saying why.

    Each ``Call`` the agent sends is logged as the gate judges it, before it is
    forwarded; a line that cannot be read ends the tool connection.

    Args:
      websocket: The agent's socket; it carries the key as a bearer, or a browser's
        session cookie and ``Origin``.
      name: The host's name.
      asker: The person the assistant acts for; any other key is its own, and a
        browser names its own or none.

    """
    await websocket.accept()
    socket = StarletteSocket(websocket)
    try:
        identity = await auth.request_user(websocket)
    except HTTPException as error:
        await _refuse(
            socket,
            _close_code(error),
            _close_reason(error),
            socket_name="agent",
            host=name,
        )
        return
    if identity.api_key_id is None and not _own_browser(
        websocket,
        email=identity.email,
        asker=asker,
    ):
        await _refuse(
            socket,
            wire_terminals.CLOSE_REFUSED,
            _FORBIDDEN,
            socket_name="agent",
            host=name,
        )
        return
    assistant = get_assistant(websocket)
    # A key that names nobody acts for its own account, as a browser does.
    owner = (identity.email if asker is None else asker).lower()
    async with request_engine(websocket).acquire() as conn:
        access = await tool_access(
            conn,
            assistant=assistant,
            api_key_id="browser"
            if identity.api_key_id is None
            else identity.api_key_id,
            asker=owner,
            host=name,
        )
    if access.failed is not None:
        await _refuse(
            socket,
            wire_terminals.CLOSE_REFUSED,
            close_reason(_REFUSED.get(access.failed, _FORBIDDEN).format(name=name)),
            socket_name="agent",
            host=name,
            check=access.failed,
        )
        return
    # The rule holds only of a key or a browser, an asker, and a host of theirs.
    assert isinstance(access.host_id, UUID)
    host_id, api_key_id = access.host_id, identity.api_key_id
    relay = get_tool_relay(websocket)
    try:
        connection = relay.join(
            host_id,
            agent=socket,
            asker=owner,
            api_key_id=api_key_id,
            inbound=_gated(
                asker=owner,
                host=name,
                line_bytes=get_tool_line_bytes(websocket),
                relay=relay,
            ),
        )
    except ToolConnectionBusyError as busy:
        await _refuse(
            socket,
            wire_terminals.CLOSE_REFUSED,
            f"{name} holds its {busy.limit} tool connections",
            socket_name="agent",
            host=name,
        )
        return
    _logger.info(
        "tool connection opened: connection=%s host=%s host_id=%s asker=%s key=%s assistant=%s",
        connection.id,
        name,
        host_id,
        owner,
        "browser" if api_key_id is None else api_key_id,
        "-" if assistant is None else assistant.actor,
    )
    await relay.serve_agent(connection, partial(_read_bytes, socket))


# Unlike the terminal wait, it keeps no receipt: a response lost on its way loses its
# offer, the agent's tool connection waits out its dial-back, and the agent dials again.
@router.get(
    HOST_TOOLS_WAIT_PATH,
    response_model_exclude_none=True,
    include_in_schema=False,
)
async def tools_wait_route(
    host_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_tools.WAIT_MAX_SEC,
) -> ToolOffer | None:
    """Hold until an agent asks for this host, then return its offer; else ``null``.

    The host's own credential authenticates, as for a heartbeat. The wait is held
    at most 25 seconds.

    Args:
      host_id: The host.
      request: Request carrying the database engine and the tool connection relay.
      instance: The daemon instance, which must be the host's current one when the
        wait starts; its socket is refused if it is not by then.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      offer: The tool connection to dial back for, or ``None`` when none came in time.

    Raises:
      HTTPException: 403 on a server with authentication off, or when another daemon
        took the host over; 410 when the host is revoked, including while its
        daemon waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_host(request, host_id)
    await require_instance(request, host_id, instance=instance)
    try:
        return await get_tool_relay(request).wait(
            host_id,
            wait_sec=min(wait_sec, wire_tools.WAIT_MAX_SEC),
        )
    except ToolConnectionRevokedError as error:
        raise host_revoked() from error


@router.websocket(HOST_TOOLS_WS_PATH)
async def tools_daemon_socket(
    websocket: WebSocket,
    host_id: UUID,
    connection_id: UUID,
    instance: UUID,
) -> None:
    """Relay a tool connection's other end: the daemon dials here with its host credential.

    Args:
      websocket: The daemon's socket; it carries the credential as a bearer.
      host_id: The host.
      connection_id: The tool connection the daemon was offered.
      instance: The daemon's instance, which must be the host's current one.

    """
    await websocket.accept()
    socket = StarletteSocket(websocket)
    refuse = partial(_refuse, socket, socket_name="daemon", host=str(host_id))
    if auth_is_disabled(websocket):
        await refuse(wire_terminals.CLOSE_REFUSED, "tools are not available here")
        return
    try:
        _ = await request_host(websocket, host_id)
        await require_instance(websocket, host_id, instance=instance)
    except (HTTPException, NotFoundError) as error:
        await refuse(_close_code(error), _close_reason(error))
        return
    relay = get_tool_relay(websocket)
    connection = relay.attach(host_id, connection_id, socket, instance=instance)
    if connection is None:
        await refuse(
            wire_terminals.CLOSE_REFUSED,
            "no such tool connection waits for this host",
        )
        return
    await relay.serve_daemon(connection, partial(_read_bytes, socket))


# Held and not answered at once: this daemon asks again the moment it hears back. It stops
# on the 410, as it always has.
@router.get(HOST_EXEC_WAIT_PATH, include_in_schema=False)
async def exec_wait_route(
    host_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_exec.WAIT_MAX_SEC,
) -> None:
    """Hold a daemon from before tool connections, and answer ``null``: it is offered nothing.

    Args:
      host_id: The host.
      request: Request carrying the database engine and the tool connection relay.
      instance: The daemon instance, which must be the host's current one.
      wait_sec: How long to hold, clamped to 25 seconds.

    Raises:
      HTTPException: 403 on a server with authentication off, or from a stale daemon;
        410 when the host is revoked, including while its daemon waits.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_host(request, host_id)
    await require_instance(request, host_id, instance=instance)
    try:
        await get_tool_relay(request).hold(
            host_id,
            wait_sec=min(wait_sec, wire_exec.WAIT_MAX_SEC),
        )
    except ToolConnectionRevokedError as error:
        raise host_revoked() from error


# The assistant is trusted to say whom it acts for, as for a chat's settings, and the
# server reads that person's role itself. Any other key, and a browser, acts for its own
# account alone: a tool runs as the host's user, which only that user's key or browser, or
# the assistant acting for them, may ask for, as only they may open a terminal there. A
# refusal of who is asking says no more than the forbidden one; one of the host says
# what to do about it.
_FORBIDDEN: Final = "tools are not available here"
_REFUSED: Final[Mapping[Check, str]] = {
    "host": "no host {name} of yours",
    "credential": "host {name} is revoked; connect it again",
    "confirmed": "host {name} is not confirmed; confirm it first",
    "tools": "{name} serves no tools: update its host, or drop --no-exec",
}


# Never a line's text: an argument may hold a secret, and a log is read more widely
# than the session. A field the agent chose is quoted, so no line it sends can forge one.
# A call let through runs on the host until its result or the agent's ack of it; a
# denied one never reaches it.
def _gated(
    *,
    asker: str,
    host: str,
    line_bytes: int,
    relay: ToolRelay,
) -> Inbound:
    """Return the check of one tool connection's agent socket: read, log each call, then let through."""
    lines = ToolLines(
        asker=asker,
        host=host,
        authorize=authorize,
        line_bytes=line_bytes,
    )

    async def inbound(connection: ToolConnection, frame: bytes) -> tuple[bytes, bytes]:
        try:
            routed, calls = lines.feed(frame)
        except ValueError as error:
            _logger.warning(
                "tool connection line refused: connection=%s failure=%s bytes=%d",
                connection.id,
                type(error).__name__,
                len(frame),
            )
            raise ToolConnectionEndError(
                f"the server will not relay a line: {error}",
            ) from error
        for size in routed.dropped:
            _logger.warning(
                "tool connection line dropped: connection=%s failure=kind_not_offered bytes=%d",
                connection.id,
                size,
            )
        for call in calls:
            _log_call(connection, call)
        for call in calls:
            if call.outcome == "allow":
                relay.called(connection, call.call_id)
        for call_id in routed.acks:
            relay.acked(connection, call_id)
        return routed.to_daemon, routed.to_agent

    return inbound


def _log_call(connection: ToolConnection, call: GatedCall) -> None:
    """Log one call the gate judged, with no word of what its arguments say."""
    _logger.info(
        "tool call: connection=%s asker=%s agent=%r session=%r host=%s tool=%r "
        "call_id=%r argument_bytes=%d verdict=%s",
        connection.id,
        call.asker,
        call.agent,
        call.session,
        call.host,
        call.tool,
        call.call_id,
        call.argument_bytes,
        call.outcome,
    )


async def _refuse(
    socket: StarletteSocket,
    code: int,
    reason: str,
    *,
    socket_name: str,
    host: str,
    check: Check | None = None,
) -> None:
    """Refuse a socket at its opening, saying so in the log, so no refusal is silent."""
    _logger.warning(
        "tool connection refused: socket=%s host=%s code=%d reason=%r check=%s",
        socket_name,
        host,
        code,
        reason,
        check or "-",
    )
    await socket.close(code, reason)


async def _read_bytes(socket: StarletteSocket) -> bytes | None:
    """Read one frame; ``None`` when the peer left or sent anything but bytes."""
    message = await socket.receive()
    data = message.get("bytes")
    if message["type"] != "websocket.receive" or not isinstance(data, bytes):
        return None
    return data


# Nothing acts on which refusal it was, so one code says it, and the reason says which;
# each says no more than a refusal to the caller's face would. A revoked host keeps
# the code a terminal hears too.
def _close_code(error: HTTPException | NotFoundError) -> int:
    """Return the close code of a socket refused with ``error``."""
    if isinstance(error, HTTPException) and error.status_code == 410:
        return wire_terminals.CLOSE_HOST_REVOKED
    return wire_terminals.CLOSE_REFUSED


# It says no more than the refusal would to the caller's face over HTTP.
def _close_reason(error: HTTPException | NotFoundError) -> str:
    """Return what a refused socket is told: the refusal's own message, cut to fit."""
    if isinstance(error, NotFoundError):
        return close_reason(str(error))
    detail = error.detail
    if isinstance(detail, dict):
        detail = cast(dict[str, object], detail).get("message", "")
    return close_reason(str(detail))


def _forbidden() -> HTTPException:
    """Return the one refusal of a caller that may not reach tools here."""
    return refusal(403, code="tools_forbidden", message=_FORBIDDEN)


# A browser sends its session cookie with every page's socket, so only this server's own
# pages may open one, as for a terminal; with authentication off every caller is one admin.
# A key may name another asker, which the rule checks; a browser names its own or none.
# Whether its account may write is the rule's ``asker`` check, read fresh from the users.
def _own_browser(websocket: WebSocket, *, email: str, asker: str | None) -> bool:
    """Whether a caller with no key is a person's browser, from this server's pages."""
    return (
        not auth_is_disabled(websocket)
        and is_strictly_same_origin(websocket)
        and not is_service_address(email)
        and (asker is None or asker.lower() == email.lower())
    )
