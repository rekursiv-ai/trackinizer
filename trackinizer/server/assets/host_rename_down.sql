-- Operator-only rollback of schema.076.sql: stop the new build first, run this with
-- psql -1, and start the old build only after it commits.
--
-- It renames hosts back to machines, the daemon's columns back to ``host_*``, each
-- constraint and index back to the name 075 gave it, and a host layer back to
-- ``machine``. No row is lost: the renames touch the catalog alone.

-- device_logins' ``host`` names the enrolled host, ``hostname`` the computer: the
-- first takes back ``machine``, then the second takes back ``host``.
DO $$
DECLARE
    found RECORD;
BEGIN
    IF to_regclass('hosts') IS NULL THEN
        RETURN;
    END IF;
    FOR found IN
        SELECT conname AS name FROM pg_constraint
        WHERE conrelid = 'device_logins'::regclass AND conname ~ '^device_logins_host_'
    LOOP
        EXECUTE format(
            'ALTER TABLE device_logins RENAME CONSTRAINT %I TO %I',
            found.name,
            regexp_replace(found.name, '^device_logins_host_', 'device_logins_machine_')
        );
    END LOOP;
    FOR found IN
        SELECT conname AS name FROM pg_constraint
        WHERE conrelid = 'device_logins'::regclass
          AND conname ~ '^device_logins_hostname_'
    LOOP
        EXECUTE format(
            'ALTER TABLE device_logins RENAME CONSTRAINT %I TO %I',
            found.name,
            regexp_replace(found.name, '^device_logins_hostname_', 'device_logins_host_')
        );
    END LOOP;
    FOR found IN
        SELECT con.conname AS name, rel.relname AS tab
        FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname IN (
            'hosts', 'host_enrollments', 'host_credentials', 'host_jobs', 'api_keys'
        ) AND con.conname ~ '(^|_)hosts?(_|$)'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            found.tab,
            found.name,
            replace(
                regexp_replace(
                    regexp_replace(found.name, '(^|_)hosts(_|$)', '\1machines\2', 'g'),
                    '(^|_)host(_|$)', '\1machine\2', 'g'
                ),
                'machines_daemon_', 'machines_host_'
            )
        );
    END LOOP;
    FOR found IN
        SELECT indexname AS name FROM pg_indexes
        WHERE tablename IN ('hosts', 'host_enrollments', 'host_credentials', 'host_jobs')
          AND indexname ~ '(^|_)hosts?(_|$)'
    LOOP
        EXECUTE format(
            'ALTER INDEX %I RENAME TO %I',
            found.name,
            regexp_replace(
                regexp_replace(found.name, '(^|_)hosts(_|$)', '\1machines\2', 'g'),
                '(^|_)host(_|$)', '\1machine\2', 'g'
            )
        );
    END LOOP;
END
$$;

ALTER TABLE device_logins RENAME COLUMN host TO machine;
ALTER TABLE device_logins RENAME COLUMN hostname TO host;
ALTER TABLE session_liveness RENAME COLUMN host TO machine;
ALTER TABLE api_keys RENAME COLUMN host_id TO machine_id;
ALTER TABLE host_jobs RENAME COLUMN host TO machine;
ALTER TABLE host_jobs RENAME COLUMN host_id TO machine_id;
ALTER TABLE host_jobs RENAME TO machine_jobs;
ALTER TABLE host_credentials RENAME COLUMN host_id TO machine_id;
ALTER TABLE host_credentials RENAME TO machine_credentials;
ALTER TABLE host_enrollments RENAME COLUMN host_id TO machine_id;
ALTER TABLE host_enrollments RENAME TO machine_enrollments;
ALTER TABLE hosts RENAME COLUMN daemon_instance TO host_instance;
ALTER TABLE hosts RENAME COLUMN daemon_version TO host_version;
ALTER TABLE hosts RENAME TO machines;

ALTER TABLE variables DROP CONSTRAINT variables_layer_check;
UPDATE variables SET layer = 'machine' WHERE layer = 'host';
ALTER TABLE variables
    ADD CONSTRAINT variables_layer_check CHECK (layer IN ('org', 'machine', 'user'));

DELETE FROM applied_migrations WHERE name = 'schema.076.sql';
