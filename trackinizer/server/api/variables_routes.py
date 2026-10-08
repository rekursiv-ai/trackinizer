"""Routes for the org's environment variables, plain and secret.

A plain value is stored in ``variables`` and read back. A secret's value goes
to the :class:`~trackinizer.server.secrets.SecretBackend` and no route
returns it; its row keeps only the name and who set it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, cast

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._deps import get_secrets
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.notify import tx
from trackinizer.server.secrets import SecretBackend, SecretRef
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.wire.wire_variables import (
    MAX_VALUE_BYTES,
    NAME_PATTERN,
    VARIABLES_PATH,
    Variable,
    VariableList,
    VariablePut,
)


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


__all__ = [
    "delete_variable_route",
    "list_variables_route",
    "put_variable_route",
    "router",
]


router = APIRouter()


@router.get(VARIABLES_PATH, response_model=VariableList)
async def list_variables_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> VariableList:
    """List the org layer's variables by name; a secret's value is ``null``.

    Args:
      request: Request carrying the database engine.
      identity: Writer-role principal.

    Returns:
      variables: Every org-layer variable, sorted by name.

    """
    del identity
    async with engine_of(request).acquire() as conn:
        rows = await conn.fetch(
            "SELECT layer, owner, name, secret, value, updated_by, updated "
            "FROM variables WHERE layer = 'org' AND owner = '' ORDER BY name",
        )
    return VariableList(
        variables=[
            Variable.model_validate(
                {
                    "layer": from_plain(row["layer"], str),
                    "owner": from_plain(row["owner"], str),
                    "name": from_plain(row["name"], str),
                    "secret": from_plain(row["secret"], bool),
                    "value": from_plain(row["value"], str | None),
                    "updated_by": from_plain(row["updated_by"], str),
                    "updated": from_plain(row["updated"], datetime),
                },
            )
            for row in rows
        ],
    )


# ``{name:path}``, not ``{name}``: a name holding a slash must reach the pattern
# check and answer 422, where ``{name}`` would not match the route and answer 404.
@router.put("/api/variables/{name:path}", status_code=204)
async def put_variable_route(
    name: Annotated[str, Path(pattern=NAME_PATTERN)],
    body: VariablePut,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Set one org-layer variable.

    A name stored as a secret stays secret: a plain value is refused until the
    variable is deleted. A plain name may become a secret.

    Args:
      name: The environment-variable name.
      body: The value and whether it is secret.
      request: Request carrying the database engine and secret backend.
      identity: Admin principal, recorded as who set it.

    Returns:
      empty: 204 with no body, so a secret is never echoed.

    Raises:
      ValidationError: The URL's last segment is not the name, or the value is
        too long in UTF-8 or holds a NUL.
      ConflictError: A plain value for a name stored as a secret.
      HTTPException: 503 when a secret is sent and no secret store is configured.

    """
    _require_exact_name(request, name=name)
    if len(body.value.encode()) > MAX_VALUE_BYTES:
        raise ValidationError(f"value is longer than {MAX_VALUE_BYTES} bytes")
    # An environment variable cannot hold NUL, and ``text`` cannot store it.
    if "\x00" in body.value:
        raise ValidationError("value contains a NUL character")
    backend = _backend(request) if body.secret else None
    ref = SecretRef(layer="org", owner="", name=name)
    async with engine_of(request).acquire() as conn, tx(conn):
        await _lock(conn, ref=ref)
        if backend is not None:
            await asyncio.to_thread(backend.put, ref, body.value)
        # A secret row's value is NULL, so the plain upsert may only touch a row
        # that is not already secret.
        stored = await conn.fetchval(
            "INSERT INTO variables (layer, owner, name, secret, value, updated_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (layer, owner, name) DO UPDATE SET "
            "secret = EXCLUDED.secret, value = EXCLUDED.value, "
            "updated_by = EXCLUDED.updated_by, updated = now() "
            "WHERE EXCLUDED.secret OR NOT variables.secret "
            "RETURNING name",
            ref.layer,
            ref.owner,
            ref.name,
            body.secret,
            None if body.secret else body.value,
            identity.email,
        )
    if stored is None:
        raise ConflictError(f"{name} is a secret; delete it first")
    return Response(status_code=204)


@router.delete("/api/variables/{name:path}", status_code=204)
async def delete_variable_route(
    name: Annotated[str, Path(pattern=NAME_PATTERN)],
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Delete one org-layer variable; a secret's value goes first.

    Args:
      name: The environment-variable name.
      request: Request carrying the database engine and secret backend.
      identity: Admin principal.

    Returns:
      empty: 204 with no body.

    Raises:
      ValidationError: The URL's last segment is not the name.
      NotFoundError: No such variable.
      HTTPException: 503 when it is a secret and no secret store is configured.

    """
    del identity
    _require_exact_name(request, name=name)
    ref = SecretRef(layer="org", owner="", name=name)
    async with engine_of(request).acquire() as conn, tx(conn):
        await _lock(conn, ref=ref)
        secret = await conn.fetchval(
            "SELECT secret FROM variables "
            "WHERE layer = $1 AND owner = $2 AND name = $3",
            ref.layer,
            ref.owner,
            ref.name,
        )
        if secret is None:
            raise NotFoundError(f"{name} is not set")
        if secret:
            await asyncio.to_thread(_backend(request).delete, ref)
        await conn.execute(
            "DELETE FROM variables WHERE layer = $1 AND owner = $2 AND name = $3",
            ref.layer,
            ref.owner,
            ref.name,
        )
    return Response(status_code=204)


# Starlette's ``$`` also matches before a final newline, so ``A%0A`` would reach the
# route as ``A``. A valid name holds no slash, so the last segment is the whole name
# whatever path prefix the app is served under.
def _require_exact_name(request: Request, *, name: str) -> None:
    """Refuse a URL whose last segment is not the name the route matched."""
    # ``request.url`` is re-parsed, which drops the newline; the scope holds it.
    if cast(str, request.scope["path"]).rsplit("/", maxsplit=1)[-1] != name:
        raise ValidationError("variable name is not a valid name")


def _backend(request: Request) -> SecretBackend:
    """Return the configured secret backend, or refuse with 503."""
    backend = get_secrets(request)
    if backend is None:
        raise HTTPException(status_code=503, detail="no secret store is configured")
    return backend


# A secret's value and its row are two writes; without the lock a delete and a put on
# one name interleave them and leave a row with no value, or a value with no row.
async def _lock(conn: Conn, *, ref: SecretRef) -> None:
    """Serialise writers of one variable until the transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"trackinizer.variables.{ref.layer}.{ref.owner}.{ref.name}",
    )
