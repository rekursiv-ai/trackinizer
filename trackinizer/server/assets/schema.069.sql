-- A campaign's box credential names the campaign's ref in usage (``campaign/Issue#7``),
-- which its slug does not, and each job it submits keeps that ref for its whole life, so
-- the job's charges count toward the campaign and its limits hold it. Empty is a job of
-- no campaign, charged under ``gpu/<id>``: every existing job, and every job of a
-- credential minted before this.
-- Additive: the old build reads neither and writes the default.
ALTER TABLE gpu_box_credentials ADD COLUMN IF NOT EXISTS
    campaign_ref TEXT NOT NULL DEFAULT '';

ALTER TABLE gpu_jobs ADD COLUMN IF NOT EXISTS
    campaign_ref TEXT NOT NULL DEFAULT '';
