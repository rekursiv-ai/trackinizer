-- Operator-only rollback: stop all new writers first. Run with psql -1.
-- Restore the old build only after this transaction succeeds.
-- Preflight every owned JSON path before rewriting. Bootstrap/psql -1 makes
-- all changes atomic, including the later label checks and DDL.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM visual_canvases WHERE metadata ? 'workstream_id'
        OR (metadata->'suggestion') ? 'workstream_id')
        OR EXISTS (SELECT 1 FROM users WHERE preferences ? 'workstreams')
        OR EXISTS (SELECT 1 FROM visual_canvas_operations WHERE response ? 'workstream_id'
            OR (response->'suggestion') ? 'workstream_id') THEN
        RAISE EXCEPTION 'Canvas cutover: target JSON key already exists';
    END IF;
END;
$$;

UPDATE visual_canvases SET metadata = (metadata - 'workspace_id') || jsonb_build_object('workstream_id', metadata->'workspace_id')
WHERE metadata ? 'workspace_id';
UPDATE visual_canvases SET metadata = jsonb_set(metadata, '{suggestion}',
    ((metadata->'suggestion') - 'workspace_id') || jsonb_build_object('workstream_id', metadata->'suggestion'->'workspace_id'))
WHERE (metadata->'suggestion') ? 'workspace_id';
UPDATE users SET preferences = (preferences - 'workspaces') || jsonb_build_object('workstreams', preferences->'workspaces')
WHERE preferences ? 'workspaces';
UPDATE visual_canvas_operations SET response = (response - 'workspace_id') || jsonb_build_object('workstream_id', response->'workspace_id')
WHERE response ? 'workspace_id';
UPDATE visual_canvas_operations SET response = jsonb_set(response, '{suggestion}',
    ((response->'suggestion') - 'workspace_id') || jsonb_build_object('workstream_id', response->'suggestion'->'workspace_id'))
WHERE (response->'suggestion') ? 'workspace_id';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM inquiries WHERE kind = 'Issue' AND 'workstream' = ANY(labels))
        OR EXISTS (SELECT 1 FROM inquiries WHERE kind = 'AgentSession'
            AND EXISTS (SELECT 1 FROM unnest(labels) AS label
                WHERE label ~ '^station:[0-9a-fA-F-]{36}$')) THEN
        RAISE EXCEPTION 'Canvas cutover: target placement labels already exist';
    END IF;
END;
$$;
UPDATE inquiries SET labels = array_replace(labels, 'workspace', 'workstream')
WHERE kind = 'Issue' AND 'workspace' = ANY(labels);
UPDATE inquiries SET labels = ARRAY(
    SELECT CASE WHEN label ~ '^canvas:[0-9a-fA-F-]{36}$'
        THEN 'station:' || substring(label FROM 8) ELSE label END
    FROM unnest(labels) WITH ORDINALITY AS item(label, ordinal) ORDER BY ordinal
)
WHERE kind = 'AgentSession' AND EXISTS (
    SELECT 1 FROM unnest(labels) AS label WHERE label ~ '^canvas:[0-9a-fA-F-]{36}$'
);

ALTER INDEX idx_visual_canvases_user_modified RENAME TO idx_visual_workspaces_user_modified;
ALTER INDEX uq_visual_canvases_user_metadata_chat RENAME TO uq_visual_workspaces_user_station_chat;
ALTER INDEX uq_visual_canvases_user_metadata_name RENAME TO uq_visual_workspaces_user_station_name;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_canvas_operations_response_check TO visual_workspace_operations_response_check;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_canvas_operations_canvas_id_fkey TO visual_workspace_operations_workspace_id_fkey;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_canvas_operations_pkey TO visual_workspace_operations_pkey;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_canvases_metadata_check TO visual_workspaces_station_check;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_canvases_state_check TO visual_workspaces_state_check;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_canvases_revision_check TO visual_workspaces_revision_check;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_canvases_user_id_fkey TO visual_workspaces_user_id_fkey;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_canvases_pkey TO visual_workspaces_pkey;
ALTER TABLE users RENAME COLUMN visual_canvas_enabled TO visual_workspace_enabled;
ALTER TABLE visual_canvas_operations RENAME COLUMN canvas_id TO workspace_id;
ALTER TABLE visual_canvas_operations RENAME TO visual_workspace_operations;
ALTER TABLE visual_canvases RENAME COLUMN metadata TO station;
ALTER TABLE visual_canvases RENAME TO visual_workspaces;

DELETE FROM applied_migrations WHERE name = 'schema.058.sql';
-- PostgreSQL 18 represents NOT NULL constraints in pg_constraint. Older engines
-- have no such rows. Derive each name from its renamed table and column.
DO $$
DECLARE item RECORD;
BEGIN
    FOR item IN
        SELECT relation.relname, con.conname, attribute.attname
        FROM pg_constraint AS con
        JOIN pg_class AS relation ON relation.oid = con.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
            AND attribute.attnum = con.conkey[1]
        WHERE namespace.nspname = 'public' AND con.contype = 'n'
            AND (relation.relname IN ('visual_workspaces', 'visual_workspace_operations')
                OR (relation.relname = 'users' AND attribute.attname = 'visual_workspace_enabled'))
    LOOP
        EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            item.relname, item.conname, item.relname || '_' || item.attname || '_not_null');
    END LOOP;
END;
$$;
