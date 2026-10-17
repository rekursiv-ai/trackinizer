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
from typing import TYPE_CHECKING, cast, get_args

import uuid

from treekle import from_plain

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.sql import load_sql
from trackinizer.server.store.core import Store
from trackinizer.types.change_log import Change
from trackinizer.wire.filters import NON_NULLABLE_COLUMNS


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import Conn, DatabaseEngine


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
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'feed', 'open')",
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
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', 2, 'active', 'tester@example.com', 't', 'open')",
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
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'row survives 023', 'open')",
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
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'tester@example.com', 'marker backfill', 'open')",
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
            "INSERT INTO visual_workspaces (id, user_id, station, revision, state) "
            'VALUES ($1, $2, \'{"name": "unnamed-workstream#1"}\', 5, $3)',
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_039_runs_and_changes_nothing(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """``schema.039`` executes on PGlite and leaves ``machines`` as it was.

    A fresh database records every migration without running it, so nothing
    else ever executes this file; a body of only comments fails under asyncpg.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'machines' "
        "ORDER BY ordinal_position"
    )
    async with scratch_engine.acquire() as conn:
        before = [r["column_name"] for r in await conn.fetch(columns)]
        await conn.execute(load_sql("schema.039"))
        after = [r["column_name"] for r in await conn.fetch(columns)]

    assert before
    assert after == before


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_040_matches_the_baseline_audience_columns(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The columns 040 adds to ``users`` and ``api_keys`` equal the fresh-install ones.

    Bootstrap builds the baseline shape. This drops the three columns (as a pre-040
    database lacks them) and the one 044 adds to ``users`` after them, replays
    ``schema.040`` then ``schema.044`` as a deployed database ran them, and compares
    every column, constraint and index of both tables against what the baseline
    produced.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        baseline = {t: await _table_shape(conn, t) for t in ("users", "api_keys")}
        assert {"hosted_domain", "google_sub"} <= {
            c["column_name"] for c in baseline["users"][0]
        }
        assert "open_into" in {c["column_name"] for c in baseline["api_keys"][0]}

        await conn.execute(
            "ALTER TABLE users DROP COLUMN hosted_domain, DROP COLUMN google_sub, "
            "DROP COLUMN preferences",
        )
        await conn.execute(
            "ALTER TABLE api_keys DROP COLUMN open_into",
        )
        await conn.execute(load_sql("schema.040"))
        await conn.execute(load_sql("schema.044"))
        migrated = {t: await _table_shape(conn, t) for t in ("users", "api_keys")}
        # Replaying it over the existing columns is a no-op, not an error.
        await conn.execute(load_sql("schema.040"))

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_audience_checks_bound_each_column(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """A bad shape is refused; absent and well-formed values are kept."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        user = await conn.fetchval(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES (gen_random_uuid(), 'a@x.y', 'a', 'writer', 'active') RETURNING id",
        )
        for domain in ("Example.com", "a" * 254):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE users SET hosted_domain = $2 WHERE id = $1",
                    user,
                    domain,
                )
        for domain in ("example.com", None):
            await conn.execute(
                "UPDATE users SET hosted_domain = $2 WHERE id = $1",
                user,
                domain,
            )
        for sub in ("", "s" * 256):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE users SET google_sub = $2 WHERE id = $1",
                    user,
                    sub,
                )
        for sub in ("1234567890", None):
            await conn.execute(
                "UPDATE users SET google_sub = $2 WHERE id = $1",
                user,
                sub,
            )
        insert = (
            "INSERT INTO api_keys (id, user_id, name, secret_hash, prefix, role, open_into) "
            "VALUES (gen_random_uuid(), $1, 'k', 'h', 'p', 'writer', $2)"
        )
        for pin in ("Staff", "", "a" * 33, "0open"):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert, user, pin)
        for pin in (None, "staff"):
            await conn.execute(insert, user, pin)


# Table-level constraints are unnamed in the baseline, so the catalog picks names the
# migration cannot predict; what must agree is what each one checks. An added column
# lands last, where the baseline puts it among the others, so columns are compared by
# name.
async def _inquiries_shape(
    conn: postgres.Conn,
) -> tuple[list[dict[str, object]], list[str], set[str]]:
    """Return ``inquiries``' columns and constraint definitions, names aside."""
    columns, constraints, indexes = await _table_shape(conn, "inquiries")
    return (
        sorted(columns, key=lambda column: str(column["column_name"])),
        sorted(definition for _, definition in constraints),
        indexes,
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_041_brings_a_pre_audience_database_to_the_baseline_shape(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """An existing database that applies 041 ends with the column a fresh one has.

    Bootstrap builds the baseline. This takes the audience column away again (its
    CHECK goes with it), leaves a session behind as such a database holds it, and lets
    the runner apply the migration. The session must come out ``open``, and every
    column and constraint of ``inquiries`` must equal the baseline's, and a replay of
    the file changes nothing.
    """
    store = Store(scratch_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with scratch_engine.acquire() as conn:
        baseline = await _inquiries_shape(conn)
        await conn.execute("ALTER TABLE inquiries DROP COLUMN agentsession_audience")
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES (gen_random_uuid(), 'AgentSession', 1, 'active', "
            "'tester@example.com', 'before')",
        )
        await conn.execute(
            "DELETE FROM applied_migrations WHERE name = 'schema.041.sql'",
        )

    await store.bootstrap()
    async with scratch_engine.acquire() as conn:
        migrated = await _inquiries_shape(conn)
        audience = await conn.fetchval(
            "SELECT agentsession_audience FROM inquiries WHERE title = 'before'",
        )
        await conn.execute(load_sql("schema.041"))
        replayed = await _inquiries_shape(conn)

    assert audience == "open"
    assert migrated == baseline
    assert replayed == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_audience_column_checks_the_shape_and_the_kind_of_row(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """A session needs a well-formed audience; no other row may hold one."""
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    session = (
        "INSERT INTO inquiries (id, kind, seq, status, account, title, "
        "agentsession_audience) VALUES (gen_random_uuid(), 'AgentSession', "
        "nextval('seq_agentsession'), 'active', 'a@x.y', 't', $1)"
    )
    issue = (
        "INSERT INTO inquiries (id, kind, seq, status, account, title, "
        "agentsession_audience) VALUES (gen_random_uuid(), 'Issue', "
        "nextval('seq_issue'), 'active', 'a@x.y', 't', $1)"
    )
    async with scratch_engine.acquire() as conn:
        for bad in (None, "", "Staff", "has space", "0open", "a" * 33, "open,staff"):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(session, bad)
        for good in ("open", "staff", "partner-1", "a" * 32):
            await conn.execute(session, good)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(issue, "open")
        await conn.execute(issue, None)


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


_CATALOG = (
    "SELECT 'table' AS kind, tablename AS name, '' AS definition "
    "FROM pg_tables WHERE schemaname = 'public' "
    "UNION ALL SELECT 'column', table_name || '.' || column_name, "
    "data_type || ':' || is_nullable || ':' || coalesce(column_default, '') "
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
"""Every table, column, constraint, index and sequence of the public schema."""


async def _make_stations_old(conn: Conn) -> None:
    """Turn a fresh database's stations back into 038's one-default-canvas shape."""
    await conn.execute(
        "ALTER TABLE visual_workspaces DROP COLUMN station, "
        "ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT TRUE, "
        "ADD COLUMN session_id UUID REFERENCES inquiries(id) ON DELETE SET NULL",
    )
    await conn.execute(
        "CREATE UNIQUE INDEX uq_visual_workspaces_default_user "
        "ON visual_workspaces (user_id) WHERE is_default",
    )
    await conn.execute(load_sql("schema.027"))
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.042.sql'")


async def _seed_old_stations(conn: Conn) -> dict[str, uuid.UUID]:
    """Give two users a canvas each, and presets that collide in every way."""
    ada, bo = uuid.uuid4(), uuid.uuid4()
    for user, email in ((ada, "ada@example.com"), (bo, "bo@example.com")):
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'T', 'writer', 'active')",
            user,
            email,
        )
    canvas, continuation = uuid.uuid4(), uuid.uuid4()
    for owner in (ada, bo):
        await conn.execute(
            "INSERT INTO visual_workspaces (id, user_id, revision, state) "
            "VALUES ($1, $2, 4, $3)",
            canvas if owner == ada else uuid.uuid4(),
            owner,
            {"visuals": []},
        )
    guided: dict[str, object] = {
        "visuals": [],
        "agent_instructions": "Compare the tails first.",
        "continuation_record_id": str(continuation),
    }
    empty: dict[str, object] = {"visuals": []}
    presets = (
        (ada, "Review", guided, 1),
        (ada, "Review", empty, 2),
        (ada, "unnamed-workstream#1", empty, 3),
        (ada, "x" * 120, empty, 4),
        (ada, "x" * 120, empty, 5),
        (bo, "Review", empty, 6),
    )
    for owner, name, state, day in presets:
        await conn.execute(
            "INSERT INTO visual_workspace_presets "
            "(id, user_id, name, state, created_at, modified_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $4)",
            owner,
            name,
            state,
            datetime(2026, 1, day, tzinfo=UTC),
        )
    return {"ada": ada, "bo": bo, "canvas": canvas, "continuation": continuation}


async def _check_stations_migrated(engine: DatabaseEngine) -> None:
    """Migrate 038's stations by bootstrap, and compare them with a fresh install."""
    await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn:
        fresh = {tuple(row.values()) for row in await conn.fetch(_CATALOG)}
        await _make_stations_old(conn)
        ids = await _seed_old_stations(conn)
    await Store(engine, embed=StubEmbedder()).bootstrap()
    async with engine.acquire() as conn:
        migrated = {tuple(row.values()) for row in await conn.fetch(_CATALOG)}
        ledger = await conn.fetchval(
            "SELECT count(*) FROM applied_migrations WHERE name = 'schema.042.sql'",
        )
        stations = await conn.fetch(
            "SELECT id, user_id, station->>'name' AS name, station->>'conversation_id' "
            "AS conversation_id, revision, state "
            "FROM visual_workspaces ORDER BY modified_at DESC, station->>'name'",
        )
        columns = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'visual_workspaces' ORDER BY ordinal_position",
        )
    assert fresh
    assert migrated == fresh
    assert ledger == 1
    # Trax's columns stay as they were; the station's fields are one optional JSON.
    assert [r["column_name"] for r in columns] == [
        "id",
        "user_id",
        "revision",
        "state",
        "created_at",
        "modified_at",
        "station",
    ]
    ada = sorted(
        from_plain(r["name"], str) for r in stations if r["user_id"] == ids["ada"]
    )
    assert ada == sorted(
        [
            "unnamed-workstream#1",
            "Review",
            "Review (2)",
            "unnamed-workstream#1 (2)",
            "x" * 120,
            "x" * 116 + " (2)",
        ],
    )
    assert [r["name"] for r in stations if r["user_id"] == ids["bo"]].count(
        "Review",
    ) == 1
    assert {r["conversation_id"] for r in stations} == {None}
    # The canvas stays the station the app opens, ahead of every converted view.
    [canvas] = [r for r in stations if r["id"] == ids["canvas"]]
    assert (canvas["name"], canvas["revision"]) == ("unnamed-workstream#1", 4)
    assert ids["canvas"] in {r["id"] for r in stations[:2]}
    kept = [r for r in stations if r["name"] == "Review" and r["user_id"] == ids["ada"]]
    state = from_plain(kept[0]["state"], dict[str, object])
    assert state["agent_instructions"] == "Compare the tails first."
    assert state["continuation_record_id"] == str(ids["continuation"])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_049_matches_the_baseline_shape(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The ``recorded`` columns 049 adds equal the fresh-install ones.

    Bootstrap builds the baseline shape (which carries them). This drops all
    three (as a pre-049 database lacks them), replays ``schema.049``, and
    compares columns -- the parity check neither file can make about itself,
    since each is read by a disjoint population.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name LIKE '%recorded' ORDER BY table_name, column_name"
    )
    async with scratch_engine.acquire() as conn:
        base_cols = [dict(r) for r in await conn.fetch(columns)]
        assert {str(r["table_name"]) for r in base_cols} == {
            "inquiries",
            "change_log",
        }, "the baseline did not create the recorded columns"

        await conn.execute("ALTER TABLE inquiries DROP COLUMN recorded")
        await conn.execute("ALTER TABLE change_log DROP COLUMN old_recorded")
        await conn.execute("ALTER TABLE change_log DROP COLUMN new_recorded")
        await conn.execute(load_sql("schema.049"))
        migrated_cols = [dict(r) for r in await conn.fetch(columns)]

    assert migrated_cols == base_cols


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_042_makes_stations_of_canvases_and_presets_on_pglite(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """Old canvases and saved views become stations, and the catalog equals fresh."""
    await reset_schema(pglite_engine)
    await _check_stations_migrated(pglite_engine)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_042_makes_stations_of_canvases_and_presets_on_postgres(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """The same on real Postgres, whose constraint and DO-block handling PGlite mirrors."""
    await _check_stations_migrated(scratch_engine)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_043_matches_the_baseline_science_chat_tables(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """The science chat tables 043 builds equal the fresh-install ones.

    Bootstrap builds the baseline shapes. This drops both tables (as a pre-043
    database lacks them), replays ``schema.043`` alone, and compares every column,
    constraint and index against what the baseline produced.
    """
    tables = ("science_chat_shares", "science_chat_tombstones")
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    async with pglite_engine.acquire() as conn:
        baseline = [await _table_shape(conn, table) for table in tables]
        assert all(shape[0] for shape in baseline), "the baseline lacks a table"

        await conn.execute("DROP TABLE science_chat_shares, science_chat_tombstones")
        await conn.execute(load_sql("schema.043"))
        migrated = [await _table_shape(conn, table) for table in tables]
        # Replaying it over the existing table is a no-op, not an error.
        await conn.execute(load_sql("schema.043"))
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO science_chat_shares (conversation_id, email, shared_by) "
                "VALUES ($1, 'Ada@Example.com', 'bob@example.com')",
                uuid.uuid4(),
            )

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_044_matches_the_baseline_users_table(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """The preferences column 044 adds equals the fresh-install one.

    Bootstrap builds the baseline. This drops the column (as a pre-044 database
    lacks it), replays ``schema.044`` alone, and compares every column, constraint
    and index of ``users`` against what the baseline produced.
    """
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    async with pglite_engine.acquire() as conn:
        baseline = await _table_shape(conn, "users")

        await conn.execute("ALTER TABLE users DROP COLUMN preferences")
        await conn.execute(load_sql("schema.044"))
        migrated = await _table_shape(conn, "users")
        # Replaying it over the existing column is a no-op, not an error.
        await conn.execute(load_sql("schema.044"))
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO users (id, email, name, role, status, preferences) "
                "VALUES ($1, 'ada@example.com', 'Ada', 'writer', 'active', $2::jsonb)",
                uuid.uuid4(),
                ["dark"],
            )

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_045_matches_the_baseline_credit_entries_table(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """The credit table 045 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-045 database
    lacks it), replays ``schema.045`` alone, and compares every column, constraint and
    index against what the baseline produced.
    """
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    async with pglite_engine.acquire() as conn:
        baseline = await _table_shape(conn, "credit_entries")
        assert baseline[0], "the baseline lacks the table"

        await conn.execute("DROP TABLE credit_entries")
        await conn.execute(load_sql("schema.045"))
        migrated = await _table_shape(conn, "credit_entries")
        # Replaying it over the existing table is a no-op, not an error.
        await conn.execute(load_sql("schema.045"))
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO credit_entries (kind, usd, ref, idem) "
                "VALUES ('purchase', 1, 'pi_1', 'evt_1')",
            )

    assert migrated == baseline


async def _catalog_names_aside(conn: Conn) -> list[tuple[str, str, str]]:
    """Return ``_CATALOG``, each constraint keyed by its table rather than its name."""
    rows = [
        (
            from_plain(r["kind"], str),
            from_plain(r["name"], str),
            from_plain(r["definition"], str),
        )
        for r in await conn.fetch(_CATALOG)
    ]
    return sorted(
        (kind, name.split(".")[0] if kind == "constraint" else name, definition)
        for kind, name, definition in rows
    )


# Nor is an audience move audited, which came after, in 047.
async def _make_app_old(conn: Conn) -> None:
    """Turn a fresh database back into 045's shape: no row has an ``app``."""
    await _make_audience_old(conn)
    # Dropping a column drops its CHECKs.
    await conn.execute("ALTER TABLE inquiries DROP COLUMN app")
    await conn.execute(
        "ALTER TABLE change_log "
        "DROP COLUMN old_app, DROP COLUMN new_app, "
        "DROP CONSTRAINT change_log_kind_check, "
        "ADD CONSTRAINT change_log_kind_check CHECK (kind IN ("
        f"{_change_kinds('app', 'agentsession_audience', 'recorded')}))",
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.046.sql'")


def _change_kinds(*left_out: str) -> str:
    """Return the change kinds but ``left_out``, quoted for a SQL ``IN`` list."""
    return ", ".join(
        f"'{kind}'"
        for kind in cast(tuple[str, ...], get_args(cast(object, Change.Kind.__value__)))
        if kind not in left_out
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_046_gives_an_existing_database_the_app_column(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """A database migrated by 046 has the catalog a fresh install has, names aside.

    Bootstrap builds the baseline. This takes ``app`` away again (its column, its
    two audit columns, their CHECKs and its change kind), and what 047 and 048 add
    after it, leaves an Issue and its ``created`` change behind as such a database
    holds them, and lets the runner apply the migrations. Every table, column,
    constraint, index and sequence must equal the baseline's, and a replay of the
    files changes nothing.
    """
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        fresh = await _catalog_names_aside(conn)
        await _make_app_old(conn)
        issue = await conn.fetchval(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES (gen_random_uuid(), 'Issue', 1, 'active', "
            "'tester@example.com', 'before') RETURNING id",
        )
        await conn.execute(
            "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind) "
            "VALUES (gen_random_uuid(), 'tester', $1, 'Issue', 'created')",
            issue,
        )

    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = await _catalog_names_aside(conn)
        app = await conn.fetchval("SELECT app FROM inquiries WHERE id = $1", issue)
        await conn.execute(load_sql("schema.046"))
        await conn.execute(load_sql("schema.047"))
        await conn.execute(load_sql("schema.048"))
        await conn.execute(load_sql("schema.049"))
        replayed = await _catalog_names_aside(conn)

    assert app is None
    assert migrated == fresh
    assert replayed == fresh


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_column_holds_one_object_of_at_most_64_kib_on_any_kind(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """An ``app`` is one JSON object of at most 65,536 bytes, on a row of any kind.

    ``{"b": "<n x>"}`` renders as n + 9 bytes, so n = 65,527 is the largest that fits.
    The audit columns hold the same shape, on a change to a row of any kind.
    """
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    inquiry = (
        "INSERT INTO inquiries (id, kind, seq, status, account, title, app) "
        "VALUES (gen_random_uuid(), $1, $2, 'active', 'a@x.y', 't', $3)"
    )
    change = (
        "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, "
        "old_app, new_app) "
        "VALUES (gen_random_uuid(), 'tester', gen_random_uuid(), $1, 'app', $2, $3)"
    )
    largest = {"b": "x" * 65_527}
    async with pglite_engine.acquire() as conn:
        for seq, bad in enumerate((["works"], "works", 7, {"b": "x" * 65_528}), 1):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(inquiry, "Issue", seq, bad)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(change, "Issue", None, bad)
        goods: tuple[tuple[str, dict[str, str] | None], ...] = (
            ("Issue", None),
            ("Artifact", {}),
            ("Experiment", largest),
        )
        for seq, (kind, good) in enumerate(goods, 1):
            await conn.execute(inquiry, kind, seq, good)
            await conn.execute(change, kind, {}, good)
        stored = await conn.fetchval(
            "SELECT max(octet_length(app::text)) FROM inquiries",
        )

    assert stored == 65_536


# Nor is there a code store, which came after, in 048, nor a declared ``recorded``,
# which came in 049.
async def _make_audience_old(conn: Conn) -> None:
    """Turn a fresh database back into 046's shape: no audience move is audited."""
    await _make_recorded_old(conn)
    await _make_code_store_old(conn)
    # Dropping a column drops the CHECKs that name it; the one tying the kind to the
    # subject names neither, so it is found by what it says.
    subject = from_plain(
        await conn.fetchval(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'change_log'::regclass "
            "AND pg_get_constraintdef(oid) LIKE '%kind <> ''agentsession_audience''%'",
        ),
        str,
    )
    await conn.execute(
        "ALTER TABLE change_log "
        "DROP COLUMN old_agentsession_audience, DROP COLUMN new_agentsession_audience, "
        f'DROP CONSTRAINT "{subject}", '
        "DROP CONSTRAINT change_log_kind_check, "
        "ADD CONSTRAINT change_log_kind_check CHECK (kind IN ("
        f"{_change_kinds('agentsession_audience', 'recorded')}))",
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.047.sql'")


async def _seed_chat_titles(conn: Conn) -> dict[str, uuid.UUID]:
    """Insert a chat titled by its line, another session, and one titled as chats are."""
    sessions = {name: uuid.uuid4() for name in ("chat", "other", "reference")}
    for name, title, cli in (
        ("chat", "my secret line", f"chat:{uuid.uuid4()}"),
        ("other", "my run", "codex-1"),
        ("reference", "Science chat", None),
    ):
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience, agentsession_cli_session_id) VALUES ($1, "
            "'AgentSession', nextval('seq_agentsession'), 'active', 'a@x.y', $2, "
            "'open', $3)",
            sessions[name],
            title,
            cli,
        )
    await conn.execute(
        "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, "
        "old_title, new_title) VALUES ($1, 'scout', $2, 'AgentSession', 'title', "
        "'first', 'my secret line')",
        uuid.uuid4(),
        sessions["chat"],
    )
    return sessions


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_047_audits_audience_and_retitles_chats(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """A database 047 migrates has a fresh catalog, and no chat's line on its row.

    Bootstrap builds the baseline and embeds the titles as they stand, a chat's first
    line among them. This then takes the audience audit away again (its two columns,
    their CHECKs and its change kind), and the code store 048 adds after it, and lets
    the runner apply the migrations. Every table, column, constraint, index and
    sequence must equal the baseline's, a replay of the files must change nothing,
    and the chat comes out ``Science chat``: its row, the title change the log kept
    for it, and its embedding, which then equals that of another session so titled.
    No other row changes.
    """
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        fresh = await _catalog_names_aside(conn)
        sessions = await _seed_chat_titles(conn)
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        await _make_audience_old(conn)

    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = await _catalog_names_aside(conn)
        titles = await conn.fetch(
            "SELECT id, title FROM inquiries WHERE id = ANY($1::uuid[]) ORDER BY title",
            [sessions["chat"], sessions["other"]],
        )
        logged = await conn.fetchrow(
            "SELECT old_title, new_title FROM change_log WHERE subject_id = $1",
            sessions["chat"],
        )
        same = await conn.fetchval(
            "SELECT bool_and(mine.embedding = theirs.embedding) "
            "FROM inquiry_embeddings AS mine JOIN inquiry_embeddings AS theirs "
            "ON theirs.model = mine.model AND theirs.inquiry_id = $2 "
            "WHERE mine.inquiry_id = $1",
            sessions["chat"],
            sessions["reference"],
        )
        await conn.execute(load_sql("schema.047"))
        await conn.execute(load_sql("schema.048"))
        await conn.execute(load_sql("schema.049"))
        replayed = await _catalog_names_aside(conn)

    assert migrated == fresh
    assert replayed == fresh
    assert [(row["id"], row["title"]) for row in titles] == [
        (sessions["chat"], "Science chat"),
        (sessions["other"], "my run"),
    ]
    assert logged is not None
    assert (logged["old_title"], logged["new_title"]) == ("", "")
    assert same is True


async def _make_recorded_old(conn: Conn) -> None:
    """Turn a fresh database back into 048's shape: no row declares ``recorded``."""
    # Dropping a column drops its CHECKs.
    await conn.execute("ALTER TABLE inquiries DROP COLUMN recorded")
    await conn.execute(
        "ALTER TABLE change_log "
        "DROP COLUMN old_recorded, DROP COLUMN new_recorded, "
        "DROP CONSTRAINT change_log_kind_check, "
        "ADD CONSTRAINT change_log_kind_check CHECK (kind IN ("
        f"{_change_kinds('recorded')}))",
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.049.sql'")


async def _make_code_store_old(conn: Conn) -> None:
    """Turn a fresh database back into 047's shape: no code store."""
    await conn.execute("DROP TABLE code_snapshots, code_blobs")
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.048.sql'")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_048_gives_an_existing_database_the_code_store(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """A database migrated by 048 has the catalog a fresh install has.

    Bootstrap builds the baseline. This drops both code tables again, as a 047
    database lacks them, and lets the runner apply the migration. Every table, column,
    constraint, index and sequence must equal the baseline's, by name too, and a
    replay of the file changes nothing.
    """
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    tables = ("code_blobs", "code_snapshots")
    async with pglite_engine.acquire() as conn:
        fresh = await _catalog_names_aside(conn)
        baseline = [await _table_shape(conn, table) for table in tables]
        await _make_code_store_old(conn)

    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = await _catalog_names_aside(conn)
        shapes = [await _table_shape(conn, table) for table in tables]
        await conn.execute(load_sql("schema.048"))
        replayed = await _catalog_names_aside(conn)

    assert all(shape[0] for shape in baseline), "the baseline lacks a code table"
    assert migrated == fresh
    assert shapes == baseline
    assert replayed == fresh


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_code_tables_check_owner_hash_size_and_files(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """A blob's owner is a lowercase address, its hash 32 bytes, its size at most 1 MiB.

    A snapshot's owner is the same, and its files are one JSON object.
    """
    await reset_schema(pglite_engine)
    await Store(pglite_engine, embed=StubEmbedder()).bootstrap()
    blob = (
        "INSERT INTO code_blobs (owner, sha256, size, data_zst) VALUES ($1, $2, $3, '')"
    )
    snapshot = (
        "INSERT INTO code_snapshots (record_id, owner, files, file_count, total_bytes) "
        "VALUES ($1, $2, $3, 0, 0)"
    )
    async with pglite_engine.acquire() as conn:
        record = await conn.fetchval(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) VALUES "
            "(gen_random_uuid(), 'Experiment', 1, 'active', 'a@x.y', 't') RETURNING id",
        )
        for owner, sha256, size in (
            ("Ada@x.y", bytes(32), 0),
            ("", bytes(32), 0),
            ("ada@x.y", bytes(31), 0),
            ("ada@x.y", bytes(32), 1_048_577),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(blob, owner, sha256, size)
        await conn.execute(blob, "ada@x.y", bytes(32), 1_048_576)
        bad_files: tuple[tuple[str, object], ...] = (
            ("Ada@x.y", {}),
            ("ada@x.y", ["a.py"]),
        )
        for owner, files in bad_files:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(snapshot, record, owner, files)
        await conn.execute(snapshot, record, "ada@x.y", {})


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_049_gates_the_mirror_on_the_change_kind(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """049's validated gates refuse a mirror written under the wrong kind.

    The fresh install states this as an unnamed CHECK inside CREATE TABLE and
    049 as a named one it can VALIDATE, so the rule is worth asserting rather
    than assumed from two spellings of it.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        await conn.execute("ALTER TABLE inquiries DROP COLUMN recorded")
        await conn.execute("ALTER TABLE change_log DROP COLUMN old_recorded")
        await conn.execute("ALTER TABLE change_log DROP COLUMN new_recorded")
        await conn.execute(load_sql("schema.049"))

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, "
                "new_recorded) VALUES ($1, 'tester', $2, 'Issue', 'title', now())",
                uuid.uuid4(),
                uuid.uuid4(),
            )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_049_gives_an_existing_database_the_recorded_kind(
    pglite_engine: postgres.PGliteEngine,
) -> None:
    """A database migrated by 049 has the catalog a fresh install has, names aside.

    Bootstrap builds the baseline. This takes ``recorded`` away again (its column, its
    two audit columns and its change kind) and lets the runner apply the migration.
    Every table, column, constraint, index and sequence must equal the baseline's, and
    the change log then takes a ``recorded`` edit, which the kind list would refuse if
    049 left it as 048 had it.
    """
    await reset_schema(pglite_engine)
    store = Store(pglite_engine, embed=StubEmbedder())
    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        fresh = await _catalog_names_aside(conn)
        await _make_recorded_old(conn)

    await store.bootstrap()
    async with pglite_engine.acquire() as conn:
        migrated = await _catalog_names_aside(conn)
        await conn.execute(
            "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, "
            "new_recorded) VALUES ($1, 'tester', $2, 'Issue', 'recorded', now())",
            uuid.uuid4(),
            uuid.uuid4(),
        )

    assert migrated == fresh


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
