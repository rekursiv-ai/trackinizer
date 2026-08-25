-- Trackinizer schema. Applied idempotently by Store.bootstrap().
--
-- Three tables:
--   * ``inquiries``  -- one row per Inquiry. ``kind`` discriminates among
--     the concrete dataclasses in ``custom_types.py``; per-kind columns are
--     populated according to ``kind`` and gated by CHECK constraints.
--     Embedded ordered-list relationships (subscribers, Experiment
--     code_changes) live as row-level arrays here. A WebSearch's findings are
--     ``produced_by`` edges (WebResult/Paper -> WebSearch), not a column.
--   * ``edges``      -- normalized Inquiry ↔ Inquiry graph for unordered
--     relationships: Issue decomposition (``narrows``), sequencing
--     (``requires``), provenance (``produced_by``), Belief/Experiment citations
--     (``proves`` / ``favors``, signed by valence), and supersession. (Against-
--     citations are the negative-valence sign of ``proves`` / ``favors``, not a
--     separate kind.)
--   * ``change_log`` -- append-only audit. ``subject_id`` is deliberately
--     FK-free so ``purged`` change tombstones survive deletion.
--
-- The Python type universe in ``custom_types.py`` is the design contract;
-- this file is the storage realization. Every closed-set literal in
-- custom_types.py is mirrored as a CHECK constraint here.

CREATE EXTENSION IF NOT EXISTS vector;


-- Per-kind short-ref sequences (``Issue#7``, ``Experiment#3``, ...).
-- Each kind has its own monotonic counter so refs stay stable per kind and
-- contiguous per ``trax list <kind>`` view.
-- Per-kind sequences. Generated from _SEQ_FOR_KIND (derived from the
-- Inquiry hierarchy) by _generate_per_kind_sequences.
{per_kind_sequences}


CREATE TABLE IF NOT EXISTS inquiries (
    id             UUID PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ({inquiry_kinds})),
    seq            INTEGER NOT NULL,
    UNIQUE (kind, seq),
    -- Optional columns are nullable: NULL is the single encoding of "unset",
    -- matching the ``| None`` fields on the Inquiry dataclass. ``status`` and
    -- the cost axes stay NOT NULL because their defaults ('active', 0) are
    -- real values, not absence stand-ins.
    owner          TEXT,
    -- The authenticated account a row is attributed to. NOT NULL: every row
    -- carries one. The Store stamps the creator on insert and requires a
    -- route-validated account; the "must be an active user" check is enforced
    -- at the route boundary (``api/submit.py`` / ``api/edit.py`` via
    -- ``auth.assert_account_active``), not by the Store. Distinct from
    -- ``owner`` (responsibility); this is the auth identity the row is held
    -- under. No DEFAULT -- the value is always supplied by the application,
    -- never a DB-side stand-in.
    account        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                       'active', 'complete', 'abandoned', 'invalid'
                   )),
    title          TEXT NOT NULL,
    description    TEXT,
    labels         TEXT[],
    subscribers    TEXT[],
    marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    created        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    modified       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),

    -- Per-kind column declarations + CASE-WHEN CHECKs. Generated from
    -- custom_types.ColumnSpec metadata by _generate_inquiry_kind_columns.
{inquiry_kind_columns},

    -- AgentSession lifecycle: ``agentsession_ended`` is set iff the session is
    -- ``complete``. Binding the two makes the zombie states unrepresentable: a
    -- session born ``ended`` while not complete (un-messageable yet
    -- un-endable), or marked ``complete`` while ``ended`` stays NULL (complete
    -- yet still "live" by the ``ended IS NULL`` messaging predicate). A live
    -- session may still be ``abandoned`` / ``invalid`` (terminal but never
    -- formally ended via /end), so only ``complete`` is tied to ``ended``. A
    -- non-AgentSession row never sets ``agentsession_ended``, so the arm is
    -- vacuously true for them.
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
-- One LIVE AgentSession per routing name: ``owner`` doubles as the ``@actor``
-- routing handle, so two concurrent live sessions cannot share it (addressing
-- would be ambiguous). A PARTIAL UNIQUE index over live sessions
-- (``agentsession_ended IS NULL``) makes the DB the arbiter -- the
-- reserve-then-insert window is otherwise racy (two starts can mint the same
-- name). The session-start path retries on a violation to mint the next
-- ``#N`` suffix. Ended sessions free the name (they fall out of the predicate).
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
-- Tie-breaker index for offset pagination over ``list_kind``.
CREATE INDEX IF NOT EXISTS idx_inquiries_created_id
    ON inquiries (created DESC, id DESC);

-- Pre-refactor cleanup: the embedding column used to live on inquiries.
-- It now lives in inquiry_embeddings (one row per (inquiry, model)).
-- Self-heal databases that predate the split. This lives in the baseline
-- (not a numbered migration) because the baseline is the only step
-- guaranteed to run before every numbered migration: migration 007 joins on
-- ``change_log.api_key_id``, so the auth-v2 column self-heals below must
-- precede it, which only a baseline placement guarantees.
ALTER TABLE inquiries DROP COLUMN IF EXISTS embedding;


-- Per-inquiry, per-model embeddings. Multiple rows per inquiry allow
-- side-by-side comparison of embedding approaches. All vectors here share
-- ``vector(384)``; when a non-384 model lands, add a sibling table
-- ``inquiry_embeddings_<dim>`` and route by dim.
CREATE TABLE IF NOT EXISTS inquiry_embeddings (
    inquiry_id  UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    embedding   vector(384) NOT NULL,
    created     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (inquiry_id, model)
);

CREATE INDEX IF NOT EXISTS idx_inquiry_embeddings_model
    ON inquiry_embeddings (model);


-- Edge catalog (validated by the CHECK on this table). Every edge is stored
-- child -> parent (from = the younger/dependent vertex, to = its older parent);
-- see ``docs/epistemy.md`` and ``types/edges.py``.
--
--   narrows                Issue → Issue       (narrower → broader)
--                          priority? (contextual edge priority)
--   requires               Issue → Issue       (requirer → prerequisite)
--   produced_by            Inquiry → Inquiry   (produced → producer)
--   proves                 Artifact → {Belief,Experiment}  (load-bearing)
--   favors                 Artifact → {Belief,Experiment}  (context)
--   supersedes             Inquiry → Inquiry   (successor → predecessor)
--
-- For-vs-against is not a separate edge kind: it is the sign of the
-- ``valence`` metadata column (positive supports the claim, negative argues
-- against it; magnitude is the evidential weight). ``proves`` is load-bearing
-- (votes in the proof predicate); ``favors`` is context.

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
        -- narrows: from = narrower (child), to = broader (parent).
        (edge_kind = 'narrows'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     -- requires: from = requirer (child), to = prerequisite (parent).
     OR (edge_kind = 'requires'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     -- produced_by: from = produced (younger child), to = producer (older
     -- parent), both any Inquiry. Provenance of origin, not containment; any
     -- inquiry can be produced by any other (a search produces the papers it
     -- surfaced; a broader Issue produces the narrower Issues it seeds). The
     -- first edge between two vertices infers this edge (younger produced_by
     -- older); see Inquiry.produced_by.
     OR (edge_kind = 'produced_by'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     -- proves/favors: from = citing Artifact (the younger evidence child),
     -- to = the cited claim (Belief or Experiment, the older parent).
     -- for-vs-against is the sign of the valence column, not a separate edge
     -- kind. proves is load-bearing; favors is context.
     OR (edge_kind IN ('proves', 'favors')
            AND from_kind IN ({artifact_kinds})
            AND to_kind IN ({claimable_kinds}))
     -- supersedes: from = successor (child), to = predecessor (parent). Both
     -- endpoints must be valid Inquiry kinds. Cross-kind supersession is allowed
     -- (a Paper can supersede an Artifact, etc.).
     OR (edge_kind = 'supersedes'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     -- cites_paper: from = citing Paper (younger child), to = cited Paper (older
     -- parent). A historical/bibliographic citation, distinct from the epistemic
     -- proves/favors; carries no valence and is provenance-neutral. Paper ->
     -- Paper only.
     OR (edge_kind = 'cites_paper'
            AND from_kind IN ({paper_kinds})
            AND to_kind IN ({paper_kinds}))
    )
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_id, edge_kind);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges (to_id, edge_kind);


-- Append-only audit log. Closed-set ``kind`` discriminates which per-column
-- ``(old_*, new_*)`` snapshot pairs may be populated. Milestone kinds carry no
-- deltas -- their signal is the row's existence.
CREATE TABLE IF NOT EXISTS change_log (
    -- -- Identity ------------------------------------------------------------
    id                 UUID PRIMARY KEY,
    -- ``clock_timestamp()`` is per-statement wall clock, not transaction
    -- start. Keeps ``what_changed_for_me``'s ``created > since`` cursor
    -- monotonic across concurrent transactions, and ensures multiple
    -- changes in one transaction are temporally ordered.
    created            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Auth v2 (Phase 2). ``api_key_id`` is the server-stamped
    -- ``api_keys.id`` of the credential used by the authenticated
    -- caller; nullable to keep pre-v2 rows and bootstrap-era emissions
    -- (``librarian`` cascade walks, ``StubEmbedder`` test writes)
    -- representable. The corresponding ``users.id`` is reachable via
    -- ``JOIN api_keys ON api_keys.id = change_log.api_key_id``.
    -- ``actor`` is the free-form audit string; formerly ``author`` and
    -- renamed by the migration block below. The FK to ``api_keys(id)``
    -- is added after ``api_keys`` is created (see the trailing ALTER
    -- TABLE block).
    api_key_id         UUID,
    actor              TEXT NOT NULL,
    -- ``subject_id`` is deliberately FK-free.
    subject_id         UUID NOT NULL,
    subject_kind       TEXT NOT NULL,
    kind               TEXT NOT NULL,
    caused_by          UUID REFERENCES change_log(id),
    reason             TEXT NOT NULL DEFAULT '',

    -- -- Deltas (one block per side, per :class:`Snapshot` field) -----------
    -- Edge-peer mirror block (paired with edge add/remove/annotation and
    -- dependency_changed events). Peer identity is structural; edge metadata
    -- mirrors are generated from Edge ColumnSpec metadata.
    old_peer_id             UUID,
    old_peer_kind           TEXT,
    old_peer_edge_kind      TEXT,
{edge_metadata_mirror_old}

    new_peer_id             UUID,
    new_peer_kind           TEXT,
    new_peer_edge_kind      TEXT,
{edge_metadata_mirror_new}

    -- Cost mirror block: always populated; monotonic across events.
    old_marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    old_marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    new_marginal_cost_agent_usd    NUMERIC(14, 6) NOT NULL DEFAULT 0,
    new_marginal_cost_resource_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,

    -- Generated per-column mirror block (one old_X/new_X pair plus the
    -- "populated iff" / value-shape CHECKs for every editable inquiries
    -- column). ``bootstrap()`` fills this placeholder from
    -- :func:`trackinizer._generate_change_log_mirror`; see there for
    -- the metadata source.
{change_log_mirror}

    -- Subscribers list captured at change-emit time. Lets
    -- ``what_changed_for_me`` and SSE fan-out route to who-was-subscribed-then
    -- without joining to ``inquiries`` (which would lose ``purged`` subjects).
    subscribers_snapshot           TEXT[] NOT NULL DEFAULT '{}',

    -- -- Constraints ---------------------------------------------------------
    -- Closed-set membership: list bodies come from custom_types Literal types.
    CHECK (subject_kind IN ({inquiry_kinds})),
    CHECK (kind IN ({change_kinds})),

    -- (Per-column "populated iff" CHECKs are generated above by
    -- _generate_change_log_mirror from ColumnSpec metadata.)

    -- Edge deltas populate peer metadata. Edge add/remove/annotation have
    -- separate event kinds; dependency_changed alerts parents that a child moved.
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

    -- (Closed-set value CHECKs on each old_X/new_X mirror column are
    -- generated above by _generate_change_log_mirror and
    -- _generate_edge_metadata_mirror.)
    CHECK (old_peer_kind IS NULL OR old_peer_kind IN ({inquiry_kinds})),
    CHECK (new_peer_kind IS NULL OR new_peer_kind IN ({inquiry_kinds})),
    CHECK (old_peer_edge_kind IS NULL OR old_peer_edge_kind IN ({edge_kinds})),
    CHECK (new_peer_edge_kind IS NULL OR new_peer_edge_kind IN ({edge_kinds})),

    -- Kind-specific changes must target the right ``subject_kind``. Catches
    -- direct-SQL bugs that the Store-side dispatch already rejects. The
    -- matrix is generated from ColumnSpec.applies_to_inquiry_kinds by
    -- _generate_change_log_kind_matrix.
{change_log_kind_matrix}
);

CREATE INDEX IF NOT EXISTS idx_change_log_subject_kind
    ON change_log (subject_id, kind, created);
CREATE INDEX IF NOT EXISTS idx_change_log_kind_created
    ON change_log (kind, created);
CREATE INDEX IF NOT EXISTS idx_change_log_created
    ON change_log (created);
-- Tie-breaker index for offset and cursor pagination.
CREATE INDEX IF NOT EXISTS idx_change_log_created_id
    ON change_log (created DESC, id DESC);
-- GIN over ``subscribers_snapshot`` keeps ``what_changed_for_me``
-- catch-up bounded by hits rather than total change_log size.
CREATE INDEX IF NOT EXISTS idx_change_log_subscribers_snapshot
    ON change_log USING gin (subscribers_snapshot);
-- Partial btree for ``what_changed_for_anyone`` (the subscriber-push scan,
-- one query per doorbell against this append-only table). Its predicate
-- (``!= '{}'``) is not GIN-indexable and ``cardinality(...) > 0`` is no
-- better (both EXPLAIN as Seq Scan); the WHERE here matches the query's
-- filter verbatim and the key matches its cursor ORDER BY, which planned
-- as a Bitmap Index Scan at 6% of the seq-scan cost (5k rows, PGlite).
CREATE INDEX IF NOT EXISTS idx_change_log_subscribed_created_id
    ON change_log (created, id)
    WHERE subscribers_snapshot != '{}';
CREATE INDEX IF NOT EXISTS idx_change_log_caused_by
    ON change_log (caused_by) WHERE caused_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_change_log_new_status
    ON change_log (new_status) WHERE new_status IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_change_log_new_judgement
    ON change_log (new_belief_judgement) WHERE new_belief_judgement IS NOT NULL;

-- Auth v2 (Phase 2) self-heal: rename pre-v2 ``change_log.author`` to
-- ``change_log.actor``. Postgres' ``ALTER TABLE ... RENAME COLUMN`` does
-- not support ``IF EXISTS`` on the column, so the DO block gates the
-- rename on a live information_schema probe. On fresh databases neither
-- column is present yet (``actor`` is created by the next ALTER), so
-- both arms of the IF are false and the block no-ops.
--
-- These auth-v2 column self-heals stay in the baseline (not a numbered
-- migration) because the baseline is the only step guaranteed to run before
-- EVERY numbered migration. Migration 007 joins on ``change_log.api_key_id``;
-- a legacy DB carrying the pre-rename ``principal_user_id`` must be repaired
-- before 007 runs, which only a baseline placement guarantees.
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

-- Belt-and-suspenders: covers an existing DB where the rename couldn't
-- run (both columns or neither column already present). NOT NULL with
-- ``DEFAULT ''`` keeps existing rows valid; new inserts always supply a
-- value via :meth:`Store.emit_change`.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT '';

-- Auth v2 self-heal: pre-rename databases shipped a
-- ``principal_user_id UUID REFERENCES users(id)`` column. Migrate
-- in place to ``api_key_id`` (FK to ``api_keys(id)``) without losing
-- recorded data. Runs *before* the ``ADD COLUMN IF NOT EXISTS
-- api_key_id`` below so a legacy DB doesn't end up with both columns.
-- Idempotent: every step is gated on the live schema.
DO $$
BEGIN
    -- Drop the legacy FK first; the rename below would carry it
    -- forward to ``api_key_id`` and point at the wrong table.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'change_log_principal_user_id_fkey'
    ) THEN
        ALTER TABLE change_log
            DROP CONSTRAINT change_log_principal_user_id_fkey;
    END IF;
    -- Rename the legacy column when it survived from a pre-rename DB.
    -- On a fresh database the inline ``api_key_id`` column already
    -- created by the ``CREATE TABLE IF NOT EXISTS change_log`` above
    -- means ``principal_user_id`` does not exist, so the rename
    -- no-ops; the trailing ``ADD COLUMN IF NOT EXISTS`` also no-ops.
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

-- Auth v2 (Phase 2): server-stamped credential column. Nullable to
-- preserve pre-v2 rows (and ``librarian`` cascade emissions that have
-- no human principal). The FK constraint to ``api_keys(id)`` is added
-- after ``api_keys`` is created -- see the trailing block below.
-- Belt-and-suspenders for any pre-rename DB whose rename block above
-- couldn't run (e.g. a paused migration that already saw both
-- columns); on a fresh DB the column was just created by the inline
-- declaration and this is a no-op.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS api_key_id UUID;


-- Auth v2 (Phase 1): users, API keys, allowlist. The browser OAuth path
-- and per-route role enforcement land in later phases; what's here is
-- the storage substrate plus the bearer-token middleware's lookup
-- target. See ``docs/auth_v2.md``.
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('viewer', 'writer', 'admin')),
    status      TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_login  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    secret_hash   TEXT NOT NULL,
    -- First N chars of the plaintext secret. Used both as a UI display
    -- ("trax_aBc...") and as a SELECT predicate so ``verify_secret``
    -- only scrypt-compares against rows sharing the prefix instead of
    -- scanning every live key.
    prefix        TEXT NOT NULL,
    -- Per-key role ceiling. Effective request role is
    -- min(users.role, api_keys.role) so a writer-user can mint a
    -- viewer-only token for a read-only agent. Named constraint so the
    -- self-heal block below can probe for it before re-adding.
    role          TEXT NOT NULL
                  CONSTRAINT api_keys_role_check
                  CHECK (role IN ('viewer', 'writer', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);

-- Auth v2 self-heal: ``api_keys.role`` was added after Phase 1 shipped.
-- On a fresh DB the CREATE TABLE above already supplies the column with
-- NOT NULL + CHECK, and the steps below are no-ops. On an existing DB
-- the column may be missing; add it nullable first so the backfill UPDATE
-- can run without violating NOT NULL, then promote. Stays in the baseline
-- (runs before every numbered migration) for the same ordering reason as
-- the change_log auth-v2 self-heals above.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS role TEXT;

DO $$
BEGIN
    -- Backfill from the owning user's role. ``min(user, key) = user``
    -- when key==user, so this preserves pre-rollout behaviour for every
    -- existing token.
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
    -- ``added_by`` is nullable to support the bootstrap-admin seed path:
    -- the first allowlist row is written by the server itself before
    -- any ``users`` row exists, so there is no principal to reference.
    -- Admin-driven inserts populate this column; bootstrap leaves NULL.
    added_by          UUID REFERENCES users(id) ON DELETE SET NULL,
    added_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Auth v2: link ``change_log.api_key_id`` to ``api_keys.id`` with
-- ``ON DELETE SET NULL``. Admin hard-delete of a user cascades through
-- ``api_keys.user_id`` (ON DELETE CASCADE) into the keys, and the
-- audit rows that referenced those keys survive with a NULL
-- ``api_key_id`` -- per-key linkage is lost but the history endures.
--
-- Idempotent: drops any existing FK by name (Phase 2 shipped this same
-- constraint with the default NO ACTION; we rewrite it here without a
-- new migration file because the constraint name is stable), then
-- re-adds with the SET NULL action. PostgreSQL has no
-- ``ALTER CONSTRAINT ... ON DELETE`` syntax for FKs, so drop+add is the
-- only path.
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

-- ============================================================================
-- agent_session_events: turn-grained agent-session capture (Phase 0).
--
-- Source of truth: types/agent_session_events.py (AgentSessionEvent). This
-- is a flat, fixed-shape table, so its DDL is written here directly rather
-- than generated from per-kind ColumnSpec metadata the way inquiries is.
--
-- Deliberately OUTSIDE ``inquiries``: an append-only turn log, not a
-- knowledge row. The owning session is the ``AgentSession`` artifact in
-- ``inquiries``; these rows hang off it by ``session_id``. Tenant scope is
-- derived by joining to inquiries (no denormalized org_id column).
--
-- ``PRIMARY KEY (session_id, seq)`` is the per-event dedup mechanism: the
-- harness assigns ``seq`` per session, and a retried batch
-- ``ON CONFLICT DO NOTHING``s. ``message`` is the typed turn content (a
-- ``Message`` member selected by ``kind``), stored as JSON; Postgres TOAST
-- absorbs the large ones, so there is no app-level blob offload.
--
-- Phase 0 is a plain Postgres table; it stays that way until the dedup key
-- is redesigned. A Timescale hypertable needs the partitioning column in
-- every unique index, which this PK excludes, so ``create_hypertable``
-- aborts (and PGlite cannot run it anyway). See docs/db_schema_migration.md.
CREATE TABLE IF NOT EXISTS agent_session_events (
    session_id   UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL CHECK (seq >= 0),
    model        TEXT,
    kind         TEXT NOT NULL CHECK (kind IN ({agent_session_event_kinds})),
    timestamp    TIMESTAMPTZ,
    created      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    message      JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (session_id, seq)
);

-- ``PRIMARY KEY (session_id, seq)`` already indexes ordered per-session
-- reads, so no separate (session_id, seq) index is needed.
CREATE INDEX IF NOT EXISTS idx_agent_session_events_kind
    ON agent_session_events (kind);

-- The cross-session console feed filters/orders by the composite key
-- ``(created, session_id, seq)`` (see ``Store.read_feed``); this index serves
-- that hot polling path so the feed need not scan+sort the whole event log.
CREATE INDEX IF NOT EXISTS idx_agent_session_events_created_session_seq
    ON agent_session_events (created, session_id, seq);

-- ============================================================================
-- experiment_metrics: step-grained metric time-series for Experiment runs
-- (the wandb ``log({key: value}, step=)`` analogue). Added in schema.011.sql;
-- mirrored here so a fresh install (which runs only this baseline and marks
-- the numbered migrations applied without executing them) gets the table.
--
-- Source of truth: types/experiment_metrics.py (ExperimentMetric). Like
-- ``agent_session_events``, a flat fixed-shape side table written directly
-- here rather than generated from per-kind ColumnSpec metadata.
--
-- Deliberately OUTSIDE ``inquiries``: append-only telemetry, not a knowledge
-- row. The owning experiment is the ``Experiment`` artifact in ``inquiries``;
-- these rows hang off it by ``experiment_id`` and carry no edges, cost,
-- supersession, or ``change_log`` audit (the same exemption
-- ``agent_session_events`` takes). Tenant scope is derived by joining to
-- inquiries.
--
-- ``PRIMARY KEY (experiment_id, key, step)`` is the per-point dedup mechanism
-- (a retried batch ``ON CONFLICT DO NOTHING``s) and the index serving both the
-- ordered per-(experiment, key) read and the latest-value-per-key roll-up
-- (``DISTINCT ON (key) ... ORDER BY key, step DESC``), so no summary column is
-- denormalized onto the Experiment row. ``value`` is a bare finite scalar and
-- ``kind`` is closed to 'scalar' today (both CHECKed below): widening ``kind``
-- to histogram / media points is a later migration that widens the wire
-- Literal, the CHECK, and the readers together -- media bytes never ride inline
-- here.
CREATE TABLE IF NOT EXISTS experiment_metrics (
    experiment_id UUID NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    -- Non-blank, length-capped: the wire ``MetricPoint.key`` is
    -- ``Field(min_length=1, max_length=512)`` plus a non-blank validator, and
    -- ``read_metrics`` reconstructs it, so a stored empty, whitespace-only, or
    -- over-long key would 500 the read. This CHECK backstops the wire like the
    -- step/value/kind CHECKs below -- the 512 bound matches
    -- ``wire_metrics._MAX_KEY_CHARS`` and ``btrim(key) <> ''`` matches the
    -- wire's blank rejection.
    key           TEXT NOT NULL CHECK (
        char_length(key) BETWEEN 1 AND 512 AND btrim(key) <> ''),
    step          BIGINT NOT NULL CHECK (step >= 0),
    -- Finite only: the wire ``MetricPoint.value`` is ``Field(allow_inf_nan=
    -- False)`` and ``read_metrics`` reconstructs it, so a stored NaN/±Inf would
    -- 500 the read (NaN/±Inf are valid float8 but not valid JSON numbers). This
    -- CHECK backstops the wire the way ``CHECK (step >= 0)`` backstops
    -- ``Field(ge=0)``. Postgres NaN/Inf are literals, not IEEE-unordered, so the
    -- guard is explicit ``<>`` against each, not ``value = value``.
    value         DOUBLE PRECISION NOT NULL CHECK (
        value <> 'NaN'::float8
        AND value <> 'Infinity'::float8
        AND value <> '-Infinity'::float8),
    -- Closed to 'scalar' today: the wire ``MetricPoint.kind`` is
    -- ``Literal["scalar"]`` and ``read_metrics`` reconstructs that Literal from
    -- this column, so a non-scalar row would 500 the read. This CHECK backstops
    -- the wire the way ``CHECK (step >= 0)`` backstops ``Field(ge=0)`` -- and
    -- mirrors the sibling ``agent_session_events.kind`` CHECK. Widening to a
    -- non-scalar kind is a later migration that widens wire + this CHECK + the
    -- readers together.
    kind          TEXT NOT NULL DEFAULT 'scalar' CHECK (kind = 'scalar'),
    timestamp     TIMESTAMPTZ,
    created       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (experiment_id, key, step)
);
