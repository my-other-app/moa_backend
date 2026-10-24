# Moving MyOtherApp to one Oracle Cloud box

Everything MyOtherApp runs moves to one Oracle Cloud (OCI) instance in India:
the API and its job worker, Postgres, Redis and Meilisearch from the AWS EC2
box, and the public site and both dashboards from Vercel. Cloudflare stays in
front (DNS, TLS at the edge, WAF, caching, the club-sites Worker). Media stays
on S3, mail on SES/Resend.

The files are in `ops/server/` (what each is: [ops/server/README.md](../ops/server/README.md)).
The old deploy path (`docker-compose.prod.yml`, `live-deploy.yml`, Vercel) is
untouched and keeps working until the cutover, and for a week after it as the
way back.

**Expected downtime: about 15-25 minutes during which the API refuses writes
(reads keep working throughout).** The rehearsal (step 7) measures it for the
real database size.

Contents:
1. [What it looks like](#1-what-it-looks-like)
2. [Capacity](#2-capacity)
3. [What to provision in OCI](#3-what-to-provision-in-oci)
4. [Set up the box](#4-set-up-the-box)
5. [Settings, certificates and secret files on the box](#5-settings-certificates-and-secret-files-on-the-box)
6. [Images and CI](#6-images-and-ci)
7. [Cloudflare, before the day](#7-cloudflare-before-the-day)
8. [Rehearsal](#8-rehearsal)
9. [The cutover](#9-the-cutover)
10. [Verify](#10-verify)
11. [The week after, and decommissioning](#11-the-week-after-and-decommissioning)
12. [Rollback](#12-rollback)
13. [Staging](#13-staging)
14. [Backups](#14-backups)
15. [Running it](#15-running-it)
16. [Risks](#16-risks)
17. [Cost](#17-cost)
18. [Every secret, by name](#18-every-secret-by-name)

---

## 1. What it looks like

```
 phones (Flutter app) · browsers · Razorpay/SES/Resend/Meta webhooks · AI assistants (MCP)
                                   │
                    Cloudflare  (proxied records, SSL "Full (strict)")
     ├─ {club}.myotherapp.in, clubs' own domains ── club-sites Worker ──┐ (reads the API)
     └─ api., apex, www., events., club-dashboard., main-portal., old names
                                   │  TLS with the Cloudflare Origin CA certificate
 ┌──────────────── OCI VM (Ampere A1, 2 OCPU, 12 GB, Mumbai/Hyderabad) ────────────────┐
 │ caddy :80/:443 ─┬─ api.myotherapp.in            → backend:8000 (uvicorn ×2)          │
 │                 ├─ myotherapp.in                → website:3000 (Next standalone)     │
 │                 ├─ club-dashboard.myotherapp.in → clubs:3000   ─┐ gateways call      │
 │                 ├─ main-portal.myotherapp.in    → admin:3000   ─┤ http://backend:8000│
 │                 └─ www, events, clubs-next, admin-next, admin-dashboard → 308        │
 │ worker (jobs, sweeps) ─┐                                                              │
 │ backend ───────────────┼─ postgres 17 (127.0.0.1:5432 only) · redis · meilisearch    │
 │ /srv/moa-data (block volume): Docker data, /var/backups/myotherapp, GeoLite2         │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        S3 (media, unchanged) · SES/Resend (mail, unchanged) · S3 ap-south-1 (backups)
        GHCR (images, built by GitHub Actions for amd64 + arm64)
```

What changes for whom:

| | Before | After |
|---|---|---|
| API | EC2 us-east-1, nginx, built on the box | OCI India, Caddy, image from GHCR |
| Site, dashboards | Vercel | Same box, Next.js standalone containers |
| Distance from India | ~280 ms per round trip to us-east-1 | Cloudflare edge in India, origin in India |
| Hostnames, cookies, webhook URLs, app builds | | unchanged |
| Deploy | `live-deploy.yml` (rsync + build on the box) | `Deploy to Oracle` (pull a tested image, health check, automatic rollback) |
| Worker, Redis, Meilisearch | could not run (4 GB box) | on by default |

What the app code needed: nothing. The site's visitor geolocation keeps
working because Caddy hands it Cloudflare's location headers under Vercel's
names (section 7); the visitor's address reaches the API and the site through
`CF-Connecting-IP` → Caddy `{client_ip}` → `X-Forwarded-For`.

## 2. Capacity

2 cores, 12 GB. Every service has a hard memory cap (a leak is killed inside its
own container, never Postgres by the kernel), a CPU cap, and a CPU weight for
when the two cores are contended (`ops/server/compose.yml`; the numbers are
held by `backend/tests/test_oracle_stack.py`):

| Service | `mem_limit` | `cpus` | `cpu_shares` | Notes |
|---|---|---|---|---|
| postgres | 2560m | 2 | 1024 | `shared_buffers` 1536 MB, `work_mem` 8 MB, `maintenance_work_mem` 256 MB, `max_connections` 100 |
| backend | 2048m | 2 | 1024 | 2 uvicorn workers (`MOA_API_WORKERS`), 1 render thread each (`MOA_RENDER_THREADS`) |
| worker | 768m | 1 | 512 | 2 jobs at once |
| redis | 300m | 0.5 | 512 | `maxmemory 224mb`, LRU, no persistence |
| meilisearch | 768m | 1 | 256 | 1 indexing thread, 256 MiB for indexing |
| website | 640m | 1 | 768 | Node heap 384 MB |
| clubs | 512m | 1 | 512 | Node heap 320 MB |
| admin | 448m | 1 | 256 | Node heap 288 MB |
| caddy | 128m | 1 | 768 | |
| **Total** | **8172m (8.0 GB)** | | | + OS and Docker ~1 GB = **~9 GB**; ~3 GB free for page cache and render spikes; 4 GB swapfile as the last resort |

Connections: 2 API workers × (10 + 20) + the worker's 4 + 8 = 72 of the 97
Postgres allows; a migration, a backup and a psql session fit beside them.

Watch after the move (`sudo /srv/moa/deploy.sh status`): if the backend sits
near 2 GB, give it the admin portal's or Meilisearch's share first; if
Postgres's data passes ~3 GB, its page cache wants more than its 2.5 GB.

## 3. What to provision in OCI

**Account**
- **Upgrade the tenancy to Pay As You Go** (Billing → Upgrade and manage
  payment). Always Free resources stay free on PAYG, and PAYG instances are
  not reclaimed when idle. On a free-tier tenancy Oracle reclaims an "idle"
  Always Free instance (over 7 days: CPU 95th percentile, network and, for
  A1, memory all under 20%), and a quiet night-time API can look exactly
  like that. PAYG also gets A1 capacity far more easily ("Out of host
  capacity" is common on free tenancies).
- **Home region**: Always Free applies only in the tenancy's home region,
  chosen at sign-up and never changeable. Put the box in **India South
  (Hyderabad)** or **India West (Mumbai)**; if the home region is elsewhere,
  the instance in India is billed at list price (section 17).
- **Since June 2026 the Always Free Ampere allowance is 1,500 OCPU-hours and
  9,000 GB-hours a month: 2 OCPUs and 12 GB in all** (Oracle's Always Free
  page, updated 2026-06-12), half of what it was. The production box uses all
  of it; a second, staging box is paid (section 13).

**Instance** (Compute → Instances → Create)
- Image: **Canonical Ubuntu 24.04** (the platform image, not "Minimal").
- Shape: **VM.Standard.A1.Flex, 2 OCPU, 12 GB** (Ampere arm64; 1 OCPU = 1
  core). Fallback if A1 capacity is not there: **VM.Standard.E5.Flex** (AMD
  x86_64; 1 OCPU = 2 threads), 1 or 2 OCPU, 12 GB. Every image is built for
  both, so nothing else changes.
- Boot volume: **50 GB**, balanced (10 VPU).
- SSH key: an ed25519 key of the admin's (`ssh-keygen -t ed25519 -f ~/.ssh/moa-oracle`).
- Networking: a VCN with a public subnet (the wizard's "Create new virtual
  cloud network" is fine: 10.0.0.0/16, internet gateway, route to it).
  Public IPv4 address: yes. Then make it a **reserved public IP** (Networking
  → IP Management → Reserved public IPs → reserve, then on the instance's
  VNIC → IPv4 addresses → edit → reserved) so a stop/start never changes it.
- Show advanced options → Management: leave Oracle Cloud Agent on (metrics).

**Block volume** (Storage → Block Volumes → Create): **100 GB**, balanced
performance, same availability domain. Attach it to the instance as
**Paravirtualized**, device path **/dev/oracleoci/oraclevdb**, read/write.
(Paravirtualized needs no iSCSI commands; the stable device path survives
reboots.) It holds Docker's data, the backups and GeoLite2, so the instance
can be replaced without losing them. Optional: a backup policy on it (Silver:
weekly) — volume backups are crash-consistent copies of the whole disk, on
top of, never instead of, the pg_dump backups in section 14. Five volume
backups are free.

Boot 50 + data 100 = 150 GB of the 200 GB Always Free block storage; a
staging boot volume of 50 GB uses the rest.

**Security list** (the VCN's default security list, or better a Network
Security Group on the instance's VNIC), ingress:

| Source | Protocol / port | Why |
|---|---|---|
| your admin IP /32 | TCP 22 | SSH (setup.sh limits it on the box too) |
| 0.0.0.0/0 | TCP 80, 443 | Cloudflare → Caddy (see "Hardening" in section 10 for Cloudflare-only) |
| 0.0.0.0/0 | ICMP type 3 code 4 | path MTU discovery (the default list has it) |

Remove the default "0.0.0.0/0 TCP 22" rule. Egress: all (default). If CI
must reach SSH without Tailscale, see section 6 ("CI's way in").

**OCI gotchas**
- **Do not use ufw.** Oracle's Ubuntu images ship `/etc/iptables/rules.v4`
  with rules the boot and block volumes rely on, and Oracle documents that
  enabling ufw can leave the instance unable to boot. setup.sh edits
  rules.v4 instead and leaves Oracle's `InstanceServices` chain alone.
- The image's rules **reject everything except SSH** (`-A INPUT -j REJECT`):
  80/443 must be allowed there *and* in the security list. setup.sh does the
  host side.
- Docker's published ports bypass the host's INPUT chain; only Caddy
  publishes ports, Postgres is bound to 127.0.0.1.
- A1 is arm64: the images are multi-arch, and every Python dependency has an
  aarch64 wheel (checked: all 119 resolve to binary wheels for
  aarch64-manylinux_2_28). wkhtmltopdf is Debian's own build on both.
- The serial console (Instance → Console connection) is the way in if SSH is
  ever locked out.

## 4. Set up the box

From the admin's machine (the instance's default user is `ubuntu`):

```bash
ssh -i ~/.ssh/moa-oracle ubuntu@<oracle-ip>
```

On the box:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone --depth 1 -b master https://github.com/my-other-app/moa_backend.git ~/moa_backend
#   (private repository: scp ops/ from a checkout instead: scp -r ops ubuntu@<ip>:~/moa_backend/)

# A key for CI, made on the admin's machine; only its public half goes to the box:
#   ssh-keygen -t ed25519 -C "github-actions deploy" -f ./moa-ci-deploy -N ""
#   scp ./moa-ci-deploy.pub ubuntu@<oracle-ip>:/tmp/moa-ci-deploy.pub

sudo bash ~/moa_backend/ops/server/setup.sh \
  --admin-cidr <your-ip>/32 \
  --data-device /dev/oracleoci/oraclevdb --format-data \
  --deploy-key-file /tmp/moa-ci-deploy.pub \
  --swap-gb 4 --timezone Asia/Kolkata --hostname moa-prod
```

`--format-data` only formats a volume with no filesystem at all; it refuses
anything else. Run it again whenever: every step checks first. It refuses to
apply a firewall that would lock out the SSH session it runs in.

What it leaves: Docker with the compose plugin, its data on
`/srv/moa-data/docker`; a 4 GB swapfile; rules.v4 allowing SSH from your
CIDR and 80/443 from anywhere; fail2ban on SSH; daily unattended security
updates (no automatic reboot unless `--auto-reboot 03:30`); SSH keys only;
journald capped at 500 MB; Docker logs rotated (5 × 20 MB per container);
the `moa-deploy` user, whose key can only run `deploy-gate.sh`; `/srv/moa`
with the stack's files; `/var/backups/myotherapp` and
`/var/lib/myotherapp/geoip` on the data volume.

Check: `sudo /srv/moa/deploy.sh doctor` (it will list what section 5 adds).

## 5. Settings, certificates and secret files on the box

Nothing here goes through GitHub. Copy from the old box by hand, over SSH,
and never print a value.

**5.1 The API's `.env`.** The old box's `/var/www/myotherapp-production/.env`
becomes `/srv/moa/.env`, unchanged, plus the lines in
`ops/server/env/backend.env.example`. From the admin's machine, without the
file ever touching it unencrypted on disk:

```bash
ssh old-box 'sudo cat /var/www/myotherapp-production/.env' | ssh -i ~/.ssh/moa-oracle ubuntu@<oracle-ip> "sudo sh -c 'umask 077; cat > /srv/moa/.env'"
```

Then on the new box, `sudo nano /srv/moa/.env` and add (values generated on the box):
- `MEILI_MASTER_KEY=` → `openssl rand -hex 24`
- `APP_SITE_REVALIDATE_URL=http://website:3000/api/revalidate`
- if the file has `APP_FIREBASE_SERVICE_ACCOUNT_PATH`: `=/secrets/service-account.json` (5.3)
- if it has `APP_GEOIP_MMDB_PATH`: keep `/geoip/GeoLite2-City.mmdb` and copy the file (5.3)
- the backup lines (section 14), once the bucket exists.

And remove any `APP_JOBS_WORKER=false` or `APP_SWEEPS_IN_WORKER=false` line
carried over: on this box the worker runs the jobs and the sweeps (the
defaults below), and with `false` the API would run them itself — during the
rehearsal, on a copy of production, that would send real mails
(`deploy.sh doctor` warns about it).

compose.yml overrides, whatever `.env` says: the database URLs (built from
`POSTGRES_*`, host `postgres`), `APP_PORT=8000`, `APP_WORKERS=2`,
`APP_RENDER_THREADS=1`, `WALLET_CERTS_DIR=/secrets/walletethings`. It turns
on by default (unless `.env` sets them): Redis, Meilisearch, the job worker
and the sweeps in the worker, and `FORWARDED_ALLOW_IPS=*` (only Caddy and the
Next servers can reach the API, so it may believe their `X-Forwarded-Proto`).

**5.2 The site's settings.** `/srv/moa/env/website.env` (mode 600) from
`ops/server/env/website.env.example`: `REVALIDATE_SECRET` and
`API_SITE_TOKEN`, copied from the Vercel project `moa-events` (Settings →
Environment Variables → Production). The dashboards need none
(`env/clubs.env`, `env/admin.env` are optional).

**5.3 Files only the old box had.** The old box bind-mounted its code, so
files that were never in git lived in `backend/`; the image has none of them.
On the old box, list them (read-only):

```bash
cd /var/www/myotherapp-production && sudo ls -la backend/walletethings backend/wallet_certs backend/service-account.json 2>&1
sudo grep -oE '^(WALLET_CERTS_DIR|APP_FIREBASE_SERVICE_ACCOUNT_PATH|APP_GEOIP_MMDB_PATH)=' .env
sudo ls -la /var/lib/myotherapp/geoip/
```

Copy each to the new box (the same `ssh … cat | ssh … install` pattern):
- wallet certificates (`pass_certificate_new.pem`, `pass_key_new.pem`,
  `wwdr.pem`) → `/srv/moa/secrets/walletethings/` (mode 600)
- the Firebase JSON, if a file is used → `/srv/moa/secrets/service-account.json`
- `GeoLite2-City.mmdb`, if there → `/var/lib/myotherapp/geoip/`

(The wallet pass key was exposed in October; if it was never rotated, do it
now rather than copy it.)

**5.4 The origin certificate.** Cloudflare → myotherapp.in → SSL/TLS →
Origin Server → Create Certificate: RSA 2048, hostnames `myotherapp.in` and
`*.myotherapp.in`, 15 years. Save the two PEM blocks on the box as
`/srv/moa/certs/origin.pem` and `/srv/moa/certs/origin.key` (both `chmod
600`, root). Browsers never see it; Cloudflare checks it ("Full (strict)").

**5.5 Images.** The packages start private. Either make each one public
(github.com/orgs/my-other-app/packages → moa-backend, moa-website, moa-clubs,
moa-admin → Package settings → Change visibility → Public: the images hold no
secret), or log the box in once with a classic token that has only
`read:packages`:

```bash
sudo docker login ghcr.io -u <github-user>   # paste the token; stored in /root/.docker/config.json
```

Then `sudo /srv/moa/deploy.sh doctor` should list no FAIL.

## 6. Images and CI

**Builds** (every image is linux/amd64 + linux/arm64, natively on GitHub's
`ubuntu-24.04-arm` runners, joined into one tag; QEMU only if the repository
variable `ARM64_RUNNER=qemu`):

| Repository | Workflow | Image | Tags |
|---|---|---|---|
| moa_backend | `image.yml`, on push to master (backend/**) | `ghcr.io/my-other-app/moa-backend` | `<sha>`, `<short sha>`, `latest` |
| moa_events | `image.yml` | `…/moa-website` | production: `<sha>`, `<short>`, `latest`; staging: `staging-<sha>`, `staging-<short>`, `staging` |
| moa_web | `images.yml` | `…/moa-clubs`, `…/moa-admin` | same as the site |

The site and dashboards bake their `NEXT_PUBLIC_*` values in at build time,
so they are built per environment from checked-in, **public-only** value
files, and their workflows run on push only once the repository variable
`ORACLE_IMAGES=true` is set (so main stays green until the files are filled):

| Image | Build-time file | What it must hold |
|---|---|---|
| moa-website | `moa_events/deploy/build/production.env` | **required**: `NEXT_PUBLIC_API_BASE_URL`, `NEXT_PUBLIC_GOOGLE_CLIENT_ID`, `ANDROID_APP_SHA256`; optional: `NEXT_PUBLIC_RAZORPAY_KEY`, `NEXT_PUBLIC_PLAY_STORE_URL`, `NEXT_PUBLIC_APP_STORE_URL`, `NEXT_PUBLIC_SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_ENVIRONMENT`, `NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE`, `NEXT_PUBLIC_SENTRY_REPLAYS_ON_ERROR_RATE`, `SENTRY_ORG`, `SENTRY_PROJECT`, `DEMO_NOINDEX`, `DEMO_CLUB_SLUGS` |
| moa-clubs | `moa_web/deploy/build/clubs.production.env` | `NEXT_PUBLIC_RAZORPAY_KEY_ID`, `NEXT_PUBLIC_SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_ENVIRONMENT`, `NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_ORG`, `SENTRY_PROJECT` |
| moa-admin | `moa_web/deploy/build/admin.production.env` | `NEXT_PUBLIC_SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_ENVIRONMENT`, `NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_ORG`, `SENTRY_PROJECT` |

(`*.staging.env` beside each, for staging.) Copy the values from each Vercel
project's Production variables. `NEXT_PUBLIC_SENTRY_RELEASE` /
`SENTRY_RELEASE` are set by the build from the commit. Real secrets used at
build time come from GitHub secrets and are mounted for the build step only:
`SENTRY_AUTH_TOKEN` (all three), `API_SITE_TOKEN` and `STAGING_API_SITE_TOKEN`
(site, optional). `MOA_API_URL` (dashboards) and `API_BASE_URL` (site) are
run-time settings the box sets: the same image works against any API.

**Deploys.** Each repository has a manual deploy: moa_backend's **Deploy to
Oracle** (any service and tag, plus `stack`, `status`, `rollback`,
`recreate`, `caddy`), and the site's and dashboards' image workflows ("Run
workflow" → environment, tick *deploy*). With `STAGING_ENABLED=true` and
`AUTO_DEPLOY_STAGING=true`, every push to main of the site or dashboards is
built for staging and deployed to the staging box. Production deploys are
always by hand; add required reviewers to the `production` Environment for a
second pair of eyes.

The job SSHes in as `moa-deploy`, whose `authorized_keys` line is
`restrict,command="/srv/moa/deploy-gate.sh"`: it can run `deploy <service>
<tag> [--migrate]`, `rollback`, `recreate`, `status`, `caddy` and `stack`,
and nothing else (no shell, no forwarding); sudoers lets it run
`/srv/moa/deploy.sh` and nothing else. The host key is pinned.

**CI's way in.** GitHub's runners come from thousands of addresses, so "SSH
from the admin IP only" and "CI deploys over SSH" need one of:
1. **Tailscale (recommended):** `setup.sh --tailscale` (allows SSH on
   `tailscale0`), then `sudo tailscale up --advertise-tags=tag:moa-server`
   once; create a Tailscale OAuth client with `tag:ci` and set
   `TS_OAUTH_CLIENT_ID`/`TS_OAUTH_SECRET`; `ORACLE_SSH_HOST` is the box's
   tailnet name. Port 22 stays closed to the internet. Free plan suffices.
2. **SSH open to all**, key-only, fail2ban, the forced-command key above
   (`setup.sh --ssh-from-anywhere`, and 0.0.0.0/0 TCP 22 in the security
   list). Simpler; more noise in the logs.

GitHub settings, per repository (names only; values never in git):
- Environments `production` and `staging`, each with secrets
  `ORACLE_SSH_HOST`, `ORACLE_SSH_USER` (`moa-deploy`), `ORACLE_SSH_KEY` (the
  private half of `moa-ci-deploy`), `ORACLE_SSH_KNOWN_HOSTS` (the box's host
  key: `ssh-keyscan -t ed25519 <host>`, compared with the fingerprint
  `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` prints on the box), and
  optionally `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`.
- Repository secrets: `SENTRY_AUTH_TOKEN` (site, dashboards), `API_SITE_TOKEN`,
  `STAGING_API_SITE_TOKEN` (site).
- Repository variables: `ORACLE_IMAGES=true` (site, dashboards: build on
  push), `STAGING_ENABLED`, `AUTO_DEPLOY_STAGING`, optionally `ARM64_RUNNER`.

**First images:** merge the three branches, fill the production build files,
set `ORACLE_IMAGES=true`, and run each image workflow once by hand. Then
**Deploy to Oracle → stack** installs this commit's `ops/server` (and
`ops/backup`, once PR #102 is in) on the box.

The repositories are public today, so Actions minutes, arm64 runners and
public GHCR packages cost nothing. Should they go private: arm64 runners
still work there (since January 2026) but every minute counts against the
plan's free 2,000 a month, and the organisation's Actions billing is
broken — a release is roughly 60-100 runner minutes across the three
repositories. Private packages would also count against 500 MB of free
package storage (the backend image alone is ~1 GB per architecture), so keep
the packages public or log the box in (5.5) and expect storage charges.

## 7. Cloudflare, before the day

All in the myotherapp.in zone. None of this changes anything visible while
the records are still DNS-only, so do it days ahead.

1. **SSL/TLS → Overview → Full (strict).** The only proxied records today are
   the club-sites wildcard (a Worker, no origin), so nothing else is affected.
   And SSL/TLS → Edge Certificates → **Always Use HTTPS: on** (Caddy would
   redirect plain HTTP too, one round trip later).
2. **The origin certificate** (5.4).
3. **Worker routes with no Worker** (Workers Routes → Add route → Worker:
   None) for every name about to be proxied, so the club-sites Worker
   (`*.myotherapp.in/*`, and `*/*` once clubs' own domains are on) never sees
   them. **Required, not insurance:** `club-dashboard` and `main-portal` are
   not in the Worker's reserved list, so without a route the Worker would
   answer them with "No club site here".
   ```
   myotherapp.in/*  www.myotherapp.in/*  events.myotherapp.in/*  api.myotherapp.in/*
   club-dashboard.myotherapp.in/*  main-portal.myotherapp.in/*
   clubs-next.myotherapp.in/*  admin-next.myotherapp.in/*  admin-dashboard.myotherapp.in/*  admin-official.myotherapp.in/*
   ```
   (`infra/cloudflare/club-sites/CUSTOM_DOMAINS.md` in the app repository has
   the API form of this.)
4. **Rules → Settings → Managed Transforms → "Add visitor location headers": on.**
   Caddy passes `cf-iplatitude`, `cf-iplongitude`, `cf-ipcity`,
   `cf-region-code`, `cf-ipcountry` to the site as Vercel's
   `x-vercel-ip-*`, which "near you" reads. (Cloudflare sends non-ASCII
   city names as raw UTF-8 where Vercel URL-encoded them; Indian city names
   are ASCII.)
5. **The API must never be challenged** (the app, webhooks and the MCP
   server are not browsers): Security → Bots → **Bot Fight Mode off**
   (zone-wide on the free plan); Rules → Configuration Rules → new rule
   "API: no browser checks", when hostname equals `api.myotherapp.in`:
   Browser Integrity Check **off**, Security Level **Essentially Off**.
   Webhook senders: Razorpay, Amazon SNS (SES), Svix (Resend), Meta
   (WhatsApp).
6. **Speed:** Rocket Loader **off**, Email Obfuscation **off** (already),
   no HTML minification: each rewrites HTML and breaks React's hydration.
7. **Caching (optional, for the CPU):** a Cache Rule "eligible for cache,
   edge TTL: use origin" when hostname is `myotherapp.in` and path starts
   with `/og/` or `/media/`: share pictures and cover images are drawn on
   the box and say `s-maxage`. Never make HTML or anything under `/api/` or
   on `api.` cacheable (Cloudflare ignores `Vary`, which Next relies on).
   `/_next/static/*` is cached by extension already. Vercel used to keep
   `/events`, `/clubs` and `/topics/*` a minute at its edge; here they render
   per request (cheap: their API reads are in Next's data cache).
8. **T-1 day: lower the TTL to 1 minute** on every record that will move,
   all still DNS-only: `@` (apex), `www`, `events`, `api`, `club-dashboard`,
   `main-portal`, `clubs-next`, `admin-next`, `admin-dashboard` (and
   `admin-official` if it exists). Write down each record's current value:
   it is the way back.

## 8. Rehearsal

A full dress rehearsal on the new box, a day or two before, with a real copy
of the data. Nothing visible changes: DNS still points at the old box and
Vercel. **Do not start the worker during the rehearsal**: it would send the
copy's queued mails, pushes and WhatsApp messages to real people. With the
compose defaults (`APP_JOBS_WORKER=true`, `APP_SWEEPS_IN_WORKER=true`) the
API runs neither jobs nor sweeps itself, so with the worker stopped nothing
is sent.

**8.1 Dump the old database** (on the old box; a normal backup, no downtime):

```bash
sudo docker exec doth-live-postgres sh -c 'pg_dump -Fc -Z 6 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > /tmp/moa-rehearsal.dump
sha256sum /tmp/moa-rehearsal.dump
sudo docker cp ops/server/rowcounts.sql doth-live-postgres:/tmp/rowcounts.sql
sudo docker exec doth-live-postgres sh -c 'psql -X -A -t -F " " -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/rowcounts.sql' > /tmp/rowcounts-old.txt
```

(`ops/server/rowcounts.sql` reaches the old box with the next `sync_code`
deploy, or by `scp`; run from `/var/www/myotherapp-production`.)

**8.2 Move it** (from the admin's machine; streams through it, nothing
decrypted is stored there):

```bash
ssh old-box 'cat /tmp/moa-rehearsal.dump' | ssh -i ~/.ssh/moa-oracle ubuntu@<oracle-ip> 'sudo tee /var/backups/myotherapp/moa-rehearsal.dump > /dev/null'
ssh -i ~/.ssh/moa-oracle ubuntu@<oracle-ip> 'sudo sha256sum /var/backups/myotherapp/moa-rehearsal.dump'   # must equal the old box's
```

**8.3 Restore and compare** (on the new box):

```bash
cd /srv/moa
sudo ./dc up -d postgres redis meilisearch
sudo docker cp /var/backups/myotherapp/moa-rehearsal.dump moa-postgres-1:/tmp/restore.dump
sudo docker cp /srv/moa/rowcounts.sql moa-postgres-1:/tmp/rowcounts.sql
sudo ./dc exec -T postgres sh -c 'pg_restore --no-owner --no-acl --exit-on-error -j 2 -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/restore.dump'
sudo ./dc exec -T postgres sh -c 'vacuumdb --analyze-only -j 2 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
sudo ./dc exec -T postgres sh -c 'psql -X -A -t -F " " -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/rowcounts.sql' > /tmp/rowcounts-new.txt
```

Copy `/tmp/rowcounts-old.txt` over and `diff /tmp/rowcounts-old.txt
/tmp/rowcounts-new.txt`: **no output** means every table, the migration
revision and the extensions match. Time each command: dump + move + restore
+ analyze is most of the cutover's downtime.

**8.4 Start the apps on the copy** (the worker stays stopped):

```bash
sudo /srv/moa/deploy.sh doctor                             # APP_JOBS_WORKER and APP_SWEEPS_IN_WORKER must say true
sudo /srv/moa/deploy.sh backend <backend-sha> --no-worker  # add --migrate if it says so
sudo /srv/moa/deploy.sh website <site-sha>
sudo /srv/moa/deploy.sh clubs <dashboards-sha>
sudo /srv/moa/deploy.sh admin <dashboards-sha>
sudo ./dc up -d caddy
sudo /srv/moa/deploy.sh status && sudo /srv/moa/deploy.sh doctor
```

(`--no-worker` leaves the worker stopped; normally `deploy.sh backend`
moves it with the API. With the worker stopped and the API leaving jobs and
sweeps to it, the copy sends nothing.)

**8.5 Smoke from the box itself** (Caddy, TLS, routing, the apps on the
copy; `-k` because the Origin CA certificate is trusted only by Cloudflare):

```bash
for u in https://api.myotherapp.in/healthz https://api.myotherapp.in/readyz \
         https://api.myotherapp.in/api/v1/plans https://myotherapp.in/ https://myotherapp.in/events \
         https://myotherapp.in/.well-known/apple-app-site-association https://myotherapp.in/sitemap.xml \
         https://club-dashboard.myotherapp.in/login https://main-portal.myotherapp.in/login \
         https://www.myotherapp.in/x https://events.myotherapp.in/x https://clubs-next.myotherapp.in/x; do
  h=${u#https://}; h=${h%%/*}
  printf '%-70s %s\n' "$u" "$(curl -sk -o /dev/null -w '%{http_code} %{redirect_url}' --resolve "$h:443:127.0.0.1" "$u")"
done
```

Expect 200s, and 308s to the new names for the old ones. From the admin's
machine the same works against `<oracle-ip>` (security list permitting).
Then `sudo ./dc logs --tail 50 backend website` for errors, and
`sudo /srv/moa/deploy.sh status` for memory.

Leave it running: the cutover replaces the copy.

## 9. The cutover

Pick a quiet hour (02:00-03:00 IST). Tell users a few minutes of read-only.
Have the rehearsal's timings, both terminals, and the Cloudflare DNS page
open. Times are from the freeze.

**T-30 min, new box:** pull the images you will run and confirm they are the
ones running: `sudo /srv/moa/deploy.sh status`. The worker is stopped (it was left so after the rehearsal).

Jobs the old box had queued but not finished at the freeze are in the dump
and run on the new box once its worker starts; a job that was half done at
the freeze (a send to a whole event) can repeat a few of its messages.

**T+0 — freeze writes on the old box.** Optional but kinder first: nginx
answers writes with 503 (webhook senders retry a 503 later, by which time
they reach the new box). In each `api.myotherapp.in` server block's
`location /` (and the stream location) of the old box's nginx:

```nginx
if ($request_method !~ ^(GET|HEAD|OPTIONS)$) { return 503 '{"message":"MyOtherApp is moving to a new server. Please try again in a few minutes."}'; }
```

then `sudo nginx -t && sudo systemctl reload nginx`. Then make the database
itself read-only, so nothing can write after the dump:

```bash
sudo docker exec doth-live-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE \"$POSTGRES_DB\" SET default_transaction_read_only = on"'
sudo docker exec doth-live-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '"'"'$POSTGRES_DB'"'"' AND pid <> pg_backend_pid()"'
```

Reads keep working (the API's pool reconnects read-only); writes fail.

**T+1 — final dump, row counts** (old box, as 8.1, file
`/tmp/moa-final.dump`, `/tmp/rowcounts-old.txt`).

**T+2 — move it** (as 8.2) and compare the sha256.

**T+5 — restore** (new box). Stop the apps that hold the copy, replace the
database, restore, analyze, compare:

```bash
cd /srv/moa
sudo ./dc stop backend worker website clubs admin
sudo ./dc exec -T postgres sh -c 'dropdb --force -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'
sudo docker cp /var/backups/myotherapp/moa-final.dump moa-postgres-1:/tmp/restore.dump
sudo ./dc exec -T postgres sh -c 'pg_restore --no-owner --no-acl --exit-on-error -j 2 -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/restore.dump'
sudo ./dc exec -T postgres sh -c 'vacuumdb --analyze-only -j 2 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
sudo ./dc exec -T postgres sh -c 'psql -X -A -t -F " " -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/rowcounts.sql' > /tmp/rowcounts-new.txt
diff /tmp/rowcounts-old.txt /tmp/rowcounts-new.txt && echo "IDENTICAL"
```

Do not go on unless it says IDENTICAL.

**T+10 — start everything on the real data** (the worker now too):

```bash
sudo /srv/moa/deploy.sh backend <backend-sha>     # --migrate if it asks; starts the worker as well (no --no-worker now)
sudo /srv/moa/deploy.sh recreate website
sudo /srv/moa/deploy.sh recreate clubs
sudo /srv/moa/deploy.sh recreate admin
sudo /srv/moa/deploy.sh status
```

Then the 8.5 loop again: all 200/308.

**T+13 — flip DNS** (Cloudflare → DNS). For each record: type **A**, content
**<oracle-ip>**, proxy status **Proxied**, TTL Auto. In this order: `api`,
then `@`, `www`, `events`, then `club-dashboard`, `main-portal`, then
`clubs-next`, `admin-next`, `admin-dashboard` (`www` may be a CNAME to
Vercel today: replace it with the A record). Proxied answers take effect at
Cloudflare at once; resolvers holding the old answer let go within the
1-minute TTL.

Stragglers in that minute: requests still reaching the old API read the
read-only copy (writes fail and are retried); requests reaching Vercel get
the old site and dashboards, whose API calls already land on the new box.

**T+15 — verify** (section 10). The write freeze ends for everyone as their
DNS answer turns over: about **15-25 minutes** of no writes in all.

The old box stays as it is: read-only, its nginx answering writes with 503.

## 10. Verify

From outside (the admin's machine, normal DNS):

```bash
for u in https://api.myotherapp.in/healthz https://api.myotherapp.in/readyz https://myotherapp.in/ \
         https://myotherapp.in/events https://myotherapp.in/.well-known/apple-app-site-association \
         https://myotherapp.in/.well-known/assetlinks.json https://club-dashboard.myotherapp.in/login \
         https://main-portal.myotherapp.in/login; do
  printf '%-62s %s\n' "$u" "$(curl -s -o /dev/null -w '%{http_code} %{remote_ip}' "$u")"
done
curl -sI https://www.myotherapp.in/events?x=1 | grep -iE '^(HTTP|location)'     # 308 → https://myotherapp.in/events?x=1
curl -sI https://clubs-next.myotherapp.in/x | grep -iE '^(HTTP|location)'        # 308 → club-dashboard
curl -sI https://myotherapp.in/ | grep -iE '^(server|cf-ray|strict-transport)'   # cloudflare, a cf-ray, HSTS
```

Checklist:
- [ ] **API**: `readyz` 200 with `"ok": true`, database at the code's head, Redis ok, jobs `mode: worker` with runners.
- [ ] **The app** (a real phone, mobile data and Wi-Fi): sign in, home feed, an event, register for a free event, ticket QR, a push arrives (Firebase from the new box).
- [ ] **Site**: home, `/events`, an event page, a club page, sign in with Google, `/me/tickets`; a share preview (WhatsApp) of an event shows its picture (`/og/...`); "near you" is filled (Managed Transform on).
- [ ] **Dashboards**: sign in at club-dashboard and main-portal (cookies set, survive a reload), edit an event, upload a logo (through the gateway), live room console opens its stream.
- [ ] **ISR**: edit an event in the dashboard, its page on myotherapp.in shows the change within seconds (the API calls `http://website:3000/api/revalidate`).
- [ ] **Payments**: a paid test registration (smallest amount) completes; in the Razorpay dashboard the webhook deliveries to `https://api.myotherapp.in/api/v1/payments/webhook` (and clubs' `/payments/webhook/club/<token>`) show 200 after the flip. The URLs are unchanged, nothing to edit there; deliveries that got 503/500 during the freeze are retried by Razorpay (re-send any still failed from its dashboard).
- [ ] **Mail**: a mail sends (password reset to yourself); SES bounce/complaint notifications reach `/api/v1/hooks/ses` (SNS console: subscription confirmed, deliveries 2xx); Resend webhook deliveries to `/api/v1/hooks/resend` are 2xx (Resend dashboard → Webhooks).
- [ ] **WhatsApp** (if on): Meta's webhook `/api/v1/hooks/whatsapp` shows 200 deliveries (Meta App Dashboard → WhatsApp → Configuration).
- [ ] **Club sites**: `https://<a club slug>.myotherapp.in` renders (the Worker reads `https://api.myotherapp.in`, now through Cloudflare to the new box); a club's own domain, if any.
- [ ] **Old names**: `events.`, `www.`, `clubs-next.`, `admin-next.`, `admin-dashboard.` → 308 with path and query.
- [ ] **Client addresses**: in the admin's sign-in history (or `sudo ./dc logs backend | tail`), your own public IP appears, not Cloudflare's or Docker's.
- [ ] **MCP**: an AI assistant connected to `https://api.myotherapp.in/mcp` still lists tools.
- [ ] **Backups**: `sudo systemctl start myotherapp-backup.service` then `journalctl -u myotherapp-backup -n 20` ends `BACKUP OK` (section 14).
- [ ] **Memory**: `sudo /srv/moa/deploy.sh status` an hour later; nothing near its limit, swap barely used.

**Old API names.** Before switching the old box off, look at what still
reaches it under other names (`api.staging.myotherapp.in`, `api.finnet.app`,
`api.myotherapp.com`): `sudo awk '{print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head` per vhost's log, or `grep -c 'Host'`. If old app builds still call one, point its DNS record (DNS-only) at the new box and uncomment/add its block at the end of `ops/server/caddy/Caddyfile` (Caddy fetches a Let's Encrypt certificate for it by itself), then `Deploy to Oracle → stack`.

**Hardening (after a quiet week):**
- Limit 80/443 in the security list to Cloudflare's ranges
  (`/srv/moa/cloudflare-ips.sh --print`), unless an old API name above stays
  DNS-only (Let's Encrypt needs port 80 from anywhere).
- Or Authenticated Origin Pulls (snippets.caddy has the block): only
  Cloudflare can complete a TLS handshake.
- Run `/srv/moa/cloudflare-ips.sh` now and then: Cloudflare's ranges rarely
  change, but Caddy must trust exactly them.
- An external uptime check (any free monitor) on
  `https://api.myotherapp.in/readyz` and `https://myotherapp.in/robots.txt`,
  every minute: there is one box (section 16).

## 11. The week after, and decommissioning

- **Keep the old box running for 7 days**, read-only (as the freeze left it),
  as the way back (section 12). It also still runs bcards and letmego:
  "decommissioning" here means MyOtherApp's containers and vhosts only.
- **Keep the Vercel projects** (moa-events, moa-clubs-next, moa-admin-next)
  for the same week; their domains point elsewhere, so nobody reaches them.
  Turn off their deploys at once: in moa_web, disable `deploy.yml`
  (Actions → Deploy → Disable workflow); in moa_events, the Vercel Git
  integration (Settings → Git → Disconnect), so a merge no longer deploys
  there.
- **Day 7, if nothing went back:**
  1. Vercel: remove the domains from each project (myotherapp.in, www,
     events, club-dashboard, main-portal, clubs-next, admin-next,
     admin-dashboard, admin-official), then pause or delete the projects.
     Domains first: a project deleted with a domain attached can keep
     claiming it.
  2. Old box: one last dump to S3 (`scripts/backup.py` there), then
     `sudo docker-compose -f docker-compose.prod.yml stop backend worker redis meilisearch postgres`,
     remove the MyOtherApp nginx vhosts (`api.myotherapp.in`,
     `api.staging.myotherapp.in` and the `finnet`/`.com` ones if moved),
     `sudo nginx -t && sudo systemctl reload nginx`. Keep the stopped
     volumes until a month has passed, then `docker volume rm`.
  3. GitHub: disable `live-deploy.yml` and `dev-deploy.yml`, delete the
     `PROD_SSH_PRIVATE_KEY`, `PROD_REMOTE_IP`, `PROD_REMOTE_USER`,
     `PROD_PROJECT_PATH`, `PROD_ENV_VARIABLES` secrets (the last is the stale
     March `.env` and must never be synced again), and the Vercel ones
     (`VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID_*`) in moa_web;
     revoke the Vercel token.
  4. Rotate what the old box and Vercel held and that leaked before: the
     Postgres password, the AWS keys (S3, SES) and, if not done in October,
     the wallet pass key — each on the new box's `.env`, then
     `deploy.sh recreate backend`.
  5. The EC2 instance itself only when bcards and letmego have a new home;
     then release its Elastic IP.

## 12. Rollback

**Before the DNS flip (T+0 to T+13):** abort. On the old box:

```bash
sudo docker exec doth-live-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE \"$POSTGRES_DB\" RESET default_transaction_read_only"'
sudo docker exec doth-live-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '"'"'$POSTGRES_DB'"'"' AND pid <> pg_backend_pid()"'
```

remove the nginx `if` line and reload. Everything is as it was; the
downtime was the freeze.

**After the flip, the same day:** writes made on the new box must come back.
1. Freeze the new box: `sudo ./dc stop backend worker` (or the same
   `default_transaction_read_only` trick through `./dc exec postgres`).
2. Dump it: `sudo ./dc exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > /var/backups/myotherapp/rollback.dump`, move it to the old box (8.2, the other way).
3. On the old box: restore into a new database and swap names (docs/OPERATIONS.md, section 4, "Restoring production", steps 4-5), clear `default_transaction_read_only`, remove the nginx 503 line.
4. Cloudflare: put each record back to its noted value (DNS-only: `api` → the EC2 address, the others → Vercel's `76.76.21.21` / `cname.vercel-dns.com`).
5. Re-enable the Vercel deploys if they were turned off.

**A bad deploy later** is not a rollback of the move: `deploy.sh` puts the
previous image back by itself when a new one is unhealthy, and
`sudo /srv/moa/deploy.sh rollback <service>` (or **Deploy to Oracle →
rollback**) returns to the image before the last deploy at any time.

**The box is lost** (instance gone, volume intact): new instance, `setup.sh`
with the same `--data-device` (no `--format-data`), copy `/srv/moa` back
from `state/stack-*.tgz` or the repository plus the hand-made files (`.env`,
`env/`, `certs/`, `secrets/` — keep an encrypted copy of these off the box),
`deploy.sh start`. **Volume lost too:** the same on a new volume, then
restore the newest S3 dump (section 14).

## 13. Staging

A second, smaller copy of the same stack: same files, `APP_ENV=staging`,
test keys, the demo data, its own names:

| | Production | Staging |
|---|---|---|
| Site | myotherapp.in | staging.myotherapp.in |
| API | api.myotherapp.in | api-staging.myotherapp.in |
| Club dashboard | club-dashboard.myotherapp.in | club-dashboard-staging.myotherapp.in |
| Main portal | main-portal.myotherapp.in | main-portal-staging.myotherapp.in |
| Caddyfile | `caddy/Caddyfile` | `caddy/Caddyfile.staging` (adds `X-Robots-Tag: noindex`) |
| Images | `<sha>` | backend `<sha>`, site and dashboards `staging-<sha>` |

One label each under myotherapp.in, so the zone's free edge certificate and
the same Origin CA certificate cover them (`api.staging.myotherapp.in` would
not be). Add "None" Worker routes for the four names too (none is in the
Worker's reserved list except `staging`).

**Cost:** the production box already uses the whole Always Free Ampere
allowance since June 2026, so a staging box is paid: A1 at 1 OCPU / 6 GB is
about **$14 a month** (2 OCPU / 12 GB about $28), plus its boot volume (free
while inside the 200 GB). On 6 GB, set in its `.env`: `MOA_API_WORKERS=1`,
and stop Meilisearch if not needed (search falls back to Postgres).

Set-up: the same as production with `setup.sh --staging` (installs the
monthly restore drill instead of the daily backup), its own `.env` built
from `env/backend.env.example`'s staging section (`MOA_CADDYFILE`,
`MOA_*_HOST`, `APP_ENV=staging`, `APP_SENTRY_ENVIRONMENT=staging`,
`APP_PUBLIC_SITE_URL`, `APP_PUBLIC_WEB_URL`, `APP_CORS_ORIGINS`, Razorpay
**test** keys and webhook secret, its own `POSTGRES_PASSWORD` and
`APP_SECRET_KEY`), the demo data (`scripts/demo` seeds clubs, events and a
showcase login), and GitHub's `staging` Environment with its own
`ORACLE_SSH_*`. Then `STAGING_ENABLED=true` and `AUTO_DEPLOY_STAGING=true`
in moa_events and moa_web deploy every merge to main there. Optionally put
the four staging names behind Cloudflare Access (free for 50 users) so only
the team sees them.

## 14. Backups

The daily backup and the monthly restore drill come with backend PR #102
(`ops/backup/`, docs/OPERATIONS.md section 19 there). They fit this layout
as they are: setup.sh writes `/etc/default/myotherapp-backup` with
`MOA_PROJECT_DIR=/srv/moa`, `MOA_COMPOSE_FILE=compose.yml`,
`MOA_SERVICE=backend`, installs the units and enables the timer (02:30 IST)
once `ops/backup` is on the box (`Deploy to Oracle → stack` after #102 is
merged, then run setup.sh again). The dump runs in the backend container
(`pg_dump` 17 is in the image) into `/var/backups/myotherapp` (on the data
volume) and is uploaded off the box.

**Where to: AWS S3, `ap-south-1`, its own bucket — recommended over
Cloudflare R2.**
- Off the provider that runs the box: an OCI account problem (a suspended
  tenancy, a reclaimed instance, a deleted volume) cannot take the backups
  with it. That rules out OCI Object Storage as the only copy.
- `scripts/backup.py` and `restore_drill.py` already speak S3: server-side
  encryption, a sha256 per object, the daily/weekly prefixes, and the
  IAM policy that can write and read but never delete
  (docs/OPERATIONS.md, section 4). R2 is S3-compatible but would need an
  endpoint setting the scripts do not have yet; its free egress only
  matters for restores, and a few GB a month costs cents on S3.
- Settings in `.env`: `APP_BACKUP_S3_BUCKET`, `APP_BACKUP_S3_REGION=ap-south-1`,
  `APP_BACKUP_S3_ACCESS_KEY`, `APP_BACKUP_S3_SECRET_KEY` (the backup-only IAM
  user), `APP_BACKUP_KEEP_LOCAL=7`; versioning and the lifecycle rule on the
  bucket. Then `sudo /srv/moa/deploy.sh recreate backend`.
- Second layer, optional: the OCI block volume backup policy (section 3).

Restore drill on staging, monthly (the timer `setup.sh --staging` enables),
proves the dumps restore.

## 15. Running it

| Task | Command |
|---|---|
| What runs, health, memory, disk | `sudo /srv/moa/deploy.sh status` |
| Deploy the API (and worker) | `sudo /srv/moa/deploy.sh backend <sha> [--migrate]` or Deploy to Oracle |
| Deploy the site / a dashboard | `sudo /srv/moa/deploy.sh website|clubs|admin <tag>` or the repo's image workflow |
| Undo the last deploy | `sudo /srv/moa/deploy.sh rollback <service>` |
| Apply an `.env` edit | `sudo /srv/moa/deploy.sh recreate backend` (website, clubs, admin for `env/*.env`) |
| Change the Caddyfile | edit in git → Deploy to Oracle → stack (validates, reloads) |
| Logs | `sudo moa-dc logs -f --tail 200 backend` |
| A psql session | `sudo moa-dc exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` |
| Database from a laptop | `ssh -L 5432:127.0.0.1:5432 ubuntu@<ip>` |
| Migration state | `sudo moa-dc exec -T backend alembic current` |
| OS updates needing a reboot | `cat /var/run/reboot-required` → `sudo reboot` at a quiet hour (containers come back by themselves, ~1-2 min) |

`deploy.sh` refuses a backend image whose migrations the database lacks
unless `--migrate` is given, and with it migrates with the new image while
the old one still serves (migrations must stay backward-compatible, as
today). Every image runs pinned by digest (`images.env`), so a rollback is
exact even after `latest` has moved. Images are pulled, never built on the
box.

## 16. Risks

- **One box, no redundancy.** A host failure, a kernel panic or an AD outage
  takes everything down until the instance is back or rebuilt (section 12:
  under an hour with the data volume, a few hours from S3). Mitigations: the
  reserved IP and the separate data volume make a rebuild quick; external
  uptime alerts; the S3 dumps; an off-box encrypted copy of `.env`, `env/`,
  `certs/`, `secrets/`. Cloudflare keeps serving cached static assets, and
  the club-sites Worker keeps serving the club pages it has cached (5
  minutes).
- **Capacity.** Two cores for nine services: the API, renders (wkhtmltopdf,
  certificates, wallet passes), Next.js server rendering and Postgres
  contend at peaks (a big event's check-in rush, a certificate send to a
  whole event, OG images for a link going viral). The CPU weights put the
  API and Postgres first; Cloudflare caching `/og/*` (section 7) takes the
  heaviest anonymous load away. Memory is capped per service with ~3 GB
  free. Growth path: the same box with more OCPUs/GB is a reboot away
  (Edit shape), and the limits in compose.yml scale up with it.
- **ARM.** Every image is built and published for arm64; all Python
  dependencies have aarch64 wheels; sharp has a linux-musl arm64 build.
  Not yet proven: an arm64 container actually *running* (no Docker on the
  machine that prepared this). First arm64 builds in CI, then the rehearsal,
  are the proof; the AMD shape is the fallback with no change but the shape.
- **Always Free.** The allowance was halved in June 2026 (2 OCPU / 12 GB);
  Oracle can change it again. On PAYG the worst case is paying list price
  (~$28/month for this shape), never a reclaimed box. Free-tenancy idle
  reclamation does not apply on PAYG. Capacity for A1 can be short in a
  region: create the instance early, keep it.
- **Cloudflare in the path for the API.** Bot Fight Mode, Browser Integrity
  Check or a high Security Level would challenge the app and webhooks
  (section 7 turns them off for `api.`). Proxied requests time out at 100 s
  (the old nginx cut at 60 s, so no endpoint relies on more). Uploads are
  capped at 100 MB by Cloudflare and 10 MB by Caddy (as nginx did).
- **What Vercel did that this does not:** the edge cache of `/events`,
  `/clubs`, `/topics/*` (now per request; optional Cloudflare rule for
  `/og`, `/media`), preview deployments per branch (staging instead), and
  automatic scaling (none: one box).
- **Secrets on one disk.** `.env`, the origin key and the wallet key live on
  the box (root, 600). The CI key can only deploy images through the gate;
  `stack` can install new scripts, so whoever can run the workflow can
  change what runs as root — protect `production` with required reviewers.
- **GHCR visibility.** Public packages expose the built code (the
  repositories are public anyway); if the repositories go private, decide
  between public packages and a read-only token on the box (5.5).

## 17. Cost

Monthly, at OCI list prices (verify in OCI's cost estimator for the region):

| Item | Production | Notes |
|---|---|---|
| Ampere A1, 2 OCPU / 12 GB | **$0** | inside the Always Free 1,500 OCPU-h / 9,000 GB-h, in the home region, PAYG account |
| Boot 50 GB + data 100 GB | **$0** | inside the 200 GB Always Free block storage |
| Reserved public IP | $0 | while attached |
| Outbound traffic | $0 | first 10 TB/month free |
| Volume backups | $0 | up to 5 free |
| S3 backups (ap-south-1) | ~$0.25-1 | a few GB with the 14-day/8-week lifecycle |
| Cloudflare | $0 | free plan (Worker free tier: 100k requests/day) |
| GitHub Actions + GHCR | $0 | public repositories, public packages |
| **Total** | **~$1/month** | |
| Staging (optional) | ~$14 (1 OCPU/6 GB) – $28 (2/12) | Always Free is used up by production |
| If not free (home region elsewhere, or AMD) | A1 2/12: ~$28; E5 2 OCPU/12 GB: ~$62 (1 OCPU: ~$40); +$0.0425/GB-month for block storage | |

Unchanged and outside this: S3 media and its egress, SES/Resend, Razorpay,
Sentry, the domain. Saved: the share of the EC2 instance MyOtherApp used
(the box stays for bcards and letmego) and any Vercel plan.

## 18. Every secret, by name

**On the box** (root, mode 600; never in git, never in GitHub):
- `/srv/moa/.env`: everything the old box's `.env` holds (`POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`, `APP_SECRET_KEY`, `APP_LINK_SIGNING_KEYS`,
  `APP_S3_*`, `APP_SES_*`, `APP_RESEND_API_KEY`, `APP_RESEND_WEBHOOK_SECRET`,
  `APP_RAZORPAY_KEY_ID`, `APP_RAZORPAY_KEY_SECRET`, `APP_RAZORPAY_WEBHOOK_SECRET`,
  `APP_PAYMENT_KEYS_SECRET`, `APP_GOOGLE_*`, `APP_FIREBASE_SERVICE_ACCOUNT_*`,
  `APP_SITE_TOKENS`, `APP_SITE_REVALIDATE_SECRET`, `APP_GEMINI_API_KEY`,
  `APP_AI_KEYS_SECRET`, `APP_WHATSAPP_*`, `APP_CLOUDFLARE_API_TOKEN`,
  `APP_DISCORD_*`, `APP_SENTRY_DSN`, `WALLET_*` …) plus `MEILI_MASTER_KEY`
  and the `APP_BACKUP_S3_*` keys.
- `/srv/moa/env/website.env`: `REVALIDATE_SECRET`, `API_SITE_TOKEN`.
- `/srv/moa/certs/origin.key` (and `origin.pem`).
- `/srv/moa/secrets/walletethings/*.pem`, `/srv/moa/secrets/service-account.json`.
- `/root/.docker/config.json` (only with a GHCR read token).

**GitHub, per Environment (`production`, `staging`), in all three repositories:**
`ORACLE_SSH_HOST`, `ORACLE_SSH_USER`, `ORACLE_SSH_KEY`, `ORACLE_SSH_KNOWN_HOSTS`,
optional `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`.

**GitHub, repository secrets:** moa_events: `SENTRY_AUTH_TOKEN`,
`API_SITE_TOKEN`, `STAGING_API_SITE_TOKEN`; moa_web: `SENTRY_AUTH_TOKEN`.
(`GITHUB_TOKEN` pushes the images; nothing to set.)

**GitHub, repository variables (not secret):** `ORACLE_IMAGES`,
`STAGING_ENABLED`, `AUTO_DEPLOY_STAGING`, `ARM64_RUNNER`.

**Elsewhere:** the Tailscale OAuth client (Tailscale admin), the S3 backup
IAM user (AWS), the Cloudflare Origin CA certificate (Cloudflare).
