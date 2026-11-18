-- schema.081.sql -- drop the old embedding engine's four tables: ``session_embeddings``
-- (022, 023), ``session_index_state`` (024), ``session_index_cursor`` (063) and
-- ``inquiry_embeddings`` (the baseline). The embedding add-on keeps every vector in
-- ``embedding.vectors`` (#2297); nothing reads or writes these since.
--
-- DESTRUCTIVE: every row goes. A deployment that wants its old vectors archives them
-- before it upgrades (``server/tools/archive_embeddings.py`` of 0.1.13, or monorepo
-- commit 2290694993, as the tables' owner); ``embedding_tables_down.sql`` builds the
-- tables again, empty, to restore into. ``IF EXISTS``: a second run changes nothing.
--
-- Sized 2026-11-18 on prod, read-only: session_embeddings 2,663,699 rows (19 GB, eight
-- indexes, seven of them partial HNSW), session_index_state 4,252,770 (1,090 MB),
-- inquiry_embeddings 111,516 (189 MB, every row of the 384-wide stub model),
-- session_index_cursor 1 (64 kB). No view, rule or trigger depends on them, and no
-- foreign key points at them.
--
-- Locks: three of them reference ``inquiries`` (``ON DELETE CASCADE``), and dropping a
-- table drops its foreign key's triggers on the referenced table, so each DROP takes
-- ACCESS EXCLUSIVE on its own table and, until the transaction commits, on
-- ``inquiries``. Catalog work and file unlinks only, no scan: well under a second. At
-- startup nothing of the server holds ``inquiries`` yet, but a session that does -- a
-- running ``pg_dump`` (ACCESS SHARE for its whole run), an operator's open
-- transaction -- holds the migration, and startup, until it ends.
DROP TABLE IF EXISTS session_embeddings;
DROP TABLE IF EXISTS session_index_state;
DROP TABLE IF EXISTS session_index_cursor;
DROP TABLE IF EXISTS inquiry_embeddings;
