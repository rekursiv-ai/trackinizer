-- Migration 004: rename four edge kinds for naming consistency.
--
-- Moves a database deployed before the edge-kind rename to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that
-- recorded the baseline before this file existed; a fresh database applies
-- the baseline (which already uses the new names) and records this name
-- without executing it, so these statements never run twice.
--
-- The rule: Edge.Kind is a present-active verb (from-side acts on to-side),
-- suffixed ``_<kind>`` iff the to-side is a single fixed kind. Four kinds
-- violated it and are renamed:
--   produced_artifact     -> produces            (drop suffix; to-side any Artifact)
--   broader_issue         -> narrows_issue        (adjectival->verb; from=narrower)
--   refuted_by_experiment -> refutes_experiment  (passive->active; DIRECTION FLIP)
--   blocked_by_issue      -> blocks_issue         (passive->active; DIRECTION FLIP)
--
-- ``produces`` and ``narrows_issue`` keep their stored direction; only the kind
-- name changes. ``refutes_experiment`` and ``blocks_issue`` go passive->active,
-- which inverts subject and object, so each rewrite must ALSO swap from_id <->
-- to_id on every edge: refutes_experiment becomes from=refuter,to=refuted and
-- blocks_issue becomes from=blocker,to=blocked. The change_log audit rows are
-- symmetric historical facts (recorded on both endpoints), so they need only
-- the peer_edge_kind NAME updated -- never a direction swap.
--
-- Ordering: every affected CHECK enumerates the kinds literally, so the data
-- rewrite and the CHECK swap cannot both be valid while the other is in place.
-- Drop the CHECK first, rewrite the data unconstrained, then add the renamed
-- CHECK. Each CHECK is unnamed (Postgres auto-names it); discover by an
-- old-name token so the whole migration is a no-op on a DB already carrying
-- the new names (and never duplicates a CHECK on a fresh DB).
-- ``pg_get_constraintdef`` strips comments and normalizes whitespace, so the
-- re-added defs match a fresh database exactly.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'edges'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%produced_artifact%';
    IF con_name IS NOT NULL THEN
        -- 1. Drop the edge-validity CHECK so the data rewrite is unconstrained.
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);

        -- 2. Rewrite the stored edge_kind values. The PRIMARY KEY is
        --    (from_id, to_id, edge_kind); a pure rename cannot collide because
        --    no row pairs the same endpoints under both an old and a new name.
        UPDATE edges SET edge_kind = 'produces'
            WHERE edge_kind = 'produced_artifact';
        UPDATE edges SET edge_kind = 'narrows_issue'
            WHERE edge_kind = 'broader_issue';
        -- refutes_experiment and blocks_issue went passive->active, which
        -- inverts subject/object, so each rewrite ALSO swaps from_id <-> to_id
        -- (both endpoints are the same kind, so the kind-column swap is a no-op
        -- but written for correctness). refutes_experiment: from=refuter,
        -- to=refuted (was from=refuted, to=refuter).
        UPDATE edges
        SET from_id = to_id, to_id = from_id,
            from_kind = to_kind, to_kind = from_kind,
            edge_kind = 'refutes_experiment'
        WHERE edge_kind = 'refuted_by_experiment';
        -- blocks_issue: from=blocker, to=blocked (was from=blocked, to=blocker).
        UPDATE edges
        SET from_id = to_id, to_id = from_id,
            from_kind = to_kind, to_kind = from_kind,
            edge_kind = 'blocks_issue'
        WHERE edge_kind = 'blocked_by_issue';

        -- 3. Add the renamed edge-validity CHECK (matches the fresh baseline).
        --    The kind-list orderings below (and every IN-list in this file) must
        --    match the canonical order in types/edges.py ``Edge.Kind`` /
        --    ``Inquiry.InquiryKind`` / ``Artifact.Kind`` -- ``quote_literal``
        --    renders them in Literal-declaration order (not sorted), and
        --    ``pg_get_constraintdef`` compares textually, so reordering a
        --    Literal member would make this migration's def disagree with the
        --    fresh one. The migration parity test catches any such drift.
        ALTER TABLE edges ADD CHECK (
            (edge_kind = 'narrows_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'blocks_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'produces'
                AND from_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                  'Belief', 'CodeChange', 'WebResult',
                                  'WebSearch', 'AgentSession')
                AND to_kind IN ('Artifact', 'Experiment', 'Paper', 'Belief',
                                'CodeChange', 'WebResult', 'WebSearch',
                                'AgentSession'))
         OR (edge_kind IN ('proves', 'disproves', 'favors', 'disfavors')
                AND from_kind = 'Belief'
                AND to_kind IN ('Artifact', 'Experiment', 'Paper', 'Belief',
                                'CodeChange', 'WebResult', 'WebSearch',
                                'AgentSession'))
         OR (edge_kind = 'supersedes'
                AND from_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                  'Belief', 'CodeChange', 'WebResult',
                                  'WebSearch', 'AgentSession')
                AND to_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                'Belief', 'CodeChange', 'WebResult',
                                'WebSearch', 'AgentSession'))
         OR (edge_kind = 'refutes_experiment'
                AND from_kind = 'Experiment' AND to_kind = 'Experiment')
        );
    END IF;

    -- 4. The edge-kind names also appear in the edges PRIORITY CHECK and in
    --    four change_log peer-kind CHECKs (old/new x priority-restricted +
    --    full-enum), plus as stored values in change_log.{old,new}_peer_edge_kind.
    --    Same ordering hazard: drop every affected CHECK first, rewrite the
    --    data unconstrained, then re-add the renamed CHECKs. Discover by an
    --    old-name token so the block is a no-op on a DB already on the new
    --    names (and never duplicates a CHECK on a fresh DB).
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid IN ('edges'::regclass, 'change_log'::regclass)
          AND contype = 'c'
          AND pg_get_constraintdef(oid) NOT LIKE '%from_kind%'
          AND (pg_get_constraintdef(oid) LIKE '%broader_issue%'
               OR pg_get_constraintdef(oid) LIKE '%produced_artifact%'
               OR pg_get_constraintdef(oid) LIKE '%blocked_by_issue%')
    ) THEN
        -- 4a. Drop the edges PRIORITY CHECK (edges CHECK with no from_kind).
        FOR con_name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'edges'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) NOT LIKE '%from_kind%'
              AND pg_get_constraintdef(oid) LIKE '%broader_issue%'
        LOOP
            EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
        END LOOP;
        -- 4b. Drop the four change_log peer-kind CHECKs.
        FOR con_name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'change_log'::regclass AND contype = 'c'
              AND (pg_get_constraintdef(oid) LIKE '%broader_issue%'
                   OR pg_get_constraintdef(oid) LIKE '%produced_artifact%')
        LOOP
            EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
        END LOOP;

        -- 4c. Rewrite the stored change_log peer-kind values (CHECKs gone).
        --     Pure rename, including blocks_issue (audit rows are symmetric, so
        --     no direction swap is needed here -- only on the live edges above).
        UPDATE change_log SET old_peer_edge_kind = 'produces'
            WHERE old_peer_edge_kind = 'produced_artifact';
        UPDATE change_log SET old_peer_edge_kind = 'refutes_experiment'
            WHERE old_peer_edge_kind = 'refuted_by_experiment';
        UPDATE change_log SET old_peer_edge_kind = 'narrows_issue'
            WHERE old_peer_edge_kind = 'broader_issue';
        UPDATE change_log SET old_peer_edge_kind = 'blocks_issue'
            WHERE old_peer_edge_kind = 'blocked_by_issue';
        UPDATE change_log SET new_peer_edge_kind = 'produces'
            WHERE new_peer_edge_kind = 'produced_artifact';
        UPDATE change_log SET new_peer_edge_kind = 'refutes_experiment'
            WHERE new_peer_edge_kind = 'refuted_by_experiment';
        UPDATE change_log SET new_peer_edge_kind = 'narrows_issue'
            WHERE new_peer_edge_kind = 'broader_issue';
        UPDATE change_log SET new_peer_edge_kind = 'blocks_issue'
            WHERE new_peer_edge_kind = 'blocked_by_issue';

        -- 4d. Re-add the renamed CHECKs (match the fresh baseline defs).
        ALTER TABLE edges ADD CHECK (
            priority IS NULL OR priority >= 0
            AND edge_kind IN ('blocks_issue', 'narrows_issue')
        );
        ALTER TABLE change_log ADD CHECK (
            old_edge_priority IS NULL OR old_edge_priority >= 0
            AND old_peer_edge_kind IN ('blocks_issue', 'narrows_issue')
        );
        ALTER TABLE change_log ADD CHECK (
            new_edge_priority IS NULL OR new_edge_priority >= 0
            AND new_peer_edge_kind IN ('blocks_issue', 'narrows_issue')
        );
        ALTER TABLE change_log ADD CHECK (
            old_peer_edge_kind IS NULL OR old_peer_edge_kind IN (
                'narrows_issue', 'blocks_issue', 'produces', 'proves',
                'disproves', 'favors', 'disfavors', 'supersedes',
                'refutes_experiment')
        );
        ALTER TABLE change_log ADD CHECK (
            new_peer_edge_kind IS NULL OR new_peer_edge_kind IN (
                'narrows_issue', 'blocks_issue', 'produces', 'proves',
                'disproves', 'favors', 'disfavors', 'supersedes',
                'refutes_experiment')
        );
    END IF;
END $$;
