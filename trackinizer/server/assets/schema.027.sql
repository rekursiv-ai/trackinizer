-- schema.027.sql -- durable, owner-scoped snapshots of visual workspace state.
-- Live session pairings stay in visual_workspaces and are never copied here.
CREATE TABLE IF NOT EXISTS visual_workspace_presets (
    id          UUID PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    state       JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_visual_workspace_presets_user_modified
    ON visual_workspace_presets (user_id, modified_at DESC, id DESC);
