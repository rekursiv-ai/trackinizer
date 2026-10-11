-- schema.039.sql -- does nothing, on purpose. An earlier draft added machine
-- columns and a machine_requests table; it was withdrawn before release. A
-- database that applied that draft keeps them, unread, until a later file
-- drops them. Databases record migrations by name, so 039 must never carry
-- another change. The statement is required: PGlite through asyncpg fails on
-- a body that is only comments.
SELECT 1;
