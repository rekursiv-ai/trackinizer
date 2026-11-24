-- schema.083.sql -- Partial covering index for ``GET /api/web/worked_on`` and
-- fixed-threshold autovacuum/autoanalyze tuning for append-only ``change_log``.
-- One index build and one catalog-only table option change; no row is rewritten.
--
-- Sized 2026-11-23 on prod, read-only:
--   - 55,606,088 ``change_log`` rows (12 GB heap, 19 GB indexes), 97.03%
--     ``dependency_changed``; ~3% (~1.5M rows) qualify for the ``worked_on``
--     partial index (excluding ``AgentSession`` and non-work kinds).
--
-- ``GET /api/web/worked_on`` previously scanned millions of ``dependency_changed``
-- rows over 30 days (~177 s per call). The partial covering index excludes
-- ``AgentSession`` and non-work kinds so both ``api_key_id = ANY(...)`` and
-- ``api_key_id IS NULL AND actor = ...`` run as index-only scans.
--
-- Build cost: ~10-25 s for ``idx_change_log_work_key_created`` (~50 MB partial
-- btree; 0 ms at restart if pre-built via
-- ``CREATE INDEX CONCURRENTLY IF NOT EXISTS``); <1 ms for
-- ``ALTER TABLE change_log SET (...)``.

CREATE INDEX IF NOT EXISTS idx_change_log_work_key_created
    ON change_log (api_key_id, created DESC)
    INCLUDE (subject_id, actor)
    WHERE subject_kind <> 'AgentSession'
      AND kind NOT IN (
          'marginal_cost',
          'dependency_changed',
          'implicit_subs_opened',
          'implicit_subs_closed',
          'purged',
          'locked',
          'unlocked'
      );

ALTER TABLE change_log SET (
    autovacuum_analyze_scale_factor = 0,
    autovacuum_analyze_threshold = 50000,
    autovacuum_vacuum_insert_scale_factor = 0,
    autovacuum_vacuum_insert_threshold = 200000
);
