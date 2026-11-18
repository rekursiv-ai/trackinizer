-- schema.050.sql -- a campaign's sessions are open: every reader of the server reads
-- its transcripts.
--
-- The campaign host's key was pinned ``open_into = staff``, so each lead session it
-- opened was ``staff``, and a chat the assistant read one into for its asker became
-- ``staff`` too, which no route lowers (session_audience_routes.py): a tester's own
-- chat turned staff-only at its first status question, and a staff chat is forked by
-- no one. The tenants decided the transcripts hide nothing worth that, so the key is
-- minted unpinned now (setup-assistant.sh campaign-profile) and new sessions open as
-- ``open``. This lowers the ones already held, every ``staff`` session alike, since
-- only such a key ever opened one; a ``private`` session stays, a person hid it. Each
-- move is audited as the audience route audits one, before the rows move, so the log
-- names the audience each row held. A replay finds no ``staff`` session and changes
-- nothing.
INSERT INTO change_log (id, actor, subject_id, subject_kind, kind, reason,
                        old_agentsession_audience, new_agentsession_audience)
SELECT gen_random_uuid(), 'schema.050', id, 'AgentSession', 'agentsession_audience',
       'campaign transcripts opened to every reader', agentsession_audience, 'open'
    FROM inquiries
    WHERE kind = 'AgentSession' AND agentsession_audience = 'staff';

UPDATE inquiries SET agentsession_audience = 'open'
    WHERE kind = 'AgentSession' AND agentsession_audience = 'staff';
