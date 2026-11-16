-- Indexes no query has used since the statistics were last reset. Read-only.
--
--   docker compose exec -T postgres psql -U <user> -d <db> -f - < backend/scripts/unused_indexes.sql
--
-- One row per index with idx_scan = 0, largest first:
--   table, index, size, scans (0), rows read, what it is (primary key,
--   unique, constraint name if it backs one), whether it is only the
--   soft-delete flag (is_deleted), the index that starts with the same
--   columns and predicate and so already serves its reads (covered_by),
--   and how long the counters have been running (stats_since, uptime).
--
-- Read before dropping anything:
--   - The counters start again on a stats reset and on a crash restart:
--     drop nothing on the strength of a server that came up an hour ago.
--     Look after a busy week (DevFest day included).
--   - An index that backs a constraint (primary key, UNIQUE, EXCLUDE) is
--     never a candidate however rarely it is scanned: it enforces the rule.
--     A unique index without a constraint enforces too (`unique` = yes).
--   - idx_scan counts the index scans on this server only: a replica has
--     counters of its own.
--   - Drop with DROP INDEX CONCURRENTLY IF EXISTS, in a migration, and set
--     the model column's index=False (or remove its Index) in the same
--     change, or the next create_all puts it back in the tests
--     (tests/test_schema_matches_models.py fails until both agree).
WITH stats AS (
    SELECT coalesce(stats_reset, pg_postmaster_start_time()) AS since
    FROM pg_stat_database WHERE datname = current_database()
),
ix AS (
    SELECT i.indexrelid, i.indrelid, i.indisunique, i.indisprimary, i.indisvalid, i.indnatts AS ncols,
           i.indkey[0] AS first_col,
           -- "2 8": the columns' numbers, and their operator classes, as text
           -- (int2vector and oidvector subscript from 0, and compare by bounds as arrays).
           i.indkey::text AS cols, i.indclass::text AS classes,
           coalesce(pg_get_expr(i.indpred, i.indrelid), '') AS pred,
           i.indexprs IS NOT NULL AS has_exprs,
           c.relam
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
)
SELECT
    s.relname AS "table",
    s.indexrelname AS "index",
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
    s.idx_scan AS scans,
    s.idx_tup_read AS rows_read,
    CASE WHEN ix.indisprimary THEN 'yes' ELSE '' END AS primary_key,
    CASE WHEN ix.indisunique THEN 'yes' ELSE '' END AS "unique",
    coalesce(con.conname || ' (' || CASE con.contype WHEN 'p' THEN 'primary key' WHEN 'u' THEN 'unique' ELSE 'exclude' END || ')', '') AS backs_constraint,
    CASE WHEN ix.ncols = 1 AND a.attname = 'is_deleted' AND NOT ix.has_exprs THEN 'yes' ELSE '' END AS is_deleted_only,
    coalesce(cover.indexname, '') AS covered_by,
    CASE WHEN ix.indisvalid THEN '' ELSE 'INVALID' END AS state,
    stats.since AS stats_since,
    date_trunc('second', now() - pg_postmaster_start_time()) AS uptime
FROM pg_stat_user_indexes s
JOIN ix ON ix.indexrelid = s.indexrelid
CROSS JOIN stats
LEFT JOIN pg_constraint con ON con.conindid = s.indexrelid AND con.contype IN ('p', 'u', 'x')
LEFT JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = ix.first_col
-- Another index of the table whose leading columns are these, with the
-- same access method, operator classes and predicate: it serves the same
-- reads (and more), so this one only costs writes.
LEFT JOIN LATERAL (
    SELECT oc.relname AS indexname
    FROM ix other
    JOIN pg_class oc ON oc.oid = other.indexrelid
    WHERE other.indrelid = ix.indrelid AND other.indexrelid <> ix.indexrelid
      AND other.relam = ix.relam AND other.pred = ix.pred
      AND NOT ix.has_exprs AND NOT other.has_exprs AND other.indisvalid
      AND (other.cols = ix.cols OR other.cols LIKE ix.cols || ' %')
      AND (other.classes = ix.classes OR other.classes LIKE ix.classes || ' %')
      -- The same columns exactly: only a unique one covers, or the older of two twins.
      AND (other.cols <> ix.cols OR other.indisunique OR other.indexrelid < ix.indexrelid)
    ORDER BY other.ncols, oc.relname
    LIMIT 1
) cover ON true
WHERE s.schemaname = 'public' AND s.idx_scan = 0
ORDER BY pg_relation_size(s.indexrelid) DESC, s.relname, s.indexrelname;
