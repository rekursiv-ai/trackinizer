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

Each migration test is a unit test of its steps: it starts from the session's build,
takes away what the steps add, runs their files itself, and compares with the build,
all inside a transaction rolled back after it (``integ_rollback_conn``,
``pglite_rollback_conn``), so the next test meets the build untouched. A CHECK test
writes to the build itself (``integ_store``, ``pglite_db``). All skip cleanly when the
Postgres toolchain is absent.

Where a test once let a booting store apply its migrations, two of its claims moved
to units of the runner in ``store/core_test.py``. That a boot applies an unrecorded
migration and records it once (042's ``ledger == 1``, and the boot that applied 041,
042, 046 to 051 and 059, and migrated again after the rollback) is
``test_bootstrap_skips_baseline_for_existing_db``. That a boot embeds a row its
migration left without an embedding (047's chat and 059's Scratch coming to share
the embedding of a row so titled) is ``test_bootstrap_backfills_missing_embeddings``
with ``TestStubEmbedder.test_embed_deterministic``; here each migration is shown to
take that embedding away.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast, get_args

import uuid

from treekle import from_plain

import asyncpg
import pytest

from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.primitives import insert_inquiry, upsert_embedding
from trackinizer.server.sql import load_sql, schema_migrations
from trackinizer.types.change_log import Change
from trackinizer.wire.filters import NON_NULLABLE_COLUMNS


if TYPE_CHECKING:
    from collections.abc import Mapping

    from trackinizer.lib.postgres import Conn, PGliteEngine
    from trackinizer.server.store.core import Store


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_non_nullable_columns_match_rendered_schema(integ_store: Store) -> None:
    """``NON_NULLABLE_COLUMNS`` equals the actual NOT-NULL ``inquiries`` columns.

    The unit drift guard recomputes the set with the production rule, so it
    only catches a hardcoded edit -- not a column made NOT NULL in
    ``schema.sql`` without a matching ``required`` / ``flatten`` spec. This
    bootstraps the real schema and compares against ``information_schema`` so
    the derivation can't silently diverge from what Postgres actually
    enforces.
    """
    async with integ_store.engine.acquire() as conn:
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
    integ_rollback_conn: Conn,
) -> None:
    """The migrated session-IR tables equal the ones a fresh install gets.

    Bootstrap builds the baseline shape. This drops the four tables (as a
    pre-019 database has them), replays ``schema.019.sql`` and the column 051 adds
    after it, and compares every column against what the baseline produced -- the
    check neither file can make about itself, since each is read by a different
    population.
    """
    conn = integ_rollback_conn
    columns = (
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = ANY($1) ORDER BY table_name, ordinal_position"
    )
    baseline = [dict(r) for r in await conn.fetch(columns, _SESSION_IR_TABLES)]
    assert baseline, "the baseline created no session IR tables"

    for table in _SESSION_IR_TABLES:
        await conn.execute(f"DROP TABLE {table}")
    await conn.execute(load_sql("schema.019"))
    await conn.execute(load_sql("schema.051"))
    migrated = [dict(r) for r in await conn.fetch(columns, _SESSION_IR_TABLES)]

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_session_ir_migrations_match_the_baseline_indexes(
    integ_rollback_conn: Conn,
) -> None:
    """Indexes match too, not just columns.

    Column parity is not shape parity. An index declared in one file and not
    the other leaves the two populations with the same rows and different
    performance -- and nothing fails, because every query still returns the
    right answer. That is the failure this catches: silent, and visible only
    as latency on whichever population got the thinner schema.
    """
    conn = integ_rollback_conn
    indexes = (
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY($1) "
        "ORDER BY tablename, indexdef"
    )
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
async def test_recent_turn_index_exists_on_a_fresh_install(integ_store: Store) -> None:
    """Fresh bootstrap must include the index the numbered migration installs."""
    async with integ_store.engine.acquire() as conn:
        index = await conn.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename = 'session_records' "
            "AND indexname = 'idx_session_records_recent_turns'",
        )
    assert index is not None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_console_feed_reads_an_index_not_the_whole_table(
    integ_rollback_conn: Conn,
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
    conn = integ_rollback_conn
    session_id = uuid.uuid4()
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
    integ_store: Store,
) -> None:
    """``context_id`` may name its own row but never a later one.

    A context applies to the records that FOLLOW it, so a record naming a
    higher idx would be reading settings that did not exist yet. ``<=`` rather
    than ``<`` because a claude TurnContext is appended at its own index and
    names itself.
    """
    async with integ_store.engine.acquire() as conn:
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
    integ_rollback_conn: Conn,
) -> None:
    """The session-index tables built by 022+023 equal the fresh-install ones.

    Bootstrap builds the baseline shape (dimension-free ``embedding`` + one
    partial HNSW per shipped model, from schema.sql). This drops the tables (as
    a pre-022 database lacks them), replays ``schema.022`` THEN ``schema.023`` --
    the full chain an existing database runs -- and compares columns AND indexes.
    Replaying 022 alone would (correctly) diverge: 023 is what widens the column
    and swaps the single global index for the per-model partials.
    """
    conn = integ_rollback_conn
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
    base_cols = [dict(r) for r in await conn.fetch(columns, tables)]
    base_idx = [str(r["indexdef"]) for r in await conn.fetch(indexes, tables)]
    assert {r["table_name"] for r in base_cols} == set(tables), (
        "the baseline did not create every session-index table"
    )
    # The baseline ships one partial HNSW per model; there must be several.
    assert sum(1 for d in base_idx if "hnsw" in d) >= _SHIPPED_PARTIAL_INDEX_COUNT, (
        base_idx
    )

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
    integ_rollback_conn: Conn,
) -> None:
    """Widening ``embedding`` to dimension-free ``halfvec`` keeps stored rows exact.

    A pre-023 database holds ``halfvec(1024)`` rows. Dropping the typmod is a
    metadata-only change, so an existing vector must read back byte-identical
    after 023 -- the guard against a migration that silently requantizes the
    corpus.
    """
    conn = integ_rollback_conn
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
    integ_rollback_conn: Conn,
) -> None:
    """The ``session_index_state`` table 024 builds equals the fresh-install one.

    Bootstrap builds the baseline shape (which carries ``session_index_state``).
    This drops it (as a pre-024 database lacks it), replays ``schema.024``, and
    compares columns AND indexes -- the parity check neither file can make about
    itself, since each is read by a disjoint population.
    """
    conn = integ_rollback_conn
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
    integ_rollback_conn: Conn,
) -> None:
    """024 seeds a marker per existing embedding group so nothing re-embeds.

    A pre-024 database has ``session_embeddings`` rows but no marker table.
    After 024, every embedded record must read up-to-date -- one marker per
    ``(session_id, part, idx, mapper, model)`` group, carrying that group's md5.
    """
    conn = integ_rollback_conn
    session_id = uuid.uuid4()
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
    integ_rollback_conn: Conn,
) -> None:
    """A database migrated by 031 enforces the fresh install's metric-key CHECK.

    Bootstrap builds the baseline shape. This restores the pre-031 ``btrim``
    CHECK, replays ``schema.031`` alone, and compares every
    ``experiment_metrics`` constraint against what the baseline produced.
    """
    conn = integ_rollback_conn
    constraints = (
        "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conrelid = 'experiment_metrics'::regclass"
    )
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
    integ_rollback_conn: Conn,
) -> None:
    """The migration's JSON surgery works on Postgres, and is safe to run again."""
    conn = integ_rollback_conn
    user, canvas = uuid.uuid4(), uuid.uuid4()
    browse = {
        "id": str(uuid.uuid4()),
        "type": "trax.browse",
        "version": 1,
        "placement": "main",
    }
    await conn.execute(
        "INSERT INTO users (id, email, name, role, status) "
        "VALUES ($1, 'old-canvas@example.com', 'O', 'writer', 'active')",
        user,
    )
    await conn.execute(
        "INSERT INTO visual_canvases (id, user_id, metadata, revision, state) "
        'VALUES ($1, $2, \'{"name": "unnamed-canvas#1"}\', 5, $3)',
        canvas,
        user,
        {"visuals": [browse]},
    )
    for expected_revision in (6, 6):
        await conn.execute(load_sql("canvas_rename_down"))
        await conn.execute(load_sql("schema.033"))
        await conn.execute(load_sql("schema.058"))
        row = await conn.fetchrow(
            "SELECT revision, (SELECT array_agg(v ->> 'type' || ':' || (v ->> 'placement')) "
            "FROM jsonb_array_elements(state -> 'visuals') AS v) AS visuals "
            "FROM visual_canvases WHERE id = $1",
            canvas,
        )
        assert row is not None
        assert row["revision"] == expected_revision
        assert row["visuals"] == ["trax.browse:main", "trax.chat:floating"]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_034_matches_the_baseline_variables_table(
    integ_rollback_conn: Conn,
) -> None:
    """The ``variables`` table 034 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-034
    database lacks it), replays ``schema.034`` alone, and compares every
    column, constraint and index against what the baseline produced.
    """
    conn = integ_rollback_conn
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
    baseline_cols = [dict(r) for r in await conn.fetch(columns)]
    baseline_cons = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
    baseline_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
    assert baseline_cols, "the baseline did not create variables"

    await conn.execute("DROP TABLE variables")
    await conn.execute(load_sql("schema.034"))
    migrated_cols = [dict(r) for r in await conn.fetch(columns)]
    migrated_cons = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
    migrated_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.034"))

    assert migrated_cols == baseline_cols
    assert migrated_cons == baseline_cons
    assert migrated_idx == baseline_idx


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_variables_checks_refuse_a_secret_that_stores_its_value(
    integ_store: Store,
) -> None:
    """A secret row cannot hold a value, a plain row must, and names are bounded."""
    insert = (
        "INSERT INTO variables (layer, name, secret, value, updated_by) "
        "VALUES ('org', $1, $2, $3, 'tester')"
    )
    async with integ_store.engine.acquire() as conn:
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
    integ_rollback_conn: Conn,
) -> None:
    """The ``machines`` table 035 builds, then later migrations equal the fresh one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-035
    database lacks it), replays 035, 036, 049 and 052, and compares every column,
    constraint and index against what the baseline produced.
    """
    conn = integ_rollback_conn
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
    baseline_cols = [dict(r) for r in await conn.fetch(columns)]
    baseline_cons = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
    baseline_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
    assert baseline_cols, "the baseline did not create machines"

    # ``machine_jobs`` (057) and ``api_keys.machine_id`` (060) refer to ``machines``,
    # so they go first and come back.
    await conn.execute("ALTER TABLE api_keys DROP COLUMN machine_id")
    await conn.execute(
        "DROP TABLE machine_jobs, machine_credentials, machine_enrollments, machines",
    )
    await conn.execute(load_sql("schema.035"))
    # The baseline holds later migrations' columns too, so they are added before
    # comparing.
    await conn.execute(load_sql("schema.036"))
    await conn.execute(load_sql("schema.049"))
    await conn.execute(load_sql("schema.052"))
    await conn.execute(load_sql("schema.057"))
    await conn.execute(load_sql("schema.060"))
    migrated_cols = [dict(r) for r in await conn.fetch(columns)]
    migrated_cons = {(r["conname"], r["def"]) for r in await conn.fetch(constraints)}
    migrated_idx = {str(r["indexdef"]) for r in await conn.fetch(indexes)}
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.035"))

    assert migrated_cols == baseline_cols
    assert migrated_cons == baseline_cons
    assert migrated_idx == baseline_idx


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_machines_checks_bound_names_roles_and_how(integ_store: Store) -> None:
    """Names, roles and the how line are bounded, and a name is unique."""
    insert = (
        "INSERT INTO machines (name, role, how, updated_by) "
        "VALUES ($1, $2, $3, 'tester')"
    )
    async with integ_store.engine.acquire() as conn:
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
    integ_rollback_conn: Conn,
) -> None:
    """The machine-host tables after later migrations equal the fresh-install ones.

    Bootstrap builds the baseline shape. This drops the three tables (as a
    pre-035 database lacks them), replays 035, 036, 049 and 052,
    and compares every column, constraint and index of each table against what
    the baseline produced.
    """
    conn = integ_rollback_conn
    baseline = {t: await _table_shape(conn, t) for t in _MACHINE_HOST_TABLES}
    assert baseline["machine_credentials"][0], "the baseline lacks the tables"

    # ``machine_jobs`` (057) and ``api_keys.machine_id`` (060) refer to ``machines``,
    # so they go first and come back.
    await conn.execute("ALTER TABLE api_keys DROP COLUMN machine_id")
    await conn.execute(
        "DROP TABLE machine_jobs, machine_credentials, machine_enrollments, machines",
    )
    await conn.execute(load_sql("schema.035"))
    await conn.execute(load_sql("schema.036"))
    await conn.execute(load_sql("schema.049"))
    await conn.execute(load_sql("schema.052"))
    await conn.execute(load_sql("schema.057"))
    await conn.execute(load_sql("schema.060"))
    migrated = {t: await _table_shape(conn, t) for t in _MACHINE_HOST_TABLES}
    # Replaying it over the existing tables is a no-op, not an error.
    await conn.execute(load_sql("schema.036"))

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_machine_host_checks_bound_each_column(integ_store: Store) -> None:
    """Each CHECK refuses its bad row, and one live credential per machine holds."""
    digest = b"x" * 32
    async with integ_store.engine.acquire() as conn:
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
    integ_rollback_conn: Conn,
) -> None:
    """``schema.039`` executes on PGlite and leaves ``machines`` as it was.

    A fresh database records every migration without running it, so nothing
    else ever executes this file; a body of only comments fails under asyncpg.
    """
    conn = integ_rollback_conn
    columns = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'machines' "
        "ORDER BY ordinal_position"
    )
    before = [r["column_name"] for r in await conn.fetch(columns)]
    await conn.execute(load_sql("schema.039"))
    after = [r["column_name"] for r in await conn.fetch(columns)]

    assert before
    assert after == before


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_040_matches_the_baseline_audience_columns(
    integ_rollback_conn: Conn,
) -> None:
    """The columns 040 adds to ``users`` and ``api_keys`` equal the fresh-install ones.

    Bootstrap builds the baseline shape. This drops the three columns (as a pre-040
    database lacks them) and the one 044 adds to ``users`` after them, replays
    ``schema.040`` then ``schema.044`` as a deployed database ran them, and compares
    every column, constraint and index of both tables against what the baseline
    produced.
    """
    conn = integ_rollback_conn
    baseline = {t: await _table_shape(conn, t) for t in ("users", "api_keys")}
    assert {"hosted_domain", "google_sub"} <= {
        c["column_name"] for c in baseline["users"][0]
    }
    assert "open_into" in {c["column_name"] for c in baseline["api_keys"][0]}

    await conn.execute(
        "ALTER TABLE users DROP COLUMN hosted_domain, DROP COLUMN google_sub, "
        "DROP COLUMN preferences",
    )
    # ``machine_id`` (060) comes after ``open_into``, so it goes too and comes back.
    await conn.execute(
        "ALTER TABLE api_keys DROP COLUMN open_into, DROP COLUMN machine_id",
    )
    await conn.execute(load_sql("schema.040"))
    await conn.execute(load_sql("schema.044"))
    await conn.execute(load_sql("schema.060"))
    migrated = {t: await _table_shape(conn, t) for t in ("users", "api_keys")}
    # Replaying it over the existing columns is a no-op, not an error.
    await conn.execute(load_sql("schema.040"))

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_audience_checks_bound_each_column(integ_store: Store) -> None:
    """A bad shape is refused; absent and well-formed values are kept."""
    async with integ_store.engine.acquire() as conn:
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
    conn: Conn,
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
    integ_rollback_conn: Conn,
) -> None:
    """An existing database that applies 041 ends with the column a fresh one has.

    Bootstrap builds the baseline. This takes the audience column away again (its
    CHECK goes with it), leaves a session behind as such a database holds it, and
    applies the migration. The session must come out ``open``, and every column and
    constraint of ``inquiries`` must equal the baseline's, and a replay of the file
    changes nothing.
    """
    conn = integ_rollback_conn
    baseline = await _inquiries_shape(conn)
    await conn.execute("ALTER TABLE inquiries DROP COLUMN agentsession_audience")
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title) "
        "VALUES (gen_random_uuid(), 'AgentSession', 1, 'active', "
        "'tester@example.com', 'before')",
    )
    await conn.execute(load_sql("schema.041"))
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
    integ_store: Store,
) -> None:
    """A session needs a well-formed audience; no other row may hold one."""
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
    async with integ_store.engine.acquire() as conn:
        for bad in (None, "", "Staff", "has space", "0open", "a" * 33, "open,staff"):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(session, bad)
        for good in ("open", "staff", "partner-1", "a" * 32):
            await conn.execute(session, good)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(issue, "open")
        await conn.execute(issue, None)


async def _table_shape(
    conn: Conn,
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


async def _make_canvases_old(conn: Conn) -> None:
    """Turn a fresh database's canvases back into 038's one-default-canvas shape."""
    await conn.execute(load_sql("canvas_rename_down"))
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


async def _seed_old_canvases(conn: Conn) -> dict[str, uuid.UUID]:
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


async def _check_canvases_migrated(conn: Conn) -> None:
    """Migrate 038's canvases by 042 and 058, and compare them with a fresh install."""
    fresh = {tuple(row.values()) for row in await conn.fetch(_CATALOG)}
    await _make_canvases_old(conn)
    ids = await _seed_old_canvases(conn)
    # The steps a boot of such a database runs: 042, and the 058 the rollback undid.
    await conn.execute(load_sql("schema.042"))
    await conn.execute(load_sql("schema.058"))
    migrated = {tuple(row.values()) for row in await conn.fetch(_CATALOG)}
    canvases = await conn.fetch(
        "SELECT id, user_id, metadata->>'name' AS name, metadata->>'conversation_id' "
        "AS conversation_id, revision, state "
        "FROM visual_canvases ORDER BY modified_at DESC, metadata->>'name'",
    )
    columns = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'visual_canvases' ORDER BY ordinal_position",
    )
    assert fresh
    assert migrated == fresh
    # Trax's columns stay as they were; the canvas's fields are one optional JSON.
    assert [r["column_name"] for r in columns] == [
        "id",
        "user_id",
        "revision",
        "state",
        "created_at",
        "modified_at",
        "metadata",
    ]
    ada = sorted(
        from_plain(r["name"], str) for r in canvases if r["user_id"] == ids["ada"]
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
    assert [r["name"] for r in canvases if r["user_id"] == ids["bo"]].count(
        "Review",
    ) == 1
    assert {r["conversation_id"] for r in canvases} == {None}
    # The canvas stays the canvas the app opens, ahead of every converted view.
    [canvas] = [r for r in canvases if r["id"] == ids["canvas"]]
    assert (canvas["name"], canvas["revision"]) == ("unnamed-workstream#1", 4)
    assert ids["canvas"] in {r["id"] for r in canvases[:2]}
    kept = [r for r in canvases if r["name"] == "Review" and r["user_id"] == ids["ada"]]
    state = from_plain(kept[0]["state"], dict[str, object])
    assert state["agent_instructions"] == "Compare the tails first."
    assert state["continuation_record_id"] == str(ids["continuation"])


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_042_makes_canvases_of_canvases_and_presets_on_pglite(
    pglite_rollback_conn: Conn,
) -> None:
    """Old canvases and saved views become canvases, and the catalog equals fresh."""
    await _check_canvases_migrated(pglite_rollback_conn)


@pytest.mark.db_postgres
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_042_makes_canvases_of_canvases_and_presets_on_postgres(
    integ_rollback_conn: Conn,
) -> None:
    """The same on real Postgres, whose constraint and DO-block handling PGlite mirrors."""
    await _check_canvases_migrated(integ_rollback_conn)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_043_matches_the_baseline_science_chat_tables(
    pglite_rollback_conn: Conn,
) -> None:
    """The science chat tables 043 builds equal the fresh-install ones.

    Bootstrap builds the baseline shapes. This drops both tables (as a pre-043
    database lacks them), replays ``schema.043`` alone, and compares every column,
    constraint and index against what the baseline produced.
    """
    conn = pglite_rollback_conn
    tables = ("science_chat_shares", "science_chat_tombstones")
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
    pglite_rollback_conn: Conn,
) -> None:
    """The preferences column 044 adds equals the fresh-install one.

    Bootstrap builds the baseline. This drops the column (as a pre-044 database
    lacks it), replays ``schema.044`` alone, and compares every column, constraint
    and index of ``users`` against what the baseline produced.
    """
    conn = pglite_rollback_conn
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
    pglite_rollback_conn: Conn,
) -> None:
    """The credit table 045 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-045 database
    lacks it), replays ``schema.045`` alone, and compares every column, constraint and
    index against what the baseline produced.
    """
    conn = pglite_rollback_conn
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


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_053_matches_the_baseline_credit_grace_table(
    pglite_rollback_conn: Conn,
) -> None:
    """The grace table 053 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-053 database
    lacks it), replays ``schema.053`` alone, and compares every column, constraint and
    index against what the baseline produced.
    """
    conn = pglite_rollback_conn
    baseline = await _table_shape(conn, "credit_grace")
    assert baseline[0], "the baseline lacks the table"

    await conn.execute("DROP TABLE credit_grace")
    await conn.execute(load_sql("schema.053"))
    migrated = await _table_shape(conn, "credit_grace")
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.053"))
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO credit_grace (email, until, set_by) "
            "VALUES ('Jan@Example.com', '2026-12-31', 'admin@example.com')",
        )

    assert migrated == baseline


_GPU_TABLES = ("gpu_owners", "gpu_jobs", "gpu_issuances")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_054_matches_the_baseline_gpu_tables(
    pglite_rollback_conn: Conn,
) -> None:
    """The three GPU tables 054 builds equal the fresh-install ones.

    Bootstrap builds the baseline shape. This drops the tables (as a pre-054 database
    lacks them), replays 054 and the later job migrations, and compares every column,
    constraint and index of each against what the baseline produced.
    """
    conn = pglite_rollback_conn
    baseline = {t: await _table_shape(conn, t) for t in _GPU_TABLES}
    assert baseline["gpu_jobs"][0], "the baseline lacks the tables"

    # ``machine_jobs`` (057) takes its ids from ``gpu_jobs``' sequence, so it goes
    # first and comes back after.
    await conn.execute(
        "DROP TABLE machine_jobs, gpu_issuances, gpu_jobs, gpu_owners",
    )
    await conn.execute(load_sql("schema.054"))
    # Later migrations add the columns the baseline's ``gpu_jobs`` ends with.
    await conn.execute(load_sql("schema.056"))
    await conn.execute(load_sql("schema.057"))
    await conn.execute(load_sql("schema.062"))
    migrated = {t: await _table_shape(conn, t) for t in _GPU_TABLES}
    # Replaying it over the existing tables is a no-op, not an error.
    await conn.execute(load_sql("schema.054"))
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO gpu_issuances (job_id, email, kind) "
            "VALUES (1, 'jan@example.com', 'ssh-key')",
        )

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_055_matches_the_baseline_device_logins_table(
    pglite_rollback_conn: Conn,
) -> None:
    """The device login table 055 builds equals the fresh-install one.

    Bootstrap builds the baseline shape. This drops the table (as a pre-055 database
    lacks it), replays ``schema.055`` alone, and compares every column, constraint and
    index against what the baseline produced.
    """
    conn = pglite_rollback_conn
    baseline = await _table_shape(conn, "device_logins")
    assert baseline[0], "the baseline lacks the table"

    await conn.execute("DROP TABLE device_logins")
    await conn.execute(load_sql("schema.055"))
    migrated = await _table_shape(conn, "device_logins")
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.055"))
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO device_logins (device_sha256, user_code, client, "
            "expires_at) VALUES (decode(repeat('00', 32), 'hex'), 'abcdefgh', "
            "'machine-host', now())",
        )

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_056_matches_the_baseline_box_credentials(
    pglite_rollback_conn: Conn,
) -> None:
    """The box-credential table and the job column 056 adds equal the baseline's.

    Drops the table and job columns added since 056, replays 056 and 062, and
    compares both tables' shapes against the baseline, including column order.
    """
    conn = pglite_rollback_conn
    tables = ("gpu_box_credentials", "gpu_jobs")
    baseline = {t: await _table_shape(conn, t) for t in tables}
    assert baseline["gpu_box_credentials"][0], "the baseline lacks the table"

    await conn.execute(
        "ALTER TABLE gpu_jobs DROP COLUMN box_credential_id, "
        "DROP COLUMN requested_node",
    )
    await conn.execute("DROP TABLE gpu_box_credentials")
    await conn.execute(load_sql("schema.056"))
    await conn.execute(load_sql("schema.062"))
    migrated = {t: await _table_shape(conn, t) for t in tables}
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.056"))

    assert migrated == baseline


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_057_matches_the_baseline_machine_jobs_table(
    pglite_rollback_conn: Conn,
) -> None:
    """The machine-job table 057 builds equals the fresh-install one.

    Drops the table (as a pre-057 database lacks it), replays ``schema.057`` alone, and
    compares its shape against the baseline. Its ids come from the GPU jobs' sequence,
    so one number never names two jobs.
    """
    conn = pglite_rollback_conn
    baseline = await _table_shape(conn, "machine_jobs")
    assert baseline[0], "the baseline lacks the table"

    await conn.execute("DROP TABLE machine_jobs")
    await conn.execute(load_sql("schema.057"))
    migrated = await _table_shape(conn, "machine_jobs")
    # Replaying it over the existing table is a no-op, not an error.
    await conn.execute(load_sql("schema.057"))
    gpu = from_plain(await conn.fetchval("SELECT nextval('gpu_jobs_id_seq')"), int)
    machine = await conn.fetchval(
        "INSERT INTO machine_jobs (email, machine, state, gpus, memory_mb, "
        "minutes, argv, remote, ref, commit) VALUES ('jan@example.com', 'gx10', "
        "'requested', 0, 1024, 5, '[\"true\"]', 'https://github.com/o/r.git', "
        "'refs/heads/main', repeat('a', 40)) RETURNING id",
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE machine_jobs SET state = 'placed' WHERE id = $1",
            machine,
        )

    assert migrated == baseline
    assert machine == gpu + 1


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_060_matches_the_baseline_run_places_and_key_machines(
    pglite_rollback_conn: Conn,
) -> None:
    """The columns 060 adds to ``session_liveness`` and ``api_keys`` equal the baseline's.

    Drops them (as a pre-060 database lacks them), replays ``schema.060`` alone, and
    compares both tables' shapes, the key's machine reference included.
    """
    conn = pglite_rollback_conn
    tables = ("session_liveness", "api_keys")
    baseline = {t: await _table_shape(conn, t) for t in tables}
    assert [c["column_name"] for c in baseline["session_liveness"][0]][-4:] == [
        "machine",
        "run",
        "cli",
        "cwd",
    ]

    for column in ("machine", "run", "cli", "cwd"):
        await conn.execute(f"ALTER TABLE session_liveness DROP COLUMN {column}")
    await conn.execute("ALTER TABLE api_keys DROP COLUMN machine_id")
    await conn.execute(load_sql("schema.060"))
    migrated = {t: await _table_shape(conn, t) for t in tables}
    # Replaying it over the existing columns is a no-op, not an error.
    await conn.execute(load_sql("schema.060"))

    assert migrated == baseline
    assert (
        "api_keys_machine_id_fkey",
        "FOREIGN KEY (machine_id) REFERENCES machines(id) ON DELETE SET NULL",
    ) in migrated["api_keys"][1]


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
        f"{_change_kinds('app', 'agentsession_audience')}))",
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.046.sql'")


def _change_kinds(*left_out: str) -> str:
    """Return the change kinds but ``left_out``, quoted for a SQL ``IN`` list."""
    return ", ".join(
        f"'{kind}'"
        for kind in cast(tuple[str, ...], get_args(Change.Kind.__value__))
        if kind not in left_out
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_046_gives_an_existing_database_the_app_column(
    pglite_rollback_conn: Conn,
) -> None:
    """A database migrated by 046 has the catalog a fresh install has, names aside.

    Bootstrap builds the baseline. This takes ``app`` away again (its column, its
    two audit columns, their CHECKs and its change kind), and what 047 and 048 add
    after it, leaves an Issue and its ``created`` change behind as such a database
    holds them, and applies the migrations. Every table, column, constraint, index
    and sequence must equal the baseline's, and a replay of the files changes
    nothing.
    """
    conn = pglite_rollback_conn
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

    for step in ("schema.046", "schema.047", "schema.048"):
        await conn.execute(load_sql(step))
    migrated = await _catalog_names_aside(conn)
    app = await conn.fetchval("SELECT app FROM inquiries WHERE id = $1", issue)
    await conn.execute(load_sql("schema.046"))
    await conn.execute(load_sql("schema.047"))
    await conn.execute(load_sql("schema.048"))
    replayed = await _catalog_names_aside(conn)

    assert app is None
    assert migrated == fresh
    assert replayed == fresh


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_app_column_holds_one_object_of_at_most_64_kib_on_any_kind(
    pglite_db: PGliteEngine,
) -> None:
    """An ``app`` is one JSON object of at most 65,536 bytes, on a row of any kind.

    ``{"b": "<n x>"}`` renders as n + 9 bytes, so n = 65,527 is the largest that fits.
    The audit columns hold the same shape, on a change to a row of any kind.
    """
    inquiry = (
        "INSERT INTO inquiries (id, kind, seq, status, account, title, app) "
        "VALUES (gen_random_uuid(), $1, $2, 'active', 'a@x.y', 't', $3)"
    )
    change = (
        "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, "
        "old_app, new_app) "
        "VALUES (gen_random_uuid(), 'tester', gen_random_uuid(), $1, 'app', $2, $3)"
    )
    largest: dict[str, str] = {"b": "x" * 65_527}
    async with pglite_db.acquire() as conn:
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


# Nor is there a code store, which came after, in 048.
async def _make_audience_old(conn: Conn) -> None:
    """Turn a fresh database back into 046's shape: no audience move is audited."""
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
        f"{_change_kinds('agentsession_audience')}))",
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.047.sql'")


# Each is embedded by its title, as the boot after a 046 server's last writes left it.
async def _seed_chat_titles(conn: Conn) -> dict[str, uuid.UUID]:
    """Insert a chat titled by its line, another session, and one titled as chats are."""
    sessions = {name: uuid.uuid4() for name in ("chat", "other", "reference")}
    embedder = StubEmbedder()
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
        await upsert_embedding(
            conn,
            sessions[name],
            embedder.name,
            await embedder.embed(title),
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
    pglite_rollback_conn: Conn,
) -> None:
    """A database 047 migrates has a fresh catalog, and no chat's line on its row.

    The build's catalog, then titles embedded as they stand, a chat's first line among
    them. This takes the audience audit away again (its two columns, their CHECKs and
    its change kind), and the code store 048 adds after it, and applies the migrations.
    Every table, column, constraint, index and sequence must equal the baseline's, a
    replay of the files must change nothing, and the chat comes out ``Science chat``:
    its row, the title change the log kept for it, and its embedding, which 047 takes
    away so the boot makes it again from the new title. No other row changes.
    """
    conn = pglite_rollback_conn
    fresh = await _catalog_names_aside(conn)
    sessions = await _seed_chat_titles(conn)
    await _make_audience_old(conn)

    for step in ("schema.047", "schema.048"):
        await conn.execute(load_sql(step))
    migrated = await _catalog_names_aside(conn)
    titles = await conn.fetch(
        "SELECT id, title FROM inquiries WHERE id = ANY($1::uuid[]) ORDER BY title",
        [sessions["chat"], sessions["other"]],
    )
    logged = await conn.fetchrow(
        "SELECT old_title, new_title FROM change_log WHERE subject_id = $1",
        sessions["chat"],
    )
    embedded = {
        from_plain(row["inquiry_id"], uuid.UUID)
        for row in await conn.fetch(
            "SELECT inquiry_id FROM inquiry_embeddings WHERE inquiry_id = ANY($1)",
            list(sessions.values()),
        )
    }
    await conn.execute(load_sql("schema.047"))
    await conn.execute(load_sql("schema.048"))
    replayed = await _catalog_names_aside(conn)

    assert migrated == fresh
    assert replayed == fresh
    assert [(row["id"], row["title"]) for row in titles] == [
        (sessions["chat"], "Science chat"),
        (sessions["other"], "my run"),
    ]
    assert logged is not None
    assert (logged["old_title"], logged["new_title"]) == ("", "")
    assert embedded == {sessions["other"], sessions["reference"]}


async def _make_code_store_old(conn: Conn) -> None:
    """Turn a fresh database back into 047's shape: no code store."""
    await conn.execute("DROP TABLE code_snapshots, code_blobs")
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.048.sql'")


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_048_gives_an_existing_database_the_code_store(
    pglite_rollback_conn: Conn,
) -> None:
    """A database migrated by 048 has the catalog a fresh install has.

    Bootstrap builds the baseline. This drops both code tables again, as a 047
    database lacks them, and applies the migration. Every table, column, constraint,
    index and sequence must equal the baseline's, by name too, and a replay of the
    file changes nothing.
    """
    conn = pglite_rollback_conn
    tables = ("code_blobs", "code_snapshots")
    fresh = await _catalog_names_aside(conn)
    baseline = [await _table_shape(conn, table) for table in tables]
    await _make_code_store_old(conn)

    await conn.execute(load_sql("schema.048"))
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
    pglite_db: PGliteEngine,
) -> None:
    """A blob's owner is a lowercase address, its hash 32 bytes, its size at most 1 MiB.

    A snapshot's owner is the same, and its files are one JSON object.
    """
    blob = (
        "INSERT INTO code_blobs (owner, sha256, size, data_zst) VALUES ($1, $2, $3, '')"
    )
    snapshot = (
        "INSERT INTO code_snapshots (record_id, owner, files, file_count, total_bytes) "
        "VALUES ($1, $2, $3, 0, 0)"
    )
    async with pglite_db.acquire() as conn:
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
async def test_migration_049_gives_a_machine_the_owner_who_enrolled_it_last(
    pglite_rollback_conn: Conn,
) -> None:
    """A 048 database gains ``machines.owner``, filled from the latest used enrollment.

    A machine that never joined stays NULL. The catalog equals a fresh install's, and a
    replay keeps an owner already set.
    """
    conn = pglite_rollback_conn
    fresh = await _catalog_names_aside(conn)
    await conn.execute(
        "ALTER TABLE machines DROP COLUMN owner, DROP COLUMN confirmed_at",
    )
    await conn.execute(
        "DELETE FROM applied_migrations WHERE name = 'schema.049.sql'",
    )
    await conn.execute(
        "INSERT INTO machines (name, updated_by) VALUES "
        "('joined', 'x@y.z'), ('never', 'x@y.z')",
    )
    enroll = (
        "INSERT INTO machine_enrollments "
        "(machine_id, secret_sha256, created_by, expires_at, used_at) "
        "SELECT id, $1, $2, now(), now() - make_interval(mins => $3) "
        "FROM machines WHERE name = 'joined'"
    )
    await conn.execute(enroll, bytes(32), "old@x.y", 10)
    await conn.execute(enroll, bytes(32), "new@x.y", 1)

    await conn.execute(load_sql("schema.049"))
    migrated = await _catalog_names_aside(conn)
    owners = await conn.fetch(
        "SELECT name, owner, confirmed_at IS NOT NULL AS confirmed "
        "FROM machines ORDER BY name",
    )
    await conn.execute("UPDATE machines SET owner = 'kept@x.y'")
    await conn.execute(load_sql("schema.049"))
    kept = await conn.fetchval("SELECT DISTINCT owner FROM machines")

    assert migrated == fresh
    assert [(row["name"], row["owner"], row["confirmed"]) for row in owners] == [
        ("joined", "new@x.y", True),
        ("never", None, False),
    ]
    assert kept == "kept@x.y"


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_050_opens_every_staff_session_and_audits_the_move(
    pglite_rollback_conn: Conn,
) -> None:
    """A 049 database's ``staff`` sessions come out ``open``, each move logged.

    A ``private`` session stays hidden and an ``open`` one is untouched, so neither
    is logged. A replay moves nothing and logs nothing more.
    """
    conn = pglite_rollback_conn
    sessions = await _seed_audiences(conn)

    await conn.execute(load_sql("schema.050"))
    audiences = await conn.fetch(
        "SELECT id, agentsession_audience AS audience FROM inquiries "
        "WHERE id = ANY($1::uuid[])",
        list(sessions.values()),
    )
    logged = await conn.fetch(
        "SELECT subject_id, actor, reason, old_agentsession_audience AS old, "
        "new_agentsession_audience AS new FROM change_log "
        "WHERE kind = 'agentsession_audience'",
    )
    await conn.execute(load_sql("schema.050"))
    replayed = await conn.fetchval(
        "SELECT count(*) FROM change_log WHERE kind = 'agentsession_audience'",
    )

    assert {row["id"]: row["audience"] for row in audiences} == {
        sessions["staff"]: "open",
        sessions["private"]: "private",
        sessions["open"]: "open",
    }
    assert [tuple(row) for row in logged] == [
        (
            sessions["staff"],
            "schema.050",
            "campaign transcripts opened to every reader",
            "staff",
            "open",
        ),
    ]
    assert replayed == 1


async def _seed_audiences(conn: Conn) -> dict[str, uuid.UUID]:
    """Insert one session per audience, as 049 left them; return their ids."""
    sessions = {audience: uuid.uuid4() for audience in ("staff", "private", "open")}
    for audience, session_id in sessions.items():
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title, "
            "agentsession_audience) "
            "VALUES ($1, 'AgentSession', nextval('seq_agentsession'), 'active', "
            "'campaign@service.invalid', 'lead', $2)",
            session_id,
            audience,
        )
    await conn.execute("DELETE FROM applied_migrations WHERE name = 'schema.050.sql'")
    return sessions


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_051_gives_a_part_the_key_that_opened_it(
    pglite_rollback_conn: Conn,
) -> None:
    """A 050 database's manifests gain ``opened_by_api_key_id``, NULL for each one held.

    The catalog equals a fresh install's, and a replay keeps a key already stamped.
    """
    conn = pglite_rollback_conn
    session_id, key = uuid.uuid4(), uuid.uuid4()
    fresh = await _catalog_names_aside(conn)
    await conn.execute(
        "ALTER TABLE session_manifests DROP COLUMN opened_by_api_key_id",
    )
    await conn.execute(
        "DELETE FROM applied_migrations WHERE name = 'schema.051.sql'",
    )
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title, "
        "agentsession_audience) VALUES ($1, 'AgentSession', "
        "nextval('seq_agentsession'), 'active', 'a@x.y', 't', 'open')",
        session_id,
    )
    await conn.execute(
        "INSERT INTO session_manifests "
        "(session_id, part, name, metadata, ir_id, format, records) "
        "VALUES ($1, 0, 'held.jsonl', '{}'::json, $1, 'claude', 0)",
        session_id,
    )

    await conn.execute(load_sql("schema.051"))
    migrated = await _catalog_names_aside(conn)
    held = await conn.fetchval(
        "SELECT opened_by_api_key_id FROM session_manifests WHERE session_id = $1",
        session_id,
    )
    await conn.execute(
        "UPDATE session_manifests SET opened_by_api_key_id = $2 WHERE session_id = $1",
        session_id,
        key,
    )
    await conn.execute(load_sql("schema.051"))
    kept = await conn.fetchval(
        "SELECT opened_by_api_key_id FROM session_manifests WHERE session_id = $1",
        session_id,
    )

    assert migrated == fresh
    assert held is None
    assert kept == key


_KEPT_USER = uuid.UUID(int=0x5901)
_NAMED_USER = uuid.UUID(int=0x5902)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_migration_059_makes_scratch_the_default_and_promotes_a_renamed_one(
    pglite_rollback_conn: Conn,
) -> None:
    """A 058 database's Scratch comes out the default, "Untitled workspace".

    A Scratch someone renamed was named, so it is promoted as a rename now promotes:
    it keeps its title, loses the mark, and keeps its canvases in no workspace. Each
    change is logged, the re-titled Scratch loses the embedding of its old title, which
    the boot makes again from the new one, and a replay changes nothing.
    """
    conn = pglite_rollback_conn
    ids = await _seed_scratches(conn)
    embedded_before = await _same_embedding(conn, ids["kept"], ids["reference"])

    await conn.execute(load_sql("schema.059"))
    workspaces = await _workspace_marks(conn, ids)
    canvases = await _canvas_workspaces(conn)
    kept_embedded = await conn.fetchval(
        "SELECT count(*) FROM inquiry_embeddings WHERE inquiry_id = $1",
        ids["kept"],
    )
    logged = await conn.fetch(
        "SELECT subject_id, kind, old_title, new_title, old_labels, new_labels "
        "FROM change_log WHERE actor = 'schema.059' ORDER BY kind DESC",
    )
    await conn.execute(load_sql("schema.059"))
    replayed = await conn.fetchval(
        "SELECT count(*) FROM change_log WHERE actor = 'schema.059'",
    )

    assert workspaces == {
        "kept": ("Untitled workspace", ["workspace", "scratch"]),
        "named": ("Inbox", ["workspace"]),
        "ordinary": ("Scratch", ["workspace"]),
    }
    assert canvases == {
        ids["kept_loose"]: None,
        ids["named_loose"]: str(ids["named"]),
        ids["named_placed"]: str(ids["reference"]),
    }
    assert (embedded_before, kept_embedded) == (False, 0)
    assert [tuple(row) for row in logged] == [
        (ids["kept"], "title", "Scratch", "Untitled workspace", None, None),
        (ids["named"], "labels", None, None, ["workspace", "scratch"], ["workspace"]),
    ]
    assert replayed == len(logged)


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_default_workspace_rollback_puts_scratch_back(
    pglite_rollback_conn: Conn,
) -> None:
    """The operator's rollback returns each untitled default to Scratch, shown.

    The Scratch the migration promoted is marked again; a later default that is not a
    person's first stays as the old build would list it. Startup never runs the
    rollback; it unrecords 059, so the next boot migrates again.
    """
    conn = pglite_rollback_conn
    ids = await _seed_scratches(conn)
    await conn.execute(load_sql("schema.059"))
    ids["later"] = await _workspace(
        conn,
        email="named@x.y",
        title="Untitled workspace 2",
        labels=["workspace", "scratch"],
    )
    await conn.execute(
        "UPDATE users SET preferences = jsonb_build_object('workspaces', "
        "jsonb_build_object('hidden', jsonb_build_array($2::text, $3::text))) "
        "WHERE id = $1",
        _KEPT_USER,
        str(ids["kept"]),
        str(ids["ordinary"]),
    )
    # Within the test's own transaction, as the operator runs it within one.
    await conn.execute(load_sql("default_workspace_down"))
    rolled = await _workspace_marks(conn, ids)
    hidden = await conn.fetchval(
        "SELECT preferences->'workspaces'->'hidden' FROM users WHERE id = $1",
        _KEPT_USER,
    )
    ledger = await conn.fetchval(
        "SELECT count(*) FROM applied_migrations WHERE name = 'schema.059.sql'",
    )
    await conn.execute(load_sql("schema.059"))
    again = await _workspace_marks(conn, ids)

    assert rolled == {
        "kept": ("Scratch", ["workspace", "scratch"]),
        "named": ("Inbox", ["workspace", "scratch"]),
        "ordinary": ("Scratch", ["workspace"]),
        "later": ("Untitled workspace 2", ["workspace", "scratch"]),
    }
    assert from_plain(hidden, list[str]) == [str(ids["ordinary"])]
    assert ledger == 0
    assert again == {
        "kept": ("Untitled workspace", ["workspace", "scratch"]),
        "named": ("Inbox", ["workspace"]),
        "ordinary": ("Scratch", ["workspace"]),
        "later": ("Untitled workspace 2", ["workspace", "scratch"]),
    }
    assert "default_workspace_down.sql" not in {name for name, _ in schema_migrations()}


# The decoys are a workspace titled Scratch without the mark and one titled "Untitled
# workspace", whose embedding a re-titled Scratch must come to share.
async def _seed_scratches(conn: Conn) -> dict[str, uuid.UUID]:
    """Make two people's Scratch as a 058 server left it, one renamed, and decoys."""
    for user_id, email in ((_KEPT_USER, "kept@x.y"), (_NAMED_USER, "named@x.y")):
        await conn.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, $2, 'Person', 'writer', 'active')",
            user_id,
            email,
        )
    ids = {
        name: await _workspace(conn, email=email, title=title, labels=labels)
        for name, email, title, labels in (
            ("kept", "kept@x.y", "Scratch", ["workspace", "scratch"]),
            ("named", "named@x.y", "Inbox", ["workspace", "scratch"]),
            ("ordinary", "kept@x.y", "Scratch", ["workspace"]),
            ("reference", "named@x.y", "Untitled workspace", ["workspace"]),
        )
    }
    for name, user_id, metadata in (
        ("kept_loose", _KEPT_USER, {"name": "kept-loose"}),
        ("named_loose", _NAMED_USER, {"name": "named-loose"}),
        (
            "named_placed",
            _NAMED_USER,
            {"name": "named-placed", "workspace_id": str(ids["reference"])},
        ),
    ):
        ids[name] = uuid.uuid4()
        await conn.execute(
            "INSERT INTO visual_canvases (id, user_id, metadata, state) "
            "VALUES ($1, $2, $3, $4)",
            ids[name],
            user_id,
            metadata,
            {"visuals": []},
        )
    return ids


async def _workspace(
    conn: Conn,
    *,
    email: str,
    title: str,
    labels: list[str],
) -> uuid.UUID:
    """File one of ``email``'s own active Issues and embed its title."""
    workspace = uuid.uuid4()
    await insert_inquiry(
        conn,
        workspace,
        "Issue",
        status="active",
        values={
            "title": title,
            "account": email,
            "owner": email,
            "labels": labels,
            "subscribers": (),
        },
    )
    embedder = StubEmbedder()
    await upsert_embedding(conn, workspace, embedder.name, await embedder.embed(title))
    return workspace


async def _workspace_marks(
    conn: Conn,
    ids: Mapping[str, uuid.UUID],
) -> dict[str, tuple[str, list[str]]]:
    """Read the title and labels of each seeded workspace, by its seed name."""
    names = {
        ids[name]: name
        for name in ("kept", "named", "ordinary", "later")
        if name in ids
    }
    rows = await conn.fetch(
        "SELECT id, title, labels FROM inquiries WHERE id = ANY($1::uuid[])",
        list(names),
    )
    return {
        names[from_plain(row["id"], uuid.UUID)]: (
            from_plain(row["title"], str),
            from_plain(row["labels"], list[str]),
        )
        for row in rows
    }


async def _canvas_workspaces(conn: Conn) -> dict[uuid.UUID, str | None]:
    """Read every canvas's stored workspace id."""
    rows = await conn.fetch(
        "SELECT id, metadata->>'workspace_id' AS workspace FROM visual_canvases",
    )
    return {
        from_plain(row["id"], uuid.UUID): from_plain(
            row["workspace"],
            str,
            default=None,
        )
        for row in rows
    }


async def _same_embedding(conn: Conn, first: uuid.UUID, second: uuid.UUID) -> bool:
    """Say whether two inquiries hold the same embedding, as equal titles do."""
    return from_plain(
        await conn.fetchval(
            "SELECT a.embedding::text = b.embedding::text "
            "FROM inquiry_embeddings AS a JOIN inquiry_embeddings AS b "
            "ON a.model = b.model WHERE a.inquiry_id = $1 AND b.inquiry_id = $2",
            first,
            second,
        ),
        bool,
    )


if __name__ == "__main__":
    from trackinizer.lib.testing.runner import test_main

    test_main(__file__)
