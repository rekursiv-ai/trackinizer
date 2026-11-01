"""The report migrations, 028 then 030, build the report tables a fresh bootstrap has."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.server.sql import load_sql


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_report_migration_matches_fresh_schema(
    pglite_rollback_conn: Conn,
) -> None:
    """Report tables and indexes match migrations 028 and 030 after bootstrap."""
    conn = pglite_rollback_conn
    tables = ["visual_reports", "visual_report_revisions"]
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1) ORDER BY table_name, ordinal_position"
    )
    indexes = (
        "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename = ANY($1) ORDER BY tablename, indexdef"
    )
    fresh_columns = [dict(row) for row in await conn.fetch(columns, tables)]
    fresh_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
    assert fresh_columns
    assert fresh_indexes
    await conn.execute("DROP TABLE visual_report_revisions")
    await conn.execute("DROP TABLE visual_reports")
    await conn.execute(load_sql("schema.028"))
    await conn.execute(load_sql("schema.030"))
    migrated_columns = [dict(row) for row in await conn.fetch(columns, tables)]
    migrated_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
    assert migrated_columns == fresh_columns
    assert migrated_indexes == fresh_indexes


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
