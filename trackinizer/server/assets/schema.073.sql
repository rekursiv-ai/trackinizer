-- schema.073.sql -- drop the old credit books, ``credit_entries`` (045) and
-- ``credit_grace`` (053). They are retired and wiped, not carried: no money was spent
-- on them, and grace periods were dropped rather than ported. Money is knowspend's
-- alone (``billing.*``).
--
-- DESTRUCTIVE: every row goes. Apply only once release N, which neither reads nor
-- writes either table, is deployed and drained on the database (the old books' GPU
-- jobs sealed at no charge), and after a dump. Rolling back from here is supported only
-- to release N's exact build; anything older needs the dump restored. No foreign key
-- points at either table, and no add-on migration reads them, so a fresh install that
-- replays add-on history still succeeds. ``IF EXISTS``: a second run changes nothing.
DROP TABLE IF EXISTS credit_grace;
DROP TABLE IF EXISTS credit_entries;
