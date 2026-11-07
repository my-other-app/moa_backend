#!/usr/bin/env bash
# Tells the admins a backup unit failed: systemd runs it as
# myotherapp-alert@<unit>.service, from the unit's OnFailure=.
#
#   alert.sh myotherapp-backup.service
#
# 0. To the owner's Telegram, through the ops bot (ops/server/ops-bot, at
#    $MOA_PROJECT_DIR/ops-bot on the box) when it is installed and set up:
#    `run.py notify` redacts the body and sends it to the one chat it
#    serves. Then the app as well (below), so the mail, Discord and the
#    dashboard's bell have it too.
# 1. Through the app: scripts/backup.py --report-failure raises the
#    `backup_failed` admin alert -- a mail to APP_ADMIN_ALERT_EMAILS, a post
#    to APP_ADMIN_ALERT_DISCORD_WEBHOOK and the admin dashboard's bell (or,
#    when the database is what is down, the mail and the post without it).
# 2. When the app cannot (the container is not running), straight to
#    Discord: APP_ADMIN_ALERT_DISCORD_WEBHOOK, else APP_DISCORD_ERROR_WEBHOOK,
#    read from the project's .env -- those two lines only; nothing else in
#    it is read, and nothing is printed from it.
# 3. None of them: an error in the journal (`journalctl -t myotherapp-backup`),
#    and exit 1. Telegram alone is enough for exit 0.
#
# The alert carries the unit's last log lines. The backup and the drill
# print no credential, and URL passwords are masked here as well.
set -uo pipefail

UNIT="${1:?usage: alert.sh <unit>}"
PROJECT_DIR="${MOA_PROJECT_DIR:-/var/www/myotherapp-production}"
COMPOSE_FILE="${MOA_COMPOSE_FILE:-docker-compose.prod.yml}"
SERVICE="${MOA_SERVICE:-backend}"
HOST="$(hostname -f 2> /dev/null || hostname)"

# systemd 251+ passes the failed unit's result to its OnFailure= unit.
RESULT="${MONITOR_SERVICE_RESULT:-failed}"
STATUS="${MONITOR_EXIT_STATUS:-unknown}"

LOG="$(journalctl -u "$UNIT" -n 25 -o cat --no-pager 2> /dev/null \
  | sed -E 's#(://[^/:@[:space:]]*:)[^@/[:space:]]+@#\1***@#g' \
  | tail -c 1500)"

case "$UNIT" in
  *restore-drill*) WHAT="The monthly restore drill" ;;
  *) WHAT="The daily database backup" ;;
esac
TITLE="$WHAT failed on $HOST"
BODY="$UNIT ended with $RESULT (status $STATUS) at $(date -u +%Y-%m-%dT%H:%M:%SZ).
Its last lines (journalctl -u $UNIT):
$LOG

docs/OPERATIONS.md, section 19, says what to check."

cd "$PROJECT_DIR" 2> /dev/null || true

if command -v docker-compose > /dev/null 2>&1; then
  DC=(docker-compose -f "$COMPOSE_FILE")
else
  DC=(docker compose -f "$COMPOSE_FILE")
fi

# Each step at most two minutes: a hung container (or network) must not keep
# the next one from going.
LIMIT=()
if command -v timeout > /dev/null 2>&1; then
  LIMIT=(timeout 120)
fi

# 0. Telegram (the ops bot), when it is there and set up.
TOLD=no
for bot in "$PROJECT_DIR/ops-bot/run.py" "$PROJECT_DIR/ops/server/ops-bot/run.py"; do
  if [ -f "$bot" ]; then
    if printf '%s\n' "$BODY" | ${LIMIT[@]+"${LIMIT[@]}"} python3 -I -B "$bot" notify --title "$TITLE"; then
      TOLD=yes
    fi
    break
  fi
done

# 1. Through the app.
if printf '%s\n' "$BODY" | ${LIMIT[@]+"${LIMIT[@]}"} "${DC[@]}" exec -T "$SERVICE" python scripts/backup.py --report-failure "$TITLE"; then
  exit 0
fi

# 2. Straight to Discord.
env_value() {
  # The last NAME=value line of .env, without surrounding quotes.
  [ -r .env ] || return 0
  grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed -E 's/^["'\'']//; s/["'\'']$//'
}
WEBHOOK="$(env_value APP_ADMIN_ALERT_DISCORD_WEBHOOK)"
[ -n "$WEBHOOK" ] || WEBHOOK="$(env_value APP_DISCORD_ERROR_WEBHOOK)"
if [ -n "$WEBHOOK" ]; then
  PAYLOAD="$(TITLE="$TITLE" BODY="$BODY" python3 -c 'import json, os; print(json.dumps({"content": ("**" + os.environ["TITLE"] + "**\n" + os.environ["BODY"] + "\n(The app could not raise this alert itself, so it is not in the admin dashboard.)")[:1900], "allowed_mentions": {"parse": []}}))')"
  if curl -fsS -m 20 -o /dev/null -H "Content-Type: application/json" -d "$PAYLOAD" "$WEBHOOK"; then
    exit 0
  fi
fi

# 3. Nobody but Telegram, or nobody at all.
if [ "$TOLD" = "yes" ]; then
  exit 0
fi
logger -p user.err -t myotherapp-backup "could not alert anyone: $TITLE"
echo "alert.sh: could not alert anyone: $TITLE" >&2
exit 1
