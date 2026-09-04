-- schema.019.sql -- session IR storage (four tables).
--
-- Additive migration for a database that predates the IR store. The baseline
-- ``schema.sql`` carries the same four tables for a fresh install, which runs
-- the baseline and records every numbered migration applied without executing
-- it; an existing database records the baseline unrun and executes only these.
-- Neither file alone reaches both, so the two must stay in step -- pinned by
-- ``schema_migration_test.py``.
--
-- Numbered 019, not 001: the deployed ledger already holds schema.018.sql, so
-- a lower number reads as applied and these tables would silently never exist.

-- ============================================================================
-- Session IR storage: four tables holding captured sessions as
-- ``trackinizer.lib.agent.sessions`` records rather than the lossy per-turn
-- ``agent_session_events`` union.
--
-- Source of truth: types/session_records.py (SessionRecordRow). Flat
-- fixed-shape side tables, written directly rather than generated from
-- per-kind ColumnSpec metadata.
--
-- A session spans several FILES -- claude splits on compaction, codex forks --
-- and ingest tails them as they appear, so it cannot fuse: fusing needs every
-- part up front. Each file is one ``part``, with ``idx`` from 0 within it.
-- Read-time order is still the provider's own: the manifest keeps each part's
-- SessionMetadata, which carries the fork link ``fuse.chain`` resolves.
CREATE TABLE IF NOT EXISTS session_records (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    -- DERIVED from the record's position in its file's normalized stream,
    -- never counted by the writer. A claude compaction REWRITES the session
    -- file, so the runner re-feeds lines it already sent; a counter would
    -- append a second copy of every retained record, while a derived idx
    -- lands each one back on the key it already holds.
    idx         INTEGER NOT NULL CHECK (idx >= 0),
    kind        TEXT NOT NULL,
    -- An idx within the SAME part. ``<=``, not ``<``: a claude TurnContext
    -- names ITSELF (it is appended at its own index), where a codex stream
    -- has none until its first turn_context line.
    context_id  INTEGER,
    timestamp   TIMESTAMPTZ,
    created     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Denormalized from the applying TurnContext: the console feed carries a
    -- model per row, and a self-join per feed row is cost for nothing. A
    -- projection, never read back into a record.
    model       TEXT,
    -- JSON, not JSONB. ``jsonb`` normalizes: it REORDERS object keys (and
    -- drops duplicates), so a payload written
    -- ``{"addedNames":...,"addedLines":...}`` reads back sorted and the
    -- rewritten session file differs from the one the CLI wrote. That breaks
    -- byte-exact round-trip, which resume depends on -- a CLI is entitled to
    -- reject a transcript it did not write. Nothing queries inside this
    -- column (it is written whole and read whole), so jsonb's indexing and
    -- containment operators buy nothing here to pay for that.
    payload     JSON NOT NULL,
    -- Computed once at ingest by ``types/session_records.py::search_text``,
    -- never re-derived: the phase-7 backfill writes a text that rule would
    -- compute as '', so a reindex would erase legacy searchability.
    text        TEXT NOT NULL DEFAULT '',
    -- 'simple', not 'english': a generated column needs an IMMUTABLE
    -- expression so the config must be a literal either way, and stemming
    -- mangles the identifiers and paths that fill a transcript.
    search      tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY (session_id, part, idx),
    CHECK (context_id IS NULL OR context_id <= idx)
);

CREATE INDEX IF NOT EXISTS idx_session_records_search
    ON session_records USING GIN (search);

CREATE INDEX IF NOT EXISTS idx_session_records_kind
    ON session_records (session_id, kind);

-- The cross-session console feed is a keyset scan over exactly this tuple
-- (``store/session.py::read_feed``), polled every 1.5s by every open console.
-- Without the index that ORDER BY is a sequential scan plus a sort over the
-- whole capture corpus -- 3,081,202 rows on the deployed instance -- and it
-- still returns the right answer, so nothing fails: the cost shows up only as
-- latency. The retired ``agent_session_events`` carried the same index for the
-- same query; it must not be lost in the move.
CREATE INDEX IF NOT EXISTS idx_session_records_created_session_part_idx
    ON session_records (created, session_id, part, idx);

-- One manifest per part: what the file was called, what it declared, and how
-- much of it is live. ``format`` names the convert.py adapter that wrote it;
-- '' means no native format (an ``sh`` PTY scrape), which is what makes a part
-- searchable but never resumable.
CREATE TABLE IF NOT EXISTS session_manifests (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    -- The whole SessionMetadata, both fields: the codex writer reads
    -- ``timestamp``, and claude's ascii-escaping convention (the majority flag
    -- plus its exception bitmap) rides in ``extra`` -- without which the file
    -- cannot be rewritten byte-exactly.
    -- JSON, not JSONB, for the same reason as ``session_records.payload``:
    -- jsonb reorders object keys. The codex launch line is rebuilt from this
    -- metadata, so a reordered ``extra`` rewrites ``{"timestamp","ordinal",
    -- "type"}`` as ``{"type","ordinal","timestamp"}`` -- same content, wrong
    -- bytes, and a session the CLI may refuse to resume.
    metadata    JSON NOT NULL,
    ir_id       UUID NOT NULL,
    format      TEXT NOT NULL,
    -- Live prefix bound: every reader takes ``idx < records``, so a file that
    -- shrank leaves its tail rows inert rather than deleted.
    records     INTEGER NOT NULL CHECK (records >= 0),
    PRIMARY KEY (session_id, part),
    -- Serializes part assignment: two appends racing on an unseen file both
    -- compute max(part)+1, and the loser re-reads the winner's.
    UNIQUE (session_id, name)
);

-- Ciphertext, split from the record so retention can drop it without
-- touching what search indexes. ``Thinking.encrypted`` is the IR's only
-- ciphertext, so the record's own key suffices.
--
-- Stored verbatim as the base64 ASCII, NOT decoded: claude writes standard
-- base64 and codex base64url, so one decode/encode pair cannot round-trip
-- both. STORAGE EXTERNAL because ciphertext does not compress -- measured
-- 1.025x under pglz (it EXPANDS), against 0.405x for plaintext of the same
-- table, so the default EXTENDED pays for a compression pass that never wins.
CREATE TABLE IF NOT EXISTS session_ciphertext (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    idx         INTEGER NOT NULL CHECK (idx >= 0),
    bytes       BYTEA NOT NULL,
    PRIMARY KEY (session_id, part, idx)
);

ALTER TABLE session_ciphertext ALTER COLUMN bytes SET STORAGE EXTERNAL;

-- A slash command is PTY-observed and absent from the session log, so it
-- cannot be re-derived and cannot hold an ``idx``. ``seq`` is server-assigned
-- (max(seq)+1) because a sink counter restarts at 0 on resume and would
-- collide. Not in search: a result keys (session_id, part, idx) and a slash
-- command has no such coordinate.
CREATE TABLE IF NOT EXISTS session_slash_commands (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL CHECK (seq >= 0),
    timestamp   TIMESTAMPTZ NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    command     TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, seq)
);
