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

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import uuid

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.lib.codec import from_plain
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.sql import load_sql
from trackinizer.server.store.core import Store
from trackinizer.wire.filters import NON_NULLABLE_COLUMNS


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_engine(pg_dsn: str) -> AsyncIterator[postgres.PostgresEngine]:
    """Return a dedicated empty database, so schema surgery never hits shared tables."""
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
        dsn=f"{base}/{name}",
        listen_channel=NOTIFY_CHANNEL,
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
                "AND is_nullable = 'NO'",
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
async def test_session_ir_migrations_match_the_baseline_indexes(
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
        await conn.execute(load_sql("schema.029"))
        migrated = [dict(r) for r in await conn.fetch(indexes, _SESSION_IR_TABLES)]

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_recent_turn_index_exists_on_a_fresh_install(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Fresh bootstrap must include the index the numbered migration installs."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        index = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename = 'session_records' "
            "AND indexname = 'idx_session_records_recent_turns'",
        )
    assert index is not None


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
    millions of rows on a busy server.

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
        plan_rows = await conn.fetch(
            "EXPLAIN SELECT e.session_id, e.part, e.idx, e.created "
            "FROM session_records e JOIN inquiries i ON i.id = e.session_id "
            "WHERE i.kind = 'AgentSession' "
            "ORDER BY e.created, e.session_id, e.part, e.idx LIMIT 200",
        )
        plan_parts: list[str] = []
        for row in plan_rows:
            value = row["QUERY PLAN"]
            assert isinstance(value, str)
            plan_parts.append(value)
        plan = "\n".join(plan_parts)
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_022_then_023_matches_the_baseline_shape(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The session-index tables built by 022+023 equal the fresh-install ones.

    Bootstrap builds the baseline shape (dimension-free ``embedding`` + one
    partial HNSW per shipped model, from schema.sql). This drops the tables (as
    a pre-022 database lacks them), replays ``schema.022`` THEN ``schema.023`` --
    the full chain an existing database runs -- and compares columns AND indexes.
    Replaying 022 alone would (correctly) diverge: 023 is what widens the column
    and swaps the single global index for the per-model partials.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1::text[]) ORDER BY table_name, ordinal_position"
    )
    indexes = (
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename = ANY($1::text[]) ORDER BY indexdef"
    )
    tables = ["session_embeddings", "session_bodies"]
    async with scratch_engine.acquire() as conn:
        base_cols = [dict(r) for r in await conn.fetch(columns, tables)]
        base_idx = [str(r["indexdef"]) for r in await conn.fetch(indexes, tables)]
        assert {r["table_name"] for r in base_cols} == set(tables), (
            "the baseline did not create every session-index table"
        )
        # The baseline ships one partial HNSW per model; there must be several.
        assert (
            sum(1 for d in base_idx if "hnsw" in d) >= _SHIPPED_PARTIAL_INDEX_COUNT
        ), base_idx

        await conn.execute("DROP TABLE session_embeddings, session_bodies")
        await conn.execute(load_sql("schema.022"))
        await conn.execute(load_sql("schema.023"))
        migrated_cols = [dict(r) for r in await conn.fetch(columns, tables)]
        migrated_idx = [str(r["indexdef"]) for r in await conn.fetch(indexes, tables)]

    assert migrated_cols == base_cols
    assert set(migrated_idx) == set(base_idx)


# The seven partial HNSW indexes schema.023 + the baseline ship (six real models
# + the stub). The parity test asserts the baseline carries at least this many;
# an exact match is enforced by the column/index set-equality above.
_SHIPPED_PARTIAL_INDEX_COUNT = 7


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_023_preserves_existing_1024_rows_byte_for_byte(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Widening ``embedding`` to dimension-free ``halfvec`` keeps stored rows exact.

    A pre-023 database holds ``halfvec(1024)`` rows. Dropping the typmod is a
    metadata-only change, so an existing vector must read back byte-identical
    after 023 -- the guard against a migration that silently requantizes the
    corpus.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        # Rebuild the PRE-023 shape: halfvec(1024) + the single global index.
        await conn.execute("DROP TABLE session_embeddings")
        await conn.execute(load_sql("schema.022"))
        session_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'row survives 023')",
            session_id,
        )
        vector = "[" + ",".join("0.0123456" for _ in range(1024)) + "]"
        await conn.execute(
            "INSERT INTO session_embeddings "
            "(session_id, part, idx, field, chunk, mapper, model, embedding, "
            "text_md5) VALUES ($1, 0, 0, 'content', 0, 'footprint', "
            "'qwen3-embedding-4b@1024', $2, 'abc')",
            session_id,
            vector,
        )
        before = await conn.fetchval(
            "SELECT embedding::text FROM session_embeddings WHERE session_id = $1",
            session_id,
        )
        await conn.execute(load_sql("schema.023"))
        after = await conn.fetchval(
            "SELECT embedding::text FROM session_embeddings WHERE session_id = $1",
            session_id,
        )
    assert before == after


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_024_matches_the_baseline_shape(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The ``session_index_state`` table 024 builds equals the fresh-install one.

    Bootstrap builds the baseline shape (which carries ``session_index_state``).
    This drops it (as a pre-024 database lacks it), replays ``schema.024``, and
    compares columns AND indexes -- the parity check neither file can make about
    itself, since each is read by a disjoint population.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1::text[]) ORDER BY table_name, ordinal_position"
    )
    indexes = (
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename = ANY($1::text[]) ORDER BY indexdef"
    )
    tables = ["session_index_state"]
    async with scratch_engine.acquire() as conn:
        base_cols = [dict(r) for r in await conn.fetch(columns, tables)]
        base_idx = [str(r["indexdef"]) for r in await conn.fetch(indexes, tables)]
        assert {r["table_name"] for r in base_cols} == set(tables), (
            "the baseline did not create session_index_state"
        )

        await conn.execute("DROP TABLE session_index_state")
        await conn.execute(load_sql("schema.024"))
        migrated_cols = [dict(r) for r in await conn.fetch(columns, tables)]
        migrated_idx = [str(r["indexdef"]) for r in await conn.fetch(indexes, tables)]

    assert migrated_cols == base_cols
    assert set(migrated_idx) == set(base_idx)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_024_backfills_markers_from_existing_vectors(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """024 seeds a marker per existing embedding group so nothing re-embeds.

    A pre-024 database has ``session_embeddings`` rows but no marker table.
    After 024, every embedded record must read up-to-date -- one marker per
    ``(session_id, part, idx, mapper, model)`` group, carrying that group's md5.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    session_id = uuid.uuid4()
    async with scratch_engine.acquire() as conn:
        # Rebuild the PRE-024 state: embeddings present, no marker table.
        await conn.execute("DROP TABLE session_index_state")
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'marker backfill')",
            session_id,
        )
        vector = "[" + ",".join("0.01" for _ in range(1024)) + "]"
        # Two chunk units of ONE record share a single md5 -> one marker.
        for chunk in (0, 1):
            await conn.execute(
                "INSERT INTO session_embeddings "
                "(session_id, part, idx, field, chunk, mapper, model, embedding, "
                "text_md5) VALUES ($1, 0, 0, 'content', $2, 'footprint-v1', "
                "'qwen3-embedding-4b@1024', $3, 'sharedmd5')",
                session_id,
                chunk,
                vector,
            )
        await conn.execute(load_sql("schema.024"))
        markers = await conn.fetch(
            "SELECT part, idx, mapper, model, text_md5 FROM session_index_state "
            "WHERE session_id = $1",
            session_id,
        )
    assert len(markers) == 1
    assert markers[0]["text_md5"] == "sharedmd5"
    assert markers[0]["mapper"] == "footprint-v1"
    assert markers[0]["model"] == "qwen3-embedding-4b@1024"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_031_matches_the_baseline_metric_checks(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """A database migrated by 031 enforces the fresh install's metric-key CHECK.

    Bootstrap builds the baseline shape. This restores the pre-031 ``btrim``
    CHECK, replays ``schema.031`` alone, and compares every
    ``experiment_metrics`` constraint against what the baseline produced.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    constraints = (
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'experiment_metrics'::regclass"
    )
    async with scratch_engine.acquire() as conn:
        baseline = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
        await conn.execute(
            "ALTER TABLE experiment_metrics "
            "DROP CONSTRAINT experiment_metrics_key_check, "
            "ADD CONSTRAINT experiment_metrics_key_check "
            "CHECK (char_length(key) BETWEEN 1 AND 512 AND btrim(key) <> '')",
        )
        await conn.execute(load_sql("schema.031"))
        migrated = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_033_gives_an_old_canvas_chat_once_on_postgres(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The migration's JSON surgery works on Postgres, and is safe to run again."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    user, workspace = uuid.uuid4(), uuid.uuid4()
    browse = {
        "id": str(uuid.uuid4()),
        "type": "trax.browse",
        "version": 1,
        "placement": "main",
    }
    async with scratch_engine.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'old-canvas@example.com', 'O', 'writer', 'active')",
            user,
        )
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, revision, state) "
            "VALUES ($1, $2, 5, $3)",
            workspace,
            user,
            {"visuals": [browse]},
        )
        for expected_revision in (6, 6):
            await conn.execute(load_sql("schema.033"))
            row = await conn.fetchrow(
                "SELECT revision, (SELECT array_agg(v ->> 'type' || ':' || (v ->> 'placement')) "
                "FROM jsonb_array_elements(state -> 'visuals') AS v) AS visuals "
                "FROM visual_workspaces WHERE id = $1",
                workspace,
            )
            assert row is not None
            assert row["revision"] == expected_revision
            assert row["visuals"] == ["trax.browse:main", "trax.chat:floating"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_034_matches_the_baseline_variables_table(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The ``variables`` table 034 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-034
    database lacks it), replays ``schema.034`` alone, and compares every
    column, constraint and index against what the baseline produced.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'variables' ORDER BY ordinal_position"
    )
    constraints = (
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'variables'::regclass"
    )
    indexes = "SELECT indexdef FROM pg_indexes WHERE tablename = 'variables'"
    async with scratch_engine.acquire() as conn:
        baseline_cols = [dict(r) for r in await conn.fetch(columns)]
        baseline_cons = {
            (r["conname"], r["def"]) for r in await conn.fetch(constraints)
        }
        baseline_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
        assert baseline_cols, "the baseline did not create variables"

        await conn.execute("DROP TABLE variables")
        await conn.execute(load_sql("schema.034"))
        migrated_cols = [dict(r) for r in await conn.fetch(columns)]
        migrated_cons = {
            (r["conname"], r["def"]) for r in await conn.fetch(constraints)
        }
        migrated_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
        # Replaying it over the existing table is a no-op, not an error.
        await conn.execute(load_sql("schema.034"))

    assert migrated_cols == baseline_cols
    assert migrated_cons == baseline_cons
    assert migrated_idx == baseline_idx


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_variables_checks_refuse_a_secret_that_stores_its_value(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """A secret row cannot hold a value, a plain row must, and names are bounded."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    insert = (
        "INSERT INTO variables (layer, name, secret, value, updated_by) "
        "VALUES ('org', $1, $2, $3, 'tester')"
    )
    async with scratch_engine.acquire() as conn:
        await conn.execute(insert, "OK_NAME", True, None)
        for name, secret, value in (
            ("SECRET_WITH_VALUE", True, "leaked"),
            ("PLAIN_WITHOUT_VALUE", False, None),
            ("1BAD", False, "v"),
            ("A" * 129, False, "v"),
            ("a/b", False, "v"),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, name, secret, value)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_035_matches_the_baseline_machines_table(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The ``machines`` table 035 builds, then 036 extends, equals the fresh one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-035
    database lacks it), replays ``schema.035`` and ``schema.036``, and compares
    every column, constraint and index against what the baseline produced.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'machines' ORDER BY ordinal_position"
    )
    constraints = (
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'machines'::regclass"
    )
    indexes = "SELECT indexdef FROM pg_indexes WHERE tablename = 'machines'"
    async with scratch_engine.acquire() as conn:
        baseline_cols = [dict(r) for r in await conn.fetch(columns)]
        baseline_cons = {
            (r["conname"], r["def"]) for r in await conn.fetch(constraints)
        }
        baseline_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
        assert baseline_cols, "the baseline did not create machines"

        await conn.execute(
            "DROP TABLE machine_credentials, machine_enrollments, machines",
        )
        await conn.execute(load_sql("schema.035"))
        # The baseline holds 036's columns too, so they are added before comparing.
        await conn.execute(load_sql("schema.036"))
        migrated_cols = [dict(r) for r in await conn.fetch(columns)]
        migrated_cons = {
            (r["conname"], r["def"]) for r in await conn.fetch(constraints)
        }
        migrated_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
        # Replaying it over the existing table is a no-op, not an error.
        await conn.execute(load_sql("schema.035"))

    assert migrated_cols == baseline_cols
    assert migrated_cons == baseline_cons
    assert migrated_idx == baseline_idx


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_machines_checks_bound_names_roles_and_how(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Names, roles and the how line are bounded, and a name is unique."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    insert = (
        "INSERT INTO machines (name, role, how, updated_by) "
        "VALUES ($1, $2, $3, 'tester')"
    )
    async with scratch_engine.acquire() as conn:
        await conn.execute(insert, "dev-1", "", "")
        await conn.execute(insert, "a" * 63, "r" * 32, "h" * 2_000)
        await conn.execute(insert, "9lives", "gpu-2", "ssh in")
        for name, role, how in (
            ("dev-1", "", ""),
            ("Upper", "", ""),
            ("-lead", "", ""),
            ("a" * 64, "", ""),
            ("a/b", "", ""),
            ("ok", "Bad", ""),
            ("ok", "1st", ""),
            ("ok", "r" * 33, ""),
            ("ok", "", "h" * 2_001),
        ):
            with pytest.raises(
                (asyncpg.CheckViolationError, asyncpg.UniqueViolationError),
            ):
                await conn.execute(insert, name, role, how)
        row = await conn.fetchrow("SELECT id, labels, created FROM machines LIMIT 1")
    assert row is not None
    assert row["labels"] == []


_MACHINE_HOST_TABLES = ("machines", "machine_enrollments", "machine_credentials")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_036_matches_the_baseline_machine_host_tables(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The columns and tables 036 adds equal the fresh-install ones.

    Bootstrap builds the baseline shape. This drops the three tables (as a
    pre-035 database lacks them), replays ``schema.035`` then ``schema.036``,
    and compares every column, constraint and index of each table against what
    the baseline produced.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        baseline = {t: await _table_shape(conn, t) for t in _MACHINE_HOST_TABLES}
        assert baseline["machine_credentials"][0], "the baseline lacks the tables"

        await conn.execute(
            "DROP TABLE machine_credentials, machine_enrollments, machines",
        )
        await conn.execute(load_sql("schema.035"))
        await conn.execute(load_sql("schema.036"))
        migrated = {t: await _table_shape(conn, t) for t in _MACHINE_HOST_TABLES}
        # Replaying it over the existing tables is a no-op, not an error.
        await conn.execute(load_sql("schema.036"))

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_machine_host_checks_bound_each_column(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Each CHECK refuses its bad row, and one live credential per machine holds."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    digest = b"x" * 32
    async with scratch_engine.acquire() as conn:
        machine = await conn.fetchval(
            "INSERT INTO machines (name, updated_by) VALUES ('dev-1', 'tester') "
            "RETURNING id",
        )
        for update, value in (
            ("UPDATE machines SET host_version = $2 WHERE id = $1", "v" * 65),
            ("UPDATE machines SET facts = $2 WHERE id = $1", ["not", "an", "object"]),
            ("UPDATE machines SET facts = $2 WHERE id = $1", {"k": "v" * 8_200}),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(update, machine, value)
        for short_digest in (
            (
                "INSERT INTO machine_enrollments "
                "(machine_id, secret_sha256, created_by, expires_at) "
                "VALUES ($1, $2, 'tester', now())"
            ),
            (
                "INSERT INTO machine_credentials (machine_id, secret_sha256) "
                "VALUES ($1, $2)"
            ),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(short_digest, machine, b"short")
        insert = (
            "INSERT INTO machine_credentials (machine_id, secret_sha256, revoked_at) "
            "VALUES ($1, $2, $3)"
        )
        await conn.execute(insert, machine, digest, None)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(insert, machine, digest, None)
        # A revoked row does not count against the one live credential.
        await conn.execute(insert, machine, digest, datetime.now(UTC))
        await conn.execute(insert, machine, digest, datetime.now(UTC))


async def _table_shape(
    conn: postgres.Conn,
    table: str,
) -> tuple[list[dict[str, object]], set[tuple[str, str]], set[str]]:
    """Return one table's columns, constraints and indexes, as the catalog holds them."""
    columns = await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = $1 ORDER BY ordinal_position",
        table,
    )
    constraints = await conn.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = $1::regclass",
        table,
    )
    indexes = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = $1",
        table,
    )
    return (
        [dict(r) for r in columns],
        {
            (from_plain(r["conname"], str), from_plain(r["def"], str))
            for r in constraints
        },
        {from_plain(r["indexdef"], str) for r in indexes},
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.main import test_main

    test_main(__file__)
