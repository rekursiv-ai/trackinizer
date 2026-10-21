"""Routes for the browser terminal: open one, attach to it, and the host's two legs.

Only a machine's owner opens a terminal on it, from a browser session. The configured
assistant may open only a detached campaign run for an attested writer or admin on
that person's machine. Opening asks the hub (``server/terminals.py``) to queue the
terminal for the machine's host, which holds a ``GET`` on the wait route and dials the
host WebSocket. The browser dials the browser WebSocket and sends ``ATTACH`` with the
one-use ticket the open returned. The hub relays; the owner's last run folder is kept.

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
    get_replays,
    get_terminal_hub,
)
from trackinizer.server.api._origin import is_strictly_same_origin
from trackinizer.server.api._routes_shared import engine_of, idempotency_key
from trackinizer.server.api.machines_routes import MachineName, fetch_machines
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.machine_auth import current_machine, machine_of
from trackinizer.server.terminals import (
    Opening,
    SocketClosedError,
    Terminal,
    TerminalHub,
    TerminalLimitError,
    decode_attach,
)
from trackinizer.types.errors import NotFoundError
from trackinizer.wire import wire_terminals
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_service_accounts import is_service_address
from trackinizer.wire.wire_terminals import (
    MACHINE_TERMINAL_WAIT_PATH,
    MACHINE_TERMINAL_WS_PATH,
    TERMINAL_ATTACH_PATH,
    TERMINAL_CLOSE_PATH,
    TERMINAL_CONFIRM_PATH,
    TERMINAL_MACHINES_PATH,
    TERMINAL_WS_PATH,
    TERMINALS_PATH,
    LiveTerminal,
    OpenTerminal,
    TerminalMachine,
    TerminalMachines,
    TerminalOpened,
    TerminalTicket,
    TerminalWait,
)


if TYPE_CHECKING:
    from starlette.requests import HTTPConnection
    from starlette.types import Message


__all__ = [
    "attach_terminal_route",
    "close_terminal_route",
    "confirm_terminal_machine_route",
    "list_terminal_machines_route",
    "open_terminal_route",
    "router",
    "terminal_host_socket",
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
    """Open a terminal on one of the caller's machines.

    A browser session opens the owner's terminal. The configured assistant may open
    only a detached campaign run for an attested writer or admin named as ``asker``;
    every other key, service account or foreign origin is refused. The machine must
    be online and its host must have asked for terminals within 45 seconds
    (``TRAX_MACHINE_TERMINAL=1``). Attach with the ticket within 30 seconds.

    Args:
      body: The machine, the terminal's size, and the station it opens from.
      request: Request carrying the store and the terminal hub.
      identity: The signed-in owner.
      asker: The person the configured assistant starts a campaign for.

    Returns:
      opened: The terminal id and the one-use attach ticket.

    Raises:
      HTTPException: 403 for an untrusted key, a service account or a foreign origin;
        404 for no such machine of the asker, or station; 409 ``terminal_unconfirmed`` until the
        owner confirms a machine a key enrolled, ``cli_not_installed`` when the machine's
        facts do not list the CLI asked for, ``terminal_not_enabled`` when the host is
        not asking; 429 ``terminal_limit`` past 10 opens a minute. Every terminal route
        answers 403 on a server with authentication off.

    """
    owner = await _terminal_owner(request, identity, asker=asker, body=body)
    machine_id, clis = await _owned_machine(
        request,
        replace(identity, email=owner),
        name=body.machine,
    )
    if body.command is not None and body.command not in clis:
        raise _refused(
            409,
            code="cli_not_installed",
            message=f"the host reports no {body.command} on its PATH",
        )
    workstream = await _station_workstream(
        request,
        owner=owner,
        workspace=body.workspace,
    )
    hub = get_terminal_hub(request)
    if not hub.ready(machine_id):
        raise _refused(
            409,
            code="terminal_not_enabled",
            message="start the host with TRAX_MACHINE_TERMINAL=1",
        )
    if body.command is not None:
        async with engine_of(request).acquire() as conn:
            await conn.execute(
                "UPDATE machines SET last_run_cwd = $1 WHERE id = $2 AND owner = $3",
                body.cwd,
                machine_id,
                owner,
            )
    try:
        key = idempotency_key(request)
        terminal_id = await get_replays(request).terminal_opens.run(
            None if key is None else (identity.user_id, identity.api_key_id, key),
            fingerprint=f"{owner}:{body.model_dump_json()}",
            operation=partial(
                _open_terminal,
                hub,
                opening=Opening(
                    user=owner,
                    machine_id=machine_id,
                    machine=body.machine,
                    rows=body.rows,
                    cols=body.cols,
                    workspace=body.workspace,
                    workstream=workstream,
                    requested_by=body.requested_by,
                    surface=body.surface,
                    command=body.command,
                    attach=body.attach,
                    run_name=body.run_name,
                    cwd=body.cwd,
                ),
            ),
        )
    except TerminalLimitError as error:
        raise _refused(
            429,
            code="terminal_limit",
            message="too many terminals opened just now; wait a minute",
        ) from error
    term = hub.terminal(terminal_id)
    if term is None:
        raise HTTPException(410, "The terminal opened by this request has ended")
    return TerminalOpened(
        terminal_id=term.id,
        ticket=hub.mint_ticket(term),
        ice_servers=list(hub.ice_servers),
    )


@router.get(TERMINAL_MACHINES_PATH, response_model_exclude_none=True)
async def list_terminal_machines_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    workspace: UUID | None = None,
    owner: str | None = None,
) -> TerminalMachines:
    """List the caller's own machines, and their live terminals for a station.

    The configured assistant names the person it answers as ``owner`` and lists that
    person's machines, so it offers a terminal only on one they have. It opens none:
    only the person's own browser does.

    Args:
      request: Request carrying the store and the terminal hub.
      identity: The signed-in owner, or the assistant's agent key.
      workspace: The station whose live terminals to list.
      owner: The person the assistant lists for, a writer or admin; only the
        assistant may name one.

    Returns:
      machines: The owner's machines, newest first, each with whether it can take a
        terminal now; and ``terminals``, the owner's live terminals opened from
        ``workspace``.

    Raises:
      HTTPException: 403 ``terminal_forbidden`` for a key, a service account, anyone
        but the assistant naming an ``owner``, and an owner who is not a writer.

    """
    email = await _machines_owner(request, identity, owner=owner)
    hub = get_terminal_hub(request)
    async with engine_of(request).acquire() as conn:
        # Read as the owner: a personal machine is listed to its owner alone.
        rows = await fetch_machines(
            conn,
            name=None,
            viewer=replace(identity, email=email),
        )
    machines = [
        TerminalMachine.model_validate(
            {
                "name": row["name"],
                "status": row["status"],
                "terminal_ready": row["status"] == "online"
                and hub.ready(from_plain(row["id"], UUID)),
                "confirmed": row["confirmed_at"] is not None,
                "clis": _clis_of(row["facts"]),
                "cwd": row["last_run_cwd"],
            },
        )
        for row in sorted(
            (row for row in rows if from_plain(row["owner"], str | None) == email),
            key=lambda row: from_plain(row["created"], datetime),
            reverse=True,
        )
    ]
    live: list[Terminal] = (
        [] if workspace is None else hub.live_in(email, workspace=workspace)
    )
    return TerminalMachines(
        machines=machines,
        terminals=[
            LiveTerminal(
                terminal_id=term.id,
                machine=term.machine,
                surface=term.surface,
            )
            for term in live
        ],
    )


@router.post(TERMINAL_CONFIRM_PATH, status_code=204)
async def confirm_terminal_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> None:
    """Confirm a machine the caller owns, so terminals may open on it.

    A machine enrolled with an API key is unconfirmed: an agent could have enrolled a
    host the person does not control. Only the person, in a browser session, confirms.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      identity: The signed-in owner.

    Raises:
      NotFoundError: The caller owns no such machine.

    """
    _require_human(request, identity)
    _require_origin(request)
    async with engine_of(request).acquire() as conn:
        confirmed = await conn.fetchval(
            "UPDATE machines SET confirmed_at = COALESCE(confirmed_at, now()) "
            "WHERE name = $1 AND owner = $2 RETURNING id",
            name,
            identity.email,
        )
    if confirmed is None:
        raise NotFoundError(f"no machine {name} of yours")


@router.post(TERMINAL_ATTACH_PATH)
async def attach_terminal_route(
    terminal_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> TerminalTicket:
    """Mint a one-use ticket to attach again to the caller's live terminal.

    Args:
      terminal_id: The terminal.
      request: Request carrying the terminal hub.
      identity: The signed-in opener.

    Returns:
      ticket: Valid for 30 seconds, once.

    """
    term = _own_terminal(request, identity, terminal_id=terminal_id)
    hub = get_terminal_hub(request)
    return TerminalTicket(
        ticket=hub.mint_ticket(term),
        ice_servers=list(hub.ice_servers),
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
      request: Request carrying the terminal hub.
      identity: The signed-in opener.

    """
    term = _own_terminal(request, identity, terminal_id=terminal_id)
    await get_terminal_hub(request).stop(term)


# A host on an older wire refuses a key it does not know, even a null one; a wait for a
# login shell names none of the fields a run adds, so such a host still opens it.
@router.get(MACHINE_TERMINAL_WAIT_PATH, response_model_exclude_none=True)
async def terminal_wait_route(
    machine_id: UUID,
    request: Request,
    instance: UUID,
    wait_sec: Annotated[float, Query(ge=0)] = wire_terminals.WAIT_MAX_SEC,
) -> TerminalWait | None:
    """Hold until the machine has a terminal to open, then return it; else ``null``.

    The machine's own credential authenticates, as for a heartbeat. The wait is held at
    most 25 seconds, and its arrival tells the server that the host serves terminals.

    Args:
      machine_id: The machine.
      request: Request carrying the database engine and the terminal hub.
      instance: The host instance, which must be the machine's current one.
      wait_sec: How long to hold, clamped to 25 seconds.

    Returns:
      terminal: The next terminal to open, or ``None`` when none came in time.

    """
    if auth_is_disabled(request):
        raise _forbidden()
    await current_machine(request, machine_id)
    await _require_instance(request, machine_id, instance=instance)
    hub = get_terminal_hub(request)
    key = idempotency_key(request)
    return await get_replays(request).terminal_waits.run(
        None if key is None else (machine_id, instance, key),
        fingerprint=str(machine_id),
        operation=partial(
            _next_terminal,
            hub,
            machine_id=machine_id,
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
    leg = _Leg(websocket)
    if auth_is_disabled(websocket) or not is_strictly_same_origin(websocket):
        await leg.close(wire_terminals.CLOSE_REFUSED)
        return
    hub = get_terminal_hub(websocket)
    try:
        identity = await auth.user_of(websocket)
    except HTTPException:
        await leg.close(wire_terminals.CLOSE_REFUSED)
        return
    attach = _parse_attach(await _first_frame(leg))
    term = hub.terminal(terminal_id)
    if (
        attach is None
        or term is None
        or identity.api_key_id is not None
        or not hub.redeem(attach[0], user=identity.email, terminal_id=terminal_id)
    ):
        await leg.close(wire_terminals.CLOSE_REFUSED)
        return
    await hub.attach_browser(term, leg, offset=attach[1])
    try:
        while (frame := await _read_frame(leg)) is not None:
            if not await hub.from_browser(term, *frame):
                break
    finally:
        hub.detach_browser(term, leg)
        await leg.close(wire_terminals.CLOSE_REFUSED)


@router.websocket(MACHINE_TERMINAL_WS_PATH)
async def terminal_host_socket(
    websocket: WebSocket,
    machine_id: UUID,
    terminal_id: UUID,
    instance: UUID,
) -> None:
    """Relay a terminal's shell: the host dials here with its machine credential.

    Args:
      websocket: The host's socket; it carries the credential as a bearer.
      machine_id: The machine.
      terminal_id: The terminal the host was asked to open.
      instance: The host's instance, which must be the machine's current one.

    """
    await websocket.accept()
    leg = _Leg(websocket)
    if auth_is_disabled(websocket):
        await leg.close(wire_terminals.CLOSE_REFUSED)
        return
    try:
        await machine_of(websocket, machine_id)
        await _require_instance(websocket, machine_id, instance=instance)
    except HTTPException as error:
        await leg.close(
            wire_terminals.CLOSE_MACHINE_REVOKED
            if error.status_code == 410
            else wire_terminals.CLOSE_REFUSED,
        )
        return
    hub = get_terminal_hub(websocket)
    term = hub.terminal(terminal_id)
    if (
        term is None
        or term.machine_id != machine_id
        or not await hub.host_connected(term, leg)
    ):
        await leg.close(wire_terminals.CLOSE_REFUSED)
        return
    try:
        while (frame := await _read_frame(leg)) is not None:
            await hub.from_host(term, *frame)
    finally:
        await hub.host_lost(term, socket=leg)


class _Leg:
    """A Starlette WebSocket, as the hub's :class:`Socket`."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def send_bytes(self, data: bytes) -> None:
        """Send one binary message."""
        try:
            await self._websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, OSError) as error:
            raise SocketClosedError from error

    async def close(self, code: int) -> None:
        """Close with ``code``; a socket already closed is left alone."""
        # Uvicorn raises WebSocketDisconnect from a close on a peer that has dropped.
        try:
            await self._websocket.close(code)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    async def receive(self) -> Message:
        """Receive one ASGI message."""
        return await self._websocket.receive()


async def _read_frame(leg: _Leg) -> tuple[Frame, bytes] | None:
    """Read one frame; ``None`` when the peer left or sent anything but a frame."""
    try:
        message = await leg.receive()
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
    leg: _Leg,
    *,
    timeout_sec: float = 10.0,
) -> tuple[Frame, bytes] | None:
    """Read the first frame, which must arrive within the timeout."""
    try:
        return await asyncio.wait_for(_read_frame(leg), timeout_sec)
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
# fields make the host use ``trax run --detach`` with a named attached session.
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
        or body.workspace is None
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
async def _machines_owner(
    request: Request,
    identity: AuthIdentity,
    *,
    owner: str | None,
) -> str:
    """Return whose machines the caller lists: its own, or the assistant's asker's."""
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
    return _refused(
        403,
        code="terminal_forbidden",
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
    term = get_terminal_hub(request).terminal(terminal_id)
    if term is None or term.user != identity.email:
        raise NotFoundError("no such terminal")
    return term


async def _owned_machine(
    request: Request,
    identity: AuthIdentity,
    *,
    name: str,
) -> tuple[UUID, list[str]]:
    """Return the id and CLIs of the caller's online machine with a live credential."""
    async with engine_of(request).acquire() as conn:
        machine = await conn.fetchrow(
            "SELECT m.id, m.facts, m.confirmed_at IS NOT NULL AS confirmed "
            "FROM machines m WHERE m.name = $1 AND m.owner = $2 "
            "AND m.host_instance IS NOT NULL "
            "AND m.last_heartbeat > now() - make_interval(secs => $3) "
            "AND EXISTS (SELECT 1 FROM machine_credentials c "
            "WHERE c.machine_id = m.id AND c.revoked_at IS NULL)",
            name,
            identity.email,
            OFFLINE_AFTER_SEC,
        )
    if machine is None:
        raise NotFoundError(f"no terminal-capable machine {name}")
    if not machine["confirmed"]:
        raise _refused(
            409,
            code="terminal_unconfirmed",
            message="confirm this machine first",
        )
    return from_plain(machine["id"], UUID), _clis_of(machine["facts"])


def _clis_of(facts: object) -> list[str]:
    """Return the CLIs a machine's facts list; none when its host reported no list."""
    # Any fact may be a string, a number or a flag, so a host can send ``clis`` in
    # another shape; that machine then offers no CLI, rather than failing the listing.
    try:
        return from_plain(
            from_plain(facts, dict[str, object]).get("clis"),
            list[str],
            default=[],
        )
    except ReadError:
        return []


async def _station_workstream(
    request: Request,
    *,
    owner: str,
    workspace: UUID | None,
) -> UUID | None:
    """Return the workstream of the owner's station; refuse another user's."""
    if workspace is None:
        return None
    async with engine_of(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT w.station->>'workstream_id' AS workstream "
            "FROM visual_workspaces w JOIN users u ON u.id = w.user_id "
            "WHERE w.id = $1 AND u.email = $2 AND u.status = 'active'",
            workspace,
            owner,
        )
    if row is None:
        raise NotFoundError("no such station")
    workstream = from_plain(row["workstream"], str, default="")
    return UUID(workstream) if workstream else None


def _refused(status_code: int, *, code: str, message: str) -> HTTPException:
    """Return a refusal whose ``detail`` is ``{code, message}``."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


async def _require_instance(
    connection: HTTPConnection,
    machine_id: UUID,
    *,
    instance: UUID,
) -> None:
    """Refuse a host that is not the machine's current instance."""
    async with engine_of(connection).acquire() as conn:
        current = await conn.fetchval(
            "SELECT host_instance = $2 FROM machines WHERE id = $1",
            machine_id,
            instance,
        )
    if current is not True:
        raise HTTPException(status_code=403, detail="stale host instance")


async def _open_terminal(hub: TerminalHub, *, opening: Opening) -> UUID:
    """Queue one terminal, retaining only its id after it ends."""
    return hub.open(opening).id


async def _next_terminal(
    hub: TerminalHub,
    *,
    machine_id: UUID,
    wait_sec: float,
) -> TerminalWait | None:
    """Take one open and freeze the offer before its response can be lost."""
    term = await hub.wait(
        machine_id,
        wait_sec=wait_sec,
    )
    if term is None:
        return None
    return TerminalWait(
        terminal_id=term.id,
        opener=term.user,
        rows=term.rows,
        cols=term.cols,
        workspace=term.workspace,
        workstream=term.workstream,
        requested_by=term.requested_by,
        command=term.command,
        attach=term.attach,
        run_name=term.run_name,
        cwd=term.cwd,
        ice_servers=list(hub.ice_servers),
    )
