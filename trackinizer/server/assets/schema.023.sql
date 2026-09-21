-- schema.023.sql -- per-model session-embedding storage + partial HNSW indexes.
--
-- Widens ``session_embeddings.embedding`` from ``halfvec(1024)`` to a
-- dimension-free ``halfvec`` so several models with DIFFERENT native dims can
-- coexist in one table (the row already keys by ``model``). A single global HNSW
-- index cannot cover a dimension-free column -- pgvector rejects it with
-- "column does not have dimensions" (measured on pgvector 0.8.1) -- so the one
-- global index is replaced by one PARTIAL index per shipped model, each casting
-- the column to that model's fixed dim: ``(embedding::halfvec(N))`` WHERE
-- model = '<slug>'. A cosine query hits its partial index only when it uses the
-- SAME cast expression (server/store/session_search.py threads the selected
-- embedder's dim into the ORDER BY).
--
-- The baseline ``schema.sql`` carries the same dimension-free column and the
-- same seven partial indexes for a fresh install; a fresh DB records this
-- migration applied WITHOUT executing it, an existing DB records the baseline
-- unrun and executes only this file, so the two must stay in step -- pinned by
-- ``schema_migration_test.py``.
--
-- Numbered 023: the deployed ledger holds through schema.022.sql.
--
-- Existing ``halfvec(1024)`` rows are preserved byte-for-byte: dropping the
-- typmod is a metadata-only change (measured -- stored values compare equal
-- before/after). The ALTER rewrites no data; only the index set changes. Future
-- models are indexed by an idempotent code-side ensure-step
-- (``store/session_index.ensure_model_index``), not a new migration per model;
-- these seven are the shipped set the parity gate covers.

-- Drop the typmod: dimension-free halfvec holds mixed-dim rows.
ALTER TABLE session_embeddings
    ALTER COLUMN embedding TYPE halfvec;

-- The old single global index cannot exist on a dimension-free column.
DROP INDEX IF EXISTS idx_session_embeddings_hnsw;

-- One partial HNSW per shipped model, cast to that model's stored dim. Cosine
-- (``halfvec_cosine_ops`` / ``<=>``), matching the query path. Empty until a
-- model is swept; an empty partial HNSW is instant to build.
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
