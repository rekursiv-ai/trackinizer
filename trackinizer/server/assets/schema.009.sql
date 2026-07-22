-- Migration 009: widen the ``produces`` edge from ``Inquiry -> Artifact`` to
-- ``Inquiry -> Inquiry``.
--
-- ``produces`` records provenance of origin. The to-side was restricted to the
-- Artifact branch, so an Issue could never be the *produced* row. The
-- first-edge provenance rule (the first edge between two vertices infers that
-- the older produced the younger; see ``types/inquiries.py::Inquiry.produces``)
-- must be able to stamp an Issue as produced, so the to-side now admits every
-- Inquiry kind. ``produced_by`` moves from ``Artifact`` to base ``Inquiry`` in
-- Python, but that is a read-side projection only -- no stored column changes,
-- so this migration is the single edges edge-validity CHECK rewrite.
--
-- A DB deployed at 008 carries the old CHECK whose ``produces`` arm reads
-- ``to_kind IN ({artifact_kinds})``. DROP-then-ADD re-renders the FULL current
-- edge-validity CHECK (the same body the baseline ``schema.sql`` emits), so
-- migrate-from-008 and fresh-from-009 reach catalog parity.
--
-- Bootstrap records each migration in ``applied_migrations`` and runs it once
-- per database, so this body needs no separate idempotency guard. The DROP
-- discriminator (the only edges CHECK carrying ``edge_kind = 'produces'`` plus
-- ``from_kind``/``to_kind``) matches the validity CHECK in BOTH the old and the
-- widened shape, so a manual replay still finds and replaces exactly one row.

DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- Drop the edge-validity CHECK, uniquely identified as the ``edges`` CHECK
    -- carrying the ``produces`` arm (the table's only multi-arm
    -- edge_kind/from_kind/to_kind CHECK; the sole other edges CHECK is
    -- ``from_id <> to_id``). Matches in both pre- and post-widen shapes.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'edges'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%edge_kind = ''produces''%'
          AND pg_get_constraintdef(oid) LIKE '%from_kind%'
          AND pg_get_constraintdef(oid) LIKE '%to_kind%'
    LOOP
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
    END LOOP;
    -- Re-add the full current edge-validity CHECK. The body mirrors the
    -- baseline ``schema.sql`` edges CHECK exactly; only the ``produces`` arm's
    -- to-side widens from ``{artifact_kinds}`` to ``{inquiry_kinds}``.
    ALTER TABLE edges ADD CHECK (
        (edge_kind = 'narrows_issue'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'blocks_issue'
            AND from_kind = 'Issue' AND to_kind = 'Issue')
     OR (edge_kind = 'produces'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind IN ('proves', 'disproves')
            AND from_kind = 'Belief'
            AND to_kind IN ({artifact_kinds}))
     OR (edge_kind IN ('favors', 'disfavors')
            AND from_kind IN ({artifact_kinds})
            AND to_kind = 'Belief')
     OR (edge_kind = 'supersedes'
            AND from_kind IN ({inquiry_kinds})
            AND to_kind IN ({inquiry_kinds}))
     OR (edge_kind = 'refutes_experiment'
            AND from_kind = 'Experiment' AND to_kind = 'Experiment')
    );
END $$;
