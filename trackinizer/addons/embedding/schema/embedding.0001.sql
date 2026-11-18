-- The embedding add-on's own schema: one row per vector, rebuilt by the sweep
-- from the rows it names.
CREATE SCHEMA IF NOT EXISTS embedding;

CREATE TABLE IF NOT EXISTS embedding.vectors (
    -- The inquiry the unit belongs to: the session a record is of, the inquiry, or
    -- the inquiry an edge starts from. Its deletion takes the vectors with it.
    owner_id   UUID NOT NULL REFERENCES public.inquiries (id) ON DELETE CASCADE,
    -- The unit: a session record (part, idx, 'content'); an inquiry's joined text
    -- (0, 0, 'text'); an edge's note (0, 0, 'edge:<edge_kind>:<to_id>').
    part       INTEGER NOT NULL DEFAULT 0,
    idx        INTEGER NOT NULL DEFAULT 0,
    field      TEXT NOT NULL,
    chunk      INTEGER NOT NULL DEFAULT 0,
    -- md5 of the unit's whole text, before chunking.
    text_md5   TEXT NOT NULL,
    -- units.settings_key. A row is current only when text_md5 and settings both
    -- match; a settings change overwrites the row.
    settings   TEXT NOT NULL,
    -- Dimension-free; each settings key has its own partial HNSW index, made by
    -- code (search.ensure_index), so a model of another width needs no migration.
    embedding  HALFVEC NOT NULL,
    created    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner_id, part, idx, field, chunk)
);

CREATE INDEX IF NOT EXISTS vectors_settings ON embedding.vectors (settings);
