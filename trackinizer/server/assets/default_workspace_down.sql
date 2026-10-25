-- Operator-only rollback of schema.059.sql: stop the new build first, run this with
-- psql -1, and start the old build only after it commits.
--
-- A Scratch that schema.059 promoted, as its audit row names it, is marked again.
-- Each person's first marked workspace with a new default's title ("Untitled
-- workspace", alone or numbered) is titled Scratch again; a later one, made after a
-- rename, is an ordinary workspace to the old build. That Scratch leaves its person's
-- hidden list, since the old build cannot show a hidden Scratch again. Each change is
-- audited, and a re-titled row is embedded again at the next boot. A canvas given a
-- workspace id keeps it: the old build reads it in Scratch all the same.
DO $$
DECLARE
    item RECORD;
BEGIN
    FOR item IN
        SELECT id, labels FROM inquiries
            WHERE kind = 'Issue' AND NOT labels @> ARRAY['scratch']::text[]
                AND id IN (SELECT subject_id FROM change_log
                    WHERE actor = 'schema.059' AND kind = 'labels')
    LOOP
        INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, reason,
                                old_labels, new_labels)
        VALUES (gen_random_uuid(), 'default_workspace_down', item.id, 'Issue', 'labels',
                'rollback of schema.059', item.labels, item.labels || ARRAY['scratch']);
        UPDATE inquiries SET labels = labels || ARRAY['scratch'],
                             modified = clock_timestamp()
            WHERE id = item.id;
    END LOOP;
    FOR item IN
        SELECT DISTINCT ON (owner) id, owner, title FROM inquiries
            WHERE kind = 'Issue' AND owner = account
                AND labels @> ARRAY['workspace', 'scratch']::text[]
            ORDER BY owner, seq
    LOOP
        IF item.title ~ '^Untitled workspace( [0-9]+)?$' THEN
            INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, reason,
                                    old_title, new_title)
            VALUES (gen_random_uuid(), 'default_workspace_down', item.id, 'Issue',
                    'title', 'rollback of schema.059', item.title, 'Scratch');
            UPDATE inquiries SET title = 'Scratch', modified = clock_timestamp()
                WHERE id = item.id;
            DELETE FROM inquiry_embeddings WHERE inquiry_id = item.id;
        END IF;
        UPDATE users SET preferences = jsonb_set(preferences,
                ARRAY['workspaces', 'hidden'],
                (preferences->'workspaces'->'hidden') - item.id::text)
            WHERE email = item.owner
                AND preferences->'workspaces'->'hidden' ? item.id::text;
    END LOOP;
END;
$$;

DELETE FROM applied_migrations WHERE name = 'schema.059.sql';
