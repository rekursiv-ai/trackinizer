"""Catalog-parity gate for numbered schema migrations.

Per ``docs/db_schema_migration.md`` (Roadmap A, step 4.5): bootstrap a fresh
DB, mutate it into the *old* (pre-migration) shape, clear the numbered-migration
ledger, re-bootstrap (so the numbered migration runs), then assert full catalog
parity against a fresh DB. Set comparison over constraint defs / columns /
indexes / sequences, never line diff -- a constraint def spans lines and sorts
unstably, so only set membership is meaningful.

Currently exercises ``schema.001.sql`` (room-scoped messaging). Runs against its
own scratch database so it never touches the shared ``integ_engine``. Skips
cleanly when the Postgres toolchain is absent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import uuid

import asyncpg
import pytest
import pytest_asyncio

from trackinizer.lib import postgres
from trackinizer.lib.postgres import Conn
from trackinizer.server.notify import NOTIFY_CHANNEL
from trackinizer.server.schema_gen import (
    substitute_schema_placeholders,
)
from trackinizer.server.store.core import Store, StubEmbedder
from trackinizer.wire.bodies import SubmitAgentSession
from trackinizer.wire.filters import NON_NULLABLE_COLUMNS


async def _catalog(engine: postgres.PostgresEngine) -> dict[str, set[str]]:
    """Snapshot the comparable catalog: columns, constraints, indexes, seqs."""
    async with engine.acquire() as conn:
        columns = {
            f"{r['table_name']}.{r['column_name']}:{r['data_type']}:{r['is_nullable']}"
            for r in await conn.fetch(
                "SELECT table_name, column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_schema = 'public'"
            )
        }
        constraints = {
            # Normalize whitespace so multi-line defs compare as sets.
            " ".join(r["def"].split())
            for r in await conn.fetch(
                "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace"
            )
        }
        indexes = {
            r["indexdef"]
            for r in await conn.fetch(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
        sequences = {
            r["sequence_name"]
            for r in await conn.fetch(
                "SELECT sequence_name FROM information_schema.sequences "
                "WHERE sequence_schema = 'public'"
            )
        }
    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "sequences": sequences,
    }


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


@pytest.mark.integration
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


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_bootstrap_converges_from_partial_state_empty_ledger(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Re-bootstrap converges when the schema is present but the ledger is empty.

    The M2 partial/manual-state gap: ``inquiries`` (and the rest of the
    current baseline) exists, but ``applied_migrations`` is empty -- e.g. a
    hand-loaded ``schema.sql``, or a process kill before the ledger INSERTs.
    ``is_fresh_database`` (the ``to_regclass('public.inquiries')`` probe)
    reports not-fresh, so bootstrap must NOT re-create the baseline and must
    reconcile the ledger without erroring on a non-idempotent replay. The
    result must match a fresh database exactly and record every migration.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    # Clear the WHOLE ledger (baseline included) while leaving the current
    # baseline schema intact -- the partial/manual state.
    async with scratch_engine.acquire() as conn:
        await conn.execute("DELETE FROM applied_migrations")

    # Re-bootstrap must converge without error (no baseline re-create, no
    # failed numbered-DDL replay) and reach fresh parity.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)
    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after partial-state re-bootstrap:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # Every migration is recorded so a later real deploy never replays them.
    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert names == {
        "schema.sql",
        "schema.001.sql",
        "schema.002.sql",
        "schema.003.sql",
        "schema.004.sql",
        "schema.005.sql",
        "schema.006.sql",
        "schema.007.sql",
        "schema.008.sql",
        "schema.009.sql",
        "schema.010.sql",
        "schema.011.sql",
        "schema.012.sql",
        "schema.013.sql",
        "schema.014.sql",
        "schema.015.sql",
        "schema.016.sql",
        "schema.017.sql",
    }


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_rooms_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    # Fresh baseline -> snapshot the target shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    # Mutate into the pre-rooms shape: drop the column (+ its CHECK cascades),
    # revert change_log_kind_check to omit agentsession_rooms, drop the feed
    # index, and clear the numbered-migration ledger so bootstrap re-applies.
    async with scratch_engine.acquire() as conn:
        await conn.execute("ALTER TABLE inquiries DROP COLUMN agentsession_rooms")
        await conn.execute(
            "ALTER TABLE change_log DROP CONSTRAINT change_log_kind_check"
        )
        await conn.execute(
            "ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check CHECK (kind IN "
            "('created','purged','status','summary','description','labels','owner',"
            "'subscribers','marginal_cost','issue_kind','issue_validation',"
            "'issue_priority','belief_judgement','belief_confidence',"
            "'experiment_outcome','experiment_codechanges','paper_source',"
            "'paper_source_kind','codechange_sha','webresult_url','websearch_query',"
            "'websearch_provider','websearch_results','agentsession_cli',"
            "'agentsession_cli_session_id','agentsession_started','agentsession_ended',"
            "'edge_added','edge_removed','edge_annotation_changed','dependency_changed',"
            "'implicit_subs_opened','implicit_subs_closed'))"
        )
        await conn.execute("DROP INDEX idx_agent_session_events_created_session_seq")
        await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.001.sql now runs against the old shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    # Full set parity -- the migration must reproduce the fresh shape exactly.
    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # The ledger records the numbered migration as applied.
    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert "schema.001.sql" in names


_PRE_012_CHANGE_KINDS = (
    "'created', 'purged', 'status', 'title', 'description', 'labels', 'owner', "
    "'account', 'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation', "
    "'issue_priority', 'belief_judgement', 'belief_confidence', 'experiment_outcome', "
    "'experiment_codechanges', 'paper_abstract', 'paper_authors', "
    "'paper_publication_type', 'paper_venue', 'paper_subvenue', 'paper_publish_date', "
    "'paper_source', 'codechange_sha', 'webresult_url', 'websearch_query', "
    "'websearch_provider', 'agentsession_cli', 'agentsession_cli_session_id', "
    "'agentsession_started', 'agentsession_ended', 'agentsession_rooms', 'edge_added', "
    "'edge_removed', 'edge_annotation_changed', 'dependency_changed', "
    "'implicit_subs_opened', 'implicit_subs_closed'"
)
"""The ``change_log_kind_check`` enum before ``experiment_config`` (migration
012). Reverting a fresh DB to this is the shape a DB deployed before 012
carried, so 012's kind-check widening runs against it."""


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_experiment_metrics_migrations_reach_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migrations 011/012/013 (metrics table + config + backstops) reach parity.

    The whole-suite drift guard for the experiment-tracking migrations. Reverts
    a fresh DB to the pre-011 shape -- drops ``experiment_metrics``, the
    ``inquiries.experiment_config`` column, the ``change_log`` config mirrors,
    and the ``experiment_config`` kind enum entry -- clears the numbered ledger,
    re-bootstraps (so 011/012/013 run their ADD paths), and asserts full catalog
    set-parity against fresh. This is what proves 012's HAND-WRITTEN CHECK defs
    are byte-identical to the baseline's GENERATED ones (the file itself flags
    that constraint defs, not names, are compared), and that 013's value
    finiteness CHECK matches the inline baseline CHECK.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        # 011: drop the whole side table.
        await conn.execute("DROP TABLE experiment_metrics")
        # 012: drop the config column (its per-kind CHECK cascades), the
        # change_log mirrors + their populated-iff CHECKs, and revert the kind
        # enum to omit experiment_config.
        await conn.execute("ALTER TABLE inquiries DROP COLUMN experiment_config")
        await conn.execute("ALTER TABLE change_log DROP COLUMN old_experiment_config")
        await conn.execute("ALTER TABLE change_log DROP COLUMN new_experiment_config")
        await conn.execute(
            "ALTER TABLE change_log DROP CONSTRAINT change_log_kind_check"
        )
        await conn.execute(
            "ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check "
            f"CHECK (kind IN ({_PRE_012_CHANGE_KINDS}))"
        )
        await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: 011 re-creates the table, 012 re-adds config + mirrors +
    # widens the kind enum, 013 re-adds the value finiteness CHECK.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after metrics migrations:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert {
        "schema.011.sql",
        "schema.012.sql",
        "schema.013.sql",
        "schema.014.sql",
    } <= names


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_account_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 007 adds the NOT NULL ``account`` attribution column.

    Reverts a fresh DB to the pre-007 shape -- drop the ``account`` column and
    its index, drop the two ``change_log`` account mirror columns plus their
    populated-iff CHECKs, and revert ``change_log_kind_check`` to omit the
    ``'account'`` kind -- seeds one inquiry whose ``created`` event carries a
    recoverable principal, clears the numbered ledger, re-bootstraps (007 runs),
    and asserts full catalog parity plus that the seeded row was backfilled from
    its created-event principal.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    seeded_id = uuid.uuid4()
    orphan_id = uuid.uuid4()
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # Revert to the pre-007 shape. Drop the account mirror CHECKs (found by
        # their column token) before the columns, then the column + index, then
        # revert the kind check to omit 'account'.
        for con in await c.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%_account IS NOT NULL%'"
        ):
            await c.execute(
                f'ALTER TABLE change_log DROP CONSTRAINT "{con["conname"]}"'
            )
        await c.execute("ALTER TABLE change_log DROP COLUMN old_account")
        await c.execute("ALTER TABLE change_log DROP COLUMN new_account")
        await c.execute("DROP INDEX idx_inquiries_account")
        await c.execute("ALTER TABLE inquiries DROP COLUMN account")
        await c.execute("ALTER TABLE change_log DROP CONSTRAINT change_log_kind_check")
        await c.execute(
            "ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check CHECK (kind IN "
            "('created','purged','status','summary','description','labels','owner',"
            "'subscribers','marginal_cost','issue_kind','issue_validation',"
            "'issue_priority','belief_judgement','belief_confidence',"
            "'experiment_outcome','experiment_codechanges','paper_source',"
            "'paper_source_kind','codechange_sha','webresult_url','websearch_query',"
            "'websearch_provider','agentsession_cli','agentsession_cli_session_id',"
            "'agentsession_started','agentsession_ended','agentsession_rooms',"
            "'edge_added','edge_removed','edge_annotation_changed','dependency_changed',"
            "'implicit_subs_opened','implicit_subs_closed'))"
        )
        # Seed an inquiry (old shape -- no account column) plus a created event
        # whose actor is an active user, so the backfill resolves a principal
        # via the actor-as-user arm.
        await c.execute(
            "INSERT INTO inquiries (id, kind, seq, status, title) "
            "VALUES ($1, 'Issue', 9001, 'active', 'seeded')",
            seeded_id,
        )
        await c.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'seed-user@example.com', 'Seed', 'writer', 'active') "
            "ON CONFLICT (email) DO NOTHING",
            uuid.uuid4(),
        )
        await c.execute(
            "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind) "
            "VALUES ($1, 'seed-user@example.com', $2, 'Issue', 'created')",
            uuid.uuid4(),
            seeded_id,
        )
        # A second row whose created-event actor is NOT a user (no recoverable
        # principal) exercises the portable admin fallback. Seed an active admin
        # so the fallback resolves to a real, deployment-derived email -- never a
        # hardcoded one.
        await c.execute(
            "INSERT INTO users (id, email, name, role, status) "
            "VALUES ($1, 'admin-user@example.com', 'Admin', 'admin', 'active') "
            "ON CONFLICT (email) DO NOTHING",
            uuid.uuid4(),
        )
        await c.execute(
            "INSERT INTO inquiries (id, kind, seq, status, title) "
            "VALUES ($1, 'Issue', 9002, 'active', 'orphan')",
            orphan_id,
        )
        await c.execute(
            "INSERT INTO change_log (id, actor, subject_id, subject_kind, kind) "
            "VALUES ($1, 'ghost', $2, 'Issue', 'created')",
            uuid.uuid4(),
            orphan_id,
        )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # The principal-bearing row backfills from its created-event principal; the
    # orphan (non-user actor) falls back to the deployment's active admin. Both
    # are non-NULL under the new constraint.
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        account = await c.fetchval(
            "SELECT account FROM inquiries WHERE id = $1", seeded_id
        )
        orphan_account = await c.fetchval(
            "SELECT account FROM inquiries WHERE id = $1", orphan_id
        )
    assert account == "seed-user@example.com"
    assert orphan_account == "admin-user@example.com"


@pytest.mark.integration
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


# A ``kind`` CHECK narrower than the current Kind literal -- the shape a DB
# would carry if an earlier render of migration 006 (before ``SlashCommand``
# joined the literal) had landed. 006's drop-then-add must WIDEN this to the
# full current literal, not leave it frozen.
_NARROW_KIND_CHECK = (
    "kind IN ('UserMessage', 'AgentSendMessage', 'SystemMessage', "
    "'AssistantMessage', 'ToolResult', 'Compaction', 'UnknownMessage')"
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_agent_session_event_kind_check_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """006 installs (and widens) the kind CHECK to the full current Kind literal.

    Two cases in one: 006 must land the CHECK on a DB that has none, AND widen a
    DB carrying a narrower render of an earlier 006 (the ``SlashCommand`` gap).
    Mutating to the narrow CHECK exercises the widen path -- the stronger case --
    then asserts a ``SlashCommand`` event inserts and the catalog matches fresh.
    """
    # Fresh baseline -> snapshot the target shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    # Mutate into the pre-widen / pre-006 shape: replace the full kind CHECK
    # with the narrow one, AND drop the AgentSession drain-credential column +
    # its per-kind CHECK (a DB deployed at 005 has neither), then clear the
    # numbered-migration ledger so bootstrap re-applies 006 against it. 006's
    # drop-then-add must widen the kind CHECK, and its ADD COLUMN / ADD CHECK
    # arm must reproduce the column + presence CHECK the baseline generates.
    async with scratch_engine.acquire() as conn:
        con_name = await conn.fetchval(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'agent_session_events'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%kind = ANY%'"
        )
        assert con_name is not None, "fresh schema must carry the kind CHECK"
        await conn.execute(
            f"ALTER TABLE agent_session_events DROP CONSTRAINT {con_name}"
        )
        await conn.execute(
            f"ALTER TABLE agent_session_events ADD CHECK ({_NARROW_KIND_CHECK})"
        )
        # Revert the drain-credential column to its 005-era absence.
        cred_check = await conn.fetchval(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'inquiries'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE "
            "'%agentsession_opened_by_api_key_id%'"
        )
        assert cred_check is not None, "fresh schema must carry the column CHECK"
        await conn.execute(f"ALTER TABLE inquiries DROP CONSTRAINT {cred_check}")
        await conn.execute(
            "ALTER TABLE inquiries DROP COLUMN agentsession_opened_by_api_key_id"
        )
        await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.006.sql runs against the narrow shape and widens it.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    # A SlashCommand event must now insert (the frozen narrow CHECK would reject
    # it before 006's widen).
    async with scratch_engine.acquire() as conn:
        sid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, title) "
            "VALUES ($1, 'AgentSession', 1, 'active', 'tester@example.com', 't')",
            sid,
        )
        await conn.execute(
            "INSERT INTO agent_session_events (session_id, seq, kind) "
            "VALUES ($1, 0, 'SlashCommand')",
            sid,
        )

    # Full set parity -- the migration must reproduce the fresh shape exactly.
    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert "schema.006.sql" in names


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_nullable_columns_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 003 makes the optional base columns nullable to fresh parity.

    Reverts ``owner`` / ``description`` / ``labels`` / ``subscribers`` to their
    old ``NOT NULL DEFAULT`` shape and the owner index to its ``owner <> ''``
    predicate, clears the numbered ledger, re-bootstraps, and asserts full
    catalog set-parity -- proving the live diff will be empty after deploy.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        # Restore the pre-003 empty-sentinel shape on the four columns.
        for col, default in (
            ("owner", "''"),
            ("description", "''"),
            ("labels", "'{}'"),
            ("subscribers", "'{}'"),
        ):
            await conn.execute(
                f"ALTER TABLE inquiries ALTER COLUMN {col} SET DEFAULT {default}"
            )
            await conn.execute(
                f"UPDATE inquiries SET {col} = {default} WHERE {col} IS NULL"  # noqa: S608 -- col/default from the hardcoded literal tuple above.
            )
            await conn.execute(f"ALTER TABLE inquiries ALTER COLUMN {col} SET NOT NULL")
        await conn.execute("DROP INDEX idx_inquiries_owner")
        await conn.execute(
            "CREATE INDEX idx_inquiries_owner ON inquiries (owner) WHERE owner <> ''"
        )
        # Seed an old-shape row carrying the empty sentinels so the
        # migration's data-rewrite arm runs against real data, not an empty
        # table. ``seq`` is set directly to avoid the per-kind sequence.
        sentinel_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, title, owner, account, "
            "description, labels, subscribers) VALUES ($1, 'Issue', 9999, 's', '', "
            "'tester@example.com', '', '{}', '{}')",
            sentinel_id,
        )
        # A whitespace-only owner must also collapse to NULL (btrim).
        whitespace_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, title, owner, account, "
            "description, labels, subscribers) VALUES ($1, 'Issue', 9998, 's', "
            "'   ', 'tester@example.com', 'x', '{}', '{}')",
            whitespace_id,
        )
        await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.003.sql now runs against the old (NOT NULL) shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    # The data-rewrite arm collapsed the empty sentinels to NULL.
    async with scratch_engine.acquire() as conn:
        rewritten = await conn.fetchrow(
            "SELECT owner, description, labels, subscribers FROM inquiries "
            "WHERE id = $1",
            sentinel_id,
        )
    assert rewritten is not None
    assert rewritten["owner"] is None
    assert rewritten["description"] is None
    assert rewritten["labels"] is None
    assert rewritten["subscribers"] is None

    async with scratch_engine.acquire() as conn:
        ws_owner = await conn.fetchval(
            "SELECT owner FROM inquiries WHERE id = $1", whitespace_id
        )
    assert ws_owner is None

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert "schema.003.sql" in names


# Old-name (pre-005) form of the change_log kind enum CHECK: identical to the
# fresh baseline except ``'summary'`` sits where ``'title'`` now does. Used to
# revert the fresh DB so migration 005's rename + CHECK rewrite both run.
#
# SQUASH-DISPOSABLE: this constant (and the sibling ``_OLD_*`` revert helpers) is
# test-only scaffolding for migration 005's forward path. When 005 is folded into
# the baseline, delete the whole 005 test block -- these constants go with it. They
# never touch production schema and carry no maintenance cost past the squash.
_OLD_CHANGE_LOG_KIND_ENUM = (
    "kind IN ('created', 'purged', 'status', 'summary', 'description', 'labels', "
    "'owner', 'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation', "
    "'issue_priority', 'belief_judgement', 'belief_confidence', "
    "'experiment_outcome', 'experiment_codechanges', 'paper_source', "
    "'paper_source_kind', 'codechange_sha', 'webresult_url', 'websearch_query', "
    "'websearch_provider', 'websearch_results', 'agentsession_cli', "
    "'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', "
    "'agentsession_rooms', 'edge_added', 'edge_removed', "
    "'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', "
    "'implicit_subs_closed')"
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_title_rename_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 005 renames the base field ``summary`` -> ``title``.

    Reverts the fresh DB to the pre-005 shape (columns ``inquiries.summary`` /
    ``change_log.{old,new}_summary``, the ``'summary'`` kind value + enum CHECK,
    and the two ``summary`` populated-iff CHECKs), seeds an inquiry and a
    ``summary`` field-edit change_log row so the column + data rewrites are
    exercised, clears the numbered ledger, re-bootstraps (005 renames), and
    asserts full catalog set-parity plus the per-row data outcomes.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # Drop the three CHECKs that mention the new ``title`` shape (the kind
        # enum + the two title populated-iff CHECKs) before renaming columns
        # back, so neither the rename nor the data rewrite fights a CHECK.
        for token in ("''title''", "old_title", "new_title"):
            for r in await c.fetch(
                "SELECT conname FROM pg_constraint "  # noqa: S608 -- token is a hardcoded literal below, never user input.
                "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
                f"AND pg_get_constraintdef(oid) LIKE '%{token}%'"
            ):
                await c.execute(
                    f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"'
                )
        # Rename the columns back to the pre-005 names.
        await c.execute("ALTER TABLE inquiries RENAME COLUMN title TO summary")
        await c.execute("ALTER TABLE change_log RENAME COLUMN old_title TO old_summary")
        await c.execute("ALTER TABLE change_log RENAME COLUMN new_title TO new_summary")
        # Re-add the old-name CHECKs (kind enum with 'summary' + the two
        # populated-iff CHECKs gated on kind='summary').
        await c.execute(
            f"ALTER TABLE change_log ADD CHECK ({_OLD_CHANGE_LOG_KIND_ENUM})"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK "
            "((old_summary IS NOT NULL) = (kind = 'summary'))"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK "
            "((new_summary IS NOT NULL) = (kind = 'summary'))"
        )
        # Seed an inquiry plus a ``summary`` field-edit change_log row so the
        # column rename carries data and the stored-kind rewrite has a row to
        # move. The populated-iff CHECK requires old_/new_summary present iff
        # kind='summary', so this edit row sets both sides.
        subj = uuid.uuid4()
        await c.execute(
            "INSERT INTO inquiries (id, kind, seq, status, account, summary) "
            "VALUES ($1, 'Issue', nextval('seq_issue'), 'active', "
            "'tester@example.com', 'before')",
            subj,
        )
        change_id = uuid.uuid4()
        await c.execute(
            "INSERT INTO change_log "
            "(id, actor, subject_id, subject_kind, kind, old_summary, new_summary) "
            "VALUES ($1, 'tester', $2, 'Issue', 'summary', 'before', 'after')",
            change_id,
            subj,
        )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.005.sql runs against the old (summary) shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # The seeded data survived under the new column / kind names; the old
    # names are gone.
    async with scratch_engine.acquire() as conn:
        inquiry_title = await conn.fetchval(
            "SELECT title FROM inquiries WHERE id = $1", subj
        )
        change = await conn.fetchrow(
            "SELECT kind, old_title, new_title FROM change_log WHERE id = $1",
            change_id,
        )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert inquiry_title == "before"
    assert change is not None
    assert change["kind"] == "title"
    assert change["old_title"] == "before"
    assert change["new_title"] == "after"
    assert "schema.005.sql" in names


# Pre-arm-B form of the change_log kind enum: ``paper_source_kind`` present, the
# five new Paper field-edit kinds absent. Otherwise identical to the fresh
# (post-005) baseline. Used to revert the fresh DB so migration 005's Paper
# arm (B) enum rewrite runs.
_OLD_PAPER_KIND_ENUM = (
    "kind IN ('created', 'purged', 'status', 'title', 'description', 'labels', "
    "'owner', 'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation', "
    "'issue_priority', 'belief_judgement', 'belief_confidence', "
    "'experiment_outcome', 'experiment_codechanges', 'paper_source', "
    "'paper_source_kind', 'codechange_sha', 'webresult_url', 'websearch_query', "
    "'websearch_provider', 'websearch_results', 'agentsession_cli', "
    "'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', "
    "'agentsession_rooms', 'edge_added', 'edge_removed', "
    "'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', "
    "'implicit_subs_closed')"
)

# The five new Paper bibliography columns, with the per-kind CASE-WHEN CHECK
# token each carries (used to find + drop the CHECK during the revert). venue
# additionally carries the closed-set membership clause.
_NEW_PAPER_COLUMNS = (
    "paper_abstract",
    "paper_authors",
    "paper_venue",
    "paper_subvenue",
    "paper_publish_date",
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_paper_bib_fields_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 005 arm B gives Paper first-class bibliography fields to fresh parity.

    Reverts the fresh DB to the pre-arm-B Paper shape: re-adds the
    ``paper_source_kind`` column (+ per-kind CHECK) and its two change_log
    ``old_/new_`` mirrors (+ populated-iff CHECKs); drops the five new
    inquiries columns and their change_log mirrors + matrix CHECKs; reverts the
    kind enum. Seeds an arxiv/doi/url Paper trio so the source scheme-tagging
    data-migration is exercised, clears the numbered ledger, re-bootstraps (005 arm B
    runs), and asserts full catalog set-parity plus the per-row source rewrite.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # 1. Drop the five new inquiries columns (their per-kind CHECKs cascade).
        for col in _NEW_PAPER_COLUMNS:
            await c.execute(f"ALTER TABLE inquiries DROP COLUMN {col}")
        # 2. Re-add the old paper_source_kind column + its per-kind CHECK.
        await c.execute("ALTER TABLE inquiries ADD COLUMN paper_source_kind TEXT")
        await c.execute(
            "ALTER TABLE inquiries ADD CHECK ("
            "CASE WHEN kind = 'Paper' THEN TRUE "
            "AND (paper_source_kind IS NULL OR paper_source_kind IN "
            "('doi', 'arxiv', 'url')) ELSE paper_source_kind IS NULL END)"
        )
        # 3. change_log: drop the matrix CHECK (``kind <> '<col>' OR subject_kind
        #    ...``) for each new field, then drop the mirror column pairs (their
        #    value / populated-iff CHECKs cascade with the column). The matrix
        #    CHECK names ``kind``/``subject_kind`` rather than the column, so it
        #    must go explicitly. Match on the ``<> '<col>'`` token so the kind
        #    enum (which merely *lists* the col as a value) is NOT touched.
        for col in _NEW_PAPER_COLUMNS:
            for con in await c.fetch(
                "SELECT conname FROM pg_constraint "  # noqa: S608 -- col from the hardcoded _NEW_PAPER_COLUMNS tuple.
                "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
                f"AND pg_get_constraintdef(oid) LIKE '%<> ''{col}''%'"
            ):
                await c.execute(
                    f'ALTER TABLE change_log DROP CONSTRAINT "{con["conname"]}"'
                )
            await c.execute(f"ALTER TABLE change_log DROP COLUMN old_{col}")
            await c.execute(f"ALTER TABLE change_log DROP COLUMN new_{col}")
        # 4. Re-add the paper_source_kind change_log mirrors + populated-iff CHECKs.
        await c.execute("ALTER TABLE change_log ADD COLUMN old_paper_source_kind TEXT")
        await c.execute("ALTER TABLE change_log ADD COLUMN new_paper_source_kind TEXT")
        await c.execute(
            "ALTER TABLE change_log ADD CHECK "
            "(kind = 'paper_source_kind' OR old_paper_source_kind IS NULL)"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK "
            "(kind = 'paper_source_kind' OR new_paper_source_kind IS NULL)"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK (old_paper_source_kind IS NULL "
            "OR old_paper_source_kind IN ('doi', 'arxiv', 'url'))"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK (new_paper_source_kind IS NULL "
            "OR new_paper_source_kind IN ('doi', 'arxiv', 'url'))"
        )
        await c.execute(
            "ALTER TABLE change_log ADD CHECK (kind <> 'paper_source_kind' "
            "OR subject_kind = 'Paper')"
        )
        # 5. Revert the kind enum to the pre-arm-B form. The fresh baseline emits
        #    this CHECK unnamed (auto-named by Postgres), so discover it by the
        #    new-shape token it carries rather than by the ``change_log_kind_check``
        #    name the numbered migrations later give it.
        enum_con = await c.fetchval(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%kind%' "
            "AND pg_get_constraintdef(oid) LIKE '%paper_abstract%' "
            "AND pg_get_constraintdef(oid) LIKE '%implicit_subs_closed%'"
        )
        assert enum_con is not None, "no post-arm-B change_log kind enum CHECK found"
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{enum_con}"')
        await c.execute(
            f"ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check "
            f"CHECK ({_OLD_PAPER_KIND_ENUM})"
        )
        # 6. Seed three Papers (arxiv / doi / url) under the OLD shape so the
        #    source scheme-tagging data-migration has rows to rewrite.
        arxiv_id, doi_id, url_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for rid, kind_val, source in (
            (arxiv_id, "arxiv", "2405.16391"),
            (doi_id, "doi", "10.1145/3292500"),
            (url_id, "url", "https://example.com/p"),
        ):
            await c.execute(
                "INSERT INTO inquiries "
                "(id, kind, seq, status, account, title, paper_source, "
                "paper_source_kind) "
                "VALUES ($1, 'Paper', nextval('seq_paper'), 'active', "
                "'tester@example.com', 's', $2, $3)",
                rid,
                source,
                kind_val,
            )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.005.sql (combined #425/#426 delta) runs against the old (paper_source_kind) shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # The seeded sources were scheme-tagged from the old source_kind; url
    # already a URL is left untouched. paper_source_kind column is gone.
    async with scratch_engine.acquire() as conn:
        arxiv_src = await conn.fetchval(
            "SELECT paper_source FROM inquiries WHERE id = $1", arxiv_id
        )
        doi_src = await conn.fetchval(
            "SELECT paper_source FROM inquiries WHERE id = $1", doi_id
        )
        url_src = await conn.fetchval(
            "SELECT paper_source FROM inquiries WHERE id = $1", url_id
        )
        has_kind_col = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'inquiries' "
            "AND column_name = 'paper_source_kind')"
        )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert arxiv_src == "arXiv:2405.16391"
    assert doi_src == "doi:10.1145/3292500"
    assert url_src == "https://example.com/p"
    assert has_kind_col is False
    assert "schema.005.sql" in names


# SQL ``IN (...)`` bodies for the inquiry / artifact kind sets, in canonical
# ``Artifact.Kind`` / ``Inquiry.InquiryKind`` Literal order. Used to build the
# pre-010 (009-shape) edge-validity CHECK the 010 parity test reverts to.
_ARTIFACT_KINDS_SQL = (
    "'Artifact', 'Experiment', 'Paper', 'Belief', "
    "'CodeChange', 'WebResult', 'WebSearch', 'AgentSession'"
)
_INQUIRY_KINDS_SQL = "'Issue', " + _ARTIFACT_KINDS_SQL
# Pre-010 (009-final) form of the edges edge-validity CHECK -- the body
# ``schema.009.sql`` emits: nine edge kinds with the pre-recast directions
# (narrows_issue/blocks_issue, produces Inquiry->Inquiry, proves/disproves
# Belief->Artifact, favors/disfavors Artifact->Belief, supersedes,
# refutes_experiment). Reverting a fresh DB to this is exactly the shape a DB
# deployed at 009 carried, so migration 010's edge recast runs against it.
_PRE_010_EDGE_CHECK = (
    "(edge_kind = 'narrows_issue'"
    "    AND from_kind = 'Issue' AND to_kind = 'Issue')"
    " OR (edge_kind = 'blocks_issue'"
    "    AND from_kind = 'Issue' AND to_kind = 'Issue')"
    f" OR (edge_kind = 'produces'"
    f"    AND from_kind IN ({_INQUIRY_KINDS_SQL})"
    f"    AND to_kind IN ({_INQUIRY_KINDS_SQL}))"
    f" OR (edge_kind IN ('proves', 'disproves')"
    f"    AND from_kind = 'Belief'"
    f"    AND to_kind IN ({_ARTIFACT_KINDS_SQL}))"
    f" OR (edge_kind IN ('favors', 'disfavors')"
    f"    AND from_kind IN ({_ARTIFACT_KINDS_SQL})"
    f"    AND to_kind = 'Belief')"
    f" OR (edge_kind = 'supersedes'"
    f"    AND from_kind IN ({_INQUIRY_KINDS_SQL})"
    f"    AND to_kind IN ({_INQUIRY_KINDS_SQL}))"
    " OR (edge_kind = 'refutes_experiment'"
    "    AND from_kind = 'Experiment' AND to_kind = 'Experiment')"
)

# Pre-010 (009) peer-edge-kind closed set: the nine edge kinds Edge.Kind
# carried before the recast. The fresh baseline regenerates the change_log
# ``{edge_kinds}`` membership CHECKs from the (now 6-kind) literal, so reverting
# to this list is the shape a DB deployed at 009 carried.
_PRE_010_EDGE_KINDS_SQL = (
    "'narrows_issue', 'blocks_issue', 'produces', 'proves', 'disproves', "
    "'favors', 'disfavors', 'supersedes', 'refutes_experiment'"
)


async def _revert_to_pre_010(conn: Conn) -> None:
    """Revert a fresh DB's edges + change_log to the pre-010 (009) edge shape.

    Renames ``valence`` back to ``relevance`` (range 0..1) on both tables, and
    reverts the edge-validity, edges-priority, and change_log peer-edge-kind
    CHECKs to their nine-kind 009 forms, so old-shape rows seeded afterward are
    valid and migration 010's data recast + CHECK rewrites both run. Shared by
    every 010 parity test so the revert lives in one place.
    """
    old_edge_check = substitute_schema_placeholders(_PRE_010_EDGE_CHECK)
    c = conn
    # 1. edges: valence -> relevance, widen-back the range CHECK to 0..1.
    #    The current baseline valence CHECK names BOTH ``valence`` and
    #    ``edge_kind`` (the citation-only restriction), so match the renamed
    #    ``relevance`` column without an ``edge_kind`` guard -- only the valence
    #    CHECK mentions that column, so this never catches the edge-validity one.
    await c.execute("ALTER TABLE edges RENAME COLUMN valence TO relevance")
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'edges'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%relevance%'"
    ):
        await c.execute(f'ALTER TABLE edges DROP CONSTRAINT "{r["conname"]}"')
    await c.execute(
        "ALTER TABLE edges ADD CHECK "
        "(relevance IS NULL OR (relevance >= 0 AND relevance <= 1))"
    )
    # 2. edges priority CHECK: revert the contextual-kind set to old names.
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'edges'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%priority%' "
        "AND pg_get_constraintdef(oid) LIKE '%edge_kind%'"
    ):
        await c.execute(f'ALTER TABLE edges DROP CONSTRAINT "{r["conname"]}"')
    await c.execute(
        "ALTER TABLE edges ADD CHECK (priority IS NULL OR "
        "(priority >= 0 AND edge_kind IN ('narrows_issue', 'blocks_issue')))"
    )
    # 3. edges edge-validity CHECK: revert to the nine-kind 009 shape.
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'edges'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%edge_kind%' "
        "AND pg_get_constraintdef(oid) LIKE '%from_kind%' "
        "AND pg_get_constraintdef(oid) LIKE '%to_kind%'"
    ):
        await c.execute(f'ALTER TABLE edges DROP CONSTRAINT "{r["conname"]}"')
    await c.execute(f"ALTER TABLE edges ADD CHECK ({old_edge_check})")
    # 4. change_log: old_/new_edge_valence -> _relevance, range CHECK -> 0..1.
    await c.execute(
        "ALTER TABLE change_log RENAME COLUMN old_edge_valence TO old_edge_relevance"
    )
    await c.execute(
        "ALTER TABLE change_log RENAME COLUMN new_edge_valence TO new_edge_relevance"
    )
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%edge_relevance%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%peer_id%'"
    ):
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"')
    await c.execute(
        "ALTER TABLE change_log ADD CHECK (old_edge_relevance IS NULL OR "
        "(old_edge_relevance >= 0 AND old_edge_relevance <= 1))"
    )
    await c.execute(
        "ALTER TABLE change_log ADD CHECK (new_edge_relevance IS NULL OR "
        "(new_edge_relevance >= 0 AND new_edge_relevance <= 1))"
    )
    # 5. change_log priority-restricted peer CHECKs (old/new): old-name set.
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%edge_priority%' "
        "AND pg_get_constraintdef(oid) LIKE '%peer_edge_kind%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%peer_id%'"
    ):
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"')
    for p in ("old", "new"):
        await c.execute(
            f"ALTER TABLE change_log ADD CHECK ({p}_edge_priority IS NULL OR "
            f"({p}_edge_priority >= 0 AND "
            f"{p}_peer_edge_kind IN ('narrows_issue', 'blocks_issue')))"
        )
    # 6. change_log peer-edge-kind enum CHECKs (old/new): the nine-kind list.
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%peer_edge_kind%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%edge_priority%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%peer_id%'"
    ):
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"')
    for p in ("old", "new"):
        await c.execute(
            f"ALTER TABLE change_log ADD CHECK ({p}_peer_edge_kind IS NULL OR "
            f"{p}_peer_edge_kind IN ({_PRE_010_EDGE_KINDS_SQL}))"
        )


async def _seed_old_inquiry(conn: Conn, rid: uuid.UUID, kind: str) -> None:
    """Insert one minimal inquiry of ``kind`` under the old/new schema."""
    await conn.execute(
        "INSERT INTO inquiries (id, kind, seq, status, account, title) "
        "VALUES ($1, $2, nextval('seq_' || lower($2)), 'active', "
        "'tester@example.com', 's')",
        rid,
        kind,
    )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_edge_model_migration_010_collapses_colliding_polarity_pairs(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """010 merges a pair carrying BOTH a for- and an against-citation.

    Pre-010 the PK ``(from_id, to_id, edge_kind)`` admitted ``proves`` AND
    ``disproves`` between the same ordered pair (likewise ``favors`` +
    ``disfavors``). Collapsing polarity into a signed ``valence`` makes them one
    row, so the migration must MERGE the pair rather than emit two rows with the
    same post-collapse PK (which aborts the whole deploy with a duplicate-key
    error). It must also default a NULL-relevance ``proves`` to the citation
    default (+0.5) and negate the audit valence for renamed ``dis*`` rows.

    This is the deploy-blocker the per-transform parity test misses: that test
    seeds one row per kind, never a colliding pair.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        await _revert_to_pre_010(c)

        belief_id, paper_id, exp_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for rid, kind in (
            (belief_id, "Belief"),
            (paper_id, "Paper"),
            (exp_id, "Experiment"),
        ):
            await _seed_old_inquiry(c, rid, kind)

        # COLLISION 1: the same Belief->Paper pair carries both proves(0.7) and
        # disproves(0.3). Post-collapse both want PK (paper, belief, 'proves').
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Belief', $2, 'Paper', 'proves', 0.7)",
            belief_id,
            paper_id,
        )
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Belief', $2, 'Paper', 'disproves', 0.3)",
            belief_id,
            paper_id,
        )
        # COLLISION 2: the same Experiment->Belief pair carries favors(0.6) and
        # disfavors(0.2). Same direction (no swap); both want PK (exp, belief,
        # 'favors').
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Experiment', $2, 'Belief', 'favors', 0.6)",
            exp_id,
            belief_id,
        )
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Experiment', $2, 'Belief', 'disfavors', 0.2)",
            exp_id,
            belief_id,
        )
        # A NULL-relevance proves row (separate pair) must default to +0.5, not
        # survive as NULL valence.
        belief2 = uuid.uuid4()
        await _seed_old_inquiry(c, belief2, "Belief")
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Belief', $2, 'Paper', 'proves')",
            belief2,
            paper_id,
        )
        # A disproves audit row under the OLD shape: the mirror column is still
        # ``new_edge_relevance`` (0..1, magnitude only). After the peer-kind
        # rename to 'proves' the migrated ``new_edge_valence`` must be negated to
        # -0.4, else the audit drifts from the edge.
        await c.execute(
            "INSERT INTO change_log "
            "(id, actor, subject_id, subject_kind, kind, "
            " new_peer_id, new_peer_kind, new_peer_edge_kind, "
            " new_edge_relevance, new_edge_note, new_edge_labels) "
            "VALUES ($1, 'tester', $2, 'Belief', 'edge_added', "
            " $3, 'Paper', 'disproves', 0.4, '', '{}')",
            uuid.uuid4(),
            belief_id,
            paper_id,
        )
        # A plain proves audit row with a NULL relevance mirror: the migration
        # must coalesce it to the citation default (+0.5), matching the live-edge
        # coalesce, so the audit never claims a NULL valence on a citation.
        null_audit_subject = uuid.uuid4()
        await c.execute(
            "INSERT INTO change_log "
            "(id, actor, subject_id, subject_kind, kind, "
            " new_peer_id, new_peer_kind, new_peer_edge_kind, "
            " new_edge_note, new_edge_labels) "
            "VALUES ($1, 'tester', $2, 'Belief', 'edge_added', "
            " $3, 'Paper', 'proves', '', '{}')",
            uuid.uuid4(),
            null_audit_subject,
            paper_id,
        )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap MUST NOT abort on a duplicate-key collision.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()

    async with scratch_engine.acquire() as conn:
        # Collision 1: exactly one proves row Paper->Belief survives, carrying the
        # merged (higher-magnitude) signed valence; the disproved 0.3 lost to the
        # proved 0.7, so the kept sign is positive 0.7.
        proves_rows = await conn.fetch(
            "SELECT from_id, to_id, valence FROM edges "
            "WHERE edge_kind = 'proves' AND from_id = $1 AND to_id = $2",
            paper_id,
            belief_id,
        )
        assert len(proves_rows) == 1, "colliding proves+disproves must merge to one"
        assert proves_rows[0]["valence"] == 0.7

        # Collision 2: exactly one favors row Experiment->Belief survives.
        favors_rows = await conn.fetch(
            "SELECT valence FROM edges "
            "WHERE edge_kind = 'favors' AND from_id = $1 AND to_id = $2",
            exp_id,
            belief_id,
        )
        assert len(favors_rows) == 1, "colliding favors+disfavors must merge to one"
        assert favors_rows[0]["valence"] == 0.6

        # NULL-relevance proves defaulted to the +0.5 citation default.
        null_proves = await conn.fetchval(
            "SELECT valence FROM edges WHERE edge_kind = 'proves' "
            "AND from_id = $1 AND to_id = $2",
            paper_id,
            belief2,
        )
        assert null_proves == 0.5, "historical NULL-relevance proves must default 0.5"

        # The disproves audit row's valence was negated alongside its kind rename.
        audit_valence = await conn.fetchval(
            "SELECT new_edge_valence FROM change_log "
            "WHERE new_peer_edge_kind = 'proves' AND new_edge_valence IS NOT NULL "
            "AND subject_id = $1",
            belief_id,
        )
        assert audit_valence == -0.4, "renamed disproves audit valence must be negated"

        # The plain-proves audit row with NULL relevance was coalesced to the
        # citation default, mirroring the live-edge coalesce (no NULL citation
        # valence survives in the audit log either).
        coalesced_audit = await conn.fetchval(
            "SELECT new_edge_valence FROM change_log "
            "WHERE subject_id = $1 AND new_peer_edge_kind = 'proves'",
            null_audit_subject,
        )
        assert coalesced_audit == 0.5, (
            "NULL-relevance proves audit mirror must coalesce to the citation default"
        )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_edge_model_migration_010_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 010 recasts the edge model (valence + child->parent) to parity.

    Reverts a fresh DB to the pre-010 (009) edge shape -- ``valence`` -> the
    ``relevance`` 0..1 column, the edge-validity CHECK to its nine-kind form, and
    the regenerated edges-priority / change_log peer-edge-kind CHECKs to their
    old-name lists -- seeds one edge per transform (and matching change_log
    peer-mirror rows) so every data rewrite is exercised, clears the numbered
    ledger, re-bootstraps (010 runs), and asserts full catalog set-parity plus
    the per-edge endpoint swaps, kind collapses, valence negations, and the
    dropped ``refutes_experiment`` rows.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        await _revert_to_pre_010(c)

        # Seed one inquiry per endpoint role, then one edge per transform under
        # the OLD shape so every data rewrite in 010 is exercised.
        issue_a, issue_b = uuid.uuid4(), uuid.uuid4()
        belief_id, paper_id = uuid.uuid4(), uuid.uuid4()
        exp_a, exp_b = uuid.uuid4(), uuid.uuid4()
        for rid, kind in (
            (issue_a, "Issue"),
            (issue_b, "Issue"),
            (belief_id, "Belief"),
            (paper_id, "Paper"),
            (exp_a, "Experiment"),
            (exp_b, "Experiment"),
        ):
            await c.execute(
                "INSERT INTO inquiries (id, kind, seq, status, account, title) "
                "VALUES ($1, $2, nextval('seq_' || lower($2)), 'active', "
                "'tester@example.com', 's')",
                rid,
                kind,
            )
        # produces (Issue -> Belief): becomes produced_by with endpoints SWAPPED.
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Issue', $2, 'Belief', 'produces')",
            issue_a,
            belief_id,
        )
        # blocks_issue (Issue -> Issue): becomes requires with endpoints SWAPPED.
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Issue', $2, 'Issue', 'blocks_issue')",
            issue_a,  # blocker (old from-side)
            issue_b,  # blocked (old to-side)
        )
        # proves (Belief -> Paper): becomes proves Paper -> Belief (SWAPPED).
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Belief', $2, 'Paper', 'proves')",
            belief_id,
            paper_id,
        )
        # disproves (Belief -> Experiment, relevance 0.3): becomes proves SWAPPED
        # with valence negated to -0.3.
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Belief', $2, 'Experiment', 'disproves', 0.3)",
            belief_id,
            exp_a,
        )
        # favors (Paper -> Belief): stays favors, SAME direction (no swap).
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Paper', $2, 'Belief', 'favors')",
            paper_id,
            belief_id,
        )
        # disfavors (Experiment -> Belief, relevance 0.6): becomes favors, SAME
        # direction, valence negated to -0.6.
        await c.execute(
            "INSERT INTO edges "
            "(from_id, from_kind, to_id, to_kind, edge_kind, relevance) "
            "VALUES ($1, 'Experiment', $2, 'Belief', 'disfavors', 0.6)",
            exp_a,
            belief_id,
        )
        # refutes_experiment (Experiment -> Experiment): the rows are DROPPED.
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Experiment', $2, 'Experiment', 'refutes_experiment')",
            exp_a,
            exp_b,
        )
        # change_log peer-mirror rows: one per renamed/dropped peer kind, on both
        # the old_* (edge_removed) and new_* (edge_added) sides, so every peer-kind
        # rewrite UPDATE in step 10 is exercised. The co-occurrence CHECK requires
        # the full peer tuple on the populated side.
        for old_kind in (
            "narrows_issue",
            "blocks_issue",
            "produces",
            "disproves",
            "disfavors",
            "refutes_experiment",
        ):
            await c.execute(
                "INSERT INTO change_log "
                "(id, actor, subject_id, subject_kind, kind, "
                " new_peer_id, new_peer_kind, new_peer_edge_kind, "
                " new_edge_note, new_edge_labels) "
                "VALUES ($1, 'tester', $2, 'Issue', 'edge_added', "
                " $3, 'Issue', $4, '', '{}')",
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
                old_kind,
            )
            await c.execute(
                "INSERT INTO change_log "
                "(id, actor, subject_id, subject_kind, kind, "
                " old_peer_id, old_peer_kind, old_peer_edge_kind, "
                " old_edge_note, old_edge_labels) "
                "VALUES ($1, 'tester', $2, 'Issue', 'edge_removed', "
                " $3, 'Issue', $4, '', '{}')",
                uuid.uuid4(),
                uuid.uuid4(),
                uuid.uuid4(),
                old_kind,
            )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.010.sql runs the edge-model recast against the old shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after edge-model migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # Per-edge data outcomes.
    async with scratch_engine.acquire() as conn:
        produced_by = await conn.fetchrow(
            "SELECT from_id, from_kind, to_id, to_kind FROM edges "
            "WHERE edge_kind = 'produced_by'"
        )
        requires = await conn.fetchrow(
            "SELECT from_id, from_kind, to_id, to_kind FROM edges "
            "WHERE edge_kind = 'requires'"
        )
        proves = {
            (r["from_id"], r["to_id"], r["valence"]): r
            for r in await conn.fetch(
                "SELECT from_id, from_kind, to_id, to_kind, valence FROM edges "
                "WHERE edge_kind = 'proves'"
            )
        }
        favors = {
            (r["from_id"], r["to_id"]): r["valence"]
            for r in await conn.fetch(
                "SELECT from_id, to_id, valence FROM edges WHERE edge_kind = 'favors'"
            )
        }
        refutes = await conn.fetchval(
            "SELECT count(*) FROM edges WHERE edge_kind = 'refutes_experiment'"
        )
        peer_kinds = {
            r["k"]
            for r in await conn.fetch(
                "SELECT old_peer_edge_kind AS k FROM change_log "
                "WHERE old_peer_edge_kind IS NOT NULL "
                "UNION SELECT new_peer_edge_kind FROM change_log "
                "WHERE new_peer_edge_kind IS NOT NULL"
            )
        }
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }

    # produces Issue->Belief became produced_by Belief->Issue (endpoints swapped).
    assert produced_by is not None
    assert (produced_by["from_id"], produced_by["from_kind"]) == (belief_id, "Belief")
    assert (produced_by["to_id"], produced_by["to_kind"]) == (issue_a, "Issue")
    # blocks_issue (blocker->blocked) became requires (blocked->blocker), swapped.
    assert requires is not None
    assert (requires["from_id"], requires["from_kind"]) == (issue_b, "Issue")
    assert (requires["to_id"], requires["to_kind"]) == (issue_a, "Issue")
    # proves Belief->Paper became proves Paper->Belief (swapped); its NULL
    # relevance defaults to the +0.5 citation default (never NULL on a citation).
    assert (paper_id, belief_id, 0.5) in proves
    # disproves Belief->Experiment became proves Experiment->Belief, valence -0.3.
    assert (exp_a, belief_id, -0.3) in proves
    # favors Paper->Belief unchanged direction; NULL relevance defaults to +0.5.
    assert favors.get((paper_id, belief_id)) == 0.5
    # disfavors Experiment->Belief became favors, same direction, valence -0.6.
    assert favors.get((exp_a, belief_id)) == -0.6
    # refutes_experiment edges are gone.
    assert refutes == 0
    # Historical peer-edge-kind values were rewritten to the new closed set; no
    # old name survives in either the old_* or new_* column.
    assert {"narrows", "requires", "produced_by", "proves", "favors"} <= peer_kinds
    assert not (
        {
            "narrows_issue",
            "blocks_issue",
            "produces",
            "disproves",
            "disfavors",
            "refutes_experiment",
        }
        & peer_kinds
    )
    assert "schema.010.sql" in names


# Pre-arm-D form of the change_log kind enum: the final 005 enum (``title``
# present, paper fields added) but STILL carrying ``'websearch_results'`` and the
# JSONB ``websearch_results`` columns. Used to revert the fresh DB so arm D's
# column drops run. SQUASH-DISPOSABLE alongside the rest of the 005 test block.
_PRE_ARM_D_KIND_ENUM = (
    "kind IN ('created', 'purged', 'status', 'title', 'description', 'labels', "
    "'owner', 'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation', "
    "'issue_priority', 'belief_judgement', 'belief_confidence', "
    "'experiment_outcome', 'experiment_codechanges', 'paper_abstract', "
    "'paper_authors', 'paper_venue', 'paper_subvenue', 'paper_publish_date', "
    "'paper_source', 'codechange_sha', 'webresult_url', 'websearch_query', "
    "'websearch_provider', 'websearch_results', 'agentsession_cli', "
    "'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', "
    "'agentsession_rooms', 'edge_added', 'edge_removed', "
    "'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', "
    "'implicit_subs_closed')"
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_drop_websearch_results_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 005 arm D drops ``WebSearch.results`` to fresh parity.

    Reverts the fresh DB to the pre-arm-D shape: re-adds the JSONB
    ``websearch_results`` inquiries column and its two change_log ``old_/new_``
    mirrors, and re-adds ``'websearch_results'`` to the kind enum. Seeds a
    WebSearch carrying a results value, clears the numbered ledger, re-bootstraps
    (arm D drops all three), and asserts full catalog set-parity plus that the
    column is gone -- membership now lives on ``produces`` edges.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # 1. Re-add the JSONB column and its two change_log mirrors. The scratch
        #    engine is session-scoped and shared, so a sibling test may leave the
        #    column present or absent; drop-then-add makes the pre-state
        #    deterministic regardless of order.
        await c.execute("ALTER TABLE inquiries DROP COLUMN IF EXISTS websearch_results")
        await c.execute(
            "ALTER TABLE change_log DROP COLUMN IF EXISTS old_websearch_results"
        )
        await c.execute(
            "ALTER TABLE change_log DROP COLUMN IF EXISTS new_websearch_results"
        )
        await c.execute("ALTER TABLE inquiries ADD COLUMN websearch_results JSONB")
        await c.execute("ALTER TABLE change_log ADD COLUMN old_websearch_results JSONB")
        await c.execute("ALTER TABLE change_log ADD COLUMN new_websearch_results JSONB")
        # 2. Revert the kind enum to the pre-arm-D form (carrying the value).
        enum_con = await c.fetchval(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%kind%' "
            "AND pg_get_constraintdef(oid) LIKE '%implicit_subs_closed%'"
        )
        assert enum_con is not None, "no change_log kind enum CHECK found"
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{enum_con}"')
        await c.execute(
            f"ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check "
            f"CHECK ({_PRE_ARM_D_KIND_ENUM})"
        )
        # 3. Seed a WebSearch carrying a results value, so arm D drops a populated
        #    column (not just an empty one).
        ws_id = uuid.uuid4()
        await c.execute(
            "INSERT INTO inquiries "
            "(id, kind, seq, status, account, title, websearch_query, "
            "websearch_results) "
            "VALUES ($1, 'WebSearch', nextval('seq_websearch'), 'active', "
            "'tester@example.com', 's', 'q', "
            "$2::jsonb)",
            ws_id,
            '[["00000000-0000-4000-8000-000000000001", "WebResult"]]',
        )
        # 4. Seed a historical ``websearch_results`` field-edit audit row -- the
        #    live origin had 8 such rows, and arm B's new kind enum (which omits
        #    'websearch_results') failed its ADD CONSTRAINT validation against
        #    them, crash-looping bootstrap (the production 502). Arm B must purge
        #    these rows before re-adding the enum; this seed reproduces that.
        ws_change_id = uuid.uuid4()
        await c.execute(
            "INSERT INTO change_log "
            "(id, actor, subject_id, subject_kind, kind, "
            "old_websearch_results, new_websearch_results) "
            "VALUES ($1, 'tester', $2, 'WebSearch', 'websearch_results', "
            "$3::jsonb, $4::jsonb)",
            ws_change_id,
            ws_id,
            "[]",
            '[["00000000-0000-4000-8000-000000000001", "WebResult"]]',
        )
        # 5. Seed a cascade child whose ``caused_by`` self-FK points at that audit
        #    row -- on the live DB 11 'dependency_changed' rows referenced the 8
        #    websearch_results rows, so a bare DELETE hit change_log_caused_by_fkey.
        #    Arm B must null the soft causal link before deleting the cause.
        child_change_id = uuid.uuid4()
        await c.execute(
            "INSERT INTO change_log "
            "(id, actor, subject_id, subject_kind, kind, caused_by) "
            "VALUES ($1, 'tester', $2, 'WebSearch', 'created', $3)",
            child_change_id,
            ws_id,
            ws_change_id,
        )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.005.sql (incl. arm D) runs against the pre-arm-D shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        has_col = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'inquiries' "
            "AND column_name = 'websearch_results')"
        )
        # The seeded WebSearch survives; only its results column is gone.
        still_there = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM inquiries WHERE id = $1)", ws_id
        )
        # The historical ``websearch_results`` audit row is purged (arm B drops
        # the now-removed kind before re-adding the enum that omits it).
        audit_gone = await conn.fetchval(
            "SELECT NOT EXISTS (SELECT 1 FROM change_log WHERE id = $1)", ws_change_id
        )
        # The cascade child survives -- only its dangling ``caused_by`` pointer
        # to the now-deleted cause was nulled, its audit history is intact.
        child = await conn.fetchrow(
            "SELECT caused_by FROM change_log WHERE id = $1", child_change_id
        )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert has_col is False
    assert still_there is True
    assert audit_gone is True
    assert child is not None
    assert child["caused_by"] is None
    assert "schema.005.sql" in names


_AGENTSESSION_MIRROR_COLUMNS = (
    "agentsession_cli",
    "agentsession_cli_session_id",
    "agentsession_started",
    "agentsession_ended",
    "agentsession_rooms",
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_agentsession_audit_mirrors_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 005 arm E adds the AgentSession change_log audit mirrors.

    The five ``agentsession_*`` change kinds were always valid, but the
    change_log lacked their ``old_/new_`` mirror columns, so AgentSession field
    edits logged an event with a NULL snapshot (silent audit-value loss).
    Reverts a fresh DB to the pre-arm-E shape -- drop the ten mirror columns and
    their populated-iff + kind-matrix CHECKs -- re-bootstraps (arm E re-adds
    them), and asserts full catalog set-parity. Idempotency is also exercised:
    arm E runs against a DB that already has the columns (a fresh bootstrap) as a
    no-op via its IF [NOT] EXISTS / pg_constraint gating.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # Revert to the pre-arm-E shape: drop the populated-iff + kind-matrix
        # CHECKs (found by their column token), then the ten mirror columns.
        for col in _AGENTSESSION_MIRROR_COLUMNS:
            for con in await c.fetch(
                "SELECT conname FROM pg_constraint "  # noqa: S608 -- col from the hardcoded tuple.
                "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
                f"AND pg_get_constraintdef(oid) LIKE '%{col}%'"
            ):
                await c.execute(
                    f'ALTER TABLE change_log DROP CONSTRAINT "{con["conname"]}"'
                )
            await c.execute(f"ALTER TABLE change_log DROP COLUMN IF EXISTS old_{col}")
            await c.execute(f"ALTER TABLE change_log DROP COLUMN IF EXISTS new_{col}")
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.005.sql (incl. arm E) runs against the pre-arm-E shape.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # The mirror columns exist after migration, and an AgentSession field edit
    # now PRESERVES its snapshot (the bug arm E fixes).
    async with scratch_engine.acquire() as conn:
        has_cols = await conn.fetchval(
            "SELECT bool_and(EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'change_log' "
            "AND column_name = c)) "
            "FROM unnest($1::text[]) AS c",
            [f"new_{col}" for col in _AGENTSESSION_MIRROR_COLUMNS],
        )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert has_cols is True
    assert "schema.005.sql" in names

    store = Store(scratch_engine, embed=StubEmbedder())
    sid = await store.submit_agentsession(
        SubmitAgentSession(title="s", cli="claude", account="tester@example.com"),
        actor="u",
    )
    await store.set_cli(sid, "codex", actor="u")
    async with scratch_engine.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT old_agentsession_cli, new_agentsession_cli FROM change_log "
            "WHERE subject_id = $1 AND kind = 'agentsession_cli'",
            sid,
        )
    assert row is not None
    assert row["old_agentsession_cli"] == "claude"
    assert row["new_agentsession_cli"] == "codex"


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_agentsession_lifecycle_check_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 006 adds the AgentSession lifecycle CHECK + live-owner index.

    Reverts a fresh DB to the pre-006 shape -- drop both
    ``inquiries_agentsession_lifecycle_check`` AND the
    ``uq_inquiries_live_session_owner`` partial unique index -- clears the
    numbered ledger, re-bootstraps (006 re-adds both), and asserts full catalog
    set-parity plus enforcement of each (a desynced row is rejected by the
    CHECK; a duplicate live routing name is rejected by the index). Idempotency:
    006 runs against a DB that already has them as a no-op (conname gate /
    ``CREATE UNIQUE INDEX IF NOT EXISTS``).
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        await conn.execute(
            "ALTER TABLE inquiries "
            "DROP CONSTRAINT inquiries_agentsession_lifecycle_check"
        )
        await conn.execute("DROP INDEX uq_inquiries_live_session_owner")
        await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.006.sql re-adds the lifecycle CHECK and the index.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)
    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after lifecycle/index migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
        # The CHECK is back and enforces: a desynced (ended set, status active)
        # AgentSession row is rejected.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, title, status, account, "
                "agentsession_ended) VALUES "
                "(gen_random_uuid(), 'AgentSession', nextval('seq_agentsession'), "
                "'zombie', 'active', 'tester@example.com', clock_timestamp())"
            )
        # The partial unique index is back and enforces: two live sessions
        # cannot share a routing name (``owner``).
        await conn.execute(
            "INSERT INTO inquiries (id, kind, seq, title, owner, account, status) "
            "VALUES "
            "(gen_random_uuid(), 'AgentSession', nextval('seq_agentsession'), "
            "'s1', 'scientist', 'tester@example.com', 'active')"
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO inquiries (id, kind, seq, title, owner, account, "
                "status) VALUES "
                "(gen_random_uuid(), 'AgentSession', nextval('seq_agentsession'), "
                "'s2', 'scientist', 'tester@example.com', 'active')"
            )
    assert "schema.006.sql" in names


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_r12_arm_e_like_probe_is_idempotent(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """ARM E's populated-iff/kind-matrix LIKE probes must MATCH the rendered
    CHECKs, so a re-run adds no duplicate constraints (R12 verification).

    If pg_get_constraintdef inserts ``::text`` casts the bare-string LIKE would
    miss, and re-running ARM E would add a second equivalent CHECK. Count the
    agentsession CHECKs after a fresh bootstrap, re-run the migration (clear the
    ledger), and assert the count is unchanged.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        before = await c.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid='change_log'::regclass AND contype='c' "
            "AND pg_get_constraintdef(oid) LIKE '%agentsession_cli%'"
        )
        sample = await c.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='change_log'::regclass AND contype='c' "
            "AND pg_get_constraintdef(oid) LIKE '%agentsession_cli %' LIMIT 1"
        )
        # Re-run the whole 005 migration against the already-migrated DB.
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        after = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid='change_log'::regclass AND contype='c' "
            "AND pg_get_constraintdef(oid) LIKE '%agentsession_cli%'"
        )
    assert after == before, (
        f"ARM E re-run added duplicate CHECKs (R12): {before} -> {after}. "
        f"Rendered sample: {sample!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_arm_b4b_paper_check_probes_are_idempotent(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """ARM B4b's change_log paper-field LIKE probes must MATCH the rendered
    CHECKs, so a re-run adds no duplicate constraints.

    Sibling of ``test_r12_arm_e_like_probe_is_idempotent`` for the paper arm.
    The original B4b probes were the raw constraint bodies, which
    pg_get_constraintdef never renders verbatim (it inserts ``::text`` casts,
    rewrites ``IN`` as ``= ANY``, and adds parens), so every re-run re-added all
    18 paper CHECKs. Count the paper-field change_log CHECKs after a fresh
    bootstrap, re-run the migration (clear the ledger), and assert it is stable.
    """
    paper_check_count = (
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid='change_log'::regclass AND contype='c' "
        "AND pg_get_constraintdef(oid) LIKE '%paper_%'"
    )
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        before = await c.fetchval(paper_check_count)
        # Re-run the whole 005 migration against the already-migrated DB.
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        after = await conn.fetchval(paper_check_count)
    assert after == before, (
        f"ARM B4b re-run added duplicate paper CHECKs: {before} -> {after}. "
        "A probe failed to match the rendered (cast-laden) constraint def."
    )


# The pre-008 closed venue set (007-era): the old conflated enum that
# migration 008 splits into publication_type + free-text venue. Used by the
# revert to rebuild the old shape so 008 runs against it.
_PRE_008_VENUE_SET = (
    "'PREPRINT', 'BOOK', 'THESIS', 'REPORT', 'OTHER', 'NeurIPS', 'ICML', "
    "'ICLR', 'ACL', 'AAAI', 'CVPR', 'JMLR', 'TMLR'"
)

# Pre-008 change_log kind enum: the current enum minus the
# ``paper_publication_type`` field-change kind 008 admits.
_PRE_008_KIND_ENUM = (
    "'created', 'purged', 'status', 'title', 'description', 'labels', "
    "'owner', 'account', 'subscribers', 'marginal_cost', 'issue_kind', "
    "'issue_validation', 'issue_priority', 'belief_judgement', "
    "'belief_confidence', 'experiment_outcome', 'experiment_codechanges', "
    "'paper_abstract', 'paper_authors', 'paper_venue', 'paper_subvenue', "
    "'paper_publish_date', 'paper_source', 'codechange_sha', 'webresult_url', "
    "'websearch_query', 'websearch_provider', 'agentsession_cli', "
    "'agentsession_cli_session_id', 'agentsession_started', "
    "'agentsession_ended', 'agentsession_rooms', 'edge_added', 'edge_removed', "
    "'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', "
    "'implicit_subs_closed'"
)


async def _revert_to_pre_008(conn: Conn) -> None:
    """Mutate a fresh DB back to the pre-008 (007-era) Paper venue shape."""
    # Drop the new closed-set column + its inquiries CHECK (CASCADE clears the
    # CHECK that names the column).
    await conn.execute(
        "ALTER TABLE inquiries DROP COLUMN paper_publication_type CASCADE"
    )
    # Re-add the old closed-set value CHECK on the now-free-text venue. First
    # drop the bare populated-iff CHECK 008 left, then restore the conflated
    # form (membership + populated-iff) the 007 schema carried.
    for tbl_drop in await conn.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid='inquiries'::regclass AND contype='c' "
        "AND pg_get_constraintdef(oid) LIKE '%paper_venue%'"
    ):
        await conn.execute(
            f'ALTER TABLE inquiries DROP CONSTRAINT "{tbl_drop["conname"]}"'
        )
    await conn.execute(
        "ALTER TABLE inquiries ADD CHECK (CASE WHEN kind = 'Paper' THEN TRUE "
        f"AND (paper_venue IS NULL OR paper_venue IN ({_PRE_008_VENUE_SET})) "
        "ELSE paper_venue IS NULL END)"
    )
    # Drop the change_log publication_type mirror columns (CASCADE clears their
    # kind-gate / value / subject-kind CHECKs).
    await conn.execute(
        "ALTER TABLE change_log DROP COLUMN old_paper_publication_type CASCADE"
    )
    await conn.execute(
        "ALTER TABLE change_log DROP COLUMN new_paper_publication_type CASCADE"
    )
    # Re-add the old closed-set value CHECKs on the venue mirror columns.
    await conn.execute(
        "ALTER TABLE change_log ADD CHECK (old_paper_venue IS NULL OR "
        f"old_paper_venue IN ({_PRE_008_VENUE_SET}))"
    )
    await conn.execute(
        "ALTER TABLE change_log ADD CHECK (new_paper_venue IS NULL OR "
        f"new_paper_venue IN ({_PRE_008_VENUE_SET}))"
    )
    # Revert the kind enum to omit paper_publication_type.
    for con in await conn.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid='change_log'::regclass AND contype='c' "
        "AND pg_get_constraintdef(oid) LIKE '%(kind = ANY%' "
        "AND pg_get_constraintdef(oid) LIKE '%''created''%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%CASE%'"
    ):
        await conn.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{con["conname"]}"')
    await conn.execute(
        f"ALTER TABLE change_log ADD CHECK (kind IN ({_PRE_008_KIND_ENUM}))"
    )
    await conn.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_publication_type_split_migration_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 008 splits venue into publication_type + free-text venue.

    Reverts a fresh DB to the pre-008 conflated-venue shape, seeds Papers
    across the old closed set, clears the ledger, re-bootstraps (008 runs),
    then asserts full catalog set-parity plus the per-row value rewrite.
    """
    store = Store(scratch_engine, embed=StubEmbedder())
    await store.bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        await _revert_to_pre_008(c)
        # Seed Papers spanning the old set: a series (-> inproceedings + name),
        # a journal (-> article + name), and a non-series (-> misc, no venue).
        for seq, venue in enumerate(("NeurIPS", "JMLR", "PREPRINT"), start=1):
            await c.execute(
                "INSERT INTO inquiries (id, kind, seq, title, account, "
                "paper_venue) VALUES ($1, 'Paper', $2, $3, 'a@b.c', $4)",
                uuid.uuid4(),
                seq,
                f"p{seq}",
                venue,
            )

    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    # Per-row rewrite: NeurIPS -> inproceedings/NeurIPS, JMLR -> article/JMLR,
    # PREPRINT -> misc/NULL.
    async with scratch_engine.acquire() as conn:
        rows = {
            r["title"]: (r["paper_publication_type"], r["paper_venue"])
            for r in await conn.fetch(
                "SELECT title, paper_publication_type, paper_venue "
                "FROM inquiries WHERE kind = 'Paper'"
            )
        }
    assert rows["p1"] == ("inproceedings", "NeurIPS")
    assert rows["p2"] == ("article", "JMLR")
    assert rows["p3"] == ("misc", None)

    async with scratch_engine.acquire() as conn:
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert "schema.008.sql" in names


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_008_like_probes_are_idempotent(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Re-running 008 against an already-migrated DB adds no duplicate CHECKs.

    The migration's LIKE probes must match the rendered (cast-laden, ``= ANY``)
    constraint defs, or a re-run re-adds every publication_type CHECK.
    """
    paper_check_count = (
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid IN ('inquiries'::regclass, 'change_log'::regclass) "
        "AND contype='c' AND pg_get_constraintdef(oid) LIKE '%publication_type%'"
    )
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        before = await c.fetchval(paper_check_count)
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    async with scratch_engine.acquire() as conn:
        after = await conn.fetchval(paper_check_count)
    assert after == before, (
        f"008 re-run added duplicate publication_type CHECKs: {before} -> {after}"
    )


# Pre-015 form of the edges edge-validity CHECK: the current (post-010) six-kind
# body WITHOUT the cites_paper arm. Reverting a fresh DB to this is the shape a
# DB deployed before cites_paper carried, so migration 015's edge-CHECK widen
# runs against it.
_PRE_015_EDGE_CHECK = (
    "(edge_kind = 'narrows'"
    "    AND from_kind = 'Issue' AND to_kind = 'Issue')"
    " OR (edge_kind = 'requires'"
    "    AND from_kind = 'Issue' AND to_kind = 'Issue')"
    f" OR (edge_kind = 'produced_by'"
    f"    AND from_kind IN ({_INQUIRY_KINDS_SQL})"
    f"    AND to_kind IN ({_INQUIRY_KINDS_SQL}))"
    f" OR (edge_kind IN ('proves', 'favors')"
    f"    AND from_kind IN ({_ARTIFACT_KINDS_SQL})"
    f"    AND to_kind IN ('Belief', 'Experiment'))"
    f" OR (edge_kind = 'supersedes'"
    f"    AND from_kind IN ({_INQUIRY_KINDS_SQL})"
    f"    AND to_kind IN ({_INQUIRY_KINDS_SQL}))"
)

# Pre-015 peer-edge-kind closed set: the six edge kinds Edge.Kind carried before
# cites_paper. The fresh baseline regenerates the change_log {edge_kinds}
# membership CHECKs from the (now 7-kind) literal, so reverting to this list is
# the shape a DB deployed before cites_paper carried.
_PRE_015_EDGE_KINDS_SQL = (
    "'narrows', 'requires', 'produced_by', 'proves', 'favors', 'supersedes'"
)


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_cites_paper_migration_015_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 015 adds the cites_paper edge kind to fresh parity.

    Reverts a fresh DB to the pre-015 edge shape -- the edge-validity CHECK
    WITHOUT the cites_paper arm, and the two change_log peer-edge-kind enums to
    their six-kind list -- clears the numbered ledger, re-bootstraps (015 runs),
    and asserts full catalog set-parity. Then seeds two Papers and confirms a
    cites_paper edge inserts (the new arm admits it) while the same edge between
    two non-Paper kinds is rejected (Paper -> Paper only).
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # 1. Revert the edges edge-validity CHECK to the pre-015 (no cites_paper)
        #    body. Match the arm-enumerating constraint (names edge_kind + both
        #    endpoint kinds) and swap it for the six-kind form.
        for r in await c.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'edges'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%cites_paper%' "
            "AND pg_get_constraintdef(oid) LIKE '%from_kind%' "
            "AND pg_get_constraintdef(oid) LIKE '%to_kind%'"
        ):
            await c.execute(f'ALTER TABLE edges DROP CONSTRAINT "{r["conname"]}"')
        await c.execute(f"ALTER TABLE edges ADD CHECK ({_PRE_015_EDGE_CHECK})")
        # 2. Revert the two change_log peer-edge-kind enum CHECKs (old/new) to the
        #    six-kind list. Match the pure-membership CHECK (enumerates
        #    'supersedes'), never the priority/valence CHECKs that also name
        #    peer_edge_kind.
        for side in ("old", "new"):
            # pg_get_constraintdef renders IN(...) as = ANY(ARRAY[...]); match the
            # column + 'supersedes' (membership-enum only), not a literal IN.
            for r in await c.fetch(
                "SELECT conname FROM pg_constraint "  # noqa: S608 -- side from the hardcoded tuple.
                "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
                f"AND pg_get_constraintdef(oid) LIKE '%{side}_peer_edge_kind%' "
                "AND pg_get_constraintdef(oid) LIKE '%supersedes%' "
                "AND pg_get_constraintdef(oid) LIKE '%cites_paper%'"
            ):
                await c.execute(
                    f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"'
                )
            await c.execute(
                f"ALTER TABLE change_log ADD CHECK ({side}_peer_edge_kind IS NULL OR "
                f"{side}_peer_edge_kind IN ({_PRE_015_EDGE_KINDS_SQL}))"
            )
        await c.execute("DELETE FROM applied_migrations WHERE name <> 'schema.sql'")

    # Re-bootstrap: schema.015.sql widens the edge + peer-edge-kind CHECKs.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after cites_paper migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # The widened arm admits a Paper -> Paper cites_paper edge.
        cited, citing = uuid.uuid4(), uuid.uuid4()
        for rid in (cited, citing):
            await _seed_old_inquiry(c, rid, "Paper")
        await c.execute(
            "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
            "VALUES ($1, 'Paper', $2, 'Paper', 'cites_paper')",
            citing,
            cited,
        )
        # But a cites_paper edge between non-Paper kinds is rejected.
        belief = uuid.uuid4()
        await _seed_old_inquiry(c, belief, "Belief")
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await c.execute(
                "INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind) "
                "VALUES ($1, 'Belief', $2, 'Paper', 'cites_paper')",
                belief,
                cited,
            )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert "schema.015.sql" in names


# Deployed-016 change_log kind enum: the current enum with the two SPLIT kinds
# (``paper_google_scholar_cluster_id`` / ``paper_google_scholar_cites_id``)
# replaced by the single shipped-016 kind ``paper_google_scholar_id``. Reverting
# a fresh DB to this reconstructs the shape a DB deployed at 016 carried, so
# migration 017's split runs.
_DEPLOYED_016_KIND_ENUM = (
    "'created', 'purged', 'status', 'title', 'description', 'labels', 'owner', "
    "'account', 'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation', "
    "'issue_priority', 'belief_judgement', 'belief_confidence', "
    "'experiment_outcome', 'experiment_config', 'experiment_codechanges', "
    "'paper_abstract', 'paper_authors', 'paper_publication_type', 'paper_venue', "
    "'paper_subvenue', 'paper_publish_date', 'paper_source', "
    "'paper_google_scholar_id', 'codechange_sha', "
    "'webresult_url', 'websearch_query', 'websearch_provider', 'agentsession_cli', "
    "'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', "
    "'agentsession_rooms', 'edge_added', 'edge_removed', "
    "'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', "
    "'implicit_subs_closed'"
)


async def _revert_to_deployed_016(c: Conn) -> None:
    """Reshape a fresh (post-017) DB back to the shape a DB deployed at 016 had.

    Drops the two split columns + their four change_log mirrors, adds back the
    single ``paper_google_scholar_id`` column + its two mirrors with 016's
    CHECKs, and reverts the kind enum to list the single kind. This is 016's
    durable footprint -- the state migration 017 must transform.
    """
    # Drop the split inquiries columns + their mirrors (CHECKs cascade).
    await c.execute(
        "ALTER TABLE inquiries DROP COLUMN paper_google_scholar_cluster_id CASCADE"
    )
    await c.execute(
        "ALTER TABLE inquiries DROP COLUMN paper_google_scholar_cites_id CASCADE"
    )
    for col in (
        "old_paper_google_scholar_cluster_id",
        "new_paper_google_scholar_cluster_id",
        "old_paper_google_scholar_cites_id",
        "new_paper_google_scholar_cites_id",
    ):
        await c.execute(f"ALTER TABLE change_log DROP COLUMN {col} CASCADE")
    # Re-create the single shipped-016 inquiries column + its per-kind CHECK.
    await c.execute("ALTER TABLE inquiries ADD COLUMN paper_google_scholar_id TEXT")
    await c.execute(
        "ALTER TABLE inquiries ADD CHECK (CASE WHEN kind = 'Paper' THEN TRUE "
        "ELSE paper_google_scholar_id IS NULL END)"
    )
    # Re-create the single-kind change_log mirrors + populated-iff + matrix CHECKs.
    await c.execute(
        "ALTER TABLE change_log ADD COLUMN old_paper_google_scholar_id TEXT"
    )
    await c.execute(
        "ALTER TABLE change_log ADD COLUMN new_paper_google_scholar_id TEXT"
    )
    await c.execute(
        "ALTER TABLE change_log ADD CHECK (kind = 'paper_google_scholar_id' "
        "OR old_paper_google_scholar_id IS NULL)"
    )
    await c.execute(
        "ALTER TABLE change_log ADD CHECK (kind = 'paper_google_scholar_id' "
        "OR new_paper_google_scholar_id IS NULL)"
    )
    await c.execute(
        "ALTER TABLE change_log ADD CHECK (kind <> 'paper_google_scholar_id' "
        "OR subject_kind = 'Paper')"
    )
    # Revert the kind enum to the single-kind list (drop the split kinds).
    for r in await c.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'change_log'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%(kind = ANY%' "
        "AND pg_get_constraintdef(oid) LIKE '%''created''%' "
        "AND pg_get_constraintdef(oid) NOT LIKE '%CASE%'"
    ):
        await c.execute(f'ALTER TABLE change_log DROP CONSTRAINT "{r["conname"]}"')
    await c.execute(
        f"ALTER TABLE change_log ADD CHECK (kind IN ({_DEPLOYED_016_KIND_ENUM}))"
    )


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_google_scholar_split_migration_017_reaches_fresh_parity(
    scratch_engine: postgres.PostgresEngine,
) -> None:
    """Migration 017 splits the single Scholar handle into cluster_id + cites_id.

    Reverts a fresh DB to the deployed-016 shape (one ``paper_google_scholar_id``
    column + its two mirrors + the single enum kind), seeds a Paper carrying a
    value plus a matching audit row, clears the numbered ledger, and
    re-bootstraps so 017 runs. Asserts full catalog parity with fresh AND that
    017 CARRIED the data: the inquiries value and the audit row migrate onto the
    ``*_cites_id`` names (the old single handle held a cites_id), and the retired
    column / kind are gone. Finally re-runs 017 a SECOND time (clear ledger,
    re-bootstrap) to pin RE-RUN IDEMPOTENCY -- the recovery path the migration
    doc standardizes on -- since Arm 4's data-copy references the now-dropped
    ``paper_google_scholar_id`` and must be guarded, or a replay 500s.
    """
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    fresh = await _catalog(scratch_engine)

    seeded_paper = uuid.uuid4()
    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        await _revert_to_deployed_016(c)
        # Seed a Paper with the old single handle + a matching audit row, so 017's
        # data-carry arms are exercised (not just the DDL).
        await _seed_old_inquiry(c, seeded_paper, "Paper")
        await c.execute(
            "UPDATE inquiries SET paper_google_scholar_id = '55555' WHERE id = $1",
            seeded_paper,
        )
        await c.execute(
            "INSERT INTO change_log "
            "(id, subject_id, subject_kind, kind, actor, "
            " old_paper_google_scholar_id, new_paper_google_scholar_id) "
            "VALUES ($1, $2, 'Paper', 'paper_google_scholar_id', 'seed', "
            " NULL, '55555')",
            uuid.uuid4(),
            seeded_paper,
        )
        # Drop ONLY 017 from the ledger so just it re-runs -- 016 stays applied,
        # exactly as on the deployed DB (016 is durable; its {change_kinds}
        # placeholder now renders the split literal, so re-running it would fail
        # on the seeded old-kind row -- which is precisely why 016 must NOT be
        # re-run and the split lives in 017).
        await c.execute("DELETE FROM applied_migrations WHERE name = 'schema.017.sql'")

    # Re-bootstrap: schema.017.sql splits the column, carries data, retires the
    # old column/kind/mirrors.
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    migrated = await _catalog(scratch_engine)

    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ migrated[section]
        assert not drift, (
            f"{section} drift after google_scholar_id split migration:\n"
            f"  fresh-only: {sorted(fresh[section] - migrated[section])}\n"
            f"  migrated-only: {sorted(migrated[section] - fresh[section])}"
        )

    async with scratch_engine.acquire() as conn:
        c = cast(Conn, conn)
        # 017 carried the inquiries value onto cites_id; cluster_id starts empty.
        carried_cites = await c.fetchval(
            "SELECT paper_google_scholar_cites_id FROM inquiries WHERE id = $1",
            seeded_paper,
        )
        carried_cluster = await c.fetchval(
            "SELECT paper_google_scholar_cluster_id FROM inquiries WHERE id = $1",
            seeded_paper,
        )
        # 017 carried the audit row onto the cites_id kind + mirror.
        audit = await c.fetchrow(
            "SELECT kind, new_paper_google_scholar_cites_id AS v FROM change_log "
            "WHERE subject_id = $1 AND kind = 'paper_google_scholar_cites_id'",
            seeded_paper,
        )
        # No row still carries the retired kind, and both new columns accept edits.
        stale = await c.fetchval(
            "SELECT count(*) FROM change_log WHERE kind = 'paper_google_scholar_id'"
        )
        await c.execute(
            "UPDATE inquiries SET paper_google_scholar_cluster_id = '12345' "
            "WHERE id = $1",
            seeded_paper,
        )
        stored = await c.fetchval(
            "SELECT paper_google_scholar_cluster_id FROM inquiries WHERE id = $1",
            seeded_paper,
        )
        names = {
            r["name"] for r in await conn.fetch("SELECT name FROM applied_migrations")
        }
    assert carried_cites == "55555"
    assert carried_cluster is None
    assert audit is not None
    assert audit["v"] == "55555"
    assert stale == 0
    assert stored == "12345"
    assert "schema.017.sql" in names

    # Re-run idempotency: clear 017 from the ledger and re-bootstrap on the
    # already-migrated (split) DB. The retired ``paper_google_scholar_id`` column
    # is GONE now, so an unguarded Arm 4 data-copy would 500 with
    # UndefinedColumnError -- the documented "clear ledger row, restart" recovery
    # must be a clean no-op. Catalog stays at fresh parity.
    async with scratch_engine.acquire() as conn:
        await conn.execute(
            "DELETE FROM applied_migrations WHERE name = 'schema.017.sql'"
        )
    await Store(scratch_engine, embed=StubEmbedder()).bootstrap()
    replayed = await _catalog(scratch_engine)
    for section in ("columns", "constraints", "indexes", "sequences"):
        drift = fresh[section] ^ replayed[section]
        assert not drift, f"{section} drift after 017 re-run (idempotency):\n{drift}"
