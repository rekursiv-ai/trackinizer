-- schema.025.sql -- index receipt membership in existing Experiment configs.
--
-- Adds a GIN index over ``inquiries.experiment_config`` so the execution-receipt
-- reverse lookup (``experiment_config @> {...}`` containment) is served by an
-- index rather than a sequential scan of every Experiment row.
--
-- The baseline ``schema.sql`` carries the same index for a fresh install; a
-- fresh DB records this migration applied WITHOUT executing it, an existing DB
-- records the baseline unrun and executes only this file, so the two must stay
-- in step -- pinned by ``schema_migration_test.py``.
--
-- Numbered 025: the deployed ledger holds through schema.024.sql. (Reassigned
-- from 022 on merge: main's session-index migrations took 022-024 concurrently,
-- so this receipt index moved to the next free slot.)
--
-- Purely additive DDL against an existing column, safe against the OLD code and
-- its duration is not downtime. Migrations run transactionally, so CREATE INDEX
-- cannot use CONCURRENTLY (see docs/db_schema_migration.md).
CREATE INDEX IF NOT EXISTS idx_inquiries_experiment_config_gin
    ON inquiries USING gin(experiment_config jsonb_path_ops);
