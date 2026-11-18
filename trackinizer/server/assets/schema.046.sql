-- schema.046.sql -- every row carries ``app``: what an application layered on trackinizer
-- keeps on the row, as one JSON object keyed by app name, at most 64 KiB. Trackinizer
-- stores and returns it verbatim and never reads it. Edits are audited like
-- ``description``: the change log gains the ``app`` kind and its old/new mirror.
--
-- The constraints below are the ones the baseline produces for the ``Inquiry.app``
-- spec; ``schema_migration_test`` proves the two agree. The baseline leaves the
-- change-log ones unnamed, so they are named here only to make a replay a no-op.
-- Additive: the old build never writes the column or the kind, and every existing row
-- passes each CHECK, so this is safe to apply before the restart. One ALTER per table,
-- so each table is scanned once to validate its new CHECKs.
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS app JSONB,
    DROP CONSTRAINT IF EXISTS inquiries_app_check,
    ADD CONSTRAINT inquiries_app_check CHECK (
        app IS NULL
        OR jsonb_typeof(app) = 'object' AND octet_length(app::text) <= 65536
    );

ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS old_app JSONB,
    ADD COLUMN IF NOT EXISTS new_app JSONB,
    DROP CONSTRAINT IF EXISTS change_log_old_app_kind_check,
    DROP CONSTRAINT IF EXISTS change_log_new_app_kind_check,
    DROP CONSTRAINT IF EXISTS change_log_old_app_check,
    DROP CONSTRAINT IF EXISTS change_log_new_app_check,
    DROP CONSTRAINT change_log_kind_check,
    ADD CONSTRAINT change_log_old_app_kind_check
        CHECK (kind = 'app' OR old_app IS NULL),
    ADD CONSTRAINT change_log_new_app_kind_check
        CHECK (kind = 'app' OR new_app IS NULL),
    ADD CONSTRAINT change_log_old_app_check
        CHECK (old_app IS NULL OR jsonb_typeof(old_app) = 'object' AND octet_length(old_app::text) <= 65536),
    ADD CONSTRAINT change_log_new_app_check
        CHECK (new_app IS NULL OR jsonb_typeof(new_app) = 'object' AND octet_length(new_app::text) <= 65536),
    ADD CONSTRAINT change_log_kind_check
        CHECK (kind IN ('created', 'purged', 'status', 'title', 'description', 'labels', 'owner', 'account', 'subscribers', 'marginal_cost', 'app', 'issue_kind', 'issue_validation', 'issue_priority', 'belief_judgement', 'belief_confidence', 'experiment_outcome', 'experiment_config', 'experiment_codechanges', 'paper_abstract', 'paper_authors', 'paper_publication_type', 'paper_venue', 'paper_subvenue', 'paper_publish_date', 'paper_source', 'paper_google_scholar_cluster_id', 'paper_google_scholar_cites_id', 'codechange_sha', 'webresult_url', 'websearch_query', 'websearch_provider', 'agentsession_cli', 'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', 'agentsession_rooms', 'edge_added', 'edge_removed', 'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', 'implicit_subs_closed'));
