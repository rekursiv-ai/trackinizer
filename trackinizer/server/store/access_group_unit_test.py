"""Unit contracts for access-group writes on the caller's connection."""

from __future__ import annotations

import pytest

from trackinizer.conftest import make_conn, make_store, new_uuid, set_field_row
from trackinizer.server.store.access_group import (
    insert_access_group,
    set_access_group_controls,
)
from trackinizer.types.errors import ConflictError, NotFoundError


@pytest.mark.asyncio
async def test_insert_writes_controls_and_a_created_audit() -> None:
    conn = make_conn()
    store, _ = make_store(conn)
    group, reader, writer, key = (new_uuid() for _ in range(4))
    async with store.engine.acquire() as held:
        await insert_access_group(
            held,
            group,
            title="reviewers",
            account="ada@example.com",
            read_group=reader,
            write_group=writer,
            system=True,
            domain="example.com",
            actor="ada",
            api_key_id=key,
        )
    insert, audit = conn.execute.call_args_list
    columns = str(insert.args[0]).split("(", 1)[1].split(")", 1)[0].split(", ")
    values = dict(zip(columns[4:], insert.args[4:], strict=True))
    assert insert.args[1:4] == (group, "AccessGroup", None)
    assert values["accessgroup_read_group"] == reader
    assert values["accessgroup_write_group"] == writer
    assert values["accessgroup_system"] is True
    assert values["accessgroup_domain"] == "example.com"
    assert audit.args[2:7] == (key, "ada", group, "AccessGroup", "created")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_controls_refuse_missing_and_wrong_kind(*, missing: bool) -> None:
    conn = make_conn()
    set_field_row(conn, None if missing else {"kind": "Issue"})
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        with pytest.raises(NotFoundError if missing else ConflictError):
            await set_access_group_controls(
                held,
                new_uuid(),
                read_group=new_uuid(),
                write_group=new_uuid(),
                actor="ada",
            )
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("move_write", [False, True])
async def test_only_moved_controls_are_audited_in_read_write_order(
    *,
    move_write: bool,
) -> None:
    conn = make_conn()
    group, old_reader, old_writer, reader, writer = (new_uuid() for _ in range(5))
    set_field_row(
        conn,
        {
            "kind": "AccessGroup",
            "accessgroup_read_group": old_reader,
            "accessgroup_write_group": old_writer,
        },
    )
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        changed = await set_access_group_controls(
            held,
            group,
            read_group=reader,
            write_group=writer if move_write else old_writer,
            actor="ada",
        )
    audits = [c for c in conn.execute.call_args_list if "change_log" in str(c.args[0])]
    assert changed == tuple(c.args[1] for c in audits)
    assert [c.args[6] for c in audits] == (
        ["accessgroup_read_group", "accessgroup_write_group"]
        if move_write
        else ["accessgroup_read_group"]
    )
    for audit, before, after in zip(
        audits,
        (old_reader, old_writer),
        (reader, writer),
        strict=False,
    ):
        columns = str(audit.args[0]).split("(", 1)[1].split(")", 1)[0].split(", ")
        logged = dict(zip(columns, audit.args[1:], strict=True))
        assert logged[f"old_{audit.args[6]}"] == before
        assert logged[f"new_{audit.args[6]}"] == after


@pytest.mark.asyncio
async def test_unchanged_controls_emit_nothing() -> None:
    conn = make_conn()
    reader, writer = new_uuid(), new_uuid()
    set_field_row(
        conn,
        {
            "kind": "AccessGroup",
            "accessgroup_read_group": reader,
            "accessgroup_write_group": writer,
        },
    )
    store, _ = make_store(conn)
    async with store.engine.acquire() as held:
        assert (
            await set_access_group_controls(
                held,
                new_uuid(),
                read_group=reader,
                write_group=writer,
                actor="ada",
            )
            == ()
        )
    conn.execute.assert_not_awaited()


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
