-- schema.072.sql -- every stored canvas becomes a panel tree (``state.panels``,
-- ``server/private/canvas_panels.py``) in place of its visuals, focused instance and
-- canvas tabs (``state.visuals``, ``state.focused_instance``, ``metadata.layout.tabs``).
--
-- SIZE, measured read-only on two deployments on 2026-11-07: 154 canvases on the
-- first and 80 on the second; the largest layout 1,814 bytes; at most 12 tabs and 21
-- kept visuals on one canvas.
-- One scan of ``visual_canvases`` and one of ``visual_canvas_operations`` (at most 64
-- receipts a canvas), plus one primary-key lookup in ``inquiries`` per record tile:
-- well under a second, so it runs at startup, in bootstrap's one transaction.
--
-- Data only: no DDL survives it. The two helper functions it makes are dropped at its
-- end, so a migrated database's catalog equals a fresh one's.
--
-- DESTRUCTIVE: the old shape is gone afterwards, and the old build cannot read the
-- new one, so the old server must not serve during it or after it. There is no
-- rollback. Only rows still of the old shape (``state ? 'visuals'``) are rewritten,
-- so a second run changes no canvas.
--
-- The mapping, for each canvas:
--
-- | Old                                          | New                                 |
-- |----------------------------------------------|-------------------------------------|
-- | ``metadata.layout.tabs``, else one tab       | The old tabs, in order              |
-- | standing for the canvas                      |                                     |
-- | ``layout.tab`` if it names a tab, else the   | The open old tab                    |
-- | first                                        |                                     |
-- | The open tab's content                       | ``state.visuals``, and              |
-- |                                              | ``metadata.page`` when the page     |
-- |                                              | visual (``trax.browse``) is among   |
-- |                                              | them                                |
-- | Any other tab's content                      | Its own ``page`` and ``visuals``    |
-- | A tab's page (not ``#/``), and each ``main`` | A tab of the main panel, in old tab |
-- | visual                                       | order, the page first               |
-- | A ``left``, ``side`` or ``floating`` visual  | A tab of a panel on the right: a    |
-- |                                              | row split, shares ``[0.6, 0.4]``    |
-- | A terminal (``trax.terminal``)               | A tab of a bottom panel: a column   |
-- |                                              | split, shares ``[2/3, 1/3]``, named |
-- |                                              | ``Terminal``, ``Terminal 2``, ... in|
-- |                                              | reading order, as ``open_tab`` names|
-- |                                              | a new one                           |
-- | A record tile (``trax.record``)              | Its page ``#/ref/<kind>/<seq>``     |
-- |                                              | from ``inquiries``, else            |
-- |                                              | ``#/lookup/<id>``                   |
-- | Any other visual, its type known or not      | A visual tab (Files and the         |
-- |                                              | explorer are not made pages)        |
-- | ``trax.chat``                                | Dropped                             |
-- | The open tab's visuals (the page, the page   | Keep their instance ids; every      |
-- | visual's)                                    | other tab gets a new id             |
-- | One thing shown twice (``same_thing``)       | One tab: the open tab's copy, with  |
-- |                                              | its instance id, else the first in  |
-- |                                              | old order                           |
-- | ``focused_instance``                       | Each panel's open tab: the focused  |
-- |                                              | instance if it holds it, else the   |
-- |                                              | old open tab's page, else the first |
-- |                                              | thing the old open tab showed, else |
-- |                                              | its first tab                       |
-- | ``focused_instance``'s panel                 | The focused panel, else the main    |
-- |                                              | panel                               |
-- | A canvas left with nothing                   | One ``New Tab`` (``#/``)            |
-- | ``layout``'s ``columns``, ``floating``,      | Dropped; ``chat_ch`` and            |
-- | ``tiles``, ``share``, ``tabs``, ``tab``      | ``console`` stay                    |
-- | Receipts not of the new shape                | Deleted: a retry of one is answered |
-- |                                              | as new                              |

CREATE FUNCTION _panels_072_tab(visual jsonb, tab_id uuid) RETURNS jsonb
LANGUAGE plpgsql AS $tab$
DECLARE
    ref_kind text;
    ref_seq bigint;
BEGIN
    IF visual->>'type' = 'trax.record' AND jsonb_typeof(visual->'record_id') = 'string'
    THEN
        SELECT kind, seq INTO ref_kind, ref_seq FROM inquiries
            WHERE id = (visual->>'record_id')::uuid;
        RETURN jsonb_build_object('kind', 'page', 'id', tab_id, 'route',
            CASE WHEN ref_seq IS NULL
                THEN '#/lookup/' || ((visual->>'record_id')::uuid)::text
                ELSE format('#/ref/%s/%s', ref_kind, ref_seq) END);
    END IF;
    RETURN jsonb_build_object(
        'kind', 'visual',
        'id', tab_id,
        'type', visual->'type',
        'version', CASE WHEN jsonb_typeof(visual->'version') = 'number'
            THEN visual->'version' ELSE '1' END,
        'record_id', coalesce(visual->'record_id', 'null'),
        'params', CASE WHEN jsonb_typeof(visual->'params') = 'object'
            THEN visual->'params' ELSE '{}' END,
        'placed_by', coalesce(visual->'placed_by', 'null'));
END
$tab$;

CREATE FUNCTION _panels_072_tree(old_state jsonb, old_metadata jsonb) RETURNS jsonb
LANGUAGE plpgsql AS $tree$
DECLARE
    old_layout jsonb := old_metadata->'layout';
    old_tabs jsonb;
    open_at bigint;
    browse_id uuid;
    built jsonb;
    focused_strip text;
    top jsonb;
    root jsonb;
    blank jsonb;
BEGIN
    old_tabs := CASE
        WHEN coalesce(jsonb_typeof(old_layout->'tabs'), '') <> 'array' THEN '[{}]'
        WHEN jsonb_array_length(old_layout->'tabs') = 0 THEN '[{}]'
        ELSE old_layout->'tabs' END;
    SELECT coalesce(min(t.ord), 1) INTO open_at
        FROM jsonb_array_elements(old_tabs) WITH ORDINALITY AS t(tab, ord)
        WHERE t.tab->>'id' = old_layout->>'tab';
    SELECT (b.visual->>'id')::uuid INTO browse_id
        FROM jsonb_array_elements(old_state->'visuals') WITH ORDINALITY AS b(visual, ord)
        WHERE b.visual->>'type' = 'trax.browse'
        ORDER BY b.ord LIMIT 1;

    WITH old_tab AS (
        SELECT t.ord, t.ord = open_at AS is_open,
            CASE WHEN t.ord = open_at THEN old_state->'visuals'
                ELSE t.tab->'visuals' END AS visuals,
            CASE WHEN t.ord <> open_at THEN t.tab->>'page'
                WHEN browse_id IS NOT NULL THEN old_metadata->>'page' END AS page,
            CASE WHEN t.ord = open_at THEN browse_id END AS page_id
        FROM jsonb_array_elements(old_tabs) WITH ORDINALITY AS t(tab, ord)
    ), item AS (
        SELECT old_tab.ord, 0::bigint AS sub, old_tab.is_open, 'main' AS strip,
            jsonb_build_object('kind', 'page',
                'id', coalesce(old_tab.page_id, gen_random_uuid()),
                'route', old_tab.page) AS tab,
            old_tab.page_id AS old_id
        FROM old_tab
        WHERE old_tab.page <> '#/' AND length(old_tab.page) <= 512
            AND old_tab.page ~ '^#/[^[:space:][:cntrl:]]*$'
        UNION ALL
        SELECT old_tab.ord, v.sub, old_tab.is_open,
            CASE WHEN v.visual->>'type' = 'trax.terminal' THEN 'bottom'
                WHEN v.visual->>'placement' IN ('left', 'side', 'floating') THEN 'right'
                ELSE 'main' END,
            _panels_072_tab(v.visual, CASE WHEN old_tab.is_open
                THEN (v.visual->>'id')::uuid ELSE gen_random_uuid() END),
            CASE WHEN old_tab.is_open THEN (v.visual->>'id')::uuid END
        FROM old_tab,
            jsonb_array_elements(CASE WHEN jsonb_typeof(old_tab.visuals) = 'array'
                THEN old_tab.visuals ELSE '[]' END) WITH ORDINALITY AS v(visual, sub)
        WHERE v.visual->>'type' NOT IN ('trax.browse', 'trax.chat')
    ), keyed AS (
        -- The thing a tab shows, as ``canvas_panels.same_thing`` tells two apart: a
        -- terminal is never the same as another.
        SELECT item.*, CASE
            WHEN item.tab->>'kind' = 'page' THEN 'page ' || (item.tab->>'route')
            WHEN item.tab->>'type' = 'trax.terminal' THEN 'terminal ' || (item.tab->>'id')
            ELSE concat_ws(' ', 'visual', item.tab->>'type', item.tab->>'record_id',
                item.tab->'params') END AS thing
        FROM item
    ), kept AS (
        -- The open tab's copy wins: its instance id is the one a box's ``tile``
        -- names, and its version is the one shown last.
        SELECT DISTINCT ON (keyed.thing) keyed.*
        FROM keyed ORDER BY keyed.thing, keyed.is_open DESC, keyed.ord, keyed.sub
    ), numbered AS (
        -- Every terminal stands in the bottom panel, so its ``terminal`` count, in
        -- the order that panel lists its tabs, is its number.
        SELECT kept.*,
            row_number() OVER (PARTITION BY kept.tab->'id' ORDER BY kept.ord, kept.sub)
                AS nth,
            row_number() OVER (PARTITION BY kept.tab->>'type' = 'trax.terminal'
                ORDER BY kept.ord, kept.sub) AS terminal
        FROM kept
    ), ranked AS (
        SELECT numbered.strip, numbered.ord, numbered.sub,
            CASE WHEN numbered.nth = 1 THEN numbered.tab
                ELSE jsonb_set(numbered.tab, '{id}', to_jsonb(gen_random_uuid())) END
                || CASE WHEN numbered.tab->>'type' = 'trax.terminal'
                    THEN jsonb_build_object('name', CASE WHEN numbered.terminal = 1
                        THEN 'Terminal' ELSE 'Terminal ' || numbered.terminal END)
                    ELSE '{}' END
                AS tab,
            CASE
                WHEN numbered.thing = (SELECT k.thing FROM keyed AS k WHERE k.is_open
                    AND k.old_id = (old_state->>'focused_instance')::uuid LIMIT 1)
                    THEN 0
                WHEN numbered.thing = (SELECT k.thing FROM keyed AS k WHERE k.is_open
                    AND k.sub = 0) THEN 1
                WHEN numbered.thing IN (SELECT k.thing FROM keyed AS k WHERE k.is_open)
                    THEN 2
                ELSE 3 END AS rank
        FROM numbered
    ), panel AS (
        SELECT ranked.strip,
            jsonb_build_object('kind', 'panel', 'id', gen_random_uuid(),
                'tabs', jsonb_agg(ranked.tab ORDER BY ranked.ord, ranked.sub),
                'open', (array_agg(ranked.tab->'id'
                    ORDER BY ranked.rank, ranked.ord, ranked.sub))[1]) AS node,
            bool_or(ranked.rank = 0) AS holds_focus
        FROM ranked GROUP BY ranked.strip
    )
    SELECT jsonb_object_agg(panel.strip, panel.node),
        (array_agg(panel.strip) FILTER (WHERE panel.holds_focus))[1]
    INTO built, focused_strip
    FROM panel;

    IF built IS NULL THEN
        blank := jsonb_build_object('kind', 'page', 'id', gen_random_uuid(),
            'route', '#/');
        root := jsonb_build_object('kind', 'panel', 'id', gen_random_uuid(),
            'tabs', jsonb_build_array(blank), 'open', blank->'id');
        RETURN jsonb_build_object('root', root, 'focused', root->'id');
    END IF;
    top := CASE WHEN built ? 'main' AND built ? 'right'
        THEN jsonb_build_object('kind', 'split', 'id', gen_random_uuid(),
            'direction', 'row',
            'children', jsonb_build_array(built->'main', built->'right'),
            'shares', '[0.6, 0.4]'::jsonb)
        ELSE coalesce(built->'main', built->'right') END;
    -- The shares are ``[1 - EDGE_SHARE, EDGE_SHARE]`` as Python spells those floats,
    -- the bottom panel a new terminal opens.
    root := CASE WHEN top IS NULL THEN built->'bottom'
        WHEN built ? 'bottom'
            THEN jsonb_build_object('kind', 'split', 'id', gen_random_uuid(),
                'direction', 'column',
                'children', jsonb_build_array(top, built->'bottom'),
                'shares', '[0.6666666666666667, 0.3333333333333333]'::jsonb)
        ELSE top END;
    RETURN jsonb_build_object('root', root, 'focused',
        built->coalesce(focused_strip, CASE WHEN built ? 'main' THEN 'main'
            WHEN built ? 'right' THEN 'right' ELSE 'bottom' END)->'id');
END
$tree$;

UPDATE visual_canvases
SET state = (state - '{visuals,focused_instance}'::text[])
        || jsonb_build_object('panels', _panels_072_tree(state, metadata)),
    metadata = CASE WHEN jsonb_typeof(metadata->'layout') = 'object'
        THEN jsonb_set(metadata, '{layout}',
            (metadata->'layout') - '{columns,floating,tiles,share,tabs,tab}'::text[])
        ELSE metadata END
WHERE state ? 'visuals';

-- A receipt replays as the state it holds, and an old one holds no panels; an old
-- navigate's ``{}`` would not replay either, now that navigate changes the canvas.
DELETE FROM visual_canvas_operations WHERE NOT response ? 'panels';

DROP FUNCTION _panels_072_tree(jsonb, jsonb);
DROP FUNCTION _panels_072_tab(jsonb, uuid);
