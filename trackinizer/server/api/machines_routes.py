"""Routes for the machines registry: where campaigns may run.

The registry never reaches a machine, so deleting one unregisters it and touches
nothing on the machine. A machine whose host holds a live credential is in service,
and is not deleted until an admin revokes it (``machine_host_routes``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Path, Request, Response

from trackinizer.lib.codec import from_plain
from trackinizer.server.api._routes_shared import engine_of
from trackinizer.server.auth import AuthIdentity, require_role
from trackinizer.server.notify import tx
from trackinizer.server.values import canonical_strs
from trackinizer.types.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from trackinizer.wire.wire_machine_host import OFFLINE_AFTER_SEC
from trackinizer.wire.wire_machines import (
    MACHINE_LABELS_PATH,
    MACHINE_PATH,
    MACHINES_PATH,
    NAME_PATTERN,
    RESERVED_NAMES,
    Machine,
    MachineDetail,
    MachineLabels,
    MachineList,
    MachinePut,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Record

    from trackinizer.lib.postgres import Conn


__all__ = [
    "MachineInServiceError",
    "MachineName",
    "absent_machine",
    "change_machine_labels_route",
    "delete_machine_route",
    "get_machine_route",
    "list_machines_route",
    "put_machine_route",
    "refuse_reserved",
    "router",
]


router = APIRouter()


class MachineInServiceError(ConflictError):
    """The machine's host holds a live credential, so it cannot be deleted."""

    code = "machine_in_service"


MachineName = Annotated[str, Path(pattern=NAME_PATTERN)]


@router.get(MACHINES_PATH, response_model=MachineList)
async def list_machines_route(
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MachineList:
    """List every registered machine.

    Args:
      request: Request carrying the database engine.
      identity: Writer-role principal.

    Returns:
      machines: Every machine, sorted by name in byte order.

    """
    del identity
    async with engine_of(request).acquire() as conn:
        rows = await _fetch_machines(conn, name=None)
    return MachineList(machines=[_machine(row) for row in rows])


@router.get(MACHINE_PATH, response_model=MachineDetail)
async def get_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("writer"))],
) -> MachineDetail:
    """Return one machine, with what its host reported.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      identity: Writer-role principal.

    Returns:
      machine: The machine.

    Raises:
      NotFoundError: No such machine.

    """
    del identity
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        rows = await _fetch_machines(conn, name=name)
    if not rows:
        raise absent_machine(name)
    return _machine_detail(rows[0])


@router.put(MACHINE_PATH, status_code=204)
async def put_machine_route(
    name: MachineName,
    body: MachinePut,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Register a machine, or change the fields the body names.

    A field left out keeps its value, or defaults to empty on a new machine;
    an empty string clears it.

    Args:
      name: The machine's name.
      body: The new role and how line, each optional.
      request: Request carrying the database engine.
      identity: Admin principal, recorded as who changed it.

    Returns:
      empty: 204 with no body.

    """
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn:
        await conn.execute(
            "INSERT INTO machines (name, role, how, updated_by) "
            "VALUES ($1, COALESCE($2, ''), COALESCE($3, ''), $4) "
            "ON CONFLICT (name) DO UPDATE SET "
            "role = COALESCE($2, machines.role), "
            "how = COALESCE($3, machines.how), "
            "updated_by = EXCLUDED.updated_by, updated = now()",
            name,
            body.role,
            body.how,
            identity.email,
        )
    return Response(status_code=204)


@router.patch(MACHINE_LABELS_PATH, status_code=204)
async def change_machine_labels_route(
    name: MachineName,
    body: MachineLabels,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Add labels, then remove labels; each is a no-op when already so.

    Labels follow the Issue label rules: stripped, deduplicated, never blank.

    Args:
      name: The machine's name.
      body: The labels to add and to remove.
      request: Request carrying the database engine.
      identity: Admin principal, recorded as who changed it.

    Returns:
      empty: 204 with no body.

    Raises:
      ValidationError: A label is blank or holds a NUL.
      NotFoundError: No such machine.

    """
    refuse_reserved(name)
    add = _labels(body.add)
    remove = _labels(body.remove)
    async with engine_of(request).acquire() as conn:
        # One statement: the labels are read, changed and written under the row lock,
        # so two concurrent changes cannot overwrite each other.
        stored = await conn.fetchval(
            "UPDATE machines SET labels = ARRAY("
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
        raise absent_machine(name)
    return Response(status_code=204)


@router.delete(MACHINE_PATH, status_code=204)
async def delete_machine_route(
    name: MachineName,
    request: Request,
    identity: Annotated[AuthIdentity, Depends(require_role("admin"))],
) -> Response:
    """Unregister a machine; nothing on the machine itself is touched.

    Args:
      name: The machine's name.
      request: Request carrying the database engine.
      identity: Admin principal.

    Returns:
      empty: 204 with no body.

    Raises:
      NotFoundError: No such machine.
      MachineInServiceError: The machine holds a live credential.

    """
    del identity
    refuse_reserved(name)
    async with engine_of(request).acquire() as conn, tx(conn):
        # The row lock makes a join racing this delete wait for it, so a credential
        # cannot appear between the check and the delete. The check is a separate
        # statement: one sharing the lock's would read the snapshot taken before the
        # lock was granted, and miss a join that committed meanwhile.
        machine_id = await conn.fetchval(
            "SELECT id FROM machines WHERE name = $1 FOR UPDATE",
            name,
        )
        if machine_id is None:
            raise absent_machine(name)
        if await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM machine_credentials "
            "WHERE machine_id = $1 AND revoked_at IS NULL)",
            machine_id,
        ):
            raise MachineInServiceError(
                f"machine {name!r} has a live credential; revoke it first",
            )
        await conn.execute("DELETE FROM machines WHERE name = $1", name)
    return Response(status_code=204)


def absent_machine(name: str) -> NotFoundError:
    """Return the error for a machine that is not registered."""
    return NotFoundError(f"machine {name!r} is not registered")


# Each route calls this in its body, so the role check refuses a caller first and the
# reserved-name check cannot answer before it.
def refuse_reserved(name: str) -> None:
    """Refuse a reserved machine name with 422."""
    if name in RESERVED_NAMES:
        raise ValidationError(f"{name!r} is a reserved machine name")


def _machine(row: Record) -> Machine:
    """Build the wire model of one ``machines`` row."""
    return Machine.model_validate(_fields(row))


def _machine_detail(row: Record) -> MachineDetail:
    """Build the detailed wire model of one ``machines`` row."""
    return MachineDetail.model_validate(
        {
            **_fields(row),
            "host_version": from_plain(row["host_version"], str),
            "facts": from_plain(row["facts"], dict[str, object]),
        },
    )


def _fields(row: Record) -> dict[str, object]:
    """Return the fields a listed machine carries."""
    return {
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
    return list(canonical_strs(raw))


# One statement for the listing and the single read, so the derived status has one
# spelling. ``$1`` is the silence, in seconds, after which a connected machine reads
# offline; ``$2`` is one name, or NULL for every machine. ``revoked`` means every
# credential the machine ever had was revoked; ``never`` means no heartbeat yet.
async def _fetch_machines(conn: Conn, *, name: str | None) -> list[Record]:
    """Read machines, with the status derived from credentials and heartbeat."""
    return await conn.fetch(
        "SELECT name, role, how, labels, updated_by, updated, last_heartbeat, "
        "host_version, facts, CASE "
        "WHEN EXISTS (SELECT 1 FROM machine_credentials c "
        "WHERE c.machine_id = machines.id) "
        "AND NOT EXISTS (SELECT 1 FROM machine_credentials c "
        "WHERE c.machine_id = machines.id AND c.revoked_at IS NULL) THEN 'revoked' "
        "WHEN last_heartbeat IS NULL THEN 'never' "
        "WHEN last_heartbeat > now() - make_interval(secs => $1) THEN 'online' "
        "ELSE 'offline' END AS status "
        'FROM machines WHERE $2::text IS NULL OR name = $2 ORDER BY name COLLATE "C"',
        OFFLINE_AFTER_SEC,
        name,
    )
