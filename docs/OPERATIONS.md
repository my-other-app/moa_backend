# Operations runbook

How to deploy, roll back, unstick a migration, restore a backup, rotate a
secret, turn on the new services, size the server and every service on it,
keep its disk from filling, open the live room's streams, look after the
rendered documents in the bucket, run AI assist, put a demo event live
for People Like You, place visitors by address without a lookup service,
fix the demo's venue pins, turn on WhatsApp, and keep production's
settings, health checks and scheduled backups honest (section 19). Every
command is meant to be copied.
Commands under **Read-only** change nothing; anything that changes something
says so.

The production box runs `docker-compose.prod.yml` from
`/var/www/myotherapp-production` (the `PROD_PROJECT_PATH` secret): `postgres`
(17.4, data in the `postgres_data` volume) and `backend` (code bind-mounted
from `./backend`, published on 8000 and 8001 behind the host's nginx). The
file also defines `worker`, `redis` and `meilisearch`, which run only once
started on purpose (section 6), and gives every service a memory and CPU
limit (section 10). Backups live outside the project directory, in
`/var/backups/myotherapp`.

On the server, everything below starts with:

```bash
cd /var/www/myotherapp-production
DC="sudo docker-compose -f docker-compose.prod.yml"
```

and, for SQL, a read-only psql session (the postgres container's own
superuser over its local socket; no password is typed or printed):

```bash
$DC exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SET default_transaction_read_only = on;
SET statement_timeout = '10s';
-- the query
SQL
```

---

## 1. Deploy

The **Live Deployment** workflow (`.github/workflows/live-deploy.yml`, run by
hand from the Actions tab). Its inputs:

| Input | What it does |
|---|---|
| `sync_code` | rsyncs the repository to the server (and writes `backend/REVISION`, the commit, for Sentry) |
| `sync_env` | writes the `PROD_ENV_VARIABLES` secret to the server's `.env`. **It overwrites the file**: make sure the secret is current first |
| `install_requirements` | builds the backend image, migrates (if `migrate`) in the running container, then recreates the backend from the new image |
| `recreate_backend` | a new backend container (and a running worker's), built first: how an `.env` change reaches the process (`restart` keeps the old environment). The build is the cache's when nothing changed (seconds), so a container is never made from an image older than `requirements.txt` (the 2026-10-17 outage). With `migrate`, migrates first |
| `migrate` | `alembic upgrade head` in the running backend container, then prints the applied revision |
| `restart_backend` | `restart backend` (new code, same environment), skipped when the backend was recreated |
| `build_backend`, `up_backend` | build the image / build, then `up backend` (without `--no-deps`, which can also recreate postgres after an `.env` change: prefer `recreate_backend`) |
| `restart_database` | `up -d postgres` |
| `restart_all` | `up --build -d postgres backend` (old order: recreate, then migrate), and recreates a running worker. Never the other services: a bare `up -d` would start them all |
| `services` | starts or refreshes extra services after the migration, e.g. `redis worker meilisearch`: `up -d --build --no-deps <svc>` each (the worker is built from the cache first; the others have nothing to build). Not `backend` or `postgres` |
| `smoke_timeout` | seconds the API has to answer 200 three times running after the deploy (default 180) |

Common runs:

| Change | Inputs |
|---|---|
| Code only | `sync_code`, `restart_backend` |
| Code + migration | `sync_code`, `migrate`, `restart_backend` |
| New or changed requirements | `sync_code`, `install_requirements`, `migrate` |
| `.env` change only | `sync_env`, `recreate_backend` |
| Turn on a new service | `services: redis` first, then `sync_env` (its settings) with `recreate_backend` (section 6) |

What the job guarantees:

- The remote script is rendered on the runner, checked with `bash -n`, copied
  to the server and run from a file with `set -e`; its last line prints
  `MOA_DEPLOY_COMPLETE`, and the job fails if the marker is missing.
- Builds use BuildKit. The script checks `docker buildx version` before
  building and stops with nothing changed if it is missing (section 9).
- Migrations run in the container that is serving, before any recreate: a
  failed migration leaves the old container serving.
- Every recreate builds first (`recreate_backend`, `up_backend`, the
  `services` input): from the cache, in seconds, when nothing changed, and
  never a container from an image older than `requirements.txt`.
- A running worker goes with the backend: recreated from the new image when
  the backend was (or `restart_all` rebuilt it), restarted when only the
  code changed. It is never started by the deploy except by `services`.
- After a build, unused images and day-old build cache are pruned (section
  11); a failed prune only warns.
- **Smoke check**: after the deploy the job asks
  `https://api.myotherapp.in/api/v1/plans` (or the repository variable
  `PROD_SMOKE_URL`; set it to `https://api.myotherapp.in/readyz` once that
  exists) until it answers 200 three times running, and fails if that takes
  longer than `smoke_timeout`. Each try sends `X-Request-ID:
  deploy-smoke-<run>-<n>`, so its log lines can be found. A red smoke check
  means: look now (section 2).

### Migrations and the guard

`alembic upgrade head` runs each migration in its own transaction, with:

| Setting (`.env`) | Default | Meaning |
|---|---|---|
| `APP_MIGRATION_LOCK_TIMEOUT` | `5s` | a DDL statement waits this long for its lock, then its migration rolls back and is tried again |
| `APP_MIGRATION_LOCK_ATTEMPTS` | `4` | tries per migration (and per statement inside an autocommit block) before failing |
| `APP_MIGRATION_RETRY_WAIT` | `5` | seconds before the first retry; doubles each time, with jitter |
| `APP_MIGRATION_STATEMENT_TIMEOUT` | `30s` | the longest any statement may run while holding its locks |
| `APP_MIGRATION_ONLINE_STATEMENT_TIMEOUT` | `0` (none) | for `CREATE/DROP INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`, `VALIDATE CONSTRAINT`, `VACUUM`, `ANALYZE` in an autocommit block: they block no one, so they may take as long as they need |
| `APP_MIGRATION_IDLE_TIMEOUT` | `60s` | `idle_in_transaction_session_timeout`: a stuck migration process loses its session and its locks |

Migrations connect straight to Postgres (`APP_DATABASE_URL_SYNC`); keep it
that way if PgBouncer arrives, since the guard's session settings would not
survive a transaction-mode pooler.

The deploy log shows the guard (`Migration guard: lock_timeout=5s ...`) and
every retry (`lock timeout on a migration (after X), rolled back; trying again
in 5.3s (attempt 2 of 4)`). For one run with other limits:

```bash
# Changes the database: runs the migrations.
$DC exec -T -e APP_MIGRATION_STATEMENT_TIMEOUT=10min backend alembic upgrade head
```

Writing a new migration: `python scripts/check_migrations.py` (also in the
lint workflow) flags, in every migration after `topic_placeholders`, a non-concurrent
index on a hot table, CONCURRENTLY outside an autocommit block, ADD COLUMN NOT
NULL without a default, an UPDATE of a whole table or one not in committed
batches, a column type change or ALTER TYPE, and DDL on a hot table without
`SET LOCAL lock_timeout` first. The shape it accepts:

```python
def upgrade():
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("events", sa.Column("score", sa.Integer(), nullable=True))
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while bind.execute(sa.text(
            "UPDATE events SET score = 0 WHERE id IN "
            "(SELECT id FROM events WHERE score IS NULL LIMIT 5000)"
        )).rowcount:
            pass
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_score")  # if a failed run left it INVALID
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_events_score ON events (score)")
```

Keep the part before an autocommit block re-runnable too (`IF NOT EXISTS`):
the block commits it, and if the migration fails later, `upgrade head` runs
it again from the top.

---

## 2. Rollback

First decide what broke. **Read-only:**

```bash
$DC ps
$DC logs --tail=200 backend
$DC exec -T backend alembic current
curl -sS -o /dev/null -w '%{http_code}\n' https://api.myotherapp.in/api/v1/plans
```

**Code** (the common case). The code is bind-mounted, so rolling back is
deploying the previous commit: in GitHub, *Actions -> Live Deployment -> Run
workflow*, choose the last good tag or commit's branch (or `git revert` the bad
merge on `master`), with `sync_code` and `restart_backend`. Do not include
`migrate` unless the old code needs a downgrade (below).

**Image** (a bad requirements change). The build replaced the image, so tag
the running one before any `install_requirements` you are unsure of (the
deploy prunes untagged images no container runs once the new one is up, so
an untagged old image is gone after it; a tagged one is kept):

```bash
IMG=$(sudo docker inspect --format '{{.Config.Image}}' doth-live-backend)    # read-only
sudo docker tag "$IMG" "$IMG-previous"                                       # changes: adds a tag
```

and to go back (changes: recreates the backend, and the worker if it runs):

```bash
sudo docker tag "$IMG-previous" "$IMG"
$DC up -d --no-deps --force-recreate backend worker < /dev/null      # leave worker out if it is not running
```

No `--build` here, on purpose: this is the one recreate that must use the
image as tagged, and a build would make a new one from the current
requirements over the tag you just restored. Everywhere else a recreate
builds (section 6).

**Schema.** Prefer a forward fix. If the new code must go and the old code
cannot run on the new schema, take a backup first (section 4), then
(changes the database):

```bash
$DC exec -T backend alembic downgrade <the revision before>
```

Downgrades are rarely exercised: read the migration's `downgrade()` first.
Once `topics` has run, downgrading below `add_event_types` fails (the topics
seed makes event types with no creator, which that downgrade cannot make
NOT NULL); every downgrade above it works.

**Never restart the backend after a failed migration** without deciding
which code it should run: the synced code expects the new schema.

---

## 3. A stalled migration

Symptoms: the deploy log sits on `Running upgrade X -> Y`, or prints
`lock timeout on a migration ... trying again`, and requests on one table slow
down. The migration's session is named `alembic-migration`.

**Read-only.** What every session is doing, oldest transaction first:

```sql
SELECT pid, application_name, state, wait_event_type, wait_event,
       now() - xact_start  AS in_transaction,
       now() - query_start AS this_query,
       left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid()
ORDER BY xact_start NULLS LAST;
```

Who is blocking whom (the `blocking_pid` with the longest `blocking_for` is
the one everyone waits behind):

```sql
SELECT waiting.pid                               AS waiting_pid,
       waiting.application_name                  AS waiting_app,
       left(waiting.query, 80)                   AS waiting_query,
       now() - waiting.query_start               AS waiting_for,
       blocking.pid                              AS blocking_pid,
       blocking.application_name                 AS blocking_app,
       blocking.state                            AS blocking_state,
       now() - blocking.xact_start               AS blocking_for,
       left(blocking.query, 80)                  AS blocking_query
FROM pg_stat_activity AS waiting
CROSS JOIN LATERAL unnest(pg_blocking_pids(waiting.pid)) AS b(pid)
JOIN pg_stat_activity AS blocking ON blocking.pid = b.pid
ORDER BY blocking_for DESC NULLS LAST;
```

The locks on a table, granted and waiting (replace `events`):

```sql
SELECT l.pid, a.application_name, l.mode, l.granted,
       now() - a.xact_start AS in_transaction, left(a.query, 80) AS query
FROM pg_locks l
JOIN pg_class c ON c.oid = l.relation
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE c.relname = 'events'
ORDER BY l.granted DESC, a.xact_start;
```

An index being built concurrently, and how far along:

```sql
SELECT pid, relid::regclass AS table_name, index_relid::regclass AS index_name, phase,
       blocks_done, blocks_total, tuples_done, tuples_total
FROM pg_stat_progress_create_index;
```

Indexes a failed CONCURRENTLY build left INVALID (the migration drops and
rebuilds them when re-run; the planner ignores them meanwhile):

```sql
SELECT indexrelid::regclass AS index_name, indrelid::regclass AS table_name
FROM pg_index WHERE NOT indisvalid;
```

Then:

- **It is retrying.** Let it: each retry rolled back and released its locks,
  so traffic flows between tries. After `APP_MIGRATION_LOCK_ATTEMPTS` it fails
  with `Gave up after N attempts` and nothing of that migration is applied
  (the ones before it are). Run the deploy again once the blocker is gone.
- **A backup is the blocker** (`application_name` `pg_dump`): it holds a
  share lock on every table until it finishes, so no ALTER can run meanwhile.
  Wait for it, and do not deploy migrations at the backup time (21:30 UTC).
- **A long query or an `idle in transaction` session is the blocker.** If it is
  a report or a stuck request, cancel its query, or end its session (both
  change things; the app retries or the user sees one error):

  ```sql
  SELECT pg_cancel_backend(<blocking_pid>);     -- cancels the running query
  SELECT pg_terminate_backend(<blocking_pid>);  -- ends the session and rolls back its transaction
  ```

  (Run these without the `default_transaction_read_only` line.)
- **The migration itself is the problem** (a long rewrite holding a hot table),
  stop it; its transaction rolls back:

  ```sql
  SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name = 'alembic-migration';
  ```

- **It failed with `was not run again`**: that migration committed part of its
  work in an autocommit block before the lock timeout. Read it; if its first
  part is not re-runnable, finish or undo that part by hand before
  `alembic upgrade head` again.

---

## 4. Backups and restore

### How backups work

- **On demand**: the admin dashboard's *Backups* (`POST /api/v1/admin/backups`,
  owners only, audited) runs `pg_dump --format=custom` into `/backups` in the
  container, which is `/var/backups/myotherapp` on the host (outside the
  rsynced directory, so a deploy never deletes them).
- **Daily** (once section 19's timer is installed): `scripts/backup.py` takes
  the same dump, copies it off-site, and prunes old local dumps.
- **Off-site**, when `APP_BACKUP_S3_BUCKET` is set: every dump, on demand or
  daily, is uploaded to `s3://<bucket>/backups/daily/` -- or `backups/weekly/`
  for a scheduled dump on a Sunday (UTC) or when the last weekly one is over a
  week old -- with server-side encryption and its sha256 in the object's
  metadata. The API's answer says where (`offsite_key`) or why not
  (`offsite_error`). A failed daily upload exits 1, leaves local dumps
  unpruned and raises an admin alert (`backup_failed`).

Settings:

| Setting | Default | |
|---|---|---|
| `APP_BACKUP_DIR` | `/backups` | in the container |
| `APP_BACKUP_S3_BUCKET` | empty (off) | the bucket for off-site copies |
| `APP_BACKUP_S3_PREFIX` | `backups/` | |
| `APP_BACKUP_S3_REGION` | `ap-south-1` | the bucket's region |
| `APP_BACKUP_S3_SSE` | `AES256` | or `aws:kms` |
| `APP_BACKUP_S3_KMS_KEY_ID` | empty | with `aws:kms`: the key's id, ARN or alias (empty: the AWS-managed key) |
| `APP_BACKUP_S3_ACCESS_KEY`, `APP_BACKUP_S3_SECRET_KEY` | empty | a backup-only IAM user (recommended); empty: the media bucket's keys |
| `APP_BACKUP_KEEP_LOCAL` | `0` (keep all) | dumps the daily run keeps in `/var/backups/myotherapp`; set `7` |

### Setting it up (once)

1. **S3** (in the AWS console or CLI; the app never creates any of this):
   - Create a bucket, e.g. `myotherapp-backups`, in `ap-south-1` (another
     region guards against a regional outage too). *Block all public access*:
     on. *Default encryption*: SSE-S3 (or SSE-KMS). *Versioning*: on, so an
     overwrite cannot destroy a backup.
   - Add the lifecycle rule (daily for 14 days, weekly for 8 weeks; old
     versions and abandoned uploads cleared):

     ```json
     {
       "Rules": [
         {"ID": "backups-daily-14-days", "Status": "Enabled",
          "Filter": {"Prefix": "backups/daily/"},
          "Expiration": {"Days": 14},
          "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
          "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}},
         {"ID": "backups-weekly-8-weeks", "Status": "Enabled",
          "Filter": {"Prefix": "backups/weekly/"},
          "Expiration": {"Days": 56},
          "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
          "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}}
       ]
     }
     ```

     `aws s3api put-bucket-lifecycle-configuration --bucket myotherapp-backups --lifecycle-configuration file://lifecycle.json`
   - Create an IAM user (or role) for backups with only this policy -- it can
     write, list and read backups, and cannot delete any:

     ```json
     {
       "Version": "2012-10-17",
       "Statement": [
         {"Sid": "WriteAndReadBackups", "Effect": "Allow",
          "Action": ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
          "Resource": "arn:aws:s3:::myotherapp-backups/backups/*"},
         {"Sid": "ListBackups", "Effect": "Allow", "Action": "s3:ListBucket",
          "Resource": "arn:aws:s3:::myotherapp-backups",
          "Condition": {"StringLike": {"s3:prefix": ["backups/*"]}}}
       ]
     }
     ```

     With `aws:kms`, also allow `kms:GenerateDataKey` and `kms:Decrypt` on the key.
2. **Settings**: add to the `PROD_ENV_VARIABLES` secret
   `APP_BACKUP_S3_BUCKET=myotherapp-backups`, the user's
   `APP_BACKUP_S3_ACCESS_KEY` and `APP_BACKUP_S3_SECRET_KEY`, and
   `APP_BACKUP_KEEP_LOCAL=7`; deploy with `sync_env` and `recreate_backend`.
3. **Check**: take a backup from the dashboard; its answer shows
   `offsite_key: backups/daily/backup-....dump`. Then run the restore drill
   (below) once.
4. **Schedule**: the systemd timer in `ops/backup` -- every day at 02:30
   IST, with an alert when a run fails -- and, on staging, the monthly
   restore drill. Section 19 has the install steps. (This used to be two
   root crontab lines; remove them if they are installed, so the backup does
   not run twice.)

   Each run prints one line per step and ends with `BACKUP OK` or
   `RESTORE DRILL PASSED`; neither ever prints a credential.

### Restore drill (monthly, and after any change to backups)

```bash
# Creates and drops a scratch database on this server; reads the live one.
$DC exec -T backend python scripts/restore_drill.py                  # the newest dump in S3
$DC exec -T backend python scripts/restore_drill.py --source local   # the newest in /var/backups/myotherapp
$DC exec -T backend python scripts/restore_drill.py --keep           # leave restore_drill_* to look at
```

It checks the download against the sha256 recorded at upload, restores into
a new `restore_drill_YYYYMMDD_HHMMSS` database with `pg_restore`, compares the
alembic revision and the rows of `users`, `clubs`, `events`,
`event_registrations_link`, `club_users_link`, `payment_logs`,
`payment_orders` and `notifications` with the live database (a 5-second cap,
then the planner's estimate), and drops the scratch database. It costs CPU
and disk while it runs: off-peak, or set `RESTORE_DRILL_DATABASE_URL` (the
environment of the exec) to restore on another server.

### Restoring production (the real thing)

Only after deciding that the data now is worse than the data in the dump.
Everything here changes the database.

1. Stop writes: `$DC stop backend` (the site is down from here).
2. Keep what is there: `$DC exec -T backend python scripts/backup.py` will not
   work with the backend stopped, so dump from the postgres container:
   `$DC exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' | sudo tee /var/backups/myotherapp/before-restore.dump > /dev/null`
3. Get the dump to restore into `/var/backups/myotherapp` (from S3:
   `aws s3 cp s3://myotherapp-backups/backups/daily/<name> /var/backups/myotherapp/`
   with credentials that can read it) and check it is the file that was
   uploaded: `sha256sum` of it must equal the object's
   `aws s3api head-object --bucket myotherapp-backups --key backups/daily/<name> --query Metadata.sha256`.
4. Restore into a new database, then swap names:

   ```bash
   sudo docker cp /var/backups/myotherapp/<name> doth-live-postgres:/tmp/restore.dump
   $DC exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" restored && pg_restore --no-owner --no-acl -U "$POSTGRES_USER" -d restored /tmp/restore.dump'
   $DC exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE \"$POSTGRES_DB\" RENAME TO broken_$(date +%Y%m%d)" -c "ALTER DATABASE restored RENAME TO \"$POSTGRES_DB\""'
   ```

5. `$DC up -d --build --no-deps backend < /dev/null` (and `worker` if it
   runs; it was not stopped, so restart it: `$DC restart worker`), check
   `alembic current` matches the code (migrate if the dump is older), and
   the smoke URL answers.
6. Drop `broken_*` only when sure.

---

## 5. Rotating secrets

Secrets live in one place: the `PROD_ENV_VARIABLES` repository secret, which
`sync_env` writes to the server's `.env`. A rotation is: make the new value
where it is issued, update the secret, deploy with `sync_env` and
`recreate_backend`, check, then revoke the old value. (`restart_backend`
does not pick up `.env` changes.) Never paste a secret into an issue, a
commit or a chat; to check a value reached the container without printing
it: `$DC exec -T backend sh -c 'printenv APP_X | sha256sum'` and compare with
the hash of what you set.

| Secret | Issued by | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | you | see below: the database must change first |
| `APP_SECRET_KEY` | `openssl rand -hex 32` | signs every token: everyone is signed out. While `APP_LINK_SIGNING_KEYS` is unset it also signs the links in emails, and rotating it breaks every one already sent: set the link keys first (below) |
| `APP_LINK_SIGNING_KEYS` | `openssl rand -hex 32` (each) | a list: the new key **first**, the old kept after it for 90 days; see below |
| `APP_RAZORPAY_KEY_ID` / `_SECRET` | Razorpay dashboard -> API keys | regenerate, update, recreate |
| `APP_RAZORPAY_WEBHOOK_SECRET` | Razorpay dashboard -> Webhooks | set the same value in both, then recreate at once: webhooks fail in between (Razorpay retries) |
| `APP_PAYMENT_KEYS_SECRET` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | a comma-separated list: put the new key **first**, keep the old after it; drop the old once every saved club key has been re-saved |
| `APP_SITE_TOKENS` | `openssl rand -hex 32` | a list: add the new token, update the site (Vercel), then remove the old |
| `APP_SITE_REVALIDATE_SECRET` | `openssl rand -hex 32` | the same value on the site (Vercel) and here |
| `APP_S3_ACCESS_KEY` / `_SECRET_KEY`, `APP_SES_*`, `APP_BACKUP_S3_*` | AWS IAM -> the user -> security credentials | create the second key, switch, recreate, then deactivate and delete the first |
| `APP_RESEND_API_KEY`, `APP_RESEND_WEBHOOK_SECRET` | Resend dashboard | |
| `APP_CLOUDFLARE_API_TOKEN` | Cloudflare -> API tokens (roll) | |
| `APP_WHATSAPP_*` | Meta Business settings -> System users (the token); the app's dashboard -> App settings -> Basic (the app secret) | section 18. A new verify token (`openssl rand -hex 24`) must be set in the webhook too, at once |
| Firebase service account | Google console | |
| Wallet pass signing key and certificate | Apple developer -> Pass Type IDs | replace the files in `backend/wallet_certs`, not in git |
| `PROD_SSH_PRIVATE_KEY` | `ssh-keygen -t ed25519` | add the new public key to the server's `authorized_keys`, update the secret, deploy once, remove the old key |
| `APP_SENTRY_DSN` | Sentry -> project -> Client Keys | not really secret (it can only send events); revoke the old key there |
| `APP_GEMINI_API_KEY` | Google AI Studio -> API keys, on the paid project | one key, never a list (section 14); make the new one, switch, recreate, delete the old |
| `APP_AI_KEYS_SECRET` | the same Fernet command as `APP_PAYMENT_KEYS_SECRET` | a list: the new key **first**, the old after it; drop the old once every club's own AI key has been saved again |

**The links in emails (`APP_LINK_SIGNING_KEYS`).** A guest's ticket and
certificate links (`/api/v1/files/{tickets|certificates}/<code>.pdf?s=...`)
and the feedback survey's (`/feedback/<event>/<registration>?k=...`) never
expire. Each carries an HMAC under the first key in the list; any key in the
list verifies one. Unset, `APP_SECRET_KEY` signs and verifies them, as it
always did -- which is why rotating that secret on 2026-10 broke every link
sent before. A link whose key is no longer listed gets, in a browser, a page
saying the link no longer works, pointing at the app and My tickets, with a
form that sends a fresh link to the address the guest registered with (an
app or a script still gets the 403 JSON). **Never list a key that leaked**,
however many old links it would bring back: anyone holding it could sign a
link to any ticket.

Moving off `APP_SECRET_KEY` (once):

1. `openssl rand -hex 32`: the new link key.
2. `APP_LINK_SIGNING_KEYS=<new key>,<the current APP_SECRET_KEY>`: new links
   are signed with the new key, and those sent since `APP_SECRET_KEY` last
   changed keep working. Update `PROD_ENV_VARIABLES`; deploy with `sync_env`
   and `recreate_backend`.
3. Check: a link from a ticket email sent before the deploy still opens the
   PDF, and a new registration's email carries a link that does too.
4. After 90 days, drop the second key (`APP_LINK_SIGNING_KEYS=<new key>`).
   From then on, rotating `APP_SECRET_KEY` signs everyone out and leaves the
   emailed links alone.

Rotating the link key later: put a new key first (`<newer>,<new>`), deploy;
after 90 days drop the old one. If a link key leaks, drop it at once, not
after 90 days: the links it signed then show the page, and their holders ask
for a fresh one. Why 90 days: most links are for an event weeks away at
most, and a certificate is downloaded within days of being sent; whoever
comes later uses the form. Each key must be at least 32 characters; a
shorter one stops the backend at startup, so check the smoke URL after the
recreate.

**The database password** (changes the database; the backend reconnects on
recreate):

```bash
NEW=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)   # on your machine; keep it in a password manager
```

1. On the server, set it in Postgres (typed into psql's prompt, not the shell
   history):
   `$DC exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` then
   `\password` and the role name.
2. Update `POSTGRES_PASSWORD` in `PROD_ENV_VARIABLES`, deploy with `sync_env`
   and `recreate_backend`. The backend's URL is rebuilt from it
   (`docker-compose.prod.yml`), so nothing else changes. Postgres itself only
   reads `POSTGRES_PASSWORD` when its volume is first created; the running
   server keeps the password set in step 1.
3. Check the smoke URL; the backend logs show no authentication errors.

---

## 6. Starting the new services

`docker-compose.prod.yml` defines three services the normal deploy never
starts: `redis`, `worker` and `meilisearch`. Each is off until its setting is
set, and the app runs as before without it.

**Not on today's box.** The t2.medium (4 GB, about 195 MB free on
2026-10-18, no swap) cannot hold any of them: a new container there risks
the kernel's OOM killer taking the backend or Postgres. Resize first
(section 10, *Resizing*), then start them one at a time, watching
`free -m` and `sudo docker stats --no-stream` after each.

Order for each: deploy the code (with `migrate`), start the service, then
set its setting and recreate what reads it.

| Service | 1. Start (workflow input) | 2. Set (`PROD_ENV_VARIABLES`), then recreate | Turn off |
|---|---|---|---|
| Redis | `services: redis` | `APP_REDIS_URL=redis://redis:6379/0` (rate limits, the discover cache's second level and live fan-out use it) | unset it, recreate, then `$DC stop redis` |
| Job worker | `services: worker` (after the migration that makes `jobs`) | `APP_JOBS_WORKER=true` on the API: it then only enqueues. Optionally `APP_SWEEPS_IN_WORKER=true`: the sweeps move too | unset first, recreate, wait for the queue to drain (`GET /api/v1/admin/jobs`), then `$DC stop worker` |
| Meilisearch | `MEILI_MASTER_KEY` (16+ bytes) in `PROD_ENV_VARIABLES` and `sync_env` first, then `services: meilisearch` | `APP_SEARCH_URL=http://meilisearch:7700`, `APP_SEARCH_KEY=<the master key>`; the backend builds the indexes itself | unset (Postgres search again), recreate, then `$DC stop meilisearch` |

**Recreate means a fresh build.** A `restart` keeps the environment the
container was made with, so a new setting needs a recreate, and every
recreate builds: from the workflow, `sync_env` + `recreate_backend` (it
builds, from the cache when nothing changed, recreates the backend and takes
a running worker along); by hand:

```bash
# Changes things: rebuilds the image if anything changed, recreates both.
sudo docker-compose -f docker-compose.prod.yml up -d --build --no-deps backend worker < /dev/null
```

Leave `worker` out while the worker is not running. Never a plain `up -d`
without `--build`: on 2026-10-17 one recreated the backend from an image that
lacked packages installed by hand, and production was down for 20 minutes.
Never `up -d` with no service named: it would start every service in the
file, whether the box can hold them or not. `--no-deps` keeps Postgres out
of it (its config, the shared `.env`, changes with every setting), and
`< /dev/null` keeps the command from reading the rest of a script it is run
from. (`recreate_backend` does the same from the workflow: it builds first,
from the cache when nothing changed.)

By hand on the server, the same as the `services` input (changes things):

```bash
$DC config --services                         # read-only: what the file defines
$DC up -d --build --no-deps redis < /dev/null # start or refresh one service (--build: a no-op for an image, a cached build for the worker)
$DC ps
$DC logs --tail=100 worker
```

The worker runs the backend's image (`moa-backend:live`) and code: every
deploy that rebuilds or recreates the backend recreates a running worker
too, and one that only syncs code restarts it (section 1). Its own sizing
(two jobs at once, its own small database pool) is in section 10.

---

## 7. Errors and logs

**Sentry** (off until set):

1. In Sentry, create a project (platform: FastAPI) and copy its DSN. Under
   *Settings -> Security & Privacy*, keep *Data Scrubber* and *Use Default
   Scrubbers* on, turn on *Prevent Storing of IP Addresses*, and add
   `email, phone, otp, token, password, secret` to *Additional Sensitive
   Fields*.
2. Make sure the running image has `sentry-sdk` (any `install_requirements`
   deploy after this change). A DSN on an older image is logged and ignored.
3. Add `APP_SENTRY_DSN=<dsn>` (and optionally `APP_SENTRY_ENVIRONMENT`,
   default `APP_ENV`, else `production`; `APP_SENTRY_TRACES_SAMPLE_RATE=0.05` to trace 5% of
   requests; `APP_SENTRY_SAMPLE_RATE`, default `1.0` of errors) and deploy
   with `sync_env`, `recreate_backend`.
4. Check: the backend log says `Sentry is on`; events carry the release
   `myotherapp-backend@<sha>` (from `backend/REVISION`, written by every
   `sync_code`) and a `request_id` tag.

What is sent: unhandled errors (the 500s) and anything logged at ERROR, by
route template (`/api/v1/events/{event_id}`), never with request bodies,
cookies, local variables, IP addresses, Authorization or token headers;
emails, phone numbers, JWTs, bearer tokens, key-shaped strings and the
server's own secrets are replaced in every message (`app/core/observability.py`).
A 4xx -- the API refusing a request, a validation error, a rate limit, an
unknown route -- is never sent. A job that dies after its last try is sent
with its exception, tagged `component: worker` from the job worker (`api`
when the API runs the jobs itself). `tests/test_error_tracking.py` proves
each of these through the real application.

**JSON logs**: `APP_LOG_JSON=true` makes every line one JSON object (`ts`,
`level`, `logger`, `msg`, `request_id`; `method`, `path`, `status` on the
access line), for a log shipper. Off by default.

**Request ids** (with either one on): every response carries `X-Request-ID`
(the caller's, when it sends a sane one); the same id is on every log line of
that request, on its Sentry event, and is the `track_id` a 500 shows the
user. To follow one: `$DC logs backend | grep <id>`.

---

## 8. Builds

`backend/Dockerfile` installs Python packages in two layers:
`requirements-base.txt` (the heavy, stable 43 pins: numpy, pandas, boto3,
firebase-admin, reportlab...) and then `requirements.txt` (the other 29, and
where a new pin goes). A change to `requirements.txt` rebuilds only the second
layer; pip's downloads are kept in a BuildKit cache mount between builds, so
nothing already downloaded is fetched again. The cache is outside the image.

Measured on a laptop (Docker Desktop, arm64) for a one-line change to
`requirements.txt` (one package added), old Dockerfile against new:

| | Old (one pip layer, `--no-cache-dir`) | New (two layers, cache mount) |
|---|---|---|
| Whole build | 94 s | 13 s |
| pip step | 60 s: 202 downloads, 100 packages installed | 6 s: 2 downloads, 36 packages installed (the base layer reused) |
| Layer export | 31 s | 2 s |
| Image size | 2.95 GB | 2.95 GB |

The server took 25 minutes for the old kind of build on 2026-10-18; the
new one skips the same parts there, as long as the build cache holds the
base layer: the deploy's prune (section 11) keeps what the last day's builds
used, so it does; a `docker builder prune -a` or `docker system prune -a` by
hand drops it, and the next build installs the base layer again.

**BuildKit is required.** Read-only check on the server:

```bash
sudo docker buildx version
sudo docker-compose version
```

If `buildx` is missing: `sudo apt-get install docker-buildx-plugin` (Docker's
apt repository) or `sudo apt-get install docker-buildx` (Ubuntu's). The
deploy sets `DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1`, which
docker-compose v1 needs and v2 ignores.

**Building at low priority.** `nice` or `ionice` on `docker-compose` do not
help: the build runs inside dockerd, not under the client. What does cap it
is a separate BuildKit builder running in a container with CPU and memory
limits (CPU only: there is no disk-IO weight option). Once, on the server
(changes: adds a builder container):

```bash
sudo docker buildx create --name lowprio --driver docker-container \
  --driver-opt cpu-quota=50000 --driver-opt cpu-period=100000 \
  --driver-opt memory=1500m --driver-opt default-load=true
sudo docker buildx inspect lowprio --bootstrap      # read-only after the first run
```

Then build with it and recreate (instead of `install_requirements`):

```bash
IMG=$(sudo docker inspect --format '{{.Config.Image}}' doth-live-backend)
sudo docker buildx build --builder lowprio --load -t "$IMG" backend/
$DC exec -T backend alembic upgrade head
$DC up -d --no-deps --force-recreate backend worker < /dev/null      # worker only if it runs
```

(No `--build` on that last line: the image was just built, by the
low-priority builder; `--build` would build it again on the default one.)

`cpu-quota=50000` of `cpu-period=100000` holds the build to half a CPU, so
the API keeps the rest; the build takes longer. Proved on Docker Desktop: the
builder's container got `CpuQuota 50000, CpuPeriod 100000, Memory 1.5 GB`,
and the same CPU-bound `RUN` step took 1.1-1.25 s on the default builder and
2.5-3.7 s on this one, with the image loaded into the engine afterwards. Not
yet tried on the server; it needs `docker buildx` (above).

**Not building on the server at all** is better still:

- **Locally**, on any machine with Docker:

  ```bash
  SHA=$(git rev-parse --short HEAD)
  docker buildx build --platform linux/amd64 -t moa-backend:$SHA --load backend/
  docker save moa-backend:$SHA | gzip | ssh <user>@<server> 'gunzip | sudo docker load'
  ssh <user>@<server> "sudo docker tag moa-backend:$SHA \$(sudo docker inspect --format '{{.Config.Image}}' doth-live-backend)"
  ```

  then on the server migrate and recreate as above (`exec ... alembic upgrade
  head`, `up -d --no-deps --force-recreate backend worker`, without `--build`:
  the image is the one just loaded). The transfer is the
  whole image (about 3 GB uncompressed, much less gzipped); a registry sends
  only the changed layers.
- **In CI**, with a registry (GHCR or ECR): a workflow builds with
  `docker/build-push-action` (`platforms: linux/amd64`, `cache-from/to:
  type=gha`) and pushes `ghcr.io/<org>/moa-backend:<sha>`; the compose service
  gets `image: ghcr.io/<org>/moa-backend:${BACKEND_TAG}` beside `build:`, and
  the deploy runs `$DC pull backend` instead of `build`. The server then only
  downloads changed layers. (Needs Actions billing working, and a registry
  login on the server.)

---

## 9. Server sizing: CPU credits or disk?

On 2026-10-18 a `pip install` in an image build took 25 minutes and the live
API timed out meanwhile: the box ran out of something. Which one decides the
fix. All of these are **read-only**; run them while it is slow, and once when
it is quiet to compare.

```bash
uptime                                   # load averages: more than the CPU count = a queue
nproc
vmstat 1 5                               # see below
iostat -x 1 5                            # package sysstat; per-disk utilisation and latency
cat /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory
free -m
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type; echo
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
```

How to read them:

| Signal | CPU credits exhausted (burstable t2/t3/t3a/t4g) | Disk IO exhausted (gp2 burst balance, small volume) | Memory |
|---|---|---|---|
| `vmstat` | `st` (steal) high, often 30-90, while `us` is low; `r` above the CPU count | `wa` high, `b` (blocked) above 0, `st` near 0 | `si`/`so` (swap) above 0 |
| `iostat -x` | disk quiet | `%util` near 100, `r_await`/`w_await` tens to hundreds of ms, `aqu-sz` above 1 | -- |
| `/proc/pressure` | `cpu some avg10` high | `io some/full avg10` high | `memory some avg10` high |
| CloudWatch | `CPUCreditBalance` at or near 0, `CPUUtilization` pinned at the baseline (20% of a t3.small, for instance) | EBS `BurstBalance` near 0 (gp2), `VolumeQueueLength` high | -- |

What to move to:

- **CPU credits**: right away, switch the instance to *unlimited* credit
  mode (EC2 -> instance -> Actions -> Instance settings -> Change credit
  specification): it bills surplus CPU instead of throttling. Then move to a
  fixed-performance instance: `m7i.large` (2 vCPU, 8 GiB, x86) or
  `m7g.large` (Graviton; the image would need building for arm64), and
  `c7g.xlarge`/`m7g.xlarge` when traffic grows (docs/SCALING.md).
- **Disk**: convert the volume to **gp3** (online, no downtime: EC2 -> Volumes
  -> Modify -> gp3). gp3 gives 3,000 IOPS and 125 MB/s whatever its size, with
  no burst bucket to run dry, and costs less than gp2; a 30 GB gp2 volume's
  baseline is only 100 IOPS once its burst credits are gone.
- **Both, and for good**: stop building on this box (section 8), and move
  Postgres off it (RDS, docs/SCALING.md Phase 0), so a build or a dump never
  competes with the database for the same CPU and disk.

What the box was on 2026-10-18 (read-only output, run by the user): a
t2.medium, 2 burstable vCPUs with CPU pressure around 60% (throttled), 3.8 GB
of memory with about 195 MB available, no swap, 5 GB of disk free (83%
used), and two other projects on it. Section 10 sizes every service against
that and against the 8 GB it is being resized to.

---

## 10. One box: memory and CPU for every service

Redis, Meilisearch and the worker run on the same box as the backend and
Postgres (the user's decision), so every service has a memory limit and a
CPU limit in `docker-compose.prod.yml` (`mem_limit`, `cpus`). A leak or a
spike is then killed and restarted inside its own container, instead of the
kernel's OOM killer choosing a victim on the whole box -- possibly
Postgres. The background services get less than the box's two vCPUs, so a
render, an index build or a big send never takes the API's CPU; the API and
Postgres may use the whole box.

The server runs docker-compose v2 (2.33.1), which reads `mem_limit` and
`cpus` whatever the file's `version` key says (v2 ignores it, and warns that
it is obsolete); `deploy.resources` would need `--compatibility` under v1,
and v2 refuses both at once. `restart: unless-stopped` everywhere: with
`on-failure`, which postgres and backend had, a container stopped cleanly
(Docker or the host restarting, as a resize does) exits 0 and stays down.

**The numbers live in `docker-compose.prod.yml`** (and the worker's database
pool in `backend/app/config.py`); this is why each is what it is. Change
them there and here together (`tests/test_one_box.py` checks the file).

What the box ran on 2026-10-18 (`docker stats`, read-only):

| Container | Memory |
|---|---|
| doth-live-backend | 1.43 GB |
| doth-live-postgres | 155 MB |
| letmego-backend-prod | 514 MB |
| letmego-postgres | 42 MB |
| bcards-backend | 243 MB |
| bcards-mongo | 120 MB |
| bcards-nginx | -- |

The limits (1 GB = 1,024 MB here):

| Service | `mem_limit` | `cpus` | Uses | Why this much |
|---|---|---|---|---|
| postgres | 1280m | 2 | 155 MB | The image's defaults, which nothing here changes: `shared_buffers` 128 MB, `work_mem` 4 MB, `maintenance_work_mem` 64 MB, `max_connections` 100. They fit with room: 128 MB shared, a few MB a connection (under 500 MB with all 100 open), a sort or hash of 4-8 MB in the few running at once, autovacuum's 64 MB times 3 workers. The rest is page cache, which the kernel charges to the container and reclaims under the limit (it does not kill for it). Raise `shared_buffers` and this together |
| backend | 2304m | 2 | 1.43 GB | `APP_WORKERS` uvicorn workers at 0.35-0.4 GB each once the app is imported, plus the render pool's peaks (wkhtmltopdf, ticket, certificate and wallet renders run in this container) |
| worker | 768m | 1 | ~0.4 GB | One process of the app, and two jobs' renders, PDFs or sheet imports. `APP_JOBS_CONCURRENCY=2` (set on the service, so the API's value never reaches it) |
| redis | 256m | 0.5 | <= 192 MB | `maxmemory 192mb`, `allkeys-lru` (when full it drops the least recently used keys, never refuses a write), `appendonly no` and no snapshots (it holds no data of record, so it never forks to write a file and never needs twice its memory); the rest is Redis's own overhead |
| meilisearch | 768m | 1 | indexing <= 256 MiB | `MEILI_MAX_INDEXING_MEMORY=256MiB` (unset, Meilisearch takes up to two thirds of the memory it sees, which in a container can be the host's) and one indexing thread; the indexes are memory-mapped files whose pages the kernel drops under the limit. Lakhs of events and clubs fit |
| **All five** | **5376m (5.25 GB)** | | | |

Every container's log is also capped (`json-file`, 5 x 20 MB), since Docker
never trims one by itself (section 11).

Against the box (GB; "usable" is what `free -m` calls total):

| | 2 GB box | 4 GB box (today's t2.medium) | 8 GB box (the resize) |
|---|---|---|---|
| Usable | ~1.9 | 3.8 | ~7.6 |
| The two other projects (measured 0.95) | 1.0 | 1.0 | 1.0 |
| OS, Docker, the host's nginx | 0.5 | 0.5 | 0.5 |
| Builds (BuildKit runs outside every limit) and the host's page cache | -- | -- | 0.85 |
| **Left for this project** | ~0.4 | ~2.3 | **~5.25** |
| backend + postgres (limits 3.5; using 1.6 today) | no: the backend alone uses 1.43 | runs today, with ~195 MB free; the limits are caps above what is used | yes |
| + worker, redis, meilisearch (limits 1.75) | no | **no: do not start redis, meilisearch or the worker here** | yes: 5.25 of 5.25 |

On the 8 GB box the limits add up to what is left for this project, so no
combination of them can push the box into the OOM killer; they are caps,
and what the services actually use (about 2.5 GB together, idle) leaves the
rest to the page cache.

**To check once the box is resized** (these assume a 2-vCPU, 8 GB instance):

- `free -m`: if "total" is well under 7,600 MB, take the difference off the
  backend first.
- `APP_WORKERS` in the backend's `.env` and the backend's real use
  (`sudo docker stats --no-stream`): 0.35-0.4 GB a worker plus about 0.6 GB
  of render peaks should stay under its 2304m.
- The database's size (read-only, in the psql session above:
  `SELECT pg_size_pretty(pg_database_size(current_database()));`): past
  about 1 GB, Postgres wants more page cache than its 1280m leaves; raise it
  (and `shared_buffers` to about a quarter of it) before raising anything
  else.
- More vCPUs: raise `cpus` on backend and postgres to the new count (a
  `cpus` above the box's count is refused when the container is created).

**The connection budget.** Postgres takes `max_connections` 100, 3 kept for
superusers: 97. Each API worker may open `APP_DB_POOL_SIZE` +
`APP_DB_MAX_OVERFLOW` (10 + 20); the job worker its own
`APP_WORKER_DB_POOL_SIZE` + `APP_WORKER_DB_MAX_OVERFLOW` (4 + 8: two jobs, and
a mail send holds a connection only for each short transaction, so a burst
waits briefly for one rather than failing). At startup the API (once
`APP_JOBS_WORKER` or `APP_SWEEPS_IN_WORKER` says there is a worker) and the
worker itself log `connection budget: ...` when `APP_WORKERS` x 30 + 12 is
more than 97:

| `APP_WORKERS` | API | + worker | Fits 97? |
|---|---|---|---|
| 1 | 30 | 42 | yes |
| 2 | 60 | 72 | yes |
| 3 | 90 | 102 | no: `APP_DB_MAX_OVERFLOW=10` (3 x 20 + 12 = 72), or PgBouncer |
| 4 | 120 | 132 | no, even without the worker: `APP_DB_POOL_SIZE=5 APP_DB_MAX_OVERFLOW=10` (4 x 15 + 12 = 72) |

A migration (one connection), a backup's `pg_dump` and a psql session come
out of what is left. Memory: each connection is a process of a few MB,
inside Postgres's 1280m.

**Renders.** Tickets, certificates, ID cards, previews and wallet passes are
drawn on `APP_RENDER_THREADS` threads per process (default **2**, was 3), so
the box draws at most `APP_WORKERS` x that at once (6 with three workers),
plus the worker's own when it runs. reportlab draws under the GIL: more
threads in one process mostly take turns, and every turn is time the
process's event loop -- its requests -- does not get. Measured with
`python -m scripts.bench.render_threads --renders 240 --threads 1 2 3`
(an Apple M2, idle cores, one process; medians of three rounds; the loop
asks to wake every 5 ms):

| Threads | Renders a second | Loop late by, p50 / p95 / p99 |
|---|---|---|
| 1 | 28.5 | 2.6 / 13.2 / 14.0 ms |
| 2 | 37.3 | 7.2 / 19.0 / 19.5 ms |
| 3 | 40.9 | 8.5 / 26.3 / 40.1 ms |

The third thread bought a tenth more throughput from a spare core the
2-vCPU box does not have (three API processes and Postgres share two), and
doubled the loop's p99. HTML designs and receipts are a wkhtmltopdf process
per render besides, outside the GIL but not outside the backend's 2304m
(1.43 GB in use with nothing rendering). Nothing caps renders across
processes: that needs a shared counter (Redis or Postgres) on every render,
and the per-process cap times `APP_WORKERS` already bounds the box.

**The job queue's slots.** Each runner (one per API process, unless
`APP_JOBS_WORKER`) holds `APP_JOBS_CONCURRENCY` jobs (4; the worker 2). A
job's priority picks which due job takes a free slot first, whatever its
name; a name's cap keeps the heavy ones to a few slots. The handler sets
both (`app/core/jobs.py`; `tests/test_jobs.py` lists them all);
`APP_JOBS_LIMITS="name=n,..."` overrides a cap without a deploy.

| Priority | Jobs | Cap per runner |
|---|---|---|
| HIGH (1): someone is waiting | `payments.receipt` | 2 (a wkhtmltopdf each) |
| | `deliveries.send` for one guest's ticket or certificate (registration, payment, sent again) | 2, shared with the event-wide sends below (one name) |
| | `clubs.approved_email`, `mail.billing_notice`, `mail.club_domain_notice`, `admin_alerts.deliver`, `checkins.after` | -- |
| NORMAL (0): a send to many | `deliveries.send` for a whole event, `emails.send_message` | 2 (renders) |
| | `notifications.new_event`, `notifications.interest`, `notifications.push`, `threads.tell_followers` | 2 (minutes-long fan-outs) |
| | `notifications.broadcast`, `push.topic`, `push.devices`, `push.unsubscribe`, `live.survey_mail` | -- (short) |
| LOW (-1): nobody watches | `closeout.retention`, `registrations.import` | 1 |

Priorities order the claims; they do not take a slot back. A runner whose
four slots all hold long jobs (two event-wide sends and two announcements,
say) makes the next ticket wait for one of them, or for another runner. With
three API runners that takes a dozen long jobs at once; if it ever happens,
lower those caps with `APP_JOBS_LIMITS`.

**Applying the limits.** Compose applies a service's limits, log options and
restart policy when it next creates the container: the backend's on its next
recreate, the worker's, Redis's and Meilisearch's when first started.
Postgres is recreated only on purpose, so for it (and for the backend before
its next recreate) apply them in place, which restarts nothing:

```bash
# Read-only: what each running container has now.
sudo docker inspect --format '{{.Name}} memory={{.HostConfig.Memory}} cpus={{.HostConfig.NanoCpus}} restart={{.HostConfig.RestartPolicy.Name}}' $(sudo docker ps -q)

# Changes the containers' settings in place (no restart).
sudo docker update --restart unless-stopped doth-live-postgres doth-live-backend
sudo docker update --memory 1280m --memory-swap 2560m --cpus 2 doth-live-postgres
sudo docker update --memory 2304m --memory-swap 4608m --cpus 2 doth-live-backend
```

(`--memory-swap` at twice the memory is what Docker gives a container whose
memory limit is set without one, as compose's `mem_limit` is; with no swap on
the box it changes nothing.) The log options
cannot be changed in place; they come with the next recreate.

**Resizing** (the same instance, to 8 GB):

1. Before stopping it: run the `docker update --restart unless-stopped`
   line above (the containers come back when the host does; with
   `on-failure` they would not). The other projects' containers are theirs
   to decide. Take a backup (section 4). Check the instance has an Elastic
   IP: a stop and start gives it a new public address otherwise, and
   `api.myotherapp.in` points at the old one.
2. EC2 -> the instance -> stop; *Actions -> Instance settings -> Change
   instance type* (t3.large: 2 vCPU, 8 GiB, or a fixed-performance m7i.large,
   section 9); turn on unlimited CPU credits for a burstable type; start.
   Grow the volume while you are there (section 11).
3. After: `free -m`, `sudo docker ps` (postgres and backend up), the smoke
   URL answering 200. Then the services, one at a time (section 6).

---

## 11. Disk

The root volume had 5 GB free on 2026-10-18 (83% used), and each backend
image is about 3 GB. What fills it, read-only:

```bash
df -h /
sudo docker system df                       # images, containers, volumes, build cache
sudo du -sh /var/lib/docker/containers/*/*-json.log 2>/dev/null | sort -h | tail
ls -la /var/backups/myotherapp | tail       # the database dumps (section 4)
```

- **Images.** A build moves the `moa-backend:live` tag to the new image and
  leaves the old one untagged ("dangling"); the layers it shared with the new
  one are kept either way, the rest (a changed requirements layer is hundreds
  of MB) is garbage once no container runs it.
- **Build cache.** BuildKit keeps every step's result, and pip's download
  cache (section 8), until pruned.
- **Container logs.** Each container's stdout is a JSON file Docker never
  trims; the compose file now caps each at 5 x 20 MB (applied when the
  container is next created).

**The deploy prunes after every build** (`build_backend`,
`install_requirements` or `restart_all`), last, once the new image runs:

```bash
# Changes things: deletes unused images and old build cache.
sudo docker image prune -f                              # untagged images no container uses
sudo docker builder prune -f --filter until=24h         # build cache unused for a day
```

Neither removes an image a container runs, nor one with a tag (tag an image
you may roll back to: section 2), and the cache the build just used is less
than a day old, so the base layer stays cached. Both act on the whole box,
so they also clear the other projects' dangling images and old build cache,
which nothing uses. A failed prune only warns. Run the same two by hand when
`df` runs low between deploys. Not `docker system prune -a` or
`docker builder prune -a`: they drop the base layer's cache, and the next
build installs numpy, pandas and the rest again (section 8).

**Room to grow.** Growing the EBS volume is online: EC2 -> Volumes ->
Modify (size, and gp3, section 9), then on the server (changes the
partition and the filesystem; check `lsblk` and `df -T /` first):

```bash
sudo growpart /dev/nvme0n1 1                # or /dev/xvda 1, as lsblk shows
sudo resize2fs /dev/nvme0n1p1               # ext4; xfs_growfs / for xfs
```

---

## 12. The live room's streams (`APP_LIVE_SSE`)

With `APP_LIVE_SSE=true` the live room is pushed over server-sent events:
`GET /api/v1/events/{id}/live/stream`, one long response per phone or console
(the client contract is docs/LIVE_SSE.md). Unset (the default), the
stream answers 404 and every client keeps polling. A stream is a socket held
for as long as someone watches, so three things on the box must allow for
thousands of them before it is turned on.

**1. nginx: a location for the stream.** `nginx.conf` in the repository has
it in each API server block, before `location /`; the host's nginx needs the
same in the server block(s) for `api.myotherapp.in` (and any other name
pointing at the API), with the upstream port that block's `location /` uses.
The stream's URL carries the browser's key and the player's key in its query
string (EventSource cannot send headers), and a player key is enough to
answer as that player, so its access line is written without the query:
`log_format moa_stream` once, in the http context (the top of the sites file
does it: a sites file is included inside `http {}`), and `access_log ...
moa_stream` in the location:

```nginx
# Once, outside any server block.
log_format moa_stream '$remote_addr - $remote_user [$time_local] "$request_method $uri $server_protocol" '
                      '$status $body_bytes_sent "$http_referer" "$http_user_agent" $request_time';

location ~ ^/api/v1/events/\d+/live/stream$ {
    proxy_pass http://localhost:8000;          # the port this server block's location / uses
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;                       # frames as they are written
    proxy_cache off;
    gzip off;
    proxy_read_timeout 120s;                   # the stream pings every 15 s
    send_timeout 60s;                          # a phone that stops reading is let go
    access_log /var/log/nginx/access.log moa_stream;   # $uri: the path, never the keys in the query
}
```

Without the location a stream still works (the app sends
`X-Accel-Buffering: no`, so nginx passes frames through unbuffered), but the
default 60 s `proxy_read_timeout` cuts it, a stalled phone is held longer,
and the access log keeps the query.

nginx's error log still quotes a request line whole when it logs an error
about one (an upstream that failed or timed out), query included: rare for a
stream that pings every 15 s, and the error log is root's to read. The
backend's own logs never carry those values: its access line (plain or
`APP_LOG_JSON`) and Sentry's events write `key`, `player`, `last_event_id`
and any `...token...` query value as `[Filtered]` (app/core/redact.py,
app/core/observability.py).

**2. nginx: connections and open files.** Each open stream holds two
connections in nginx (the phone's and the one to the backend), and each is
an open file. Ubuntu's default is `worker_connections 768` per worker
process (`worker_processes auto`: one per vCPU, so 1,536 on two vCPUs): some
700 streams in all, then nginx refuses connections for everyone, page loads
included. In `/etc/nginx/nginx.conf`, main context and `events`:

```nginx
worker_rlimit_nofile 65536;          # main context: open files per worker process

events {
    worker_connections 16384;        # per worker: about 8,000 streams each, with normal traffic beside
}
```

Each idle connection costs nginx a few hundred bytes (with `proxy_buffering
off`, a small buffer while a frame passes), so this is memory well inside the
OS share of section 10. Read-only checks, then apply:

```bash
sudo nginx -T 2>/dev/null | grep -E 'worker_(processes|connections|rlimit_nofile)'
grep 'open files' /proc/$(pgrep -f 'nginx: worker' | head -1)/limits
sudo nginx -t                         # read-only: the new config parses
sudo systemctl reload nginx           # changes: workers restart gracefully, no connection dropped
```

**3. The backend: open files and memory.** `docker-compose.prod.yml` raises
the backend's open-file limit to 65,536 (Docker's default soft limit is
1,024: about a thousand phones a container). It applies when the backend is
next recreated (`recreate_backend`, or by hand `up -d --build --no-deps
backend`); a restart keeps the old limit. Read-only check:
`sudo docker exec doth-live-backend sh -c 'ulimit -n'`. A stream costs the
backend about 40 KB (scripts/bench/RESULTS.md, "The live room pushed"): a
hall of 3,000 phones some 120 MB, inside its 2304m (section 10). Each API
process holds at most `APP_LIVE_SSE_MAX_STREAMS` (5,000), 4,000 per event
and 3,000 per address; past them a phone is told 503 `LIVE_STREAM_FULL`
with `Retry-After` (exposed by CORS, so the site's page can read it) and
polls.

**Turning it on**, in order:

1. Deploy with `APP_LIVE_SSE` unset (the streams answer 404).
2. nginx: the location and the limits above; `nginx -t`, reload.
3. With more than one API process (`APP_WORKERS` above 1), Redis first
   (section 6): a change made in one process reaches the streams held by
   the others only through it.
4. The clients ship "stream first, poll on 404" (event site, app, clubs
   dashboard).
5. `APP_LIVE_SSE=true` in `PROD_ENV_VARIABLES`, then `sync_env` +
   `recreate_backend` (a recreate, which builds; section 6). Unset it the
   same way to put every client back on the poll.

---

## 13. Rendered documents: artwork, one render each, and expiry

Tickets and certificates are drawn when first asked for (a download, an
emailed link, an email's attachment, a bundle) and kept in the bucket, so
the next ask is a read (app/api/events/doc_cache.py):

```
{APP_S3_BASE_PATH}/docs/{ticket|certificate}/{registration uuid}/{design version}-{24 hex}.pdf
```

Production's base path is, by every sign, empty, so that is `docs/...`;
`scripts/doc_cache_report.py` prints the exact prefix (`prefix`). The 24 hex are a
hash of everything the document prints, its design (image keys included)
and the renderer's version: change any of them and the document gets a new
key, so a kept file is never stale and nothing has to be invalidated. ID
cards and wallet passes are drawn per request and not kept.

### Artwork is stored ready for print

What a document costs follows its artwork: the renderer copies a JPEG into
the PDF as it is and re-compresses anything else, for every guest. Uploads
used to be stored as sent. Now every picture uploaded for a printed design
(the designer's templates and pictures, club's and event's, every kind; the
ticket and certificate templates' logos, backgrounds and signatures) goes
through app/core/storage/doc_art.py: the camera's orientation applied, the
longest side at most `APP_DOC_ART_MAX_PX` (2400, about 200 dpi across an A4
certificate), converted to sRGB, metadata stripped, a photo as a progressive
JPEG at quality 85, transparency as a lossless PNG. Keys and answers keep
their shape. `python -m scripts.bench.doc_art` measures it (Apple M2, warm
p50 of 15 renders, about the same for a ticket, a certificate and an ID card;
taken while other test suites ran, so the times are high and the ratios are
what counts; scripts/bench/RESULTS.md has the whole table):

| Artwork as uploaded | Stored now | Render before -> after | PDF before -> after |
|---|---|---|---|
| A4 poster, PNG 2480 x 3508, 10.8 MB | JPEG 1697 x 2400, 244 KB | 3.5 s -> 0.1 s | 18.9 MB -> 0.36 MB |
| Phone photo, JPEG 4032 x 3024, sideways, 779 KB | JPEG 1800 x 2400, upright, 235 KB | 240 ms -> 90 ms | 1.03 MB -> 0.35 MB |
| Photo JPEG 2000 x 1100 at quality 98, 455 KB | JPEG, 147 KB | 113 ms -> 56 ms | 626 KB -> 241 KB |
| Smooth JPEG 2000 x 1100, 50 KB | JPEG, 34 KB | 35 ms -> 33 ms | 120 KB -> 99 KB |
| Noise JPEG 2000 x 1100, 1.35 MB (nothing compresses) | JPEG, 1.34 MB | about the same | about the same |
| Logo PNG with transparency, 2000 x 800, 54 KB | lossless PNG, 54 KB | the same | the same |

**Artwork stored before** stays as it was until `scripts/optimize_doc_art.py`
is run. It finds every picture a design names (template columns, their
configs, legacy links, layered designs' template and pictures; deleted
designs too), and for each object of at least `--min-kb` (300) writes the
normalised file under a **new** key in the same folder and points every
design at it. It never deletes or overwrites the old object; the report
lists them (`old_keys`). Documents already kept follow on their own: a
repointed design hashes to new document keys, so the next download draws on
the new artwork, and the old documents are no longer asked for (the expiry
below removes them).

```bash
# Read-only: what it would do (reads each object, writes nothing).
$DC exec -T backend python scripts/optimize_doc_art.py > ~/doc-art-dry-run-$(date +%F).json
# Changes things: writes the new files, repoints the designs. Keep the report: it names the old keys.
$DC exec -T backend python scripts/optimize_doc_art.py --apply > ~/doc-art-apply-$(date +%F).json
```

Read `failed` (an object that could not be read or decoded is left as it
was), `optimised`, `bytes_before` and `bytes_after`. Safe to run again: a
repointed design names a small file. The old objects can go once nothing
will be rolled back (a month, say): one `aws s3 rm s3://<bucket>/<key>` per
key in `old_keys`.

### One render per document

Two first asks for the same document at the same moment drew it twice. Now
one process draws it and the others wait for it and read what it kept: in a
process, callers share one drawing; across processes, the drawing claims the
document first with a Postgres advisory lock (`lock_keys.DOC_RENDER`, a hash
of the key; session-level, on its own autocommit connection, no transaction
open), or with a Redis key when `APP_REDIS_URL` is set. A waiter asks again
every 50 ms to 1 s and, after `APP_DOC_RENDER_WAIT_SECONDS` (20), draws the
document itself rather than fail. A claim that cannot be had (the database
unreachable) never fails a download.

A Postgres claim holds one pooled connection while its holder looks in the
bucket, draws and keeps the document: tens of milliseconds for a layered
design, up to `RENDER_TIMEOUT` for an HTML one. At most twice
`APP_RENDER_THREADS` documents per process are drawn under a claim at once
(4 by default), so a burst of first downloads holds at most that many of the
pool's connections; waiters hold none. With Redis on, the claim holds no
database connection at all. Behind a transaction-mode PgBouncer the claim,
like the other session locks, needs Redis or a direct connection.

### Expiry: the sweep, or a lifecycle rule (one of them)

Nothing else deletes a kept document, so `docs/` grows by every document
drawn. Two ways to expire them; use **one**. With both on at different day
counts, the shorter decides and the other only adds confusion (a rule at 30
days under a sweep at 90 deletes what the sweep's report says it keeps).
Switching: turn one off before the other on.

**The sweep** (app/api/events/doc_cache_sweep.py), off by default. Once a day
on one process it pages through `docs/` (ListObjectsV2) and deletes, 1,000 a
call with a pause between (DeleteObjects), each kept document last written
more than `APP_DOC_CACHE_DAYS` ago.

| Setting | Default | |
|---|---|---|
| `APP_DOC_CACHE_DAYS` | `0` (off) | days since a kept document was last written; production: e.g. `90` |
| `APP_DOC_CACHE_MAX_DELETE_PER_RUN` | `50000` | the rest wait for the next day |
| `APP_DOC_CACHE_SKIP_ALERT_PERCENT` | `5` | an admin alert (`doc_cache_unknown`) above this share of `docs/` that is not kept documents |

It touches only keys of the exact shape above (kinds and extensions the code
writes); anything else under `docs/` is counted, named in the log and left,
and nothing outside `docs/` is ever listed. "Last written", as S3 keeps no
read time: a document downloaded every day is deleted at the same age as any
other and drawn again, once, when next asked for. Each run logs one line,
`doc cache sweep: N deleted (B bytes) of M kept documents over D days old;
...`, and keeps its counts in `scheduled_runs` (`name = 'doc-cache-sweep'`,
`result`).

The media bucket's key needs, besides what it has, `s3:ListBucket` on the
bucket for the `docs/` prefix and `s3:DeleteObject` on `docs/*` (the app
never listed or deleted there before):

```json
{"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>",
 "Condition": {"StringLike": {"s3:prefix": ["docs/*"]}}},
{"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::<bucket>/docs/*"}
```

Without them each run logs `doc cache sweep failed` with AccessDenied and
deletes nothing.

**A lifecycle rule** does the same inside the bucket, with no code, no
listing and no permissions: S3 (or R2, *Object lifecycle rules* in its
dashboard, or the same API) expires objects by age under a prefix. It checks
no key's shape, has no cap and raises no alert: anything put under `docs/`
goes at that age.

```json
{"Rules": [{"ID": "docs-90-days", "Status": "Enabled", "Filter": {"Prefix": "docs/"},
            "Expiration": {"Days": 90}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}}]}
```

The rule's prefix is the report's `prefix`.
`put-bucket-lifecycle-configuration` **replaces** the bucket's whole
configuration: read it first and add this rule to what is there.

```bash
# Read-only.
aws s3api get-bucket-lifecycle-configuration --bucket <bucket>
# Changes things: the bucket's lifecycle rules, all of them.
aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --lifecycle-configuration file://lifecycle.json
```

### Turning it on in production

1. Deploy the code (`sync_code`, `restart_backend`; no migration, no new
   requirement). New uploads are stored ready for print from then on.
2. `optimize_doc_art.py` dry run, then `--apply` (above). Keep the apply
   report.
3. Read-only, what is there and what an expiry would take:

   ```bash
   $DC exec -T backend python scripts/doc_cache_report.py --days 90
   ```

   Counts and bytes per kind (and "not a kept document") under 30, 30 to 90
   and over 90 days; `would_delete` at `--days`, with the daily runs it
   needs at the cap; and the share of `docs/` that is not kept documents.
   If that share is above 5%, find what writes there before going on. An
   AccessDenied here means the key cannot list `docs/` yet (the policy
   above).
4. Either `APP_DOC_CACHE_DAYS=90` in `PROD_ENV_VARIABLES`, then `sync_env`
   and `recreate_backend` (section 6: a recreate builds), or the lifecycle
   rule. Not both.
5. The next day: the `doc cache sweep:` line in the backend's log, and no
   `doc_cache_unknown` alert. To stop: unset it the same way.

---

## 14. AI assist

What the clubs dashboard calls (`app/api/ai`, on the layer in `app/core/ai`):
a draft event from notes, a poster or a PDF (`POST /clubs/ai/event-draft`);
writing -- an event description, an announcement, an email, a sponsor
thank-you, a caption, in English, Malayalam or both (`POST /clubs/ai/write`);
an event's survey comments summed up (`GET /events/{id}/ai/feedback-summary`,
and `.../refresh`); the status (`GET /clubs/ai/status`); and a club's own AI
key (`GET/PUT/DELETE /clubs/ai/key`). Nothing is saved from a draft or a
piece of writing; the organiser reviews it in the form.

### Settings

| Setting (`.env`) | Default | Meaning |
|---|---|---|
| `APP_AI_ENABLED` | `false` | the switch. Off: every AI action answers 403 `AI_DISABLED` and no provider is called, a club's own key included. The status still answers (`enabled: false`) |
| `APP_GEMINI_API_KEY` | empty | the platform's one Gemini key. Empty with the switch on: 503 `AI_UNAVAILABLE` for clubs without their own key |
| `APP_AI_MODELS` | `gemini-3.8-flash,gemini-3.5-flash-lite` | tried in this order: on a 429, a 5xx, a timeout, an unknown model or a refusal, the next, after `APP_AI_FALLBACK_BACKOFF_SECONDS` (0.5). All failing: 503 `AI_UNAVAILABLE` |
| `APP_AI_VISION_MODELS` | the same two | the list for a request with a poster or PDF |
| `APP_AI_GEMINI_THINKING` | `low` | Gemini's thinking level for these calls (`low`, `medium`, `high`; empty: the model's default) |
| `APP_AI_TIMEOUT_SECONDS` / `APP_AI_VISION_TIMEOUT_SECONDS` | `30` / `60` | one call, without and with a file |
| `APP_AI_DEADLINE_SECONDS` | `100` | the whole request with every fallback; keep it under nginx's `proxy_read_timeout` (120 s) |
| `APP_AI_FALLBACK_PROVIDER`, `_API_KEY`, `_MODELS` | empty | an optional second provider for the platform (`anthropic`), tried after every Gemini model; its own key, and its models (empty: `claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5`). A different service, never a second Gemini key |
| `APP_AI_KEYS_SECRET` | empty | the Fernet key clubs' own AI keys are encrypted with (a list rotates, as for `APP_PAYMENT_KEYS_SECRET`). Empty: no club can connect its own key, and one already saved answers 503 rather than falling back to ours |

The model ids are Google's, from https://ai.google.dev/gemini-api/docs/models
(checked 2026-10-20; both stable, both read images and PDFs). When Google
retires one, change `APP_AI_MODELS` in the server's `.env` and recreate the
backend (step 4 below); no code changes. The SDKs are `google-genai` and `anthropic` (requirements.txt).

**One key, always.** Exactly one Gemini key is configured. Never spread
calls over several keys, accounts or projects to get past Gemini's rate
limits: that is against Google's terms. The fallback is between models,
each with its own limits; past them, clubs get 503 and try again.

### The plans and their monthly actions

Each successful draft, piece of writing or new summary is one action; a
failed call is never counted. On the platform's key a club needs the plan
flag `ai_assist`, and has `ai_actions_per_month` of them (null: no cap),
counted per club per calendar month in India. Seeded by the `ai_assist`
migration:

| Plan | `ai_assist` | `ai_actions_per_month` |
|---|---|---|
| Free | off | 0 |
| Starter | on | 20 |
| Growth | on | 150 |
| Pro | on | 500 |
| Campus, Enterprise | on | unlimited |

Past the cap: 429 `AI_QUOTA_REACHED` ("You've used 20 of 20 AI actions this
month on Starter. They reset on 1 November."), with the next plan up in
`errors`. Admins edit both in Admin -> Subscriptions like every flag and
limit; until the admin app lists `ai_actions_per_month` among its limit
fields, saving a plan there keeps the value it has. As with every plan gate,
an admin (or an admin acting as a club) is never gated or capped (their
actions still show in the club's count), and
`APP_PLAN_ENFORCEMENT=false` lifts the flag and the cap too: `APP_AI_ENABLED`
is the switch that stops spending.

### A club's own key

The owner or an admin member pastes a Gemini or Anthropic key and picks a
model (Anthropic: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`;
Gemini: the models above). It is tested with one tiny call before it is
saved (422 `AI_KEY_INVALID`, saying why, when it fails), encrypted, and
shown only as its last four characters; it is never returned, logged or
audited. Then every AI action of the club runs on it: its provider, its
model and the lighter ones after it, never counted against the plan (plan
flag `ai_byok`, on every plan). A failing club key is 401 `AI_KEY_INVALID`
or 402 `AI_KEY_QUOTA`, never quietly swapped for the platform's.
`APP_AI_ENABLED` still has to be on.

### What is sent, and what is kept

Only what a feature needs: the club's notes or file, an event's public facts
(name, date, venue, about, the club's name), survey comments and ratings
without names or registrations. Email addresses and phone numbers are
scrubbed out of everything sent. What people wrote is sent in data blocks
the model is told never to take instructions from, and every answer is
checked against a schema (one retry, then 503). No prompt and no answer is
logged or stored, except the last feedback summary of each event
(`ai_feedback_summaries`).

### Reading `ai_usage`

One row per action: `club_id`, `user_id` (the club's account, or an admin's),
`member_id` (the team member, if one), `action` (`event_draft`, `write`,
`feedback_summary`, `key_check`), `provider`, `model` (the one that answered,
or the last tried), `byok`, `input_tokens`, `output_tokens`, `latency_ms`,
`ok` (null while under way; true counts; false never does) and `error`
(`rate_limited`, `server`, `timeout`, `auth`, `billing`, `refused`,
`invalid_output`...). Read-only, in the psql session from the top of this
runbook:

```sql
-- This month (India) per club: actions on our key, on their own, failures.
SELECT club_id,
       count(*) FILTER (WHERE ok AND NOT byok) AS counted,
       count(*) FILTER (WHERE ok AND byok)     AS own_key,
       count(*) FILTER (WHERE ok IS FALSE)     AS failed
FROM ai_usage
WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'
GROUP BY club_id ORDER BY counted DESC LIMIT 20;

-- Tokens and latency per model over the last week (our key only: what we pay for).
SELECT model, count(*), sum(input_tokens) AS input, sum(output_tokens) AS output,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM ai_usage
WHERE NOT byok AND ok AND created_at > now() - interval '7 days'
GROUP BY model ORDER BY count(*) DESC;

-- What is failing, and on which model.
SELECT error, model, count(*) FROM ai_usage
WHERE ok IS FALSE AND created_at > now() - interval '1 day'
GROUP BY error, model ORDER BY count(*) DESC;
```

Many `rate_limited` on the first model: its quota (AI Studio -> the
project's rate limits) is too small for the traffic; raise the project's
tier, never add a second key. Many `invalid_output`: a model that has
stopped following the schema; move another first in `APP_AI_MODELS`.

### Turning it on

1. **A paid key.** In Google AI Studio, make the key on a Google Cloud
   project with billing enabled (the paid tier). On the free tier, Google
   may use what is sent -- clubs' notes, posters, attendees' comments -- to
   improve its products, and people may read it; production must never use
   a free-tier key. Set a budget alert on the project's billing.
2. `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`:
   the value for `APP_AI_KEYS_SECRET`.
3. Deploy the code and the new requirements, with the migration:
   `sync_code`, `install_requirements`, `migrate` (section 1). The base
   layer changes (pydantic, google-auth, typing_extensions), so the build
   installs it again: expect the long build (section 8), off-peak.
4. On the server, add `APP_GEMINI_API_KEY`, `APP_AI_KEYS_SECRET` and
   `APP_AI_ENABLED=true` to `/var/www/myotherapp-production/.env` by hand
   (`sudo nano .env`), then recreate the backend so it reads them:
   `$DC up -d --build --no-deps backend < /dev/null`. Do NOT use the
   workflow's `sync_env` here: it writes the whole `.env` from the
   `PROD_ENV_VARIABLES` secret, which is older than the server's file and
   would put retired keys back.
5. Check, as a club on Starter or up: `GET /api/v1/clubs/ai/status` says
   `enabled: true`; one draft from a line of notes works; its row is in
   `ai_usage` with `ok` true and a model.
6. To stop at once: set `APP_AI_ENABLED=false` in the server's `.env` and
   recreate as in step 4.
   Clubs see 403 `AI_DISABLED`; nothing is called.

A poster near 10 MB, with the form around it, is a little over 10 MB: an
nginx in front with `client_max_body_size 10M` refuses it (413) before the
API sees it. Give that location 12M if large posters matter.

---

## 15. The demo: People Like You, live now

The demo's reset, seed and removal are in `backend/scripts/demo/README.md`.
People Like You only has people in it during an event, so one more script
puts a showcase event live now: Respawn Room's "Open Night", started ten
minutes ago, four hours long. It checks in the main demo user and ten demo
members, one of them hidden.

```bash
# Read-only: what it would do.
$DC exec -T backend python -m scripts.demo.live_now --dry-run

# Changes things, only inside the demo: the event, registrations, check-ins
# and the members' People Like You switch. Sends nothing; free; safe to rerun.
$DC exec -T backend python -m scripts.demo.live_now
```

Then sign in to the app as `demo@demo.myotherapp.in`. The event's page has
the People like you card, and Home's DISCOVER says where you are.

It needs the backend deployed with the `add_networking_visible` migration
(`migrate`, section 1). If its summary says `networking: OFF`, the showcase
club's plan lacks Attendee networking. The rest is in that README, section 7.

## 16. Placing visitors by address (GeoLite2)

A visitor who shares no location is placed roughly by their address: the
app's first guess of a city, and the API's "near you" for a reader the site
cannot place itself (`app/api/discover/geo.py`). Two ways, by `.env`:

| Setting | What answers |
|---|---|
| `APP_GEOIP_MMDB_PATH` | A MaxMind GeoLite2-City database on the box. No request leaves the server, nothing is waited for, nothing is refused. Wins when set and readable. |
| `APP_GEOIP_URL` (the default, ipapi.co) | Otherwise. Its free tier is about a thousand lookups a day. A place found is kept a week. After a refusal the lookup rests a minute, doubling to ten while refusals continue; meanwhile an address placed in the last 30 days keeps its place, and only one never seen goes unplaced. |

Today the box has no database, so ipapi.co answers. Its refusals are what
place new visitors "nowhere" for a while. GeoLite2 ends that, at no cost.

The Security page's sign-in places still use `APP_GEOIP_URL`, from the same
free tier.

### Turning GeoLite2 on (once)

1. **The owner, in a browser:** sign up for a free GeoLite2 account at
   `https://www.maxmind.com/en/geolite2/signup` and accept the GeoLite2
   licence. Then, under *Manage License Keys*, generate a licence key. Note
   the account ID and the key. Only you type the key, on the server, in
   step 3. Never put it in the repository, a workflow secret or a chat.
2. **Deploy the code** with `sync_code`. Since this change,
   `docker-compose.prod.yml` mounts `/var/lib/myotherapp/geoip` read-only
   at `/geoip`. The folder is outside the project directory, as the backups
   are, because the deploy's `rsync --delete` would erase a file inside it.
   The mount reaches the backend when it is recreated, in step 5.
3. **Download the database** on the server. This changes things: it writes
   one file. The key is read without echoing and never lands in the shell
   history.

   ```bash
   sudo mkdir -p /var/lib/myotherapp/geoip
   read -r -p 'MaxMind account ID: ' MM_ACCOUNT; read -r -s -p 'MaxMind licence key: ' MM_KEY; echo
   TMP=$(mktemp -d)
   URL='https://download.maxmind.com/geoip/databases/GeoLite2-City/download?suffix=tar.gz'
   curl -fsSL -u "$MM_ACCOUNT:$MM_KEY" -o "$TMP/city.tar.gz" "$URL"
   curl -fsSL -u "$MM_ACCOUNT:$MM_KEY" -o "$TMP/city.sha256" "$URL.sha256"
   echo "$(cut -d' ' -f1 "$TMP/city.sha256")  $TMP/city.tar.gz" | sha256sum -c -    # must say OK
   tar -xzf "$TMP/city.tar.gz" -C "$TMP"
   sudo install -m 0644 "$TMP"/GeoLite2-City_*/GeoLite2-City.mmdb /var/lib/myotherapp/geoip/GeoLite2-City.mmdb.new
   sudo mv /var/lib/myotherapp/geoip/GeoLite2-City.mmdb.new /var/lib/myotherapp/geoip/GeoLite2-City.mmdb
   rm -rf "$TMP"; unset MM_KEY MM_ACCOUNT
   ls -lh /var/lib/myotherapp/geoip/                                                # about 60 MB
   ```

4. **Point the backend at it.** Add one line to
   `/var/www/myotherapp-production/.env` by hand (`sudo nano .env`):

   ```bash
   APP_GEOIP_MMDB_PATH=/geoip/GeoLite2-City.mmdb
   ```

   Do NOT use the workflow's `sync_env` for this. It writes the whole
   `.env` from the `PROD_ENV_VARIABLES` secret, which is older than the
   server's file and would put retired keys back.
5. **Recreate the backend** so it reads the line and gets the mount. This
   changes things. The API returns 502 for about a minute.

   ```bash
   $DC up -d --build --no-deps backend < /dev/null
   ```

6. **Check.** All read-only.

   ```bash
   $DC exec -T backend python -c "import maxminddb; m = maxminddb.open_database('/geoip/GeoLite2-City.mmdb').metadata(); print(m.database_type, m.build_epoch)"
   $DC logs --since 10m backend | grep GEOIP_MMDB_PATH    # after a few visits, nothing: a file that cannot be opened is logged once
   ```

   The first command should print `GeoLite2-City` and a recent epoch. An
   `ImportError` means the image predates `maxminddb` in
   `requirements.txt`: deploy with `install_requirements`, then check
   again. From your own laptop, not the server,
   `curl -s https://api.myotherapp.in/api/v1/discover/locate` should say
   `"source":"ip"` with your city.

### Keeping it current

MaxMind updates GeoLite2 twice a week, and its licence asks you to move to
a new release within 30 days. Once a month, repeat step 3. Then restart the
backend, because the file is opened once per process:

```bash
$DC restart backend < /dev/null      # changes things: about a minute of 502s
```

**To turn it off:** delete the line from `.env`, then recreate as in step
5. ipapi.co answers again.

---

## 17. The demo's venue pins

A demo seeded before each venue had its own point has every in-person event
pinned beside its club. "Systems Weekend at Vagamon" shows 2.9 km from
Kochi, when it is some 73 km away. Deploy the code first (`sync_code`). The
script and its data come with it, and nothing needs a restart. Then:

```bash
$DC exec -T backend python -m scripts.demo.fix_pins --dry-run    # read-only: what would move, and how far
$DC exec -T backend python -m scripts.demo.fix_pins              # changes things: moves them, in one transaction
```

It touches only the demo clubs' events. Within those, it moves only the pins
the seed made; anything someone changed by hand stays as it is. Running it
twice is safe. The rules, and what the output says, are in
`backend/scripts/demo/README.md`, section 8, next to the demo's reset, seed
and remove.

---

## 18. WhatsApp (Meta's WhatsApp Cloud API)

Off until the server's `.env` has the access token and the phone number id;
until then nothing is sent on the channel and the clubs dashboard's
providers list says "No WhatsApp provider is configured". Only the five
templates below are ever sent, and only to a guest who ticked **"Send my
ticket and event reminders on WhatsApp"** on the website's registration
form (the box shows once a phone number is typed). Nobody is opted in
without ticking it; registrations made before this are not opted in. The
app's registration screen does not show the box yet.

**What goes, and when** (each beside the email, never instead of it):

| When | Template | Sent once per guest? |
|---|---|---|
| The ticket goes out: at registration (free event), at payment (paid event), or Send tickets on the attendees page | `ticket_confirmed`, with the ticket PDF | yes: Send again re-sends the email, never the WhatsApp |
| Send certificates | `certificate_ready`, with the certificate PDF and its own number (`CERT-...`) | yes |
| About 24 hours before the start, by the minute sweep, to guests who registered before then, hold a live ticket (paid for, on a paid event) and the event is published | `event_reminder` | yes, ever: a moved event does not get a second |
| Not wired yet: there is no pending-payment email to go beside | `payment_pending` | -- |
| Not used yet | `event_update` | -- |

Skipped, with the reason on the attendee's delivery row: a guest who did
not tick the box (no WhatsApp row at all), a number that is not a mobile
number (India is assumed without a country code), a number WhatsApp said
cannot receive messages (marked until the organiser corrects it), and the
demo's invented people (`APP_MAIL_SUPPRESS_DOMAINS`). Meta busy or
rate-limiting: tried again with email's backoff, up to `APP_MAIL_MAX_ATTEMPTS`.
A template or token problem: failed, with Meta's code in the reason. The
code reads Meta's answer in `backend/app/core/whatsapp.py`.

### Settings

| Setting (`.env`) | Default | Meaning |
|---|---|---|
| `APP_WHATSAPP_ACCESS_TOKEN` | empty | the system user's permanent token. With the phone number id: the switch |
| `APP_WHATSAPP_PHONE_NUMBER_ID` | empty | the sending number's id (not the number itself) |
| `APP_WHATSAPP_APP_SECRET` | empty | the app's secret: the hook checks each status call's signature with it, and refuses every one without it |
| `APP_WHATSAPP_VERIFY_TOKEN` | empty | what Meta must send back when the webhook is subscribed |
| `APP_WHATSAPP_GRAPH_VERSION` | `v23.0` | the Graph API version in the URLs. Meta retires one about two years after its release: move it on when Meta says so |
| `APP_WHATSAPP_TEMPLATE_LANGUAGE` | `en` | the language the templates were approved in |
| `APP_WHATSAPP_TIMEOUT_SECONDS` | `15` | one call to Meta |

### The templates to submit

In WhatsApp Manager -> Message templates, five templates, each **category
Utility, language English (`en`)**. The names, the order of the values and
the buttons must be exactly these: the code fills them in this order, and
Meta refuses a send whose values do not match (132000).

1. `ticket_confirmed`. Header: Document. Body: `Hi {{1}}, you're registered for {{2}} on {{3}}. Your ticket ({{4}}) is attached. Show its QR code at the entry.` Button: Visit website, dynamic, `https://myotherapp.in/tickets/{{1}}`.
2. `event_reminder`. Body: `Hi {{1}}, a reminder that {{2}} starts {{3}} at {{4}}. Keep your ticket ready at the entry.` Button: Visit website, dynamic, `https://myotherapp.in/tickets/{{1}}`.
3. `certificate_ready`. Header: Document. Body: `Hi {{1}}, thank you for attending {{2}}. Your certificate from {{3}} is attached. Anyone can check it is genuine on myotherapp.in with the number {{4}}.` Button: Visit website, dynamic, `https://myotherapp.in/verify/{{1}}`.
4. `payment_pending`. Body: `Hi {{1}}, your registration for {{2}} is waiting for a payment of ₹{{3}}. Complete it to keep your spot.` Button: Visit website, dynamic, `https://myotherapp.in/events/{{1}}/register`.
5. `event_update`. Body: `Hi {{1}}, {{2}} has an update about {{3}}: {{4}}. See the event page for details.`

The words may change (Meta's review may ask for it) as long as each
template keeps its values in this order, its header and its button: the
code fills in values, never words. Pick the language **English** (`en`),
not English (US) (`en_US`), or set `APP_WHATSAPP_TEMPLATE_LANGUAGE` to match.

Meta asks for a sample value for each `{{n}}` when the template is
submitted: `Asha`, `Build Night`, `Sat 25 Oct 2026, 6:30 PM`,
`MOA-K7Q2M-9XH4P` (a certificate: `CERT-K7Q2M-9XH4P`; an amount: `1,499`;
a slug: `build-night-k7q2m`). A template must say `Approved` before step 6.

### Turning it on

1. **Deploy the code with its migration** (`whatsapp_opt_in`): `sync_code`,
   `restart_backend`, `migrate` (section 1). It adds two columns and an
   index to `event_registrations_link` and `reminder` to the `deliverykind`
   type; nothing is sent until step 5.
2. **A permanent token.** Meta Business settings -> Users -> System users ->
   Add (an admin system user) -> Assign assets: the app, and the WhatsApp
   account with full control -> Generate new token: the app, expiry
   **Never**, permissions `whatsapp_business_messaging` and
   `whatsapp_business_management`. Copy it once; it is not shown again.
   (The temporary token on the app's API Setup page lasts 24 hours: never
   use it here.)
3. **The phone number id.** The app's dashboard -> WhatsApp -> API Setup:
   the "Phone number ID" under the sending number (a long number, not the
   phone number). **The app secret:** App settings -> Basic -> App secret ->
   Show. **A verify token:** run `openssl rand -hex 24` and keep the output.
4. On the server, add the lines to `/var/www/myotherapp-production/.env` by
   hand (`sudo nano .env`):

   ```
   APP_WHATSAPP_ACCESS_TOKEN=<the system user's token>
   APP_WHATSAPP_PHONE_NUMBER_ID=<the phone number id>
   APP_WHATSAPP_APP_SECRET=<the app secret>
   APP_WHATSAPP_VERIFY_TOKEN=<the openssl output>
   ```

5. Recreate the backend so it reads them (changes things: about a minute of
   502s):

   ```bash
   $DC up -d --build --no-deps backend < /dev/null
   ```

   Do NOT use the workflow's `sync_env`: it writes the whole `.env` from the
   `PROD_ENV_VARIABLES` secret, which is older than the server's file.
6. **The webhook.** The app's dashboard -> WhatsApp -> Configuration ->
   Webhook -> Edit: callback URL
   `https://api.myotherapp.in/api/v1/hooks/whatsapp`, verify token the
   openssl output from step 3 -> Verify and save (Meta calls the hook, which
   answers with Meta's challenge only when the token matches). Then, under
   Webhook fields, **Subscribe** to `messages`: that field carries the
   sent/delivered/read/failed statuses.
7. **One test message**, to your own phone (changes things: sends one real
   WhatsApp message):

   ```bash
   $DC exec -T backend python -m scripts.whatsapp_test --to <your number> --template ticket_confirmed
   ```

   It prints `{"sent": true, ..., "message_id": "wamid..."}`, or Meta's
   reason in words (an unapproved template, a token without a permission).
   It will not run without `--to`. Try each template the same way
   (`event_reminder`, `certificate_ready`, `payment_pending`,
   `event_update`); the document ones carry a sample PDF.
8. Check the dashboard: a club's deliveries page lists WhatsApp as
   configured; register for a test event from the website with the box
   ticked and your number, and the ticket arrives on WhatsApp beside the
   email, its row going `sent`, then `delivered` and `read` as the
   statuses arrive.

**To turn it off:** delete `APP_WHATSAPP_ACCESS_TOKEN` (or the phone number
id) from `.env` and recreate as in step 5. Nothing more is sent; what was
queued is skipped with "no WhatsApp provider is configured". The guests'
ticks stay, for when it is turned on again.

### Reading what was sent

Read-only, in the psql session from the top of this runbook:

```sql
-- WhatsApp sends over the last week, by kind and what became of them.
SELECT kind, status, outcome, count(*) FROM deliveries
WHERE channel = 'whatsapp' AND created_at > now() - interval '7 days'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- Why they failed or were skipped (Meta's code is in the brackets).
SELECT error, count(*) FROM deliveries
WHERE channel = 'whatsapp' AND status IN ('failed', 'skipped', 'dead') AND created_at > now() - interval '7 days'
GROUP BY error ORDER BY count(*) DESC LIMIT 20;

-- How many guests have asked for WhatsApp.
SELECT count(*) FROM event_registrations_link WHERE whatsapp_opt_in AND NOT is_deleted;
```

Many `(132001)`: a template is not approved in `en` under that exact name.
`(190)`: the token expired or was revoked (step 2 again). `(131030)`: the
account is still in test mode and the number is not on its test list.
`(131026)`: the number cannot receive WhatsApp; the guest's row is marked
until the number is corrected on the attendee page.

---

## 19. Production hardening: start-up checks, health URLs, scheduled backups

### Settings the API refuses to start with

In production the API (`main.py`, and the app's startup) and the job worker
check their settings before they serve anything
(`backend/app/core/config_checks.py`). On a problem the process stops with
exit status 78 and one line per setting, naming it and never its value:

```
Refusing to start: 2 setting(s) are missing, placeholders or half set (values not shown):
  - APP_S3_SECRET_KEY: holds a placeholder, not a real value
  - APP_WHATSAPP_PHONE_NUMBER_ID: is empty while APP_WHATSAPP_ACCESS_TOKEN is set: set both or neither
```

What is refused:

| Rule | Settings |
|---|---|
| Empty | `APP_SECRET_KEY`, `APP_DATABASE_URL`, `APP_DATABASE_URL_SYNC`, `APP_S3_BUCKET`, `APP_S3_ACCESS_KEY`, `APP_S3_SECRET_KEY`, `APP_S3_BASE_PATH`, `APP_RAZORPAY_KEY_ID`, `APP_RAZORPAY_KEY_SECRET`, `APP_RAZORPAY_WEBHOOK_SECRET`; with `APP_MAIL_PROVIDER=ses` (the default) `APP_SES_ACCESS_KEY`, `APP_SES_SECRET_KEY`, `APP_SES_DEFAULT_SENDER`; with `resend`, `APP_RESEND_API_KEY` and `APP_MAIL_FROM_ADDRESS` |
| A placeholder, in any setting | `PASTE_KEY_HERE` (any "paste ... here"), `changeme`, `replace-me`, a run of `xxx`, `your-...`/`your_...`, `<...>`, `${...}`/`{{...}}`, an `example.com` address; and in a credential, id, address or the database password, whole values like `password`, `secret`, `test`, `none`, `postgres` |
| A weak `APP_SECRET_KEY` | under 32 characters, fewer than 10 distinct characters, or a value from a sample or a test suite |
| Half set | `APP_RAZORPAY_KEY_ID`/`_SECRET`, `APP_WHATSAPP_ACCESS_TOKEN`/`APP_WHATSAPP_PHONE_NUMBER_ID`, `APP_CLOUDFLARE_API_TOKEN`/`APP_CLOUDFLARE_ZONE_ID`, `APP_BACKUP_S3_ACCESS_KEY`/`_SECRET_KEY`, `APP_AI_FALLBACK_PROVIDER`/`APP_AI_FALLBACK_API_KEY` (both or neither); `APP_SEARCH_URL` without `APP_SEARCH_KEY` |
| The wrong shape | `APP_MAIL_PROVIDER` not `ses` or `resend`; `APP_BACKUP_S3_SSE` not `AES256` or `aws:kms`; `APP_PAYMENT_KEYS_SECRET` or `APP_AI_KEYS_SECRET` not Fernet keys; `APP_SENTRY_DSN` not a DSN; `APP_FIREBASE_SERVICE_ACCOUNT_BASE64` not a base64 service account; `APP_DEBUG` on with `APP_ENV=production` |

Logged as warnings, never fatal: a `rzp_test_` key, no Sentry DSN, no
off-site bucket, no Firebase credentials (or a missing file), AI on without
`APP_GEMINI_API_KEY`, Resend without its webhook secret, a short site token,
`*` in `APP_CORS_ORIGINS`.

**Which processes count as production.** `APP_ENV` names the deployment:
`production`, `staging` or anything else is checked; `development`, `dev`,
`local`, `test` and `ci` are not. With `APP_ENV` unset, a process is checked
whenever `APP_DEBUG` is off -- so production needs nothing new, a laptop sets
`APP_DEBUG=true` (or `APP_ENV=development`), and staging is checked like
production (set `APP_ENV=staging` there; Sentry then tags its events
`staging`).

**Before deploying a change to `.env`, or the first deploy of these checks**
(read-only; the synced code is bind-mounted, so after `sync_code` the
running container already has the checker):

```bash
$DC exec -T backend python -m app.core.config_checks                                   # the environment the container has now
sudo cat .env | $DC exec -T backend python -m app.core.config_checks --env-file -    # the .env the next container will get
```

It prints `FATAL` and `warn` lines (names only) and exits 1 when a restart
would be refused. Fix those lines in the `PROD_ENV_VARIABLES` secret first.

**When it has refused** (the deploy's smoke check fails; the backend keeps
restarting): `$DC logs --tail 40 backend` shows the lines above. Fix the
settings, `sync_env`, `recreate_backend`. To get the site up while you fix
it, `APP_CONFIG_CHECKS=warn` in `.env` logs every finding at ERROR and
starts anyway; remove it once fixed.

### Health URLs for uptime monitors

| URL | Answers | Use |
|---|---|---|
| `https://api.myotherapp.in/health` | 200 `{"status": "ok", "revision": "<7-character git sha>"}` while the process answers; asks nothing else | liveness |
| `https://api.myotherapp.in/health/ready` | 200 `{"status": "ok", "checks": {"database": "ok"}, "revision": "..."}`; 503 with `"status": "fail"` when the database does not answer `SELECT 1` within 1 second, or Redis does not answer (only when `APP_REDIS_URL` is set: `"redis": "ok"` is then in `checks`) | what to monitor |

Both take GET or HEAD, need no sign-in, are never rate limited, answer
`Cache-Control: no-store`, and carry nothing but the status, the checks and
the short sha. `/healthz` and `/readyz` stay as they were (`/readyz` also
compares the database's migration with the code's, for the deploy's smoke
check: set the repository variable `PROD_SMOKE_URL` to
`https://api.myotherapp.in/readyz`).

Monitor `/health/ready` every minute (UptimeRobot, Better Stack...): alert
on a non-200 or on `"status":"fail"`, after 3 failures in a row -- a deploy
that recreates the backend is a minute of 502s. Point the alert at the same
people as `APP_ADMIN_ALERT_EMAILS`.

### Daily off-site backups, on a systemd timer

`ops/backup` (synced to the server with the code) runs section 4's
`scripts/backup.py` every day at **02:30 India time** (21:00 UTC), replacing
section 4's cron lines:

- `ops/backup/systemd/myotherapp-backup.timer` -> `myotherapp-backup.service`
  -> `ops/backup/run.sh backup` -> `docker-compose exec -T backend python
  scripts/backup.py --no-alert`: `pg_dump` (custom format, compressed) into
  `/var/backups/myotherapp`, uploaded to
  `s3://<bucket>/backups/daily/backup-YYYYMMDD-HHMMSS.dump` (or `weekly/`)
  with server-side encryption and its sha256, older local dumps pruned to
  `APP_BACKUP_KEEP_LOCAL`. `Persistent=true`: a run missed while the box was
  off happens when it is back.
- On any failure -- the dump, the upload, the container not running, a
  timeout -- `OnFailure=` starts `myotherapp-alert@myotherapp-backup.service`
  -> `ops/backup/alert.sh`, which raises one `backup_failed` admin alert
  through the app (a mail to `APP_ADMIN_ALERT_EMAILS`, a post to
  `APP_ADMIN_ALERT_DISCORD_WEBHOOK`, the dashboard's bell) with the run's
  last log lines; when the app cannot (container or database down), it posts
  straight to the Discord webhook in `.env`.

**AWS (once, in the console or with the CLI; the app creates none of it).**
Section 4 has the lifecycle JSON and the IAM policy in full.

1. Bucket `myotherapp-backups` in `ap-south-1`: *Block all public access* on,
   *Default encryption* SSE-S3, *Versioning* on.
2. Lifecycle (section 4's JSON): `backups/daily/` expires after 14 days,
   `backups/weekly/` after 56, old versions after 7, abandoned uploads after 2.
3. IAM user `myotherapp-backups` with only section 4's policy:
   `s3:PutObject`, `s3:GetObject`, `s3:AbortMultipartUpload`,
   `s3:ListMultipartUploadParts` on `arn:aws:s3:::myotherapp-backups/backups/*`
   and `s3:ListBucket` on the bucket for the `backups/*` prefix -- no delete.
   Create an access key for it.
4. For the staging drill, a second user with only `s3:GetObject` on
   `.../backups/*` and `s3:ListBucket` for that prefix.

**`.env`** (the `PROD_ENV_VARIABLES` secret; `sync_env`, then `recreate_backend`):

```
APP_BACKUP_S3_BUCKET=myotherapp-backups
APP_BACKUP_S3_REGION=ap-south-1
APP_BACKUP_S3_ACCESS_KEY=<the backup user's key id>
APP_BACKUP_S3_SECRET_KEY=<its secret>
APP_BACKUP_KEEP_LOCAL=7
APP_ADMIN_ALERT_EMAILS=<who hears about a failed backup, comma-separated>
APP_ADMIN_ALERT_DISCORD_WEBHOOK=<a Discord channel's webhook, optional>
```

(`APP_BACKUP_S3_PREFIX=backups/` and `APP_BACKUP_S3_SSE=AES256` are the
defaults; `aws:kms` with `APP_BACKUP_S3_KMS_KEY_ID` for a KMS key.)

**On the server (once; changes things).** After a deploy with `sync_code`,
so `ops/` and the new `scripts/backup.py` are there:

```bash
cd /var/www/myotherapp-production
sudo install -d -m 0700 /var/backups/myotherapp
systemd-analyze calendar "*-*-* 02:30:00 Asia/Kolkata"       # read-only: prints the next run in UTC
sudo install -m 0644 ops/backup/systemd/myotherapp-backup.service ops/backup/systemd/myotherapp-backup.timer \
  'ops/backup/systemd/myotherapp-alert@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now myotherapp-backup.timer
systemctl list-timers myotherapp-backup.timer                 # NEXT: 21:00 UTC
sudo crontab -l | grep -n scripts/                             # remove section 4's cron lines if they are there: sudo crontab -e
```

Then prove it, once:

```bash
sudo systemctl start myotherapp-backup.service                # a backup now (seconds)
journalctl -u myotherapp-backup -n 20 --no-pager               # ends with BACKUP OK and the s3:// key
sudo systemctl start myotherapp-alert@myotherapp-backup.service   # a test alert: check the mail and Discord
```

Nothing to change in `ops/backup` for production. Elsewhere (another path,
`docker-compose.yml`), copy `ops/backup/myotherapp-backup.default` to
`/etc/default/myotherapp-backup` and set `MOA_PROJECT_DIR`,
`MOA_COMPOSE_FILE`. A failed run shows in `systemctl --failed` and
`journalctl -u myotherapp-backup`; the alert says the same.

### The restore drill, monthly on staging

`scripts/restore_drill.py` downloads the newest dump in S3, checks it against
the sha256 recorded at upload, restores it into a new
`restore_drill_YYYYMMDD_HHMMSS` database, checks every table in the dump was
restored and the core tables (`users`, `clubs`, `events`, registrations,
memberships, payments, notifications) have rows, prints the counts, and
drops the scratch database. `--standalone` checks the restore on its own
(staging); without it the counts are put beside the live database's
(read-only). `--max-age-hours 26` fails when the newest dump is older (a
missed backup).

On the staging box (its backend's `.env` gets `APP_BACKUP_S3_BUCKET` and the
read-only user's keys as `APP_BACKUP_S3_ACCESS_KEY`/`_SECRET_KEY`):

```bash
cd <staging project dir>
sudo install -m 0644 ops/backup/myotherapp-backup.default /etc/default/myotherapp-backup
sudoedit /etc/default/myotherapp-backup     # MOA_PROJECT_DIR=<that dir>, MOA_COMPOSE_FILE=docker-compose.yml
sudo install -m 0644 ops/backup/systemd/myotherapp-restore-drill.service ops/backup/systemd/myotherapp-restore-drill.timer \
  'ops/backup/systemd/myotherapp-alert@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now myotherapp-restore-drill.timer    # the 1st of each month, 04:00 IST
sudo systemctl start myotherapp-restore-drill.service         # once now: journalctl -u myotherapp-restore-drill ends RESTORE DRILL PASSED
```

By hand, anywhere: `sudo /bin/bash ops/backup/run.sh drill --standalone`.
On production (off-peak; it costs CPU and disk while it runs):
`$DC exec -T backend python scripts/restore_drill.py --max-age-hours 26`.
