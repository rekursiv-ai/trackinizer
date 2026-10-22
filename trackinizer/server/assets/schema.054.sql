-- schema.054.sql -- direct GPU jobs: who asked for one, and every certificate and token
-- the server issued for it. ``gpu_owners`` numbers each person once, so a node can keep
-- one code cache per person without learning an email. ``gpu_jobs`` is one row per
-- job, from the request to the sealed result; its id is the job's name on the node,
-- taken from the sequence before the row is written and never reused. ``gpu_issuances``
-- is the audit: a row is written before anything is signed or sent, and afterwards only
-- ``fetched_at`` (the authority's one use) and ``signed_at`` with ``certificate`` (a
-- public value) change. A ``secret-read`` row names the secrets a node's proxy was
-- given, never their values. Emails are kept lowercase with no FK to ``users``, as in
-- ``credit_entries``. Additive: the old build never reads these tables, so this is safe
-- to apply before the restart.
CREATE TABLE IF NOT EXISTS gpu_owners (
    id         BIGSERIAL PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS gpu_jobs (
    id                 BIGSERIAL PRIMARY KEY,
    email              TEXT NOT NULL,
    owner_id           BIGINT NOT NULL REFERENCES gpu_owners(id),
    state              TEXT NOT NULL
                       CHECK (state IN ('requested', 'placed', 'granted', 'starting',
                                        'queued', 'running', 'completed', 'failed',
                                        'cancelled', 'timeout', 'expired')),
    cancel_requested   BOOLEAN NOT NULL DEFAULT false,
    gpus               INT NOT NULL,
    minutes            INT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    argv               JSONB NOT NULL,
    commit             TEXT NOT NULL,
    public_key         TEXT NOT NULL,
    key_sha256         TEXT NOT NULL,
    node               TEXT,
    host               TEXT,
    port               INT,
    uid                BIGINT,
    host_key           TEXT,
    slurm_id           TEXT,
    token_sha256       TEXT,
    bundle_sha256      TEXT,
    bundle_bytes       BIGINT,
    requested_by       UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    placed_at          TIMESTAMPTZ,
    granted_at         TIMESTAMPTZ,
    start_requested_at TIMESTAMPTZ,
    queued_at          TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    ended_at           TIMESTAMPTZ,
    sealed_at          TIMESTAMPTZ,
    elapsed_s          BIGINT,
    gpu_minutes        BIGINT,
    usd                NUMERIC(12, 4),
    files              BIGINT,
    bytes              BIGINT,
    manifest_sha256    TEXT,
    manifest           JSONB,
    reason             TEXT NOT NULL DEFAULT '',
    claimed_until      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS gpu_jobs_email_created
    ON gpu_jobs (email, created_at DESC);

CREATE INDEX IF NOT EXISTS gpu_jobs_live
    ON gpu_jobs (state)
    WHERE state NOT IN ('completed', 'failed', 'cancelled', 'timeout', 'expired');

CREATE UNIQUE INDEX IF NOT EXISTS gpu_jobs_token
    ON gpu_jobs (token_sha256) WHERE token_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS gpu_issuances (
    id           BIGSERIAL PRIMARY KEY,
    job_id       BIGINT NOT NULL REFERENCES gpu_jobs(id),
    email        TEXT NOT NULL,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('upload-cert', 'read-cert', 'job-token', 'secret-read')),
    principal    TEXT,
    key_sha256   TEXT,
    public_key   TEXT,
    certificate  TEXT,
    names        TEXT[],
    node         TEXT,
    valid_after  TIMESTAMPTZ,
    valid_before TIMESTAMPTZ,
    requested_by UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    fetched_at   TIMESTAMPTZ,
    signed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS gpu_issuances_job
    ON gpu_issuances (job_id, id);
