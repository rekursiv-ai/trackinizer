-- schema.042.sql -- a canvas is a named station, and a user has many of them.
-- A station's own fields are one optional JSON object, ``station``, that only the
-- workbench reads: its name, the science chat it holds (``conversation_id``) and the
-- page it was last on, each key present only when set. Trax's columns stay as they
-- were. The app opens the station changed last (``modified_at``). The
-- one-default-canvas rule goes (``is_default``), as does ``session_id``, which
-- nothing read. Saved views are stations without a chat now, so each preset becomes
-- one, and its table goes with it.
ALTER TABLE visual_workspaces
    ADD COLUMN station JSONB CHECK (jsonb_typeof(station) = 'object');
-- Every canvas so far is its user's one station. A user with a second row (never
-- made by the app) numbers after the default, oldest first, to keep names unique.
-- Naming it changes the row, so it is the newest of its user's stations.
UPDATE visual_workspaces AS station
SET station = jsonb_build_object('name', 'unnamed-workstream#' || numbered.position),
    modified_at = clock_timestamp()
FROM (
    SELECT id, row_number() OVER (
        PARTITION BY user_id ORDER BY is_default DESC, created_at, id
    ) AS position
    FROM visual_workspaces
) AS numbered
WHERE station.id = numbered.id;
-- A preset keeps its name; a taken one, however it came to be taken, gets " (2)",
-- " (3)" and so on, cut so the whole name stays within 120 characters. Its state is
-- the whole canvas state, so its instructions and continuation record come along.
-- It keeps the time it was saved, older than the canvases just named, so it never
-- becomes the station the app opens.
DO $$
DECLARE
    preset RECORD;
    candidate TEXT;
    attempt INTEGER;
BEGIN
    FOR preset IN
        SELECT id, user_id, name, state, created_at, modified_at
        FROM visual_workspace_presets
        ORDER BY created_at, id
    LOOP
        candidate := preset.name;
        attempt := 1;
        WHILE EXISTS (
            SELECT 1 FROM visual_workspaces
            WHERE user_id = preset.user_id AND station->>'name' = candidate
        ) LOOP
            attempt := attempt + 1;
            candidate := left(preset.name, 120 - length(' (' || attempt || ')'))
                || ' (' || attempt || ')';
        END LOOP;
        INSERT INTO visual_workspaces
            (id, user_id, is_default, station, state, created_at, modified_at)
        VALUES
            (gen_random_uuid(), preset.user_id, FALSE,
             jsonb_build_object('name', candidate), preset.state,
             preset.created_at, preset.modified_at);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_workspaces_user_station_name
    ON visual_workspaces (user_id, (station->>'name'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_visual_workspaces_user_station_chat
    ON visual_workspaces (user_id, (station->>'conversation_id'));
CREATE INDEX IF NOT EXISTS idx_visual_workspaces_user_modified
    ON visual_workspaces (user_id, modified_at DESC, id DESC);
DROP INDEX IF EXISTS uq_visual_workspaces_default_user;
ALTER TABLE visual_workspaces
    DROP COLUMN is_default,
    DROP COLUMN session_id;
DROP TABLE visual_workspace_presets;
