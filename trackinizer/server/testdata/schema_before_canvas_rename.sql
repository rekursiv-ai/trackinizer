-- Frozen pre-cutover schema through 057 from main abfbd46def30.
CREATE EXTENSION IF NOT EXISTS vector;
{per_kind_sequences}
CREATE TABLE IF NOT EXISTS inquiries (
    id             UUID PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ({inquiry_kinds})),
    seq            INTEGER NOT NULL,
    UNIQUE (kind, seq),
    owner          TEXT,
    account        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                       'active', 'complete', 'abandoned', 'invalid'
                   )),
    title          TEXT NOT NULL,
    description    TEXT,
    labels         TEXT[],
    subscribers    TEXT[],
    app            JSONB CHECK (
                       app IS NULL
                       OR jsonb_typeof(app) = 'object' AND octet_length(app::text) <= 65536
                   ),
    marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    proves_authority    DOUBLE PRECISION,
    favors_authority    DOUBLE PRECISION,
    cited_by_authority  DOUBLE PRECISION,
    issue_authority     DOUBLE PRECISION,
    locked         BOOLEAN NOT NULL DEFAULT FALSE,
    created        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
{inquiry_kind_columns},
    CONSTRAINT inquiries_agentsession_lifecycle_check CHECK (
        kind <> 'AgentSession'
        OR (agentsession_ended IS NOT NULL) = (status = 'complete')
    )
);
CREATE INDEX IF NOT EXISTS idx_inquiries_kind_status
    ON inquiries (kind, status);
CREATE INDEX IF NOT EXISTS idx_inquiries_kind_seq
    ON inquiries (kind, seq);
CREATE INDEX IF NOT EXISTS idx_inquiries_owner
    ON inquiries (owner) WHERE owner IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inquiries_account
    ON inquiries (account);
CREATE UNIQUE INDEX IF NOT EXISTS uq_inquiries_live_session_owner
    ON inquiries (owner)
    WHERE kind = 'AgentSession' AND owner IS NOT NULL
          AND agentsession_ended IS NULL;
CREATE INDEX IF NOT EXISTS idx_inquiries_judgement
    ON inquiries (belief_judgement) WHERE belief_judgement IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inquiries_confidence
    ON inquiries (belief_confidence) WHERE belief_confidence IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inquiries_priority
    ON inquiries (issue_priority) WHERE issue_priority IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inquiries_issue_kind_gin
    ON inquiries USING gin(issue_kind);
CREATE INDEX IF NOT EXISTS idx_inquiries_labels_gin
    ON inquiries USING gin(labels);
CREATE INDEX IF NOT EXISTS idx_inquiries_subscribers_gin
    ON inquiries USING gin(subscribers);
CREATE INDEX IF NOT EXISTS idx_inquiries_created_id
    ON inquiries (created DESC, id DESC);
ALTER TABLE inquiries DROP COLUMN IF EXISTS embedding;
CREATE TABLE IF NOT EXISTS inquiry_embeddings (
    inquiry_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    embedding   vector(384) NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (inquiry_id, model)
);
CREATE INDEX IF NOT EXISTS idx_inquiry_embeddings_model
    ON inquiry_embeddings (model);
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
CREATE TABLE IF NOT EXISTS session_bodies (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    payload_zst BYTEA NOT NULL,
    text_zst    BYTEA NOT NULL,
    PRIMARY KEY (session_id, part, idx)
);
CREATE TABLE IF NOT EXISTS edges (
    from_id        UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    from_kind      TEXT NOT NULL,
    to_id          UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    to_kind        TEXT NOT NULL,
    edge_kind      TEXT NOT NULL,
{edge_metadata_columns},
    PRIMARY KEY (from_id, to_id, edge_kind),
    CHECK (from_id <> to_id),
    CHECK (
        (edge_kind = 'narrows'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'requires'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'produced_by'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind IN ('proves', 'favors')
            AND from_kind IN ({artifact_kinds})
            AND to_kind IN ({claimable_kinds}))
     OR (edge_kind = 'supersedes'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind = 'cites_paper'
            AND from_kind IN ({paper_kinds})
            AND to_kind IN ({paper_kinds}))
    )
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_id, edge_kind);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges (to_id, edge_kind);
CREATE TABLE IF NOT EXISTS change_log (
    id                 UUID PRIMARY KEY,
    created            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    api_key_id         UUID,
    actor              TEXT NOT NULL,
    subject_id         UUID NOT NULL,
    subject_kind       TEXT NOT NULL,
    kind               TEXT NOT NULL,
    caused_by          UUID REFERENCES change_log(id),
    reason             TEXT NOT NULL DEFAULT '',
    old_peer_id             UUID,
    old_peer_kind           TEXT,
    old_peer_edge_kind      TEXT,
{edge_metadata_mirror_old}
    new_peer_id             UUID,
    new_peer_kind           TEXT,
    new_peer_edge_kind      TEXT,
{edge_metadata_mirror_new}
    old_marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    old_marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    new_marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    new_marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
{change_log_mirror}
    subscribers_snapshot           TEXT[] NOT NULL DEFAULT '{}',
    CHECK (subject_kind IN ({inquiry_kinds})),
    CHECK (kind IN ({change_kinds})),
    CHECK (
        CASE WHEN kind IN ('edge_added', 'edge_removed', 'edge_annotation_changed', 'dependency_changed')
            THEN
                (old_peer_id IS NOT NULL) = (old_peer_kind IS NOT NULL)
            AND (old_peer_id IS NOT NULL) = (old_peer_edge_kind IS NOT NULL)
            AND (old_peer_id IS NOT NULL) = (old_edge_note IS NOT NULL)
            AND (old_peer_id IS NOT NULL) = (old_edge_labels IS NOT NULL)
            AND (new_peer_id IS NOT NULL) = (new_peer_kind IS NOT NULL)
            AND (new_peer_id IS NOT NULL) = (new_peer_edge_kind IS NOT NULL)
            AND (new_peer_id IS NOT NULL) = (new_edge_note IS NOT NULL)
            AND (new_peer_id IS NOT NULL) = (new_edge_labels IS NOT NULL)
            AND (old_peer_id IS NOT NULL OR new_peer_id IS NOT NULL)
            ELSE old_peer_id IS NULL AND new_peer_id IS NULL
             AND old_peer_kind IS NULL AND new_peer_kind IS NULL
             AND old_peer_edge_kind IS NULL AND new_peer_edge_kind IS NULL
             AND old_edge_priority IS NULL AND new_edge_priority IS NULL
             AND old_edge_note IS NULL AND new_edge_note IS NULL
             AND old_edge_valence IS NULL AND new_edge_valence IS NULL
             AND old_edge_labels IS NULL AND new_edge_labels IS NULL
        END
    ),
    CHECK (old_peer_kind IS NULL OR old_peer_kind IN ({inquiry_kinds})),
    CHECK (new_peer_kind IS NULL OR new_peer_kind IN ({inquiry_kinds})),
    CHECK (old_peer_edge_kind IS NULL OR old_peer_edge_kind IN ({edge_kinds})),
    CHECK (new_peer_edge_kind IS NULL OR new_peer_edge_kind IN ({edge_kinds})),
{change_log_kind_matrix}
);
CREATE INDEX IF NOT EXISTS idx_change_log_subject_kind
    ON change_log (subject_id, kind, created);
CREATE INDEX IF NOT EXISTS idx_change_log_kind_created
    ON change_log (kind, created);
CREATE INDEX IF NOT EXISTS idx_change_log_created
    ON change_log (created);
CREATE INDEX IF NOT EXISTS idx_change_log_created_id
    ON change_log (created DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_change_log_subscribers_snapshot
    ON change_log USING gin (subscribers_snapshot);
CREATE INDEX IF NOT EXISTS idx_change_log_subscribed_created_id
    ON change_log (created, id)
    WHERE subscribers_snapshot != '{}';
CREATE INDEX IF NOT EXISTS idx_change_log_caused_by
    ON change_log (caused_by) WHERE caused_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_change_log_new_status
    ON change_log (new_status) WHERE new_status IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_change_log_new_judgement
    ON change_log (new_belief_judgement) WHERE new_belief_judgement IS NOT NULL;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'author'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'actor'
    ) THEN
        ALTER TABLE change_log RENAME COLUMN author TO actor;
    END IF;
END$$;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT '';
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'change_log_principal_user_id_fkey'
    ) THEN
        ALTER TABLE change_log
            DROP CONSTRAINT change_log_principal_user_id_fkey;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'principal_user_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'api_key_id'
    ) THEN
        ALTER TABLE change_log RENAME COLUMN principal_user_id TO api_key_id;
    END IF;
END$$;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS api_key_id UUID;
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('viewer', 'writer', 'admin')),
    status      TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    visual_workspace_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_login  TIMESTAMPTZ,
    acknowledged_at            TIMESTAMPTZ,
    acknowledged_rules_version TEXT,
    hosted_domain TEXT
        CHECK (hosted_domain = lower(hosted_domain) AND char_length(hosted_domain) <= 253),
    google_sub TEXT
        CHECK (char_length(google_sub) BETWEEN 1 AND 255),
    preferences JSONB CHECK (jsonb_typeof(preferences) = 'object')
);
CREATE TABLE IF NOT EXISTS inquiry_lock_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    inquiry_id UUID NOT NULL,
    locked     BOOLEAN NOT NULL,
    actor      TEXT NOT NULL,
    created    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    secret_hash   TEXT NOT NULL,
    prefix        TEXT NOT NULL,
    role          TEXT NOT NULL
                  CONSTRAINT api_keys_role_check
                  CHECK (role IN ('viewer', 'writer', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    open_into     TEXT
        CHECK (open_into ~ '^[a-z][a-z0-9_-]{0,31}$')
);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS role TEXT;
DO $$
BEGIN
    UPDATE api_keys
       SET role = users.role
      FROM users
     WHERE api_keys.user_id = users.id
       AND api_keys.role IS NULL;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'api_keys'
          AND column_name = 'role'
          AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE api_keys ALTER COLUMN role SET NOT NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'api_keys_role_check'
    ) THEN
        ALTER TABLE api_keys
            ADD CONSTRAINT api_keys_role_check
            CHECK (role IN ('viewer', 'writer', 'admin'));
    END IF;
END$$;
CREATE INDEX IF NOT EXISTS idx_api_keys_user
    ON api_keys (user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix_live
    ON api_keys (prefix) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS allowlist (
    email_or_pattern  TEXT PRIMARY KEY,
    role              TEXT NOT NULL CHECK (role IN ('viewer', 'writer', 'admin')),
    added_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'change_log_api_key_id_fkey'
    ) THEN
        ALTER TABLE change_log
            DROP CONSTRAINT change_log_api_key_id_fkey;
    END IF;
    ALTER TABLE change_log
        ADD CONSTRAINT change_log_api_key_id_fkey
        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
        ON DELETE SET NULL;
END$$;
CREATE TABLE IF NOT EXISTS experiment_metrics (
    experiment_id UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    key           TEXT NOT NULL CHECK (
        char_length(key) BETWEEN 1 AND 512
        AND btrim(key, E'\t\n\x0b\f\r\x1c\x1d\x1e\x1f \u0085  '
            '          '
            '     　') <> ''),
    step          BIGINT NOT NULL CHECK (step >= 0),
    value         DOUBLE PRECISION NOT NULL CHECK (
        value <> 'NaN'::float8
        AND value <> 'Infinity'::float8
        AND value <> '-Infinity'::float8),
    kind          TEXT NOT NULL DEFAULT 'scalar' CHECK (kind = 'scalar'),
    timestamp     TIMESTAMPTZ,
    created       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (experiment_id, key, step)
);
CREATE TABLE IF NOT EXISTS session_records (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    idx         INTEGER NOT NULL CHECK (idx >= 0),
    kind        TEXT NOT NULL,
    context_id  INTEGER,
    timestamp   TIMESTAMPTZ,
    created     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    model       TEXT,
    payload     JSON NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    search      tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    PRIMARY KEY (session_id, part, idx),
    CHECK (context_id IS NULL OR context_id <= idx)
);
CREATE INDEX IF NOT EXISTS idx_session_records_search
    ON session_records USING GIN (search);
CREATE INDEX IF NOT EXISTS idx_session_records_kind
    ON session_records (session_id, kind);
CREATE INDEX IF NOT EXISTS idx_session_records_recent_turns
    ON session_records (session_id, part DESC, idx DESC)
    WHERE kind IN ('UserMessage', 'AssistantMessage');
CREATE INDEX IF NOT EXISTS idx_session_records_created_session_part_idx
    ON session_records (created, session_id, part, idx);
CREATE TABLE IF NOT EXISTS session_manifests (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    metadata    JSON NOT NULL,
    ir_id       UUID NOT NULL,
    format      TEXT NOT NULL,
    records     INTEGER NOT NULL CHECK (records >= 0),
    opened_by_api_key_id UUID,
    PRIMARY KEY (session_id, part),
    UNIQUE (session_id, name)
);
CREATE TABLE IF NOT EXISTS session_ciphertext (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    part        INTEGER NOT NULL DEFAULT 0,
    idx         INTEGER NOT NULL CHECK (idx >= 0),
    bytes       BYTEA NOT NULL,
    PRIMARY KEY (session_id, part, idx)
);
ALTER TABLE session_ciphertext ALTER COLUMN bytes SET STORAGE EXTERNAL;
CREATE TABLE IF NOT EXISTS session_slash_commands (
    session_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL CHECK (seq >= 0),
    timestamp   TIMESTAMPTZ NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    command     TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS session_liveness (
    session_id  UUID PRIMARY KEY REFERENCES inquiries(id) ON DELETE CASCADE,
    last_seen   TIMESTAMPTZ NOT NULL,
    reaped      BOOLEAN NOT NULL DEFAULT FALSE
);
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
CREATE TABLE IF NOT EXISTS machines (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    role        TEXT NOT NULL DEFAULT ''
                CHECK (role = '' OR role ~ '^[a-z][a-z0-9-]{0,31}$'),
    how         TEXT NOT NULL DEFAULT '' CHECK (char_length(how) <= 2000),
    labels      TEXT[] NOT NULL DEFAULT '{}',
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT NOT NULL,
    updated     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ,
    host_instance  UUID,
    host_version   TEXT NOT NULL DEFAULT '' CHECK (char_length(host_version) <= 64),
    facts          JSONB NOT NULL DEFAULT '{}'
                   CHECK (jsonb_typeof(facts) = 'object' AND octet_length(facts::text) <= 8192),
    owner          TEXT,
    confirmed_at   TIMESTAMPTZ,
    last_run_cwd   TEXT
);
CREATE TABLE IF NOT EXISTS machine_enrollments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id    UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    secret_sha256 BYTEA NOT NULL CHECK (octet_length(secret_sha256) = 32),
    created_by    TEXT NOT NULL,
    created       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_machine_enrollments_open
    ON machine_enrollments (machine_id) WHERE used_at IS NULL;
CREATE TABLE IF NOT EXISTS machine_credentials (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id    UUID NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    secret_sha256 BYTEA NOT NULL CHECK (octet_length(secret_sha256) = 32),
    created       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used     TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_machine_credentials_live
    ON machine_credentials (machine_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS visual_workspaces (
    id          UUID PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision    BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    state       JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    station     JSONB CHECK (jsonb_typeof(station) = 'object')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_workspaces_user_station_name
    ON visual_workspaces (user_id, (station->>'name'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_workspaces_user_station_chat
    ON visual_workspaces (user_id, (station->>'conversation_id'));
CREATE INDEX IF NOT EXISTS idx_visual_workspaces_user_modified
    ON visual_workspaces (user_id, modified_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS visual_workspace_operations (
    workspace_id UUID NOT NULL REFERENCES visual_workspaces(id) ON DELETE CASCADE,
    key          UUID NOT NULL,
    request_hash TEXT NOT NULL,
    response     JSONB NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, key)
);
CREATE TABLE IF NOT EXISTS visual_reports (
    id          UUID PRIMARY KEY,
    issue_id    UUID NOT NULL,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS visual_report_revisions (
    report_id    UUID NOT NULL REFERENCES visual_reports(id) ON DELETE RESTRICT,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    artifact_id  UUID NOT NULL UNIQUE,
    author_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    author_email TEXT NOT NULL,
    content      JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    publish_key  UUID NOT NULL,
    request_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    content_bytes BIGINT NOT NULL CHECK (content_bytes > 0),
    PRIMARY KEY (report_id, revision),
    UNIQUE (author_id, publish_key)
);
CREATE TABLE IF NOT EXISTS science_chat_shares (
    conversation_id UUID NOT NULL,
    email           TEXT NOT NULL CHECK (email = lower(email)),
    shared_by       TEXT NOT NULL,
    shared_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (conversation_id, email)
);
CREATE TABLE IF NOT EXISTS science_chat_tombstones (
    conversation_id UUID PRIMARY KEY,
    deleted_by      TEXT NOT NULL,
    deleted_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS credit_entries (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      TEXT CHECK (email = lower(email)),
    kind       TEXT NOT NULL
               CHECK (kind IN ('grant', 'purchase', 'chat', 'campaign', 'release', 'refund', 'dispute')),
    usd        NUMERIC(14, 6) NOT NULL CHECK (usd >= 0),
    ref        TEXT NOT NULL,
    idem       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (email IS NOT NULL OR kind IN ('refund', 'dispute'))
);
CREATE UNIQUE INDEX IF NOT EXISTS credit_entries_one_purchase_per_payment
    ON credit_entries (ref) WHERE kind = 'purchase';
CREATE INDEX IF NOT EXISTS credit_entries_user_created
    ON credit_entries (email, created_at);
CREATE TABLE IF NOT EXISTS credit_grace (
    email  TEXT PRIMARY KEY CHECK (email = lower(email)),
    until  DATE NOT NULL,
    note   TEXT NOT NULL DEFAULT '',
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
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
CREATE TABLE IF NOT EXISTS gpu_owners (
    id         BIGSERIAL PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS gpu_box_credentials (
    id            UUID PRIMARY KEY,
    email         TEXT NOT NULL,
    campaign      TEXT NOT NULL,
    box           TEXT NOT NULL,
    secret_sha256 BYTEA NOT NULL,
    minted_by     UUID,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS gpu_box_credentials_live
    ON gpu_box_credentials (email, box) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS gpu_box_credentials_campaign
    ON gpu_box_credentials (campaign);
CREATE TABLE IF NOT EXISTS gpu_jobs (
    id                 BIGSERIAL PRIMARY KEY,
    email              TEXT NOT NULL,
    owner_id           BIGINT NOT NULL REFERENCES gpu_owners(id),
    state              TEXT NOT NULL
                       CHECK (state IN ('requested', 'placed', 'granted', 'starting',
                                        'queued', 'running', 'completed', 'failed',
                                        'cancelled', 'timeout', 'expired')),
    cancel_requested   BOOLEAN NOT NULL DEFAULT false,
    gpus               INT NOT NULL,
    minutes            INT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    argv               JSONB NOT NULL,
    commit             TEXT NOT NULL,
    public_key         TEXT NOT NULL,
    key_sha256         TEXT NOT NULL,
    node               TEXT,
    host               TEXT,
    port               INT,
    uid                BIGINT,
    host_key           TEXT,
    slurm_id           TEXT,
    token_sha256       TEXT,
    bundle_sha256      TEXT,
    bundle_bytes       BIGINT,
    requested_by       UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    placed_at          TIMESTAMPTZ,
    granted_at         TIMESTAMPTZ,
    start_requested_at TIMESTAMPTZ,
    queued_at          TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    ended_at           TIMESTAMPTZ,
    sealed_at          TIMESTAMPTZ,
    elapsed_s          BIGINT,
    gpu_minutes        BIGINT,
    usd                NUMERIC(12, 4),
    files              BIGINT,
    bytes              BIGINT,
    manifest_sha256    TEXT,
    manifest           JSONB,
    reason             TEXT NOT NULL DEFAULT '',
    claimed_until      TIMESTAMPTZ,
    box_credential_id  UUID REFERENCES gpu_box_credentials(id)
);
CREATE INDEX IF NOT EXISTS gpu_jobs_email_created
    ON gpu_jobs (email, created_at DESC);
CREATE INDEX IF NOT EXISTS gpu_jobs_live
    ON gpu_jobs (state)
    WHERE state NOT IN ('completed', 'failed', 'cancelled', 'timeout', 'expired');
CREATE UNIQUE INDEX IF NOT EXISTS gpu_jobs_token
    ON gpu_jobs (token_sha256) WHERE token_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS gpu_jobs_box
    ON gpu_jobs (box_credential_id) WHERE box_credential_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS gpu_issuances (
    id           BIGSERIAL PRIMARY KEY,
    job_id       BIGINT NOT NULL REFERENCES gpu_jobs(id),
    email        TEXT NOT NULL,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('upload-cert', 'read-cert', 'job-token', 'secret-read')),
    principal    TEXT,
    key_sha256   TEXT,
    public_key   TEXT,
    certificate  TEXT,
    names        TEXT[],
    node         TEXT,
    valid_after  TIMESTAMPTZ,
    valid_before TIMESTAMPTZ,
    requested_by UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    fetched_at   TIMESTAMPTZ,
    signed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS gpu_issuances_job
    ON gpu_issuances (job_id, id);
CREATE TABLE IF NOT EXISTS device_logins (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_sha256  BYTEA NOT NULL UNIQUE CHECK (octet_length(device_sha256) = 32),
    user_code      TEXT NOT NULL UNIQUE CHECK (user_code ~ '^[A-Z]{8}$'),
    client         TEXT NOT NULL CHECK (client ~ '^[a-z][a-z0-9-]{0,31}$'),
    host           TEXT NOT NULL DEFAULT '' CHECK (char_length(host) <= 63),
    machine        TEXT CHECK (machine ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'denied')),
    approver       UUID REFERENCES users(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    last_polled_at TIMESTAMPTZ,
    CHECK ((status = 'pending') = (approver IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_device_logins_expires
    ON device_logins (expires_at);
CREATE TABLE IF NOT EXISTS machine_jobs (
    id               BIGINT PRIMARY KEY DEFAULT nextval('gpu_jobs_id_seq'),
    email            TEXT NOT NULL,
    machine_id       UUID REFERENCES machines(id) ON DELETE SET NULL,
    machine          TEXT NOT NULL,
    state            TEXT NOT NULL
                     CHECK (state IN ('requested', 'queued', 'running', 'completed',
                                      'failed', 'cancelled', 'timeout')),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    gpus             INT NOT NULL,
    memory_mb        INT NOT NULL,
    minutes          INT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    argv             JSONB NOT NULL,
    remote           TEXT NOT NULL,
    ref              TEXT NOT NULL,
    commit           TEXT NOT NULL,
    exit_code        INT,
    reason           TEXT NOT NULL DEFAULT '',
    requested_by     UUID,
    claimed_until    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    queued_at        TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS machine_jobs_email_created
    ON machine_jobs (email, created_at DESC);
CREATE INDEX IF NOT EXISTS machine_jobs_live
    ON machine_jobs (machine_id)
    WHERE state NOT IN ('completed', 'failed', 'cancelled', 'timeout');
