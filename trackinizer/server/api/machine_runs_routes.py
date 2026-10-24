"""Routes a machine's owner uses to see and stop what still reaches the server from it.

A run of a CLI reaches the server with its account's key, through its session's drain,
not through the machine's host: it stays live after the host stops, while the machine
reads offline. Each drain names the run's machine (``inbound.PollerRun``), so
the owner sees here the live runs of their account that name it, and stops them. A
stopped run's next drain is refused with 403 ``run_stopped``, on which every run ends.
Disconnecting a machine revokes its host, as the revoke route does, and stops every
run that names it.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import get_hub, get_inbound
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.machine_host_routes import revoke_machine
from trackinizer.server.api.machines_routes import MachineName, refuse_reserved
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_machines import (
    MACHINE_DISCONNECT_PATH,
    MACHINE_RUN_STOP_PATH,
    MACHINE_RUNS_PATH,
    MachineRun,
    MachineRuns,
)


__all__ = [
    "disconnect_machine_route",
    "list_machine_runs_route",
    "live_runs",
    "router",
    "stop_machine_run_route",
]


router = APIRouter()


@router.get(MACHINE_RUNS_PATH, response_model=MachineRuns)
async def list_machine_runs_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MachineRuns:
    """List the caller's live runs that name one of their machines.

    Args:
      name: The machine's name.
      request: Request carrying the database engine and the inbound queue.
      identity: The machine's owner.

    Returns:
      runs: Each live run, by CLI, then host name, then session.

    Raises:
      NotFoundError: The caller owns no such machine.

    """
    owner = await _owner(request, identity, name=name)
    return MachineRuns(runs=live_runs(request, machine=name, owner=owner))


@router.post(MACHINE_RUN_STOP_PATH, status_code=204)
async def stop_machine_run_route(
    name: MachineName,
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Stop one of the caller's live runs on one of their machines.

    Its lease ends now, and its next drain is refused, so the run ends within one
    drain's hold. Its chat stays, and a new run may take it.

    Args:
      name: The machine's name.
      session_id: The session the run drains.
      request: Request carrying the database engine and the inbound queue.
      identity: The machine's owner.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: The caller owns no such machine, or no live run of theirs on it
        drains that session.

    """
    owner = await _owner(request, identity, name=name)
    inbound = get_inbound(request)
    if session_id not in inbound.runs_on(name, account=owner):
        raise NotFoundError(f"no live run of session {session_id} on {name}")
    inbound.stop_run(session_id)
    # The run's chat lost its partner.
    get_hub(request).nudge()
    return Response(status_code=204)


@router.post(MACHINE_DISCONNECT_PATH, response_model=MachineRuns)
async def disconnect_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MachineRuns:
    """Revoke a machine's host and stop every live run of its owner that names it.

    The host learns of it by a 410 on its next request, and its terminals and
    commands are closed, as a revoke does. The host joins it again with the key the
    computer holds, which this leaves untouched.

    Args:
      name: The machine's name.
      request: Request carrying the database engine, the hubs and the inbound queue.
      identity: Admin principal, or the machine's owner.

    Returns:
      runs: The runs it stopped.

    Raises:
      NotFoundError: No such machine.
      HTTPException: 403 when a writer names a machine that is not theirs.

    """
    owner = await revoke_machine(request, name=name, identity=identity)
    runs = [] if owner is None else live_runs(request, machine=name, owner=owner)
    inbound = get_inbound(request)
    for each in runs:
        inbound.stop_run(each.session_id)
    if runs:
        get_hub(request).nudge()
    return MachineRuns(runs=runs)


def live_runs(request: Request, *, machine: str, owner: str) -> list[MachineRun]:
    """Return ``owner``'s live runs that name ``machine``, by CLI, host name, session.

    Args:
      request: Request carrying the inbound queue.
      machine: The machine's name.
      owner: The machine owner's email.

    Returns:
      runs: Each live run.

    """
    runs = [
        MachineRun(
            session_id=session_id,
            cli=from_plain(where.cli, str),
            run=where.run,
            cwd=where.cwd,
            state=where.state,
        )
        for session_id, where in get_inbound(request)
        .runs_on(machine, account=owner)
        .items()
    ]
    return sorted(runs, key=lambda run: (run.cli, run.run or "", str(run.session_id)))


async def _owner(request: Request, identity: AuthIdentity, *, name: str) -> str:
    """Return the caller's email when they own the machine; refuse anyone else."""
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        owner = from_plain(
            await conn.fetchval("SELECT owner FROM machines WHERE name = $1", name),
            str,
            default=None,
        )
    # Another person's machine reads as no machine, as the confirm route has it.
    if owner is None or owner.lower() != identity.email.lower():
        raise NotFoundError(f"no machine {name} of yours")
    return owner
