-- Keep requested placement separate from the node assigned by the broker.
-- Nullable and additive: existing jobs retain automatic placement.
ALTER TABLE gpu_jobs ADD COLUMN IF NOT EXISTS requested_node TEXT;
