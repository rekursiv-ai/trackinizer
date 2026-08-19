"""Baseline schema invariants against a real Postgres database.

The schema is squashed to a single clean baseline (``assets/schema.sql``); there
are no numbered ``schema.NNN.sql`` migrations. What remains here are the
migration-independent properties that must hold for any fresh bootstrap: the
derived ``NON_NULLABLE_COLUMNS`` set matches what Postgres enforces, and the
``agent_session_events.kind`` CHECK rejects out-of-vocabulary kinds. Both run
against their own scratch database so they never touch the shared
``integ_engine``, and skip cleanly when the Postgres toolchain is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import uuid

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.server.notify import NOTIFY_CHANNEL
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_agent_session_events_kind_check_rejects_bogus_kind(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """A direct INSERT of an out-of-vocabulary ``kind`` is rejected by the CHECK.

    Without the constraint a bogus ``kind`` writes silently and 500s on read
    (``message_for_kind`` raises). The CHECK closes that hole at the DB.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        # A real AgentSession row to satisfy the session_id FK.
        sid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', 1, 'active', 'tester@example.com', 't')",
            sid,
        )
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await conn.execute(
                "INSERT INTO agent_session_events (session_id, seq, kind) "
                "VALUES ($1, 0, 'bogus')",
                sid,
            )
        # A valid kind still inserts.
        await conn.execute(
            "INSERT INTO agent_session_events (session_id, seq, kind) "
            "VALUES ($1, 1, 'UserMessage')",
            sid,
        )


if __name__ == "__main__":  # pragma: no cover -- entry point only.
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
