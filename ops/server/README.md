# ops/server: the whole platform on one Oracle Cloud box

Everything MyOtherApp runs, on one machine: the API and its job worker,
Postgres 17, Redis, Meilisearch, the media store (Garage), the render
service, the public site (moa_events), the club dashboard and the main
portal (moa_web), and Caddy in front. The images are built on the box
(`build/build.sh`) and deployed with `deploy.sh <app> <sha> --local`; CI can
also build them for GHCR, and `deploy.sh` without `--local` pulls those.

The move from Vercel and the EC2 box, step by step, is
[docs/MOVE_TO_ORACLE.md](../../docs/MOVE_TO_ORACLE.md). The old deploy
(`docker-compose.prod.yml`, `.github/workflows/live-deploy.yml`) is untouched
and keeps working until the cutover.

```
                      Cloudflare (DNS, edge TLS, WAF, cache; club-sites Worker)
                                    |  Full (strict), Origin CA certificate
  ┌─────────────────────────────── OCI instance, /srv/moa ────────────────────────────────┐
  │  caddy :80/:443                                                                         │
  │   ├─ api.myotherapp.in            -> backend:8000  ──┐                                  │
  │   ├─ myotherapp.in                -> website:3000  ──┤ (API_BASE_URL / MOA_API_URL =   │
  │   ├─ club-dashboard.myotherapp.in -> clubs:3000    ──┤  http://backend:8000)            │
  │   ├─ main-portal.myotherapp.in    -> admin:3000    ──┘                                  │
  │   └─ www., events., clubs-next., admin-next., admin-dashboard., admin-official. -> 308   │
  │  backend (2 uvicorn workers) + worker (jobs) ── postgres:5432 (127.0.0.1 only)          │
  │                                             ├─ redis:6379                               │
  │                                             └─ meilisearch:7700                         │
  └─────────────────────────────────────────────────────────────────────────────────────────┘
        media: S3 (unchanged)   mail: SES / Resend (unchanged)   backups: S3 ap-south-1
```

## Files

| File | On the server | What it is |
|---|---|---|
| `compose.yml` | `/srv/moa/compose.yml` | Every service, its image, limits and health check |
| `caddy/Caddyfile`, `Caddyfile.staging` | `/srv/moa/caddy/` | Names to containers, redirects, client IP, TLS |
| `caddy/global.caddy`, `snippets.caddy` | `/srv/moa/caddy/` | Cloudflare's ranges, the Origin CA certificate, shared proxy settings |
| `deploy.sh` | `/srv/moa/deploy.sh` | Deploy, health check, roll back, status, doctor, stack install |
| `build/build.sh` | `/srv/moa-build/build.sh` | Builds the images on the box from `/srv/moa-src` (see "Building on the box") |
| `db/app-role.sh`, `app-role.sql` | `/srv/moa/db/` | Makes the API's own database role (see "The API's database role") |
| `dc` | `/srv/moa/dc` (and `moa-dc`) | `docker compose` with `.env` and `images.env` loaded |
| `deploy-gate.sh` | `/srv/moa/deploy-gate.sh` | The only command CI's SSH key may run |
| `setup.sh` | run once (and again: idempotent) | Prepares a fresh Ubuntu 24.04 instance |
| `cloudflare-ips.sh` | `/srv/moa/cloudflare-ips.sh` | Checks Caddy's trusted ranges against Cloudflare's list |
| `env/*.example` | `/srv/moa/env/*.example` | What goes in `.env` and `env/<app>.env` |
| `deploy.conf.example` | `/srv/moa/deploy.conf` | deploy.sh's own settings (registry, timeouts) |
| `ops-bot/` | `/srv/moa/ops-bot/` | The Telegram ops bot: encrypted nightly backups, alerts, `/status` ([its README](ops-bot/README.md)) |
| `tidy/` | `/srv/moa/tidy/tidy.sh`, `moa-tidy.{service,timer}` | Every night at 04:30 IST: Docker's build cache cut to 5 GB, old app images beyond the last 3 per app removed (`sudo bash ops/server/tidy/install.sh`) |

Never in git, only on the server (root, mode 600/700): `.env`, `env/*.env`
(including `env/ops-bot.env`, the bot's token),
`certs/origin.pem` and `origin.key`, `secrets/` (wallet certificates,
Firebase JSON). `images.env` and `state/` are written by deploy.sh.

## Everyday commands

```bash
sudo /srv/moa/deploy.sh status                       # what runs, healthy?, disk, memory
sudo /srv/moa/deploy.sh backend <git-sha> --local    # an image built on this box (build/build.sh): never pulled
sudo /srv/moa/deploy.sh backend <git-sha>            # API + worker, refused if it needs a migration
sudo /srv/moa/deploy.sh backend <git-sha> --migrate  # a dump, then alembic upgrade head with the new image
sudo /srv/moa/deploy.sh backend <git-sha> --no-worker  # rehearsals on a copy: nothing queued gets sent
sudo /srv/moa/deploy.sh website <git-sha>            # or clubs / admin
sudo /srv/moa/deploy.sh rollback website             # the image before the last deploy
sudo /srv/moa/deploy.sh recreate backend             # same image, new container: applies .env edits
sudo /srv/moa/deploy.sh caddy                        # validate + reload the Caddyfile
sudo /srv/moa/deploy.sh doctor                       # files, settings, mounts (names only)
sudo moa-dc logs -f --tail 100 backend               # anything else: docker compose
sudo /srv/moa/tidy/tidy.sh --dry-run                 # what the nightly Docker tidy would remove
sudo systemctl start moa-tidy.service                # tidy now; journalctl -u moa-tidy.service
```

From GitHub: each repository's **Deploy to Oracle** workflow (manual) SSHes in
as `moa-deploy` and runs the same commands through `deploy-gate.sh`.

## Testing these files on a laptop

Nothing here needs the server to be checked:

```bash
docker compose --project-directory /tmp/stack --env-file /tmp/stack/.env -f ops/server/compose.yml config -q
MOA_TLS_CERT=/tmp/c.pem MOA_TLS_KEY=/tmp/c.key caddy validate --config ops/server/caddy/Caddyfile --adapter caddyfile
shellcheck ops/server/*.sh ops/server/dc
```

To try the routing itself, run Caddy with `MOA_HTTPS_PORT=8443`,
`MOA_UPSTREAM_BACKEND=127.0.0.1:8000` (and `_WEBSITE`, `_CLUBS`, `_ADMIN`),
a self-signed `MOA_TLS_CERT`/`MOA_TLS_KEY`, and
`MOA_EXTRA_TRUSTED_PROXIES=127.0.0.1/32` to act as Cloudflare, then
`curl -k --resolve api.myotherapp.in:8443:127.0.0.1 -H 'CF-Connecting-IP: 1.2.3.4' ...`.
Never set the `MOA_UPSTREAM_*` or `MOA_EXTRA_TRUSTED_PROXIES` variables on a
server. `tests/test_oracle_stack.py` holds the limits and the rules above.

## Media store (Garage)

Uploads (posters, logos, tickets, certificates, store photos) live on this box
in Garage, an S3-compatible store: compose.yml's `media` service,
`media/garage.toml`. The API writes over the box's own network
(`http://media:3900`); browsers read signed links at `https://media.myotherapp.in`,
where Caddy lets through reads only.

Once, on a new box (after `.env` has `GARAGE_RPC_SECRET` and
`GARAGE_ADMIN_TOKEN`, each `openssl rand -hex 32`):

```bash
sudo install -d -m 700 /var/lib/myotherapp/media/meta /var/lib/myotherapp/media/data
sudo bash /srv/moa/media/init.sh            # layout, bucket, the API's key into .env
```

Then in `.env`: `APP_S3_ENDPOINT_URL=http://media:3900`,
`APP_S3_PUBLIC_URL=https://media.myotherapp.in`, `APP_S3_REGION=garage`,
`APP_S3_BUCKET=myotherapp-media`, `APP_S3_BASE_PATH` as
`ops/server/env/backend.env.example` says, and
`sudo /srv/moa/deploy.sh recreate backend` (and the worker). Cloudflare:
`media` as a proxied record, and a "None" Worker route `media.myotherapp.in/*`
so the club-sites Worker never takes it.

It is on this disk: turn on the provider's automatic backups (snapshots)
for the box, and the nightly copy off the box ("Off-box copies" below).
`sudo moa-dc exec media /garage status` and
`... /garage bucket info myotherapp-media` show its state.

## Building on the box

`build/build.sh` (on the box `/srv/moa-build/build.sh`) builds from the
`git archive` of each repository in `/srv/moa-src`, with the commits on ONE
line of `/srv/moa-src/VERSIONS`:

```bash
git -C moa_backend archive <sha> | ssh <box> 'tar -x -C /srv/moa-src/backend'   # and web, events
echo 'backend=<sha> web=<sha> events=<sha>' | sudo tee /srv/moa-src/VERSIONS
sudo setsid nohup /srv/moa-build/build.sh backend clubs admin website < /dev/null > /dev/null 2>&1 &
tail -f /srv/moa-build/logs/summary.log                  # ends "ALL DONE ok: ... failed: ..."
sudo /srv/moa/deploy.sh backend <sha> --local --migrate  # then website / clubs / admin <sha> --local
```

The build settings stay in `/srv/moa-build` (`<app>.production.env`,
`api_site_token`, `sentry_auth_token`): BuildKit secrets, in no layer and not
in the cache key, so after changing one build that app with `--no-cache`.
`render` builds `moa-render:8.39.0-1` (compose.yml's `RENDER_IMAGE`).

## Networks

`default` is the edge: Caddy and what it serves (backend, website, clubs,
admin, media). `data` is for Postgres, Redis and Meilisearch: once `.env`
says `MOA_DATA_NETWORK=data` and each of them is recreated, only the API, the
worker and the one-off tasks (they join both) can reach them. Unset, the
three stay on `default` as before. The media store stays on `default`
because Caddy serves it; each of its ports needs a key or a secret.

Moving them is a recreate of each: Postgres is down for its restart (~10 s),
so do it at a quiet hour. An older compose.yml cannot be put back while they
are on `data` without first unsetting `MOA_DATA_NETWORK` and recreating them.

## Redis

`REDIS_PASSWORD` in `.env` (hex, `openssl rand -hex 32`: it goes into the
API's address as it is) gives Redis a password and puts it in the API's and
the worker's `APP_REDIS_URL` (unless `.env` sets `APP_REDIS_URL` itself: then
add it there). Redis and its clients must change together:
`sudo ./dc up -d --no-deps --force-recreate redis backend worker`.

## The API's database role

The API and the worker can use a role of their own that reads and writes
rows but cannot change the schema, create anything or run programs; the
owner (`POSTGRES_USER`) keeps the schema and runs the migrations through the
one-off `migrate` service (`deploy.sh --migrate` does; by hand
`env BACKEND_IMAGE=<image> ./dc run --rm --no-deps -T migrate`), and the
restore drill runs there too. In `.env`: `MOA_APP_DB_USER=moa_app` and
`MOA_APP_DB_PASSWORD=<openssl rand -hex 32>`, then
`sudo bash /srv/moa/db/app-role.sh` (safe again at any time; `check` only
checks), then `sudo /srv/moa/deploy.sh recreate backend`. To go back: remove
the two lines and recreate.

## The API as a non-root user

The backend image runs as uid 10001; compose keeps root until `.env` says
`MOA_BACKEND_USER=10001:10001`. Before that, once (the image must be one
built from a commit that has the user):

```bash
sudo chown -R 10001:10001 "$(readlink -f /var/backups/myotherapp)"    # the dumps it writes
sudo chgrp -R 10001 /srv/moa/secrets && sudo chmod -R g+rX,g-w /srv/moa/secrets   # wallet certificates it reads
ls -ld /var/lib/myotherapp/geoip                                         # world-readable (755) already
```

then `sudo /srv/moa/deploy.sh recreate backend` and `deploy.sh doctor`. It
writes nothing else outside `/tmp`.

## Off-box copies

- **The database**: every nightly dump also goes to the bucket in
  `APP_BACKUP_S3_*` (AWS, or another S3-compatible store with
  `APP_BACKUP_S3_ENDPOINT_URL`); the ops bot also sends it to Telegram
  (under 50 MB), and says so when no dump is newer than 26 hours.
- **The media**: `APP_MEDIA_SYNC=true` (with the backup bucket set) and
  `sudo systemctl enable --now myotherapp-media-sync.timer` copy every new
  object of the media bucket to that bucket under `media/` at 03:30 IST,
  copy only. `sudo bash /srv/moa/ops/backup/run.sh media-sync --dry-run`
  counts what it would copy. The backup key needs `s3:PutObject` and
  `s3:ListBucket` on `media/*` too.
