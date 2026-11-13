-- schema.078.sql -- ``users.session_generation``: each browser session cookie carries
-- the value as it was when signed, and a logout raises it, so a cookie copied before a
-- logout is refused on every route and socket (``server/auth.py``). Additive with a
-- default: the old build never reads it, and a cookie it signed names no generation,
-- which reads as 0, so applying this before the restart signs nobody out.
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_generation INTEGER NOT NULL DEFAULT 0;
