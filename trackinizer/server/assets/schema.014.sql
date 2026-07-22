-- Migration 014: enforce non-empty, length-capped experiment_metrics.key.
--
-- Moves a database deployed before the key CHECK to the current ``schema.sql``
-- shape. Numbered migrations run only on databases that recorded the baseline
-- before this file existed; a fresh database applies the baseline (which
-- already has the CHECK) and records this name without executing it, so this
-- statement never runs twice.
--
-- ``key`` is a metric name and a primary-key component. The wire
-- ``MetricPoint.key`` is ``Field(min_length=1, max_length=512)`` plus a
-- non-blank validator, and ``read_metrics`` reconstructs it, so a stored empty,
-- whitespace-only, or over-long key would 500 the read (the read-path pydantic
-- model rejects it). The DB backstop was missing -- ``step``, ``value``, and
-- ``kind`` already backstop their wire guards, but ``key`` did not -- so a
-- direct-SQL or bulk-load writer could persist a key the read cannot
-- reconstruct. Add the CHECK to complete the backstop set. The 512 bound
-- matches ``wire_metrics._MAX_KEY_CHARS`` and ``btrim(key) <> ''`` matches the
-- wire's blank rejection. Existing rows all satisfy it (the only writer, the
-- wire, already enforced these), so no data migration.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'experiment_metrics'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%char_length(key)%'
    ) THEN
        ALTER TABLE experiment_metrics
            ADD CHECK (char_length(key) BETWEEN 1 AND 512 AND btrim(key) <> '');
    END IF;
END $$;
