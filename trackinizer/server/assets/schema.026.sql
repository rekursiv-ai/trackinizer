-- schema.026.sql -- client-declared ``recorded`` timestamp for backfilled rows.
--
-- Adds one nullable TIMESTAMPTZ to ``inquiries``, plus its ``old_``/``new_``
-- mirror in ``change_log`` so the edit is audited like every other field.
-- ``created`` and ``modified`` stay server-stamped (``clock_timestamp()``) and
-- authoritative for audit; ``recorded`` carries what the CLIENT declares about
-- when the knowledge originated, so a corpus imported in one batch keeps its
-- own chronology instead of collapsing to import time. NULL means "born here",
-- where ``created`` already answers the question.
--
-- Same shape as the declared-provenance datetimes already in the schema
-- (``AgentSession.started``, ``Paper.publish_date``): the server cannot know
-- the truth, so the client supplies it.
--
-- The baseline ``schema.sql`` carries the same columns for a fresh install; a
-- fresh DB records this migration applied WITHOUT executing it, an existing DB
-- records the baseline unrun and executes only this file, so the two must stay
-- in step -- pinned by ``schema_migration_test.py``.
--
-- Numbered 026: the deployed ledger holds through schema.025.sql.
--
-- Safe against the OLD code and not downtime (run it against the live database
-- with the old server still serving; see docs/db_schema_migration.md). The
-- columns are additive and the old build never reads them. The two mirror
-- gates are added NOT VALID and validated separately: every existing row has
-- both mirrors NULL and so already satisfies them, and this way the scan takes
-- SHARE UPDATE EXCLUSIVE rather than holding ACCESS EXCLUSIVE over a
-- change_log that only grows. A fresh install gets the same two rules as
-- unnamed CHECKs inside CREATE TABLE, auto-named by position; named here
-- because ALTER needs a handle to validate. The rule enforced is identical.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS recorded TIMESTAMPTZ;

ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_recorded TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_recorded TIMESTAMPTZ;

ALTER TABLE change_log
    ADD CONSTRAINT change_log_old_recorded_gate
    CHECK (kind = 'recorded' OR old_recorded IS NULL) NOT VALID;
ALTER TABLE change_log
    ADD CONSTRAINT change_log_new_recorded_gate
    CHECK (kind = 'recorded' OR new_recorded IS NULL) NOT VALID;

ALTER TABLE change_log VALIDATE CONSTRAINT change_log_old_recorded_gate;
ALTER TABLE change_log VALIDATE CONSTRAINT change_log_new_recorded_gate;
