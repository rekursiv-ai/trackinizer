-- Migration 007: attribute every inquiry to an authenticated ``account``.
--
-- ``account`` is the auth identity a row is held under -- distinct from
-- ``owner`` (who is responsible). The baseline ``schema.sql`` declares it as a
-- NOT NULL base column, generates its ``change_log`` ``old_/new_`` mirror from
-- ``CHANGE_LOG_COLUMN_ORDER``, and admits the ``'account'`` field-change kind
-- through ``{change_kinds}``. This migration reproduces that whole set on a DB
-- deployed at 006 so migrate-from-006 and fresh-from-007 reach catalog parity,
-- and backfills the new NOT NULL column from existing audit history.
--
-- Arms:
--   1. ``inquiries.account`` column (added nullable for the backfill).
--   2. Backfill each row's account from its ``created`` event's principal
--      (the api_keys -> users email, mirroring ``web.py``'s _CHANGE_SELECT),
--      falling back to the system admin for the handful of pre-auth rows whose
--      ``created`` event carries no recoverable principal (actor 'user',
--      api_key_id NULL).
--   3. Promote ``account`` to NOT NULL once every row is populated.
--   4. ``idx_inquiries_account`` filter index.
--   5. ``change_log`` ``old_account`` / ``new_account`` mirror columns + their
--      required-column populated-iff CHECKs.
--   6. Re-render ``change_log_kind_check`` from the full ``{change_kinds}``
--      literal so ``'account'`` becomes an admitted field-change kind.

-- Arm 1: add the column nullable so the backfill UPDATE can run before the
-- NOT NULL promotion. IF NOT EXISTS makes a re-render against a DB already at
-- 007 (or fresh) a no-op.
ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS account TEXT;

-- Arm 2: backfill from the created-event principal. The COALESCE chain mirrors
-- ``server/web.py``'s ``_CHANGE_SELECT`` (api-key user email, then actor-as-user
-- email); the final literal attributes the 2 legacy ``user``/NULL-key rows to
-- the deployment's own admin so the column is a valid active account
-- everywhere -- derived from ``users``, never a hardcoded email, so this
-- migration is portable to any deployment. Only touches rows still NULL, so a
-- re-run after a partial apply is idempotent.
--
-- The fallback is the earliest-created active admin (a deterministic, stable
-- pick). A deployment with zero active admins cannot satisfy the
-- ``account``-is-an-active-user invariant for an unattributable row, so the
-- NOT NULL promotion below will fail loudly rather than stamp an invalid
-- value -- the correct outcome (seed an admin first).
WITH created AS (
    SELECT DISTINCT ON (c.subject_id)
           c.subject_id,
           COALESCE(k_user.email, actor_user.email) AS principal
    FROM change_log c
    LEFT JOIN api_keys k ON k.id = c.api_key_id
    LEFT JOIN users k_user ON k_user.id = k.user_id
    LEFT JOIN users actor_user
        ON actor_user.email = c.actor AND c.api_key_id IS NULL
    WHERE c.kind = 'created'
    ORDER BY c.subject_id, c.created
),
fallback AS (
    SELECT email FROM users
    WHERE status = 'active' AND role = 'admin'
    ORDER BY created_at, email
    LIMIT 1
)
UPDATE inquiries i
   SET account = COALESCE(created.principal, (SELECT email FROM fallback))
  FROM created
 WHERE created.subject_id = i.id
   AND i.account IS NULL;

-- Any row with no ``created`` event at all (should be none -- every inquiry
-- emits one) still must satisfy NOT NULL; attribute it to the same admin.
UPDATE inquiries
   SET account = (
       SELECT email FROM users
       WHERE status = 'active' AND role = 'admin'
       ORDER BY created_at, email
       LIMIT 1
   )
 WHERE account IS NULL;

-- Arm 3: promote to NOT NULL now that every row carries a value. Guarded so a
-- re-render against an already-NOT-NULL column is a no-op.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'inquiries'
          AND column_name = 'account'
          AND is_nullable = 'YES'
    ) THEN
        ALTER TABLE inquiries ALTER COLUMN account SET NOT NULL;
    END IF;
END $$;

-- Arm 4: filter index, mirroring ``idx_inquiries_owner``. Idempotent.
CREATE INDEX IF NOT EXISTS idx_inquiries_account ON inquiries (account);

-- Arm 5: change_log mirror columns for the new required field. A required
-- column's populated-iff gate is the strict ``(old_X IS NOT NULL) = (kind =
-- 'X')`` form (see ``generate_change_log_mirror``); reproduce exactly that for
-- both sides. Columns added IF NOT EXISTS; CHECKs added only when absent.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS old_account TEXT;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS new_account TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%old_account IS NOT NULL%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK ((old_account IS NOT NULL) = (kind = 'account'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%new_account IS NOT NULL%'
    ) THEN
        ALTER TABLE change_log
            ADD CHECK ((new_account IS NOT NULL) = (kind = 'account'));
    END IF;
END $$;

-- Arm 6: admit the ``'account'`` field-change kind. DROP-then-ADD re-renders
-- the FULL current ``{change_kinds}`` literal (the same single source the
-- baseline uses), so this both adds ``'account'`` and widens any stale list a
-- prior render left. The CHECK is unnamed (auto-assigned), so drop by
-- discovered name; the parity gate compares constraint DEFS, not names.
DO $$
DECLARE
    con_name TEXT;
BEGIN
    -- Match ONLY the change-kind enum CHECK. Several other change_log CHECKs
    -- contain ``kind = ANY`` -- ``subject_kind``/``*_peer_edge_kind`` membership
    -- and the big edge-peer ``CASE WHEN (kind = ANY (ARRAY['edge_added'...``
    -- presence CHECK -- so neither ``%kind = ANY%`` nor ``%(kind = ANY%`` is
    -- discriminating. The enum is the unique CHECK that lists ``'created'`` as a
    -- ``kind`` member (no other CHECK references the created milestone), so gate
    -- on that. These extra CHECKs are never re-added by this migration, so
    -- dropping one would leave permanent drift.
    FOR con_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'change_log'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%(kind = ANY%'
          AND pg_get_constraintdef(oid) LIKE '%''created''%'
          AND pg_get_constraintdef(oid) NOT LIKE '%CASE%'
    LOOP
        EXECUTE format('ALTER TABLE change_log DROP CONSTRAINT %I', con_name);
    END LOOP;
    ALTER TABLE change_log ADD CHECK (kind IN ({change_kinds}));
END $$;
