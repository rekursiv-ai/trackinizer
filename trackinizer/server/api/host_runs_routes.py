"""Routes a host's owner uses to see and stop what still reaches the server from it.

A run of a CLI reaches the server with its account's key, through its session's drain,
not through the daemon: it stays live after the host stops, while the host
reads offline. Each drain names the run's host (``inbound.PollerRun``), so
the owner sees here the live sessions of their account that name it, and stops them. A
stopped run's next drain is refused with 403 ``run_stopped``, on which every run ends.
Disconnecting a host revokes its host, as the revoke route does, and stops every
run that names it. Logging it out also revokes the owner's keys a device login made for
it, so nothing on the computer acts as them until a person approves a login again.
Where each session's run last said it ran is kept (``session_liveness``), so a host's
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
from trackinizer.server.api.daemon_routes import revoke_host
from trackinizer.server.api.hosts_routes import HostName, refuse_reserved
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.values import vetted_sql
from trackinizer.types.errors import NotFoundError
from trackinizer.wire.wire_hosts import (
    HOST_DISCONNECT_PATH,
    HOST_LOGOUT_PATH,
    HOST_RUN_STOP_PATH,
    HOST_RUNS_PATH,
    HostLogout,
    HostRun,
    HostRuns,
)
from trackinizer.wire.wire_science_chat import chat_conversation_id


if TYPE_CHECKING:
    from collections.abc import Sequence

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.access_policy import AccessPolicy, Principal


__all__ = [
    "disconnect_host_route",
    "list_host_runs_route",
    "live_sessions",
    "logout_host_route",
    "named_runs",
    "router",
    "stop_host_run_route",
]


router = APIRouter()


@router.get(HOST_RUNS_PATH, response_model=HostRuns)
async def list_host_runs_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
    past: Annotated[int, Query(ge=0, le=100)] = 20,
) -> HostRuns:
    """List the caller's live sessions that name one of their hosts, and its past ones.

    Args:
      name: The host's name.
      request: Request carrying the database engine and the inbound queue.
      identity: The host's owner.
      past: The most past runs to list.

    Returns:
      runs: Each live session, by CLI, then host name, then session; and ``past``, the
        last ``past`` that are not live, most recently heard from first.

    Raises:
      NotFoundError: The caller owns no such host.

    """
    owner = await _owner(request, identity, name=name)
    live = live_sessions(request, host=name, owner=owner)
    shown = {run.session_id for run in live}
    before = await get_store(request).past_runs(
        host=name,
        account=owner,
        limit=past + len(shown),
    )
    earlier = [
        HostRun(
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
    return HostRuns(runs=named[: len(live)], past=named[len(live) :])


@router.post(HOST_RUN_STOP_PATH, status_code=204)
async def stop_host_run_route(
    name: HostName,
    session_id: UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Stop one of the caller's live sessions on one of their hosts.

    Its lease ends now, and its next drain is refused, so the run ends within one
    drain's hold. Its chat stays, and a new run may take it.

    Args:
      name: The host's name.
      session_id: The session the run drains.
      request: Request carrying the database engine and the inbound queue.
      identity: The host's owner.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: The caller owns no such host, or no live session of theirs on it
        drains that session.

    """
    owner = await _owner(request, identity, name=name)
    inbound = get_inbound(request)
    if session_id not in inbound.runs_on(name, account=owner):
        raise NotFoundError(f"no live session {session_id} on {name}")
    inbound.stop_run(session_id)
    # The run's chat lost its partner.
    nudge_streams(request)
    return Response(status_code=204)


@router.post(HOST_DISCONNECT_PATH, response_model=HostRuns)
async def disconnect_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> HostRuns:
    """Revoke a daemon and stop every live session of its owner that names it.

    The host learns of it by a 410 on its next request, and its terminals and
    commands are closed, as a revoke does. The host joins it again with the key the
    computer holds, which this leaves untouched.

    Args:
      name: The host's name.
      request: Request carrying the database engine, the hubs and the inbound queue.
      identity: Admin principal, or the host's owner.

    Returns:
      runs: The runs it stopped.

    Raises:
      NotFoundError: No such host.
      HTTPException: 403 when a writer names a host that is not theirs.

    """
    owner = await revoke_host(request, name=name, identity=identity)
    runs = [] if owner is None else live_sessions(request, host=name, owner=owner)
    inbound = get_inbound(request)
    for each in runs:
        inbound.stop_run(each.session_id)
    if runs:
        nudge_streams(request)
    return HostRuns(runs=runs)


@router.post(HOST_LOGOUT_PATH, response_model=HostLogout)
async def logout_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> HostLogout:
    """Disconnect one of the caller's hosts and revoke the keys it logged in with.

    Disconnects it as the disconnect route does, then revokes the caller's live keys
    a device login made for it (``api_keys.host_id``). A key made another way, or
    before keys named their host, is not one: it is revoked in Settings.

    Args:
      name: The host's name.
      request: Request carrying the database engine, the hubs and the inbound queue.
      identity: The host's owner.

    Returns:
      logout: The runs it stopped and how many keys it revoked.

    Raises:
      NotFoundError: The caller owns no such host.

    """
    owner = await _owner(request, identity, name=name)
    stopped = await disconnect_host_route(name, request, identity)
    async with request_engine(request).acquire() as conn:
        keys = await conn.fetch(
            "UPDATE api_keys SET revoked_at = clock_timestamp() FROM hosts "
            "WHERE api_keys.host_id = hosts.id AND hosts.name = $1 "
            "AND hosts.owner = $2 AND api_keys.user_id = $3 "
            "AND api_keys.revoked_at IS NULL RETURNING api_keys.id",
            name,
            owner,
            identity.user_id,
        )
    store = get_store(request)
    for key in keys:
        store.forget_bearer_identity_for_key(from_plain(key["id"], UUID))
    return HostLogout(runs=stopped.runs, keys=len(keys))


def live_sessions(request: Request, *, host: str, owner: str) -> list[HostRun]:
    """Return ``owner``'s live sessions that name ``host``, by CLI, host name, session.

    Args:
      request: Request carrying the inbound queue.
      host: The host's name.
      owner: The host owner's email.

    Returns:
      runs: Each live session.

    """
    runs = [
        HostRun(
            session_id=session_id,
            cli=from_plain(where.cli, str),
            run=where.run,
            cwd=where.cwd,
            state=where.state,
        )
        for session_id, where in get_inbound(request)
        .runs_on(host, account=owner)
        .items()
    ]
    return sorted(runs, key=lambda run: (run.cli, run.run or "", str(run.session_id)))


async def named_runs(
    conn: Conn,
    runs: Sequence[HostRun],
    *,
    owner: str,
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[HostRun]:
    """Name each run's AgentSession and the chat it is a session of, in ``runs``' order.

    A run names its host on its own word, and drains with its owner's key, so only
    a session of the owner's account that the caller may read is named; any other run
    is returned as it came, naming no session and no chat.

    Args:
      conn: Database connection.
      runs: The runs, live or past, of ``owner``'s host.
      owner: The host owner's email.
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
    """Return the caller's email when they own the host; refuse anyone else."""
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn:
        owner = from_plain(
            await conn.fetchval("SELECT owner FROM hosts WHERE name = $1", name),
            str,
            default=None,
        )
    # Another person's host reads as no host, as the confirm route has it.
    if owner is None or owner.lower() != identity.email.lower():
        raise NotFoundError(f"no host {name} of yours")
    return owner
