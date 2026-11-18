-- schema.036.sql -- a machine's heartbeat, host instance and facts, the one-use
-- enrollment tokens that let a host join, and the credentials it joins with. A
-- revoked credential row is kept, so the server can tell a revoked host (410) from
-- an unknown one (401). Additive: the old build never reads these columns or tables,
-- so this is safe to apply before the restart.
ALTER TABLE machines
    ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS host_instance  UUID,
    ADD COLUMN IF NOT EXISTS host_version   TEXT NOT NULL DEFAULT ''
        CHECK (char_length(host_version) <= 64),
    ADD COLUMN IF NOT EXISTS facts          JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(facts) = 'object' AND octet_length(facts::text) <= 8192);

CREATE TABLE IF NOT EXISTS machine_enrollments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id    UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    secret_sha256 BYTEA NOT NULL CHECK (octet_length(secret_sha256) = 32),
    created_by    TEXT NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_machine_enrollments_open
    ON machine_enrollments (machine_id) WHERE used_at IS NULL;

CREATE TABLE IF NOT EXISTS machine_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id    UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    secret_sha256 BYTEA NOT NULL CHECK (octet_length(secret_sha256) = 32),
    created       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used     TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_machine_credentials_live
    ON machine_credentials (machine_id) WHERE revoked_at IS NULL;
