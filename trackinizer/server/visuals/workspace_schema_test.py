"""The workspace migration has the same table shape as fresh bootstrap."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.sql import load_sql
from trackinizer.server.store.core import Store


if TYPE_CHECKING:
    from trackinizer.lib.postgres import PGliteEngine


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_migration_matches_fresh_schema(
    pglite_engine: PGliteEngine,
) -> None:
    """Migrated and fresh databases expose the same columns and indexes."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    tables = ["visual_workspaces", "visual_workspace_operations"]
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1) ORDER BY table_name, ordinal_position"
    )
    indexes = (
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY($1) "
        "ORDER BY tablename, indexdef"
    )
    preference_column = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_name = 'users' "
        "AND column_name = 'visual_workspace_enabled'"
    )
    async with pglite_engine.acquire() as conn:
        fresh_columns = [dict(row) for row in await conn.fetch(columns, tables)]
        fresh_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
        fresh_preference = await conn.fetchrow(preference_column)
        assert fresh_columns
        assert fresh_indexes
        assert fresh_preference is not None
        await conn.execute("DROP TABLE visual_workspace_operations")
        await conn.execute("DROP TABLE visual_workspaces")
        await conn.execute("ALTER TABLE users DROP COLUMN visual_workspace_enabled")
        await conn.execute(load_sql("schema.026"))
        migrated_columns = [dict(row) for row in await conn.fetch(columns, tables)]
        migrated_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
        migrated_preference = await conn.fetchrow(preference_column)
    assert migrated_columns == fresh_columns
    assert migrated_indexes == fresh_indexes
    assert migrated_preference is not None
    assert dict(migrated_preference) == dict(fresh_preference)


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
