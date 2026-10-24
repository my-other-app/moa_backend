#!/usr/bin/env bash
# The only thing CI's SSH key can run on the box. setup.sh installs the key
# for the `moa-deploy` user as
#
#   restrict,command="/srv/moa/deploy-gate.sh" ssh-ed25519 AAAA... ci
#
# so whatever the client asks for arrives here in SSH_ORIGINAL_COMMAND, and
# only these get through, each word checked, to `sudo deploy.sh` (sudoers
# allows moa-deploy that one command, nothing else):
#
#   deploy <backend|website|clubs|admin> <tag> [--migrate]
#   rollback <service>
#   recreate <service>
#   status
#   caddy
#   stack              (a tar.gz of ops/server and ops/backup on stdin)
#
# No shell, no port forwarding, no other command: a leaked CI key can deploy
# an image from the registry and nothing more.
# `a && say ok || say problem`: the middle only prints, so it cannot fail.
# shellcheck disable=SC2015
set -euo pipefail

say() { printf 'deploy-gate: %s\n' "$*" >&2; }
refuse() { say "refused: $*"; exit 65; }

cmd="${SSH_ORIGINAL_COMMAND:-}"
[ -n "$cmd" ] || refuse "no command (this key only runs deploy.sh)"
[ "${#cmd}" -le 300 ] || refuse "command too long"
# Words of letters, digits and . _ - only: no quotes, no $, no ; or |.
[[ "$cmd" =~ ^[A-Za-z0-9._\ -]+$ ]] || refuse "unexpected characters"

read -r -a words <<< "$cmd"
service_ok() { [[ "${1:-}" =~ ^(backend|website|clubs|admin)$ ]]; }
tag_ok() { [[ "${1:-}" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; }

case "${words[0]}" in
  deploy)
    service_ok "${words[1]:-}" || refuse "deploy <backend|website|clubs|admin> <tag> [--migrate]"
    tag_ok "${words[2]:-}" || refuse "bad tag"
    args=("${words[1]}" "${words[2]}")
    if [ "${#words[@]}" -eq 4 ]; then
      [ "${words[3]}" = "--migrate" ] || refuse "the only option is --migrate"
      args+=(--migrate)
    elif [ "${#words[@]}" -ne 3 ]; then
      refuse "deploy <service> <tag> [--migrate]"
    fi
    ;;
  rollback | recreate)
    [ "${#words[@]}" -eq 2 ] && service_ok "${words[1]}" || refuse "${words[0]} <backend|website|clubs|admin>"
    args=("${words[0]}" "${words[1]}")
    ;;
  status | caddy | stack)
    [ "${#words[@]}" -eq 1 ] || refuse "${words[0]} takes nothing"
    args=("${words[0]}")
    ;;
  *)
    refuse "unknown command '${words[0]}' (deploy, rollback, recreate, status, caddy, stack)"
    ;;
esac

say "$(id -un) from ${SSH_CLIENT%% *}: ${args[*]}"
logger -t moa-deploy-gate "$(id -un) from ${SSH_CLIENT%% *}: ${args[*]}" 2> /dev/null || true
exec sudo -n /srv/moa/deploy.sh "${args[@]}"
