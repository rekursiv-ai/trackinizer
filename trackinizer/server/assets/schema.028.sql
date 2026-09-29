-- Immutable team-readable report revisions with one Artifact per publication.
CREATE TABLE IF NOT EXISTS visual_reports (
    id          UUID PRIMARY KEY,
    -- Historical target survives an ordinary graph-row purge.
    issue_id    UUID NOT NULL,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS visual_report_revisions (
    report_id    UUID NOT NULL REFERENCES visual_reports(id) ON DELETE RESTRICT,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    artifact_id  UUID NOT NULL UNIQUE,
    author_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    author_email TEXT NOT NULL,
    content      JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    publish_key  UUID NOT NULL,
    request_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (report_id, revision),
    UNIQUE (author_id, publish_key)
);
