"""Additive migrations leave the full database catalog equal to fresh bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("old_shape", "migrations", "migration"),
    [
        (
            "DROP TABLE session_liveness",
            ["schema.032.sql", "schema.060.sql"],
            "schema.032.sql",
        ),
        (
            "ALTER TABLE gpu_jobs DROP COLUMN requested_node",
            ["schema.062.sql"],
            "schema.062.sql",
        ),
    ],
)
async def test_liveness_old_to_fresh_catalog_parity(
    pglite_engine: PGliteEngine,
    old_shape: str,
    migrations: list[str],
    migration: str,
) -> None:
    """An old database receiving its migration has the full fresh schema."""
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
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
    async with pglite_engine.acquire() as conn:
        fresh = {tuple(row.values()) for row in await conn.fetch(catalog)}
        assert any(row[1] == "session_liveness" for row in fresh)
        await conn.execute(old_shape)
        # 060 adds the table's later columns, so it runs again after 032 makes it.
        await conn.execute(
            "DELETE FROM applied_migrations WHERE name = ANY($1::text[])",
            migrations,
        )
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = {tuple(row.values()) for row in await conn.fetch(catalog)}
        recorded = await conn.fetchval(
            "SELECT count(*) FROM applied_migrations WHERE name = $1",
            migration,
        )
    assert migrated == fresh
    assert recorded == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
