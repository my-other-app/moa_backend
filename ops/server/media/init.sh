#!/usr/bin/env bash
# init.sh -- one-time setup of the media store (Garage) on this box, safe to
# run again: the node's layout, the bucket, the API's key (written into .env,
# never printed) and the bucket's CORS. Run as root from /srv/moa:
#
#   sudo bash /srv/moa/media/init.sh [bucket] [capacity]
#
# Defaults: bucket myotherapp-media, capacity 150G (what the layout may use).
# Also sets the bucket's CORS: our sites may read files in the browser.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
BUCKET="${1:-myotherapp-media}"
CAPACITY="${2:-150G}"
DC=(./dc)
g() { "${DC[@]}" exec -T media /garage "$@"; }

"${DC[@]}" up -d media < /dev/null
for _ in $(seq 1 30); do g status > /dev/null 2>&1 && break; sleep 2; done

node="$(g node id -q 2> /dev/null | cut -d@ -f1)"
[ -n "$node" ] || { echo "init.sh: Garage did not answer" >&2; exit 1; }
if g layout show 2> /dev/null | grep -q "No nodes currently have a role"; then
  g layout assign -z dc1 -c "$CAPACITY" "$node" > /dev/null
  g layout apply --version 1 > /dev/null
  echo "layout: this node, $CAPACITY"
else
  echo "layout: already set"
fi

g bucket info "$BUCKET" > /dev/null 2>&1 || { g bucket create "$BUCKET" > /dev/null; echo "bucket $BUCKET created"; }

# The API's key: made once, its two halves written straight into .env.
if ! grep -qE '^APP_S3_ACCESS_KEY=GK' .env; then
  out="$(g key create moa-api)"
  id="$(printf '%s\n' "$out" | awk '/Key ID:/ {print $3}')"
  secret="$(printf '%s\n' "$out" | awk '/Secret key:/ {print $3}')"
  [ -n "$id" ] && [ -n "$secret" ] || { echo "init.sh: could not read the new key" >&2; exit 1; }
  set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi; }
  set_env APP_S3_ACCESS_KEY "$id"
  set_env APP_S3_SECRET_KEY "$secret"
  unset out secret
  echo "key moa-api created and written to .env"
fi
id="$(grep -E '^APP_S3_ACCESS_KEY=' .env | cut -d= -f2)"
g bucket allow --read --write --owner "$BUCKET" --key "$id" > /dev/null
echo "key $id may read and write $BUCKET"

# CORS: the sites may read (GET/HEAD) from the browser, nothing else (the
# dashboards' ticket and certificate designers fetch template images). Set
# through the S3 API with the API's own key, from the backend's image.
"${DC[@]}" run --rm --no-deps -T -e MOA_BUCKET="$BUCKET" backend python -c '
import os, boto3
from botocore.config import Config
s3 = boto3.client("s3", endpoint_url="http://media:3900", region_name="garage",
                  aws_access_key_id=os.environ["APP_S3_ACCESS_KEY"],
                  aws_secret_access_key=os.environ["APP_S3_SECRET_KEY"],
                  config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
origins = ["https://myotherapp.in", "https://club-dashboard.myotherapp.in", "https://main-portal.myotherapp.in"]
s3.put_bucket_cors(Bucket=os.environ["MOA_BUCKET"], CORSConfiguration={"CORSRules": [{
    "AllowedOrigins": origins, "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"], "MaxAgeSeconds": 3600}]})
print("CORS: GET/HEAD from " + ", ".join(origins))
' < /dev/null
