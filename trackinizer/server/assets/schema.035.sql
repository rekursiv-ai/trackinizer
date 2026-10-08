-- schema.035.sql -- the machines a campaign may run on: a name, a role, one line
-- telling an agent how to use the machine, and labels. Additive: the old build
-- never reads the table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS machines (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    role        TEXT NOT NULL DEFAULT ''
                CHECK (role = '' OR role ~ '^[a-z][a-z0-9-]{0,31}$'),
    how         TEXT NOT NULL DEFAULT '' CHECK (char_length(how) <= 2000),
    labels      TEXT[] NOT NULL DEFAULT '{}',
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL,
    updated     TIMESTAMPTZ NOT NULL DEFAULT now()
);
