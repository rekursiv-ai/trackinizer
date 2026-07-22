-- Migration 006: constrain ``agent_session_events.kind`` to the Kind literal,
-- and add the AgentSession per-session drain credential column (#438 / G1).
--
-- The capture table shipped (Phase 0) with ``kind TEXT NOT NULL`` and no CHECK,
-- so a direct INSERT could write any string. A row whose ``kind`` is not a
-- ``types/agent_session_events.py`` ``Kind`` member then 500s on read --
-- ``message_for_kind`` raises ``unknown message kind`` when ``from_row``
-- resolves the typed ``message`` member. This adds the CHECK that mirrors the
-- inquiries-side discipline (``status IN (...)``), closing that hole.
--
-- The allowed set is rendered from the ``Kind`` literal by
-- ``substitute_schema_placeholders`` -- the same single source the baseline
-- ``schema.sql`` uses, so the migration CHECK and the baseline CHECK can never
-- drift from the Python type.
--
-- DROP-then-ADD (not ADD-if-absent): a plain ``ADD ... IF NOT EXISTS`` lands the
-- CHECK on a fresh-from-005 DB, but could never WIDEN a kind CHECK left by an
-- earlier render of this same migration -- so a future ``Kind`` member added
-- before this migration is deployed anywhere would need a separate widen
-- migration. Discovering and dropping any existing ``kind`` CHECK first makes
-- this migration re-render the FULL current literal every time it runs, so it
-- both installs the CHECK and widens a stale one. Idempotent in effect: the
-- re-added def is byte-identical on a DB already at the full literal (the
-- catalog-parity gate compares constraint DEFS, not auto-assigned names). No
-- existing row needs purging: every persisted ``kind`` was minted by the typed
-- ``AgentSessionEvent`` boundary, which already enforces the same vocabulary.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- Probe cast-agnostically: ``pg_get_constraintdef`` renders ``kind IN
    -- (...)`` as ``kind = ANY (ARRAY[...])``, so match the ``kind = ANY`` token.
    -- The CHECK is unnamed (auto-assigned), so drop it by discovered name.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'agent_session_events'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%kind = ANY%'
    LOOP
        EXECUTE format(
            'ALTER TABLE agent_session_events DROP CONSTRAINT %I', con_name
        );
    END LOOP;
    ALTER TABLE agent_session_events
        ADD CHECK (kind IN ({agent_session_event_kinds}));
END $$;

-- AgentSession per-session drain credential (#438 / G1).
--
-- ``opened_by_api_key_id`` records the ``api_keys.id`` that opened the session;
-- the inbound-drain route authorizes by matching it, closing the cross-user
-- inbox drain. It is an ``immutable`` column on the type, so it carries no
-- setter, no field route, and no ``change_log`` mirror -- only the ``inquiries``
-- column and its per-kind presence CHECK, both of which the baseline
-- ``schema.sql`` generates from the type. This arm reproduces exactly that pair
-- on a DB deployed at 005, so migrate-from-005 and fresh-from-006 reach parity.
-- Idempotent: the column add is IF NOT EXISTS; the CHECK is added only when
-- absent (matching the 005 paper_* arm).
ALTER TABLE inquiries
    ADD COLUMN IF NOT EXISTS agentsession_opened_by_api_key_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%agentsession_opened_by_api_key_id%'
    ) THEN
        ALTER TABLE inquiries ADD CHECK (
            CASE WHEN kind = 'AgentSession'
                THEN TRUE
                ELSE agentsession_opened_by_api_key_id IS NULL
            END
        );
    END IF;
END $$;

-- AgentSession lifecycle CHECK (A2a/A2b): ``agentsession_ended`` is set iff
-- the session is ``complete``, so a session can never be born ended while not
-- complete (un-messageable yet un-endable), nor marked ``complete`` while
-- ``ended`` stays NULL (complete yet still "live"). A live session may still
-- be ``abandoned`` / ``invalid`` with ``ended`` NULL, so only ``complete`` is
-- bound to ``ended``. Reproduces the baseline
-- ``inquiries_agentsession_lifecycle_check`` on a DB deployed at 005 so
-- migrate-from-005 and fresh-from-006 reach parity. Added only when absent
-- (matching the opened_by arm above), keyed by the constraint def so a
-- re-render is a no-op. No existing row needs purging: end_session has always
-- stamped ``ended`` + ``status = 'complete'`` together, and create never set
-- ``ended`` in any shipped build, so every closed row is (ended, 'complete')
-- and every other row has ``ended`` NULL -- both satisfy the new CHECK.
-- Probe by NAME (the baseline declares it explicitly named), not by def:
-- ``pg_get_constraintdef`` re-renders the predicate with ``::text`` casts and
-- normalized parens, so a def-substring probe would miss it and re-ADD a
-- duplicate-named constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'inquiries'::regclass
          AND conname = 'inquiries_agentsession_lifecycle_check'
    ) THEN
        ALTER TABLE inquiries
            ADD CONSTRAINT inquiries_agentsession_lifecycle_check CHECK (
                kind <> 'AgentSession'
                OR (agentsession_ended IS NOT NULL) = (status = 'complete')
            );
    END IF;
END $$;

-- One LIVE AgentSession per routing name (B3): a PARTIAL UNIQUE index over
-- live sessions makes the DB enforce ``@actor`` routing uniqueness, closing
-- the racy reserve-then-insert window (two concurrent starts minting the same
-- name). ``CREATE UNIQUE INDEX IF NOT EXISTS`` is idempotent and reproduces
-- the baseline ``uq_inquiries_live_session_owner`` so migrate-from-005 and
-- fresh-from-006 reach parity. Safe to build on a 005 DB: the prior advisory
-- reservation already kept live owners distinct (the #N suffix), so no live
-- duplicate exists to block the unique build; ended sessions are excluded by
-- the predicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_inquiries_live_session_owner
    ON inquiries (owner)
    WHERE kind = 'AgentSession' AND owner IS NOT NULL
          AND agentsession_ended IS NULL;
