-- schema.032.sql -- when each polling session was last heard from, so the
-- session reaper survives a server restart. Additive: the old build never reads
-- the table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS session_liveness (
    session_id  UUID PRIMARY KEY REFERENCES inquiries(id) ON DELETE CASCADE,
    last_seen   TIMESTAMPTZ NOT NULL,
    reaped      BOOLEAN NOT NULL DEFAULT FALSE
);
