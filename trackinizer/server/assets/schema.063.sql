-- schema.063.sql -- stamp each session record's last text change, and keep a
-- per-model cursor, so the embedding sweep reads only what changed.
--
-- The follow service used to rescan every ``session_records`` row each round and
-- ask ``session_index_state`` about each one: on the production host 9.6M rows
-- and 3.6M marker lookups every ~10 minutes, to find a few hundred. A sweep
-- now remembers the snapshot it last reconciled through (``session_index_cursor``)
-- and next reads only the rows whose ``changed_xid`` that snapshot could not see.
--
-- ``changed_xid`` is the transaction that inserted the row or last changed its
-- ``text`` -- the only column the embedding depends on, beside the ``kind`` that
-- picks its units. A trigger sets it, not the writers: four paths write record
-- text (an append's upsert, a fork's copy, the body offload, the legacy retype),
-- and a writer that forgot the stamp would leave its rows unembedded with nothing
-- failing. An upsert that re-feeds the same text keeps the old stamp, so a claude
-- compaction's replay costs no re-read.
--
-- A snapshot, not a timestamp or a sequence: a row stamped by a transaction that
-- was still open when the sweep read commits later with an older value, and a
-- watermark would skip it. ``pg_visible_in_snapshot`` asks the one question that
-- is exact -- had this transaction committed when the cursor was taken.
--
-- The baseline ``schema.sql`` carries the same column, trigger, index and table
-- for a fresh install, so the two must stay in step -- pinned by
-- ``schema_migration_test.py``.
--
-- COST. ``ADD COLUMN`` without a default is a catalog change. The partial index
-- holds no existing row (all NULL), but building it still scans the table once,
-- holding off writes while it runs; over 9.6M rows that is the bulk of this
-- migration's downtime. Existing rows keep NULL: a model with no cursor
-- catches up with one full pending scan, which reads them by their markers.

ALTER TABLE session_records ADD COLUMN IF NOT EXISTS changed_xid xid8;

CREATE OR REPLACE FUNCTION session_records_stamp_change() RETURNS trigger
LANGUAGE plpgsql AS $stamp$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.text IS DISTINCT FROM OLD.text THEN
        NEW.changed_xid := pg_current_xact_id();
    END IF;
    RETURN NEW;
END
$stamp$;

CREATE OR REPLACE TRIGGER session_records_stamp_change
    BEFORE INSERT OR UPDATE OF text ON session_records
    FOR EACH ROW EXECUTE FUNCTION session_records_stamp_change();

CREATE INDEX IF NOT EXISTS idx_session_records_changed_xid
    ON session_records (changed_xid) WHERE changed_xid IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_index_cursor (
    mapper      TEXT NOT NULL,
    model       TEXT NOT NULL,
    snapshot    pg_snapshot NOT NULL,
    updated     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mapper, model)
);
