"""Admin-only routes for managing users and the allowlist.

These endpoints back the ``/admin`` page. They cover listing users,
changing roles, disabling/enabling/deleting users, and CRUD on the
allowlist. Every endpoint requires the admin role: unauthenticated
callers get 401, non-admin callers get 403.

Disabling a user also revokes their outstanding API keys. Re-enabling
does not restore those keys; the user mints fresh ones, matching the
rotate-on-suspicion stance taken across the auth surface. The disable
and enable endpoints stay available even though the UI's per-row action
is hard-delete, so a future "suspend but keep the audit trail" workflow
has them ready.
"""

from __future__ import annotations

from typing import Annotated, cast

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.lib.postgres import Conn
from trackinizer.server.api._deps import get_store
from trackinizer.server.api._routes_shared import (
    RoleLiteral,
    engine_of,
    iso_format,
)
from trackinizer.server.auth import AuthIdentity, Role, require_role
from trackinizer.server.notify import tx


__all__ = [
    "AllowlistAddBody",
    "RoleChangeBody",
    "admin_add_allowlist_route",
    "admin_disable_user_route",
    "admin_enable_user_route",
    "admin_list_allowlist_route",
    "admin_list_users_route",
    "admin_remove_allowlist_route",
    "admin_remove_user_route",
    "admin_set_allowlist_role_route",
    "admin_set_user_role_route",
]


router = APIRouter()


class RoleChangeBody(BaseModel):
    """Request body carrying a new role for a user."""

    role: RoleLiteral


class AllowlistAddBody(BaseModel):
    """Request body for adding an allowlist entry.

    ``email_or_pattern`` is either a literal address (``user@example.com``)
    or a domain wildcard (``*@example.com``), matched on each OAuth
    callback. ``role`` is the role granted to users onboarded through
    this entry.
    """

    email_or_pattern: str = Field(min_length=1, max_length=320)
    role: RoleLiteral


@router.get("/api/admin/users")
async def admin_list_users_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """List every user row for the admin table."""
    del identity
    engine = engine_of(request)
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, email, name, role, status, created_at, last_login "
            "FROM users ORDER BY created_at DESC, id DESC"
        )
    return {"users": [_serialize_user(dict(row)) for row in rows]}


@router.put("/api/admin/users/{user_id}/role")
async def admin_set_user_role_route(
    user_id: uuid.UUID,
    body: RoleChangeBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Update one user's role; 404 when the id matches no row."""
    if user_id == identity.user_id and body.role != "admin":
        raise HTTPException(
            status_code=409,
            detail="Demoting one's own admin role is not permitted.",
        )
    engine = engine_of(request)
    async with engine.acquire() as conn, tx(conn):
        if body.role != "admin":
            await _lock_admin_roster(conn)
            await _refuse_last_admin_loss(conn, user_id)
        result = await conn.execute(
            "UPDATE users SET role = $2 WHERE id = $1",
            user_id,
            body.role,
        )
        if not result.endswith(" 1"):
            raise HTTPException(status_code=404, detail="user not found")
    # Cached bearer entries carry the old role; without this the demotion
    # would not bind until VERIFIED_BEARER_TTL_SEC elapsed.
    get_store(request).forget_bearer_identities(user_id)
    return {"ok": True, "role": body.role}


@router.post("/api/admin/users/{user_id}/disable")
async def admin_disable_user_route(
    user_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Disable a user and revoke every API key they own.

    The status change and the revocation share one transaction, so a
    failed revoke can't leave a disabled user holding live tokens.
    """
    if user_id == identity.user_id:
        raise HTTPException(
            status_code=409,
            detail="Disabling one's own account is not permitted.",
        )
    engine = engine_of(request)
    async with engine.acquire() as conn, tx(conn):
        await _lock_admin_roster(conn)
        await _refuse_last_admin_loss(conn, user_id)
        result = await conn.execute(
            "UPDATE users SET status = 'disabled' WHERE id = $1",
            user_id,
        )
        if not result.endswith(" 1"):
            raise HTTPException(status_code=404, detail="user not found")
        await conn.execute(
            "UPDATE api_keys SET revoked_at = clock_timestamp() "
            "WHERE user_id = $1 AND revoked_at IS NULL",
            user_id,
        )
    # Revoking the rows is not enough: a cached entry bypasses the query
    # entirely, so a disabled user would keep authenticating until the TTL.
    get_store(request).forget_bearer_identities(user_id)
    return {"ok": True, "status": "disabled"}


@router.post("/api/admin/users/{user_id}/enable")
async def admin_enable_user_route(
    user_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Re-enable a previously disabled user.

    Revoked tokens stay revoked; the user mints fresh keys via ``/me``,
    matching the rotate-on-suspicion stance across the auth surface.
    """
    del identity
    engine = engine_of(request)
    async with engine.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET status = 'active' WHERE id = $1",
            user_id,
        )
    if not result.endswith(" 1"):
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True, "status": "active"}


@router.delete("/api/admin/users/{user_id}", status_code=204)
async def admin_remove_user_route(
    user_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Hard-delete a user, cascading their API keys and nulling audit links.

    The ``api_keys.user_id`` FK cascades, so the user's keys vanish in
    the same statement. The ``change_log.api_key_id`` FK sets null on
    delete, so audit rows survive: only the per-key linkage is lost,
    while ``actor`` and every other column stay intact.

    Self-delete is refused with 409 so an admin can't accidentally lock
    the org out. A multi-admin org still rotates admins by having one
    admin delete the other.
    """
    if user_id == identity.user_id:
        raise HTTPException(
            status_code=409,
            detail="Deleting one's own account is not permitted.",
        )
    engine = engine_of(request)
    async with engine.acquire() as conn, tx(conn):
        await _lock_admin_roster(conn)
        await _refuse_last_admin_loss(conn, user_id)
        result = await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        if not result.endswith(" 1"):
            raise HTTPException(status_code=404, detail="user not found")
    # The FK cascade drops the api_keys rows, but a cached entry never
    # queries them; a deleted user would keep authenticating until the TTL.
    get_store(request).forget_bearer_identities(user_id)
    return Response(status_code=204)


@router.get("/api/admin/allowlist")
async def admin_list_allowlist_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """List allowlist entries for the admin page."""
    del identity
    engine = engine_of(request)
    async with engine.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email_or_pattern, role, added_by, added_at "
            "FROM allowlist ORDER BY added_at DESC, email_or_pattern"
        )
    return {"entries": [_serialize_allowlist(dict(row)) for row in rows]}


@router.post("/api/admin/allowlist")
async def admin_add_allowlist_route(
    body: AllowlistAddBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Insert a new allowlist entry; 409 on a duplicate ``email_or_pattern``.

    The 409 comes from the global unique-violation handler in
    ``api.app``, so this route needs no try/except of its own.
    """
    email_or_pattern = _canonical_allowlist_entry(body.email_or_pattern)
    engine = engine_of(request)
    async with engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO allowlist (email_or_pattern, role, added_by) "
            "VALUES ($1, $2, $3)",
            email_or_pattern,
            body.role,
            identity.user_id,
        )
    return {
        "ok": True,
        "email_or_pattern": email_or_pattern,
        "role": body.role,
    }


def _canonical_allowlist_entry(value: str) -> str:
    canonical = value.strip().lower()
    if not canonical:
        raise HTTPException(status_code=422, detail="allowlist entry cannot be blank")
    return canonical


@router.put("/api/admin/allowlist/{email_or_pattern}/role")
async def admin_set_allowlist_role_route(
    email_or_pattern: str,
    body: RoleChangeBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Change one allowlist entry's role; 404 when the key matches no row.

    Same URL-decode behavior as the DELETE route: clients percent-encode
    wildcard rows like ``*@example.com`` as ``%2A%40example.com`` and
    FastAPI hands the path param back already decoded.
    """
    del identity
    engine = engine_of(request)
    async with engine.acquire() as conn:
        result = await conn.execute(
            "UPDATE allowlist SET role = $2 WHERE email_or_pattern = $1",
            _canonical_allowlist_entry(email_or_pattern),
            body.role,
        )
    if not result.endswith(" 1"):
        raise HTTPException(status_code=404, detail="allowlist entry not found")
    return {"ok": True, "role": body.role}


@router.delete("/api/admin/allowlist/{email_or_pattern}")
async def admin_remove_allowlist_route(
    email_or_pattern: str,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> MutableJSON:
    """Delete one allowlist entry by its (URL-decoded) primary key.

    Clients URL-encode ``*@example.com`` as ``%2A%40example.com``;
    Starlette percent-decodes path params before the handler sees them,
    so the value matches the stored row as-is.
    """
    del identity
    engine = engine_of(request)
    async with engine.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM allowlist WHERE email_or_pattern = $1",
            _canonical_allowlist_entry(email_or_pattern),
        )
    if not result.endswith(" 1"):
        raise HTTPException(status_code=404, detail="allowlist entry not found")
    return {"ok": True}


async def _lock_admin_roster(conn: Conn) -> None:
    """Serialize admin-roster mutations within the current transaction.

    The last-admin guard is a cross-row invariant. Under Read Committed,
    two concurrent demote/disable/delete transactions on distinct admins
    each see ``other_admins >= 1`` and both commit, leaving zero active
    admins. A fixed-key, transaction-scoped advisory lock funnels every
    role-affecting transaction through one critical section, so the guard
    reads a stable roster.
    """
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('admin_roster'))")


async def _refuse_last_admin_loss(conn: Conn, target_id: uuid.UUID) -> None:
    """Refuse a demote/disable/delete that would leave zero active admins.

    Queries the ``users`` table for whether ``target_id`` is an active
    admin and how many other active admins exist, then raises 409 with
    ``last_admin`` when removing this one would empty the roster. Callers
    must already hold ``_lock_admin_roster`` for the read to be race-free.
    """
    row = await conn.fetchrow(
        "SELECT "
        "(SELECT role = 'admin' AND status = 'active' FROM users WHERE id = $1) "
        "  AS target_is_admin_active, "
        "(SELECT COUNT(*) FROM users "
        "  WHERE role = 'admin' AND status = 'active' AND id != $1) "
        "  AS other_admins",
        target_id,
    )
    if row is None or not row["target_is_admin_active"]:
        return
    if row["other_admins"] >= 1:
        return
    raise HTTPException(
        status_code=409,
        detail="last_admin: refusing to leave the org without an active admin.",
    )


def _serialize_user(row: dict[str, object]) -> MutableJSON:
    """Build the wire shape for one ``users`` row."""
    return {
        "id": str(row["id"]),
        "email": str(row["email"]),
        "name": str(row["name"]),
        "role": cast(Role, row["role"]),
        "status": str(row["status"]),
        "created_at": iso_format(row["created_at"]),
        "last_login": iso_format(row["last_login"]),
    }


def _serialize_allowlist(row: dict[str, object]) -> MutableJSON:
    """Build the wire shape for one ``allowlist`` row."""
    added_by = row.get("added_by")
    return {
        "email_or_pattern": str(row["email_or_pattern"]),
        "role": str(row["role"]),
        "added_by": (None if added_by is None else str(cast(uuid.UUID, added_by))),
        "added_at": iso_format(row["added_at"]),
    }
