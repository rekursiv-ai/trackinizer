-- Canvas Chat conversations: one signed-in user, one canvas, one partner at a time.
-- Chat is on for everyone by default; Settings still lets a user opt out.
ALTER TABLE users ALTER COLUMN visual_workspace_enabled SET DEFAULT TRUE;
UPDATE users SET visual_workspace_enabled = TRUE;
-- A canvas made before Chat shipped gets it, floating, once; a full canvas does not.
UPDATE visual_workspaces
SET state = jsonb_set(
        state,
        '{visuals}',
        (state -> 'visuals') || jsonb_build_array(jsonb_build_object(
            'id', gen_random_uuid(),
            'type', 'trax.chat',
            'version', 1,
            'placement', 'floating',
            'record_id', 'null'::jsonb,
            'params', '{}'::jsonb,
            'floating_rect', 'null'::jsonb
        ))
    ),
    revision = revision + 1,
    modified_at = clock_timestamp()
WHERE jsonb_array_length(state -> 'visuals') < 12
  AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(state -> 'visuals') AS visual
        WHERE visual ->> 'type' = 'trax.chat'
  );
CREATE TABLE IF NOT EXISTS chat_conversations (
    id                 UUID PRIMARY KEY,
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id       UUID NOT NULL REFERENCES visual_workspaces(id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    partner_session_id UUID REFERENCES inquiries(id) ON DELETE SET NULL,
    partner_actor      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_user_modified
    ON chat_conversations (user_id, modified_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_chat_conversations_partner_session
    ON chat_conversations (partner_session_id);
CREATE TABLE IF NOT EXISTS chat_messages (
    id              UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    seq             BIGINT NOT NULL CHECK (seq >= 1),
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    author          TEXT NOT NULL,
    text            TEXT NOT NULL,
    -- The browser send that stored a user message: its Idempotency-Key and a hash of
    -- the request, so a retry finds the message and a different request is refused.
    request_key     UUID UNIQUE,
    request_hash    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (conversation_id, seq)
);
