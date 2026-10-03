"""Migration 032 leaves the full database catalog equal to fresh bootstrap."""

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
async def test_liveness_old_to_fresh_catalog_parity(
    pglite_engine: PGliteEngine,
) -> None:
    """An old database receiving migration 032 has the full fresh schema."""
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
        await conn.execute("DROP TABLE session_liveness")
        await conn.execute(
            "DELETE FROM applied_migrations WHERE name = 'schema.032.sql'",
        )
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = {tuple(row.values()) for row in await conn.fetch(catalog)}
        recorded = await conn.fetchval(
            "SELECT count(*) FROM applied_migrations WHERE name = 'schema.032.sql'",
        )
    assert migrated == fresh
    assert recorded == 1


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
