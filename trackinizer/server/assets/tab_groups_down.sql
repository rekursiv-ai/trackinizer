-- Operator-only rollback of schema.065.sql: stop the new build first, run this with
-- psql -1, and start the old build only after it commits.
--
-- The old build's strict workspaces preference refuses the keys the new build
-- writes, so each person's colours, expanded groups and tab order go, and the pins
-- it reads come back empty: the pins schema.065 folded stay first in the order.
-- Canvases it closed or renamed stay as they are, which the old build reads.
UPDATE users SET preferences = jsonb_set(preferences, '{workspaces}',
        (preferences->'workspaces') - 'colors' - 'expanded' - 'tabs'
            || '{"pinned": []}'::jsonb)
    WHERE preferences ? 'workspaces';

DELETE FROM applied_migrations WHERE name = 'schema.065.sql';
