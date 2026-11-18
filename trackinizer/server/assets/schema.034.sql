-- schema.034.sql -- the environment variables an agent launch exports, plain and
-- secret. A secret's value is never stored here: ``value`` is NULL and the
-- server's secret backend holds it. Additive: the old build never reads the
-- table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS variables (
    layer       TEXT NOT NULL CHECK (layer IN ('org', 'machine', 'user')),
    owner       TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]{0,127}$'),
    secret      BOOLEAN NOT NULL,
    value       TEXT,
    updated_by  TEXT NOT NULL,
    updated     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (layer, owner, name),
    CHECK (secret = (value IS NULL))
);
