"""Schema invariants against a real Postgres database.

Three properties, each needing a live server. The derived
``NON_NULLABLE_COLUMNS`` set matches what Postgres enforces; a CHECK rejects
an out-of-vocabulary value; and a database migrated by the numbered files ends
up with the same session-IR shape a fresh one gets from the baseline.

That last is the parity gate. A fresh install runs ``schema.sql`` and records
every numbered migration applied WITHOUT executing it, while an existing
database records the baseline unrun and executes only the numbered files --
so the two files are read by disjoint populations and can drift with nothing
noticing until a query hits the column one of them lacks.

Each runs against its own scratch database so none touches the shared
``integ_engine``, and all skip cleanly when the Postgres toolchain is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import uuid

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.sql import load_sql
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.wire.filters import NON_NULLABLE_COLUMNS


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_engine(pg_dsn: str) -> AsyncIterator[postgres.PostgresEngine]:
    """A dedicated empty database, so schema surgery never hits shared tables."""
    # Create a sibling DB on the same server as pg_dsn.
    base = pg_dsn.rsplit("/", 1)[0]
    name = "trackinizer_mig_gate"
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    setup = await asyncpg.connect(f"{base}/{name}")
    try:
        await setup.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await setup.close()
    async with postgres.PostgresEngine(
        dsn=f"{base}/{name}", listen_channel=NOTIFY_CHANNEL
    ) as engine:
        yield engine
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await admin.close()


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_non_nullable_columns_match_rendered_schema(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """``NON_NULLABLE_COLUMNS`` equals the actual NOT-NULL ``inquiries`` columns.

    The unit drift guard recomputes the set with the production rule, so it
    only catches a hardcoded edit -- not a column made NOT NULL in
    ``schema.sql`` without a matching ``required`` / ``flatten`` spec. This
    bootstraps the real schema and compares against ``information_schema`` so
    the derivation can't silently diverge from what Postgres actually
    enforces.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        schema_not_null = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'inquiries' "
                "AND is_nullable = 'NO'"
            )
        }
    assert set(NON_NULLABLE_COLUMNS) == schema_not_null


_SESSION_IR_TABLES = (
    "session_records",
    "session_manifests",
    "session_ciphertext",
    "session_slash_commands",
)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_019_matches_the_baseline_shape(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The migrated session-IR tables equal the ones a fresh install gets.

    Bootstrap builds the baseline shape. This drops the four tables (as a
    pre-019 database has them), replays ``schema.019.sql`` alone, and compares
    every column against what the baseline produced -- the check neither file
    can make about itself, since each is read by a different population.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1) ORDER BY table_name, ordinal_position"
    )
    async with scratch_engine.acquire() as conn:
        baseline = [dict(r) for r in await conn.fetch(columns, _SESSION_IR_TABLES)]
        assert baseline, "the baseline created no session IR tables"

        for table in _SESSION_IR_TABLES:
            await conn.execute(f"DROP TABLE {table}")
        await conn.execute(load_sql("schema.019"))
        migrated = [dict(r) for r in await conn.fetch(columns, _SESSION_IR_TABLES)]

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_019_matches_the_baseline_indexes(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Indexes match too, not just columns.

    Column parity is not shape parity. An index declared in one file and not
    the other leaves the two populations with the same rows and different
    performance -- and nothing fails, because every query still returns the
    right answer. That is the failure this catches: silent, and visible only
    as latency on whichever population got the thinner schema.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    indexes = (
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY($1) "
        "ORDER BY tablename, indexdef"
    )
    async with scratch_engine.acquire() as conn:
        baseline = [dict(r) for r in await conn.fetch(indexes, _SESSION_IR_TABLES)]
        assert baseline, "the baseline created no session IR indexes"

        for table in _SESSION_IR_TABLES:
            await conn.execute(f"DROP TABLE {table}")
        await conn.execute(load_sql("schema.019"))
        migrated = [dict(r) for r in await conn.fetch(indexes, _SESSION_IR_TABLES)]

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_console_feed_reads_an_index_not_the_whole_table(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The feed's order key is indexed, so polling does not sort the table.

    ``read_feed`` is a keyset scan over ``(created, session_id, part, idx)``
    polled every 1.5s by every open console. The retired
    ``agent_session_events`` carried a dedicated index for exactly this; the
    IR tables that replaced it must too, or the hot path degrades to a
    sequential scan plus a sort that grows with the whole capture corpus --
    3,081,202 rows on the deployed instance.

    Asserted against the PLANNER rather than by checking an index exists: an
    index the planner declines to use is not a fix.

    75,000 rows because that is where the choice becomes real. Measured on
    this schema: the planner takes a sequential scan at 5,001 and 25,002 rows
    (correctly -- sorting a small table beats walking an index) and switches
    to an index-only scan at 75,003. A fixture below the crossover asserts
    nothing about the schema, only about the fixture's size.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    session_id = uuid.uuid4()
    async with scratch_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'feed')",
            session_id,
        )
        await conn.execute(
            "INSERT INTO session_records "
            "(session_id, part, idx, kind, payload, text) "
            "SELECT $1, 0, g, 'UserMessage', '{}'::json, 'x' "
            "FROM generate_series(0, 75000) g",
            session_id,
        )
        await conn.execute("ANALYZE session_records")
        plan = "\n".join(
            r["QUERY PLAN"]
            for r in await conn.fetch(
                "EXPLAIN SELECT e.session_id, e.part, e.idx, e.created "
                "FROM session_records e JOIN inquiries i ON i.id = e.session_id "
                "WHERE i.kind = 'AgentSession' "
                "ORDER BY e.created, e.session_id, e.part, e.idx LIMIT 200"
            )
        )
    assert "idx_session_records_created_session_part_idx" in plan, (
        f"the console feed scans every record instead of its index:\n{plan}"
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_session_records_context_id_check_rejects_a_forward_reference(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """``context_id`` may name its own row but never a later one.

    A context applies to the records that FOLLOW it, so a record naming a
    higher idx would be reading settings that did not exist yet. ``<=`` rather
    than ``<`` because a claude TurnContext is appended at its own index and
    names itself.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        sid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', 2, 'active', 'tester@example.com', 't')",
            sid,
        )
        insert = (
            "INSERT INTO session_records (session_id, part, idx, kind, "
            "context_id, payload) VALUES ($1, 0, $2, 'TurnContext', $3, '{}')"
        )
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await conn.execute(insert, sid, 4, 5)
        # Naming itself is legal, and is what claude actually writes.
        await conn.execute(insert, sid, 6, 6)


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
