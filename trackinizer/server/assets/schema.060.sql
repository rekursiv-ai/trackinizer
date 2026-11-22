-- schema.060.sql -- what a machine's owner sees and cuts off.
-- ``session_liveness`` keeps where each session's run last said it ran, so a machine's
-- past runs outlive the server's memory. ``api_keys.machine_id`` names the machine a
-- device login enrolled with the key it made, so logging the machine out revokes the
-- keys it logged in with; a key made before this names none. Additive and nullable: the
-- old build reads neither column, so this is safe to apply before the restart.
ALTER TABLE session_liveness ADD COLUMN IF NOT EXISTS machine TEXT;
ALTER TABLE session_liveness ADD COLUMN IF NOT EXISTS run TEXT;
ALTER TABLE session_liveness ADD COLUMN IF NOT EXISTS cli TEXT;
ALTER TABLE session_liveness ADD COLUMN IF NOT EXISTS cwd TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS machine_id UUID REFERENCES machines(id) ON DELETE SET NULL;
