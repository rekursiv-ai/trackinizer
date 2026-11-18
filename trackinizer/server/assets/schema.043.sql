-- schema.043.sql -- who a science chat is shared with, and which chats were deleted.
-- A share is one row per conversation and person: the email, who shared it and when.
-- Under TRACKINIZER_CHAT_ORGS=individual only the starter and these people post into
-- a chat; under domain they join the starter's organisation. A tombstone names a
-- deleted chat, under whose id no line starts a chat again. Additive: the old build
-- never reads either table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS science_chat_shares (
    conversation_id UUID NOT NULL,
    email           TEXT NOT NULL CHECK (email = lower(email)),
    shared_by       TEXT NOT NULL,
    shared_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (conversation_id, email)
);

CREATE TABLE IF NOT EXISTS science_chat_tombstones (
    conversation_id UUID PRIMARY KEY,
    deleted_by      TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
