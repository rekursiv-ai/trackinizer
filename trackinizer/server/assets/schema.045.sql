-- schema.045.sql -- what each person has been given, has bought and has spent.
-- One row per entry: a grant, a purchase, a chat turn or campaign launch charged, a
-- release of a launch's charge when the launch failed, or a refund or dispute Stripe
-- reported (which takes the purchase's credit back). ``idem`` makes each
-- entry apply once, so Stripe's redelivered event and a retried charge are no-ops;
-- ``ref`` is what the entry is for, and one payment buys once. A refund or dispute
-- whose payment names no purchase here has no ``email``. Entries are kept under the
-- lowercase email, with no FK to ``users``: a deleted account that signs in again
-- keeps its debt and does not get a second grant. Additive: the old build never reads the
-- table, so this is safe to apply before the restart.
CREATE TABLE IF NOT EXISTS credit_entries (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      TEXT CHECK (email = lower(email)),
    kind       TEXT NOT NULL
               CHECK (kind IN ('grant', 'purchase', 'chat', 'campaign', 'release', 'refund', 'dispute')),
    usd        NUMERIC(14, 6) NOT NULL CHECK (usd >= 0),
    ref        TEXT NOT NULL,
    idem       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (email IS NOT NULL OR kind IN ('refund', 'dispute'))
);

CREATE UNIQUE INDEX IF NOT EXISTS credit_entries_one_purchase_per_payment
    ON credit_entries (ref) WHERE kind = 'purchase';

CREATE INDEX IF NOT EXISTS credit_entries_user_created
    ON credit_entries (email, created_at);
