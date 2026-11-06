-- A GPU job is charged for the seconds Slurm counted, not for each minute begun, so
-- what a job was charged is kept in GPU seconds. A job charged before keeps its whole
-- minutes, as seconds. Nothing reads ``gpu_minutes`` after this.
-- Additive: the old build reads and writes only ``gpu_minutes``, so this is safe to
-- apply before the restart. A report the old build records after it is charged again
-- under the same keys, which usage takes once.
ALTER TABLE gpu_jobs ADD COLUMN IF NOT EXISTS gpu_seconds BIGINT;

UPDATE gpu_jobs SET gpu_seconds = gpu_minutes * 60
WHERE gpu_seconds IS NULL AND gpu_minutes IS NOT NULL;
