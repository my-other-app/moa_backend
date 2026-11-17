#!/usr/bin/env bash
# build.sh -- builds the app images on this box, from the sources in
# /srv/moa-src, as production's images are made today (the box is not logged
# in to GHCR, so nothing is pulled from or pushed to it). The box keeps its
# copy at /srv/moa-build/build.sh; this is the repository's.
#
#   build.sh [--no-cache] <app>...      app: backend clubs admin website render
#   NO_CACHE_BUILD=1 build.sh <app>...  the same as --no-cache
#
# Sources: /srv/moa-src/backend, /srv/moa-src/web and /srv/moa-src/events,
# each a `git archive` of one commit, named on ONE line of
# /srv/moa-src/VERSIONS:
#
#     backend=<sha> web=<sha> events=<sha>
#
# Images: ghcr.io/my-other-app/moa-<app>:<sha of its repository> (the names
# images.env and `deploy.sh <app> <sha> --local` use); render is
# moa-render:<RENDER_TAG> (compose.yml's RENDER_IMAGE default).
#
# Build settings, in /srv/moa-build, never in git:
#   <app>.production.env  clubs, admin and website: the NEXT_PUBLIC_* values,
#                         given to the build as the secret `build_env`
#   api_site_token        the website's token for prerendering (secret)
#   sentry_auth_token     source-map upload, when present (secret)
# BuildKit secrets are in no layer, and not in the cache key either: after
# changing one, build that app again with --no-cache.
#
# The Node base of the three web images is pinned by digest here (NODE_IMAGE
# below; the Dockerfiles' own default is the bare tag). The backend's Python
# base is pinned in backend/Dockerfile itself.
#
# Logs: /srv/moa-build/logs/<app>-<time>.log, and one line per app in
# logs/summary.log, then "ALL DONE ..." once every app asked for is done
# (whatever happened: the line says which failed). Exit status 1 when any
# failed. One build at a time. Nothing running is changed: deploy each image
# with deploy.sh <app> <sha> --local.
set -Eeuo pipefail

SRC="${MOA_SRC:-/srv/moa-src}"
BUILD_DIR="${MOA_BUILD_DIR:-/srv/moa-build}"
REGISTRY="${MOA_REGISTRY:-ghcr.io/my-other-app}"
RENDER_TAG="${RENDER_TAG:-8.39.0-1}"
# node:22-alpine on 16 Nov 2026 (Node 22.23.5). Moving it is a deliberate
# change: a new digest here, and the web images rebuilt.
NODE_IMAGE="${MOA_NODE_IMAGE:-node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a}"
LOGS="$BUILD_DIR/logs"
SUMMARY="$LOGS/summary.log"

die() { printf 'build.sh: %s\n' "$*" >&2; exit 1; }
usage() {
  sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
  exit 64
}

no_cache=()
[ "${NO_CACHE_BUILD:-}" = "1" ] && no_cache=(--no-cache)
apps=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-cache) no_cache=(--no-cache) ;;
    -h | --help) usage ;;
    backend | clubs | admin | website | render) apps+=("$1") ;;
    *) die "unknown app or option: $1 (backend, clubs, admin, website, render, --no-cache)" ;;
  esac
  shift
done
[ "${#apps[@]}" -gt 0 ] || usage

command -v docker > /dev/null || die "docker is not installed"
[ -r "$SRC/VERSIONS" ] || die "$SRC/VERSIONS is missing (one line: backend=<sha> web=<sha> events=<sha>)"

# VERSIONS is read, never run: KEY=sha words on its first line.
backend_sha="" web_sha="" events_sha=""
read -r versions < "$SRC/VERSIONS" || true
for word in $versions; do
  case "$word" in
    backend=*) backend_sha="${word#*=}" ;;
    web=*) web_sha="${word#*=}" ;;
    events=*) events_sha="${word#*=}" ;;
  esac
done
sha_ok() { [[ "$1" =~ ^[0-9a-f]{7,40}$ ]]; }

mkdir -p "$LOGS"
exec 9> "$BUILD_DIR/.build.lock"
flock -n 9 || die "another build.sh is running (lock $BUILD_DIR/.build.lock)"

stamp() { date '+%F %T'; }
note() { printf '%s %s\n' "$(stamp)" "$*" >> "$SUMMARY"; }

# The secret flags for a web app; dies when its settings file is missing (an
# image without its NEXT_PUBLIC_* values would go live broken).
web_secrets() {
  local app="$1"
  local env_file="$BUILD_DIR/$app.production.env"
  [ -r "$env_file" ] || die "$env_file is missing: $app's build settings (NEXT_PUBLIC_*)"
  secrets=(--secret "id=build_env,src=$env_file")
  if [ "$app" = "website" ]; then
    [ -r "$BUILD_DIR/api_site_token" ] || die "$BUILD_DIR/api_site_token is missing: the website prerenders with it"
    secrets+=(--secret "id=api_site_token,src=$BUILD_DIR/api_site_token")
  fi
  if [ -r "$BUILD_DIR/sentry_auth_token" ]; then
    secrets+=(--secret "id=sentry_auth_token,src=$BUILD_DIR/sentry_auth_token")
  fi
}

# One app: its arguments for `docker build`, its sha and its tag.
plan() {
  local app="$1"
  secrets=()
  case "$app" in
    backend)
      sha="$backend_sha"
      context="$SRC/backend/backend"
      tag="$REGISTRY/moa-backend:$sha"
      args=(-f "$context/Dockerfile")
      ;;
    render)
      sha="$backend_sha"
      context="$SRC/backend/backend"
      tag="moa-render:$RENDER_TAG"
      args=(-f "$context/render/Dockerfile")
      ;;
    clubs | admin)
      sha="$web_sha"
      context="$SRC/web"
      tag="$REGISTRY/moa-$app:$sha"
      web_secrets "$app"
      args=(-f "$context/Dockerfile" --build-arg "APP=$app" --build-arg "GIT_SHA=$sha"
        --build-arg BUILD_ENVIRONMENT=production --build-arg "NODE_IMAGE=$NODE_IMAGE")
      ;;
    website)
      sha="$events_sha"
      context="$SRC/events"
      tag="$REGISTRY/moa-website:$sha"
      web_secrets website
      args=(-f "$context/Dockerfile" --build-arg "GIT_SHA=$sha"
        --build-arg BUILD_ENVIRONMENT=production --build-arg "NODE_IMAGE=$NODE_IMAGE")
      ;;
  esac
  sha_ok "$sha" || die "$SRC/VERSIONS has no sha for $app"
  [ -f "${args[1]}" ] || die "${args[1]} is missing: is $context the right git archive?"
}

# Every app's sources and settings are checked before anything is built.
for app in "${apps[@]}"; do plan "$app"; done

ok="" failed=""
for app in "${apps[@]}"; do
  plan "$app"
  if [ "$app" = "backend" ]; then
    # /health reports the revision from this file (app/core/observability.py).
    printf '%s\n' "$sha" > "$context/REVISION"
  fi
  log="$LOGS/$app-$(date -u +%Y%m%dT%H%M%SZ).log"
  started=$SECONDS
  attempts=1
  # The backend's apt step fetches a signing key from postgresql.org, which
  # fails now and then; one more try before giving up.
  [ "$app" = "backend" ] && attempts=2
  built=no
  for try in $(seq 1 "$attempts"); do
    echo "== $(stamp) build $app $sha -> $tag (try $try)" >> "$log"
    if docker build ${no_cache[@]+"${no_cache[@]}"} ${secrets[@]+"${secrets[@]}"} "${args[@]}" -t "$tag" "$context" < /dev/null >> "$log" 2>&1; then
      built=yes
      break
    fi
  done
  if [ "$built" = "yes" ]; then
    ok="$ok $app"
    note "$app $sha OK $tag ($((SECONDS - started))s)"
  else
    failed="$failed $app"
    note "$app $sha FAILED ($((SECONDS - started))s; $log)"
  fi
done

note "ALL DONE ok:${ok:- none} failed:${failed:- none}"
tail -n "$((${#apps[@]} + 1))" "$SUMMARY"
[ -z "$failed" ]
