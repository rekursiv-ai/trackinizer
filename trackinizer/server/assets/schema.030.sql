-- Account for existing Artifact content before enforcing a per-user quota.
ALTER TABLE visual_report_revisions ADD COLUMN content_bytes BIGINT;
UPDATE visual_report_revisions
SET content_bytes = octet_length(content::text);
ALTER TABLE visual_report_revisions
    ALTER COLUMN content_bytes SET NOT NULL,
    ADD CONSTRAINT visual_report_revisions_content_bytes_positive
        CHECK (content_bytes > 0);
