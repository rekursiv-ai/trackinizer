-- schema.056.sql -- campaign-box credentials for direct GPU jobs. A campaign's E2B box
-- submits GPU jobs as its asker with a credential of its own: ``gpu_box_credentials``
-- keeps only the sha256 of the secret, who it speaks for, the campaign and box it was
-- minted for, and when it ends (``expires_at``, or ``revoked_at`` if sooner).
-- ``gpu_jobs.box_credential_id`` names the credential a job was submitted with; it is
-- NULL for a person's own job. Additive: the old build never reads either, so this is
-- safe to apply before the restart.
CREATE TABLE IF NOT EXISTS gpu_box_credentials (
    id            UUID PRIMARY KEY,
    email         TEXT NOT NULL,
    campaign      TEXT NOT NULL,
    box           TEXT NOT NULL,
    secret_sha256 BYTEA NOT NULL,
    minted_by     UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS gpu_box_credentials_live
    ON gpu_box_credentials (email, box) WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS gpu_box_credentials_campaign
    ON gpu_box_credentials (campaign);

ALTER TABLE gpu_jobs ADD COLUMN IF NOT EXISTS
    box_credential_id UUID REFERENCES gpu_box_credentials(id);

CREATE INDEX IF NOT EXISTS gpu_jobs_box
    ON gpu_jobs (box_credential_id) WHERE box_credential_id IS NOT NULL;
