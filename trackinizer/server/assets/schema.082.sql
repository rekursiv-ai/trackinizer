-- schema.082.sql -- Partial indexes for AgentSession CLI / science-chat lookups and
-- a covering email index on ``science_chat_shares``. Three index builds; no row is
-- rewritten.
--
-- Sized 2026-11-23 on prod, read-only: 112,182 inquiries (849 MB), 2,744 with a
-- non-null ``agentsession_cli_session_id``, 240 matching ``LIKE 'chat:%'``, and 0
-- ``science_chat_shares`` rows. Every lookup by ``agentsession_cli_session_id``
-- (canvas bar polling, session resume, chat visibility) and every ``LIKE 'chat:%'``
-- starter scan previously walked ``inquiries`` sequentially, and every share lookup
-- by email walked ``science_chat_shares`` sequentially.
--
-- The ``chat:%`` partial index carries the ``LIKE 'chat:%'`` predicate itself so the
-- planner uses it under non-C collations (such as ``en_US.UTF-8``) while keeping the
-- default-collation ``(agentsession_cli_session_id, created, id)`` ordering needed by
-- ``DISTINCT ON (agentsession_cli_session_id) ... ORDER BY ..., created, id``.
--
-- Build cost: two partial btree builds over 2,744 and 240 qualifying rows (<1 s on
-- prod) and one index over 0 rows (<1 ms).

CREATE INDEX IF NOT EXISTS idx_inquiries_agentsession_cli_session_id
    ON inquiries (agentsession_cli_session_id, created, id)
    WHERE kind = 'AgentSession' AND agentsession_cli_session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inquiries_science_chat_sessions
    ON inquiries (agentsession_cli_session_id, created, id)
    WHERE kind = 'AgentSession' AND agentsession_cli_session_id LIKE 'chat:%';

CREATE INDEX IF NOT EXISTS idx_science_chat_shares_email
    ON science_chat_shares (email, conversation_id);
