"""Routes for the browser terminal: open one, attach to it, and the daemon's two sockets.

Only a host's owner opens a terminal on it, from a browser session. The configured
assistant may open only a detached campaign run for an attested writer or admin on
that person's host. Opening asks the relay (``server/terminals.py``) to queue the
terminal for the daemon, which holds a ``GET`` on the wait route and dials the
daemon WebSocket. The browser dials the browser WebSocket and sends ``ATTACH`` with the
one-use ticket the open returned. The relay relays; the owner's last run folder is kept.
A ``files`` terminal opens and relays the same way, on a host whose facts say
``files``; its daemon answers file requests on it instead of starting a shell.

A WebSocket cannot answer 404 or 410 after the handshake, so both sockets accept, and
then refuse with a close code (``wire_terminals``).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from treekle import ReadError, from_plain

from trackinizer.lib.posix.host import Frame, decode_frame
from trackinizer.server import auth
from trackinizer.server.api._deps import (
    auth_is_disabled,
    get_host_changes,
    get_inbound,
    get_replays,
    get_store,
    get_terminal_relay,
    hosts_changed,
)
from trackinizer.server.api._origin import is_strictly_same_origin
from trackinizer.server.api._routes_shared import (
    idempotency_key,
    refusal,
    request_engine,
    require_instance,
)
from trackinizer.server.api.host_runs_routes import live_sessions, named_runs
from trackinizer.server.api.hosts_routes import HostName, fetch_hosts
from trackinizer.server.api.session_access import (
    resolve_asker,
)
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.api.tool_access import serves_tools
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.host_auth import current_host, request_host
from trackinizer.server.terminals import (
    Opening,
    SocketClosedError,
    Terminal,
    TerminalLimitError,
    TerminalRelay,
    decode_attach,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_daemon import (
    MIN_DAEMON_VERSION,
    OFFLINE_AFTER_SEC,
    older_than,
)
from trackinizer.wire.wire_service_accounts import is_service_address
from trackinizer.wire.wire_sessions import (
    CHAT_RUN_LIVE,
)
from trackinizer.wire.wire_terminals import (
    HOST_TERMINAL_WAIT_PATH,
    HOST_TERMINAL_WS_PATH,
    TERMINAL_ATTACH_PATH,
    TERMINAL_CLOSE_PATH,
    TERMINAL_CONFIRM_PATH,
    TERMINAL_HOSTS_CHANGES_PATH,
    TERMINAL_HOSTS_PATH,
    TERMINAL_WS_PATH,
    TERMINALS_PATH,
    HostsVersion,
    LiveTerminal,
    OpenTerminal,
    TerminalHost,
    TerminalHosts,
    TerminalOpened,
    TerminalTicket,
    TerminalWait,
)


if TYPE_CHECKING:
    from starlette.requests import HTTPConnection
    from starlette.types import Message


__all__ = [
    "StarletteSocket",
    "attach_terminal_route",
    "close_terminal_route",
    "confirm_terminal_host_route",
    "hosts_version_route",
    "list_terminal_hosts_route",
    "open_terminal_route",
    "router",
    "terminal_daemon_socket",
    "terminal_wait_route",
    "terminal_web_socket",
]

router = APIRouter()


@router.post(TERMINALS_PATH, status_code=201)
async def open_terminal_route(
    body: OpenTerminal,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    asker: str | None = None,
) -> TerminalOpened:
    """Open a terminal on one of the caller's hosts.

    A browser session opens the owner's terminal. The configured assistant may open
    only a detached campaign run for an attested writer or admin named as ``asker``;
    every other key, service account or foreign origin is refused. The host must
    be online and its daemon must have asked for terminals within 45 seconds
    (the daemon does by default). Attach with the ticket within 30 seconds.

    Args:
      body: The host, the terminal's size, and the canvas it opens from.
      request: Request carrying the store and the terminal relay.
      identity: The signed-in owner.
      asker: The person the configured assistant starts a campaign for.

    Returns:
      opened: The terminal id and the one-use attach ticket.

    Raises:
      HTTPException: 403 for an untrusted key, a service account or a foreign origin;
        404 for no such host of the asker, or another user's canvas; 409 ``terminal_unconfirmed`` until the
        owner confirms a host a key enrolled, ``cli_not_installed`` when the host's
        facts do not list the CLI asked for, ``files_not_enabled`` for a ``files``
        terminal when they do not say ``files``, ``terminal_not_enabled`` when the daemon
        is not asking, ``chat_run_live`` when a run is already attached to the ``attach``
        session, whether opened here or started by hand; 429 ``terminal_limit`` past 10
        opens a minute. Every terminal route answers 403 on a server with authentication
        off.

    """
    owner = await _terminal_owner(request, identity, asker=asker, body=body)
    host_id, facts = await _owned_host(
        request,
        replace(identity, email=owner),
        name=body.host,
    )
    if body.command is not None and body.command not in _host_clis(facts):
        raise refusal(
            409,
            code="cli_not_installed",
            message=f"the host reports no {body.command} on its PATH",
        )
    if body.kind == "files" and not _serves_files(facts):
        raise refusal(
            409,
            code="files_not_enabled",
            message=f"the daemon on {body.host} is too old to serve files; update it",
        )
    workspace: UUID | None = None
    relay = get_terminal_relay(request)
    if not relay.ready(host_id):
        raise refusal(
            409,
            code="terminal_not_enabled",
            message="start this daemon first",
        )
    try:
        key = idempotency_key(request)
        terminal_id = await get_replays(request).terminal_opens.run(
            None if key is None else (identity.user_id, identity.api_key_id, key),
            fingerprint=f"{owner}:{body.model_dump_json()}",
            operation=partial(
                _open_terminal,
                request,
                opening=Opening(
                    user=owner,
                    host_id=host_id,
                    host=body.host,
                    rows=body.rows,
                    cols=body.cols,
                    canvas=body.canvas,
                    workspace=workspace,
                    requested_by=body.requested_by,
                    surface=body.surface,
                    command=body.command,
                    attach=body.attach,
                    run_name=body.run_name,
                    cwd=body.cwd,
                    kind=body.kind,
                ),
            ),
        )
    except TerminalLimitError as error:
        raise refusal(
            429,
            code="terminal_limit",
            message="too many terminals opened just now; wait a minute",
        ) from error
    term = relay.terminal(terminal_id)
    if term is None:
        raise HTTPException(410, "The terminal opened by this request has ended")
    return TerminalOpened(
        terminal_id=term.id,
        ticket=relay.mint_ticket(term),
        ice_servers=list(relay.ice_servers),
    )


@router.get(TERMINAL_HOSTS_PATH, response_model_exclude_none=True)
async def list_terminal_hosts_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    canvas: UUID | None = None,
    owner: str | None = None,
) -> TerminalHosts:
    """List the caller's own hosts, and their live terminals for a canvas.

    The configured assistant names the person it answers as ``owner`` and lists that
    person's hosts, so it offers a terminal only on one they have. It opens none:
    only the person's own browser does.

    Args:
      request: Request carrying the store and the terminal relay.
      identity: The signed-in owner, or the assistant's agent key.
      canvas: The canvas whose live terminals to list.
      owner: The person the assistant lists for, a writer or admin; only the
        assistant may name one.

    Returns:
      hosts: The owner's hosts, newest first, each with whether it can take a
        terminal now; and ``terminals``, the owner's live terminals opened from
        ``canvas``.

    Raises:
      HTTPException: 403 ``terminal_forbidden`` for a key, a service account, anyone
        but the assistant naming an ``owner``, and an owner who is not a writer.

    """
    email = await _hosts_owner(request, identity, owner=owner)
    relay = get_terminal_relay(request)
    principal = await request_principal(request, identity)
    async with request_engine(request).acquire() as conn:
        # Read as the owner: a personal host is listed to its owner alone.
        rows = await fetch_hosts(
            conn,
            name=None,
            viewer=replace(identity, email=email),
        )
        mine = sorted(
            (row for row in rows if from_plain(row["owner"], str | None) == email),
            key=lambda row: from_plain(row["created"], datetime),
            reverse=True,
        )
        runs = {
            name: await named_runs(
                conn,
                live_sessions(request, host=name, owner=email),
                owner=email,
                policy=get_store(request).access_policy,
                principal=principal,
            )
            for name in (from_plain(row["name"], str) for row in mine)
        }
    hosts = [
        TerminalHost.model_validate(
            {
                "name": row["name"],
                "status": row["status"],
                "terminal_ready": row["status"] == "online"
                and relay.ready(from_plain(row["id"], UUID)),
                "confirmed": row["confirmed_at"] is not None,
                "clis": _host_clis(row["facts"]),
                "cwd": row["last_run_cwd"],
                "runs": runs[from_plain(row["name"], str)],
                "daemon_outdated": older_than(
                    from_plain(row["daemon_version"], str),
                    oldest=MIN_DAEMON_VERSION,
                ),
                "files": _serves_files(row["facts"]),
                "tools": serves_tools(row["facts"]),
            },
        )
        for row in mine
    ]
    live: list[Terminal] = [] if canvas is None else relay.live_in(email, canvas=canvas)
    return TerminalHosts(
        hosts=hosts,
        terminals=[
            LiveTerminal(
                terminal_id=term.id,
                host=term.host,
                surface=term.surface,
            )
            for term in live
        ],
    )


# Declared before any route whose path could take ``changes`` as a parameter.
@router.get(TERMINAL_HOSTS_CHANGES_PATH, include_in_schema=False)
async def hosts_version_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    owner: str | None = None,
    since: int | None = None,
    wait_sec: Annotated[float, Query(ge=0)] = wire_terminals.WAIT_MAX_SEC,
) -> HostsVersion:
    """Hold until the listing's version is not ``since``, then return it.

    Whoever may read the listing may wait on it, ``owner`` as for the listing. A
    follower lists the hosts, waits here, and lists them again when it answers
    another version; the version moves with each write to the owner's hosts.

    Args:
      request: Request carrying the host-list versions.
      identity: The signed-in owner, or the assistant's agent key.
      owner: The person the assistant waits for; only the assistant may name one.
      since: The version the caller holds; left out, the answer comes at once.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      version: The listing's version now; ``since`` only when the hold ran out.

    Raises:
      HTTPException: 403 ``terminal_forbidden`` as for the listing.

    """
    email = await _hosts_owner(request, identity, owner=owner)
    return HostsVersion(
        version=await get_host_changes(request).wait(
            email,
            since=since,
            wait_sec=min(wait_sec, wire_terminals.WAIT_MAX_SEC),
        ),
    )


@router.post(TERMINAL_CONFIRM_PATH, status_code=204)
async def confirm_terminal_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> None:
    """Confirm a host the caller owns, so terminals may open on it.

    A host enrolled with an API key is unconfirmed: an agent could have enrolled a
    host the person does not control. Only the person, in a browser session, confirms.

    Args:
      name: The host's name.
      request: Request carrying the database engine.
      identity: The signed-in owner.

    Raises:
      NotFoundError: The caller owns no such host.

    """
    _require_human(request, identity)
    _require_origin(request)
    async with request_engine(request).acquire() as conn:
        confirmed = await conn.fetchval(
            "UPDATE hosts SET confirmed_at = COALESCE(confirmed_at, now()) "
            "WHERE name = $1 AND owner = $2 RETURNING id",
            name,
            identity.email,
        )
    if confirmed is None:
        raise NotFoundError(f"no host {name} of yours")
    hosts_changed(request, owner=identity.email)


@router.post(TERMINAL_ATTACH_PATH)
async def attach_terminal_route(
    terminal_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> TerminalTicket:
    """Mint a one-use ticket to attach again to the caller's live terminal.

    Args:
      terminal_id: The terminal.
      request: Request carrying the terminal relay.
      identity: The signed-in opener.

    Returns:
      ticket: Valid for 30 seconds, once.

    """
    term = _own_terminal(request, identity, terminal_id=terminal_id)
    relay = get_terminal_relay(request)
    return TerminalTicket(
        ticket=relay.mint_ticket(term),
        ice_servers=list(relay.ice_servers),
    )


@router.post(TERMINAL_CLOSE_PATH, status_code=204)
async def close_terminal_route(
    terminal_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> None:
    """Stop the caller's terminal; its shell is killed.

    Args:
      terminal_id: The terminal.
      request: Request carrying the terminal relay.
      identity: The signed-in opener.

    """
    term = _own_terminal(request, identity, terminal_id=terminal_id)
    await get_terminal_relay(request).stop(term)


# A daemon on an older wire refuses a key it does not know, even a null one; a wait for a
# login shell names none of the fields a run adds, so such a daemon still opens it.
@router.get(HOST_TERMINAL_WAIT_PATH, response_model_exclude_none=True)
async def terminal_wait_route(
    host_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_terminals.WAIT_MAX_SEC,
) -> TerminalWait | None:
    """Hold until the host has a terminal to open, then return it; else ``null``.

    The host's own credential authenticates, as for a heartbeat. The wait is held at
    most 25 seconds, and its arrival tells the server that the daemon serves terminals.

    Args:
      host_id: The host.
      request: Request carrying the database engine and the terminal relay.
      instance: The daemon instance, which must be the host's current one.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      terminal: The next terminal to open, or ``None`` when none came in time.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_host(request, host_id)
    await require_instance(request, host_id, instance=instance)
    relay = get_terminal_relay(request)
    key = idempotency_key(request)
    return await get_replays(request).terminal_waits.run(
        None if key is None else (host_id, instance, key),
        fingerprint=str(host_id),
        operation=partial(
            _next_terminal,
            relay,
            host_id=host_id,
            wait_sec=min(wait_sec, wire_terminals.WAIT_MAX_SEC),
        ),
    )


@router.websocket(TERMINAL_WS_PATH)
async def terminal_web_socket(websocket: WebSocket, terminal_id: UUID) -> None:
    """Relay a terminal to its opener's browser; the first frame must be ``ATTACH``.

    Args:
      websocket: The browser's socket; it carries the session cookie and ``Origin``.
      terminal_id: The terminal.

    """
    await websocket.accept()
    socket = StarletteSocket(websocket)
    if auth_is_disabled(websocket) or not is_strictly_same_origin(websocket):
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    relay = get_terminal_relay(websocket)
    try:
        identity = await auth.request_user(websocket)
    except HTTPException:
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    attach = _parse_attach(await _first_frame(socket))
    term = relay.terminal(terminal_id)
    if (
        attach is None
        or term is None
        or identity.api_key_id is not None
        or not relay.redeem(attach[0], user=identity.email, terminal_id=terminal_id)
    ):
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    await relay.attach_browser(term, socket, offset=attach[1])
    try:
        while (frame := await _read_frame(socket)) is not None:
            if term.browser is not socket:
                break
            if not await relay.from_browser(term, *frame):
                break
    finally:
        relay.detach_browser(term, socket)
        await socket.close(wire_terminals.CLOSE_REFUSED)


@router.websocket(HOST_TERMINAL_WS_PATH)
async def terminal_daemon_socket(
    websocket: WebSocket,
    host_id: UUID,
    terminal_id: UUID,
    instance: UUID,
    ready: bool = False,
) -> None:
    """Relay a terminal's shell: the daemon dials here with its host credential.

    Args:
      websocket: The daemon's socket; it carries the credential as a bearer.
      host_id: The host.
      terminal_id: The terminal the daemon was asked to open.
      instance: The daemon's instance, which must be the host's current one.
      ready: Require an application acknowledgement before claiming the terminal.

    """
    await websocket.accept()
    socket = StarletteSocket(websocket)
    if auth_is_disabled(websocket):
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    try:
        await request_host(websocket, host_id)
        await require_instance(websocket, host_id, instance=instance)
    except HTTPException as error:
        await socket.close(
            wire_terminals.CLOSE_HOST_REVOKED
            if error.status_code == 410
            else wire_terminals.CLOSE_REFUSED,
        )
        return
    relay = get_terminal_relay(websocket)
    # A timed-out HTTP upgrade must leave the terminal pending: that daemon never
    # received the upgrade and cannot have started its CLI.
    if ready and await _first_frame(socket) != (Frame.ACK, b""):
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    term = relay.terminal(terminal_id)
    if (
        term is None
        or term.host_id != host_id
        or not await relay.daemon_connected(term, socket, ready=ready)
    ):
        await socket.close(wire_terminals.CLOSE_REFUSED)
        return
    try:
        while (frame := await _read_frame(socket)) is not None:
            if term.daemon is not socket:
                break
            await relay.from_daemon(term, *frame)
    finally:
        await relay.daemon_lost(term, socket=socket)


class StarletteSocket:
    """A Starlette WebSocket, as the relay's :class:`Socket`."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary message."""
        try:
            await self._websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError) as error:
            raise SocketClosedError from error

    async def close(self, code: int, reason: str = "") -> None:
        """Close with ``code`` and ``reason``; a socket already closed is left alone."""
        # Uvicorn raises WebSocketDisconnect from a close on a peer that has dropped.
        try:
            await self._websocket.close(code, reason)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    async def receive(self) -> Message:
        """Receive one ASGI message."""
        return await self._websocket.receive()


async def _read_frame(socket: StarletteSocket) -> tuple[Frame, bytes] | None:
    """Read one frame; ``None`` when the peer left or sent anything but a frame."""
    try:
        message = await socket.receive()
    except (WebSocketDisconnect, RuntimeError):
        return None
    data = message.get("bytes")
    if message["type"] != "websocket.receive" or not isinstance(data, bytes):
        return None
    try:
        return decode_frame(data)
    except ValueError:
        return None


async def _first_frame(
    socket: StarletteSocket,
    *,
    timeout_sec: float = 10.0,
) -> tuple[Frame, bytes] | None:
    """Read the first frame, which must arrive within the timeout."""
    try:
        return await asyncio.wait_for(_read_frame(socket), timeout_sec)
    except TimeoutError:
        return None


def _parse_attach(frame: tuple[Frame, bytes] | None) -> tuple[str, int] | None:
    """Return the ticket and offset of an ``ATTACH`` frame, or ``None`` if it is not one."""
    if frame is None or frame[0] is not Frame.ATTACH:
        return None
    try:
        return decode_attach(frame[1])
    except ValueError:
        return None


# A browser opens any of its own terminal forms. The configured assistant is trusted
# to name its asker, but only for the detached campaign shape: never a login shell or
# a freely chosen command. The wire admits only claude/codex as ``command``; the other
# fields make the daemon start a detached agent run with a named attached session.
async def _terminal_owner(
    request: Request,
    identity: AuthIdentity,
    *,
    asker: str | None,
    body: OpenTerminal,
) -> str:
    """Return the owner for a browser terminal or an attested campaign run."""
    if asker is None:
        _require_human(request, identity)
        _require_origin(request)
        return identity.email
    if auth_is_disabled(request):
        raise _forbidden()
    found = await resolve_asker(request, identity, email=asker)
    if (
        found is None
        or found.role not in {"writer", "admin"}
        or body.command is None
        or body.attach is None
        or body.canvas is None
        or body.cwd is None
        or body.run_name is None
        or not body.run_name.startswith("campaign-")
    ):
        raise _forbidden()
    return found.email


# The assistant is trusted to say whom it answers, as for a chat's settings, and the
# server reads that person's role itself: a viewer opens no terminal, so the assistant
# lists none for one. Anyone else naming an owner is refused, not ignored, so a person
# never reads a listing they took for someone else's as their own.
async def _hosts_owner(
    request: Request,
    identity: AuthIdentity,
    *,
    owner: str | None,
) -> str:
    """Return whose hosts the caller lists: its own, or the assistant's asker's."""
    asker = (
        None
        if auth_is_disabled(request)
        else await resolve_asker(request, identity, email=owner)
    )
    if asker is None:
        if owner is not None:
            raise _forbidden()
        _require_human(request, identity)
        return identity.email
    if asker.role not in {"writer", "admin"}:
        raise _forbidden()
    return asker.email


# Only a person's session opens a shell. With authentication off every caller is a
# synthetic admin, and a rebound web page would pass the origin check.
def _require_human(request: HTTPConnection, identity: AuthIdentity) -> None:
    """Refuse an API key, a service account, and a server with authentication off."""
    if (
        auth_is_disabled(request)
        or identity.api_key_id is not None
        or is_service_address(identity.email)
    ):
        raise _forbidden()


def _require_origin(request: Request) -> None:
    """Refuse a request that does not carry this server's own ``Origin``."""
    if not is_strictly_same_origin(request):
        raise _forbidden()


def _forbidden() -> HTTPException:
    """Return the one refusal of a caller that may not reach terminals here."""
    return refusal(
        403,
        code=wire_terminals.TERMINAL_FORBIDDEN,
        message="terminals are not available here",
    )


def _own_terminal(
    request: Request,
    identity: AuthIdentity,
    *,
    terminal_id: UUID,
) -> Terminal:
    """Return the caller's live terminal, after the human and origin checks."""
    _require_human(request, identity)
    _require_origin(request)
    term = get_terminal_relay(request).terminal(terminal_id)
    if term is None or term.user != identity.email:
        raise NotFoundError("no such terminal")
    return term


async def _owned_host(
    request: Request,
    identity: AuthIdentity,
    *,
    name: str,
) -> tuple[UUID, object]:
    """Return the id and facts of the caller's online host with a live credential."""
    async with request_engine(request).acquire() as conn:
        host = await conn.fetchrow(
            "SELECT m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed "
            "FROM hosts m WHERE m.name = $1 AND m.owner = $2 "
            "AND m.daemon_instance IS NOT NULL "
            "AND m.last_heartbeat > now() - make_interval(secs => $3) "
            "AND EXISTS (SELECT 1 FROM host_credentials c "
            "WHERE c.host_id = m.id AND c.revoked_at IS NULL)",
            name,
            identity.email,
            OFFLINE_AFTER_SEC,
        )
    if host is None:
        raise NotFoundError(f"no terminal-capable host {name}")
    if not host["confirmed"]:
        raise refusal(
            409,
            code="terminal_unconfirmed",
            message="confirm this host first",
        )
    return from_plain(host["id"], UUID), host["facts"]


def _host_clis(facts: object) -> list[str]:
    """Return the CLIs a daemon's facts list; none when its daemon reported no list."""
    # Any fact may be a string, a number or a flag, so a daemon can send ``clis`` in
    # another shape; that daemon then offers no CLI, rather than failing the listing.
    try:
        return from_plain(
            from_plain(facts, dict[str, object]).get("clis"),
            list[str],
            default=list[str](),
        )
    except ReadError:
        return []


def _serves_files(facts: object) -> bool:
    """Return whether a daemon's facts say its daemon answers a ``files`` terminal."""
    try:
        return from_plain(facts, dict[str, object]).get("files") is True
    except ReadError:
        return False


# A session has one agent process at a time. A run the server opened is in the relay from the
# open; one started by hand is known by the lease its drain takes, and a lease that
# names no CLI is the assistant's, which is not a run.
def _is_attached_to_a_live_session(
    request: Request,
    *,
    session_id: UUID | None,
) -> bool:
    """Say whether a run is live on the session a new run would attach to."""
    if session_id is None:
        return False
    held = get_inbound(request).poller_run(session_id)
    return bool(get_terminal_relay(request).attached_to(session_id)) or (
        held is not None and held.cli is not None
    )


# The check and the replacement run here, behind the receipt, for the reason
# ``_open_terminal``'s check does: a retry of the same reattach finds the run its first
# try queued, and must get that back, not retire it.
# The live-session check runs here, behind the receipt, and not in the route: the run a
# first open queued is attached to the session, so a retry of that open checked in
# the route would be refused as a second run instead of getting its terminal back.
async def _open_terminal(request: Request, *, opening: Opening) -> UUID:
    """Queue one terminal unless a run is live on its session; return only its id."""
    if opening.command is not None:
        async with request_engine(request).acquire() as conn:
            await conn.execute(
                "UPDATE hosts SET last_run_cwd = $1 WHERE id = $2 AND owner = $3",
                opening.cwd,
                opening.host_id,
                opening.user,
            )
        hosts_changed(request, owner=opening.user)
        # Checking and reserving must share one event-loop turn: another open
        # can pass the check while this request is awaiting its database update.
        if _is_attached_to_a_live_session(request, session_id=opening.attach):
            raise refusal(
                409,
                code=CHAT_RUN_LIVE,
                message="another run is attached to this session; stop it first",
            )
    return get_terminal_relay(request).open(opening).id


async def _next_terminal(
    relay: TerminalRelay,
    *,
    host_id: UUID,
    wait_sec: float,
) -> TerminalWait | None:
    """Take one open and freeze the offer before its response can be lost."""
    term = await relay.wait(
        host_id,
        wait_sec=wait_sec,
    )
    if term is None:
        return None
    return TerminalWait(
        terminal_id=term.id,
        opener=term.user,
        rows=term.rows,
        cols=term.cols,
        canvas=term.canvas,
        workspace=term.workspace,
        requested_by=term.requested_by,
        command=term.command,
        attach=term.attach,
        run_name=term.run_name,
        cwd=term.cwd,
        kind="files" if term.kind == "files" else None,
        ice_servers=list(relay.ice_servers),
    )
