-- schema.048.sql -- the code store: each campaign iteration's code, so any two
-- iterations' diff shows with full context. A blob is one text file, stored once per
-- owner by the sha256 of its UTF-8 bytes and kept zstd-compressed; dedupe never
-- crosses owners, since a store shared between owners would let anyone learn whether
-- a file exists. A snapshot maps a record's paths to blob hashes and names the record
-- whose snapshot it builds on; a private one is read by its owner alone. Additive:
-- the old build never reads either table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS code_blobs (
    owner      TEXT NOT NULL CHECK (owner <> '' AND owner = lower(owner)),
    sha256     BYTEA NOT NULL CHECK (octet_length(sha256) = 32),
    size       INTEGER NOT NULL CHECK (size >= 0 AND size <= 1048576),
    data_zst   BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (owner, sha256)
);

CREATE TABLE IF NOT EXISTS code_snapshots (
    record_id    UUID PRIMARY KEY REFERENCES inquiries(id) ON DELETE CASCADE,
    parent_id    UUID REFERENCES code_snapshots(record_id) ON DELETE SET NULL,
    owner        TEXT NOT NULL CHECK (owner <> '' AND owner = lower(owner)),
    private      BOOLEAN NOT NULL DEFAULT false,
    files        JSONB NOT NULL CHECK (jsonb_typeof(files) = 'object'),
    file_count   INTEGER NOT NULL CHECK (file_count >= 0),
    total_bytes  BIGINT NOT NULL CHECK (total_bytes >= 0),
    partial      TEXT NOT NULL DEFAULT '',
    created_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
