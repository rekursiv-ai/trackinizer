-- Migration 001: room-scoped session messaging.
--
-- Moves a database deployed before room support to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that
-- recorded the baseline before this file existed; a fresh database applies
-- the baseline (which already has everything here) and records this name
-- without executing it, so these statements never run twice.
--
-- Blast radius (every object ``schema.sql`` now builds that an old DB lacks):
--   1. ``inquiries.agentsession_rooms`` column + its per-kind CHECK.
--   2. ``change_log_kind_check`` widened to admit the ``agentsession_rooms``
--      field-change kind (a rooms edit emits an audit row of that kind).
--   3. the cross-session console feed index.

-- 1. The room-membership column and its CHECK. The CHECK def matches the
--    baseline's inline column check (the catalog diff compares constraint
--    *definitions*, not names, so the auto-named baseline constraint and this
--    explicitly-named one are equivalent).
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS agentsession_rooms TEXT[];

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass
          AND pg_get_constraintdef(oid) ILIKE '%agentsession_rooms%'
    ) THEN
        ALTER TABLE inquiries ADD CONSTRAINT inquiries_agentsession_rooms_check CHECK (
            CASE WHEN kind = 'AgentSession'
                THEN TRUE
                ELSE agentsession_rooms IS NULL
            END
        );
    END IF;
END $$;

-- 2. Widen the change_log kind enum to admit ``agentsession_rooms``. Drop and
--    re-add with the full value list in baseline order so the re-rendered
--    constraint def matches a fresh database exactly.
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_kind_check;
ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check CHECK (kind IN (
    'created', 'purged', 'status', 'summary', 'description', 'labels', 'owner',
    'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation',
    'issue_priority', 'belief_judgement', 'belief_confidence',
    'experiment_outcome', 'experiment_codechanges', 'paper_source',
    'paper_source_kind', 'codechange_sha', 'webresult_url', 'websearch_query',
    'websearch_provider', 'websearch_results', 'agentsession_cli',
    'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended',
    'agentsession_rooms', 'edge_added', 'edge_removed',
    'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened',
    'implicit_subs_closed'
));

-- 3. The cross-session console feed orders by ``(created, session_id, seq)``
--    (see ``Store.read_feed``); this index serves that hot polling path.
CREATE INDEX IF NOT EXISTS idx_agent_session_events_created_session_seq
    ON agent_session_events (created, session_id, seq);
