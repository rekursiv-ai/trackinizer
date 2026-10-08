"""Routes a machine host uses to enroll, join and stay connected, and the revoke route.

An admin enrolls a machine by name and hands the host a one-use, 15-minute token.
The host joins with it, once, and receives a machine credential. It then heartbeats
with that credential, and its machine reads online until it has been silent for 180
seconds. An admin revokes the machine; the credential's row is kept, so every host
route then answers it 410 instead of 401.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.machines_routes import (
    MachineName,
    absent_machine,
    refuse_reserved,
)
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.machine_auth import (
    MachineIdentity,
    ParsedSecret,
    current_machine,
    machine_revoked,
    mint_secret,
    parse_enrollment,
)
from trackinizer.server.notify import tx
from trackinizer.types.errors import ConflictError
from trackinizer.wire.wire_machine_host import (
    CREDENTIAL_PREFIX,
    ENROLL_PATH,
    ENROLL_TTL_SEC,
    ENROLLMENT_PREFIX,
    HEARTBEAT_PATH,
    JOIN_PATH,
    OFFLINE_AFTER_SEC,
    REVOKE_PATH,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JoinRequest,
    JoinResponse,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "AnotherHostConnectedError",
    "enroll_machine_route",
    "heartbeat_machine_route",
    "join_machine_route",
    "revoke_machine_route",
    "router",
]


router = APIRouter()


class AnotherHostConnectedError(ConflictError):
    """A different host instance heartbeat while the connected one is still live."""

    code = "another_host_connected"


@router.post(ENROLL_PATH, response_model=EnrollResponse, status_code=201)
async def enroll_machine_route(
    body: EnrollRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> EnrollResponse:
    """Register the machine if it is new and issue a one-use enrollment token.

    A new token supersedes any open one for the same machine.

    Args:
      body: The machine's name.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.
      identity: Admin principal, recorded as who enrolled the machine.

    Returns:
      enrolled: The token, shown once, and when it expires.

    Raises:
      ValidationError: The name is reserved.

    """
    refuse_reserved(body.name)
    minted = mint_secret(ENROLLMENT_PREFIX)
    async with engine_of(request).acquire() as conn, tx(conn):
        # One statement registers the machine or, through the no-op update, locks its
        # existing row. A DO NOTHING insert followed by a separate lock leaves a gap in
        # which a delete commits: the lock then finds no row, the enrollment is
        # inserted with a NULL machine, and the route answers 500. The lock also
        # serialises two enrollments of one machine, so the later one always
        # supersedes the earlier.
        machine_id = await conn.fetchval(
            "INSERT INTO machines (name, updated_by) VALUES ($1, $2) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            body.name,
            identity.email,
        )
        await conn.execute(
            "UPDATE machine_enrollments SET used_at = now() "
            "WHERE machine_id = $1 AND used_at IS NULL",
            machine_id,
        )
        expires_at = await conn.fetchval(
            "INSERT INTO machine_enrollments "
            "(id, machine_id, secret_sha256, created_by, expires_at) "
            "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) "
            "RETURNING expires_at",
            minted.id,
            machine_id,
            minted.digest,
            identity.email,
            ENROLL_TTL_SEC,
        )
    response.headers["Cache-Control"] = "no-store"
    return EnrollResponse(
        token=minted.token,
        expires_at=from_plain(expires_at, datetime),
    )


@router.post(JOIN_PATH, response_model=JoinResponse, status_code=201)
async def join_machine_route(
    body: JoinRequest,
    request: Request,
    response: Response,
) -> JoinResponse:
    """Exchange an enrollment token for a machine credential.

    The token is the credential, so the route has no other authentication. It is
    valid once, within 15 minutes, for the machine name it was issued for, and only
    while the admin who issued it is still an active admin. Joining a machine that
    already has a live credential revokes that credential first.

    Args:
      body: The machine name, the token, and the host's instance, version and facts.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.

    Returns:
      joined: The machine's id and its credential, shown once.

    Raises:
      HTTPException: 401 when the token is malformed, unknown, used, expired, issued
        for another machine, or issued by an account that is no longer an active admin.

    """
    parsed = parse_enrollment(body.token)
    if parsed is None:
        raise _refused()
    async with engine_of(request).acquire() as conn:
        # Refused by a plain read, before any lock. Join has no authentication, and
        # the machine row lock below queues behind that machine's heartbeats and admin
        # writes, so a junk token naming a real machine would stall all of them.
        if not await _token_is_usable(conn, parsed=parsed, name=body.name):
            raise _refused()
        minted = mint_secret(CREDENTIAL_PREFIX)
        async with tx(conn):
            # The machine row first, as in every other route that writes a machine: a
            # join that claimed the enrollment first would deadlock with an enroll,
            # revoke or delete holding the machine and wanting the enrollment.
            machine_id = await conn.fetchval(
                "SELECT id FROM machines WHERE name = $1 FOR UPDATE",
                body.name,
            )
            if machine_id is None:
                raise _refused()
            # The claim checks everything again, because the read above held no lock:
            # a concurrent join or revoke may have used the token since, and two joins
            # that both passed the read would otherwise each get a credential.
            claimed = await conn.fetchval(
                "UPDATE machine_enrollments SET used_at = now() "
                "WHERE id = $1 AND used_at IS NULL AND expires_at > now() "
                "AND secret_sha256 = $2 AND machine_id = $3 "
                "AND EXISTS (SELECT 1 FROM users WHERE email = created_by "
                "AND status = 'active' AND role = 'admin') "
                "RETURNING id",
                parsed.id,
                parsed.digest,
                machine_id,
            )
            if claimed is None:
                raise _refused()
            await _revoke_credentials(conn, from_plain(machine_id, UUID))
            await conn.execute(
                "INSERT INTO machine_credentials (id, machine_id, secret_sha256) "
                "VALUES ($1, $2, $3)",
                minted.id,
                machine_id,
                minted.digest,
            )
            await conn.execute(
                "UPDATE machines SET facts = $2, host_instance = $3, "
                "host_version = $4, last_heartbeat = now() WHERE id = $1",
                machine_id,
                body.facts,
                body.instance,
                body.host_version,
            )
    response.headers["Cache-Control"] = "no-store"
    return JoinResponse(
        machine_id=from_plain(machine_id, UUID),
        credential=minted.token,
    )


@router.post(HEARTBEAT_PATH, response_model=HeartbeatResponse)
async def heartbeat_machine_route(
    body: HeartbeatRequest,
    request: Request,
    machine: Annotated[MachineIdentity, Depends(current_machine)],
) -> HeartbeatResponse:
    """Record that the host is alive, and what it reports about itself.

    One host instance holds a machine at a time. A different instance is refused
    until the connected one has been silent for 180 seconds, then takes over.

    Args:
      body: The host's instance, version, and facts when they changed.
      request: Request carrying the database engine.
      machine: The machine the credential authenticated as.

    Returns:
      beat: The server's clock at the heartbeat.

    Raises:
      AnotherHostConnectedError: Another instance is connected and live.
      HTTPException: 410 when the credential was revoked after it authenticated.

    """
    async with engine_of(request).acquire() as conn, tx(conn):
        # Lock first and check second, as separate statements: a revoke in flight
        # holds this row until it commits, and only a statement that starts after the
        # lock sees its commit. The write then cannot land after the revoke cleared
        # the host.
        await conn.fetchval(
            "SELECT id FROM machines WHERE id = $1 FOR UPDATE",
            machine.machine_id,
        )
        live = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM machine_credentials "
            "WHERE id = $1 AND revoked_at IS NULL)",
            machine.credential_id,
        )
        if not live:
            raise machine_revoked()
        beat = await conn.fetchval(
            "UPDATE machines SET host_instance = $2, last_heartbeat = now(), "
            "host_version = $3, facts = COALESCE($4, facts) "
            "WHERE id = $1 AND (host_instance IS NULL OR host_instance = $2 "
            "OR last_heartbeat IS NULL "
            "OR last_heartbeat < now() - make_interval(secs => $5)) "
            "RETURNING last_heartbeat",
            machine.machine_id,
            body.instance,
            body.host_version,
            body.facts,
            OFFLINE_AFTER_SEC,
        )
    if beat is None:
        raise AnotherHostConnectedError("another host is connected")
    return HeartbeatResponse(server_time=from_plain(beat, datetime))


@router.post(REVOKE_PATH, status_code=204)
async def revoke_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Take a machine out of service; revoking one already revoked is a no-op.

    The machine's credential row is kept, so its host learns of the revoke by a 410
    on its next request. An enrollment token not yet used is closed too, so a revoke
    is not undone by a host that joins with it.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      identity: Admin principal.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: No such machine.

    """
    del identity
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn, tx(conn):
        machine_id = await conn.fetchval(
            "SELECT id FROM machines WHERE name = $1 FOR UPDATE",
            name,
        )
        if machine_id is None:
            raise absent_machine(name)
        await _revoke_credentials(conn, from_plain(machine_id, UUID))
        await conn.execute(
            "UPDATE machine_enrollments SET used_at = now() "
            "WHERE machine_id = $1 AND used_at IS NULL",
            machine_id,
        )
        await conn.execute(
            "UPDATE machines SET host_instance = NULL WHERE id = $1",
            machine_id,
        )
    return Response(status_code=204)


async def _token_is_usable(conn: Conn, *, parsed: ParsedSecret, name: str) -> bool:
    """Return True when the token is open and matches, read without any lock."""
    stored = await conn.fetchval(
        "SELECT e.secret_sha256 FROM machine_enrollments e "
        "JOIN machines m ON m.id = e.machine_id "
        "WHERE e.id = $1 AND m.name = $2 AND e.used_at IS NULL "
        "AND e.expires_at > now() "
        "AND EXISTS (SELECT 1 FROM users u WHERE u.email = e.created_by "
        "AND u.status = 'active' AND u.role = 'admin')",
        parsed.id,
        name,
    )
    return stored is not None and secrets.compare_digest(
        from_plain(stored, bytes),
        parsed.digest,
    )


async def _revoke_credentials(conn: Conn, machine_id: UUID) -> None:
    """Revoke the machine's live credential, if it has one."""
    await conn.execute(
        "UPDATE machine_credentials SET revoked_at = now() "
        "WHERE machine_id = $1 AND revoked_at IS NULL",
        machine_id,
    )


def _refused() -> HTTPException:
    """Return the one 401 every unusable enrollment token gets."""
    return HTTPException(status_code=401, detail="invalid enrollment token")
