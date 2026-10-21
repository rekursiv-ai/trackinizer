-- schema.052.sql -- remember the folder a machine's owner chose for a CLI run.
--
-- The browser used to keep this only in local storage, which the assistant cannot
-- read when the person asks it to start a campaign on that machine. Nullable for a
-- machine whose owner has not picked a folder. Additive and safe before restart.
ALTER TABLE machines ADD COLUMN IF NOT EXISTS last_run_cwd TEXT;
