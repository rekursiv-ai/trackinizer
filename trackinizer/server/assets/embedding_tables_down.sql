-- embedding_tables_down.sql -- operator-only: build again, empty, the four tables of the
-- old embedding engine that schema.081.sql drops, as schema.sql built them before 081.
-- For loading an archive of their rows back (``server/tools/archive_embeddings.py`` of
-- 0.1.13, or monorepo commit 2290694993, restores into these), and for tests that hold
-- a database of an older shape. Startup never runs it. ``IF NOT EXISTS``: a second run changes nothing.

-- Per-inquiry, per-model title vectors, ``vector(384)``.
CREATE TABLE IF NOT EXISTS inquiry_embeddings (
    inquiry_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    embedding   vector(384) NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (inquiry_id, model)
);

CREATE INDEX IF NOT EXISTS idx_inquiry_embeddings_model
    ON inquiry_embeddings (model);

-- One row per session record unit, per mapper and model; dimension-free ``halfvec``
-- with one partial HNSW index per shipped model.
CREATE TABLE IF NOT EXISTS session_embeddings (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    field       TEXT NOT NULL,
    chunk       INTEGER NOT NULL DEFAULT 0 CHECK (chunk >= 0),
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    embedding   halfvec NOT NULL,
    text_md5    TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, part, idx, field, chunk, mapper, model)
);

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_stub_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'stub-1024';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_qwen3_embedding_0_6b_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'qwen3-embedding-0.6b@1024';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_qwen3_embedding_4b_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'qwen3-embedding-4b@1024';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_qwen3_embedding_8b_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'qwen3-embedding-8b@1024';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_octen_embedding_8b_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'octen-embedding-8b@1024';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_jina_embeddings_v5_text_nano_768
    ON session_embeddings USING hnsw ((embedding::halfvec(768)) halfvec_cosine_ops)
    WHERE model = 'jina-embeddings-v5-text-nano@768';

CREATE INDEX IF NOT EXISTS idx_session_embeddings_hnsw_jina_embeddings_v5_text_small_1024
    ON session_embeddings USING hnsw ((embedding::halfvec(1024)) halfvec_cosine_ops)
    WHERE model = 'jina-embeddings-v5-text-small@1024';

-- The sweep's freshness marker per (record, mapper, model), and its cursor.
CREATE TABLE IF NOT EXISTS session_index_state (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    text_md5    TEXT NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, part, idx, mapper, model)
);

CREATE TABLE IF NOT EXISTS session_index_cursor (
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    snapshot    pg_snapshot NOT NULL,
    updated     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mapper, model)
);
