"""Routes a machine host uses to enroll, join and stay connected, and the revoke route.

An admin enrolls a machine by name and hands the host a one-use, 15-minute token.
A writer may enroll a machine of their own the same way: it is theirs (``owner``) and
personal, so only they and admins see it. The host joins with the token, once, and
receives a machine credential. It then heartbeats with that credential, and its
machine reads online until it has been silent for 180 seconds or has released it.
An admin, or the owner, revokes the machine; the credential's row is kept, so every
host route then answers it 410 instead of 401.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import (
    auth_is_disabled,
    get_exec_hub,
    get_terminal_hub,
)
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.machines_routes import (
    MachineName,
    absent_machine,
    refuse_reserved,
)
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.machine_auth import (
    MachineIdentity,
    MintedSecret,
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
    MIN_HOST_VERSION,
    OFFLINE_AFTER_SEC,
    REVOKE_PATH,
    TOLERANT_HOST_VERSION,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JoinRequest,
    JoinResponse,
    older_than,
)
from trackinizer.wire.wire_machines import PERSONAL_ROLE


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "AnotherHostConnectedError",
    "MachineOwnedError",
    "claim_machine",
    "enroll_machine_route",
    "heartbeat_machine_route",
    "issue_enrollment",
    "join_machine_route",
    "revoke_machine",
    "revoke_machine_route",
    "router",
]


router = APIRouter()


class AnotherHostConnectedError(ConflictError):
    """A different host instance heartbeat while the connected one is still live."""

    code = "another_host_connected"


class MachineOwnedError(ConflictError):
    """The machine name is taken by a machine another person owns."""

    code = "machine_owned"


@router.post(ENROLL_PATH, response_model=EnrollResponse, status_code=201)
async def enroll_machine_route(
    body: EnrollRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> EnrollResponse:
    """Register the machine if it is new and issue a one-use enrollment token.

    A new token supersedes any open one for the same machine. An admin enrolls a
    machine that is new, unowned or already theirs, and confirms it; one another person
    owns answers 409 ``machine_owned``, naming the owner. A writer may enroll a machine
    that is new, which becomes theirs with the role ``personal`` and stays unconfirmed
    until they confirm it in the web app, or one that is already theirs.

    The configured assistant may name an ``asker``, a writer or admin it answers. The
    machine is then the asker's: personal, confirmed, and theirs to open a terminal on,
    and the token joins only for them. The assistant owns nothing it enrolls.

    Args:
      body: The machine's name, and the asker when the assistant enrolls for one.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.
      identity: Writer principal, recorded as who enrolled the machine.

    Returns:
      enrolled: The token, shown once, when it expires, and the machine's owner.

    Raises:
      ValidationError: The name is reserved.
      MachineOwnedError: An admin names a machine another person owns.
      HTTPException: 403 when a writer names a machine that is not theirs, or anyone
        but the assistant names an asker, or the asker is no active writer.

    """
    refuse_reserved(body.name)
    asker = await _asker_of(request, identity, asker=body.asker)
    who = identity.email if asker is None else asker
    admin = asker is None and identity.role == "admin"
    async with engine_of(request).acquire() as conn, tx(conn):
        machine_id = await claim_machine(
            conn,
            name=body.name,
            email=who,
            admin=admin,
            confirmed=asker is not None or admin,
        )
        minted, expires_at = await issue_enrollment(
            conn,
            machine_id=machine_id,
            created_by=who,
        )
    response.headers["Cache-Control"] = "no-store"
    return EnrollResponse(
        token=minted.token,
        expires_at=expires_at,
        owner=who,
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
    while the account that issued it is still an active admin, or owns the machine and
    it is a personal one.
    Joining a machine that already has a live credential revokes that credential first
    and closes the terminals open on it.

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
                "AND EXISTS (SELECT 1 FROM users u JOIN machines m ON m.id = $3 "
                "WHERE u.email = machine_enrollments.created_by "
                "AND u.status = 'active' AND ((u.role = 'admin' "
                "AND (m.owner IS NULL OR m.owner = u.email)) "
                "OR (m.owner = u.email AND m.role = $4))) "
                "RETURNING id",
                parsed.id,
                parsed.digest,
                machine_id,
                PERSONAL_ROLE,
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
    get_exec_hub(request).close_machine(from_plain(machine_id, UUID))
    await get_terminal_hub(request).close_machine(from_plain(machine_id, UUID))
    response.headers["Cache-Control"] = "no-store"
    return JoinResponse(
        machine_id=from_plain(machine_id, UUID),
        credential=minted.token,
    )


# A host before ``TOLERANT_HOST_VERSION`` refuses an answer naming a field it does not
# know, so a false ``host_outdated`` is left out of the answer, and only a host that
# reads it is told: an older one hears of it only from its owner's machine list.
@router.post(
    HEARTBEAT_PATH,
    response_model=HeartbeatResponse,
    response_model_exclude_defaults=True,
)
async def heartbeat_machine_route(
    body: HeartbeatRequest,
    request: Request,
    machine: Annotated[MachineIdentity, Depends(current_machine)],
) -> HeartbeatResponse:
    """Record that the host is alive, and what it reports about itself.

    One host instance holds a machine at a time. Under ``keep`` a different instance
    is refused until the connected one has been silent for 180 seconds, then takes
    over; under ``take`` it takes over at once. ``release`` from the instance that
    holds the machine leaves it held by none, so it reads offline; from any other
    instance it changes nothing.

    Args:
      body: The host's instance, version, claim, and facts when they changed.
      request: Request carrying the database engine.
      machine: The machine the credential authenticated as.

    Returns:
      beat: The server's clock at the heartbeat, and ``host_outdated`` for a host older
        than ``MIN_HOST_VERSION`` that can read it.

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
        if body.claim == "release":
            # Matches the row whoever holds it, so the answer always has a clock.
            beat = await conn.fetchval(
                "UPDATE machines SET host_instance = CASE WHEN host_instance = $2 "
                "THEN NULL ELSE host_instance END WHERE id = $1 RETURNING now()",
                machine.machine_id,
                body.instance,
            )
            return HeartbeatResponse(server_time=from_plain(beat, datetime))
        beat = await conn.fetchval(
            "UPDATE machines SET host_instance = $2, last_heartbeat = now(), "
            "host_version = $3, facts = COALESCE($4, facts) "
            "WHERE id = $1 AND ($6::boolean OR host_instance IS NULL "
            "OR host_instance = $2 "
            "OR last_heartbeat IS NULL "
            "OR last_heartbeat < now() - make_interval(secs => $5)) "
            "RETURNING last_heartbeat",
            machine.machine_id,
            body.instance,
            body.host_version,
            body.facts,
            OFFLINE_AFTER_SEC,
            body.claim == "take",
        )
    if beat is None:
        raise AnotherHostConnectedError("another host is connected")
    return HeartbeatResponse(
        server_time=from_plain(beat, datetime),
        host_outdated=older_than(body.host_version, oldest=MIN_HOST_VERSION)
        and not older_than(body.host_version, oldest=TOLERANT_HOST_VERSION),
    )


@router.post(REVOKE_PATH, status_code=204)
async def revoke_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Take a machine out of service; revoking one already revoked is a no-op.

    An admin may revoke any machine, and a writer the machines they own. The machine's
    credential row is kept, so its host learns of the revoke by a 410 on its next
    request, and the terminals open on it are closed with 4410. An enrollment token not
    yet used is closed too, so a revoke is not undone by a host that joins with it.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      identity: Admin principal, or the machine's owner.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: No such machine.
      HTTPException: 403 when a writer names a machine that is not theirs.

    """
    _ = await revoke_machine(request, name=name, identity=identity)
    return Response(status_code=204)


async def revoke_machine(
    request: Request,
    *,
    name: str,
    identity: AuthIdentity,
) -> str | None:
    """Revoke a machine as the revoke route does, and return its owner.

    Args:
      request: Request carrying the database engine and the hubs.
      name: The machine's name.
      identity: Admin principal, or the machine's owner.

    Returns:
      owner: The machine's owner; ``None`` for a machine no person owns.

    Raises:
      NotFoundError: No such machine.
      HTTPException: 403 when a writer names a machine that is not theirs.

    """
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn, tx(conn):
        machine = await conn.fetchrow(
            "SELECT id, owner FROM machines WHERE name = $1 FOR UPDATE",
            name,
        )
        if machine is None:
            raise absent_machine(name)
        owner = from_plain(machine["owner"], str, default=None)
        if identity.role != "admin" and owner != identity.email:
            raise HTTPException(
                status_code=403,
                detail="machine belongs to another user",
            )
        machine_id = machine["id"]
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
    get_exec_hub(request).close_machine(from_plain(machine_id, UUID))
    await get_terminal_hub(request).close_machine(from_plain(machine_id, UUID))
    return owner


async def claim_machine(
    conn: Conn,
    *,
    name: str,
    email: str,
    admin: bool,
    confirmed: bool | None = None,
) -> UUID:
    """Register the machine if it is new, or find it, for ``email`` to enroll.

    An admin takes a machine that is new, unowned or already theirs, registers a new
    one with no role, and confirms it. Anyone else registers a new one as their own
    ``personal`` machine, and may enroll only one that is already theirs. It is
    confirmed when ``confirmed`` is true, which defaults to ``admin``: a person who has
    seen the machine's name approve it, or the assistant enrolling for its asker.
    The caller opens the transaction, so a refusal keeps nothing.

    Args:
      conn: A connection inside a transaction.
      name: The machine's name; a reserved name is the caller's to refuse.
      email: Who enrolls the machine, and owns it when it is new.
      admin: Whether ``email`` enrolls as an admin.
      confirmed: Whether the machine is confirmed now; ``None`` means ``admin``.

    Returns:
      machine_id: The machine's id.

    Raises:
      MachineOwnedError: An admin names a machine another person owns.
      HTTPException: 403 when a non-admin names a machine that is not theirs.

    """
    # One statement registers the machine or, through the no-op update, locks its
    # existing row. A DO NOTHING insert followed by a separate lock leaves a gap in
    # which a delete commits: the lock then finds no row, the enrollment is
    # inserted with a NULL machine, and the route answers 500. The lock also
    # serialises two enrollments of one machine, so the later one always
    # supersedes the earlier.
    machine = await conn.fetchrow(
        "INSERT INTO machines (name, updated_by, owner, role, confirmed_at) "
        "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END) "
        "ON CONFLICT (name) DO UPDATE SET "
        "owner = CASE WHEN $4 AND $3 = '' "
        "THEN COALESCE(machines.owner, EXCLUDED.owner) "
        "ELSE machines.owner END, "
        "confirmed_at = CASE WHEN $4 THEN COALESCE(machines.confirmed_at, now()) "
        "ELSE machines.confirmed_at END RETURNING id, owner",
        name,
        email,
        "" if admin else PERSONAL_ROLE,
        admin if confirmed is None else confirmed,
    )
    if machine is None:
        raise ValueError("Expected machine is not None.")
    owner = from_plain(machine["owner"], str | None)
    if owner != email:
        # Nobody adopts a name another person holds: the machine under it, and any
        # shell opened on it, stay that person's. Raised inside the transaction, so
        # nothing is kept.
        if admin:
            raise MachineOwnedError(
                f"machine {name!r} is owned by {owner}; "
                "choose another name, or have them revoke it first",
            )
        raise HTTPException(
            status_code=403,
            detail="machine belongs to another user",
        )
    return from_plain(machine["id"], UUID)


async def issue_enrollment(
    conn: Conn,
    *,
    machine_id: UUID,
    created_by: str,
) -> tuple[MintedSecret, datetime]:
    """Issue a one-use enrollment token for the machine, superseding any open one.

    Args:
      conn: A connection inside a transaction.
      machine_id: The machine the token lets a host join as.
      created_by: Who enrolls it; the join checks they still may.

    Returns:
      issued: The token, shown once, and when it stops working.

    """
    minted = mint_secret(ENROLLMENT_PREFIX)
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
        created_by,
        ENROLL_TTL_SEC,
    )
    return minted, from_plain(expires_at, datetime)


# The assistant is trusted to say whom it answers, as for a chat's settings, and the
# server reads that person's role itself. Anyone else naming an asker is refused, not
# ignored: a machine enrolled for the wrong person would be theirs to open a shell on.
async def _asker_of(
    request: Request,
    identity: AuthIdentity,
    *,
    asker: str | None,
) -> str | None:
    """Return the account the assistant enrolls for, or ``None`` when it names none."""
    if asker is None:
        return None
    if auth_is_disabled(request):
        raise _no_asker()
    found = await resolve_asker(request, identity, email=asker)
    if found is None or found.role not in {"writer", "admin"}:
        raise _no_asker()
    return found.email


def _no_asker() -> HTTPException:
    """Return the one 403 of every asker that may not have a machine enrolled."""
    return HTTPException(status_code=403, detail="asker not accepted")


async def _token_is_usable(conn: Conn, *, parsed: ParsedSecret, name: str) -> bool:
    """Return True when the token is open and matches, read without any lock."""
    stored = await conn.fetchval(
        "SELECT e.secret_sha256 FROM machine_enrollments e "
        "JOIN machines m ON m.id = e.machine_id "
        "WHERE e.id = $1 AND m.name = $2 AND e.used_at IS NULL "
        "AND e.expires_at > now() "
        "AND EXISTS (SELECT 1 FROM users u WHERE u.email = e.created_by "
        "AND u.status = 'active' AND ((u.role = 'admin' "
        "AND (m.owner IS NULL OR m.owner = u.email)) "
        "OR (u.email = m.owner AND m.role = $3)))",
        parsed.id,
        name,
        PERSONAL_ROLE,
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
