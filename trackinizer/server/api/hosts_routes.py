"""Routes for the hosts registry: where campaigns may run.

The registry never reaches a host, so deleting one unregisters it and touches
nothing on the host. A host whose host holds a live credential is in service,
and is not deleted until an admin revokes it (``daemon_routes``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from treekle import from_plain

from trackinizer.server.api._deps import hosts_changed
from trackinizer.server.api._routes_shared import request_engine
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.notify import tx
from trackinizer.server.values import canonical_strs
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.wire.wire_daemon import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_hosts import (
    HOST_LABELS_PATH,
    HOST_PATH,
    HOSTS_PATH,
    NAME_PATTERN,
    PERSONAL_ROLE,
    RESERVED_LABELS,
    RESERVED_NAMES,
    Host,
    HostDetail,
    HostLabels,
    HostList,
    HostPut,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn


__all__ = [
    "HostInServiceError",
    "HostName",
    "absent_host",
    "change_host_labels_route",
    "delete_host_route",
    "fetch_hosts",
    "get_host_route",
    "list_hosts_route",
    "put_host_route",
    "refuse_reserved",
    "router",
]


router = APIRouter()


class HostInServiceError(ConflictError):
    """The daemon holds a live credential, so it cannot be deleted."""

    code = "host_in_service"


HostName = Annotated[str, Path(pattern=NAME_PATTERN)]


@router.get(HOSTS_PATH, response_model=HostList)
async def list_hosts_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> HostList:
    """List every registered host.

    Args:
      request: Request carrying the database engine.
      identity: Writer-role principal.

    Returns:
      hosts: Every host, sorted by name in byte order.

    """
    async with request_engine(request).acquire() as conn:
        rows = await fetch_hosts(conn, name=None, viewer=identity)
    return HostList(hosts=[_host(row, viewer=identity) for row in rows])


@router.get(HOST_PATH, response_model=HostDetail)
async def get_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> HostDetail:
    """Return one host, with what its host reported.

    Args:
      name: The host's name.
      request: Request carrying the database engine.
      identity: Writer-role principal.

    Returns:
      host: The host.

    Raises:
      NotFoundError: No such host.

    """
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn:
        rows = await fetch_hosts(conn, name=name, viewer=identity)
    if not rows:
        raise absent_host(name)
    return _host_detail(rows[0], viewer=identity)


@router.put(HOST_PATH, status_code=204)
async def put_host_route(
    name: HostName,
    body: HostPut,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Register a host, or change the fields the body names.

    A field left out keeps its value, or defaults to empty on a new host;
    an empty string clears it.

    Args:
      name: The host's name.
      body: The new role and how line, each optional.
      request: Request carrying the database engine.
      identity: Admin principal, recorded as who changed it.

    Returns:
      empty: 204 with no body.

    """
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn:
        await conn.execute(
            "INSERT INTO hosts (name, role, how, updated_by) "
            "VALUES ($1, COALESCE($2, ''), COALESCE($3, ''), $4) "
            "ON CONFLICT (name) DO UPDATE SET "
            "role = COALESCE($2, hosts.role), "
            "how = COALESCE($3, hosts.how), "
            "updated_by = EXCLUDED.updated_by, updated = now()",
            name,
            body.role,
            body.how,
            identity.email,
        )
    return Response(status_code=204)


@router.patch(HOST_LABELS_PATH, status_code=204)
async def change_host_labels_route(
    name: HostName,
    body: HostLabels,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Add labels, then remove labels; each is a no-op when already so.

    Labels follow the Issue label rules: stripped, deduplicated, never blank.

    Args:
      name: The host's name.
      body: The labels to add and to remove.
      request: Request carrying the database engine.
      identity: Admin principal, recorded as who changed it.

    Returns:
      empty: 204 with no body.

    Raises:
      ValidationError: A label is blank or holds a NUL.
      NotFoundError: No such host.

    """
    refuse_reserved(name)
    add = _labels(body.add)
    remove = _labels(body.remove)
    async with request_engine(request).acquire() as conn:
        # One statement: the labels are read, changed and written under the row lock,
        # so two concurrent changes cannot overwrite each other.
        stored = await conn.fetchval(
            "UPDATE hosts SET labels = ARRAY("
            "SELECT label FROM unnest(labels || $2::text[]) "
            "WITH ORDINALITY AS t(label, position) "
            "WHERE label <> ALL($3::text[]) "
            "GROUP BY label ORDER BY min(position)), "
            "updated_by = $4, updated = now() "
            "WHERE name = $1 RETURNING name",
            name,
            add,
            remove,
            identity.email,
        )
    if stored is None:
        raise absent_host(name)
    return Response(status_code=204)


@router.delete(HOST_PATH, status_code=204)
async def delete_host_route(
    name: HostName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Unregister a host; nothing on the host itself is touched.

    Args:
      name: The host's name.
      request: Request carrying the database engine.
      identity: Admin principal.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: No such host.
      HostInServiceError: The host holds a live credential.

    """
    del identity
    refuse_reserved(name)
    async with request_engine(request).acquire() as conn, tx(conn):
        # The row lock makes a join racing this delete wait for it, so a credential
        # cannot appear between the check and the delete. The check is a separate
        # statement: one sharing the lock's would read the snapshot taken before the
        # lock was granted, and miss a join that committed meanwhile.
        host = await conn.fetchrow(
            "SELECT id, owner FROM hosts WHERE name = $1 FOR UPDATE",
            name,
        )
        if host is None:
            raise absent_host(name)
        if await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM host_credentials "
            "WHERE host_id = $1 AND revoked_at IS NULL)",
            host["id"],
        ):
            raise HostInServiceError(
                f"host {name!r} has a live credential; revoke it first",
            )
        await conn.execute("DELETE FROM hosts WHERE name = $1", name)
    hosts_changed(request, owner=from_plain(host["owner"], str, default=None))
    return Response(status_code=204)


def absent_host(name: str) -> NotFoundError:
    """Return the error for a host that is not registered."""
    return NotFoundError(f"host {name!r} is not registered")


# Each route calls this in its body, so the role check refuses a caller first and the
# reserved-name check cannot answer before it.
def refuse_reserved(name: str) -> None:
    """Refuse a reserved host name with 422."""
    if name in RESERVED_NAMES:
        raise ValidationError(f"{name!r} is a reserved host name")


# One statement for the listing and the single read, so the derived status has one
# spelling. ``$1`` is the silence, in seconds, after which a connected host reads
# offline; ``$2`` is one name, or NULL for every host. ``revoked`` means every
# credential the host ever had was revoked; ``never`` means no heartbeat yet. A
# personal host (``$3``) is read only by its owner (``$4``), an admin included: an
# admin revokes it by name, but never lists it, so nothing that lists hosts to choose
# one offers a person's laptop.
async def fetch_hosts(
    conn: Conn,
    *,
    name: str | None,
    viewer: AuthIdentity,
) -> list[Record]:
    """Read the hosts ``viewer`` may see, with the status derived.

    Args:
      conn: A connection.
      name: One host's name, or ``None`` for every host.
      viewer: Who reads; a personal host is read only by its owner.

    Returns:
      rows: The hosts, sorted by name in byte order.

    """
    return await conn.fetch(
        "SELECT id, name, role, how, labels, updated_by, updated, last_heartbeat, owner, "
        "created, confirmed_at, last_run_cwd, "
        "daemon_version, facts, CASE "
        "WHEN EXISTS (SELECT 1 FROM host_credentials c "
        "WHERE c.host_id = hosts.id) "
        "AND NOT EXISTS (SELECT 1 FROM host_credentials c "
        "WHERE c.host_id = hosts.id AND c.revoked_at IS NULL) THEN 'revoked' "
        "WHEN last_heartbeat IS NULL THEN 'never' "
        "WHEN daemon_instance IS NOT NULL "
        "AND last_heartbeat > now() - make_interval(secs => $1) THEN 'online' "
        "ELSE 'offline' END AS status "
        "FROM hosts WHERE ($2::text IS NULL OR name = $2) "
        "AND (role <> $3 OR owner = $4) "
        'ORDER BY name COLLATE "C"',
        OFFLINE_AFTER_SEC,
        name,
        PERSONAL_ROLE,
        viewer.email,
    )


def _host(row: Record, *, viewer: AuthIdentity) -> Host:
    """Build the wire model of one ``hosts`` row."""
    return Host.model_validate(_fields(row, viewer=viewer))


def _host_detail(row: Record, *, viewer: AuthIdentity) -> HostDetail:
    """Build the detailed wire model of one ``hosts`` row."""
    return HostDetail.model_validate(
        {
            **_fields(row, viewer=viewer),
            "daemon_version": from_plain(row["daemon_version"], str),
            "facts": from_plain(row["facts"], dict[str, object]),
        },
    )


def _fields(row: Record, *, viewer: AuthIdentity) -> dict[str, object]:
    """Return the fields a listed host carries; the owner only to who may see it."""
    owner = from_plain(row["owner"], str | None)
    return {
        "owner": owner if viewer.role == "admin" or owner == viewer.email else None,
        "name": from_plain(row["name"], str),
        "role": from_plain(row["role"], str),
        "how": from_plain(row["how"], str),
        "labels": from_plain(row["labels"], list[str]),
        "updated_by": from_plain(row["updated_by"], str),
        "updated": from_plain(row["updated"], datetime),
        "last_heartbeat": from_plain(row["last_heartbeat"], datetime | None),
        "status": from_plain(row["status"], str),
    }


def _labels(raw: Sequence[str]) -> list[str]:
    """Canonicalise labels as an Issue's are, refusing a blank or NUL one."""
    if any(not label.strip() or "\x00" in label for label in raw):
        raise ValidationError("a label is blank or contains a NUL character")
    labels = list(canonical_strs(raw))
    for label in labels:
        if label in RESERVED_LABELS:
            raise ValidationError(
                f"label {label} is the server's own, and is not edited",
            )
    return labels
