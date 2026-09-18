-- Index receipt membership in existing Experiment configs.
-- Migrations run transactionally, so CREATE INDEX cannot use CONCURRENTLY.
CREATE INDEX IF NOT EXISTS idx_inquiries_experiment_config_gin
    ON inquiries USING gin(experiment_config jsonb_path_ops);
