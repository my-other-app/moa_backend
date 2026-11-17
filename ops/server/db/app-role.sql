-- The API's and the worker's own database role: it may read and write rows,
-- use the sequences and call the functions in the `public` schema, and
-- nothing else. It owns nothing, is not a superuser, and cannot create
-- databases, roles, tables or anything else: the schema stays with the
-- owner (POSTGRES_USER), which runs the migrations.
--
-- Safe to run again: it creates the role once, then sets the same
-- attributes, password and grants each time. Run it through db/app-role.sh,
-- which gives it these psql variables (never on a command line):
--   app_role      the role (MOA_APP_DB_USER in .env, e.g. moa_app)
--   app_password  its password (MOA_APP_DB_PASSWORD in .env)
--   owner         the role that owns the schema and runs the migrations
--   db            the database
--
-- Tables, sequences and functions the owner creates later (every
-- migration) get the same grants by default (ALTER DEFAULT PRIVILEGES), so
-- this needs no re-run after a migration. It changes nothing for the owner
-- and drops nothing.
\set ON_ERROR_STOP on

SELECT :'app_role' <> :'owner' AS distinct_roles \gset
\if :distinct_roles
\else
  \echo 'app-role.sql: the app role must not be the owner role'
  SELECT 1 / 0;
\endif

SELECT format('CREATE ROLE %I LOGIN', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role') \gexec

ALTER ROLE :"app_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'app_password';

-- Connect to this database; use (not create in) the public schema.
GRANT CONNECT ON DATABASE :"db" TO :"app_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_role";

-- Rows: read and write. No TRUNCATE, REFERENCES or TRIGGER.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_role";
-- Ids: nextval (USAGE), currval (SELECT), setval (UPDATE).
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO :"app_role";
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"app_role";

-- The migration head: the API reads it (readiness), only migrations write it.
SELECT to_regclass('public.alembic_version') IS NOT NULL AS has_alembic \gset
\if :has_alembic
  REVOKE INSERT, UPDATE, DELETE ON public.alembic_version FROM :"app_role";
\endif

-- What the owner creates from now on.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO :"app_role";

-- The check: every table readable and writable (alembic_version readable
-- only), every sequence usable. Stops with an error if any is not.
SELECT count(*) AS missing
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
WHERE (c.relkind IN ('r', 'p') AND NOT (
         has_table_privilege(:'app_role', c.oid, 'SELECT')
         AND (c.relname = 'alembic_version'
              OR (has_table_privilege(:'app_role', c.oid, 'INSERT')
                  AND has_table_privilege(:'app_role', c.oid, 'UPDATE')
                  AND has_table_privilege(:'app_role', c.oid, 'DELETE')))))
   OR (c.relkind = 'S' AND NOT has_sequence_privilege(:'app_role', c.oid, 'USAGE'))
\gset
SELECT :missing = 0 AS all_granted \gset
\if :all_granted
  \echo 'app-role.sql: done; every table and sequence in public is granted'
\else
  \echo 'app-role.sql: some tables or sequences are not granted'
  SELECT 1 / 0;
\endif
