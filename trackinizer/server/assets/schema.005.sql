-- Migration 005: the #425/#426 trax-grammar + Paper-bibliography delta.
--
-- One migration for one push. It moves a database deployed at migration 004 to
-- the current ``schema.sql`` shape, carrying five coordinated changes that ship
-- together:
--   A. Base Inquiry field ``summary`` -> ``title`` (the one-line headline;
--      ``description`` unchanged).
--   B. Paper drops ``source_kind`` and grows five bibliography fields
--      (``abstract``, ``authors``, ``venue``, ``subvenue``, ``publish_date``),
--      with ``source`` repurposed to a scheme-tagged identifier.
--   C. The ``favors`` / ``disfavors`` citation edges flip direction to
--      Artifact -> Belief (evidence acts on the belief).
--   D. Drop ``WebSearch.results`` -- search->finding membership is recorded as
--      ``produces`` edges (found Artifacts are many-to-one, so the edge set is
--      the membership). Drops the ``websearch_results`` inquiries column and its
--      two change_log mirrors; the ``'websearch_results'`` change kind is already
--      omitted from arm B's final ``change_log_kind_check``.
--   E. Add the AgentSession change_log audit mirrors (10 ``old_/new_
--      agentsession_*`` columns + CHECKs). The kinds were always valid but the
--      mirror columns were never added, so AgentSession field edits logged an
--      event with a NULL snapshot (silent audit-value loss).
--
-- Numbered migrations run only on databases that recorded the baseline before
-- this file existed; a fresh database applies the baseline (already in the final
-- shape) and records this name without executing it, so nothing runs twice.
--
-- Each arm is independently gated (information_schema / pg_constraint probes,
-- ``IF [NOT] EXISTS``) so the whole file is a no-op on a fresh or already-migrated
-- DB. ``pg_get_constraintdef`` strips comments and normalizes whitespace, so the
-- re-added defs match a fresh database exactly (the parity test set-compares
-- constraint defs). ``{inquiry_kinds}`` / ``{artifact_kinds}`` expand to the same
-- lists the baseline uses.
--
-- ORDERING NOTE: the single source of truth for the ``change_log`` kind enum is
-- arm B (it writes the FINAL ``change_log_kind_check`` -- ``title`` present,
-- ``paper_source_kind`` absent, the five paper kinds added). Arm A therefore
-- renames the summary columns and rewrites stored ``'summary'`` kind values but
-- does NOT re-add the kind enum CHECK; arm B owns it.

-- =========================================================================
-- ARM A: rename base ``summary`` -> ``title`` (columns + stored kind value).
-- =========================================================================
DO $$
DECLARE
    con_name TEXT;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'inquiries'
          AND column_name = 'summary'
    ) THEN
        -- Drop the two populated-iff CHECKs gating old_summary / new_summary AND
        -- the kind enum CHECK (it names 'summary'); all must be gone before the
        -- column rename and the kind-value rewrite. The kind enum is re-added in
        -- its FINAL form by arm B, so it is intentionally not re-added here.
        FOR con_name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'change_log'::regclass AND contype = 'c'
              AND (pg_get_constraintdef(oid) LIKE '%old_summary%'
                   OR pg_get_constraintdef(oid) LIKE '%new_summary%'
                   OR pg_get_constraintdef(oid) LIKE '%''summary''%')
        LOOP
            EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
        END LOOP;

        ALTER TABLE inquiries RENAME COLUMN summary TO title;
        ALTER TABLE change_log RENAME COLUMN old_summary TO old_title;
        ALTER TABLE change_log RENAME COLUMN new_summary TO new_title;

        UPDATE change_log SET kind = 'title' WHERE kind = 'summary';

        -- Re-add only the two populated-iff mirror CHECKs in title form (the
        -- kind enum is arm B's).
        ALTER TABLE change_log ADD CHECK (
            (old_title IS NOT NULL) = (kind = 'title')
        );
        ALTER TABLE change_log ADD CHECK (
            (new_title IS NOT NULL) = (kind = 'title')
        );

    -- Columns already renamed but a stale 'summary' kind enum survives (e.g.
    -- re-added by schema.001 regardless of the rename): rewrite the stored value;
    -- arm B re-adds the final enum.
    ELSIF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%''summary''%'
    ) THEN
        FOR con_name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'change_log'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%''summary''%'
        LOOP
            EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
        END LOOP;
        UPDATE change_log SET kind = 'title' WHERE kind = 'summary';
    END IF;
END $$;

-- =========================================================================
-- ARM B: Paper bibliography fields (drop source_kind; add five; scheme-tag
-- source; rewrite the change_log mirror + the FINAL kind enum).
-- =========================================================================

-- B1. The five new inquiries columns + their per-kind presence CHECKs.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_abstract TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_authors TEXT[];
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_venue TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_subvenue TEXT;
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_publish_date TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_abstract%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper' THEN TRUE ELSE paper_abstract IS NULL END
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_authors%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper' THEN TRUE ELSE paper_authors IS NULL END
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_venue%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper'
                THEN TRUE
             AND (paper_venue IS NULL OR paper_venue IN ('PREPRINT', 'BOOK', 'THESIS', 'REPORT', 'OTHER', 'NeurIPS', 'ICML', 'ICLR', 'ACL', 'AAAI', 'CVPR', 'JMLR', 'TMLR'))
                ELSE paper_venue IS NULL
            END
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_subvenue%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper' THEN TRUE ELSE paper_subvenue IS NULL END
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_publish_date%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper' THEN TRUE ELSE paper_publish_date IS NULL END
        );
    END IF;
END $$;

-- B2. Scheme-tag existing paper_source from the old paper_source_kind.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'inquiries'
          AND column_name = 'paper_source_kind'
    ) THEN
        UPDATE inquiries
            SET paper_source = 'arXiv:' || paper_source
            WHERE kind = 'Paper' AND paper_source_kind = 'arxiv'
              AND paper_source IS NOT NULL AND paper_source NOT LIKE 'arXiv:%';
        UPDATE inquiries
            SET paper_source = 'doi:' || paper_source
            WHERE kind = 'Paper' AND paper_source_kind = 'doi'
              AND paper_source IS NOT NULL AND paper_source NOT LIKE 'doi:%';
    END IF;
END $$;

-- B3. Drop the paper_source_kind column (its inline CHECK drops with it).
ALTER TABLE inquiries DROP COLUMN IF EXISTS paper_source_kind;

-- B4a. change_log old_/new_ mirrors for the five new audited fields.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_abstract TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_abstract TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_authors TEXT[];
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_authors TEXT[];
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_venue TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_venue TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_subvenue TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_subvenue TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_publish_date TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_publish_date TIMESTAMPTZ;

-- B4b. The mirror CHECKs (populated-iff, venue closed-set, subject_kind matrix).
DO $$
DECLARE
    rec RECORD;
BEGIN
    -- Each probe is a CAST-AGNOSTIC substring of the rendered constraint def
    -- (pg_get_constraintdef inserts ``::text`` casts and extra parens, so the
    -- raw body text is NOT a substring of the rendered form -- see R12/ARM E).
    -- Distinguishing tokens:
    --   populated-iff -> ``<col> IS NULL))`` (double close-paren; the venue
    --                    closed-set ends ``IS NULL) OR (...)`` instead);
    --   venue closed-set -> ``<col> = ANY`` (PG renders ``IN`` as ``= ANY``);
    --   subject-kind matrix -> ``<> ''<col>''%subject_kind`` (``%`` skips the
    --                    cast between the two stable tokens).
    FOR rec IN
        SELECT * FROM (VALUES
            ('old_paper_abstract IS NULL))',
             'kind = ''paper_abstract'' OR old_paper_abstract IS NULL'),
            ('new_paper_abstract IS NULL))',
             'kind = ''paper_abstract'' OR new_paper_abstract IS NULL'),
            ('old_paper_authors IS NULL))',
             'kind = ''paper_authors'' OR old_paper_authors IS NULL'),
            ('new_paper_authors IS NULL))',
             'kind = ''paper_authors'' OR new_paper_authors IS NULL'),
            ('old_paper_venue IS NULL))',
             'kind = ''paper_venue'' OR old_paper_venue IS NULL'),
            ('new_paper_venue IS NULL))',
             'kind = ''paper_venue'' OR new_paper_venue IS NULL'),
            ('old_paper_subvenue IS NULL))',
             'kind = ''paper_subvenue'' OR old_paper_subvenue IS NULL'),
            ('new_paper_subvenue IS NULL))',
             'kind = ''paper_subvenue'' OR new_paper_subvenue IS NULL'),
            ('old_paper_publish_date IS NULL))',
             'kind = ''paper_publish_date'' OR old_paper_publish_date IS NULL'),
            ('new_paper_publish_date IS NULL))',
             'kind = ''paper_publish_date'' OR new_paper_publish_date IS NULL'),
            ('old_paper_venue = ANY',
             'old_paper_venue IS NULL OR old_paper_venue IN (''PREPRINT'', ''BOOK'', ''THESIS'', ''REPORT'', ''OTHER'', ''NeurIPS'', ''ICML'', ''ICLR'', ''ACL'', ''AAAI'', ''CVPR'', ''JMLR'', ''TMLR'')'),
            ('new_paper_venue = ANY',
             'new_paper_venue IS NULL OR new_paper_venue IN (''PREPRINT'', ''BOOK'', ''THESIS'', ''REPORT'', ''OTHER'', ''NeurIPS'', ''ICML'', ''ICLR'', ''ACL'', ''AAAI'', ''CVPR'', ''JMLR'', ''TMLR'')'),
            ('<> ''paper_abstract''%subject_kind',
             'kind <> ''paper_abstract'' OR subject_kind = ''Paper'''),
            ('<> ''paper_authors''%subject_kind',
             'kind <> ''paper_authors'' OR subject_kind = ''Paper'''),
            ('<> ''paper_venue''%subject_kind',
             'kind <> ''paper_venue'' OR subject_kind = ''Paper'''),
            ('<> ''paper_subvenue''%subject_kind',
             'kind <> ''paper_subvenue'' OR subject_kind = ''Paper'''),
            ('<> ''paper_publish_date''%subject_kind',
             'kind <> ''paper_publish_date'' OR subject_kind = ''Paper''')
        ) AS t(probe, body)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'change_log'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%' || rec.probe || '%'
        ) THEN
            EXECUTE format('ALTER TABLE change_log ADD CHECK (%s)', rec.body);
        END IF;
    END LOOP;
END $$;

-- B4c. Drop the paper_source_kind change_log mirror CHECKs + matrix CHECK, then
--      the mirror columns.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND (pg_get_constraintdef(oid) LIKE '%old_paper_source_kind%'
               OR pg_get_constraintdef(oid) LIKE '%new_paper_source_kind%'
               OR pg_get_constraintdef(oid) LIKE '%<> ''paper_source_kind''%')
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
END $$;
ALTER TABLE change_log DROP COLUMN IF EXISTS old_paper_source_kind;
ALTER TABLE change_log DROP COLUMN IF EXISTS new_paper_source_kind;

-- B4d. The FINAL change_log kind enum: 'title' present, 'paper_source_kind'
--      and 'websearch_results' absent (the latter's column is dropped by arm D),
--      the five new paper field kinds added, in Change.Kind order.
--
-- First purge the historical audit rows for the removed 'websearch_results'
-- field, or the ADD CONSTRAINT fails validation against them and bootstrap
-- crashes (the live origin's 502: 8 such rows predated the field's removal).
-- 'summary' rows were already rewritten to 'title' by arm A, so only this kind
-- remains. A removed field has no successor kind, and its
-- old_/new_websearch_results snapshot columns are dropped by arm D, so deleting
-- the now-payloadless rows is the only coherent option.
--
-- A cascade-caused edit (e.g. a 'dependency_changed' row) may carry a
-- ``caused_by`` self-FK pointing at one of these rows, so the bare DELETE would
-- hit ``change_log_caused_by_fkey``. Sever those soft causal links first
-- (``caused_by`` is nullable) -- the child audit rows are legitimate history of
-- real cascades; only their pointer to the now-deleted cause is dropped.
UPDATE change_log SET caused_by = NULL
    WHERE caused_by IN (SELECT id FROM change_log WHERE kind = 'websearch_results');
DELETE FROM change_log WHERE kind = 'websearch_results';
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_kind_check;
ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check CHECK (kind IN (
    'created', 'purged', 'status', 'title', 'description', 'labels', 'owner',
    'subscribers', 'marginal_cost', 'issue_kind', 'issue_validation',
    'issue_priority', 'belief_judgement', 'belief_confidence',
    'experiment_outcome', 'experiment_codechanges', 'paper_abstract',
    'paper_authors', 'paper_venue', 'paper_subvenue', 'paper_publish_date',
    'paper_source', 'codechange_sha', 'webresult_url', 'websearch_query',
    'websearch_provider', 'agentsession_cli',
    'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended',
    'agentsession_rooms', 'edge_added', 'edge_removed',
    'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened',
    'implicit_subs_closed'
));

-- =========================================================================
-- ARM C: flip favors / disfavors to Artifact -> Belief (evidence acts on the
-- belief). Swap stored endpoints; split favors/disfavors into their own arm of
-- the edge-validity CHECK. proves/disproves stay Belief -> Artifact.
-- =========================================================================
DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'edges'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%from_kind%'
      AND pg_get_constraintdef(oid) LIKE '%favors%'
      AND pg_get_constraintdef(oid) NOT LIKE '%to_kind = ''Belief''%';
    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);

        UPDATE edges
        SET from_id = to_id, to_id = from_id,
            from_kind = to_kind, to_kind = from_kind
        WHERE edge_kind IN ('favors', 'disfavors');

        ALTER TABLE edges ADD CHECK (
            (edge_kind = 'narrows_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'blocks_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'produces'
                AND from_kind IN ({inquiry_kinds})
                AND to_kind IN ({artifact_kinds}))
         OR (edge_kind IN ('proves', 'disproves')
                AND from_kind = 'Belief'
                AND to_kind IN ({artifact_kinds}))
         OR (edge_kind IN ('favors', 'disfavors')
                AND from_kind IN ({artifact_kinds})
                AND to_kind = 'Belief')
         OR (edge_kind = 'supersedes'
                AND from_kind IN ({inquiry_kinds})
                AND to_kind IN ({inquiry_kinds}))
         OR (edge_kind = 'refutes_experiment'
                AND from_kind = 'Experiment' AND to_kind = 'Experiment')
        );
    END IF;
END $$;

-- =========================================================================
-- ARM D: drop WebSearch.results. Membership now lives on the ``produces`` edge
-- set (found Artifacts are many-to-one). Drop the JSONB column and its two
-- change_log mirrors; arm B already wrote the kind enum without
-- ``'websearch_results'``. Each drop is IF EXISTS, so this is a no-op on a fresh
-- or already-migrated DB.
-- =========================================================================
ALTER TABLE inquiries DROP COLUMN IF EXISTS websearch_results;
ALTER TABLE change_log DROP COLUMN IF EXISTS old_websearch_results;
ALTER TABLE change_log DROP COLUMN IF EXISTS new_websearch_results;

-- =========================================================================
-- ARM E: add the AgentSession change_log audit mirrors. The five
-- ``agentsession_*`` field-change kinds were always valid Change.Kind values
-- (the kind enum lists them), but the change_log carried no ``old_/new_``
-- columns for them, so ``set_cli`` / ``set_rooms`` etc. logged an event with a
-- NULL snapshot -- the value was silently dropped from the audit. Add the ten
-- mirror columns plus their populated-iff and kind-matrix CHECKs to match what a
-- fresh schema generates. Idempotent: column adds are IF NOT EXISTS; the CHECKs
-- are added only when absent.
-- =========================================================================
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_agentsession_cli TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_agentsession_cli TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_agentsession_cli_session_id TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_agentsession_cli_session_id TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_agentsession_started TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_agentsession_started TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_agentsession_ended TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_agentsession_ended TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_agentsession_rooms TEXT[];
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_agentsession_rooms TEXT[];

DO $$
DECLARE
    col TEXT;
    side TEXT;
BEGIN
    FOREACH col IN ARRAY ARRAY[
        'agentsession_cli', 'agentsession_cli_session_id',
        'agentsession_started', 'agentsession_ended', 'agentsession_rooms'
    ] LOOP
        FOREACH side IN ARRAY ARRAY['old', 'new'] LOOP
            -- populated-iff: the mirror is non-NULL only on its own kind event.
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'change_log'::regclass AND contype = 'c'
                  AND pg_get_constraintdef(oid)
                      -- Match cast-agnostically: pg_get_constraintdef renders
                      -- ``kind = 'x'::text`` with parens/casts, so probe the mirror
                      -- column's ``<col> IS NULL`` token, which survives normalization.
                      LIKE '%' || side || '_' || col || ' IS NULL%'
            ) THEN
                EXECUTE format(
                    'ALTER TABLE change_log ADD CHECK (kind = %L OR %I IS NULL)',
                    col, side || '_' || col
                );
            END IF;
        END LOOP;
        -- kind-matrix: an agentsession field event must have an AgentSession subject.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'change_log'::regclass AND contype = 'c'
              -- Cast-agnostic: the rendered form is ``kind <> 'col'::text OR
              -- subject_kind = 'AgentSession'``; match on the quoted col plus the
              -- subject-kind clause, both of which survive normalization.
              AND pg_get_constraintdef(oid) LIKE '%''' || col || '''%'
              AND pg_get_constraintdef(oid) LIKE '%subject_kind%AgentSession%'
              AND pg_get_constraintdef(oid) LIKE '%kind <>%'
        ) THEN
            EXECUTE format(
                'ALTER TABLE change_log ADD CHECK (kind <> %L OR subject_kind = ''AgentSession'')',
                col
            );
        END IF;
    END LOOP;
END $$;
