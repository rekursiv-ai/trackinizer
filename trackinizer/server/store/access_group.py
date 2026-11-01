"""AccessGroup rows written on the caller's connection, for the installed access policy.

The access policy creates groups and moves their controls inside its own transaction,
under its own locks, so each write here joins the caller's connection. Each lands in
the change log as a submit or a field edit does; no route reaches the controls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from trackinizer.server.primitives import insert_inquiry
from trackinizer.server.store.cascade import _apply_change
from trackinizer.server.store.edit import _snapshot_field
from trackinizer.server.values import vetted_sql
from trackinizer.types.change_log import Change, Snapshot
from trackinizer.types.cost import Cost
from trackinizer.types.errors import ConflictError, NotFoundError


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn
    from trackinizer.types.inquiries import Inquiry


__all__ = ["insert_access_group", "set_access_group_controls"]


_CONTROLS: Final[tuple[Change.Kind, ...]] = (
    "accessgroup_read_group",
    "accessgroup_write_group",
)
"""The audited control columns, each its own change kind."""


async def insert_access_group(
    conn: Conn,
    group_id: UUID,
    *,
    title: str,
    account: str,
    read_group: UUID,
    write_group: UUID,
    system: bool = False,
    domain: str | None = None,
    actor: Inquiry.Actor,
    api_key_id: UUID | None = None,
) -> None:
    """Insert an AccessGroup row and its ``created`` change.

    Args:
      conn: The caller's connection, inside its transaction.
      group_id: The group's id.
      title: Its name.
      account: The account it is filed under, lowercase.
      read_group: The group whose members read it.
      write_group: The group whose members change it.
      system: Whether the server alone manages it.
      domain: Its verified company domain; only a system group holds one.
      actor: Who the change log names.
      api_key_id: The API key the change log names; ``None`` for none.

    """
    await insert_inquiry(
        conn,
        group_id,
        "AccessGroup",
        values={
            "title": title,
            "account": account,
            "accessgroup_read_group": read_group,
            "accessgroup_write_group": write_group,
            "accessgroup_system": system,
            "accessgroup_domain": domain,
        },
    )
    await _record(
        conn,
        group_id,
        kind="created",
        old=Snapshot(),
        new=Snapshot(),
        actor=actor,
        api_key_id=api_key_id,
    )


async def set_access_group_controls(
    conn: Conn,
    group_id: UUID,
    *,
    read_group: UUID,
    write_group: UUID,
    actor: Inquiry.Actor,
    api_key_id: UUID | None = None,
) -> tuple[UUID, ...]:
    """Move an AccessGroup's controls, auditing each control that changed.

    Args:
      conn: The caller's connection, inside its transaction.
      group_id: The group whose controls move.
      read_group: The group that reads it from now on.
      write_group: The group that changes it from now on.
      actor: Who the change log names.
      api_key_id: The API key the change log names; ``None`` for none.

    Returns:
      change_ids: One change per control that moved, read before write; empty when
        both already stood.

    Raises:
      NotFoundError: No inquiry has ``group_id``.
      ConflictError: The inquiry is not an AccessGroup.

    """
    row = await conn.fetchrow(
        "SELECT kind, accessgroup_read_group, accessgroup_write_group "
        "FROM inquiries WHERE id = $1 FOR UPDATE",
        group_id,
    )
    if row is None:
        raise NotFoundError(f"inquiry {group_id} not found")
    if row["kind"] != "AccessGroup":
        raise ConflictError(
            f"inquiry {group_id} is a {row['kind']}, not an AccessGroup",
        )
    changes: list[UUID] = []
    for column, value in zip(_CONTROLS, (read_group, write_group), strict=True):
        before = row[column]
        if before == value:
            continue
        await conn.execute(
            vetted_sql(
                "UPDATE inquiries SET ",
                column,
                " = $1, modified = clock_timestamp() WHERE id = $2",
            ),
            value,
            group_id,
        )
        changes.append(
            await _record(
                conn,
                group_id,
                kind=column,
                old=_snapshot_field(column, before, prefix="old_"),
                new=_snapshot_field(column, value, prefix="new_"),
                actor=actor,
                api_key_id=api_key_id,
            ),
        )
    return tuple(changes)


async def _record(
    conn: Conn,
    group_id: UUID,
    *,
    kind: Change.Kind,
    old: Snapshot,
    new: Snapshot,
    actor: Inquiry.Actor,
    api_key_id: UUID | None,
) -> UUID:
    """Write one change about ``group_id`` and return its id."""
    change_id = uuid4()
    await _apply_change(
        conn,
        change_id=change_id,
        subject_id=group_id,
        subject_kind="AccessGroup",
        kind=kind,
        api_key_id=api_key_id,
        actor=actor,
        caused_by=None,
        reason="",
        marginal_cost=Cost(),
        cost_delta=Cost(),
        old=old,
        new=new,
        extra_subscribers=(),
    )
    return change_id
