-- schema.055.sql -- device login: a CLI asks for a code, a signed-in person approves it,
-- and the next poll returns an API key once. One row per open code, kept in Postgres
-- because every redeploy restarts the server. ``device_sha256`` is the hash of the poll
-- secret and ``user_code`` the eight letters the person checks; neither the secret nor
-- the key it buys is stored. ``approver`` is whoever approved or denied, and the row is
-- deleted in the transaction that issues the key. Additive: the old build never reads
-- the table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS device_logins (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_sha256  BYTEA NOT NULL UNIQUE CHECK (octet_length(device_sha256) = 32),
    user_code      TEXT NOT NULL UNIQUE CHECK (user_code ~ '^[A-Z]{8}$'),
    client         TEXT NOT NULL CHECK (client ~ '^[a-z][a-z0-9-]{0,31}$'),
    host           TEXT NOT NULL DEFAULT '' CHECK (char_length(host) <= 63),
    machine        TEXT CHECK (machine ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'denied')),
    approver       UUID REFERENCES users(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    last_polled_at TIMESTAMPTZ,
    CHECK ((status = 'pending') = (approver IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_device_logins_expires
    ON device_logins (expires_at);
