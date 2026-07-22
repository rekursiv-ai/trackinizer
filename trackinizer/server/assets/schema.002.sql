-- Migration 002: generalize produced_artifact to any-Inquiry producer.
--
-- Moves a database deployed before the producer generalization to the
-- current ``schema.sql`` shape. Numbered migrations run only on databases
-- that recorded the baseline before this file existed; a fresh database
-- applies the baseline (which already has the widened CHECK) and records
-- this name without executing it, so these statements never run twice.
--
-- Blast radius (the single object ``schema.sql`` now builds differently):
--   1. the ``edges`` table-level edge-validity CHECK: the
--      ``produced_artifact`` arm widened from ``from_kind = 'Issue'`` to
--      ``from_kind IN (<every inquiry kind>)``, so any inquiry (e.g. a
--      Belief) can produce an Artifact. The projection field rename
--      (``Issue.artifacts`` -> ``Inquiry.produces`` /
--      ``Artifact.issues`` -> ``Artifact.produced_by``) is pure read-side
--      Python; storage is unchanged, so no column/data migration is needed.
--
-- The edges edge-validity CHECK is an unnamed table-level constraint, so
-- Postgres auto-names it (``edges_check``/``edges_check1``/...). Discover it
-- by definition (it is the one enumerating ``produced_artifact``), not by a
-- guessed name, then drop and re-add with the widened arm. ``pg_get_constraintdef``
-- strips comments and normalizes whitespace, so the re-added def matches a
-- fresh database's CHECK exactly despite the baseline carrying inline comments.

DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'edges'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%produced_artifact%'
      AND pg_get_constraintdef(oid) LIKE '%from_kind = ''Issue''%';
    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
        ALTER TABLE edges ADD CHECK (
            (edge_kind = 'broader_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'blocked_by_issue'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'produced_artifact'
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
         OR (edge_kind = 'refuted_by_experiment'
                AND from_kind = 'Experiment' AND to_kind = 'Experiment')
        );
    END IF;
END $$;
