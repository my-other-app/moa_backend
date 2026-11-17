#!/usr/bin/env bash
# db/app-role.sh -- creates the API's own database role, or brings it up to
# date, from db/app-role.sql; then checks what it can and cannot do. Safe to
# run at any time and again: nothing running changes. The API and the worker
# use the role only once .env names it and they are recreated.
#
#   sudo bash /srv/moa/db/app-role.sh          create or update the role, then check it
#   sudo bash /srv/moa/db/app-role.sh check    only check it
#
# Reads two lines of /srv/moa/.env, and prints neither:
#   MOA_APP_DB_USER=moa_app
#   MOA_APP_DB_PASSWORD=<openssl rand -hex 32>
# The password reaches psql on its standard input, never on a command line.
# Runs psql in the postgres container as the owner (POSTGRES_USER) over the
# container's own socket.
set -Eeuo pipefail

here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MOA_DIR="${MOA_DIR:-$(dirname "$here")}"
DC=("$MOA_DIR/dc")

die() { printf 'app-role.sh: %s\n' "$*" >&2; exit 1; }

env_value() {
  grep -E "^$1=" "$MOA_DIR/.env" 2> /dev/null | tail -n 1 | cut -d= -f2- | sed -E 's/^["'\'']//; s/["'\'']$//' || true
}

[ -r "$MOA_DIR/.env" ] || die "$MOA_DIR/.env is not readable (run as root)"
[ -x "${DC[0]}" ] || die "${DC[0]} is missing"
[ -r "$here/app-role.sql" ] || die "$here/app-role.sql is missing"

user="$(env_value MOA_APP_DB_USER)"
password="$(env_value MOA_APP_DB_PASSWORD)"
owner="$(env_value POSTGRES_USER)"
[ -n "$user" ] || die "MOA_APP_DB_USER is not in .env (e.g. MOA_APP_DB_USER=moa_app)"
[[ "$user" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "MOA_APP_DB_USER must be lowercase letters, digits and _"
[ "$user" != "$owner" ] || die "MOA_APP_DB_USER must not be POSTGRES_USER: the app role is a second, smaller role"

# psql as the owner, inside the postgres container, reading stdin.
owner_psql() {
  # shellcheck disable=SC2016 # expanded inside the postgres container
  "${DC[@]}" exec -T postgres sh -c 'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v owner="$POSTGRES_USER" -v db="$POSTGRES_DB" -f -'
}
# One statement as the app role (the container's socket needs no password).
app_psql() {
  # shellcheck disable=SC2016 # expanded inside the postgres container
  "${DC[@]}" exec -T -e "MOA_ROLE=$user" postgres sh -c 'exec psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$MOA_ROLE" -d "$POSTGRES_DB" -c "$0"' "$1" < /dev/null
}

if [ "${1:-}" != "check" ]; then
  [ $# -eq 0 ] || die "usage: app-role.sh [check]"
  [ -n "$password" ] || die "MOA_APP_DB_PASSWORD is not in .env (openssl rand -hex 32)"
  [[ "$password" =~ ^[A-Za-z0-9]{24,128}$ ]] || die "MOA_APP_DB_PASSWORD must be 24-128 letters and digits (openssl rand -hex 32): it goes into a URL as it is"
  echo "app-role.sh: creating or updating role $user"
  {
    printf '\\set app_role %s\n' "$user"
    printf "\\\\set app_password '%s'\n" "$password"
    cat "$here/app-role.sql"
  } | owner_psql
fi
unset password

echo "app-role.sh: checking role $user"
attrs="$(app_psql "SELECT rolsuper::text || ' ' || rolcreatedb::text || ' ' || rolcreaterole::text FROM pg_roles WHERE rolname = current_user")" ||
  die "role $user cannot connect (run app-role.sh without 'check' first)"
[ "$attrs" = "false false false" ] || die "role $user is a superuser or may create databases or roles ($attrs)"
echo "  ok    not a superuser; creates no databases or roles"
app_psql "SELECT count(*) FROM alembic_version" > /dev/null || die "role $user cannot read alembic_version"
echo "  ok    reads the migration head"
if app_psql "CREATE TABLE moa_app_role_check (x int)" > /dev/null 2>&1; then
  printf 'DROP TABLE IF EXISTS moa_app_role_check;\n' | owner_psql || true
  die "role $user could create a table: it must not"
fi
echo "  ok    cannot create tables"
if app_psql "UPDATE alembic_version SET version_num = version_num" > /dev/null 2>&1; then
  die "role $user could write alembic_version: it must not"
fi
echo "  ok    cannot change the migration head"
echo "app-role.sh: role $user is ready. To use it: docs in ops/server/README.md, \"The API's database role\"."
