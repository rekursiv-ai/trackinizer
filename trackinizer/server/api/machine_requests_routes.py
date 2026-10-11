"""Routes for machine requests: what a machine asks its provider to do.

A machine writes a request with its own credential. An admin (the provider's key)
or the machine itself reads it back; a machine credential never reaches another
machine's requests, which answer it 404. Only an admin records the result, once.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from treekle import from_plain

from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.machines_routes import (
    MachineName,
    absent_machine,
    refuse_reserved,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.machine_auth import (
    MachineIdentity,
    machine_revoked,
    named_machine,
    named_machine_or_admin,
)
from trackinizer.server.notify import tx
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.wire.wire_machine_requests import (
    MACHINE_REQUEST_PATH,
    MACHINE_REQUEST_RESULT_PATH,
    MACHINE_REQUESTS_PATH,
    MachineRequest,
    MachineRequestCreate,
    MachineRequestList,
    MachineRequestResult,
    RequestStatus,
)


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn


__all__ = [
    "RequestFinishedError",
    "create_machine_request_route",
    "finish_machine_request_route",
    "get_machine_request_route",
    "list_machine_requests_route",
    "router",
]


router = APIRouter()


class RequestFinishedError(ConflictError):
    """The request already has its result."""

    code = "request_finished"


@router.post(MACHINE_REQUESTS_PATH, response_model=MachineRequest, status_code=201)
async def create_machine_request_route(
    name: MachineName,
    body: MachineRequestCreate,
    request: Request,
    machine: Annotated[MachineIdentity, Depends(named_machine)],
) -> MachineRequest:
    """Write a request for the machine's provider, as the machine.

    Args:
      name: The machine's name; the credential must be its own.
      body: What is asked for, and what the provider needs to do it.
      request: Request carrying the database engine.
      machine: The machine the credential authenticated as.

    Returns:
      created: The request, ``pending``.

    Raises:
      HTTPException: 410 when the credential was revoked after it authenticated.

    """
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn, tx(conn):
        # Lock first and check second, as the heartbeat does: a revoke or destroy in
        # flight holds the row until it commits, and only a statement that starts
        # after the lock sees that commit, so no request lands after it. A share lock
        # is enough to wait for one, and lets a machine's own requests run together.
        await conn.execute(
            "SELECT id FROM machines WHERE id = $1 FOR SHARE",
            machine.machine_id,
        )
        if not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM machine_credentials "
            "WHERE id = $1 AND revoked_at IS NULL)",
            machine.credential_id,
        ):
            raise machine_revoked()
        (row,) = await conn.fetch(
            "INSERT INTO machine_requests (machine_id, kind, body, created_by) "
            "VALUES ($1, $2, $3, $4) RETURNING id, kind, body, status, result, "
            "created_by, created, updated",
            machine.machine_id,
            body.kind,
            body.body,
            machine.name,
        )
    return _request(row)


@router.get(MACHINE_REQUESTS_PATH, response_model=MachineRequestList)
async def list_machine_requests_route(
    name: MachineName,
    request: Request,
    caller: Annotated[
        MachineIdentity | AuthIdentity,
        Depends(named_machine_or_admin),
    ],
    status: Annotated[RequestStatus | None, Query()] = None,
) -> MachineRequestList:
    """List the machine's requests, oldest first, as an admin or the machine.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      caller: The machine itself, or an admin.
      status: Only requests in this status; every request when absent.

    Returns:
      requests: The machine's requests, oldest first.

    Raises:
      NotFoundError: No such machine.

    """
    del caller
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        machine_id = await _machine_id(conn, name)
        rows = await conn.fetch(
            "SELECT r.id, r.kind, r.body, r.status, r.result, r.created_by, r.created, "
            "r.updated FROM machine_requests r WHERE r.machine_id = $1 AND ($2::text IS NULL OR r.status = $2) "
            "ORDER BY r.created, r.id",
            machine_id,
            status,
        )
    return MachineRequestList(requests=[_request(row) for row in rows])


@router.get(MACHINE_REQUEST_PATH, response_model=MachineRequest)
async def get_machine_request_route(
    name: MachineName,
    request_id: UUID,
    request: Request,
    caller: Annotated[
        MachineIdentity | AuthIdentity,
        Depends(named_machine_or_admin),
    ],
) -> MachineRequest:
    """Return one of the machine's requests, as an admin or the machine.

    Args:
      name: The machine's name.
      request_id: The request's id.
      request: Request carrying the database engine.
      caller: The machine itself, or an admin.

    Returns:
      found: The request.

    Raises:
      NotFoundError: No such machine, or no such request of it.

    """
    del caller
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        row = await conn.fetchrow(
            "SELECT r.id, r.kind, r.body, r.status, r.result, r.created_by, r.created, "
            "r.updated FROM machine_requests r JOIN machines m ON m.id = r.machine_id WHERE r.id = $1 AND m.name = $2",
            request_id,
            name,
        )
    if row is None:
        raise _absent_request(name, request_id)
    return _request(row)


@router.post(MACHINE_REQUEST_RESULT_PATH, response_model=MachineRequest)
async def finish_machine_request_route(
    name: MachineName,
    request_id: UUID,
    body: MachineRequestResult,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MachineRequest:
    """Record the provider's one answer to a pending request.

    Args:
      name: The machine's name.
      request_id: The request's id.
      body: ``done`` or ``failed``, and what the provider reports.
      request: Request carrying the database engine.
      identity: Admin principal: the provider's key.

    Returns:
      finished: The request with its result.

    Raises:
      NotFoundError: No such machine, or no such request of it.
      RequestFinishedError: The request already has its result.

    """
    del identity
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        # One statement: two answers racing each other serialise on the row, and the
        # second re-reads ``status`` after the first commits, so only one lands.
        row = await conn.fetchrow(
            "UPDATE machine_requests r SET status = $3, result = $4, updated = now() "
            "FROM machines m WHERE m.id = r.machine_id AND r.id = $1 AND m.name = $2 "
            "AND r.status = 'pending' RETURNING r.id, r.kind, r.body, r.status, "
            "r.result, r.created_by, r.created, r.updated",
            request_id,
            name,
            body.status,
            body.result,
        )
        if row is None and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM machine_requests r "
            "JOIN machines m ON m.id = r.machine_id WHERE r.id = $1 AND m.name = $2)",
            request_id,
            name,
        ):
            raise RequestFinishedError(f"request {request_id} already has its result")
    if row is None:
        raise _absent_request(name, request_id)
    return _request(row)


async def _machine_id(conn: Conn, name: str) -> UUID:
    """Return the machine's id, or raise the 404 for an unregistered name."""
    machine_id = await conn.fetchval("SELECT id FROM machines WHERE name = $1", name)
    if machine_id is None:
        raise absent_machine(name)
    return from_plain(machine_id, UUID)


def _absent_request(name: str, request_id: UUID) -> NotFoundError:
    """Return the error for a request the machine does not have."""
    return NotFoundError(f"machine {name!r} has no request {request_id}")


def _request(row: Record) -> MachineRequest:
    """Build the wire model of one ``machine_requests`` row."""
    return MachineRequest.model_validate(
        {
            "id": from_plain(row["id"], UUID),
            "kind": from_plain(row["kind"], str),
            "body": from_plain(row["body"], dict[str, object]),
            "status": from_plain(row["status"], str),
            "result": from_plain(row["result"], dict[str, object]),
            "created_by": from_plain(row["created_by"], str),
            "created": from_plain(row["created"], datetime),
            "updated": from_plain(row["updated"], datetime),
        },
    )
