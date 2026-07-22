-- Migration 003: make optional inquiry columns nullable.
--
-- Moves a database deployed before the nullability fix to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that
-- recorded the baseline before this file existed; a fresh database applies
-- the baseline (already nullable) and records this name without executing
-- it, so these statements never run twice.
--
-- Rationale: ``owner``, ``description``, ``labels``, ``subscribers`` are
-- ``| None`` on the Inquiry dataclass -- genuinely optional. They were stored
-- ``NOT NULL DEFAULT ''`` / ``'{}'``, so "unset" had two encodings (empty
-- sentinel on base columns, SQL NULL on per-kind columns). NULL is now the
-- single encoding of "unset", so absence is uniform across every nullable
-- column. ``status`` and the cost axes stay NOT NULL: their defaults
-- ('active', 0) are real values, not absence stand-ins.
--
-- Blast radius (objects ``schema.sql`` now builds differently):
--   1. The four columns lose NOT NULL and their DEFAULT.
--   2. Existing empty (or whitespace-only string) values become NULL, so old
--      rows share the new single encoding rather than staying empty.
--   3. ``idx_inquiries_owner`` partial predicate moves from ``owner <> ''``
--      to ``owner IS NOT NULL`` (the empty sentinel no longer exists).

ALTER TABLE inquiries ALTER COLUMN owner       DROP NOT NULL;
ALTER TABLE inquiries ALTER COLUMN owner       DROP DEFAULT;
ALTER TABLE inquiries ALTER COLUMN description DROP NOT NULL;
ALTER TABLE inquiries ALTER COLUMN description DROP DEFAULT;
ALTER TABLE inquiries ALTER COLUMN labels      DROP NOT NULL;
ALTER TABLE inquiries ALTER COLUMN labels      DROP DEFAULT;
ALTER TABLE inquiries ALTER COLUMN subscribers DROP NOT NULL;
ALTER TABLE inquiries ALTER COLUMN subscribers DROP DEFAULT;

-- ``btrim() = ''`` collapses both empty and whitespace-only strings, so no
-- non-NULL stand-in for "unset" survives on the scalar columns.
UPDATE inquiries SET owner       = NULL WHERE btrim(owner)       = '';
UPDATE inquiries SET description = NULL WHERE btrim(description) = '';
UPDATE inquiries SET labels      = NULL WHERE labels      = '{}';
UPDATE inquiries SET subscribers = NULL WHERE subscribers = '{}';

DROP INDEX IF EXISTS idx_inquiries_owner;
CREATE INDEX IF NOT EXISTS idx_inquiries_owner
    ON inquiries (owner) WHERE owner IS NOT NULL;
