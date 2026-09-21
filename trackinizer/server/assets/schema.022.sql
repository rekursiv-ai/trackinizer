-- schema.022.sql -- per-record session embeddings (the vector search surface).
--
-- Additive migration for a database that predates ``session_embeddings``. The
-- baseline ``schema.sql`` carries this same table and index for a fresh
-- install, which runs the baseline and records this migration applied WITHOUT
-- executing it; an existing database records the baseline unrun and executes
-- only this file. Neither file alone reaches both populations, so the two must
-- stay in step -- pinned by ``schema_migration_test.py``.
--
-- Numbered 022: the deployed ledger already holds through schema.021.sql, so a
-- lower number reads as applied and this table would silently never exist.
--
-- Purely additive DDL into a table the old build never reads, so it is safe
-- against the OLD code and its duration is not downtime (run it against the
-- live database with the old server still serving; see
-- ``docs/db_schema_migration.md``). No backfill here: the ``text_md5`` sweep
-- (``store/session_embed.py``) populates rows, and its predicate treats a
-- missing row identically to a stale one -- one pass serves both backfill and
-- live ingest.

-- ============================================================================
-- Per-record session embeddings: the vector surface over ``session_records``
-- (design: docs/private/session_indexing.md). One row per INDEX UNIT -- a
-- record's field ("content" whole/chunked, or a machine-output "head") as
-- selected by a SemanticMapper policy (server/semantic_mapper.py).
--
-- ``halfvec(1024)``: Qwen3-Embedding-4B Matryoshka-truncated to 1024, fp16.
-- Requires pgvector >= 0.7 (production runs 0.8.6; pglite 0.8.1).
--
-- ``mapper`` names the POLICY that produced the unit, ``model`` the embedder;
-- both key the row so a policy change and a model change are separately
-- re-backfillable, side by side, like ``inquiry_embeddings.model``.
--
-- ``text_md5`` is the re-embed trigger: a claude compaction rewrites a part's
-- records in place (``restart`` upserts), so a sweep re-embeds exactly the
-- rows where ``md5(r.text) IS DISTINCT FROM e.text_md5`` -- one predicate
-- serves both backfill and live ingest.
--
-- No FK to ``session_records``: the retype runner and compaction restarts
-- DELETE+reinsert record rows, and a CASCADE would silently drop vectors that
-- one ``text_md5`` sweep would otherwise reconcile. Orphans are reaped by the
-- same sweep. The session-level FK still bounds the lifetime.
CREATE TABLE IF NOT EXISTS session_embeddings (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    field       TEXT NOT NULL,
    chunk       INTEGER NOT NULL DEFAULT 0 CHECK (chunk >= 0),
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    embedding   halfvec(1024) NOT NULL,
    text_md5    TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, part, idx, field, chunk, mapper, model)
);

-- HNSW over cosine distance, matching ``find_similar``'s ``<=>`` operator
-- choice on inquiry_embeddings.
CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw
    ON session_embeddings USING hnsw (embedding halfvec_cosine_ops);


-- Replay-only bodies for heavy record kinds: the cold half of the hot/cold
-- split. The twin of ``session_ciphertext``: same key, spliced back only on
-- replay (``read_session_records`` ``plaintext_only=False``). Full rationale
-- on the baseline copy in ``schema.sql``.
CREATE TABLE IF NOT EXISTS session_bodies (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    payload_zst BYTEA NOT NULL,
    text_zst    BYTEA NOT NULL,
    PRIMARY KEY (session_id, part, idx)
);
