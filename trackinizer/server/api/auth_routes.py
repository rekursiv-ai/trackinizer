"""Routes under ``/api/me`` for the caller's own profile and API keys.

Every endpoint authenticates through the ``current_user`` Bearer-token
dependency. The token endpoints let a caller mint a key (the plaintext
secret is shown exactly once), list their keys without exposing secrets
or hashes, revoke a key, and re-tier a key's role. Keys not owned by the
caller (or already revoked) return 404, never a hint that they exist.
"""

from __future__ import annotations

from typing import Annotated, cast

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from trackinizer.lib.custom_json import MutableJSON
from trackinizer.server.api._routes_shared import (
    RoleLiteral,
    engine_of,
    iso_format,
)
from trackinizer.server.auth import (
    AuthIdentity,
    Role,
    RoleCeilingError,
    create_api_key,
    current_user,
    list_api_keys,
    revoke_api_key,
    set_api_key_role,
)


__all__ = [
    "CreateTokenBody",
    "TokenRoleChangeBody",
    "create_token_route",
    "list_tokens_route",
    "profile_route",
    "revoke_token_route",
    "set_token_role_route",
]


router = APIRouter()


class CreateTokenBody(BaseModel):
    """Request body for minting an API key.

    ``name`` is a human-facing label stored on the key so the UI can show
    what runs from where. ``role`` is an optional per-key ceiling that
    defaults to the caller's own role; a stronger role is rejected with
    403, which lets a writer mint a viewer-only token for a read-only
    agent without losing their own write capability.
    """

    name: str = Field(min_length=1, max_length=128)
    role: RoleLiteral | None = None


class TokenRoleChangeBody(BaseModel):
    """Request body carrying a key's new role ceiling.

    The role is bounded by the caller's own role; anything stronger
    surfaces as 403.
    """

    role: RoleLiteral


@router.get("/api/me/profile")
async def profile_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> MutableJSON:
    """Return the authenticated caller's profile fields.

    The ``/me`` and ``/admin`` pages need the caller's name and role, and
    this endpoint is the only way the browser can learn them: the session
    cookie is HttpOnly, so JS can't decode it. The shape mirrors the
    ``users`` table minus ``status``, since a disabled caller never
    reaches this route.
    """
    engine = engine_of(request)
    async with engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, last_login FROM users WHERE id = $1",
            identity.user_id,
        )
    name = identity.email if row is None else cast(str, row["name"])
    last_login = None if row is None else row["last_login"]
    return {
        "user_id": str(identity.user_id),
        "email": identity.email,
        "name": name,
        "role": identity.role,
        "last_login": iso_format(last_login),
    }


@router.post("/api/me/tokens")
async def create_token_route(
    body: CreateTokenBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> MutableJSON:
    """Create a new API key for the authenticated user.

    The plaintext ``secret`` is returned exactly once; the server stores
    only its hash and prefix. A client that loses the secret rotates by
    creating a new key and revoking the old one.

    The optional ``role`` field caps the new key at or below the caller's
    *effective* role -- the weaker of their user role and the role of the
    credential they presented -- defaulting to that ceiling when omitted. A
    stronger role returns 403, the boundary that keeps a writer from minting
    an admin token and a scoped-down key from self-escalating.
    """
    engine = engine_of(request)
    async with engine.acquire() as conn:
        try:
            key_id, secret, prefix, role = await create_api_key(
                conn,
                user_id=identity.user_id,
                name=body.name,
                role=body.role,
                ceiling=identity.role,
            )
        except RoleCeilingError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "id": str(key_id),
        "name": body.name,
        "prefix": prefix,
        "role": role,
        "secret": secret,
    }


@router.get("/api/me/tokens")
async def list_tokens_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> MutableJSON:
    """List the authenticated user's API keys.

    Hashes and secrets are never returned; ``prefix`` is the only field a
    caller can use to recognize a specific key.
    """
    engine = engine_of(request)
    async with engine.acquire() as conn:
        rows = await list_api_keys(conn, user_id=identity.user_id)
    return {"tokens": [_serialize(row) for row in rows]}


@router.post("/api/me/tokens/{key_id}/revoke")
async def revoke_token_route(
    key_id: uuid.UUID,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> MutableJSON:
    """Revoke one of the authenticated user's keys.

    Unknown id, foreign owner, and already-revoked all return 404, so an
    attacker probing UUIDs learns nothing about which keys exist.
    """
    engine = engine_of(request)
    async with engine.acquire() as conn:
        revoked = await revoke_api_key(conn, key_id=key_id, user_id=identity.user_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True}


@router.put("/api/me/tokens/{key_id}/role")
async def set_token_role_route(
    key_id: uuid.UUID,
    body: TokenRoleChangeBody,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(current_user)],
) -> MutableJSON:
    """Re-tier one of the caller's keys within the caller's effective ceiling.

    Same policy as create: a key can move to any role at or below the
    caller's *effective* role (the weaker of their user role and the
    presented credential's role), and anything stronger returns 403.
    Unknown id, foreign owner, and already-revoked all fold to 404, matching
    the revoke route's anti-enumeration stance.
    """
    engine = engine_of(request)
    async with engine.acquire() as conn:
        try:
            updated = await set_api_key_role(
                conn,
                key_id=key_id,
                user_id=identity.user_id,
                role=body.role,
                ceiling=identity.role,
            )
        except RoleCeilingError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True, "role": body.role}


def _serialize(row: dict[str, object]) -> MutableJSON:
    """Build the wire shape for one ``api_keys`` row, without hash or secret."""
    return {
        "id": str(row["id"]),
        "name": cast(str, row["name"]),
        "prefix": cast(str, row["prefix"]),
        "role": cast(Role, row["role"]),
        "created_at": iso_format(row["created_at"]),
        "last_used_at": iso_format(row["last_used_at"]),
        "revoked_at": iso_format(row["revoked_at"]),
    }
