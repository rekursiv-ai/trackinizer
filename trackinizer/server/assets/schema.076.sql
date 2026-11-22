-- schema.076.sql -- a computer enrolled with the server is a "host", not a "machine", and
-- the process that connects it to the server is its "daemon":
--
--   machines              -> hosts (host_instance, host_version -> daemon_instance,
--                            daemon_version)
--   machine_enrollments   -> host_enrollments (machine_id -> host_id)
--   machine_credentials   -> host_credentials (machine_id -> host_id)
--   machine_jobs          -> host_jobs (machine_id, machine -> host_id, host)
--   api_keys.machine_id   -> host_id
--   session_liveness.machine -> host
--   device_logins.host, .machine -> hostname, host (the computer's own name, then the
--                            host it enrolls as)
--   variables.layer 'machine' -> 'host'
--
-- Renames only touch the catalog; the one data rewrite is the variables layer, a few
-- rows. Every step is guarded, so a database part-way through, or one a test rebuilt
-- from older files, reaches the baseline, and a second run changes nothing. Constraints
-- and indexes take their tables' new names, as a fresh install names them.
DO $$
BEGIN
    IF to_regclass('machines') IS NOT NULL AND to_regclass('hosts') IS NULL THEN
        ALTER TABLE machines RENAME TO hosts;
    END IF;
    IF to_regclass('machine_enrollments') IS NOT NULL
        AND to_regclass('host_enrollments') IS NULL THEN
        ALTER TABLE machine_enrollments RENAME TO host_enrollments;
    END IF;
    IF to_regclass('machine_credentials') IS NOT NULL
        AND to_regclass('host_credentials') IS NULL THEN
        ALTER TABLE machine_credentials RENAME TO host_credentials;
    END IF;
    IF to_regclass('machine_jobs') IS NOT NULL AND to_regclass('host_jobs') IS NULL THEN
        ALTER TABLE machine_jobs RENAME TO host_jobs;
    END IF;
END
$$;

-- device_logins' ``host`` goes first: ``machine`` takes its name.
DO $$
DECLARE
    each_rename TEXT[];
BEGIN
    FOREACH each_rename SLICE 1 IN ARRAY ARRAY[
        ['hosts', 'host_instance', 'daemon_instance'],
        ['hosts', 'host_version', 'daemon_version'],
        ['host_enrollments', 'machine_id', 'host_id'],
        ['host_credentials', 'machine_id', 'host_id'],
        ['host_jobs', 'machine_id', 'host_id'],
        ['host_jobs', 'machine', 'host'],
        ['api_keys', 'machine_id', 'host_id'],
        ['session_liveness', 'machine', 'host'],
        ['device_logins', 'host', 'hostname'],
        ['device_logins', 'machine', 'host']
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = each_rename[1] AND column_name = each_rename[2]
        ) AND NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = each_rename[1] AND column_name = each_rename[3]
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I RENAME COLUMN %I TO %I',
                each_rename[1], each_rename[2], each_rename[3]
            );
        END IF;
    END LOOP;
END
$$;

-- A constraint or index keeps the name its table and column had when it was made.
-- device_logins' old ``host`` and ``machine`` constraints swap names, so the first
-- move to ``hostname`` before the second take ``host``.
DO $$
DECLARE
    found RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname ~ '^device_logins_hostname_'
    ) THEN
        FOR found IN
            SELECT conname AS name FROM pg_constraint
            WHERE conname ~ '^device_logins_host_'
        LOOP
            EXECUTE format(
                'ALTER TABLE device_logins RENAME CONSTRAINT %I TO %I',
                found.name,
                regexp_replace(found.name, '^device_logins_host_', 'device_logins_hostname_')
            );
        END LOOP;
    END IF;
    FOR found IN
        SELECT con.conname AS name, rel.relname AS tab
        FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relnamespace = current_schema()::regnamespace
          AND (con.conname ~ 'machine' OR con.conname ~ '^hosts_host_(instance|version)')
    LOOP
        EXECUTE format(
            'ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            found.tab,
            found.name,
            replace(
                replace(replace(found.name, 'machines', 'hosts'), 'machine', 'host'),
                'hosts_host_',
                'hosts_daemon_'
            )
        );
    END LOOP;
    FOR found IN
        SELECT idx.relname AS name
        FROM pg_class idx
        WHERE idx.relkind = 'i'
          AND idx.relnamespace = current_schema()::regnamespace
          AND idx.relname ~ 'machine'
    LOOP
        EXECUTE format(
            'ALTER INDEX %I RENAME TO %I',
            found.name,
            replace(replace(found.name, 'machines', 'hosts'), 'machine', 'host')
        );
    END LOOP;
END
$$;

-- An agent launch's layer: the org's, a host's, or a person's.
ALTER TABLE variables DROP CONSTRAINT IF EXISTS variables_layer_check;
UPDATE variables SET layer = 'host' WHERE layer = 'machine';
ALTER TABLE variables
    ADD CONSTRAINT variables_layer_check CHECK (layer IN ('org', 'host', 'user'));
