"""Admin routes for service accounts: identities for programs, keyed without a sign-in.

A service account is a ``users`` row at ``NAME@service.invalid``, which is the whole
definition: no column says so. An admin creates it and mints its API keys here; it
signs in nowhere, holds no Google account, is never an admin, and its keys cannot
mint, re-tier or revoke keys (``auth_routes``). The existing admin routes list, disable, enable and delete it as any
user. A key minted for it stays inside the account's role and audiences, and inside what
the minting admin's credential reads and opens into: a key never grants more than it
holds.

Admin actions on users keep no audit table, so each action here is logged at WARNING with
the admin who acted and the service account acted on, never a secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, cast

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import get_assistant, get_staff, get_store
from trackinizer.server.api._routes_shared import engine_of, iso_format
from trackinizer.server.api.admin_routes import USER_COLUMNS, user_entry
from trackinizer.server.audience import audiences_for
from trackinizer.server.auth import (
    AudienceCeilingError,
    AuthIdentity,
    Role,
    RoleCeilingError,
    create_api_key,
    list_api_keys,
    require_role,
    revoke_api_key,
)
from trackinizer.server.notify import tx
from trackinizer.server.service_accounts import (
    require_assistant_grant,
    require_grantable,
    service_token_pin,
)
from trackinizer.server.values import vetted_sql
from trackinizer.wire.json_types import MutableJSON
from trackinizer.wire.wire_service_accounts import (
    SERVICE_ACCOUNT_KEY_REVOKE_PATH,
    SERVICE_ACCOUNT_TOKENS_PATH,
    SERVICE_ACCOUNTS_PATH,
    ServiceAccount,
    ServiceAccountCreate,
    ServiceToken,
    ServiceTokenCreate,
    ServiceTokenCreated,
    ServiceTokenList,
    is_service_address,
    service_address,
)


if TYPE_CHECKING:
    from asyncpg import Record

    from trackinizer.lib.postgres import Conn


__all__ = [
    "create_service_account_route",
    "create_service_token_route",
    "list_service_tokens_route",
    "revoke_service_token_route",
    "router",
]


router = APIRouter()
_logger = logging.getLogger(__name__)


@router.post(SERVICE_ACCOUNTS_PATH, response_model=ServiceAccount, status_code=201)
async def create_service_account_route(
    body: ServiceAccountCreate,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Create a service account; 409 when its name is taken, whatever the row's status.

    A disabled account keeps its name until an admin deletes it, so creating the name
    again never revives it; a deleted account takes its keys with it, and a new account
    of the same name starts with none. The account holds the audiences the staff
    setting gives its address, and a caller cannot create one that holds more than it
    does.

    Args:
      body: The account's name and role.
      request: Request carrying the database engine and the configuration.
      identity: Admin principal, recorded in the log as who created it.

    Returns:
      user: The new user, as ``GET /api/admin/users`` lists one.

    Raises:
      HTTPException: 403 when the account would hold an audience the caller does not,
        409 when the name is taken.

    """
    staff = get_staff(request)
    email = service_address(body.name)
    held = audiences_for(email, hosted_domain=None, staff=staff)
    try:
        require_grantable(identity, reads=held, opens=held)
        require_assistant_grant(
            identity,
            email=email,
            assistant=get_assistant(request),
        )
    except AudienceCeilingError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    async with engine_of(request).acquire() as conn, tx(conn):
        row = await conn.fetchrow(
            vetted_sql(
                "INSERT INTO users (id, email, name, role, status) "
                "VALUES ($1, $2, $3, $4, 'active') "
                "ON CONFLICT (email) DO NOTHING RETURNING ",
                USER_COLUMNS,
            ),
            uuid.uuid4(),
            email,
            body.name,
            body.role,
        )
        if row is not None:
            await get_store(request).access_policy.on_user(
                conn,
                email=email,
                role=body.role,
            )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=f"service account {body.name!r} already exists",
        )
    _log("service_account_created", admin=identity, account=row, role=body.role)
    return user_entry(row, staff=staff)


@router.post(
    SERVICE_ACCOUNT_TOKENS_PATH,
    response_model=ServiceTokenCreated,
    status_code=201,
)
async def create_service_token_route(
    user_id: uuid.UUID,
    body: ServiceTokenCreate,
    request: Request,
    response: Response,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> ServiceTokenCreated:
    """Mint one API key for a service account; the secret is shown once.

    The key's role is at most the account's and defaults to it. Its pin stays
    inside the audiences the account holds under the staff setting, and inside what
    the caller's own credential reads and opens into; a pinned key hands its pin on.
    The account row stays locked until the key is stored, so a disable cannot slip
    between the check and the insert.

    Args:
      user_id: The service account.
      body: The key's label, and optionally its role and pin.
      request: Request carrying the database engine and the configuration.
      response: Response, marked ``no-store`` because it carries a secret.
      identity: Admin principal, recorded in the log as who minted it.

    Returns:
      token: The key's id, label, prefix, role, stored pin and plaintext secret.

    Raises:
      HTTPException: 403 when the role or the pin exceeds the account's or the
        caller's, 404 when the id is no service account, 409 when it is disabled.

    """
    staff = get_staff(request)
    async with engine_of(request).acquire() as conn, tx(conn):
        account = await _account(conn, user_id, lock=True)
        if account is None:
            raise _no_account()
        if from_plain(account["status"], str) != "active":
            raise HTTPException(status_code=409, detail="service account is disabled")
        held = audiences_for(
            from_plain(account["email"], str),
            hosted_domain=from_plain(account["hosted_domain"], str, default=None),
            staff=staff,
        )
        try:
            require_assistant_grant(
                identity,
                email=from_plain(account["email"], str),
                assistant=get_assistant(request),
            )
            pin = service_token_pin(
                held,
                minter=identity,
                open_into=body.open_into,
            )
            key_id, secret, prefix, role = await create_api_key(
                conn,
                user_id=user_id,
                name=body.name,
                role=body.role,
                ceiling=cast(Role, account["role"]),
                open_into=pin,
            )
        except (RoleCeilingError, AudienceCeilingError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    _log(
        "service_token_minted",
        admin=identity,
        account=account,
        key_id=str(key_id),
        key_name=body.name,
        role=role,
        open_into=pin,
    )
    return ServiceTokenCreated(
        id=str(key_id),
        name=body.name,
        prefix=prefix,
        role=role,
        open_into=pin,
        secret=secret,
    )


@router.get(SERVICE_ACCOUNT_TOKENS_PATH, response_model=ServiceTokenList)
async def list_service_tokens_route(
    user_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> ServiceTokenList:
    """List the keys of a service account, never a secret or a hash.

    Args:
      user_id: The service account.
      request: Request carrying the database engine.
      identity: Admin principal.

    Returns:
      tokens: Every key of the account, live and revoked, newest first.

    Raises:
      HTTPException: 404 when the id is no service account, a person's included.

    """
    del identity
    async with engine_of(request).acquire() as conn:
        if await _account(conn, user_id, lock=False) is None:
            raise _no_account()
        rows = await list_api_keys(conn, user_id=user_id)
    return ServiceTokenList(tokens=[_token(row) for row in rows])


@router.post(SERVICE_ACCOUNT_KEY_REVOKE_PATH)
async def revoke_service_token_route(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Revoke one key of a service account.

    Unknown id, another account's key, and an already revoked key all answer 404, as
    does an id that is no service account: the route never reaches a person's keys.

    Args:
      user_id: The service account.
      key_id: The key to revoke.
      request: Request carrying the database engine and the bearer cache.
      identity: Admin principal, recorded in the log as who revoked it.

    Returns:
      result: ``{"ok": true}``.

    Raises:
      HTTPException: 404 when the account or its live key is not found.

    """
    async with engine_of(request).acquire() as conn:
        account = await _account(conn, user_id, lock=False)
        revoked = account is not None and await revoke_api_key(
            conn,
            key_id=key_id,
            user_id=user_id,
        )
    if account is None or not revoked:
        raise HTTPException(status_code=404, detail="token not found")
    # The verified-bearer cache would otherwise keep honoring the key for up to its TTL.
    get_store(request).forget_bearer_identity_for_key(key_id)
    _log("service_token_revoked", admin=identity, account=account, key_id=str(key_id))
    return {"ok": True}


async def _account(conn: Conn, user_id: uuid.UUID, *, lock: bool) -> Record | None:
    """Read a service account's row; ``None`` for no row or a row that is a person's."""
    row = await conn.fetchrow(
        vetted_sql(
            "SELECT ",
            USER_COLUMNS,
            " FROM users WHERE id = $1",
            " FOR SHARE" if lock else "",
        ),
        user_id,
    )
    if row is None or not is_service_address(from_plain(row["email"], str)):
        return None
    return row


def _no_account() -> HTTPException:
    """Answer an id that is no service account, a person's or an absent one alike."""
    return HTTPException(status_code=404, detail="service account not found")


def _token(row: dict[str, object]) -> ServiceToken:
    """Build the wire shape of one ``api_keys`` row, without hash or secret."""
    return ServiceToken(
        id=str(row["id"]),
        name=from_plain(row["name"], str),
        prefix=from_plain(row["prefix"], str),
        role=cast(Role, row["role"]),
        open_into=from_plain(row["open_into"], str, default=None),
        created_at=from_plain(iso_format(row["created_at"]), str),
        last_used_at=iso_format(row["last_used_at"]),
        revoked_at=iso_format(row["revoked_at"]),
    )


# Admin actions on users keep no audit table, so this is the trail: one WARNING line per
# action naming the admin and the account, and the key where there is one. WARNING,
# because the deployment default level drops INFO. The text
# quotes free-form values with ``%r`` so a crafted label cannot forge a field.
def _log(
    action: str,
    *,
    admin: AuthIdentity,
    account: Record,
    **detail: object,
) -> None:
    """Log an admin action on a service account with both identities."""
    account_email = from_plain(account["email"], str)
    admin_key = None if admin.api_key_id is None else str(admin.api_key_id)
    _logger.warning(
        "event=trackinizer_admin_action action=%s admin_user_id=%s admin_email=%r "
        "admin_api_key_id=%s account_user_id=%s account_email=%r detail=%r",
        action,
        admin.user_id,
        admin.email,
        admin_key,
        account["id"],
        account_email,
        detail,
        extra={
            "event": "trackinizer_admin_action",
            "action": action,
            "admin_user_id": str(admin.user_id),
            "admin_email": admin.email,
            "admin_api_key_id": admin_key,
            "account_user_id": str(account["id"]),
            "account_email": account_email,
            **detail,
        },
    )
