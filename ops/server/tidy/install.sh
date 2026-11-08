#!/bin/bash
# Installs the nightly Docker tidy. Idempotent: run it again after a change.
#
#   sudo bash ops/server/tidy/install.sh     # from a checkout of moa_backend
#
#   1. tidy.sh -> /srv/moa/tidy/tidy.sh (root, 755)
#   2. moa-tidy.service and moa-tidy.timer -> /etc/systemd/system
#   3. the timer enabled and started (it does not run the tidy now; start
#      moa-tidy.service for that)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MOA_DIR=${MOA_DIR:-/srv/moa}
UNITS=/etc/systemd/system

die() { echo "install: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
for f in tidy.sh moa-tidy.service moa-tidy.timer; do
  [ -f "$HERE/$f" ] || die "missing $HERE/$f"
done
bash -n "$HERE/tidy.sh" || die "tidy.sh does not parse"

install -d -m 755 "$MOA_DIR/tidy"
install -m 755 "$HERE/tidy.sh" "$MOA_DIR/tidy/tidy.sh"
install -m 644 "$HERE/moa-tidy.service" "$UNITS/moa-tidy.service"
install -m 644 "$HERE/moa-tidy.timer" "$UNITS/moa-tidy.timer"
systemctl daemon-reload
systemctl enable --now moa-tidy.timer > /dev/null

echo "installed: $MOA_DIR/tidy/tidy.sh, moa-tidy.service, moa-tidy.timer"
systemctl list-timers moa-tidy.timer --no-pager | head -2
