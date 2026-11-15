"""Where the routes meet the agent policy: a remote agent's start, its lines, its records.

A remote agent's start, and each line sent to a live one, first asks the deployment's
agent policy (:mod:`trackinizer.server.agent_policy`), and is answered as it
says when it refuses. Each upload a remote agent's process makes is shown to the
policy once it is stored; when the policy asks, every agent of each session it names
is halted, and each of those sessions' viewers told what it says.

A remote agent is one a configured remote agent service runs: the host enrolled under
the service's name by its own service account. With no policy installed each of
these does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import logging

from trackinizer.server.api._deps import (
    get_agent_policy,
    get_remote_agent_services,
    get_session_relay,
)
from trackinizer.server.api._routes_shared import refusal, request_engine
from trackinizer.server.session_relay import InputRefusedError
from trackinizer.wire.wire_session_relay import EventLine, NewInput


if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.requests import HTTPConnection

    from trackinizer.lib.postgres import Conn
    from trackinizer.server.agent_policy import AgentPolicy, AgentWork, Halt
    from trackinizer.server.auth import AuthIdentity
    from trackinizer.server.config import RemoteAgentService
    from trackinizer.wire.wire_session_ir import RecordBody


__all__ = ["after_stored", "refuse_line", "refuse_start", "remote_agent_hosts"]

_logger = logging.getLogger(__name__)

_HALT_AUTHOR: Final = "server"
"""Who a halt the policy asks for is sent as: the server, never a person."""


async def refuse_start(
    request: HTTPConnection,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> None:
    """Refuse a remote agent's start that the agent policy refuses.

    Args:
      request: The request, carrying the policy and the database.
      identity: Who asks for the start.
      session_id: The session whose agent would start.

    Raises:
      HTTPException: As the policy's refusal says.

    """
    policy = get_agent_policy(request)
    if policy is not None:
        await _refuse(request, policy, identity, session_id=session_id, work="start")


async def refuse_line(
    request: HTTPConnection,
    identity: AuthIdentity,
    *,
    session_id: UUID,
) -> None:
    """Refuse a line to a live remote agent that the agent policy refuses.

    A line to an agent on a person's own host is never asked about.

    Args:
      request: The request, carrying the policy, the relay and the database.
      identity: Who sends the line: a person, or another session's agent's key.
      session_id: The session the line is for.

    Raises:
      HTTPException: As the policy's refusal says.

    """
    policy = get_agent_policy(request)
    if policy is None:
        return
    async with request_engine(request).acquire() as conn:
        hosts = await remote_agent_hosts(conn, get_remote_agent_services(request))
    relay = get_session_relay(request)
    if any(
        process.session_id == session_id
        for host in hosts
        for process in relay.processes(host)
    ):
        await _refuse(request, policy, identity, session_id=session_id, work="line")


async def after_stored(
    request: HTTPConnection,
    identity: AuthIdentity,
    *,
    session_id: UUID,
    part: int,
    records: Sequence[RecordBody],
) -> None:
    """Show the agent policy what a remote agent stored; halt what it says to halt.

    Only a key a start minted for a process on a remote agent service is shown: a
    person's own key, and a process on a person's own host, are not.

    Args:
      request: The request, carrying the policy, the relay and the database.
      identity: The key that appended the records.
      session_id: The session they are stored in.
      part: The part they are stored in.
      records: The request's records, in ``idx`` order.

    """
    policy = get_agent_policy(request)
    process = identity.process
    if policy is None or process is None or not records:
        return
    engine = request_engine(request)
    async with engine.acquire() as conn:
        hosts = await remote_agent_hosts(conn, get_remote_agent_services(request))
    if process.host_id not in hosts:
        return
    relay = get_session_relay(request)
    live = [
        each.session_id
        for host in hosts
        for each in relay.processes(host)
        if each.state != "detached"
    ]
    halt = await policy.stored(
        engine,
        session_id=session_id,
        part=part,
        records=records,
        live=live,
    )
    if halt is not None:
        _halt(request, halt=halt, live=live)


async def remote_agent_hosts(
    conn: Conn,
    services: Sequence[RemoteAgentService],
) -> frozenset[UUID]:
    """Return the host of each remote agent service the configuration names.

    A service is the host enrolled under its name by its own service account: the
    name enrolled by anyone else is not one.

    Args:
      conn: A database connection.
      services: The configured remote agent services.

    Returns:
      hosts: Their hosts' ids.

    """
    rows = await conn.fetch(
        "SELECT h.id FROM hosts AS h "
        "JOIN unnest($1::text[], $2::text[]) AS s(name, owner) "
        "ON h.name = s.name AND h.owner = s.owner",
        [service.name for service in services],
        [service.account for service in services],
    )
    return frozenset(cast(UUID, row["id"]) for row in rows)


async def _refuse(
    request: HTTPConnection,
    policy: AgentPolicy,
    identity: AuthIdentity,
    *,
    session_id: UUID,
    work: AgentWork,
) -> None:
    """Raise the policy's refusal of ``work`` on the session, when it refuses."""
    refused = await policy.refusal(
        request_engine(request),
        asker=identity.email,
        session_id=session_id,
        work=work,
    )
    if refused is not None:
        raise refusal(refused.status, code=refused.code, message=refused.message)


# A process whose inputs are too full to take a halt now is passed: the policy asks
# again at the session's next upload. A session that is no longer live is skipped, so
# a halt reaches only a process the relay serves; nothing awaits between the read of
# ``live`` and the sends.
def _halt(request: HTTPConnection, *, halt: Halt, live: Sequence[UUID]) -> None:
    """Halt every agent of each live session ``halt`` names; tell its viewers why."""
    relay = get_session_relay(request)
    told = EventLine(
        kind="CommandOutput",
        payload={
            "py/object": "CommandOutput",
            "text": halt.notice,
            "tone": "error",
            "command": "",
        },
    )
    for session_id in live:
        if session_id not in halt.sessions:
            continue
        try:
            _ = relay.send_input(
                session_id,
                NewInput(kind="Halt", payload={"py/object": "Halt"}),
                author=_HALT_AUTHOR,
                recipient="all",
            )
        except InputRefusedError:
            _logger.warning("could not halt session %s now", session_id)
            continue
        relay.tell(session_id, told)
