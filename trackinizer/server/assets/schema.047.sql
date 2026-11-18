-- schema.047.sql -- a session's audience moves, audited; a science chat's session row
-- carries none of the chat's words.
--
-- A session's owner now hides it (``private``) and shows it again, and a chat whose
-- assistant read a staff session for an asker becomes ``staff``. Each move is audited
-- like any field edit: the change log gains the ``agentsession_audience`` kind and its
-- old/new mirror. The constraints below are the ones the baseline produces for the
-- ``AgentSession.audience`` spec; ``schema_migration_test`` proves the two agree. The
-- baseline leaves the change-log ones unnamed, so they are named here only to make a
-- replay a no-op. Additive: no existing row holds the kind, and every one passes each
-- CHECK. One ALTER, so the table is scanned once to validate its new CHECKs.
ALTER TABLE change_log
    ADD COLUMN IF NOT EXISTS old_agentsession_audience TEXT,
    ADD COLUMN IF NOT EXISTS new_agentsession_audience TEXT,
    DROP CONSTRAINT IF EXISTS change_log_old_agentsession_audience_kind_check,
    DROP CONSTRAINT IF EXISTS change_log_new_agentsession_audience_kind_check,
    DROP CONSTRAINT IF EXISTS change_log_old_agentsession_audience_check,
    DROP CONSTRAINT IF EXISTS change_log_new_agentsession_audience_check,
    DROP CONSTRAINT IF EXISTS change_log_agentsession_audience_subject_check,
    DROP CONSTRAINT change_log_kind_check,
    ADD CONSTRAINT change_log_old_agentsession_audience_kind_check
        CHECK ((old_agentsession_audience IS NOT NULL) = (kind = 'agentsession_audience')),
    ADD CONSTRAINT change_log_new_agentsession_audience_kind_check
        CHECK ((new_agentsession_audience IS NOT NULL) = (kind = 'agentsession_audience')),
    ADD CONSTRAINT change_log_old_agentsession_audience_check
        CHECK (old_agentsession_audience IS NULL OR old_agentsession_audience ~ '^[a-z][a-z0-9_-]{0,31}$'),
    ADD CONSTRAINT change_log_new_agentsession_audience_check
        CHECK (new_agentsession_audience IS NULL OR new_agentsession_audience ~ '^[a-z][a-z0-9_-]{0,31}$'),
    ADD CONSTRAINT change_log_agentsession_audience_subject_check
        CHECK (kind <> 'agentsession_audience' OR subject_kind = 'AgentSession'),
    ADD CONSTRAINT change_log_kind_check
        CHECK (kind IN ('created', 'purged', 'status', 'title', 'description', 'labels', 'owner', 'account', 'subscribers', 'marginal_cost', 'app', 'issue_kind', 'issue_validation', 'issue_priority', 'belief_judgement', 'belief_confidence', 'experiment_outcome', 'experiment_config', 'experiment_codechanges', 'paper_abstract', 'paper_authors', 'paper_publication_type', 'paper_venue', 'paper_subvenue', 'paper_publish_date', 'paper_source', 'paper_google_scholar_cluster_id', 'paper_google_scholar_cites_id', 'codechange_sha', 'webresult_url', 'websearch_query', 'websearch_provider', 'agentsession_cli', 'agentsession_cli_session_id', 'agentsession_started', 'agentsession_ended', 'agentsession_rooms', 'agentsession_audience', 'edge_added', 'edge_removed', 'edge_annotation_changed', 'dependency_changed', 'implicit_subs_opened', 'implicit_subs_closed'));

-- Every key reads a session's row, and only a chat's members read its lines, but a
-- chat's partner titled its session with the chat's first line. The server now titles
-- each one ``Science chat`` (``CHAT_TITLE`` in ``wire/wire_science_chat.py``) whatever
-- the partner asks; this retitles the ones already held. A title change the change log
-- kept for one is blanked, as a deleted chat's are, and its title's embedding goes, so
-- the bootstrap that runs this embeds the new title in its place. An older build
-- titles a new chat by its line again, so apply this with the build that does not.
UPDATE inquiries SET title = 'Science chat'
    WHERE kind = 'AgentSession' AND agentsession_cli_session_id LIKE 'chat:%';

UPDATE change_log SET old_title = '', new_title = ''
    WHERE kind = 'title' AND subject_id IN (
        SELECT id FROM inquiries
        WHERE kind = 'AgentSession' AND agentsession_cli_session_id LIKE 'chat:%'
    );

DELETE FROM inquiry_embeddings
    WHERE inquiry_id IN (
        SELECT id FROM inquiries
        WHERE kind = 'AgentSession' AND agentsession_cli_session_id LIKE 'chat:%'
    );
