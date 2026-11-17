#!/usr/bin/env bash
# Runs the daily backup or the restore drill inside the running backend
# container. The systemd units in ops/backup/systemd call it; by hand:
#
#   sudo /bin/bash /var/www/myotherapp-production/ops/backup/run.sh backup
#   sudo /bin/bash /var/www/myotherapp-production/ops/backup/run.sh drill --standalone
#   sudo /bin/bash /srv/moa/ops/backup/run.sh media-sync [--dry-run]
#
# backup: scripts/backup.py -- pg_dump (compressed custom format) into
#   /var/backups/myotherapp, uploaded to S3 with server-side encryption
#   under backups/daily/ or backups/weekly/, old local dumps pruned. It
#   raises no alert itself (--no-alert): when it fails, systemd's OnFailure
#   runs ops/backup/alert.sh, which alerts once whatever went wrong --
#   including a container that is not running, which the script cannot see.
# drill: scripts/restore_drill.py -- the newest dump in S3 restored into a
#   scratch database, checked, and dropped. Extra arguments go to it. On
#   the Oracle box it runs in a one-off `migrate` container, as the
#   database's owner (it creates and drops that scratch database), whatever
#   role the API uses.
# media-sync: scripts/media_sync.py -- the media bucket copied to the
#   off-site backup bucket (copy only; off until APP_MEDIA_SYNC=true).
# deliver: the ops bot (ops/server/ops-bot, /srv/moa/ops-bot on the box)
#   encrypts the newest dump with age and sends it to the owner's Telegram.
#   The unit runs it after `backup` succeeded (ExecStartPost=). No bot here,
#   or not set up yet: says so and exits 0.
#
# Settings (environment, or /etc/default/myotherapp-backup for the units):
#   MOA_PROJECT_DIR   the checkout docker-compose runs from
#                     (default /var/www/myotherapp-production)
#   MOA_COMPOSE_FILE  default docker-compose.prod.yml (staging: docker-compose.yml)
#   MOA_SERVICE       the backend's compose service (default backend)
# On the Oracle box ($MOA_PROJECT_DIR/dc exists) docker compose is that
# wrapper, which also loads images.env: the pinned images, never :latest.
#
# Never prints a credential: the scripts scrub what they print, and nothing
# here reads a secret.
set -euo pipefail

PROJECT_DIR="${MOA_PROJECT_DIR:-/var/www/myotherapp-production}"
COMPOSE_FILE="${MOA_COMPOSE_FILE:-docker-compose.prod.yml}"
SERVICE="${MOA_SERVICE:-backend}"

cd "$PROJECT_DIR"

what="${1:-}"
[ $# -gt 0 ] && shift

if [ "$what" = "deliver" ]; then
  for bot in "$PROJECT_DIR/ops-bot/run.py" "$PROJECT_DIR/ops/server/ops-bot/run.py"; do
    if [ -f "$bot" ]; then
      echo "run.sh: sending the new dump to Telegram, encrypted"
      exec python3 -I -B "$bot" deliver-backup "$@"
    fi
  done
  echo "run.sh: no ops bot in $PROJECT_DIR (ops-bot/run.py): the dump was not sent to Telegram"
  exit 0
fi

OWNER_TOOL=no
if [ -x "$PROJECT_DIR/dc" ]; then
  DC=("$PROJECT_DIR/dc")
  OWNER_TOOL=yes
elif command -v docker-compose > /dev/null 2>&1; then
  DC=(docker-compose -f "$COMPOSE_FILE")
elif docker compose version > /dev/null 2>&1; then
  DC=(docker compose -f "$COMPOSE_FILE")
else
  echo "run.sh: neither docker-compose nor 'docker compose' is installed" >&2
  exit 127
fi

case "$what" in
  backup)
    echo "run.sh: backup starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exec "${DC[@]}" exec -T "$SERVICE" python scripts/backup.py --no-alert "$@" < /dev/null
    ;;
  drill)
    echo "run.sh: restore drill starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ "$OWNER_TOOL" = "yes" ] && "${DC[@]}" --profile tools config --services < /dev/null 2> /dev/null | grep -qx migrate; then
      exec "${DC[@]}" run --rm --no-deps -T migrate python scripts/restore_drill.py "$@" < /dev/null
    fi
    exec "${DC[@]}" exec -T "$SERVICE" python scripts/restore_drill.py "$@" < /dev/null
    ;;
  media-sync)
    echo "run.sh: media sync starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exec "${DC[@]}" exec -T "$SERVICE" python scripts/media_sync.py "$@" < /dev/null
    ;;
  *)
    echo "usage: run.sh backup [--keep-local N] | drill [restore_drill.py arguments] | media-sync [--dry-run] | deliver" >&2
    exit 64
    ;;
esac
