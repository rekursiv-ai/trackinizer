-- Align the metric-key CHECK with the wire's blank rule (Python ``str.strip``).
-- ``btrim(key)`` trims only spaces, so the masked write stored a tab key that
-- every later read of its experiment refused with a 500. Adding the constraint
-- validates existing rows: if one holds a whitespace-only key, this migration
-- fails and leaves the database unchanged; delete that row (no read could
-- return it) and restart.
ALTER TABLE experiment_metrics
    DROP CONSTRAINT experiment_metrics_key_check,
    ADD CONSTRAINT experiment_metrics_key_check CHECK (
        char_length(key) BETWEEN 1 AND 512
        AND btrim(key, E'\t\n\x0b\f\r\x1c\x1d\x1e\x1f \u0085  '
            '          '
            '     　') <> '');
