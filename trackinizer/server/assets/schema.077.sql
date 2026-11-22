-- schema.077.sql -- each row the assistant files from a science chat names the session
-- that began that chat, ``filed_from_chat``; while the chat is not ``open``, the row is
-- read as the chat is, and by its own account. No FK to that session: a deleted chat
-- takes its sessions, and its rows stay hidden rather than open. Additive: the old build
-- never reads or writes the column, so this is safe to apply before the restart; a row
-- filed before it names no chat and stays open, as it was, until it is backfilled.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS filed_from_chat UUID;
