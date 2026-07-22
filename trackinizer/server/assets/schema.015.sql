-- Migration 015: add the cites_paper edge kind (historical Paper -> Paper
-- citation), distinct from the epistemic proves/favors.
--
-- Moves a database deployed before cites_paper existed to the current
-- ``schema.sql`` shape. Numbered migrations run only on databases that recorded
-- the baseline before this file existed; a fresh database applies the baseline
-- (which already admits cites_paper) and records this name without executing
-- it, so these statements never run twice.
--
-- Blast radius (the single object ``schema.sql`` now builds differently): the
-- ``edges`` table-level edge-validity CHECK gains one arm --
--   OR (edge_kind = 'cites_paper'
--          AND from_kind IN ('Paper') AND to_kind IN ('Paper'))
-- No new column, table, sequence, or per-kind constraint: cites_paper is an
-- edge KIND, not an Inquiry kind, and it carries no valence (so the existing
-- ``valence ... edge_kind IN ('favors', 'proves')`` CHECK is unchanged) and is
-- provenance-neutral (no data touched). The change_log peer-edge-kind CHECKs
-- enumerate {edge_kinds} generatively, so schema.sql already admits the new
-- kind there on a fresh DB -- but a pre-existing DB's two anonymous change_log
-- CHECKs (old_/new_peer_edge_kind) still list the old closed set, so widen them
-- too or an audit row naming a cites_paper peer would violate the check.
--
-- The edges edge-validity CHECK and the two change_log peer-edge-kind CHECKs are
-- unnamed table-level constraints, so Postgres auto-names them. Discover each by
-- its definition (not a guessed name), drop, and re-add widened.
-- ``pg_get_constraintdef`` strips comments and normalizes whitespace, so the
-- re-added defs match a fresh database's CHECKs exactly despite the baseline
-- carrying inline comments.

DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- 1. edges edge-validity CHECK: the arm-enumerating constraint that lists
    --    supersedes but not yet cites_paper.
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'edges'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%supersedes%'
      AND pg_get_constraintdef(oid) NOT LIKE '%cites_paper%';
    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
        ALTER TABLE edges ADD CHECK (
            (edge_kind = 'narrows'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'requires'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'produced_by'
                AND from_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                  'Belief', 'CodeChange', 'WebResult',
                                  'WebSearch', 'AgentSession')
                AND to_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                'Belief', 'CodeChange', 'WebResult',
                                'WebSearch', 'AgentSession'))
         OR (edge_kind IN ('proves', 'favors')
                AND from_kind IN ('Artifact', 'Experiment', 'Paper', 'Belief',
                                  'CodeChange', 'WebResult', 'WebSearch',
                                  'AgentSession')
                AND to_kind IN ('Belief', 'Experiment'))
         OR (edge_kind = 'supersedes'
                AND from_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                  'Belief', 'CodeChange', 'WebResult',
                                  'WebSearch', 'AgentSession')
                AND to_kind IN ('Issue', 'Artifact', 'Experiment', 'Paper',
                                'Belief', 'CodeChange', 'WebResult',
                                'WebSearch', 'AgentSession'))
         OR (edge_kind = 'cites_paper'
                AND from_kind IN ('Paper')
                AND to_kind IN ('Paper'))
        );
    END IF;

    -- 2. change_log old_peer_edge_kind CHECK: widen its closed edge-kind set.
    --    Match the pure-membership constraint (it enumerates 'supersedes'),
    --    NOT the old_edge_priority/old_edge_valence CHECKs that also mention
    --    old_peer_edge_kind but only list narrows/requires or favors/proves.
    -- pg_get_constraintdef renders ``IN (...)`` as ``= ANY (ARRAY[...])``, so
    -- match the column token plus 'supersedes' (present only in the membership
    -- enum, not the priority/valence CHECKs) rather than a literal ``IN``.
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'change_log'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%old_peer_edge_kind%'
      AND pg_get_constraintdef(oid) LIKE '%supersedes%'
      AND pg_get_constraintdef(oid) NOT LIKE '%cites_paper%';
    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
        ALTER TABLE change_log ADD CHECK (
            old_peer_edge_kind IS NULL OR old_peer_edge_kind IN (
                'narrows', 'requires', 'produced_by', 'proves', 'favors',
                'supersedes', 'cites_paper'
            )
        );
    END IF;

    -- 3. change_log new_peer_edge_kind CHECK: same widening.
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'change_log'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%new_peer_edge_kind%'
      AND pg_get_constraintdef(oid) LIKE '%supersedes%'
      AND pg_get_constraintdef(oid) NOT LIKE '%cites_paper%';
    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
        ALTER TABLE change_log ADD CHECK (
            new_peer_edge_kind IS NULL OR new_peer_edge_kind IN (
                'narrows', 'requires', 'produced_by', 'proves', 'favors',
                'supersedes', 'cites_paper'
            )
        );
    END IF;
END $$;
