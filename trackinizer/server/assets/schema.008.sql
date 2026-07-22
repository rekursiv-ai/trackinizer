-- Migration 008: split the closed ``paper_venue`` enum into a closed BibTeX
-- ``paper_publication_type`` and a free-text ``paper_venue``.
--
-- The baseline ``schema.sql`` formerly modelled venue as one closed set that
-- conflated the publication *type* (preprint/book/thesis/report) with an
-- arbitrary 8-venue ML series whitelist, forcing every other venue to
-- ``OTHER`` and losing its identity. This migration moves a DB deployed at
-- 007 to the new shape so migrate-from-007 and fresh-from-008 reach catalog
-- parity:
--
--   A. ``inquiries`` grows ``paper_publication_type`` (closed BibTeX entry
--      type: article|inproceedings|book|thesis|techreport|misc) and
--      ``paper_venue`` loses its closed-set value CHECK (now free-text).
--   B. ``change_log`` gains the ``old_/new_paper_publication_type`` mirror
--      (kind-gate + value + subject-kind CHECKs) and drops the now-stale
--      ``old_/new_paper_venue`` closed-set value CHECKs.
--   C. ``change_log_kind_check`` is re-rendered to admit the
--      ``'paper_publication_type'`` field-change kind.
--   D. Existing ``paper_venue`` values are rewritten into the two columns.
--
-- Arms A1/B1 add columns IF NOT EXISTS and gate every CHECK on absence, so a
-- re-render against a DB already at 008 (or fresh) is a no-op.

-- Arm A1: the new closed-set column, nullable like the rest of the
-- bibliography fields. The CHECK is byte-identical to what
-- ``generate_inquiry_kind_columns`` emits for a nullable Paper column with a
-- ``sql_check`` body.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS paper_publication_type TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_publication_type%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper'
                THEN TRUE
                 AND (paper_publication_type IS NULL OR paper_publication_type IN ('article', 'inproceedings', 'book', 'thesis', 'techreport', 'misc'))
                ELSE paper_publication_type IS NULL
            END
        );
    END IF;
END $$;

-- Arm D: rewrite the closed venue values into ``(publication_type, venue)``.
-- Runs before the venue value-CHECK is dropped, but every value written is a
-- free-text superset of the old closed set, so ordering is immaterial. Only
-- touches rows whose ``paper_publication_type`` is still NULL, so a re-run
-- after a partial apply is idempotent. The non-series categories clear the
-- venue (no series name); the series values keep their name as free text.
UPDATE inquiries
   SET paper_publication_type =
           CASE paper_venue
               WHEN 'PREPRINT' THEN 'misc'
               WHEN 'OTHER'    THEN 'misc'
               WHEN 'BOOK'     THEN 'book'
               WHEN 'THESIS'   THEN 'thesis'
               WHEN 'REPORT'   THEN 'techreport'
               WHEN 'JMLR'     THEN 'article'
               WHEN 'TMLR'     THEN 'article'
               ELSE 'inproceedings'
           END,
       paper_venue =
           CASE paper_venue
               WHEN 'PREPRINT' THEN NULL
               WHEN 'OTHER'    THEN NULL
               WHEN 'BOOK'     THEN NULL
               WHEN 'THESIS'   THEN NULL
               WHEN 'REPORT'   THEN NULL
               ELSE paper_venue
           END
 WHERE kind = 'Paper'
   AND paper_venue IS NOT NULL
   AND paper_publication_type IS NULL;

-- Arm A2: drop the old closed-set venue value CHECK. ``paper_venue`` keeps
-- only its populated-iff-Paper CHECK, matching the now-free-text fresh schema.
-- The discriminating token is the closed-set membership (``= ANY`` once PG
-- renders ``IN``); the bare populated-iff CHECK (``paper_venue IS NULL`` in
-- the ELSE) does not list the members, so gate on a member literal.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_venue%'
          AND pg_get_constraintdef(oid) LIKE '%''NeurIPS''%'
    LOOP
        EXECUTE format('ALTER TABLE inquiries DROP CONSTRAINT %I', con_name);
    END LOOP;
    -- Re-add the bare populated-iff CHECK only if no paper_venue CHECK
    -- survives (the drop above removed the only one that also carried values).
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_venue%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper' THEN TRUE ELSE paper_venue IS NULL END
        );
    END IF;
END $$;

-- Arm B1: change_log mirror for the new nullable column. A nullable per-kind
-- column's gates are the kind-gate form (``kind = 'X' OR old_X IS NULL``), a
-- closed-set value CHECK, and the subject-kind matrix CHECK -- exactly what
-- ``generate_change_log_mirror`` + ``generate_change_log_kind_matrix`` emit.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_paper_publication_type TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_paper_publication_type TEXT;

DO $$
BEGIN
    -- Kind-gate CHECKs: uniquely identified by ``kind = 'paper_publication_type'``
    -- next to the side column, and absence of a member literal (which only the
    -- value CHECK carries). Paren-count-independent so the detection survives
    -- PG's constraint-def rendering.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%kind = ''paper_publication_type''%'
          AND pg_get_constraintdef(oid) LIKE '%old_paper_publication_type IS NULL%'
          AND pg_get_constraintdef(oid) NOT LIKE '%''article''%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK (kind = 'paper_publication_type' OR old_paper_publication_type IS NULL);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%kind = ''paper_publication_type''%'
          AND pg_get_constraintdef(oid) LIKE '%new_paper_publication_type IS NULL%'
          AND pg_get_constraintdef(oid) NOT LIKE '%''article''%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK (kind = 'paper_publication_type' OR new_paper_publication_type IS NULL);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%old_paper_publication_type = ANY%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK (old_paper_publication_type IS NULL OR old_paper_publication_type IN ('article', 'inproceedings', 'book', 'thesis', 'techreport', 'misc'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%new_paper_publication_type = ANY%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK (new_paper_publication_type IS NULL OR new_paper_publication_type IN ('article', 'inproceedings', 'book', 'thesis', 'techreport', 'misc'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%''paper_publication_type''%subject_kind%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK (kind <> 'paper_publication_type' OR subject_kind = 'Paper');
    END IF;
END $$;

-- Arm B2: drop the now-stale closed-set value CHECKs on the venue mirror
-- columns (``paper_venue`` is free text now). The kind-gate and subject-kind
-- CHECKs stay; only the membership CHECKs (the ones listing a series literal)
-- go, matching the fresh schema where a free-text column emits no value CHECK.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_venue%'
          AND pg_get_constraintdef(oid) LIKE '%''NeurIPS''%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
END $$;

-- Arm C: admit the ``'paper_publication_type'`` field-change kind. DROP-then-
-- ADD re-renders the FULL current ``{change_kinds}`` literal (the same single
-- source the baseline uses). Gate on the enum CHECK uniquely: it is the only
-- ``kind = ANY`` CHECK that lists ``'created'`` and is not the big edge-peer
-- CASE presence CHECK (mirrors migration 007 arm 6).
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
