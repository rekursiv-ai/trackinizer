-- schema.053.sql -- a period in which a person is not held to their credit.
-- One row per email: the last day, a note, who set it and when. Grace ends by itself
-- after its last day, so no job removes rows; only an admin sets or clears one. The
-- email is the lowercase address, with no FK to ``users``, as in ``credit_entries``: a
-- collaborator can be put on grace before their first sign-in, and a deleted account
-- that signs in again keeps it. Additive: the old build never reads the table, so this
-- is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS credit_grace (
    email  TEXT PRIMARY KEY CHECK (email = lower(email)),
    until  DATE NOT NULL,
    note   TEXT NOT NULL DEFAULT '',
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
