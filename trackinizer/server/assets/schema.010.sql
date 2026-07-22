-- Migration 010: recast the edge model around child -> parent provenance and
-- collapse citation polarity into a signed ``valence``.
--
-- See ``docs/epistemy.md`` and ``types/edges.py`` for the theory. Every edge is
-- stored child -> parent (from = younger/dependent, to = older parent). The
-- 009 edge set is transformed as follows:
--
--   * Metadata column ``relevance`` (CHECK 0..1) -> ``valence`` (CHECK -1..1).
--     A pre-existing 0..1 relevance reads as a positive valence; ``disproves`` /
--     ``disfavors`` rows are negated when collapsed (their polarity was the edge
--     KIND, not the column). A NULL relevance on a citation defaults to the
--     citation default (+0.5, negated to -0.5 for a dis* row): a citation never
--     carries a NULL valence. When one ordered pair held BOTH a for- and an
--     against-citation (legal under the old per-kind PK, colliding under the new
--     signed model), the higher-magnitude signed citation is kept and the loser
--     dropped (step 7b) before the swap.
--   * ``narrows_issue`` -> ``narrows`` (same direction: narrower -> broader).
--   * ``blocks_issue`` (from=blocker, to=blocked) -> ``requires`` (from=requirer,
--     to=prerequisite). The blocked issue REQUIRES the blocker, so the stored
--     endpoints SWAP: new from = old to (blocked/requirer), new to = old from
--     (blocker/prerequisite).
--   * ``produces`` (from=producer, to=produced) -> ``produced_by`` (from=produced,
--     to=producer). Endpoints SWAP.
--   * ``proves`` (from=Belief, to=Artifact) -> stays ``proves`` but now stored
--     Artifact -> Belief; endpoints SWAP.
--   * ``disproves`` -> ``proves`` with endpoints SWAPPED and valence negated.
--   * ``favors`` (from=Artifact, to=Belief) -> stays ``favors``, SAME direction
--     (Artifact -> Belief is already the new direction); no swap.
--   * ``disfavors`` -> ``favors`` (same direction, no swap) with valence negated.
--   * ``refutes_experiment`` rows are dropped (refutation now lives as a
--     negative-valence citation; no Belief endpoint exists to carry them).
--
-- Bootstrap records each migration in ``applied_migrations`` and runs it EXACTLY
-- ONCE per database; that ledger -- not the body -- is the idempotency guard.
-- The schema-shape steps (column renames, CONSTRAINT drops/adds) are written to
-- be self-skipping (column / constraint probes), but the DATA steps are NOT
-- body-idempotent: steps 6/7/8/9 unconditionally SWAP endpoints for a kind, so a
-- second run over already-migrated rows would re-swap and invert direction. This
-- is safe only because ``applied_migrations`` prevents a second run. A datadir
-- rebuild that drops ``applied_migrations`` while keeping ``edges`` MUST restore
-- the ledger too, or re-bootstrap will corrupt edge directions.

DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- 1. Rename relevance -> valence and widen its range CHECK.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'edges' AND column_name = 'relevance'
    ) THEN
        ALTER TABLE edges RENAME COLUMN relevance TO valence;
    END IF;
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'edges'::regclass AND contype = 'c'
          AND (pg_get_constraintdef(oid) LIKE '%relevance%'
               OR pg_get_constraintdef(oid) LIKE '%valence%')
          AND pg_get_constraintdef(oid) NOT LIKE '%edge_kind%'
    LOOP
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
    END LOOP;
    -- The valence range + citation-only CHECK (mirroring Edge.valence's
    -- ``applies_to_edge_kinds={'proves','favors'}``) is added in step 10b, AFTER
    -- the kind rewrites collapse disproves/disfavors into proves/favors. Adding
    -- it here would reject the still-present dis* citation rows that carry a
    -- valence; the rewrites in steps 8/9 keep every value within [-1, 1], so the
    -- column needs no interim guard.

    -- 2. change_log edge-relevance mirror columns -> edge-valence.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'old_edge_relevance'
    ) THEN
        ALTER TABLE change_log RENAME COLUMN old_edge_relevance TO old_edge_valence;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'change_log' AND column_name = 'new_edge_relevance'
    ) THEN
        ALTER TABLE change_log RENAME COLUMN new_edge_relevance TO new_edge_valence;
    END IF;
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND (pg_get_constraintdef(oid) LIKE '%edge_relevance%'
               OR pg_get_constraintdef(oid) LIKE '%edge_valence%')
          AND pg_get_constraintdef(oid) NOT LIKE '%peer_id%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
    -- The old/new edge-valence range + citation-only CHECKs are added in step
    -- 10b, after step 10 renames the disproves/disfavors peer kinds to
    -- proves/favors (the citation restriction would otherwise reject the still
    -- dis*-tagged audit rows that carry a valence).

    -- 3. Drop the edge-validity CHECK so the data transforms below (new kinds,
    --    swapped endpoints) are not rejected mid-flight. Re-added at step 11.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'edges'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%edge_kind%'
          AND pg_get_constraintdef(oid) LIKE '%from_kind%'
          AND pg_get_constraintdef(oid) LIKE '%to_kind%'
    LOOP
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
    END LOOP;

    -- 3b. Drop every change_log CHECK that names the peer edge kind NOW, before
    --     the step-10 peer_edge_kind value rewrites set new names the old enums
    --     (both the 9-kind membership enum and the priority-restriction CHECK
    --     keyed on the old Issue-to-Issue kinds) would reject. Both are re-added
    --     in their new 6-kind shape in step 10b. The co-occurrence CASE-WHEN
    --     CHECK also names peer kinds but is excluded via its ``peer_id``
    --     reference. ``pg_get_constraintdef`` renders ``IN (...)`` as
    --     ``= ANY (ARRAY[...])``, so match the bare column, not an ``IN`` token.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%peer_edge_kind%'
          AND pg_get_constraintdef(oid) NOT LIKE '%peer_id%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
END $$;

-- 4. Drop refutes_experiment edges (folded into citation valence).
DELETE FROM edges WHERE edge_kind = 'refutes_experiment';

-- 5. narrows_issue -> narrows (same direction).
UPDATE edges SET edge_kind = 'narrows' WHERE edge_kind = 'narrows_issue';

-- 6. blocks_issue -> requires, swapping endpoints (blocked requires blocker).
--    Swap from_*/to_* AND rename in one UPDATE.
UPDATE edges
   SET from_id = to_id, from_kind = to_kind,
       to_id = from_id, to_kind = from_kind,
       edge_kind = 'requires'
 WHERE edge_kind = 'blocks_issue';

-- 7. produces -> produced_by, swapping endpoints (produced -> producer).
UPDATE edges
   SET from_id = to_id, from_kind = to_kind,
       to_id = from_id, to_kind = from_kind,
       edge_kind = 'produced_by'
 WHERE edge_kind = 'produces';

-- 7b. Collapse colliding polarity pairs BEFORE the kind rewrites. Pre-009 the
--     PK (from_id, to_id, edge_kind) admitted a for- AND an against-citation
--     between one ordered pair (proves + disproves; favors + disfavors). Steps
--     8/9 fold polarity into the SIGN of valence and collapse the dis* kind into
--     its base kind, so both rows would land on one post-collapse PK -- a
--     duplicate-key error that aborts the whole deploy. Keep the
--     higher-magnitude signed citation per post-collapse pair and delete the
--     loser here, so steps 8/9 transform a unique survivor.
--
--     The post-collapse ordered pair differs by family: proves/disproves SWAP
--     endpoints (-> (to_id, from_id)), favors/disfavors do not (-> (from_id,
--     to_id)). The kept row is the one whose EFFECTIVE signed valence has the
--     greatest magnitude -- relevance defaulted to 0.5 when NULL, negated for a
--     dis* row (its polarity was the kind, not the column). ``ctid`` breaks an
--     exact magnitude tie so the choice is deterministic.
--
--     Policy note (irreversible): "higher magnitude wins" is a deliberate
--     conflict-resolution choice for the pre-009 data, NOT a value combination.
--     Two equal-and-opposite citations (e.g. proves 0.5 + disproves 0.5) keep one
--     side by ``ctid`` order -- the magnitudes do not net to 0. The collision is
--     vanishingly rare (it required a user to both prove and disprove the same
--     ordered pair under the old model). The LOSER's ``change_log`` history is
--     intentionally LEFT in place: those audit rows record an edge that genuinely
--     existed, so deleting them would falsify history. As a result the live
--     ``edges`` table is NOT byte-replayable from ``change_log`` across this one
--     collapse -- by design, since the audit log is a historical record, not a
--     live-state journal.
WITH ranked AS (
    SELECT ctid,
           CASE edge_kind
               WHEN 'disproves' THEN -COALESCE(valence, 0.5)
               WHEN 'disfavors' THEN -COALESCE(valence, 0.5)
               ELSE COALESCE(valence, 0.5)
           END AS signed_valence,
           CASE edge_kind
               WHEN 'proves'    THEN to_id
               WHEN 'disproves' THEN to_id
               ELSE from_id
           END AS pair_from,
           CASE edge_kind
               WHEN 'proves'    THEN from_id
               WHEN 'disproves' THEN from_id
               ELSE to_id
           END AS pair_to,
           CASE edge_kind
               WHEN 'proves'    THEN 'proves'
               WHEN 'disproves' THEN 'proves'
               ELSE 'favors'
           END AS target_kind
      FROM edges
     WHERE edge_kind IN ('proves', 'disproves', 'favors', 'disfavors')
),
losers AS (
    SELECT ctid FROM (
        SELECT ctid,
               row_number() OVER (
                   PARTITION BY pair_from, pair_to, target_kind
                   ORDER BY abs(signed_valence) DESC, ctid
               ) AS rn
          FROM ranked
    ) r
    WHERE rn > 1
)
DELETE FROM edges WHERE ctid IN (SELECT ctid FROM losers);

-- 8. proves: was Belief -> Artifact; now Artifact -> Belief. Swap endpoints.
--    disproves collapses into proves with swapped endpoints and negated valence.
--    A NULL relevance on EITHER kind defaults to the citation default (+0.5 for
--    proves, -0.5 for disproves): a citation never carries a NULL valence.
UPDATE edges
   SET from_id = to_id, from_kind = to_kind,
       to_id = from_id, to_kind = from_kind,
       valence = CASE
                   WHEN edge_kind = 'disproves' THEN -COALESCE(valence, 0.5)
                   ELSE COALESCE(valence, 0.5)
                 END,
       edge_kind = 'proves'
 WHERE edge_kind IN ('proves', 'disproves');

-- 9. favors: already Artifact -> Belief (the new direction); no swap. disfavors
--    collapses into favors with negated valence, same direction. A NULL
--    relevance defaults to the citation default like step 8.
UPDATE edges
   SET valence = CASE
                   WHEN edge_kind = 'disfavors' THEN -COALESCE(valence, 0.5)
                   ELSE COALESCE(valence, 0.5)
                 END,
       edge_kind = 'favors'
 WHERE edge_kind IN ('favors', 'disfavors');

-- 10. Rewrite historical change_log peer_edge_kind values to the new closed set
--     (the regenerated {edge_kinds} CHECK no longer admits the dropped kinds).
UPDATE change_log SET old_peer_edge_kind = 'narrows'      WHERE old_peer_edge_kind = 'narrows_issue';
UPDATE change_log SET new_peer_edge_kind = 'narrows'      WHERE new_peer_edge_kind = 'narrows_issue';
UPDATE change_log SET old_peer_edge_kind = 'requires'     WHERE old_peer_edge_kind = 'blocks_issue';
UPDATE change_log SET new_peer_edge_kind = 'requires'     WHERE new_peer_edge_kind = 'blocks_issue';
UPDATE change_log SET old_peer_edge_kind = 'produced_by'  WHERE old_peer_edge_kind = 'produces';
UPDATE change_log SET new_peer_edge_kind = 'produced_by'  WHERE new_peer_edge_kind = 'produces';
-- disproves/disfavors collapse into proves/favors with a NEGATED valence (the
-- edge data does the same in steps 8/9). Negate the mirrored audit valence in
-- the SAME statement as the kind rename, so the audit row never drifts from the
-- edge it records. A NULL mirror defaults to the citation default first (then
-- negates), so a citation audit row never claims a NULL valence either --
-- matching the live-edge COALESCE in steps 8/9.
UPDATE change_log
   SET old_peer_edge_kind = 'proves',
       old_edge_valence = -COALESCE(old_edge_valence, 0.5)
 WHERE old_peer_edge_kind = 'disproves';
UPDATE change_log
   SET new_peer_edge_kind = 'proves',
       new_edge_valence = -COALESCE(new_edge_valence, 0.5)
 WHERE new_peer_edge_kind = 'disproves';
UPDATE change_log
   SET old_peer_edge_kind = 'favors',
       old_edge_valence = -COALESCE(old_edge_valence, 0.5)
 WHERE old_peer_edge_kind = 'disfavors';
UPDATE change_log
   SET new_peer_edge_kind = 'favors',
       new_edge_valence = -COALESCE(new_edge_valence, 0.5)
 WHERE new_peer_edge_kind = 'disfavors';
-- Plain proves/favors audit rows with a NULL valence mirror coalesce to the
-- citation default, mirroring the live-edge COALESCE: a citation never claims a
-- NULL valence in the audit log. Runs after the dis* renames so every citation
-- audit row (renamed or already-proves/favors) is covered exactly once.
UPDATE change_log SET old_edge_valence = 0.5
 WHERE old_peer_edge_kind IN ('proves', 'favors') AND old_edge_valence IS NULL;
UPDATE change_log SET new_edge_valence = 0.5
 WHERE new_peer_edge_kind IN ('proves', 'favors') AND new_edge_valence IS NULL;
-- refutes_experiment peer rows: the edge is gone, so its audit rows have no
-- meaningful peer. Nulling the mirror would leave an edge_added/edge_removed row
-- violating the co-occurrence CHECK (which requires a non-null peer for those
-- kinds), so DELETE the rows instead. Sever any ``caused_by`` self-FK pointing
-- at them first (nullable; mirrors schema.005 arm D's websearch_results drop).
UPDATE change_log SET caused_by = NULL
    WHERE caused_by IN (
        SELECT id FROM change_log
        WHERE old_peer_edge_kind = 'refutes_experiment'
           OR new_peer_edge_kind = 'refutes_experiment');
DELETE FROM change_log
    WHERE old_peer_edge_kind = 'refutes_experiment'
       OR new_peer_edge_kind = 'refutes_experiment';

-- 10b. Re-render the edge-kind enum CHECKs that the baseline schema generates
--      from ``Edge.Kind``. The two change_log peer-edge-kind CHECKs (the 6-kind
--      membership enum and the priority-restriction CHECK) were dropped in step
--      3b so the step-10 value rewrites could run; re-add them in the new shape
--      here. The edges priority-restriction CHECK is not keyed on the peer kind
--      and was not pre-dropped, so it is drop-then-add here. Without these a
--      migrated DB keeps the stale lists and the catalog-parity gate fails.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- edges priority-restriction CHECK: priority allowed only on the two
    -- Issue-to-Issue kinds. Old: ('blocks_issue','narrows_issue'). Match the
    -- priority CHECK (priority + edge_kind) without the edge-VALIDITY CHECK
    -- (which carries from_kind); ``IN`` renders as ``= ANY``, so don't match it.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'edges'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%priority%'
          AND pg_get_constraintdef(oid) LIKE '%edge_kind%'
          AND pg_get_constraintdef(oid) NOT LIKE '%from_kind%'
    LOOP
        EXECUTE format('ALTER TABLE edges DROP CONSTRAINT %I', con_name);
    END LOOP;
    ALTER TABLE edges ADD CHECK (
        priority IS NULL OR priority >= 0 AND edge_kind IN ('narrows', 'requires'));

    -- change_log old/new priority-restriction CHECKs (dropped in step 3b).
    ALTER TABLE change_log ADD CHECK (
        old_edge_priority IS NULL
        OR old_edge_priority >= 0 AND old_peer_edge_kind IN ('narrows', 'requires'));
    ALTER TABLE change_log ADD CHECK (
        new_edge_priority IS NULL
        OR new_edge_priority >= 0 AND new_peer_edge_kind IN ('narrows', 'requires'));

    -- change_log old/new peer-edge-kind closed-set enums (dropped in step 3b).
    ALTER TABLE change_log ADD CHECK (
        old_peer_edge_kind IS NULL
        OR old_peer_edge_kind IN (
            'narrows', 'requires', 'produced_by', 'proves', 'favors', 'supersedes'));
    ALTER TABLE change_log ADD CHECK (
        new_peer_edge_kind IS NULL
        OR new_peer_edge_kind IN (
            'narrows', 'requires', 'produced_by', 'proves', 'favors', 'supersedes'));

    -- Valence is citation-only (Edge.valence's applies_to_edge_kinds): a
    -- non-NULL value is valid solely on a proves/favors edge. Added here, after
    -- the step-8/9/10 kind rewrites collapsed every dis* row into proves/favors,
    -- so no stale dis*-tagged valence row fails validation. Mirrors the
    -- baseline-generated CHECKs on edges.valence and the change_log mirrors.
    -- ``IN`` lists are written in the SAME order the baseline codegen emits
    -- (``_quote_values`` sorts: 'favors' before 'proves'); the catalog-parity
    -- gate compares constraint defs as strings, so a reordered ARRAY drifts.
    ALTER TABLE edges ADD CHECK (
        valence IS NULL
        OR (valence >= -1 AND valence <= 1) AND edge_kind IN ('favors', 'proves'));
    ALTER TABLE change_log ADD CHECK (
        old_edge_valence IS NULL
        OR (old_edge_valence >= -1 AND old_edge_valence <= 1)
           AND old_peer_edge_kind IN ('favors', 'proves'));
    ALTER TABLE change_log ADD CHECK (
        new_edge_valence IS NULL
        OR (new_edge_valence >= -1 AND new_edge_valence <= 1)
           AND new_peer_edge_kind IN ('favors', 'proves'));
END $$;

-- 11. Re-add the full current edge-validity CHECK (mirrors baseline schema.sql).
ALTER TABLE edges ADD CHECK (
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
);
