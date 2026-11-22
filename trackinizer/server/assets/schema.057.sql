-- schema.057.sql -- jobs on a person's own machine, run by its machine host.
-- One row per job, from the request to its end. The id comes from ``gpu_jobs_id_seq``,
-- so one number names one job, a GPU job or a machine job. A row names the git remote,
-- branch and commit the machine's host fetches itself, and what the host reported: no
-- code, log or result byte is ever stored here. ``claimed_until`` keeps a start or a
-- cancel the host was handed from being handed out again while the host acts on it.
-- ``machine_id`` is cleared when the machine is deleted; ``machine`` keeps its name.
-- Emails are kept lowercase with no FK to ``users``, as in ``gpu_jobs``. Additive: the
-- old build never reads the table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS machine_jobs (
    id               BIGINT PRIMARY KEY DEFAULT nextval('gpu_jobs_id_seq'),
    email            TEXT NOT NULL,
    machine_id       UUID REFERENCES machines(id) ON DELETE SET NULL,
    machine          TEXT NOT NULL,
    state            TEXT NOT NULL
                     CHECK (state IN ('requested', 'queued', 'running', 'completed',
                                      'failed', 'cancelled', 'timeout')),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    gpus             INT NOT NULL,
    memory_mb        INT NOT NULL,
    minutes          INT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    argv             JSONB NOT NULL,
    remote           TEXT NOT NULL,
    ref              TEXT NOT NULL,
    commit           TEXT NOT NULL,
    exit_code        INT,
    reason           TEXT NOT NULL DEFAULT '',
    requested_by     UUID,
    claimed_until    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    queued_at        TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS machine_jobs_email_created
    ON machine_jobs (email, created_at DESC);

CREATE INDEX IF NOT EXISTS machine_jobs_live
    ON machine_jobs (machine_id)
    WHERE state NOT IN ('completed', 'failed', 'cancelled', 'timeout');
