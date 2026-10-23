SELECT 'table' AS kind, tablename::TEXT AS name, '' AS definition
FROM pg_tables WHERE schemaname = 'public'
UNION ALL SELECT 'column', table_name || '.' || column_name,
    data_type || ':' || is_nullable || ':' || coalesce(column_default, '')
FROM information_schema.columns WHERE table_schema = 'public'
UNION ALL SELECT 'constraint', relation.relname || '.' || con.conname,
    regexp_replace(pg_get_constraintdef(con.oid), '\s+', ' ', 'g')
FROM pg_constraint AS con
JOIN pg_class AS relation ON relation.oid = con.conrelid
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
UNION ALL SELECT 'index', tablename || '.' || indexname, indexdef
FROM pg_indexes WHERE schemaname = 'public'
UNION ALL SELECT 'sequence', sequencename,
    data_type || ':' || start_value || ':' || min_value || ':' || max_value
    || ':' || increment_by || ':' || cycle || ':' || cache_size
FROM pg_sequences WHERE schemaname = 'public';
