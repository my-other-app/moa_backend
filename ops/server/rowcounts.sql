-- Exact row counts of every table in the public schema, and the migration
-- revision: run on the old database and on the restored one, then diff the
-- two outputs (docs/MOVE_TO_ORACLE.md, "Restore and compare"). Read-only.
--
--   psql -X -A -t -F ' ' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f rowcounts.sql
\set ON_ERROR_STOP on
SELECT 'alembic_version', string_agg(version_num, ',' ORDER BY version_num)
FROM alembic_version;

SELECT t.table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', t.table_schema, t.table_name),
                           false, true, '')))[1]::text::bigint
FROM information_schema.tables AS t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name;

SELECT 'extensions', string_agg(extname || '=' || extversion, ',' ORDER BY extname)
FROM pg_extension;
