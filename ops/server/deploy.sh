#!/usr/bin/env bash
# deploy.sh -- puts an image live on the Oracle box, checks it, and puts the
# previous one back if it is not healthy. It lives at /srv/moa/deploy.sh and
# runs as root (by hand with sudo, or from CI through deploy-gate.sh).
#
#   deploy.sh <service> <tag> [--migrate] [--no-worker] [--timeout SECONDS]
#       service: backend | website | clubs | admin
#       tag:     an image tag in ghcr.io/my-other-app (the git sha CI pushed,
#                or staging-<sha>, or latest)
#       backend also moves the worker to the same image, once the API is
#       healthy: the worker never runs older code than the API.
#       --migrate (backend only): `alembic upgrade head` with the NEW image,
#       before it takes traffic. Without it, an image whose migrations the
#       database lacks is refused before anything changes.
#       --no-worker (backend only): leave the worker stopped (a rehearsal on a
#       copy of production must not send its queued mails and pushes).
#   deploy.sh rollback <service>     the image that ran before the last deploy
#   deploy.sh recreate <service>     the same image, a new container (applies
#                                    .env / env/*.env edits); backend: + worker
#   deploy.sh status                 images, health, readiness, disk, memory
#   deploy.sh caddy                  validate the Caddyfile, then reload it
#   deploy.sh stack                  install ops/server (+ ops/backup) from a
#                                    tar.gz on stdin (CI's "stack" deploy)
#   deploy.sh start                  start everything, in order (first boot)
#   deploy.sh doctor                 check files, settings and mounts; names
#                                    only, never a value
#
# Every image runs pinned by digest (images.env), so a rollback is exact even
# when a tag such as `latest` has moved since. Prints MOA_DEPLOY_OK last when
# everything it was asked to do worked (CI looks for it).
#
# Never syncs, prints or rewrites .env: it is edited by hand on the server.
# `a && say ok || say problem`: the middle only prints, so it cannot fail.
# shellcheck disable=SC2015
set -Eeuo pipefail

MOA_DIR="${MOA_DIR:-/srv/moa}"
CONF="$MOA_DIR/deploy.conf"
IMAGES="$MOA_DIR/images.env"
STATE="$MOA_DIR/state"
HISTORY="$STATE/history.log"

# Settings a box may change in deploy.conf (KEY=value lines; nothing in it is
# run as shell).
conf() {
  local key="$1" default="$2" value=""
  if [ -r "$CONF" ]; then
    value="$(grep -E "^${key}=" "$CONF" | tail -n 1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${value:-$default}"
}
REGISTRY="$(conf MOA_REGISTRY ghcr.io/my-other-app)"
HEALTH_TIMEOUT="$(conf HEALTH_TIMEOUT 180)"
KEEP_IMAGES="$(conf KEEP_IMAGES 3)"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '%s WARNING: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { printf '%s ERROR: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

# docker compose for this stack: .env for the settings, images.env for the
# images. A variable exported in the environment wins over both, which is how
# a candidate image is tried before images.env is changed.
dc() {
  docker compose --project-directory "$MOA_DIR" -f "$MOA_DIR/compose.yml" \
    --env-file "$MOA_DIR/.env" --env-file "$IMAGES" "$@"
}

# service -> image name, the images.env variable, and the containers it moves.
image_name() {
  case "$1" in
    backend) echo moa-backend ;;
    website) echo moa-website ;;
    clubs) echo moa-clubs ;;
    admin) echo moa-admin ;;
    *) return 1 ;;
  esac
}
image_var() {
  case "$1" in
    backend) echo BACKEND_IMAGE ;;
    website) echo WEBSITE_IMAGE ;;
    clubs) echo CLUBS_IMAGE ;;
    admin) echo ADMIN_IMAGE ;;
    *) return 1 ;;
  esac
}

valid_service() { image_name "$1" > /dev/null 2>&1; }
valid_tag() { [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; }
# A full reference as images.env and history.log hold them.
valid_ref() { [[ "$1" =~ ^[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(@sha256:[0-9a-f]{64})?$ ]]; }

current_ref() {
  local var="$1"
  [ -r "$IMAGES" ] || return 0
  grep -E "^${var}=" "$IMAGES" | tail -n 1 | cut -d= -f2- || true
}

set_ref() {
  local var="$1" ref="$2" tmp
  valid_ref "$ref" || die "refusing to write an invalid image reference for $var"
  tmp="$(mktemp "$IMAGES.XXXXXX")"
  {
    echo "# Written by deploy.sh: the image each app runs, pinned by digest."
    echo "# Never edit by hand; deploy.sh <service> <tag> changes it."
    for v in BACKEND_IMAGE WEBSITE_IMAGE CLUBS_IMAGE ADMIN_IMAGE; do
      if [ "$v" = "$var" ]; then
        echo "$v=$ref"
      else
        local old
        old="$(current_ref "$v")"
        if [ -n "$old" ]; then echo "$v=$old"; fi
      fi
    done
  } > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$IMAGES"
}

record() {
  # time service result new old
  mkdir -p "$STATE"
  printf '%s %s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "${4:--}" >> "$HISTORY"
}

container_of() { dc ps -q "$1" 2> /dev/null | head -n 1; }

# Waits for a container's own healthcheck to say healthy. Without a
# healthcheck (the worker), it must stay up, unrestarted, for 20 s.
wait_healthy() {
  local service="$1" timeout="$2" id status state restarts started=$SECONDS settle=20
  [ "$timeout" -gt "$settle" ] || settle=$((timeout > 1 ? timeout - 1 : 1))
  while :; do
    id="$(container_of "$service")"
    if [ -n "$id" ]; then
      read -r state restarts < <(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$id" 2> /dev/null || echo "missing 0")
      # Crashed since it was created (the restart policy brings it back, so
      # it would otherwise only show as "starting" until the timeout).
      if [ "$state" = "exited" ] || [ "$state" = "dead" ] || [ "${restarts:-0}" != "0" ]; then
        return 1
      fi
      status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2> /dev/null || echo missing)"
      case "$status" in
        healthy) return 0 ;;
        # Past its start period and failing its checks.
        unhealthy) return 1 ;;
        none)
          [ "$state" = "running" ] && [ $((SECONDS - started)) -ge "$settle" ] && return 0
          ;;
      esac
    fi
    if [ $((SECONDS - started)) -ge "$timeout" ]; then
      return 1
    fi
    sleep 3
  done
}

# The API's readiness from inside its container: /health/ready (once the
# hardening PR is in), else /readyz, else /healthz. 0 when it answers 200.
api_ready() {
  local timeout="$1" started=$SECONDS out
  while :; do
    if out="$(dc exec -T backend python -c '
import sys, urllib.request, urllib.error
for path in ("/health/ready", "/readyz", "/healthz"):
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5)
        print(path, r.status, r.read(600).decode("utf-8", "replace"))
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            continue
        print(path, e.code, e.read(600).decode("utf-8", "replace"))
        sys.exit(1)
    except Exception as e:
        print(path, type(e).__name__)
        sys.exit(1)
sys.exit(1)
' < /dev/null 2>&1)"; then
      log "ready: $out"
      return 0
    fi
    if [ $((SECONDS - started)) -ge "$timeout" ]; then
      warn "not ready after ${timeout}s: $out"
      return 1
    fi
    sleep 5
  done
}

# Does the database lack migrations this image has? 0 when they agree, 3 when
# the image is ahead, anything else when it could not tell.
migrations_agree() {
  local var="$1" ref="$2"
  env "$var=$ref" docker compose --project-directory "$MOA_DIR" -f "$MOA_DIR/compose.yml" \
    --env-file "$MOA_DIR/.env" --env-file "$IMAGES" \
    run --rm --no-deps -T backend python -c '
import os, sys
import sqlalchemy as sa
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
config = Config("alembic.ini")
config.set_main_option("script_location", "migrations")
code = set(ScriptDirectory.from_config(config).get_heads())
engine = sa.create_engine(os.environ["APP_DATABASE_URL_SYNC"])
with engine.connect() as conn:
    db = set(MigrationContext.configure(conn).get_current_heads())
print("migrations: database", sorted(db) or "-", "/ image", sorted(code))
sys.exit(0 if db == code else 3)
' < /dev/null
}

# The visitor's view through Caddy (TLS and routing), from the box itself.
# A warning only: the container is healthy by then.
through_caddy() {
  local service="$1" host path
  case "$service" in
    backend) host="$(env_value MOA_API_HOST api.myotherapp.in)"; path=/healthz ;;
    website) host="$(env_value MOA_SITE_HOST myotherapp.in)"; path=/favicon.ico ;;
    clubs) host="$(env_value MOA_CLUBS_HOST club-dashboard.myotherapp.in)"; path=/brand/icon.png ;;
    admin) host="$(env_value MOA_ADMIN_HOST main-portal.myotherapp.in)"; path=/brand/icon.png ;;
  esac
  local code
  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve "$host:443:127.0.0.1" "https://$host$path" || true)"
  code="${code:-000}"
  if [ "$code" = "200" ]; then
    log "through Caddy: https://$host$path 200"
  else
    warn "through Caddy: https://$host$path answered $code (the container is healthy: check caddy, certs/ and the Caddyfile)"
  fi
}

# A non-secret setting from .env (host names), else the default.
env_value() {
  local value=""
  value="$(grep -E "^$1=" "$MOA_DIR/.env" 2> /dev/null | tail -n 1 | cut -d= -f2- | tr -d '"'"'" || true)"
  printf '%s' "${value:-$2}"
}

pull() {
  local ref="$1" n
  for n in 1 2 3; do
    if docker pull -q "$ref" < /dev/null > /dev/null; then
      return 0
    fi
    warn "pull $ref failed (attempt $n of 3)"
    sleep $((n * 5))
  done
  return 1
}

# name:tag@sha256:... for what was just pulled, so images.env is exact.
pinned() {
  local ref="$1" repo="${1%%@*}" digest
  repo="${repo%:*}"
  [[ "$ref" == *@sha256:* ]] && { echo "$ref"; return; }
  digest="$(docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" | grep -E "^${repo}@sha256:" | head -n 1 | cut -d@ -f2 || true)"
  if [ -n "$digest" ]; then
    echo "$ref@$digest"
  else
    echo "$ref"
  fi
}

show_logs() {
  local service="$1"
  warn "last lines of $service:"
  dc logs --no-color --tail 40 "$service" < /dev/null >&2 || true
}

# Swap one service to `ref`; 0 when healthy.
swap() {
  local service="$1" var="$2" ref="$3" timeout="$4"
  log "recreating $service on ${ref##*/}"
  env "$var=$ref" docker compose --project-directory "$MOA_DIR" -f "$MOA_DIR/compose.yml" \
    --env-file "$MOA_DIR/.env" --env-file "$IMAGES" \
    up -d --no-deps --force-recreate "$service" < /dev/null
  if ! wait_healthy "$service" "$timeout"; then
    show_logs "$service"
    return 1
  fi
  if [ "$service" = "backend" ] && ! api_ready "$timeout"; then
    show_logs "$service"
    return 1
  fi
  log "$service healthy"
}

# Keep the newest KEEP_IMAGES images of a repository (the running one and the
# rollback target among them); a running one is never removed.
prune() {
  local repo="$1"
  docker image ls --format '{{.ID}}' "$repo" | awk '!seen[$0]++' | tail -n +"$((KEEP_IMAGES + 1))" |
    while read -r id; do docker image rm "$id" > /dev/null 2>&1 || true; done
  docker image prune -f > /dev/null 2>&1 || true
}

deploy() {
  local service="$1" ref="$2" migrate="$3" timeout="$4" reason="${5:-deploy}" worker="${6:-yes}"
  local var name previous new
  var="$(image_var "$service")"
  name="$(image_name "$service")"
  previous="$(current_ref "$var")"

  log "pulling $ref"
  pull "$ref" || die "could not pull $ref (is the package public, or the box logged in to ghcr.io?)"
  new="$(pinned "$ref")"
  valid_ref "$new" || die "unexpected image reference: $new"

  if [ "$service" = "backend" ]; then
    if [ "$migrate" = "yes" ]; then
      log "migrating with the new image (the running API keeps serving)"
      env "$var=$new" docker compose --project-directory "$MOA_DIR" -f "$MOA_DIR/compose.yml" \
        --env-file "$MOA_DIR/.env" --env-file "$IMAGES" \
        run --rm --no-deps -T backend alembic upgrade head < /dev/null ||
        die "migration failed; nothing was swapped (docs/OPERATIONS.md, section 3)"
      record backend migrated "$new" "$previous"
    else
      local agree=0
      migrations_agree "$var" "$new" || agree=$?
      if [ "$agree" = "3" ]; then
        die "this image has migrations the database lacks: deploy again with --migrate. Nothing was changed."
      elif [ "$agree" != "0" ]; then
        die "could not compare the migrations (is postgres up?). Nothing was changed."
      fi
    fi
  fi

  if swap "$service" "$var" "$new" "$timeout"; then
    set_ref "$var" "$new"
    record "$service" ok "$new" "$previous"
  else
    record "$service" failed "$new" "$previous"
    if [ -n "$previous" ] && [ "$previous" != "$new" ]; then
      warn "$service is not healthy on the new image: putting ${previous##*/} back"
      [ "$migrate" = "yes" ] && warn "the migration stays applied: the previous code must cope with the new schema (they are written to)"
      if swap "$service" "$var" "$previous" "$timeout"; then
        record "$service" rolled-back "$previous" "$new"
        die "$service rolled back to ${previous##*/}"
      fi
      die "$service is unhealthy on both images: see the logs above, and docs/MOVE_TO_ORACLE.md, Rollback"
    fi
    die "$service is unhealthy and there is no previous image to go back to"
  fi

  if [ "$service" = "backend" ] && [ "$worker" = "no" ]; then
    dc stop worker < /dev/null > /dev/null 2>&1 || true
    warn "the worker is stopped (--no-worker): no jobs or sweeps run until 'deploy.sh recreate backend'"
  elif [ "$service" = "backend" ]; then
    if ! swap worker "$var" "$new" "$timeout"; then
      record worker failed "$new" "$previous"
      if [ -n "$previous" ] && [ "$previous" != "$new" ]; then
        warn "the worker is not healthy on the new image: putting the API and the worker back on ${previous##*/}"
        swap backend "$var" "$previous" "$timeout" || true
        swap worker "$var" "$previous" "$timeout" || true
        set_ref "$var" "$previous"
        record backend rolled-back "$previous" "$new"
        die "backend and worker rolled back to ${previous##*/}"
      fi
      die "the worker is unhealthy and there is no previous image to go back to"
    fi
    record worker ok "$new" "$previous"
  fi

  through_caddy "$service"
  prune "$REGISTRY/$name"
  log "$reason of $service done: ${new##*/}"
}

cmd_deploy() {
  local service="$1" tag="$2"
  shift 2
  local migrate=no timeout="$HEALTH_TIMEOUT" worker=yes
  while [ $# -gt 0 ]; do
    case "$1" in
      --migrate) migrate=yes ;;
      --no-worker) worker=no ;;
      --timeout)
        [ $# -ge 2 ] && [[ "$2" =~ ^[0-9]{1,4}$ ]] || die "--timeout takes seconds"
        timeout="$2"
        shift
        ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
  valid_service "$service" || die "unknown service: $service (backend, website, clubs, admin)"
  valid_tag "$tag" || die "not an image tag: $tag"
  [ "$migrate" = "yes" ] && [ "$service" != "backend" ] && die "--migrate is for the backend only"
  [ "$worker" = "no" ] && [ "$service" != "backend" ] && die "--no-worker is for the backend only"
  preflight
  deploy "$service" "$REGISTRY/$(image_name "$service"):$tag" "$migrate" "$timeout" deploy "$worker"
}

cmd_rollback() {
  local service="$1" line target
  valid_service "$service" || die "unknown service: $service"
  preflight
  # The last good deploy of this service, and what ran before it. (No
  # history.log yet is "nothing on record": under pipefail awk's failure
  # ended the script there, silently, before it could say so.)
  line="$(awk -v s="$service" '$2 == s && $3 == "ok"' "$HISTORY" 2> /dev/null | tail -n 1 || true)"
  target="$(printf '%s' "$line" | awk '{print $5}')"
  [ -n "$target" ] && [ "$target" != "-" ] || die "no earlier image of $service on record"
  valid_ref "$target" || die "history.log holds an invalid reference for $service"
  log "rolling $service back to ${target##*/}"
  deploy "$service" "$target" no "$HEALTH_TIMEOUT" rollback
}

cmd_recreate() {
  local service="$1" var ref
  valid_service "$service" || die "unknown service: $service"
  preflight
  var="$(image_var "$service")"
  ref="$(current_ref "$var")"
  [ -n "$ref" ] || die "images.env names no image for $service yet: deploy it first"
  swap "$service" "$var" "$ref" "$HEALTH_TIMEOUT" || die "$service is not healthy after the recreate"
  if [ "$service" = "backend" ]; then
    swap worker "$var" "$ref" "$HEALTH_TIMEOUT" || die "the worker is not healthy after the recreate"
  fi
  record "$service" recreated "$ref" "$ref"
}

cmd_status() {
  echo "== images (images.env)"
  grep -E '^[A-Z_]+=' "$IMAGES" 2> /dev/null | sed 's/^/  /' || echo "  (none yet)"
  echo "== containers"
  dc ps --format 'table {{.Service}}\t{{.Status}}\t{{.Image}}' < /dev/null || true
  echo "== API readiness"
  if [ -n "$(container_of backend)" ]; then api_ready 5 || true; else echo "  backend not running"; fi
  echo "== memory and swap"
  free -m | sed 's/^/  /'
  echo "== disk"
  df -h / /srv/moa-data 2> /dev/null | sed 's/^/  /' || df -h / | sed 's/^/  /'
  docker stats --no-stream --format '  {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' < /dev/null || true
  echo "== last deploys"
  tail -n 10 "$HISTORY" 2> /dev/null | sed 's/^/  /' || echo "  (none)"
}

cmd_caddy() {
  local file
  file="$(env_value MOA_CADDYFILE Caddyfile)"
  [[ "$file" =~ ^Caddyfile(\.[a-z]+)?$ ]] || die "MOA_CADDYFILE must be Caddyfile or Caddyfile.<name>"
  log "validating caddy/$file"
  dc exec -T caddy caddy validate --config "/etc/caddy/$file" --adapter caddyfile < /dev/null > /dev/null ||
    die "caddy/$file does not validate; nothing was reloaded"
  dc exec -T caddy caddy reload --config "/etc/caddy/$file" --adapter caddyfile --force < /dev/null
  log "caddy reloaded"
}

# Install ops/server (and ops/backup when present) from a tar.gz on stdin.
# Validates before touching anything; keeps the files it replaces in
# state/stack-<time>.tgz. Recreates nothing: Caddy is reloaded if its files
# changed, and services whose compose definition changed are listed for a
# `recreate` (or the next deploy).
cmd_stack() {
  [ -t 0 ] && die "stack reads a tar.gz on stdin (CI sends it; by hand: tar -czf - ops/server ops/backup | sudo deploy.sh stack)"
  local tmp new
  tmp="$(mktemp -d)"
  STACK_TMP="$tmp"
  trap 'rm -rf "${STACK_TMP:-/nonexistent}"' EXIT
  head -c 20000000 > "$tmp/stack.tgz"
  mkdir "$tmp/x"
  tar -xzf "$tmp/stack.tgz" -C "$tmp/x" --no-same-owner --no-same-permissions
  new="$tmp/x/ops/server"
  for f in compose.yml deploy.sh dc deploy-gate.sh caddy/Caddyfile caddy/global.caddy caddy/snippets.caddy; do
    [ -f "$new/$f" ] || die "the tarball has no ops/server/$f"
  done
  if find "$tmp/x" -type l | grep -q .; then die "the tarball holds symlinks"; fi
  bash -n "$new/deploy.sh" || die "the new deploy.sh does not parse"
  bash -n "$new/deploy-gate.sh" || die "the new deploy-gate.sh does not parse"
  docker compose --project-directory "$MOA_DIR" -f "$new/compose.yml" --env-file "$MOA_DIR/.env" --env-file "$IMAGES" config -q ||
    die "the new compose.yml does not validate against this box's .env"

  local file
  file="$(env_value MOA_CADDYFILE Caddyfile)"
  dc run --rm --no-deps -T -v "$new/caddy:/etc/caddy-new:ro" caddy \
    caddy validate --config "/etc/caddy-new/$file" --adapter caddyfile < /dev/null > /dev/null ||
    die "the new caddy/$file does not validate"

  local before after
  before="$(dc config --hash '*' 2> /dev/null | sort || true)"
  mkdir -p "$STATE"
  local keep=() f
  for f in compose.yml caddy deploy.sh dc deploy-gate.sh setup.sh cloudflare-ips.sh README.md env ops; do
    [ -e "$MOA_DIR/$f" ] && keep+=("$f")
  done
  # env/ holds the apps' settings: the copy is root's alone, like the original.
  (umask 077 && tar -czf "$STATE/stack-$(date -u +%Y%m%dT%H%M%SZ).tgz" -C "$MOA_DIR" "${keep[@]}") || warn "could not keep a copy of the old stack files"

  local caddy_changed=no
  diff -rq "$new/caddy" "$MOA_DIR/caddy" > /dev/null 2>&1 || caddy_changed=yes
  install_tree "$new" "$MOA_DIR"
  if [ -d "$tmp/x/ops/backup" ]; then
    mkdir -p "$MOA_DIR/ops"
    install_tree "$tmp/x/ops/backup" "$MOA_DIR/ops/backup"
  fi
  after="$(dc config --hash '*' 2> /dev/null | sort || true)"
  log "stack installed (previous files in $STATE)"
  if [ "$caddy_changed" = "yes" ]; then cmd_caddy; fi
  local changed
  changed="$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") | awk '/^>/ {print $2}' | sort -u | tr '\n' ' ')"
  if [ -n "${changed// /}" ]; then
    warn "compose definitions changed for: $changed-- applied when each is next recreated (deploy.sh recreate <service>; postgres, redis, meilisearch: ./dc up -d <service>)"
  fi
}

# Copy a directory over another: files replaced by rename (a running
# deploy.sh keeps reading its old copy), modes set, nothing else removed.
install_tree() {
  local from="$1" to="$2" rel mode
  (cd "$from" && find . -type f) | while read -r rel; do
    rel="${rel#./}"
    case "$rel" in
      *.sh | dc) mode=755 ;;
      *) mode=644 ;;
    esac
    mkdir -p "$(dirname "$to/$rel")"
    install -m "$mode" -o root -g root "$from/$rel" "$to/$rel.new"
    mv -f "$to/$rel.new" "$to/$rel"
  done
}

cmd_start() {
  preflight
  log "pulling every image"
  dc pull -q < /dev/null
  log "starting postgres, redis, meilisearch"
  dc up -d postgres redis meilisearch < /dev/null
  wait_healthy postgres 120 || die "postgres did not become healthy"
  dc up -d backend < /dev/null
  wait_healthy backend "$HEALTH_TIMEOUT" || { show_logs backend; die "backend did not become healthy"; }
  api_ready "$HEALTH_TIMEOUT" || warn "the API is up but not ready (migrations? deploy.sh backend <tag> --migrate)"
  dc up -d worker website clubs admin < /dev/null
  for s in worker website clubs admin; do
    wait_healthy "$s" "$HEALTH_TIMEOUT" || warn "$s is not healthy yet: ./dc logs $s"
  done
  dc up -d caddy < /dev/null
  wait_healthy caddy 60 || warn "caddy is not healthy yet: ./dc logs caddy"
  # Pin what is running, so later deploys can roll back to it.
  local s var id ref
  for s in backend website clubs admin; do
    var="$(image_var "$s")"
    id="$(container_of "$s")"
    [ -n "$id" ] || continue
    ref="$(pinned "$(docker inspect -f '{{.Config.Image}}' "$id")")"
    valid_ref "$ref" && set_ref "$var" "$ref"
  done
  cmd_status
}

doctor_line() { printf '  %-5s %s\n' "$1" "$2"; }

cmd_doctor() {
  local fails=0 f
  echo "== files in $MOA_DIR"
  for f in compose.yml images.env deploy.sh dc caddy/global.caddy caddy/snippets.caddy; do
    if [ -e "$MOA_DIR/$f" ]; then doctor_line ok "$f"; else doctor_line FAIL "$f missing"; fails=$((fails + 1)); fi
  done
  if [ -f "$MOA_DIR/.env" ]; then
    local m
    m="$(stat -c '%a %U' "$MOA_DIR/.env")"
    if [ "$m" = "600 root" ]; then doctor_line ok ".env (600 root)"; else doctor_line WARN ".env is $m: chmod 600, chown root"; fi
    for f in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB APP_SECRET_KEY MEILI_MASTER_KEY APP_CORS_ORIGINS; do
      if grep -qE "^$f=.+" "$MOA_DIR/.env"; then doctor_line ok ".env sets $f"; else doctor_line FAIL ".env lacks $f"; fails=$((fails + 1)); fi
    done
    local key
    key="$(grep -E '^MEILI_MASTER_KEY=' "$MOA_DIR/.env" | tail -n 1 | cut -d= -f2- || true)"
    [ "${#key}" -ge 16 ] || doctor_line WARN "MEILI_MASTER_KEY is shorter than 16 bytes: Meilisearch will not start"
    grep -qE '^APP_FIREBASE_SERVICE_ACCOUNT_PATH=/backend/' "$MOA_DIR/.env" &&
      doctor_line WARN "APP_FIREBASE_SERVICE_ACCOUNT_PATH points into /backend: the image has no such file; copy it to secrets/ and use /secrets/..."
  else
    doctor_line FAIL ".env missing (the old box's file; docs/MOVE_TO_ORACLE.md, step 3)"
    fails=$((fails + 1))
  fi
  local caddyfile
  caddyfile="$(env_value MOA_CADDYFILE Caddyfile)"
  [ -f "$MOA_DIR/caddy/$caddyfile" ] && doctor_line ok "caddy/$caddyfile" || { doctor_line FAIL "caddy/$caddyfile missing"; fails=$((fails + 1)); }
  for f in certs/origin.pem certs/origin.key; do
    [ -s "$MOA_DIR/$f" ] && doctor_line ok "$f" || { doctor_line FAIL "$f missing (Cloudflare Origin CA certificate)"; fails=$((fails + 1)); }
  done
  [ -f "$MOA_DIR/certs/origin.key" ] && [ "$(stat -c '%a' "$MOA_DIR/certs/origin.key")" != "600" ] && doctor_line WARN "certs/origin.key should be mode 600"
  if ls "$MOA_DIR"/secrets/walletethings/*.pem > /dev/null 2>&1; then doctor_line ok "secrets/walletethings/*.pem"; else doctor_line WARN "secrets/walletethings has no .pem: Apple Wallet passes will fail"; fi
  [ -f "$MOA_DIR/env/website.env" ] && doctor_line ok "env/website.env" || doctor_line WARN "env/website.env missing: no REVALIDATE_SECRET or API_SITE_TOKEN for the site"
  for f in REVALIDATE_SECRET API_SITE_TOKEN; do
    grep -qE "^$f=.+" "$MOA_DIR/env/website.env" 2> /dev/null || doctor_line WARN "env/website.env lacks $f"
  done
  echo "== compose"
  if dc config -q < /dev/null 2> /dev/null; then doctor_line ok "compose.yml validates"; else doctor_line FAIL "compose.yml does not validate (./dc config)"; fails=$((fails + 1)); fi
  echo "== host"
  swapon --show=NAME,SIZE --noheadings | grep -q . && doctor_line ok "swap: $(swapon --show=SIZE --noheadings | head -n 1 | tr -d ' ')" || doctor_line WARN "no swap"
  local avail
  avail="$(df -P "$MOA_DIR" | awk 'NR == 2 {print int($4 / 1048576)}')"
  [ "$avail" -ge 10 ] && doctor_line ok "disk: ${avail} GB free" || doctor_line WARN "disk: only ${avail} GB free"
  [ -d /var/backups/myotherapp ] && doctor_line ok "/var/backups/myotherapp" || doctor_line WARN "/var/backups/myotherapp missing"
  systemctl is-active --quiet myotherapp-backup.timer 2> /dev/null && doctor_line ok "backup timer active" || doctor_line WARN "backup timer not active (ops/backup; setup.sh installs it once the files are here)"
  if [ -n "$(container_of backend)" ]; then
    echo "== inside the API container"
    dc exec -T backend python -c '
import os
def line(level, text): print(f"  {level:<5} {text}")
p = os.environ.get("APP_FIREBASE_SERVICE_ACCOUNT_PATH", "")
if p: line("ok" if os.path.isfile(p) else "FAIL", f"APP_FIREBASE_SERVICE_ACCOUNT_PATH -> {p}" + ("" if os.path.isfile(p) else " is missing"))
elif os.environ.get("APP_FIREBASE_SERVICE_ACCOUNT_BASE64"): line("ok", "Firebase credentials from APP_FIREBASE_SERVICE_ACCOUNT_BASE64")
else: line("WARN", "no Firebase credentials: no push notifications")
w = os.environ.get("WALLET_CERTS_DIR", "")
names = ("pass_certificate_new.pem", "pass_key_new.pem", "wwdr.pem")
missing = [n for n in names if not os.path.isfile(os.path.join(w, n))]
line("ok" if not missing else "WARN", f"wallet certificates in {w}" + (": missing " + ", ".join(missing) if missing else ""))
g = os.environ.get("APP_GEOIP_MMDB_PATH", "")
if g: line("ok" if os.path.isfile(g) else "WARN", f"GeoLite2 at {g}" + ("" if os.path.isfile(g) else " is missing"))
for k in ("APP_REDIS_URL", "APP_SEARCH_URL", "APP_WORKERS", "FORWARDED_ALLOW_IPS"):
    line("ok", f"{k} is set" if os.environ.get(k) else f"{k} is empty")
for k in ("APP_JOBS_WORKER", "APP_SWEEPS_IN_WORKER"):
    v = os.environ.get(k, "").lower()
    shown = v or "(unset)"
    if v == "true":
        line("ok", f"{k}={shown}")
    else:
        line("WARN", f"{k}={shown}: the API runs jobs and sweeps itself; on this box the worker should (drop that line from .env)")
' < /dev/null || true
  fi
  echo
  if [ "$fails" -gt 0 ]; then
    echo "$fails problem(s) to fix first."
    return 1
  fi
  echo "No blocking problems."
}

preflight() {
  [ "$(id -u)" = "0" ] || die "run as root (sudo)"
  command -v docker > /dev/null || die "docker is not installed (setup.sh)"
  docker compose version > /dev/null 2>&1 || die "the docker compose plugin is not installed (setup.sh)"
  [ -f "$MOA_DIR/compose.yml" ] || die "$MOA_DIR/compose.yml is missing"
  [ -f "$MOA_DIR/.env" ] || die "$MOA_DIR/.env is missing (docs/MOVE_TO_ORACLE.md, step 3)"
  [ -f "$IMAGES" ] || : > "$IMAGES"
  mkdir -p "$STATE"
  chmod 700 "$STATE"
}

main() {
  [ $# -ge 1 ] || usage
  case "$1" in -h | --help | help) usage ;; esac
  [ "$(id -u)" = "0" ] || die "run as root (sudo)"
  cd "$MOA_DIR"
  mkdir -p "$STATE"
  # One at a time: CI and a person must never deploy over each other.
  exec 9> "$STATE/deploy.lock"
  flock -w 900 9 || die "another deploy.sh has held the lock for 15 minutes"

  local verb="$1"
  shift
  case "$verb" in
    backend | website | clubs | admin)
      [ $# -ge 1 ] || usage
      cmd_deploy "$verb" "$@"
      ;;
    rollback)
      [ $# -eq 1 ] || usage
      cmd_rollback "$1"
      ;;
    recreate)
      [ $# -eq 1 ] || usage
      cmd_recreate "$1"
      ;;
    status) cmd_status ;;
    caddy) preflight && cmd_caddy ;;
    stack) preflight && cmd_stack ;;
    start) cmd_start ;;
    doctor) cmd_doctor ;;
    -h | --help | help) usage ;;
    *) usage ;;
  esac
  echo "MOA_DEPLOY_OK"
}

main "$@"
