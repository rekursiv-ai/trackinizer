"""Routes a host's owner uses to see and stop the agent processes that run on it.

An agent process reaches the server on its own socket, not through the daemon: it
stays live after the daemon stops, while the host reads offline. The session relay
holds each process with the host it runs on (``SessionRelay.processes``), so the owner
sees here every process of the host that may still run, and stops one as its session's
own stop does (``stop_process``). Disconnecting a host revokes it, as the revoke route
does, which stops every process on it. Logging it out also revokes the owner's keys a
device login made for it, so nothing on the computer acts as them until a person
approves a login again. Where each session last said it ran is kept
(``session_liveness``), so a host's past runs are listed too, each with its
AgentSession's number and the chat it is a session of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from treekle import from_plain

from trackinizer.server.access_policy import inquiry_read_filter
from trackinizer.server.api._deps import get_session_relay, get_store
from trackinizer.server.api._routes_shared import request_engine
from trackinizer.server.api.daemon_routes import revoke_host
from trackinizer.server.api.hosts_routes import HostName, refuse_reserved
from trackinizer.server.api.session_identity import request_principal
from trackinizer.server.api.session_relay_routes import stop_process
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
    from collections.abc import Iterable, Sequence

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
    """List the agent processes on one of the caller's hosts, and its past runs.

    Args:
      name: The host's name.
      request: Request carrying the database engine and the session relay.
      identity: The host's owner.
      past: The most past runs to list.

    Returns:
      runs: Each agent process that may still run there, by session; and ``past``,
        the last ``past`` of the owner's sessions that ran there and are not live,
        most recently heard from first.

    Raises:
      NotFoundError: The caller owns no such host.

    """
    host_id, owner = await _host(request, identity, name=name)
    live = live_sessions(request, host_id=host_id)
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
    """Stop the agent process of a session on one of the caller's hosts.

    It is stopped as the session's own stop does: its socket closes, and its key is
    revoked a minute later. Its session stays, and a new process may serve it.

    Args:
      name: The host's name.
      session_id: The session the process serves.
      request: Request carrying the database engine and the session relay.
      identity: The host's owner.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: The caller owns no such host, or no process on it that may
        still run serves that session.

    """
    host_id, _ = await _host(request, identity, name=name)
    running = get_session_relay(request).processes(host_id)
    if session_id not in {process.session_id for process in running}:
        raise NotFoundError(f"no live session {session_id} on {name}")
    _ = await stop_process(request, session_id)
    return Response(status_code=204)


@router.post(HOST_DISCONNECT_PATH, response_model=HostRuns)
async def disconnect_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> HostRuns:
    """Revoke a daemon, which stops every agent process on its host.

    The host learns of it by a 410 on its next request, and its terminals, tool
    connections and agent processes are ended, as a revoke does. The host joins it
    again with the key the computer holds, which this leaves untouched.

    Args:
      name: The host's name.
      request: Request carrying the database engine and the relays.
      identity: Admin principal, or the host's owner.

    Returns:
      runs: The agent processes it stopped, by session.

    Raises:
      NotFoundError: No such host.
      HTTPException: 403 when a writer names a host that is not theirs.

    """
    return HostRuns(
        runs=_runs(await revoke_host(request, name=name, identity=identity)),
    )


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
      request: Request carrying the database engine and the relays.
      identity: The host's owner.

    Returns:
      logout: The agent processes it stopped and how many keys it revoked.

    Raises:
      NotFoundError: The caller owns no such host.

    """
    _, owner = await _host(request, identity, name=name)
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


def live_sessions(request: Request, *, host_id: UUID) -> list[HostRun]:
    """Return the agent processes that may still run on a host, by session.

    Args:
      request: Request carrying the session relay.
      host_id: The host.

    Returns:
      runs: Each process, as its session's run.

    """
    return _runs(
        process.session_id for process in get_session_relay(request).processes(host_id)
    )


async def named_runs(
    conn: Conn,
    runs: Sequence[HostRun],
    *,
    owner: str,
    policy: AccessPolicy | None,
    principal: Principal | None,
) -> list[HostRun]:
    """Name each run's AgentSession and the chat it is a session of, in ``runs``' order.

    A past run named its host on its own word, and a remote agent service runs the
    processes of accounts other than its owner's, so only a session of the owner's
    account that the caller may read is named; any other run is returned as it came,
    naming no session and no chat.

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


async def _host(
    request: Request,
    identity: AuthIdentity,
    *,
    name: str,
) -> tuple[UUID, str]:
    """Return the id of the caller's host ``name`` and its owner; refuse anyone else."""
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn:
        row = await conn.fetchrow("SELECT id, owner FROM hosts WHERE name = $1", name)
    owner = None if row is None else from_plain(row["owner"], str, default=None)
    # Another person's host reads as no host, as the confirm route has it.
    if row is None or owner is None or owner.lower() != identity.email.lower():
        raise NotFoundError(f"no host {name} of yours")
    return from_plain(row["id"], UUID), owner


# Every process the relay holds is sagent's own agent.
def _runs(session_ids: Iterable[UUID]) -> list[HostRun]:
    """Return the run of each session's agent process, by session."""
    return [HostRun(session_id=each, cli="sagent") for each in sorted(session_ids)]
