-- Migration 012: experiment config (hyperparameters) as a JSONB column.
--
-- Moves a database deployed before ``Experiment.config`` to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that
-- recorded the baseline before this file existed; a fresh database applies
-- the baseline (which already has everything here) and records this name
-- without executing it, so these statements never run twice.
--
-- ``experiment_config`` is the first JSONB column on ``inquiries``. It stores
-- the run's input settings verbatim (the wandb ``config`` analogue); trackinizer
-- never interprets it. The registered jsonb codec (trackinizer/lib/postgres/substrate.py)
-- handles dict<->jsonb, so no value-shape CHECK is emitted (JSONB is open).
--
-- Blast radius (every object ``schema.sql`` now builds that an old DB lacks):
--   1. ``inquiries.experiment_config`` column + its per-kind CHECK.
--   2. ``change_log.old_/new_experiment_config`` mirror columns + populated-iff
--      CHECKs (an edit emits an audit row of the ``experiment_config`` kind).
--   3. ``change_log_kind_check`` widened to admit the ``experiment_config``
--      field-change kind.

-- 1. The config column and its per-kind CHECK. The CHECK def matches the
--    baseline's generated column check (the catalog diff compares constraint
--    *definitions*, not names, so the auto-named baseline constraint and this
--    explicitly-named one are equivalent).
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS experiment_config JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass
          AND pg_get_constraintdef(oid) ILIKE '%experiment_config%'
    ) THEN
        ALTER TABLE inquiries ADD CONSTRAINT inquiries_experiment_config_check CHECK (
            CASE WHEN kind = 'Experiment'
                THEN TRUE
                ELSE experiment_config IS NULL
            END
        );
    END IF;
END $$;

-- 2. The change_log mirror columns + their populated-iff CHECKs, matching the
--    baseline's generated mirror block for a non-required column
--    (declarations + two "kind = ... OR X IS NULL" gates; no value-shape
--    CHECK, since JSONB carries no closed-set spec). NOTE: this file passes
--    through substitute_schema_placeholders, which does a blind brace-token
--    replace -- so no generated-block token may appear even in a comment.
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS old_experiment_config JSONB,
    ADD COLUMN IF NOT EXISTS new_experiment_config JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass
          AND pg_get_constraintdef(oid) ILIKE '%old_experiment_config%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'experiment_config' OR old_experiment_config IS NULL);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass
          AND pg_get_constraintdef(oid) ILIKE '%new_experiment_config%'
    ) THEN
        ALTER TABLE change_log ADD CHECK (
            kind = 'experiment_config' OR new_experiment_config IS NULL);
    END IF;
END $$;

-- 3. Widen the change_log kind enum to admit ``experiment_config``. Drop and
--    re-add with the full generated value list (the change-kinds placeholder
--    used in the ADD below) so the re-rendered constraint def matches a fresh
--    database exactly -- the same placeholder the baseline uses, so the two
--    can never drift. (The placeholder token itself is kept out of this
--    comment: substitution is a blind brace replace.)
ALTER TABLE change_log DROP CONSTRAINT IF EXISTS change_log_kind_check;
ALTER TABLE change_log ADD CONSTRAINT change_log_kind_check CHECK (kind IN ({change_kinds}));
