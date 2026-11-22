-- A GPU job charged through usage keeps the rate it was submitted at: every interval
-- and its final charge are priced at it. NULL is a job of the old credit books, which
-- holds its budget there and is settled there, never through usage.
-- Nullable and additive: every existing job stays on the old books.
ALTER TABLE gpu_jobs ADD COLUMN IF NOT EXISTS usd_per_gpu_minute DOUBLE PRECISION;
