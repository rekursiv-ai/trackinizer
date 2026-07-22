-- Migration 013: enforce finite experiment_metrics.value at the DB.
--
-- Moves a database deployed before the value finiteness CHECK to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that
-- recorded the baseline before this file existed; a fresh database applies
-- the baseline (which already has the CHECK) and records this name without
-- executing it, so this statement never runs twice.
--
-- ``value`` is the logged scalar. The wire ``MetricPoint.value`` is
-- ``Field(allow_inf_nan=False)`` and ``read_metrics`` reconstructs it, so a
-- stored NaN/±Inf would 500 the read (NaN/±Inf are valid float8 but not valid
-- JSON numbers). The DB backstop was missing -- the ``step >= 0`` and
-- ``kind = 'scalar'`` CHECKs (schema.011) already backstop their wire guards,
-- but ``value`` did not -- so a direct-SQL or bulk-load writer could persist a
-- non-finite value the read cannot serialize. Add the CHECK to complete the
-- backstop set. Existing rows all satisfy it (the only writer, the wire,
-- already rejected non-finite values), so no data migration.
--
-- Postgres NaN/Inf are literals (not IEEE-unordered), so the guard is explicit
-- ``<>`` against each, not ``value = value``.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'experiment_metrics'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%NaN%'
    ) THEN
        ALTER TABLE experiment_metrics ADD CHECK (
            value <> 'NaN'::float8
            AND value <> 'Infinity'::float8
            AND value <> '-Infinity'::float8);
    END IF;
END $$;
