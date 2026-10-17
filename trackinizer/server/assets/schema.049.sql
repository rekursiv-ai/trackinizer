-- schema.049.sql -- client-declared ``recorded`` timestamp for backfilled rows.
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
-- Numbered 049: the deployed ledger holds through schema.048.sql.
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
-- Each is dropped first if present, so a replay of this file changes nothing.
--
-- The change log's kind list gains ``recorded``, so an edit of it can be audited.
-- The list is the one the baseline produces from ``Change.Kind``, in its order, so
-- a migrated database and a fresh one carry the same constraint. Widening it admits
-- every row the old one did, so it is validated in place.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS recorded TIMESTAMPTZ;

ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_recorded TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_recorded TIMESTAMPTZ;

ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_old_recorded_gate,
    ADD CONSTRAINT change_log_old_recorded_gate
    CHECK (kind = 'recorded' OR old_recorded IS NULL) NOT VALID;
ALTER TABLE change_log
    DROP CONSTRAINT IF EXISTS change_log_new_recorded_gate,
    ADD CONSTRAINT change_log_new_recorded_gate
    CHECK (kind = 'recorded' OR new_recorded IS NULL) NOT VALID;

ALTER TABLE change_log VALIDATE CONSTRAINT change_log_old_recorded_gate;
ALTER TABLE change_log VALIDATE CONSTRAINT change_log_new_recorded_gate;

ALTER TABLE change_log
    DROP CONSTRAINT change_log_kind_check,
    ADD CONSTRAINT change_log_kind_check
        CHECK (kind IN ('created', 'purged', 'status', 'title', 'description', 'labels', 'owner', 'account', 'subscribers', 'marginal_cost', 'app', 'recorded', 'issue_kind', 'issue_validation', 'issue_priority', 'belief_judgement', 'belief_confidence', 'experiment_outcome', 'experiment_config', 'experiment_codechanges', 'paper_abstract', 'paper_authors', 'paper_publication_type', 'paper_venue', 'paper_subvenue', 'paper_publish_date', 'paper_source', 'paper_google_scholar_cluster_id', 'paper_google_scholar_cites_id', 'codechange_sha', 'webresult_url', 'websearch_query', 'websearch_provider', 'agentsession_cli', 'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', 'agentsession_rooms', 'agentsession_audience', 'edge_added', 'edge_removed', 'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', 'implicit_subs_closed'));
