#!/usr/bin/env bash
# install.sh -- installs (or updates) the Telegram ops bot and the backup
# units it hooks into. Safe to run again: it changes only what differs and
# never touches a filled-in settings file.
#
#   sudo bash ops/server/ops-bot/install.sh        # from a checkout of moa_backend
#   sudo bash /srv/moa/ops-bot/install.sh          # after deploy.sh stack put the files there
#
# What it does:
#   1. installs age (apt) if it is missing -- the backups are encrypted with it
#   2. the bot's files -> /srv/moa/ops-bot (from a checkout; on the box they
#      already are there)
#   3. ops/backup -> /srv/moa/ops/backup (from a checkout)
#   4. /srv/moa/env/ops-bot.env from the template, mode 600, WITHOUT a token,
#      only if there is none yet
#   5. the units -> /etc/systemd/system: moa-ops-bot.service and the backup
#      units (timer at 03:00 IST, delivery to Telegram, alert hook)
#   6. enables the backup timer and the bot; starts the bot only when the
#      settings are complete
#
# It runs nothing in the stack and restarts no container.
set -Eeuo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MOA_DIR="${MOA_DIR:-/srv/moa}"
BOT_DIR="$MOA_DIR/ops-bot"
CONFIG="$MOA_DIR/env/ops-bot.env"
UNITS=/etc/systemd/system

say() { printf '   %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root: sudo bash $0"
[ -d "$MOA_DIR" ] || die "$MOA_DIR does not exist: this is for the box ops/server/setup.sh prepared"
command -v python3 > /dev/null || die "python3 is not installed"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "python3 is older than 3.10"
for f in run.py opsbot/main.py; do
  [ -f "$HERE/$f" ] || die "run this from ops/server/ops-bot (run.py and opsbot/ beside it)"
done

CHANGED_UNITS=no

# Copies $1 to $2 with mode $3 when the content differs; says so.
put() {
  local from="$1" to="$2" mode="$3"
  if [ -f "$to" ] && cmp -s "$from" "$to"; then
    return 1
  fi
  install -D -m "$mode" -o root -g root "$from" "$to.moa-new"
  mv -f "$to.moa-new" "$to"
  say "updated $to"
  return 0
}

# ─── 1. age ──────────────────────────────────────────────────────────────────
step "1. age (encrypts the backups)"
if command -v age > /dev/null; then
  say "age present: $(age --version 2> /dev/null || echo '?')"
else
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends age > /dev/null ||
    { apt-get update -q > /dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends age > /dev/null; } ||
    die "could not install age (apt-get install age)"
  say "age installed: $(age --version 2> /dev/null || echo '?')"
fi

# ─── 2. the bot ──────────────────────────────────────────────────────────────
step "2. the bot's files ($BOT_DIR)"
if [ "$(readlink -f "$HERE")" = "$(readlink -f "$BOT_DIR" 2> /dev/null || echo none)" ]; then
  say "already in place (running from $BOT_DIR)"
else
  install -d -m 755 -o root -g root "$BOT_DIR" "$BOT_DIR/opsbot" "$BOT_DIR/systemd"
  any=no
  files=(run.py install.sh README.md systemd/moa-ops-bot.service)
  for module in "$HERE"/opsbot/*.py; do files+=("opsbot/$(basename "$module")"); done
  for rel in "${files[@]}"; do
    mode=644
    case "$rel" in *.sh) mode=755 ;; esac
    if put "$HERE/$rel" "$BOT_DIR/$rel" "$mode"; then any=yes; fi
  done
  # Modules that no longer exist in the source.
  for old in "$BOT_DIR"/opsbot/*.py; do
    [ -f "$HERE/opsbot/$(basename "$old")" ] || { rm -f "$old"; say "removed $old"; any=yes; }
  done
  [ "$any" = "yes" ] || say "unchanged"
fi
python3 -I -B "$BOT_DIR/run.py" --help > /dev/null || die "$BOT_DIR/run.py does not start"

# ─── 3. ops/backup ───────────────────────────────────────────────────────────
step "3. the backup scripts ($MOA_DIR/ops/backup)"
BACKUP_SRC=""
for candidate in "$HERE/../../backup" "$MOA_DIR/ops/backup"; do
  if [ -f "$candidate/run.sh" ] && [ -d "$candidate/systemd" ]; then
    BACKUP_SRC="$(readlink -f "$candidate")"
    break
  fi
done
[ -n "$BACKUP_SRC" ] || die "no ops/backup found beside this checkout or in $MOA_DIR/ops/backup"
if [ "$BACKUP_SRC" = "$(readlink -f "$MOA_DIR/ops/backup" 2> /dev/null || echo none)" ]; then
  say "already in place"
else
  any=no
  while read -r rel; do
    mode=644
    case "$rel" in *.sh) mode=755 ;; esac
    if put "$BACKUP_SRC/$rel" "$MOA_DIR/ops/backup/$rel" "$mode"; then any=yes; fi
  done < <(cd "$BACKUP_SRC" && find . -type f | sed 's#^\./##')
  [ "$any" = "yes" ] || say "unchanged"
fi
grep -q 'deliver' "$MOA_DIR/ops/backup/run.sh" || die "$MOA_DIR/ops/backup/run.sh has no 'deliver' step: install the current ops/backup"

# ─── 4. settings ─────────────────────────────────────────────────────────────
step "4. settings ($CONFIG)"
install -d -m 700 -o root -g root "$MOA_DIR/env"
TEMPLATE=""
for candidate in "$HERE/../env/ops-bot.env.example" "$MOA_DIR/env/ops-bot.env.example"; do
  [ -f "$candidate" ] && { TEMPLATE="$candidate"; break; }
done
if [ -f "$CONFIG" ]; then
  chown root:root "$CONFIG"
  chmod 600 "$CONFIG"
  say "present (left as it is; mode 600, root)"
else
  [ -n "$TEMPLATE" ] || die "no ops-bot.env.example to start the settings from"
  install -m 600 -o root -g root "$TEMPLATE" "$CONFIG"
  say "created from the template, without a token: fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
fi
if [ -n "$TEMPLATE" ] && [ "$(readlink -f "$TEMPLATE")" != "$(readlink -f "$MOA_DIR/env/ops-bot.env.example")" ]; then
  put "$TEMPLATE" "$MOA_DIR/env/ops-bot.env.example" 644 || true
fi

# ─── 5. units ────────────────────────────────────────────────────────────────
step "5. systemd units"
if [ ! -f /etc/default/myotherapp-backup ]; then
  printf '%s\n' "# ops/server/ops-bot/install.sh: the backup units' paths on this box." \
    "MOA_PROJECT_DIR=$MOA_DIR" "MOA_COMPOSE_FILE=compose.yml" "MOA_SERVICE=backend" \
    "MOA_DRILL_ARGS=--max-age-hours 26" > /etc/default/myotherapp-backup
  say "wrote /etc/default/myotherapp-backup"
else
  grep -q "^MOA_PROJECT_DIR=$MOA_DIR\$" /etc/default/myotherapp-backup ||
    say "NOTE: /etc/default/myotherapp-backup does not say MOA_PROJECT_DIR=$MOA_DIR -- check it"
fi
for unit in "$BOT_DIR/systemd/moa-ops-bot.service" "$MOA_DIR"/ops/backup/systemd/*; do
  if put "$unit" "$UNITS/$(basename "$unit")" 644; then CHANGED_UNITS=yes; fi
done
if [ "$CHANGED_UNITS" = "yes" ]; then
  systemctl daemon-reload
  say "systemd reloaded"
else
  say "unchanged"
fi

# ─── 6. enable ───────────────────────────────────────────────────────────────
step "6. enable"
systemctl enable --quiet myotherapp-backup.timer
if [ "$CHANGED_UNITS" = "yes" ]; then
  # Persistent=true: when today's 03:00 has passed since the last run, this
  # starts one backup now -- the first delivery test.
  systemctl restart myotherapp-backup.timer
fi
say "backup timer on: next $(systemctl show -p NextElapseUSecRealtime --value myotherapp-backup.timer 2> /dev/null || echo '?')"
systemctl enable --quiet moa-ops-bot.service
if python3 -I -B "$BOT_DIR/run.py" check-config > /dev/null 2>&1; then
  systemctl restart moa-ops-bot.service
  sleep 3
  if systemctl is-active --quiet moa-ops-bot.service; then
    say "bot running (a 'started' message goes to the owner's chat)"
  else
    say "bot did NOT stay up: journalctl -u moa-ops-bot -n 30 --no-pager"
  fi
else
  systemctl stop moa-ops-bot.service 2> /dev/null || true
  say "bot enabled, not started: the settings are not complete:"
  python3 -I -B "$BOT_DIR/run.py" check-config 2>&1 | sed 's/^/     /' || true
  say "fill in $CONFIG (sudoedit), then: sudo systemctl restart moa-ops-bot"
fi

step "Done"
say "Check: sudo python3 -I -B $BOT_DIR/run.py test-message    # a message to the owner"
say "       sudo systemctl start myotherapp-backup.service      # a backup now; the .age file arrives in Telegram"
