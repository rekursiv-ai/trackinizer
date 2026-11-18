-- schema.080.sql -- What changed on inquiries and edges, and keyword search over every
-- inquiry text and edge note that gets a vector. A change stamp on ``inquiries``
-- (``changed_xid``, as ``session_records`` has since 063) set when a text column
-- changes, and by an edge write that adds, changes, moves or removes the note of an
-- edge the inquiry starts from; then the two keyword indexes. DDL and two index
-- builds; no row is rewritten.
--
-- Sized 2026-11-18 on prod, read-only: 111,498 inquiries (683 MB), 115,299 edges
-- (32 MB), 4,314 with a note, the longest 1,440 bytes.
--
-- The keyword text is cut to its first 100,000 characters. 84 inquiries hold over
-- 1 MB of text (all Artifacts; the largest a 6.6 MB description), and to_tsvector of
-- the whole text refuses them: "string is too long for tsvector (2542990 bytes, max
-- 1048575 bytes)". An index expression that raises fails the migration, and later
-- every write of such a row. A word of n characters and its separator cost at most
-- 4n lexeme bytes, a 4-byte entry and a 2-byte position, so a tsvector takes at most
-- 5 bytes a character, and 100,000 characters stay under the 1 MB limit by
-- construction; on prod the cut gives 164,990 bytes at the most. Text past the cut is
-- found by vector for the kinds that get vectors and not at all for the others.
--
-- Build cost, measured as the expressions' computation on one core (no sort, no
-- write): 24.3 s over every inquiry, 40 ms over the noted edges. The column and the
-- triggers are catalog changes only. Not timed: the GIN builds themselves on a
-- restored copy; if they pass a minute, build them first on live prod with
-- ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` and this file changes nothing.

ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS changed_xid xid8;

-- Only the columns an inquiry's text is made of stamp the row, so an update that
-- names none of them (scores, labels, status, the lock) never calls this.
CREATE OR REPLACE FUNCTION inquiries_stamp_change() RETURNS trigger
LANGUAGE plpgsql AS $stamp$
BEGIN
    IF TG_OP = 'INSERT'
        OR NEW.title IS DISTINCT FROM OLD.title
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.paper_abstract IS DISTINCT FROM OLD.paper_abstract
    THEN
        NEW.changed_xid := pg_current_xact_id();
    END IF;
    RETURN NEW;
END
$stamp$;

CREATE OR REPLACE TRIGGER inquiries_stamp_change
    BEFORE INSERT OR UPDATE OF title, description, paper_abstract ON inquiries
    FOR EACH ROW EXECUTE FUNCTION inquiries_stamp_change();

-- An edge's note belongs to the inquiry it starts from. Only a write that adds,
-- changes, moves or removes a note stamps that inquiry; a cascade from either end
-- fires these as a DELETE.
CREATE OR REPLACE FUNCTION edges_stamp_owner() RETURNS trigger
LANGUAGE plpgsql AS $stamp$
BEGIN
    UPDATE inquiries SET changed_xid = pg_current_xact_id()
     WHERE id IN (
         CASE WHEN TG_OP <> 'INSERT' THEN OLD.from_id END,
         CASE WHEN TG_OP <> 'DELETE' THEN NEW.from_id END
     );
    RETURN NULL;
END
$stamp$;

CREATE OR REPLACE TRIGGER edges_stamp_owner_insert
    AFTER INSERT ON edges
    FOR EACH ROW WHEN (NEW.note IS NOT NULL)
    EXECUTE FUNCTION edges_stamp_owner();

CREATE OR REPLACE TRIGGER edges_stamp_owner_delete
    AFTER DELETE ON edges
    FOR EACH ROW WHEN (OLD.note IS NOT NULL)
    EXECUTE FUNCTION edges_stamp_owner();

CREATE OR REPLACE TRIGGER edges_stamp_owner_update
    AFTER UPDATE ON edges
    FOR EACH ROW WHEN (
        OLD.note IS DISTINCT FROM NEW.note
        OR (OLD.note IS NOT NULL AND (OLD.from_id, OLD.to_id, OLD.edge_kind)
            IS DISTINCT FROM (NEW.from_id, NEW.to_id, NEW.edge_kind))
    )
    EXECUTE FUNCTION edges_stamp_owner();

-- No index on inquiries.changed_xid, so an edge trigger's stamp is a HOT update; the
-- sweep's owner delta scans inquiries instead (49 ms on prod for one such scan).

CREATE INDEX IF NOT EXISTS idx_inquiries_search ON inquiries USING GIN (
    to_tsvector('simple', left(
        coalesce(title, '') || ' ' || coalesce(description, '') || ' '
        || coalesce(paper_abstract, ''), 100000))
);

CREATE INDEX IF NOT EXISTS idx_edges_note_search
    ON edges USING GIN (to_tsvector('simple', note)) WHERE note IS NOT NULL;
