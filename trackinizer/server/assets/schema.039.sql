-- schema.039.sql -- provisioned machines and machine requests. A provisioned
-- machine names the provider that creates its host, the spec it was asked for, who
-- asked, when it expires, when it was taken down and when it was destroyed; a
-- hand-registered machine keeps an empty provider. A machine request is a row a
-- machine writes to ask its provider for what it may not do itself, and the provider
-- answers it once. Each JSON object's limit is twice the one the API accepts, as
-- for ``facts``: the table measures the jsonb text, which is wider than compact
-- JSON. Additive: the old build never reads these columns or the table, so this is
-- safe to apply before the restart.
ALTER TABLE machines
    ADD COLUMN IF NOT EXISTS provider     TEXT NOT NULL DEFAULT ''
        CHECK (provider = '' OR provider ~ '^[a-z][a-z0-9-]{0,31}$'),
    ADD COLUMN IF NOT EXISTS spec         JSONB NOT NULL DEFAULT '{}'
        CHECK (jsonb_typeof(spec) = 'object' AND octet_length(spec::text) <= 16384),
    ADD COLUMN IF NOT EXISTS requested_by TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS expires_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS down_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS destroyed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS machine_requests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id  UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind ~ '^[a-z][a-z0-9-]{0,31}$'),
    body        JSONB NOT NULL DEFAULT '{}'
                CHECK (jsonb_typeof(body) = 'object' AND octet_length(body::text) <= 131072),
    result      JSONB NOT NULL DEFAULT '{}'
                CHECK (jsonb_typeof(result) = 'object' AND octet_length(result::text) <= 131072),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'done', 'failed')),
    created_by  TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_machine_requests_machine
    ON machine_requests (machine_id, created);
