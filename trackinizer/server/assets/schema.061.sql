-- Forgive allowance usage without changing job receipts or the credit ledger.
-- Snapshot terminal job IDs so jobs live at reset still count after they finish.
CREATE TABLE IF NOT EXISTS gpu_allowance_resets (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    reset_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    job_ids BIGINT[] NOT NULL
);
CREATE INDEX IF NOT EXISTS gpu_allowance_resets_email
    ON gpu_allowance_resets (email);
