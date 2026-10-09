-- schema.038.sql -- the welcome flow's acknowledgement, and locked rows. A user
-- records that they agreed to the alpha rules at one rules version (Issue#1's last
-- change), so editing the rules shows the agreement again. A locked inquiry changes
-- only for an admin. Additive: the old build never reads these columns, so this is
-- safe to apply before the restart. Locks are logged: who set or cleared one, and
-- when. Nothing is locked here: a deployment that wants the welcome flow writes its
-- rules into Issue#1 and has an admin lock it, which is what turns the flow on.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS acknowledged_at            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS acknowledged_rules_version TEXT;

ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS inquiry_lock_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inquiry_id UUID NOT NULL,
    locked     BOOLEAN NOT NULL,
    actor      TEXT NOT NULL,
    created    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
