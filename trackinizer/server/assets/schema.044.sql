-- schema.044.sql -- a user's preferences, kept on their row so any browser or machine
-- finds them: an object of the keys they set (``server/preferences.py``), NULL until
-- they set one. Additive: the old build never reads the column, so this is safe to
-- apply before the restart.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS preferences JSONB CHECK (jsonb_typeof(preferences) = 'object');
