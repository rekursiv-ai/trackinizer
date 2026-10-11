-- schema.040.sql -- who holds which audience. A user records the Google Workspace
-- domain (the ``hd`` claim) of their last login, which a ``*@domain`` staff pattern
-- needs before it counts, and the Google account (``sub``) that first signed in as
-- that email, so a second Google account holding the same address cannot take over
-- the row and its keys. An API key may pin the one audience it opens sessions into; it
-- is NULL for an unpinned key, so every existing key is unchanged. Additive: the old
-- build never reads these columns, so this is safe to apply before the restart.
--
-- The shape check is the one in ``wire/audience.py``: it admits any lowercase name, so
-- a later audience needs no migration. The accepted set lives in code.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS hosted_domain TEXT
        CHECK (hosted_domain = lower(hosted_domain) AND char_length(hosted_domain) <= 253),
    ADD COLUMN IF NOT EXISTS google_sub TEXT
        CHECK (char_length(google_sub) BETWEEN 1 AND 255);

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS open_into TEXT
        CHECK (open_into ~ '^[a-z][a-z0-9_-]{0,31}$');
