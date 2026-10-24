# ops/server: the whole platform on one Oracle Cloud box

Everything MyOtherApp runs, on one machine: the API and its job worker,
Postgres 17, Redis, Meilisearch, the public site (moa_events), the club
dashboard and the main portal (moa_web), and Caddy in front. Images are built
by CI in each repository and pulled from GHCR; nothing is built on the box.

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
| `dc` | `/srv/moa/dc` (and `moa-dc`) | `docker compose` with `.env` and `images.env` loaded |
| `deploy-gate.sh` | `/srv/moa/deploy-gate.sh` | The only command CI's SSH key may run |
| `setup.sh` | run once (and again: idempotent) | Prepares a fresh Ubuntu 24.04 instance |
| `cloudflare-ips.sh` | `/srv/moa/cloudflare-ips.sh` | Checks Caddy's trusted ranges against Cloudflare's list |
| `env/*.example` | `/srv/moa/env/*.example` | What goes in `.env` and `env/<app>.env` |
| `deploy.conf.example` | `/srv/moa/deploy.conf` | deploy.sh's own settings (registry, timeouts) |

Never in git, only on the server (root, mode 600/700): `.env`, `env/*.env`,
`certs/origin.pem` and `origin.key`, `secrets/` (wallet certificates,
Firebase JSON). `images.env` and `state/` are written by deploy.sh.

## Everyday commands

```bash
sudo /srv/moa/deploy.sh status                       # what runs, healthy?, disk, memory
sudo /srv/moa/deploy.sh backend <git-sha>            # API + worker, refused if it needs a migration
sudo /srv/moa/deploy.sh backend <git-sha> --migrate  # alembic upgrade head first, with the new image
sudo /srv/moa/deploy.sh backend <git-sha> --no-worker  # rehearsals on a copy: nothing queued gets sent
sudo /srv/moa/deploy.sh website <git-sha>            # or clubs / admin
sudo /srv/moa/deploy.sh rollback website             # the image before the last deploy
sudo /srv/moa/deploy.sh recreate backend             # same image, new container: applies .env edits
sudo /srv/moa/deploy.sh caddy                        # validate + reload the Caddyfile
sudo /srv/moa/deploy.sh doctor                       # files, settings, mounts (names only)
sudo moa-dc logs -f --tail 100 backend               # anything else: docker compose
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
