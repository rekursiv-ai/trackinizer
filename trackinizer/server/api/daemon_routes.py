"""Routes a daemon uses to enroll, join and stay connected, and the revoke route.

An admin enrolls a host by name and hands the daemon a one-use, 15-minute token.
A writer may enroll a host of their own the same way: it is theirs (``owner``) and
personal, so only they and admins see it. A remote agent service the configuration
names enrolls so under its service account, and its host is confirmed at once. The daemon joins with the token, once, and
receives a host credential. It then heartbeats with that credential, and its
host reads online until it has been silent for 180 seconds or has released it.
An admin, or the owner, revokes the host; the credential's row is kept, so every
daemon route then answers it 410 instead of 401.
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
    get_remote_agent_services,
    get_session_relay,
    get_store,
    get_terminal_relay,
    get_tool_relay,
    hosts_changed,
)
from trackinizer.server.api._routes_shared import request_engine
from trackinizer.server.api.hosts_routes import (
    HostName,
    absent_host,
    refuse_reserved,
)
from trackinizer.server.api.session_access import resolve_asker
from trackinizer.server.auth import (
    AuthIdentity,
    require_role,
    revoke_host_process_keys,
)
from trackinizer.server.host_auth import (
    HostIdentity,
    MintedSecret,
    ParsedSecret,
    current_host,
    host_revoked,
    mint_secret,
    parse_enrollment,
)
from trackinizer.server.notify import tx
from trackinizer.types.errors import ConflictError
from trackinizer.wire.wire_daemon import (
    CREDENTIAL_PREFIX,
    ENROLL_PATH,
    ENROLL_TTL_SEC,
    ENROLLMENT_PREFIX,
    HEARTBEAT_PATH,
    JOIN_PATH,
    MIN_DAEMON_VERSION,
    OFFLINE_AFTER_SEC,
    REVOKE_PATH,
    TOLERANT_DAEMON_VERSION,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JoinRequest,
    JoinResponse,
    older_than,
)
from trackinizer.wire.wire_hosts import PERSONAL_ROLE


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "AnotherDaemonConnectedError",
    "HostOwnedError",
    "claim_host",
    "enroll_host_route",
    "heartbeat_host_route",
    "issue_enrollment",
    "join_host_route",
    "revoke_host",
    "revoke_host_route",
    "router",
    "unregister_host",
]


router = APIRouter()


class AnotherDaemonConnectedError(ConflictError):
    """A different daemon instance heartbeat while the connected one is still live."""

    code = "another_daemon_connected"


class HostOwnedError(ConflictError):
    """The host name is taken by a host another person owns."""

    code = "host_owned"


@router.post(ENROLL_PATH, response_model=EnrollResponse, status_code=201)
async def enroll_host_route(
    body: EnrollRequest,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> EnrollResponse:
    """Register the host if it is new and issue a one-use enrollment token.

    A new token supersedes any open one for the same host. An admin enrolls a
    host that is new, unowned or already theirs, and confirms it; one another person
    owns answers 409 ``host_owned``, naming the owner. A writer may enroll a host
    that is new, which becomes theirs with the role ``personal`` and stays unconfirmed
    until they confirm it in the web app, or one that is already theirs. A remote
    agent service the configuration names, by its name and its account, is confirmed
    as it enrolls: a service account has no web app to confirm it in.

    The configured assistant may name an ``asker``, a writer or admin it answers. The
    host is then the asker's: personal, confirmed, and theirs to open a terminal on,
    and the token joins only for them. The assistant owns nothing it enrolls.

    Args:
      body: The host's name, and the asker when the assistant enrolls for one.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.
      identity: Writer principal, recorded as who enrolled the host.

    Returns:
      enrolled: The token, shown once, when it expires, and the host's owner.

    Raises:
      ValidationError: The name is reserved.
      HostOwnedError: An admin names a host another person owns.
      HTTPException: 403 when a writer names a host that is not theirs, or anyone
        but the assistant names an asker, or the asker is no active writer.

    """
    refuse_reserved(body.name)
    asker = await _enrolled_asker(request, identity, asker=body.asker)
    who = identity.email if asker is None else asker
    admin = asker is None and identity.role == "admin"
    service = any(
        (each.name, each.account) == (body.name, who)
        for each in get_remote_agent_services(request)
    )
    async with request_engine(request).acquire() as conn, tx(conn):
        host_id = await claim_host(
            conn,
            name=body.name,
            email=who,
            admin=admin,
            confirmed=asker is not None or admin or service,
        )
        minted, expires_at = await issue_enrollment(
            conn,
            host_id=host_id,
            created_by=who,
        )
    hosts_changed(request, owner=who)
    response.headers["Cache-Control"] = "no-store"
    return EnrollResponse(
        token=minted.token,
        expires_at=expires_at,
        owner=who,
    )


@router.post(JOIN_PATH, response_model=JoinResponse, status_code=201)
async def join_host_route(
    body: JoinRequest,
    request: Request,
    response: Response,
) -> JoinResponse:
    """Exchange an enrollment token for a host credential.

    The token is the credential, so the route has no other authentication. It is
    valid once, within 15 minutes, for the host name it was issued for, and only
    while the account that issued it is still an active admin, or owns the host and
    it is a personal one.
    Joining a host that already has a live credential revokes that credential first
    and closes the terminals open on it.

    Args:
      body: The host name, the token, and the daemon's instance, version and facts.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.

    Returns:
      joined: The host's id and its credential, shown once.

    Raises:
      HTTPException: 401 when the token is malformed, unknown, used, expired, issued
        for another host, or issued by an account that is no longer an active admin.

    """
    parsed = parse_enrollment(body.token)
    if parsed is None:
        raise _refused()
    async with request_engine(request).acquire() as conn:
        # Refused by a plain read, before any lock. Join has no authentication, and
        # the host row lock below queues behind that host's heartbeats and admin
        # writes, so a junk token naming a real host would stall all of them.
        if not await _token_is_usable(conn, parsed=parsed, name=body.name):
            raise _refused()
        minted = mint_secret(CREDENTIAL_PREFIX)
        async with tx(conn):
            # The host row first, as in every other route that writes a host: a
            # join that claimed the enrollment first would deadlock with an enroll,
            # revoke or delete holding the host and wanting the enrollment.
            host = await conn.fetchrow(
                "SELECT id, owner FROM hosts WHERE name = $1 FOR UPDATE",
                body.name,
            )
            if host is None:
                raise _refused()
            host_id = host["id"]
            # The claim checks everything again, because the read above held no lock:
            # a concurrent join or revoke may have used the token since, and two joins
            # that both passed the read would otherwise each get a credential.
            claimed = await conn.fetchval(
                "UPDATE host_enrollments SET used_at = now() "
                "WHERE id = $1 AND used_at IS NULL AND expires_at > now() "
                "AND secret_sha256 = $2 AND host_id = $3 "
                "AND EXISTS (SELECT 1 FROM users u JOIN hosts m ON m.id = $3 "
                "WHERE u.email = host_enrollments.created_by "
                "AND u.status = 'active' AND ((u.role = 'admin' "
                "AND (m.owner IS NULL OR m.owner = u.email)) "
                "OR (m.owner = u.email AND m.role = $4))) "
                "RETURNING id",
                parsed.id,
                parsed.digest,
                host_id,
                PERSONAL_ROLE,
            )
            if claimed is None:
                raise _refused()
            await _revoke_credentials(conn, from_plain(host_id, UUID))
            await conn.execute(
                "INSERT INTO host_credentials (id, host_id, secret_sha256) "
                "VALUES ($1, $2, $3)",
                minted.id,
                host_id,
                minted.digest,
            )
            await conn.execute(
                "UPDATE hosts SET facts = $2, daemon_instance = $3, "
                "daemon_version = $4, last_heartbeat = now() WHERE id = $1",
                host_id,
                body.facts,
                body.instance,
                body.daemon_version,
            )
    # First, as nothing after the commit may keep a follower from hearing of it.
    hosts_changed(request, owner=from_plain(host["owner"], str, default=None))
    get_tool_relay(request).close_host(from_plain(host_id, UUID))
    await get_terminal_relay(request).close_host(from_plain(host_id, UUID))
    response.headers["Cache-Control"] = "no-store"
    return JoinResponse(
        host_id=from_plain(host_id, UUID),
        credential=minted.token,
    )


# A daemon before ``TOLERANT_DAEMON_VERSION`` refuses an answer naming a field it does not
# know, so a false ``daemon_outdated`` is left out of the answer, and only a daemon that
# reads it is told: an older one hears of it only from its owner's host list.
@router.post(
    HEARTBEAT_PATH,
    response_model=HeartbeatResponse,
    response_model_exclude_defaults=True,
)
async def heartbeat_host_route(
    body: HeartbeatRequest,
    request: Request,
    host: Annotated[HostIdentity, Depends(current_host)],
) -> HeartbeatResponse:
    """Record that the daemon is alive, and what it reports about itself.

    One daemon instance holds a host at a time. Under ``keep`` a different instance
    is refused until the connected one has been silent for 180 seconds, then takes
    over; under ``take`` it takes over at once. ``release`` from the instance that
    holds the host leaves it held by none, so it reads offline; from any other
    instance it changes nothing.

    Args:
      body: The daemon's instance, version, claim, and facts when they changed.
      request: Request carrying the database engine.
      host: The host the credential authenticated as.

    Returns:
      beat: The server's clock at the heartbeat, and ``daemon_outdated`` for a daemon older
        than ``MIN_DAEMON_VERSION`` that can read it.

    Raises:
      AnotherDaemonConnectedError: Another instance is connected and live.
      HTTPException: 410 when the credential was revoked after it authenticated.

    """
    async with request_engine(request).acquire() as conn, tx(conn):
        # Lock first and check second, as separate statements: a revoke in flight
        # holds this row until it commits, and only a statement that starts after the
        # lock sees its commit. The write then cannot land after the revoke cleared
        # the daemon.
        owner = from_plain(
            await conn.fetchval(
                "SELECT owner FROM hosts WHERE id = $1 FOR UPDATE",
                host.host_id,
            ),
            str,
            default=None,
        )
        live = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM host_credentials "
            "WHERE id = $1 AND revoked_at IS NULL)",
            host.credential_id,
        )
        if not live:
            raise host_revoked()
        if body.claim == "release":
            # Matches the row whoever holds it, so the answer always has a clock.
            beat = await conn.fetchval(
                "UPDATE hosts SET daemon_instance = CASE WHEN daemon_instance = $2 "
                "THEN NULL ELSE daemon_instance END WHERE id = $1 RETURNING now()",
                host.host_id,
                body.instance,
            )
        else:
            beat = await conn.fetchval(
                "UPDATE hosts SET daemon_instance = $2, last_heartbeat = now(), "
                "daemon_version = $3, facts = COALESCE($4, facts) "
                "WHERE id = $1 AND ($6::boolean OR daemon_instance IS NULL "
                "OR daemon_instance = $2 "
                "OR last_heartbeat IS NULL "
                "OR last_heartbeat < now() - make_interval(secs => $5)) "
                "RETURNING last_heartbeat",
                host.host_id,
                body.instance,
                body.daemon_version,
                body.facts,
                OFFLINE_AFTER_SEC,
                body.claim == "take",
            )
    if body.claim == "release":
        hosts_changed(request, owner=owner)
        return HeartbeatResponse(server_time=from_plain(beat, datetime))
    if beat is None:
        raise AnotherDaemonConnectedError("another daemon is connected")
    # A heartbeat says its facts only when they changed; the rest of it moves only
    # whether the host reads online, which is its link's to find, not the listing's.
    if body.facts is not None:
        hosts_changed(request, owner=owner)
    return HeartbeatResponse(
        server_time=from_plain(beat, datetime),
        daemon_outdated=older_than(body.daemon_version, oldest=MIN_DAEMON_VERSION)
        and not older_than(body.daemon_version, oldest=TOLERANT_DAEMON_VERSION),
    )


@router.post(REVOKE_PATH, status_code=204)
async def revoke_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> Response:
    """Take a host out of service; revoking one already revoked is a no-op.

    An admin may revoke any host, and a writer the hosts they own. The host's
    credential row is kept, so its daemon learns of the revoke by a 410 on its next
    request, and the terminals open on it are closed with 4410. An enrollment token not
    yet used is closed too, so a revoke is not undone by a daemon that joins with it.

    Args:
      name: The host's name.
      request: Request carrying the database engine.
      identity: Admin principal, or the host's owner.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: No such host.
      HTTPException: 403 when a writer names a host that is not theirs.

    """
    _ = await revoke_host(request, name=name, identity=identity)
    return Response(status_code=204)


async def revoke_host(
    request: Request,
    *,
    name: str,
    identity: AuthIdentity,
) -> list[UUID]:
    """Revoke a host as the revoke route does, and return what it stopped there.

    Args:
      request: Request carrying the database engine and the relays.
      name: The host's name.
      identity: Admin principal, or the host's owner.

    Returns:
      session_ids: The sessions whose agent process it stopped.

    Raises:
      NotFoundError: No such host.
      HTTPException: 403 when a writer names a host that is not theirs.

    """
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn, tx(conn):
        host = await conn.fetchrow(
            "SELECT id, owner FROM hosts WHERE name = $1 FOR UPDATE",
            name,
        )
        if host is None:
            raise absent_host(name)
        owner = from_plain(host["owner"], str, default=None)
        if identity.role != "admin" and owner != identity.email:
            raise HTTPException(
                status_code=403,
                detail="host belongs to another user",
            )
        host_id = from_plain(host["id"], UUID)
        revoked = await _out_of_service(conn, host_id)
    store = get_store(request)
    for key_id in revoked:
        store.forget_bearer_identity_for_key(key_id)
    return await _closed(request, host_id=host_id, owner=owner)


async def unregister_host(conn: Conn, *, name: str, owner: str) -> UUID | None:
    """Revoke and delete ``owner``'s host ``name`` in the caller's transaction.

    For the server's own caller that made the host for its owner and has ended the
    daemon it served, such as a KnowHost deleted: the name is free again and no
    listing shows it. The caller tells the listing and closes the host's tool connections once
    the transaction commits.

    Args:
      conn: A connection inside a transaction.
      name: The host's name.
      owner: Whose host it must be.

    Returns:
      host_id: The host's id; ``None`` when it is gone or another person's.

    """
    host = await conn.fetchrow(
        "SELECT id FROM hosts WHERE name = $1 AND owner = $2 FOR UPDATE",
        name,
        owner,
    )
    if host is None:
        return None
    host_id = from_plain(host["id"], UUID)
    # Its callers hold no store to forget the processes' keys in, so each stays
    # verified in this server's cache until its entry expires.
    _ = await _out_of_service(conn, host_id)
    await conn.execute("DELETE FROM hosts WHERE id = $1", host_id)
    return host_id


async def claim_host(
    conn: Conn,
    *,
    name: str,
    email: str,
    admin: bool,
    confirmed: bool | None = None,
) -> UUID:
    """Register the host if it is new, or find it, for ``email`` to enroll.

    An admin takes a host that is new, unowned or already theirs, registers a new
    one with no role, and confirms it. Anyone else registers a new one as their own
    ``personal`` host, and may enroll only one that is already theirs. It is
    confirmed when ``confirmed`` is true, which defaults to ``admin``: a person who has
    seen the host's name approve it, the assistant enrolling for its asker, or a
    remote agent service the configuration names.
    The caller opens the transaction, so a refusal keeps nothing.

    Args:
      conn: A connection inside a transaction.
      name: The host's name; a reserved name is the caller's to refuse.
      email: Who enrolls the host, and owns it when it is new.
      admin: Whether ``email`` enrolls as an admin.
      confirmed: Whether the host is confirmed now; ``None`` means ``admin``.

    Returns:
      host_id: The host's id.

    Raises:
      HostOwnedError: An admin names a host another person owns.
      HTTPException: 403 when a non-admin names a host that is not theirs.

    """
    # One statement registers the host or, through the no-op update, locks its
    # existing row. A DO NOTHING insert followed by a separate lock leaves a gap in
    # which a delete commits: the lock then finds no row, the enrollment is
    # inserted with a NULL host, and the route answers 500. The lock also
    # serialises two enrollments of one host, so the later one always
    # supersedes the earlier.
    host = await conn.fetchrow(
        "INSERT INTO hosts (name, updated_by, owner, role, confirmed_at) "
        "VALUES ($1, $2, $2, $3, CASE WHEN $4 THEN now() END) "
        "ON CONFLICT (name) DO UPDATE SET "
        "owner = CASE WHEN $4 AND $3 = '' "
        "THEN COALESCE(hosts.owner, EXCLUDED.owner) "
        "ELSE hosts.owner END, "
        "confirmed_at = CASE WHEN $4 THEN COALESCE(hosts.confirmed_at, now()) "
        "ELSE hosts.confirmed_at END RETURNING id, owner",
        name,
        email,
        "" if admin else PERSONAL_ROLE,
        admin if confirmed is None else confirmed,
    )
    if host is None:
        raise ValueError("Expected host is not None.")
    owner = from_plain(host["owner"], str | None)
    if owner != email:
        # Nobody adopts a name another person holds: the host under it, and any
        # shell opened on it, stay that person's. Raised inside the transaction, so
        # nothing is kept.
        if admin:
            raise HostOwnedError(
                f"host {name!r} is owned by {owner}; "
                "choose another name, or have them revoke it first",
            )
        raise HTTPException(
            status_code=403,
            detail="host belongs to another user",
        )
    return from_plain(host["id"], UUID)


async def issue_enrollment(
    conn: Conn,
    *,
    host_id: UUID,
    created_by: str,
) -> tuple[MintedSecret, datetime]:
    """Issue a one-use enrollment token for the host, superseding any open one.

    Args:
      conn: A connection inside a transaction.
      host_id: The host the token lets a daemon join as.
      created_by: Who enrolls it; the join checks they still may.

    Returns:
      issued: The token, shown once, and when it stops working.

    """
    minted = mint_secret(ENROLLMENT_PREFIX)
    await conn.execute(
        "UPDATE host_enrollments SET used_at = now() "
        "WHERE host_id = $1 AND used_at IS NULL",
        host_id,
    )
    expires_at = await conn.fetchval(
        "INSERT INTO host_enrollments "
        "(id, host_id, secret_sha256, created_by, expires_at) "
        "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) "
        "RETURNING expires_at",
        minted.id,
        host_id,
        minted.digest,
        created_by,
        ENROLL_TTL_SEC,
    )
    return minted, from_plain(expires_at, datetime)


# The assistant is trusted to say whom it answers, as for a chat's settings, and the
# server reads that person's role itself. Anyone else naming an asker is refused, not
# ignored: a host enrolled for the wrong person would be theirs to open a shell on.
async def _enrolled_asker(
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
    """Return the one 403 of every asker that may not have a host enrolled."""
    return HTTPException(status_code=403, detail="asker not accepted")


async def _token_is_usable(conn: Conn, *, parsed: ParsedSecret, name: str) -> bool:
    """Return True when the token is open and matches, read without any lock."""
    stored = await conn.fetchval(
        "SELECT e.secret_sha256 FROM host_enrollments e "
        "JOIN hosts m ON m.id = e.host_id "
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


# An enrollment token not yet used is closed too, so the revoke is not undone by a daemon
# that joins with it. The keys minted for its agent processes go with it, as a process
# dies with its host; a host that joins again keeps them, since its processes outlive
# their daemon. Their ids are returned: the verified-bearer cache would honour each for
# up to a minute more, so the caller forgets them once the transaction commits.
async def _out_of_service(conn: Conn, host_id: UUID) -> list[UUID]:
    """Revoke the host's credential and its processes' keys, close its open enrollment, and free its daemon."""
    await _revoke_credentials(conn, host_id)
    revoked = await revoke_host_process_keys(conn, host_id=host_id)
    await conn.execute(
        "UPDATE host_enrollments SET used_at = now() "
        "WHERE host_id = $1 AND used_at IS NULL",
        host_id,
    )
    await conn.execute(
        "UPDATE hosts SET daemon_instance = NULL WHERE id = $1",
        host_id,
    )
    return revoked


# First, as nothing after the commit may keep a follower from hearing of it.
async def _closed(
    request: Request,
    *,
    host_id: UUID,
    owner: str | None,
) -> list[UUID]:
    """Tell the owner's listing, end a revoked host's tool connections, terminals and processes; return the processes' sessions."""
    hosts_changed(request, owner=owner)
    get_tool_relay(request).close_host(host_id)
    await get_terminal_relay(request).close_host(host_id)
    return await get_session_relay(request).stop_host(host_id)


async def _revoke_credentials(conn: Conn, host_id: UUID) -> None:
    """Revoke the host's live credential, if it has one."""
    await conn.execute(
        "UPDATE host_credentials SET revoked_at = now() "
        "WHERE host_id = $1 AND revoked_at IS NULL",
        host_id,
    )


def _refused() -> HTTPException:
    """Return the one 401 every unusable enrollment token gets."""
    return HTTPException(status_code=401, detail="invalid enrollment token")
