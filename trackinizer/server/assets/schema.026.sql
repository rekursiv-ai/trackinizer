-- schema.026.sql -- per-user visual workspace and idempotent operation receipts.
-- Also present in schema.sql for fresh databases.
ALTER TABLE users ADD COLUMN IF NOT EXISTS visual_workspace_enabled
    BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS visual_workspaces (
    id          UUID PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_default  BOOLEAN NOT NULL DEFAULT TRUE,
    revision    BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    state       JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    session_id  UUID REFERENCES inquiries(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_workspaces_default_user
    ON visual_workspaces (user_id) WHERE is_default;

CREATE TABLE IF NOT EXISTS visual_workspace_operations (
    workspace_id UUID NOT NULL REFERENCES visual_workspaces(id) ON DELETE CASCADE,
    key          UUID NOT NULL,
    request_hash TEXT NOT NULL,
    response     JSONB NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, key)
);
