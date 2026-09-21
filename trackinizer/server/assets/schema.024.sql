-- schema.024.sql -- session-index freshness markers (decouple sweep freshness
-- from the presence of a vector).
--
-- Adds ``session_index_state``: one row per (record, mapper, model) the
-- embedding sweep has reconciled, carrying the ``md5(text)`` it saw. Before
-- this table the sweep keyed freshness on the presence of a
-- ``session_embeddings`` row, so a record with NO vector (a machine-output
-- head, a SystemMessage, a Thinking block -- all fts-only under the narrowed
-- footprint policy) had nothing to mark and re-swept forever; the sweep RAISED
-- rather than loop. The marker lets an fts-only record read up-to-date.
--
-- The baseline ``schema.sql`` carries the same table for a fresh install; a
-- fresh DB records this migration applied WITHOUT executing it, an existing DB
-- records the baseline unrun and executes only this file, so the two must stay
-- in step -- pinned by ``schema_migration_test.py``.
--
-- Numbered 024: the deployed ledger holds through schema.023.sql.
--
-- BACKFILL, not pure DDL. The INSERT ... SELECT seeds one marker per existing
-- (session_id, part, idx, mapper, model) group in ``session_embeddings`` so
-- ALREADY-EMBEDDED records read up-to-date after deploy and NOTHING re-embeds.
-- Every unit of one record shares its source text, so ``max(text_md5)`` over the
-- group is that record's single md5 (the group is md5-homogeneous by
-- construction of the sweep). Idempotent: ``ON CONFLICT DO NOTHING`` on the PK,
-- so a re-run after a partial apply adds only the missing markers.
--
-- Safe against the OLD code: purely additive into a table the old build never
-- reads, its duration is not downtime (run it against the live database with the
-- old server still serving; see docs/db_schema_migration.md). It is a backfill,
-- so it uses ONE core -- shard by ``session_id`` for the live run if the row
-- count warrants it (see the migration doc's sharding recipe).

CREATE TABLE IF NOT EXISTS session_index_state (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    text_md5    TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, part, idx, mapper, model)
);

-- Seed markers from existing vectors so no already-embedded record re-embeds.
INSERT INTO session_index_state
    (session_id, part, idx, mapper, model, text_md5)
SELECT session_id, part, idx, mapper, model, max(text_md5)
FROM session_embeddings
GROUP BY session_id, part, idx, mapper, model
ON CONFLICT (session_id, part, idx, mapper, model) DO NOTHING;
