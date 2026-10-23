INSERT INTO users (id, email, name, role, status, preferences)
VALUES ('00000000-0000-0000-0000-000000000001', 'rename@example.com', 'Rename',
    'writer', 'active', '{"theme":"dark","workstreams":{"order":["00000000-0000-0000-0000-000000000002"],"pinned":["00000000-0000-0000-0000-000000000002"],"hidden":[]}}');
INSERT INTO inquiries (id, kind, seq, account, owner, title, labels, app)
VALUES ('00000000-0000-0000-0000-000000000002', 'Issue', 1,
    'rename@example.com', 'rename@example.com', 'User workspace station workstream',
    ARRAY['workstream', 'scratch'], '{"unrelated":{"workstream_id":"keep verbatim"}}');
INSERT INTO inquiries (id, kind, seq, account, title, labels, agentsession_audience)
VALUES ('00000000-0000-0000-0000-000000000003', 'AgentSession', 1,
    'rename@example.com', 'Before cutover',
    ARRAY['station:00000000-0000-0000-0000-000000000004','git:https://example.com/repo@main'], 'open');
INSERT INTO edges (from_id, to_id, from_kind, to_kind, edge_kind)
VALUES ('00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002',
    'AgentSession', 'Issue', 'produced_by');
INSERT INTO visual_workspaces (id, user_id, revision, state, station)
VALUES ('00000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000001',
    19, '{"visuals":[],"provider":"claude","marks_seq":3}',
    '{"name":"unnamed-workstream#1","workstream_id":"00000000-0000-0000-0000-000000000002","conversation_id":"00000000-0000-0000-0000-000000000005","session_id":"00000000-0000-0000-0000-000000000003","layout":{"chat_ch":42},"suggestion":{"title":"Suggested workspace","workstream_id":"00000000-0000-0000-0000-000000000006"}}');
INSERT INTO visual_workspace_operations (workspace_id, key, request_hash, response)
VALUES ('00000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000007',
    '694bea63d8f323ca1443d9135181fb1d7e4d056709a4f50e308aeb68c7671e9d', '{"id":"00000000-0000-0000-0000-000000000004","revision":18,"workstream_id":"00000000-0000-0000-0000-000000000002","suggestion":{"title":"Suggested workspace","workstream_id":"00000000-0000-0000-0000-000000000006"},"name":"unnamed-workstream#1","visuals":[]}');
