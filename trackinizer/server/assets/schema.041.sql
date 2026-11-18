-- schema.041.sql -- a session says who may read its transcript. An AgentSession
-- carries an ``audience`` (``open`` or ``staff``) that every reader and writer of
-- session content checks; only the transcript is gated, so every other column of the
-- row and every row a session files stays as open as before. Existing sessions are
-- ``open``, so a deployment that never sets a staff pattern behaves as it did.
--
-- The shape check is the one in ``wire/audience.py``: it admits any lowercase name, so
-- a later audience needs no migration. The accepted set lives in code.
--
-- The audience is stamped once at submit and never edited, so the change log does not
-- mirror it. Not safe to apply ahead of the restart: the CHECK refuses an AgentSession
-- row without an audience, which is how a build older than this one inserts it, so
-- apply it with the build that supplies the column. The CHECK is added after the
-- backfill, so it is never tested against a NULL the table still holds.
--
-- The constraint below is the one the baseline's generated block produces for the
-- ``AgentSession.audience`` spec; ``schema_migration_test`` proves the two agree.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS agentsession_audience TEXT;

UPDATE inquiries SET agentsession_audience = 'open'
    WHERE kind = 'AgentSession' AND agentsession_audience IS NULL;

ALTER TABLE inquiries DROP CONSTRAINT IF EXISTS inquiries_agentsession_audience_check;
ALTER TABLE inquiries
    ADD CONSTRAINT inquiries_agentsession_audience_check CHECK (
        CASE WHEN kind = 'AgentSession'
            THEN agentsession_audience IS NOT NULL
         AND (agentsession_audience IS NULL OR agentsession_audience ~ '^[a-z][a-z0-9_-]{0,31}$')
            ELSE agentsession_audience IS NULL
        END
    );
