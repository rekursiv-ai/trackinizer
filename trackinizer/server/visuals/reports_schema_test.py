"""The report migration preserves the fresh bootstrap table shape."""

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
async def test_report_migration_matches_fresh_schema(
    pglite_engine: PGliteEngine,
) -> None:
    """Report tables and indexes match migration 028 after bootstrap."""
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
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
    async with pglite_engine.acquire() as conn:
        fresh_columns = [dict(row) for row in await conn.fetch(columns, tables)]
        fresh_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
        assert fresh_columns
        assert fresh_indexes
        await conn.execute("DROP TABLE visual_report_revisions")
        await conn.execute("DROP TABLE visual_reports")
        await conn.execute(load_sql("schema.028"))
        migrated_columns = [dict(row) for row in await conn.fetch(columns, tables)]
        migrated_indexes = [dict(row) for row in await conn.fetch(indexes, tables)]
    assert migrated_columns == fresh_columns
    assert migrated_indexes == fresh_indexes


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
