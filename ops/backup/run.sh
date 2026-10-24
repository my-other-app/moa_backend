#!/usr/bin/env bash
# Runs the daily backup or the restore drill inside the running backend
# container. The systemd units in ops/backup/systemd call it; by hand:
#
#   sudo /bin/bash /var/www/myotherapp-production/ops/backup/run.sh backup
#   sudo /bin/bash /var/www/myotherapp-production/ops/backup/run.sh drill --standalone
#
# backup: scripts/backup.py -- pg_dump (compressed custom format) into
#   /var/backups/myotherapp, uploaded to S3 with server-side encryption
#   under backups/daily/ or backups/weekly/, old local dumps pruned. It
#   raises no alert itself (--no-alert): when it fails, systemd's OnFailure
#   runs ops/backup/alert.sh, which alerts once whatever went wrong --
#   including a container that is not running, which the script cannot see.
# drill: scripts/restore_drill.py -- the newest dump in S3 restored into a
#   scratch database, checked, and dropped. Extra arguments go to it.
#
# Settings (environment, or /etc/default/myotherapp-backup for the units):
#   MOA_PROJECT_DIR   the checkout docker-compose runs from
#                     (default /var/www/myotherapp-production)
#   MOA_COMPOSE_FILE  default docker-compose.prod.yml (staging: docker-compose.yml)
#   MOA_SERVICE       the backend's compose service (default backend)
#
# Never prints a credential: the scripts scrub what they print, and nothing
# here reads a secret.
set -euo pipefail

PROJECT_DIR="${MOA_PROJECT_DIR:-/var/www/myotherapp-production}"
COMPOSE_FILE="${MOA_COMPOSE_FILE:-docker-compose.prod.yml}"
SERVICE="${MOA_SERVICE:-backend}"

cd "$PROJECT_DIR"

if command -v docker-compose > /dev/null 2>&1; then
  DC=(docker-compose -f "$COMPOSE_FILE")
elif docker compose version > /dev/null 2>&1; then
  DC=(docker compose -f "$COMPOSE_FILE")
else
  echo "run.sh: neither docker-compose nor 'docker compose' is installed" >&2
  exit 127
fi

what="${1:-}"
[ $# -gt 0 ] && shift

case "$what" in
  backup)
    echo "run.sh: backup starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exec "${DC[@]}" exec -T "$SERVICE" python scripts/backup.py --no-alert "$@" < /dev/null
    ;;
  drill)
    echo "run.sh: restore drill starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exec "${DC[@]}" exec -T "$SERVICE" python scripts/restore_drill.py "$@" < /dev/null
    ;;
  *)
    echo "usage: run.sh backup [--keep-local N] | drill [restore_drill.py arguments]" >&2
    exit 64
    ;;
esac
