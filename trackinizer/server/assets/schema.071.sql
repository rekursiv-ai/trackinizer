-- schema.071.sql -- a row can be ``stopped``: its work was ended from outside before it
-- finished, by a budget or a usage limit. ``complete`` stays for finished work and
-- ``abandoned`` for work a person gave up. The status CHECK on ``inquiries`` and its
-- old/new mirror on ``change_log`` each gain the value.
--
-- The constraints below are the ones the baseline produces for the ``Inquiry.status``
-- spec; ``schema_migration_test`` proves the two agree. The baseline leaves the
-- change-log ones unnamed, so they are found by what they say and rebuilt under their
-- own names, and a replay changes nothing. Widening only: every existing row passes
-- each CHECK and the old build never writes ``stopped``, so this is safe to apply
-- before the restart. One ALTER per table, so each table is scanned once to validate
-- its new CHECKs.
ALTER TABLE inquiries
    DROP CONSTRAINT inquiries_status_check,
    ADD CONSTRAINT inquiries_status_check
        CHECK (status IN ('active', 'complete', 'stopped', 'abandoned', 'invalid'));

DO $change_log$
DECLARE
    old_check text;
    new_check text;
BEGIN
    SELECT conname INTO STRICT old_check FROM pg_constraint
    WHERE conrelid = 'change_log'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%(old_status IS NULL)%''abandoned''%';
    SELECT conname INTO STRICT new_check FROM pg_constraint
    WHERE conrelid = 'change_log'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%(new_status IS NULL)%''abandoned''%';
    EXECUTE format($alter$
        ALTER TABLE change_log
            DROP CONSTRAINT %1$I,
            DROP CONSTRAINT %2$I,
            ADD CONSTRAINT %1$I CHECK (old_status IS NULL
                OR old_status IN ('active', 'complete', 'stopped', 'abandoned', 'invalid')),
            ADD CONSTRAINT %2$I CHECK (new_status IS NULL
                OR new_status IN ('active', 'complete', 'stopped', 'abandoned', 'invalid'))
    $alter$, old_check, new_check);
END $change_log$;
