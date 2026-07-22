-- Migration 016: add the optional Paper.google_scholar_id column.
--
-- A Google Scholar cluster handle (``cites_id``) that coexists with
-- ``paper_source`` on one Paper: cite by DOI/arXiv (in source), pivot the
-- cited-by graph via Scholar (this column). The baseline ``schema.sql`` declares
-- it as a Paper-only nullable column, generates its ``change_log`` old_/new_
-- mirror from ``CHANGE_LOG_COLUMN_ORDER``, and admits the
-- ``'paper_google_scholar_id'`` field-change kind through ``{change_kinds}``.
-- This migration reproduces that whole set on a DB deployed at 015 so
-- migrate-from-015 and fresh-from-016 reach catalog parity. Optional column, so
-- no backfill, NOT NULL promotion, or index (unlike migration 007's account).
--
-- Arms:
--   1. ``inquiries.paper_google_scholar_id`` column + its per-kind CASE-WHEN
--      CHECK (Paper-only; NULL for every other kind).
--   2. ``change_log`` old_/new_ mirror columns + their optional populated-iff
--      CHECKs (``kind = 'X' OR old_X IS NULL``) and the subject-kind matrix
--      CHECK (``kind <> 'X' OR subject_kind = 'Paper'``).
--   3. Re-render ``change_log_kind_check`` from the full ``{change_kinds}``
--      literal so ``'paper_google_scholar_id'`` becomes an admitted kind.

-- Arm 1: nullable Paper-only column. IF NOT EXISTS makes a re-render against a
-- DB already at 016 (or fresh) a no-op; the CASE-WHEN CHECK is added only when
-- absent (discovered by the column token, since it is unnamed).
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_google_scholar_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_id%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper'
                THEN TRUE
                ELSE paper_google_scholar_id IS NULL
            END
        );
    END IF;
END $$;

-- Arm 2: change_log mirror columns + their optional populated-iff CHECKs and
-- the subject-kind matrix CHECK. Columns added IF NOT EXISTS; each CHECK added
-- only when absent.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_google_scholar_id TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_google_scholar_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%old_paper_google_scholar_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_id' OR old_paper_google_scholar_id IS NULL
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%new_paper_google_scholar_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_id' OR new_paper_google_scholar_id IS NULL
        );
    END IF;
    -- Subject-kind matrix: a paper_google_scholar_id change must target a Paper.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_id%'
          AND pg_get_constraintdef(oid) LIKE '%subject_kind%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind <> 'paper_google_scholar_id' OR subject_kind = 'Paper'
        );
    END IF;
END $$;

-- Arm 3: admit the new field-change kind. DROP-then-ADD re-renders the FULL
-- current {change_kinds} literal (the same single source the baseline uses), so
-- this both adds 'paper_google_scholar_id' and widens any stale list. The enum
-- is the unique change_log CHECK that lists 'created' as a kind member and is
-- not the edge-peer CASE presence CHECK; gate on that (see migration 007).
DO $$
DECLARE
    con_name TEXT;
BEGIN
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%(kind = ANY%'
          AND pg_get_constraintdef(oid) LIKE '%''created''%'
          AND pg_get_constraintdef(oid) NOT LIKE '%CASE%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
    ALTER TABLE change_log ADD CHECK (kind IN ({change_kinds}));
END $$;
