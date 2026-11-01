-- schema.065.sql -- Workspaces become the tab bar's groups, and each canvas a tab.
--
-- Data only; every step is a no-op on a second run. Rows each step changed on two
-- deployments, measured read-only on 2026-10-31 (first / second):
--
-- 1. A canvas made for a run, holding no chat, is closed: a new one starts closed
--    (session_placement.py), so a campaign's runs do not each open a tab. 14 / 3.
-- 2. A hidden default is folded into its canvases: the default holds the ungrouped
--    tabs and is never hidden (workspaces.py), so the canvases open in it close
--    and its id leaves the hidden list. 3 canvases, 1 person / 5, 3 people.
-- 3. Workspace pins are folded into the order, pinned first as the bar sorted them,
--    and the key is dropped: the strict preference model no longer takes it.
--    4 pins, 2 people / none.
-- 4. A canvas named ``unnamed-workstream#N``, as canvases were named before
--    schema.058, is named ``unnamed-canvas#M``, M counting up past its owner's
--    highest: the browser and the assistant treat only that name as unnamed, and a
--    tab shows its canvas's name. 30 / 38 open.
--
-- Canvases holding neither a chat nor a run stay open: each holds a page, and a
-- new tab with no session yet is a tab. ``modified_at`` stays, as for a canvas
-- moved into a promoted default (``_keep_loose_canvases``): the canvas changed last
-- is the one the app opens. Run it at startup with the new build, which refuses a
-- stored ``pinned``. ``tab_groups_down.sql`` is the operator's rollback.
UPDATE visual_canvases SET metadata = metadata || '{"hidden": true}'::jsonb
    WHERE metadata ? 'session_id' AND NOT metadata ? 'conversation_id'
        AND NOT coalesce((metadata->>'hidden')::boolean, FALSE);

WITH defaults AS (
    SELECT DISTINCT ON (owner) id, owner FROM inquiries
        WHERE kind = 'Issue' AND owner = account
            AND labels @> ARRAY['workspace', 'scratch']::text[]
        ORDER BY owner, seq
), hidden AS (
    SELECT users.id AS user_id, defaults.id::text AS workspace_id
        FROM users JOIN defaults ON defaults.owner = users.email
        WHERE users.preferences->'workspaces'->'hidden' ? defaults.id::text
), closed AS (
    UPDATE visual_canvases AS canvas
        SET metadata = canvas.metadata || '{"hidden": true}'::jsonb
        FROM hidden
        WHERE canvas.user_id = hidden.user_id
            AND coalesce(canvas.metadata->>'workspace_id', hidden.workspace_id)
                = hidden.workspace_id
            AND NOT coalesce((canvas.metadata->>'hidden')::boolean, FALSE)
)
UPDATE users SET preferences = jsonb_set(preferences, '{workspaces,hidden}',
        (preferences->'workspaces'->'hidden') - hidden.workspace_id)
    FROM hidden
    WHERE users.id = hidden.user_id;

DO $$
DECLARE
    person RECORD;
BEGIN
    FOR person IN
        SELECT id, preferences->'workspaces' AS bar FROM users
            WHERE preferences->'workspaces' ? 'pinned'
    LOOP
        UPDATE users SET preferences = jsonb_set(preferences, '{workspaces}',
            (person.bar - 'pinned') || jsonb_build_object('order', (
                SELECT coalesce(
                    jsonb_agg(entry.id ORDER BY entry.pinned DESC, entry.place,
                              entry.seq),
                    '[]'::jsonb)
                FROM (
                    SELECT kept.value AS id,
                           person.bar->'pinned' ? kept.value AS pinned,
                           kept.place, 0::bigint AS seq
                        FROM jsonb_array_elements_text(
                                coalesce(person.bar->'order', '[]'::jsonb))
                            WITH ORDINALITY AS kept(value, place)
                    UNION ALL
                    -- A pin the saved order did not hold came after those it did,
                    -- in the order its Issue was made.
                    SELECT pin.value, TRUE, 9223372036854775807, issue.seq
                        FROM jsonb_array_elements_text(person.bar->'pinned')
                            AS pin(value)
                        JOIN inquiries AS issue ON issue.id::text = pin.value
                        WHERE NOT coalesce(person.bar->'order', '[]'::jsonb)
                            ? pin.value
                ) AS entry)))
            WHERE id = person.id;
    END LOOP;
END;
$$;

WITH highest AS (
    SELECT user_id,
           max(substring(metadata->>'name' FROM '^unnamed-canvas#([0-9]+)$')::bigint)
               AS n
        FROM visual_canvases GROUP BY user_id
), legacy AS (
    SELECT canvas.id,
           coalesce(highest.n, 0)
               + row_number() OVER (PARTITION BY canvas.user_id
                                    ORDER BY canvas.created_at, canvas.id) AS n
        FROM visual_canvases AS canvas LEFT JOIN highest USING (user_id)
        WHERE canvas.metadata->>'name' ~ '^unnamed-workstream#[0-9]+$'
)
UPDATE visual_canvases AS canvas
    SET metadata = canvas.metadata
        || jsonb_build_object('name', 'unnamed-canvas#' || legacy.n)
    FROM legacy
    WHERE canvas.id = legacy.id;
