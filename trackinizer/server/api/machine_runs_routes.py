"""Routes a machine's owner uses to see and stop what still reaches the server from it.

A run of a CLI reaches the server with its account's key, through its session's drain,
not through the machine's host: it stays live after the host stops, while the machine
reads offline. Each drain names the run's machine (``inbound.PollerRun``), so
the owner sees here the live runs of their account that name it, and stops them. A
stopped run's next drain is refused with 403 ``run_stopped``, on which every run ends.
Disconnecting a machine revokes its host, as the revoke route does, and stops every
run that names it. Logging it out also revokes the owner's keys a device login made for
it, so nothing on the computer acts as them until a person approves a login again.
Where each session's run last said it ran is kept (``session_liveness``), so a machine's
past runs are listed too, each with its AgentSession's number and the chat it is a
session of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from treekle import from_plain

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.api._deps import get_inbound, get_store, nudge_streams
from trackinizer.server.api._routes_shared import request_engine
from trackinizer.server.api.machine_host_routes import revoke_machine
from trackinizer.server.api.machines_routes import MachineName, refuse_reserved
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_machines import (
    MACHINE_DISCONNECT_PATH,
    MACHINE_LOGOUT_PATH,
    MACHINE_RUN_STOP_PATH,
    MACHINE_RUNS_PATH,
    MachineLogout,
    MachineRun,
    MachineRuns,
)
from trackinizer.wire.wire_science_chat import chat_conversation_id


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy, Principal


__all__ = [
    "disconnect_machine_route",
    "list_machine_runs_route",
    "live_runs",
    "logout_machine_route",
    "named_runs",
    "router",
    "stop_machine_run_route",
]


router = APIRouter()


@router.get(MACHINE_RUNS_PATH, response_model=MachineRuns)
async def list_machine_runs_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    past: Annotated[int, Query(ge=0, le=100)] = 20,
) -> MachineRuns:
    """List the caller's live runs that name one of their machines, and its past ones.

    Args:
      name: The machine's name.
      request: Request carrying the database engine and the inbound queue.
      identity: The machine's owner.
      past: The most past runs to list.

    Returns:
      runs: Each live run, by CLI, then host name, then session; and ``past``, the
        last ``past`` that are not live, most recently heard from first.

    Raises:
      NotFoundError: The caller owns no such machine.

    """
    owner = await _owner(request, identity, name=name)
    live = live_runs(request, machine=name, owner=owner)
    shown = {run.session_id for run in live}
    before = await get_store(request).past_runs(
        machine=name,
        account=owner,
        limit=past + len(shown),
    )
    earlier = [
        MachineRun(
            session_id=run.session_id,
            cli=run.cli,
            run=run.run,
            cwd=run.cwd,
            last_seen=run.last_seen,
            ended=run.ended,
        )
        for run in before
        if run.session_id not in shown
    ][:past]
    principal = await request_principal(request, identity)
    async with request_engine(request).acquire() as conn:
        named = await named_runs(
            conn,
            [*live, *earlier],
            owner=owner,
            policy=get_store(request).access_policy,
            principal=principal,
        )
    return MachineRuns(runs=named[: len(live)], past=named[len(live) :])


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
    nudge_streams(request)
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
        nudge_streams(request)
    return MachineRuns(runs=runs)


@router.post(MACHINE_LOGOUT_PATH, response_model=MachineLogout)
async def logout_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MachineLogout:
    """Disconnect one of the caller's machines and revoke the keys it logged in with.

    Disconnects it as the disconnect route does, then revokes the caller's live keys
    a device login made for it (``api_keys.machine_id``). A key made another way, or
    before keys named their machine, is not one: it is revoked in Settings.

    Args:
      name: The machine's name.
      request: Request carrying the database engine, the hubs and the inbound queue.
      identity: The machine's owner.

    Returns:
      logout: The runs it stopped and how many keys it revoked.

    Raises:
      NotFoundError: The caller owns no such machine.

    """
    owner = await _owner(request, identity, name=name)
    stopped = await disconnect_machine_route(name, request, identity)
    async with request_engine(request).acquire() as conn:
        keys = await conn.fetch(
            "UPDATE api_keys SET revoked_at = clock_timestamp() FROM machines "
            "WHERE api_keys.machine_id = machines.id AND machines.name = $1 "
            "AND machines.owner = $2 AND api_keys.user_id = $3 "
            "AND api_keys.revoked_at IS NULL RETURNING api_keys.id",
            name,
            owner,
            identity.user_id,
        )
    store = get_store(request)
    for key in keys:
        store.forget_bearer_identity_for_key(from_plain(key["id"], UUID))
    return MachineLogout(runs=stopped.runs, keys=len(keys))


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


async def named_runs(
    conn: Conn,
    runs: Sequence[MachineRun],
    *,
    owner: str,
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[MachineRun]:
    """Name each run's AgentSession and the chat it is a session of, in ``runs``' order.

    A run names its machine on its own word, and drains with its owner's key, so only
    a session of the owner's account that the caller may read is named; any other run
    is returned as it came, naming no session and no chat.

    Args:
      conn: Database connection.
      runs: The runs, live or past, of ``owner``'s machine.
      owner: The machine owner's email.
      policy: Deployment policy; None preserves trusted reads.
      principal: The caller, as the access policy reads them; None preserves
        trusted reads.

    Returns:
      runs: Each run, with its session's ``seq`` and ``conversation_id``.

    """
    if not runs:
        return []
    params: list[object] = [[run.session_id for run in runs], owner]
    readable = inquiry_read_filter(
        "inquiries.id",
        policy=policy,
        principal=principal,
        params=params,
    )
    rows = await conn.fetch(
        vetted_sql(
            "SELECT id, seq, agentsession_cli_session_id AS chat FROM inquiries "
            "WHERE id = ANY($1::uuid[]) AND kind = 'AgentSession' "
            "AND lower(account) = lower($2)",
            "" if readable == "TRUE" else f" AND ({readable})",
        ),
        *params,
    )
    found = {from_plain(row["id"], UUID): row for row in rows}
    return [
        run
        if (row := found.get(run.session_id)) is None
        else run.model_copy(
            update={
                "seq": from_plain(row["seq"], int),
                "conversation_id": chat_conversation_id(
                    from_plain(row["chat"], str, default=None),
                ),
            },
        )
        for run in runs
    ]


async def _owner(request: Request, identity: AuthIdentity, *, name: str) -> str:
    """Return the caller's email when they own the machine; refuse anyone else."""
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn:
        owner = from_plain(
            await conn.fetchval("SELECT owner FROM machines WHERE name = $1", name),
            str,
            default=None,
        )
    # Another person's machine reads as no machine, as the confirm route has it.
    if owner is None or owner.lower() != identity.email.lower():
        raise NotFoundError(f"no machine {name} of yours")
    return owner
