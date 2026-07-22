-- Migration 017: split the single Paper Google Scholar handle into two.
--
-- Migration 016 shipped ONE handle, ``paper_google_scholar_id``, holding the
-- Scholar ``cites_id`` (cited-by pivot). We now distinguish TWO Scholar handles
-- that coexist on one Paper:
--   ``paper_google_scholar_cluster_id`` (data-cid): the paper's stable Scholar
--     IDENTITY, present for every indexed paper (cited or not);
--   ``paper_google_scholar_cites_id`` (cites_id): the cited-by pivot handle,
--     present ONLY once a paper has citations.
-- The old single column held a cites_id, so its data migrates to the new
-- ``*_cites_id`` column; ``*_cluster_id`` starts empty (backfilled out-of-band).
--
-- 016 is append-only and already durably applied (name-only migration gate,
-- ``core.py``), so it is NOT edited. This migration transforms the 016 shape
-- (one column, one enum kind) into the split shape the fresh baseline
-- ``schema.sql`` now generates, so migrate-from-016 and fresh reach catalog
-- parity. It preserves ALL data: the inquiries column value and every
-- ``paper_google_scholar_id`` change_log audit row are carried onto the new
-- ``*_cites_id`` names before the old ones are dropped.
--
-- On a FRESH DB the baseline already emits the split shape and this body never
-- runs (bootstrap marks every migration applied without executing it); it runs
-- ONLY against a DB deployed at 016.

-- Arm 1: add the two nullable Paper-only columns + their per-kind CASE-WHEN
-- CHECKs. IF NOT EXISTS makes a re-render a no-op; each CHECK is added only when
-- absent (discovered by the column token, since it is unnamed).
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS paper_google_scholar_cluster_id TEXT;
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS paper_google_scholar_cites_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_cluster_id%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper'
                THEN TRUE
                ELSE paper_google_scholar_cluster_id IS NULL
            END
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_cites_id%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'Paper'
                THEN TRUE
                ELSE paper_google_scholar_cites_id IS NULL
            END
        );
    END IF;
END $$;

-- Arm 2: add the four change_log mirror columns for BOTH new handles + their
-- populated-iff and subject-kind matrix CHECKs.
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS old_paper_google_scholar_cluster_id TEXT;
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS new_paper_google_scholar_cluster_id TEXT;
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS old_paper_google_scholar_cites_id TEXT;
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS new_paper_google_scholar_cites_id TEXT;

DO $$
BEGIN
    -- cluster_id populated-iff + subject-kind.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid)
              LIKE '%old_paper_google_scholar_cluster_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_cluster_id'
            OR old_paper_google_scholar_cluster_id IS NULL
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid)
              LIKE '%new_paper_google_scholar_cluster_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_cluster_id'
            OR new_paper_google_scholar_cluster_id IS NULL
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_cluster_id%'
          AND pg_get_constraintdef(oid) LIKE '%subject_kind%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind <> 'paper_google_scholar_cluster_id' OR subject_kind = 'Paper'
        );
    END IF;
    -- cites_id populated-iff + subject-kind.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid)
              LIKE '%old_paper_google_scholar_cites_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_cites_id'
            OR old_paper_google_scholar_cites_id IS NULL
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid)
              LIKE '%new_paper_google_scholar_cites_id IS NULL%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'paper_google_scholar_cites_id'
            OR new_paper_google_scholar_cites_id IS NULL
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid = 'change_log'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%paper_google_scholar_cites_id%'
          AND pg_get_constraintdef(oid) LIKE '%subject_kind%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind <> 'paper_google_scholar_cites_id' OR subject_kind = 'Paper'
        );
    END IF;
END $$;

-- Arm 3: install a TRANSITIONAL enum that admits BOTH the retiring
-- ``paper_google_scholar_id`` kind (the rows that still carry it) AND the final
-- split literal (the kind Arm 4 rewrites them to). Postgres validates a CHECK
-- immediately (no deferral), so the enum swap cannot be a single DROP-then-ADD:
-- ADDing the final split enum while old-kind rows exist would fail, and rewriting
-- the rows first would fail against the still-active enum that lacks the new
-- kind. The superset breaks the deadlock -- both the old and new kinds are legal
-- during the Arm 4 rewrite; Arm 5 then tightens the enum to the final split set.
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
    ALTER TABLE change_log
        ADD CHECK (kind = 'paper_google_scholar_id' OR kind IN ({change_kinds}));
END $$;

-- Arm 4: carry the data. The old single handle held a cites_id, so its live
-- value migrates to the new ``*_cites_id`` column, and every audit row migrates
-- onto the ``*_cites_id`` mirrors + kind.
--
-- GUARDED on the retiring ``inquiries.paper_google_scholar_id`` column, which
-- Arm 5 drops: the guard makes Arm 4 a no-op on a SECOND run (the documented
-- "clear the ledger row, re-bootstrap" recovery), matching every other arm's
-- idempotency -- an unguarded reference to the dropped column would 500 with
-- UndefinedColumnError. The two change_log mirror columns are dropped in the
-- same Arm 5, so they co-exist-or-co-absent with the inquiries column; the one
-- probe covers both UPDATEs.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'inquiries'
          AND column_name = 'paper_google_scholar_id'
    ) THEN
        UPDATE inquiries
            SET paper_google_scholar_cites_id = paper_google_scholar_id
            WHERE paper_google_scholar_id IS NOT NULL;
        -- The populated-iff CHECKs require the mirror value to sit under the
        -- MATCHING kind, so copy the value and rewrite the kind in one
        -- statement, clearing the old mirror columns so their (about-to-drop)
        -- CHECKs are not tripped meanwhile.
        UPDATE change_log
            SET new_paper_google_scholar_cites_id = new_paper_google_scholar_id,
                old_paper_google_scholar_cites_id = old_paper_google_scholar_id,
                new_paper_google_scholar_id = NULL,
                old_paper_google_scholar_id = NULL,
                kind = 'paper_google_scholar_cites_id'
            WHERE kind = 'paper_google_scholar_id';
    END IF;
END $$;

-- Arm 4b: tighten the transitional enum to the FINAL split literal (drop the
-- retiring kind). Safe now that Arm 4 rewrote every row that carried it.
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

-- Arm 5: drop the retired single column and its change_log mirrors. Dropping a
-- mirror column CASCADEs its own per-kind / populated-iff CHECKs, but the
-- subject-kind matrix CHECK (``kind <> 'paper_google_scholar_id' OR subject_kind
-- = 'Paper'``) references only ``kind`` / ``subject_kind`` -- neither dropped
-- column -- so CASCADE cannot reach it; it is dropped explicitly. Every step is
-- guarded (column-existence probe / constraint-def match) so a re-render, or a
-- fresh DB where the baseline never created these, is a no-op.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- Drop the orphan subject-kind matrix CHECK for the retiring kind first
    -- (CASCADE on the mirror columns never removes it -- it names no column).
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid)
              LIKE '%kind <> ''paper_google_scholar_id''%'
          AND pg_get_constraintdef(oid) LIKE '%subject_kind%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'inquiries'
          AND column_name = 'paper_google_scholar_id'
    ) THEN
        ALTER TABLE inquiries DROP COLUMN paper_google_scholar_id CASCADE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log'
          AND column_name = 'old_paper_google_scholar_id'
    ) THEN
        ALTER TABLE change_log DROP COLUMN old_paper_google_scholar_id CASCADE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log'
          AND column_name = 'new_paper_google_scholar_id'
    ) THEN
        ALTER TABLE change_log DROP COLUMN new_paper_google_scholar_id CASCADE;
    END IF;
END $$;
