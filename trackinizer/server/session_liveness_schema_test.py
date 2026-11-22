"""Additive migrations leave the full database catalog equal to fresh bootstrap.

Each case runs its migration steps on the session's build inside a transaction rolled
back after it (``pglite_rollback_conn``), so the next test meets the build untouched.
That the runner applies an unrecorded migration and records it once, which this file
once checked by booting a store, is ``core_test``'s
``test_bootstrap_skips_baseline_for_existing_db``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.server.sql import load_sql


if TYPE_CHECKING:
    from trackinizer.lib.postgres import Conn


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("old_shape", "migrations"),
    [
        # 060 names the machine tables as 075 had them, which 076 renamed.
        (
            ("host_rename_down", "DROP TABLE session_liveness"),
            ["schema.032", "schema.060", "schema.076"],
        ),
        (("ALTER TABLE gpu_jobs DROP COLUMN requested_node",), ["schema.062"]),
    ],
)
async def test_liveness_old_to_fresh_catalog_parity(
    pglite_rollback_conn: Conn,
    old_shape: tuple[str, ...],
    migrations: list[str],
) -> None:
    """An old database receiving its migration has the full fresh schema."""
    conn = pglite_rollback_conn
    catalog = (
        "SELECT 'table' AS kind, tablename AS name, '' AS definition "
        "FROM pg_tables WHERE schemaname = 'public' "
        "UNION ALL SELECT 'column', table_name || '.' || column_name, "
        "data_type || ':' || is_nullable || ':' || coalesce(column_default, '') AS definition "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "UNION ALL SELECT 'constraint', relation.relname || '.' || con.conname, "
        "regexp_replace(pg_get_constraintdef(con.oid), '\\s+', ' ', 'g') "
        "FROM pg_constraint AS con "
        "JOIN pg_class AS relation ON relation.oid = con.conrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "WHERE namespace.nspname = 'public' "
        "UNION ALL SELECT 'index', tablename || '.' || indexname, indexdef "
        "FROM pg_indexes WHERE schemaname = 'public' "
        "UNION ALL SELECT 'sequence', relation.relname, '' "
        "FROM pg_class AS relation "
        "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
        "WHERE namespace.nspname = 'public' AND relation.relkind = 'S'"
    )
    fresh = {tuple(row.values()) for row in await conn.fetch(catalog)}
    assert any(row[1] == "session_liveness" for row in fresh)
    # A bare name is an asset; anything else is a statement.
    for step in old_shape:
        await conn.execute(load_sql(step) if " " not in step else step)
    # 060 adds the table's later columns, so it runs again after 032 makes it.
    for migration in migrations:
        await conn.execute(load_sql(migration))
    migrated = {tuple(row.values()) for row in await conn.fetch(catalog)}
    assert migrated == fresh


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
