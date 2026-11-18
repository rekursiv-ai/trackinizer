-- schema.051.sql -- a session's part names the key that opened it.
--
-- One AgentSession holds a whole science chat, and each run that answers in it
-- (the assistant, then claude or codex) appends its own part. A part is written only by
-- the key whose first write opened it, so the assistant's lines cannot be rewritten
-- by the starter's run, or the starter's by the assistant. A part opened before this
-- column keeps NULL and the chat's old rule: its opener's key alone writes it.
-- Additive: the old build never reads the column, so this is safe to apply before
-- the restart. A replay changes nothing.
ALTER TABLE session_manifests ADD COLUMN IF NOT EXISTS opened_by_api_key_id UUID;
