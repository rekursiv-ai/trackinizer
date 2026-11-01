"""AccessGroup rows written on the caller's connection, over PGlite.

Each test gets the session's build put back (``store``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from trackinizer.server.notify import tx
from trackinizer.server.store.access_group import (
    insert_access_group,
    set_access_group_controls,
)
from trackinizer.types.errors import ConflictError, NotFoundError
from trackinizer.types.inquiries import AccessGroup
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from uuid import UUID

    from trackinizer.server.store.core import Store


pytestmark = [pytest.mark.db_pglite, pytest.mark.asyncio(loop_scope="session")]

ADA = "ada@example.com"


async def _insert(store: Store, *, read_group: UUID, write_group: UUID) -> UUID:
    group = uuid4()
    async with store.engine.acquire() as conn, tx(conn):
        await insert_access_group(
            conn,
            group,
            title="reviewers",
            account=ADA,
            read_group=read_group,
            write_group=write_group,
            system=True,
            domain="example.com",
            actor=ADA,
        )
    return group


async def test_an_inserted_group_holds_its_controls_and_its_created_change(
    store: Store,
) -> None:
    reader, writer = uuid4(), uuid4()

    group = await _insert(store, read_group=reader, write_group=writer)

    row = await store.get_inquiry(group)
    assert isinstance(row, AccessGroup)
    assert (row.title, row.account, row.seq) == ("reviewers", ADA, 1)
    assert (row.read_group, row.write_group, row.system, row.domain) == (
        reader,
        writer,
        True,
        "example.com",
    )
    changes = await store.list_changes(subject_id=group)
    assert [(change.kind, change.subject_kind) for change in changes] == [
        ("created", "AccessGroup"),
    ]


async def test_a_control_move_is_audited_once_per_changed_control(
    store: Store,
) -> None:
    reader, writer, moved = uuid4(), uuid4(), uuid4()
    group = await _insert(store, read_group=reader, write_group=writer)

    async with store.engine.acquire() as conn, tx(conn):
        changed = await set_access_group_controls(
            conn,
            group,
            read_group=moved,
            write_group=writer,
            actor=ADA,
        )
        again = await set_access_group_controls(
            conn,
            group,
            read_group=moved,
            write_group=writer,
            actor=ADA,
        )

    assert again == ()
    (change_id,) = changed
    change = await store.get_change(change_id)
    assert change is not None
    assert (change.kind, change.subject_kind) == (
        "accessgroup_read_group",
        "AccessGroup",
    )
    assert (change.old.accessgroup_read_group, change.new.accessgroup_read_group) == (
        reader,
        moved,
    )
    row = await store.get_inquiry(group)
    assert isinstance(row, AccessGroup)
    assert (row.read_group, row.write_group) == (moved, writer)


async def test_controls_move_only_on_an_existing_access_group(store: Store) -> None:
    issue = await store.submit_issue(SubmitIssue(account=ADA, title="not a group"))
    async with store.engine.acquire() as conn:
        with pytest.raises(ConflictError, match="not an AccessGroup"):
            await set_access_group_controls(
                conn,
                issue,
                read_group=uuid4(),
                write_group=uuid4(),
                actor=ADA,
            )
        with pytest.raises(NotFoundError):
            await set_access_group_controls(
                conn,
                uuid4(),
                read_group=uuid4(),
                write_group=uuid4(),
                actor=ADA,
            )


@pytest.mark.parametrize("turn", ["first", "second"])
async def test_each_test_meets_only_its_own_rows(store: Store, turn: str) -> None:
    group = await _insert(store, read_group=uuid4(), write_group=uuid4())

    async with store.engine.acquire() as conn:
        held = await conn.fetch("SELECT id, seq FROM inquiries")
        changes = await conn.fetchval("SELECT count(*) FROM change_log")
    assert [(row["id"], row["seq"]) for row in held] == [(group, 1)], turn
    assert changes == 1, turn


async def test_an_access_group_is_never_purged(store: Store) -> None:
    group = await _insert(store, read_group=uuid4(), write_group=uuid4())

    with pytest.raises(ConflictError, match="retired by status"):
        await store.purge(group, actor=ADA)

    assert await store.get_inquiry(group) is not None


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
