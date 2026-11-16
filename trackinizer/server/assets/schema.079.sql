-- schema.079.sql -- Place each unplaced science-chat session whose own account's
-- canvas holds its chat, as a chat started in Chat is now placed
-- (``science_chat_routes.start_chat_route``): the ``produced_by`` edge to that
-- canvas's workspace, else to the account's default, and the ``canvas:<id>`` label
-- of that canvas, so the workspace lists it and a moved canvas takes it along
-- (``session_placement``). That is chiefly each chat started in Chat before this
-- build, and any unplaced session of such a chat. A session placed already, the
-- assistant's (its account owns no canvas), and one whose account has no workspace
-- to land in are left alone. Rows only, written as a placement writes them less
-- their audit rows. Run at the new build's boot: a chat the old build starts after
-- it would stay unplaced, so it is not run ahead of the restart.
--
-- Sized 2026-11-15: AgentSession numbers reach 22,066, a ceiling on the sessions the
-- GIN index on ``labels`` narrows to science chats; one statement, well under a
-- second.
WITH unplaced AS (
    SELECT
        sess.id AS session_id,
        canvas.id AS canvas_id,
        coalesce(
            (
                SELECT issue.id FROM inquiries AS issue
                WHERE issue.id::text = canvas.metadata ->> 'workspace_id'
                    AND issue.kind = 'Issue' AND issue.owner = sess.account
                    AND issue.labels @> ARRAY['workspace']::text[]
            ),
            (
                SELECT issue.id FROM inquiries AS issue
                WHERE issue.kind = 'Issue' AND issue.owner = sess.account
                    AND issue.account = sess.account
                    AND issue.labels @> ARRAY['workspace', 'scratch']::text[]
                ORDER BY issue.seq LIMIT 1
            )
        ) AS workspace_id
    FROM inquiries AS sess
    JOIN users AS member ON member.email = sess.account
    JOIN visual_canvases AS canvas
        ON canvas.user_id = member.id
        AND canvas.metadata ->> 'conversation_id'
            = substr(sess.agentsession_cli_session_id, length('chat:') + 1)
    WHERE sess.kind = 'AgentSession'
        AND sess.labels @> ARRAY['science-chat']::text[]
        AND sess.agentsession_cli_session_id LIKE 'chat:%'
        AND NOT EXISTS (
            SELECT 1 FROM edges AS e
            JOIN inquiries AS placed ON placed.id = e.to_id
            WHERE e.from_id = sess.id AND e.edge_kind = 'produced_by'
                AND e.to_kind = 'Issue'
                AND placed.labels @> ARRAY['workspace']::text[]
        )
),
edged AS (
    INSERT INTO edges (from_id, from_kind, to_id, to_kind, edge_kind)
    SELECT session_id, 'AgentSession', workspace_id, 'Issue', 'produced_by'
    FROM unplaced WHERE workspace_id IS NOT NULL
    ON CONFLICT DO NOTHING
    RETURNING from_id
)
UPDATE inquiries AS sess
SET labels = array_append(coalesce(sess.labels, ARRAY[]::text[]),
    'canvas:' || unplaced.canvas_id)
FROM unplaced
WHERE sess.id = unplaced.session_id AND unplaced.workspace_id IS NOT NULL
    AND NOT coalesce(sess.labels, ARRAY[]::text[])
        @> ARRAY['canvas:' || unplaced.canvas_id]::text[];
