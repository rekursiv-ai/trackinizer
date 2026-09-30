-- Seek recent conversation turns without scanning intervening tool records.
CREATE INDEX IF NOT EXISTS idx_session_records_recent_turns
    ON session_records (session_id, part DESC, idx DESC)
    WHERE kind IN ('UserMessage', 'AssistantMessage');
