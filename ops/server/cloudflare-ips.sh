#!/usr/bin/env bash
# Checks the Cloudflare ranges Caddy trusts (caddy/global.caddy) against the
# ones Cloudflare publishes. Exit 0 when they agree, 1 with the difference
# when they do not (then edit the trusted_proxies line, and the OCI security
# list if it is limited to Cloudflare; docs/MOVE_TO_ORACLE.md, "Hardening").
#
#   ops/server/cloudflare-ips.sh              # check
#   ops/server/cloudflare-ips.sh --print      # the published ranges, one a line
#
# Read-only: fetches two public text files, changes nothing.
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
global="$here/caddy/global.caddy"

published() {
  {
    curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4
    echo
    curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6
    echo
  } | tr -d '\r' | grep -E '^[0-9a-f:.]+/[0-9]+$' | sort -u
}

if [ "${1:-}" = "--print" ]; then
  published
  exit 0
fi

ours="$(grep -E '^\s*trusted_proxies static ' "$global" | tr -s '[:space:]' '\n' | grep -E '^[0-9a-f:.]+/[0-9]+$' | sort -u)"
theirs="$(published)"
[ -n "$theirs" ] || { echo "could not fetch Cloudflare's ranges" >&2; exit 2; }
if [ "$ours" = "$theirs" ]; then
  echo "caddy/global.caddy trusts exactly Cloudflare's $(printf '%s\n' "$theirs" | wc -l | tr -d ' ') published ranges."
  exit 0
fi
echo "caddy/global.caddy and Cloudflare's published ranges differ (< ours, > Cloudflare's):"
diff <(printf '%s\n' "$ours") <(printf '%s\n' "$theirs") || true
exit 1
