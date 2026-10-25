-- schema.059.sql -- Scratch becomes an ordinary workspace, and each person's is their
-- default: where a chat or a session with no other home lands.
--
-- A person's Scratch is the Issue a 058 server reads as one: their own (owner =
-- account), labelled ``workspace`` and ``scratch``, the first by seq. The label stays
-- the default's mark (workspaces.py, ``_DEFAULT_LABEL``). A Scratch still titled
-- Scratch is titled "Untitled workspace" and stays the default. One its person renamed
-- was named, and a rename now makes the default ordinary, so it is promoted the way
-- that rename promotes it: its owner's canvases in no workspace are given its id, so
-- they stay in it, then it loses the label. It keeps its title, and its person's next
-- chat or session with no other home makes a new "Untitled workspace".
--
-- Each title and label change is audited as the store audits one, before the row
-- moves, so the log names the value it held. A re-titled Scratch loses its embedding,
-- which bootstrap makes again from the new title. A default with a new default's
-- title ("Untitled workspace", alone or numbered) is left alone, so a replay, or a run
-- after the rollback, changes only what the rollback put back.
--
-- Run it at startup, not before the restart: the old build would make a new Scratch
-- for each person whose Scratch this promotes. ``default_workspace_down.sql`` is the
-- operator's rollback.
DO $$
DECLARE
    scratch RECORD;
BEGIN
    FOR scratch IN
        SELECT DISTINCT ON (owner) id, owner, title, labels FROM inquiries
            WHERE kind = 'Issue' AND owner = account
                AND labels @> ARRAY['workspace', 'scratch']::text[]
            ORDER BY owner, seq
    LOOP
        IF scratch.title = 'Scratch' THEN
            INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, reason,
                                    old_title, new_title)
            VALUES (gen_random_uuid(), 'schema.059', scratch.id, 'Issue', 'title',
                    'Scratch is the default workspace, Untitled workspace',
                    scratch.title, 'Untitled workspace');
            UPDATE inquiries SET title = 'Untitled workspace', modified = clock_timestamp()
                WHERE id = scratch.id;
            DELETE FROM inquiry_embeddings WHERE inquiry_id = scratch.id;
        ELSIF scratch.title !~ '^Untitled workspace( [0-9]+)?$' THEN
            UPDATE visual_canvases AS canvas
                SET metadata = canvas.metadata
                    || jsonb_build_object('workspace_id', scratch.id::text)
                FROM users
                WHERE users.email = scratch.owner AND canvas.user_id = users.id
                    AND NOT canvas.metadata ? 'workspace_id';
            INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, reason,
                                    old_labels, new_labels)
            VALUES (gen_random_uuid(), 'schema.059', scratch.id, 'Issue', 'labels',
                    'a renamed Scratch is an ordinary workspace',
                    scratch.labels, array_remove(scratch.labels, 'scratch'));
            UPDATE inquiries SET labels = array_remove(labels, 'scratch'),
                                 modified = clock_timestamp()
                WHERE id = scratch.id;
        END IF;
    END LOOP;
END;
$$;
