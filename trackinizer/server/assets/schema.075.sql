-- schema.075.sql -- drop ``gpu_allowance_resets`` (061) and ``inquiry_lock_log`` (038).
-- Release N (#2089) stopped reading and writing both: GPU jobs are held to knowspend's
-- dollar limit, not a per-person minute allowance, and lock history is ``change_log``'s
-- protected ``locked``/``unlocked`` kinds, which 074 backfilled from the lock log.
--
-- DESTRUCTIVE: every row goes. Apply only once release N is deployed, so 074 has moved
-- the lock log, and after a dump. Rolling back from here is supported only to release
-- N's exact build; anything older needs the dump restored. No foreign key points at
-- either table, and no add-on migration reads them. ``IF EXISTS``: a second run changes
-- nothing.
DROP TABLE IF EXISTS gpu_allowance_resets;
DROP TABLE IF EXISTS inquiry_lock_log;
