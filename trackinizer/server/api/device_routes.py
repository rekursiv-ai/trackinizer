"""Device login: a program gets an API key by a code a signed-in person approves.

``POST /auth/device/code`` and ``POST /auth/device/token`` are public: a program with
no browser asks for a code, shows it, and polls. The page at ``GET /auth/device``
(``server/web.py``) is where the person signs in, checks who is asking and clicks. Its
three routes, lookup, approve and deny, answer a person's session cookie alone, from
strictly this server's own origin: an API key, a service account, a server with
authentication off or a foreign page is refused, since the click is the one thing
standing between a stranger's request and a key in the person's name.

An approval marks the row and, when the request named a machine, makes that machine
the approver's own confirmed personal machine. The key is minted by the first poll
after it, in the transaction that deletes the row, so the secret is shown once and
never stored, and a second poll finds nothing. Neither secret is stored: the row keeps
a SHA-256 of the poll secret and the eight letters the person checks.

The routes stay out of the OpenAPI schema, as the sign-in routes do: the web app is
typed against one committed schema, and the web deploy refuses a build whose schema
differs from the live server's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final
from uuid import UUID

import hashlib
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import auth_is_disabled
from trackinizer.server.api._origin import is_strictly_same_origin
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.api.device_limits import (
    device_code_requests,
    device_wrong_entries,
)
from trackinizer.server.api.machine_host_routes import (
    claim_machine,
    issue_enrollment,
)
from trackinizer.server.api.machines_routes import refuse_reserved
from trackinizer.server.auth import (
    AuthIdentity,
    create_api_key,
    current_user,
    effective_role,
)
from trackinizer.server.notify import tx
from trackinizer.wire.wire_device import (
    DEVICE_APPROVE_PATH,
    DEVICE_CODE_PATH,
    DEVICE_CODE_TTL_SEC,
    DEVICE_DENY_PATH,
    DEVICE_LOOKUP_PATH,
    DEVICE_PAGE_PATH,
    DEVICE_POLL_PATH,
    DEVICE_POLL_SEC,
    USER_CODE_ALPHABET,
    USER_CODE_LENGTH,
    DeviceCode,
    DeviceCodeRequest,
    DeviceDecision,
    DeviceLoginView,
    DeviceToken,
    DeviceTokenRequest,
    IssuedKey,
    format_user_code,
    normalize_user_code,
)
from trackinizer.wire.wire_service_accounts import is_service_address


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "device_approve_route",
    "device_code_route",
    "device_deny_route",
    "device_lookup_route",
    "device_token_route",
    "router",
]


router = APIRouter(include_in_schema=False)


@router.post(DEVICE_CODE_PATH, status_code=201)
async def device_code_route(
    body: DeviceCodeRequest,
    request: Request,
    response: Response,
) -> DeviceCode:
    """Open a login: return the code to show and the secret to poll with.

    Anyone may ask, so each address is limited to a few codes per ten minutes, and past
    a thousand open ones in all the answer is 429. Codes that expired are deleted first.
    The page's address is the public one the deployment names, since behind a proxy the
    request's own is not what a person can open.

    Args:
      body: The program, the computer it runs on, and the machine it asks to enroll.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it carries a secret.

    Returns:
      code: Both codes, where to enter the code, and how long and how often.

    Raises:
      ValidationError: The machine name is reserved.
      HTTPException: 403 ``device_auth_disabled`` on a server with authentication off,
        which needs no key; 429 ``device_rate_limited`` when this address asked for
        too many codes lately; 429 ``device_codes_busy`` when a thousand codes are open.

    """
    if auth_is_disabled(request):
        raise _refused(
            403,
            code="device_auth_disabled",
            message="this server has authentication off; no key is needed",
        )
    if not device_code_requests(request).allow(
        request.client.host if request.client else "",
    ):
        raise _refused(
            429,
            code="device_rate_limited",
            message="too many logins were started from this address; try again later",
        )
    if body.machine is not None:
        refuse_reserved(body.machine)
    device_code = secrets.token_urlsafe(32)
    user_code = "".join(
        secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH)
    )
    async with engine_of(request).acquire() as conn, tx(conn):
        await conn.execute("DELETE FROM device_logins WHERE expires_at <= now()")
        open_codes = await conn.fetchval("SELECT count(*) FROM device_logins")
        if from_plain(open_codes, int) >= _MAX_OPEN_CODES:
            raise _refused(
                429,
                code="device_codes_busy",
                message="too many logins are open; try again in a few minutes",
            )
        # A code that collides with an open one is 1 in 10^10 and answers 409, so the
        # program asks again.
        await conn.execute(
            "INSERT INTO device_logins "
            "(device_sha256, user_code, client, host, machine, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, now() + make_interval(secs => $6))",
            _digest(device_code),
            user_code,
            body.client,
            body.host,
            body.machine,
            DEVICE_CODE_TTL_SEC,
        )
    response.headers["Cache-Control"] = "no-store"
    page = f"{_public_base(request)}{DEVICE_PAGE_PATH}"
    shown = format_user_code(user_code)
    return DeviceCode(
        device_code=device_code,
        user_code=shown,
        verification_uri=page,
        verification_uri_complete=f"{page}?user_code={shown}",
        expires_in=DEVICE_CODE_TTL_SEC,
        interval=DEVICE_POLL_SEC,
    )


@router.post(DEVICE_POLL_PATH)
async def device_token_route(
    body: DeviceTokenRequest,
    request: Request,
    response: Response,
) -> DeviceToken:
    """Answer a poll: wait, slow down, denied, expired, or the key.

    An unknown or expired secret reads ``expired_token``, so nothing tells a guess from
    an old code. A poll sooner than the interval after the last reads ``slow_down``. The
    first poll after an approval mints the key and deletes the row in one transaction,
    so a second reads ``expired_token`` and no key is ever stored.

    Args:
      body: The poll secret.
      request: Request carrying the database engine.
      response: Response, marked ``no-store`` because it may carry a key.

    Returns:
      token: The status, with the key when approved.

    Raises:
      HTTPException: 409 ``device_machine_gone`` when the approved machine was deleted
        meanwhile; the code stays open and nothing is minted.

    """
    response.headers["Cache-Control"] = "no-store"
    async with engine_of(request).acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            "SELECT id, status, approver, client, host, machine, "
            "COALESCE(last_polled_at > now() - make_interval(secs => $2), false) "
            "AS too_soon FROM device_logins "
            "WHERE device_sha256 = $1 AND expires_at > now() FOR UPDATE",
            _digest(body.device_code),
            DEVICE_POLL_SEC,
        )
        if row is None:
            return DeviceToken(status="expired_token")
        login = from_plain(dict(row), _Login)
        await conn.execute(
            "UPDATE device_logins SET last_polled_at = now() WHERE id = $1",
            login.id,
        )
        if login.too_soon:
            return DeviceToken(status="slow_down")
        match login.status:
            case "pending":
                return DeviceToken(status="pending")
            case "denied":
                return DeviceToken(status="denied")
            case _:
                return await _issue(conn, login=login)


@router.post(DEVICE_LOOKUP_PATH)
async def device_lookup_route(
    body: DeviceDecision,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> DeviceLoginView:
    """Describe the open login a person entered, for the page to show before a click.

    A code that matches none counts as a wrong entry; five lock the person's entries
    out for ten minutes.

    Args:
      body: The code as typed.
      request: Request carrying the database engine.
      identity: The signed-in person.

    Returns:
      view: The account signed in, the program, its computer, the machine, and the
        role the key would hold.

    Raises:
      HTTPException: 403 ``device_forbidden`` for a key, a service account, a server
        with authentication off or a foreign origin; 404 ``device_code_unknown``;
        409 ``device_already_decided``; 429 ``device_locked``.

    """
    _require_person(request, identity)
    async with engine_of(request).acquire() as conn, tx(conn):
        login = await _open_login(request, conn, identity=identity, body=body)
    return DeviceLoginView(
        user_code=format_user_code(login.user_code),
        account=identity.email,
        client=login.client,
        host=login.host,
        machine=login.machine,
        role=effective_role(identity.role, "writer"),
    )


@router.post(DEVICE_APPROVE_PATH, status_code=204)
async def device_approve_route(
    body: DeviceDecision,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> None:
    """Approve the login: the next poll returns a key in the person's name.

    When the login named a machine, it becomes the person's own personal machine,
    already confirmed, and the key's reply carries the token its host joins with. A
    machine another person owns, or a viewer asking for any, is refused and the code
    stays open.

    Args:
      body: The code as typed.
      request: Request carrying the database engine.
      identity: The signed-in person.

    Raises:
      HTTPException: As ``lookup``, and 403 ``device_machine_needs_writer`` for a
        viewer and 403 for a machine that is not theirs.

    """
    _require_person(request, identity)
    async with engine_of(request).acquire() as conn, tx(conn):
        login = await _open_login(request, conn, identity=identity, body=body)
        if login.machine is not None:
            if identity.role == "viewer":
                raise _refused(
                    403,
                    code="device_machine_needs_writer",
                    message="enrolling a machine needs the writer role",
                )
            await claim_machine(
                conn,
                name=login.machine,
                email=identity.email,
                admin=False,
                # The person is at their browser and has seen the machine's name: this
                # is the confirmation a key could not give.
                confirmed=True,
            )
        await _decide(conn, login=login, status="approved", identity=identity)


@router.post(DEVICE_DENY_PATH, status_code=204)
async def device_deny_route(
    body: DeviceDecision,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> None:
    """Deny the login: the next poll reads ``denied`` and no key is ever minted.

    Args:
      body: The code as typed.
      request: Request carrying the database engine.
      identity: The signed-in person.

    Raises:
      HTTPException: As ``lookup``.

    """
    _require_person(request, identity)
    async with engine_of(request).acquire() as conn, tx(conn):
        login = await _open_login(request, conn, identity=identity, body=body)
        await _decide(conn, login=login, status="denied", identity=identity)


def _digest(device_code: str) -> bytes:
    """Return the SHA-256 the row keeps of a poll secret."""
    return hashlib.sha256(device_code.encode()).digest()


# Only a person's session decides. With authentication off every caller is a synthetic
# admin, and a rebound web page would pass the origin check. The lock comes last, so a
# refused caller learns nothing about the count. It counts the entry before the code is
# looked up and forgives it when the code is open, so a burst of guesses is counted too.
# A person who passes has one entry counted; ``_open_login`` forgives it on a hit.
def _require_person(request: Request, identity: AuthIdentity) -> None:
    """Refuse a key, a service account, a foreign origin and a locked-out person."""
    if (
        auth_is_disabled(request)
        or identity.api_key_id is not None
        or is_service_address(identity.email)
        or not is_strictly_same_origin(request)
    ):
        raise _refused(
            403,
            code="device_forbidden",
            message="a login is approved by a signed-in person in their own browser",
        )
    if not device_wrong_entries(request).reserve(identity.user_id):
        raise _refused(
            429,
            code="device_locked",
            message="too many wrong codes; try again in ten minutes",
        )


# A code that names none, or whose login expired, counts as a wrong entry.
async def _open_login(
    request: Request,
    conn: Conn,
    *,
    identity: AuthIdentity,
    body: DeviceDecision,
) -> _Login:
    """Lock and return the open login the typed code names, or refuse."""
    row = await conn.fetchrow(
        "SELECT id, user_code, client, host, machine, status FROM device_logins "
        "WHERE user_code = $1 AND expires_at > now() FOR UPDATE",
        normalize_user_code(body.user_code),
    )
    if row is None:
        raise _refused(
            404,
            code="device_code_unknown",
            message="that code is not open; check it, or start the login again",
        )
    device_wrong_entries(request).forgive(identity.user_id)
    login = from_plain(dict(row), _Login)
    if login.status != "pending":
        raise _refused(
            409,
            code="device_already_decided",
            message="this login was already decided",
        )
    return login


async def _decide(
    conn: Conn,
    *,
    login: _Login,
    status: str,
    identity: AuthIdentity,
) -> None:
    """Record who approved or denied the login."""
    await conn.execute(
        "UPDATE device_logins SET status = $2, approver = $3 WHERE id = $1",
        login.id,
        status,
        identity.user_id,
    )


async def _issue(conn: Conn, *, login: _Login) -> DeviceToken:
    """Mint the approved login's key, consume the row, and return both tokens."""
    approver = await conn.fetchrow(
        "SELECT email FROM users WHERE id = $1 AND status = 'active'",
        login.approver,
    )
    if approver is None:
        await conn.execute("DELETE FROM device_logins WHERE id = $1", login.id)
        return DeviceToken(status="expired_token")
    email = from_plain(approver["email"], str)
    machine_token = (
        ""
        if login.machine is None
        else await _enrollment_token(conn, machine=login.machine, email=email)
    )
    _, secret, _, role = await create_api_key(
        conn,
        user_id=from_plain(login.approver, UUID),
        name=f"{login.client}@{login.host}" if login.host else login.client,
        ceiling="writer",
    )
    await conn.execute("DELETE FROM device_logins WHERE id = $1", login.id)
    return DeviceToken(
        status="approved",
        issued=IssuedKey(
            api_key=secret,
            role=role,
            email=email,
            machine_token=machine_token,
        ),
    )


# The approval made the machine theirs; it is read again here because the token cannot
# be stored, and a machine deleted since is not enrolled again.
async def _enrollment_token(conn: Conn, *, machine: str, email: str) -> str:
    """Return a one-use token for the approver's machine, or refuse if it is gone."""
    machine_id = await conn.fetchval(
        "SELECT id FROM machines WHERE name = $1 AND owner = $2 FOR UPDATE",
        machine,
        email,
    )
    if machine_id is None:
        raise _refused(
            409,
            code="device_machine_gone",
            message="the machine was deleted after the approval; log in again",
        )
    minted, _ = await issue_enrollment(
        conn,
        machine_id=from_plain(machine_id, UUID),
        created_by=email,
    )
    return minted.token


# Behind a TLS-terminating proxy the request's own scheme, host and path prefix are not
# the ones a person can open (the Hub's front strips ``/hub``), so the deployment's
# public address wins: ``TRACKINIZER_PUBLIC_URL``, which includes any path prefix.
# A box that serves Trackinizer under a prefix sets TRACKINIZER_PUBLIC_URL; ops/stack
# does not set it yet.
def _public_base(request: Request) -> str:
    """Return the public address of this server, without a trailing slash."""
    public = os.environ.get("TRACKINIZER_PUBLIC_URL", "").rstrip("/")
    return public or str(request.base_url).rstrip("/")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Login:
    """One ``device_logins`` row, as the columns a route selected."""

    id: UUID
    client: str
    host: str
    machine: str | None
    status: str
    user_code: str = ""
    approver: UUID | None = None
    too_soon: bool = False


# A public route that inserts a row per request needs a ceiling. Codes live ten
# minutes, so this bounds the table at a few thousand even under a flood.
_MAX_OPEN_CODES: Final = 1_000


def _refused(status_code: int, *, code: str, message: str) -> HTTPException:
    """Return a refusal whose ``detail`` is ``{code, message}``."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )
