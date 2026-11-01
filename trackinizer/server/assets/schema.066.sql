-- schema.066.sql -- AccessGroup is a kind: a shared group of readers and writers, as a
-- graph row. Its controls name groups by id and its create and control moves are
-- audited like any field edit, so the change log gains both control kinds and their
-- old/new mirrors. It cites no claim, so ``proves``/``favors`` keep their citing set.
--
-- The constraints below are the ones the baseline produces for the ``AccessGroup``
-- specs; ``schema_migration_test`` proves the two agree. The kind lists come from the
-- same placeholders the baseline renders. Each new column's CHECKs ride on the column
-- they guard, added only when the column is absent, so a replay changes nothing.
-- Additive: no existing row is an AccessGroup, and every row passes each CHECK, so
-- this is safe to apply before the restart.
CREATE SEQUENCE IF NOT EXISTS seq_accessgroup;

DO $columns$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'inquiries'
          AND column_name = 'accessgroup_read_group'
    ) THEN
        ALTER TABLE inquiries
            ADD COLUMN accessgroup_read_group UUID CHECK (
                CASE WHEN kind = 'AccessGroup'
                    THEN accessgroup_read_group IS NOT NULL
                    ELSE accessgroup_read_group IS NULL
                END
            ),
            ADD COLUMN accessgroup_write_group UUID CHECK (
                CASE WHEN kind = 'AccessGroup'
                    THEN accessgroup_write_group IS NOT NULL
                    ELSE accessgroup_write_group IS NULL
                END
            ),
            ADD COLUMN accessgroup_system BOOLEAN CHECK (
                CASE WHEN kind = 'AccessGroup'
                    THEN accessgroup_system IS NOT NULL
                    ELSE accessgroup_system IS NULL
                END
            ),
            ADD COLUMN accessgroup_domain TEXT CHECK (
                CASE WHEN kind = 'AccessGroup'
                    THEN TRUE
                 AND (accessgroup_domain IS NULL OR accessgroup_domain = lower(accessgroup_domain) AND accessgroup_system)
                    ELSE accessgroup_domain IS NULL
                END
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'change_log'
          AND column_name = 'old_accessgroup_read_group'
    ) THEN
        ALTER TABLE change_log
            ADD COLUMN old_accessgroup_read_group UUID
                CHECK ((old_accessgroup_read_group IS NOT NULL) = (kind = 'accessgroup_read_group'))
                CHECK (kind <> 'accessgroup_read_group' OR subject_kind = 'AccessGroup'),
            ADD COLUMN new_accessgroup_read_group UUID
                CHECK ((new_accessgroup_read_group IS NOT NULL) = (kind = 'accessgroup_read_group')),
            ADD COLUMN old_accessgroup_write_group UUID
                CHECK ((old_accessgroup_write_group IS NOT NULL) = (kind = 'accessgroup_write_group'))
                CHECK (kind <> 'accessgroup_write_group' OR subject_kind = 'AccessGroup'),
            ADD COLUMN new_accessgroup_write_group UUID
                CHECK ((new_accessgroup_write_group IS NOT NULL) = (kind = 'accessgroup_write_group'));
    END IF;
END $columns$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_inquiries_accessgroup_domain
    ON inquiries (accessgroup_domain)
    WHERE accessgroup_domain IS NOT NULL;

ALTER TABLE inquiries
    DROP CONSTRAINT inquiries_kind_check,
    ADD CONSTRAINT inquiries_kind_check CHECK (kind IN ({inquiry_kinds}));

ALTER TABLE change_log
    DROP CONSTRAINT change_log_subject_kind_check,
    DROP CONSTRAINT change_log_kind_check,
    DROP CONSTRAINT change_log_old_peer_kind_check,
    DROP CONSTRAINT change_log_new_peer_kind_check,
    ADD CONSTRAINT change_log_subject_kind_check
        CHECK (subject_kind IN ({inquiry_kinds})),
    ADD CONSTRAINT change_log_kind_check CHECK (kind IN ({change_kinds})),
    ADD CONSTRAINT change_log_old_peer_kind_check
        CHECK (old_peer_kind IS NULL OR old_peer_kind IN ({inquiry_kinds})),
    ADD CONSTRAINT change_log_new_peer_kind_check
        CHECK (new_peer_kind IS NULL OR new_peer_kind IN ({inquiry_kinds}));

-- The baseline leaves the edge topology CHECK unnamed, so it is found by what it says
-- and rebuilt under its own name.
DO $edges$
DECLARE
    topology text;
BEGIN
    SELECT conname INTO STRICT topology FROM pg_constraint
    WHERE conrelid = 'edges'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%from_kind%';
    EXECUTE format($alter$
        ALTER TABLE edges DROP CONSTRAINT %1$I, ADD CONSTRAINT %1$I CHECK (
            (edge_kind = 'narrows'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'requires'
                AND from_kind = 'Issue' AND to_kind = 'Issue')
         OR (edge_kind = 'produced_by'
                AND from_kind IN ({inquiry_kinds})
                AND to_kind IN ({inquiry_kinds}))
         OR (edge_kind IN ('proves', 'favors')
                AND from_kind IN ({evidence_kinds})
                AND to_kind IN ({claimable_kinds}))
         OR (edge_kind = 'supersedes'
                AND from_kind IN ({inquiry_kinds})
                AND to_kind IN ({inquiry_kinds}))
         OR (edge_kind = 'cites_paper'
                AND from_kind IN ({paper_kinds})
                AND to_kind IN ({paper_kinds}))
        )
    $alter$, topology);
END $edges$;
