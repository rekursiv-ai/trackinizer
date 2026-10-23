-- Canvas/workspace cutover. Stop every old writer before applying this migration.
-- Only owned structured fields change; user text and audit history stay untouched.
-- Bootstrap wraps this file and its ledger entry in one transaction.
-- Preflight every owned JSON path before rewriting. Bootstrap/psql -1 makes
-- all changes atomic, including the later label checks and DDL.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM visual_workspaces WHERE station ? 'workspace_id'
        OR (station->'suggestion') ? 'workspace_id')
        OR EXISTS (SELECT 1 FROM users WHERE preferences ? 'workspaces')
        OR EXISTS (SELECT 1 FROM visual_workspace_operations WHERE response ? 'workspace_id'
            OR (response->'suggestion') ? 'workspace_id') THEN
        RAISE EXCEPTION 'Canvas cutover: target JSON key already exists';
    END IF;
END;
$$;

UPDATE visual_workspaces SET station = (station - 'workstream_id') || jsonb_build_object('workspace_id', station->'workstream_id')
WHERE station ? 'workstream_id';
UPDATE visual_workspaces SET station = jsonb_set(station, '{suggestion}',
    ((station->'suggestion') - 'workstream_id') || jsonb_build_object('workspace_id', station->'suggestion'->'workstream_id'))
WHERE (station->'suggestion') ? 'workstream_id';
UPDATE users SET preferences = (preferences - 'workstreams') || jsonb_build_object('workspaces', preferences->'workstreams')
WHERE preferences ? 'workstreams';
UPDATE visual_workspace_operations SET response = (response - 'workstream_id') || jsonb_build_object('workspace_id', response->'workstream_id')
WHERE response ? 'workstream_id';
UPDATE visual_workspace_operations SET response = jsonb_set(response, '{suggestion}',
    ((response->'suggestion') - 'workstream_id') || jsonb_build_object('workspace_id', response->'suggestion'->'workstream_id'))
WHERE (response->'suggestion') ? 'workstream_id';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM inquiries WHERE kind = 'Issue' AND 'workspace' = ANY(labels))
        OR EXISTS (SELECT 1 FROM inquiries WHERE kind = 'AgentSession'
            AND EXISTS (SELECT 1 FROM unnest(labels) AS label
                WHERE label ~ '^canvas:[0-9a-fA-F-]{36}$')) THEN
        RAISE EXCEPTION 'Canvas cutover: target placement labels already exist';
    END IF;
END;
$$;
UPDATE inquiries SET labels = array_replace(labels, 'workstream', 'workspace')
WHERE kind = 'Issue' AND 'workstream' = ANY(labels);
UPDATE inquiries SET labels = ARRAY(
    SELECT CASE WHEN label ~ '^station:[0-9a-fA-F-]{36}$'
        THEN 'canvas:' || substring(label FROM 9) ELSE label END
    FROM unnest(labels) WITH ORDINALITY AS item(label, ordinal) ORDER BY ordinal
)
WHERE kind = 'AgentSession' AND EXISTS (
    SELECT 1 FROM unnest(labels) AS label WHERE label ~ '^station:[0-9a-fA-F-]{36}$'
);

ALTER TABLE visual_workspaces RENAME TO visual_canvases;
ALTER TABLE visual_canvases RENAME COLUMN station TO metadata;
ALTER TABLE visual_workspace_operations RENAME TO visual_canvas_operations;
ALTER TABLE visual_canvas_operations RENAME COLUMN workspace_id TO canvas_id;
ALTER TABLE users RENAME COLUMN visual_workspace_enabled TO visual_canvas_enabled;

ALTER TABLE visual_canvases RENAME CONSTRAINT visual_workspaces_pkey TO visual_canvases_pkey;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_workspaces_user_id_fkey TO visual_canvases_user_id_fkey;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_workspaces_revision_check TO visual_canvases_revision_check;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_workspaces_state_check TO visual_canvases_state_check;
ALTER TABLE visual_canvases RENAME CONSTRAINT visual_workspaces_station_check TO visual_canvases_metadata_check;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_workspace_operations_pkey TO visual_canvas_operations_pkey;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_workspace_operations_workspace_id_fkey TO visual_canvas_operations_canvas_id_fkey;
ALTER TABLE visual_canvas_operations RENAME CONSTRAINT visual_workspace_operations_response_check TO visual_canvas_operations_response_check;
ALTER INDEX uq_visual_workspaces_user_station_name RENAME TO uq_visual_canvases_user_metadata_name;
ALTER INDEX uq_visual_workspaces_user_station_chat RENAME TO uq_visual_canvases_user_metadata_chat;
ALTER INDEX idx_visual_workspaces_user_modified RENAME TO idx_visual_canvases_user_modified;

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
            AND (relation.relname IN ('visual_canvases', 'visual_canvas_operations')
                OR (relation.relname = 'users' AND attribute.attname = 'visual_canvas_enabled'))
    LOOP
        EXECUTE format('ALTER TABLE %I RENAME CONSTRAINT %I TO %I',
            item.relname, item.conname, item.relname || '_' || item.attname || '_not_null');
    END LOOP;
END;
$$;
