#!/bin/bash
# Gives back the disk Docker fills on this box, every night (moa-tidy.timer).
#
#   sudo /srv/moa/tidy/tidy.sh             tidy now, and say what went
#   sudo /srv/moa/tidy/tidy.sh --dry-run   say what would go; change nothing
#
# Two things grow without end here:
#   * the build cache. Every image built on the box (build.sh, while CI images
#     are not available) leaves its layers behind, and a --no-cache build never
#     reuses them: 47 GB in the first 11 days. Cut to the most recently used
#     KEEP_CACHE (default 5GB), so the next build still finds its base layers.
#   * old app images (ghcr.io/my-other-app/moa-*). Kept per app: the image
#     images.env runs, whatever any container uses, the :local tag, and the
#     newest others up to KEEP_IMAGES versions in all (default 3, as
#     deploy.sh's KEEP_IMAGES: the running one and two to roll back to).
#     deploy.sh prunes the same way after its own deploys; this also covers
#     images switched by hand and builds that were never deployed.
# Nothing else is touched: no volume, no container, no other image.
set -euo pipefail

MOA_DIR=${MOA_DIR:-/srv/moa}
REGISTRY=${REGISTRY:-ghcr.io/my-other-app}
KEEP_IMAGES=${KEEP_IMAGES:-3}
KEEP_CACHE=${KEEP_CACHE:-5GB}

dry=0
case "${1:-}" in
  "") ;;
  --dry-run) dry=1 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

log() { echo "$(date '+%F %T') $*"; }
used() { df -B1 --output=used / | tail -1 | tr -d ' '; }
gb() { awk -v b="$1" 'BEGIN { printf "%.1f GB", b / 1e9 }'; }

[[ "$KEEP_IMAGES" =~ ^[1-9][0-9]*$ ]] || { echo "KEEP_IMAGES must be 1 or more" >&2; exit 2; }
command -v docker > /dev/null || { echo "docker is not installed" >&2; exit 1; }

before=$(used)

# ─── What must stay ─────────────────────────────────────────────────────────
declare -A pinned=()
if [ -f "$MOA_DIR/images.env" ]; then
  while IFS='=' read -r key value; do
    value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
    case "$key" in *_IMAGE) [ -n "$value" ] && pinned["$value"]=1 ;; esac
  done < "$MOA_DIR/images.env"
fi
while read -r image; do
  [ -n "$image" ] && pinned["$image"]=1
done < <(docker ps -a --format '{{.Image}}')

# ─── Old app images ─────────────────────────────────────────────────────────
remove=()
while read -r repo; do
  kept=0
  # Newest first; one line per tag.
  while IFS=$'\t' read -r _created tag; do
    [ "$tag" = "<none>" ] && continue
    [ "$tag" = "local" ] && continue
    ref="$repo:$tag"
    if [ -n "${pinned[$ref]:-}" ]; then
      kept=$((kept + 1))
    elif [ "$kept" -lt "$KEEP_IMAGES" ]; then
      kept=$((kept + 1))
    else
      remove+=("$ref")
    fi
  done < <(docker image ls "$repo" --format '{{.CreatedAt}}\t{{.Tag}}' | sort -r)
done < <(docker image ls --format '{{.Repository}}' | grep "^$REGISTRY/moa-" | sort -u)

for ref in "${remove[@]}"; do
  if [ "$dry" = 1 ]; then
    log "would remove $ref"
  elif docker image rm "$ref" > /dev/null 2>&1; then
    log "removed $ref"
  else
    log "kept $ref (still in use)"
  fi
done
[ ${#remove[@]} -eq 0 ] && log "no old app images to remove"

# Layers no tag points at any more (an image rebuilt under the same tag).
[ "$dry" = 1 ] || docker image prune -f > /dev/null

# ─── Build cache ────────────────────────────────────────────────────────────
cache=$(docker buildx du 2>/dev/null | awk '/^Total:/ { print $2 }')
if [ "$dry" = 1 ]; then
  log "build cache is ${cache:-unknown}; would cut it to $KEEP_CACHE"
else
  docker builder prune -af --max-used-space "$KEEP_CACHE" > /dev/null
  log "build cache was ${cache:-unknown}, now $(docker buildx du 2>/dev/null | awk '/^Total:/ { print $2 }')"
fi

after=$(used)
if [ "$dry" = 1 ]; then
  log "disk used: $(gb "$after") (dry run: nothing changed)"
else
  freed=$((before - after))
  [ "$freed" -lt 0 ] && freed=0
  log "disk used: $(gb "$before") -> $(gb "$after"), freed $(gb "$freed")"
fi
