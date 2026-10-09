-- schema.037.sql -- canvas Chat conversations are AgentSessions now, so the separate
-- chat store goes: a conversation is the session whose cli_session_id is
-- ``chat:<conversation id>``, and its lines are that session's records. Existing
-- local chat history is discarded; the hosted server has none. Messages first: they
-- reference their conversation.
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS chat_conversations;
