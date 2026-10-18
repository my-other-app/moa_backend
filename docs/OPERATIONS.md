# Operations runbook

How to deploy, roll back, unstick a migration, restore a backup, rotate a
secret, turn on the new services and size the server. Every command is
meant to be copied. Commands under **Read-only** change nothing; anything
that changes something says so.

The production box runs `docker-compose.prod.yml` from
`/var/www/myotherapp-production` (the `PROD_PROJECT_PATH` secret): `postgres`
(17.4, data in the `postgres_data` volume) and `backend` (code bind-mounted
from `./backend`, published on 8000 and 8001 behind the host's nginx).
Backups live outside the project directory, in `/var/backups/myotherapp`.

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
| `recreate_backend` | a new backend container from the current image, no build: how an `.env` change reaches the process (`restart` keeps the old environment). With `migrate`, migrates first |
| `migrate` | `alembic upgrade head` in the running backend container, then prints the applied revision |
| `restart_backend` | `restart backend` (new code, same environment), skipped when the backend was recreated |
| `build_backend`, `up_backend` | build the image / `up backend` (without `--no-deps`, which can also recreate postgres after an `.env` change: prefer `recreate_backend`) |
| `restart_database` | `up -d postgres` |
| `restart_all` | `up --build -d` for everything (old order: recreate, then migrate) |
| `services` | starts or refreshes extra services after the migration, e.g. `redis worker meilisearch`: `up -d --no-deps <svc>` each. Not `backend` or `postgres` |
| `smoke_timeout` | seconds the API has to answer 200 three times running after the deploy (default 180) |

Common runs:

| Change | Inputs |
|---|---|
| Code only | `sync_code`, `restart_backend` |
| Code + migration | `sync_code`, `migrate`, `restart_backend` |
| New or changed requirements | `sync_code`, `install_requirements`, `migrate` |
| `.env` change only | `sync_env`, `recreate_backend` |
| Turn on a new service | `sync_env` (its settings), `services: redis`, then `recreate_backend` (section 6) |

What the job guarantees:

- The remote script is rendered on the runner, checked with `bash -n`, copied
  to the server and run from a file with `set -e`; its last line prints
  `MOA_DEPLOY_COMPLETE`, and the job fails if the marker is missing.
- Builds use BuildKit. The script checks `docker buildx version` before
  building and stops with nothing changed if it is missing (section 9).
- Migrations run in the container that is serving, before any recreate: a
  failed migration leaves the old container serving.
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
lint workflow) flags, in every migration after `topics`, a non-concurrent
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
the running one before any `install_requirements` you are unsure of:

```bash
IMG=$(sudo docker inspect --format '{{.Config.Image}}' doth-live-backend)    # read-only
sudo docker tag "$IMG" "$IMG-previous"                                       # changes: adds a tag
```

and to go back (changes: recreates the backend):

```bash
sudo docker tag "$IMG-previous" "$IMG"
$DC up -d --no-deps --force-recreate backend
```

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
- **Daily** (once the cron line below is installed): `scripts/backup.py` takes
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
4. **Schedule** (root's crontab on the host: `sudo crontab -e`; find the
   compose binary with `command -v docker-compose`). 21:30 UTC is 03:00 IST:

   ```cron
   30 21 * * * cd /var/www/myotherapp-production && /usr/local/bin/docker-compose -f docker-compose.prod.yml exec -T backend python scripts/backup.py >> /var/log/myotherapp-backup.log 2>&1
   0 22 1 * * cd /var/www/myotherapp-production && /usr/local/bin/docker-compose -f docker-compose.prod.yml exec -T backend python scripts/restore_drill.py --max-age-hours 26 >> /var/log/myotherapp-restore-drill.log 2>&1
   ```

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

5. `$DC up -d --no-deps backend`, check `alembic current` matches the code
   (migrate if the dump is older), and the smoke URL answers.
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
| `APP_SECRET_KEY` | `openssl rand -hex 32` | signs every token: everyone is signed out |
| `APP_RAZORPAY_KEY_ID` / `_SECRET` | Razorpay dashboard -> API keys | regenerate, update, recreate |
| `APP_RAZORPAY_WEBHOOK_SECRET` | Razorpay dashboard -> Webhooks | set the same value in both, then recreate at once: webhooks fail in between (Razorpay retries) |
| `APP_PAYMENT_KEYS_SECRET` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | a comma-separated list: put the new key **first**, keep the old after it; drop the old once every saved club key has been re-saved |
| `APP_SITE_TOKENS` | `openssl rand -hex 32` | a list: add the new token, update the site (Vercel), then remove the old |
| `APP_SITE_REVALIDATE_SECRET` | `openssl rand -hex 32` | the same value on the site (Vercel) and here |
| `APP_S3_ACCESS_KEY` / `_SECRET_KEY`, `APP_SES_*`, `APP_BACKUP_S3_*` | AWS IAM -> the user -> security credentials | create the second key, switch, recreate, then deactivate and delete the first |
| `APP_RESEND_API_KEY`, `APP_RESEND_WEBHOOK_SECRET` | Resend dashboard | |
| `APP_CLOUDFLARE_API_TOKEN` | Cloudflare -> API tokens (roll) | |
| `APP_WHATSAPP_*` | Meta developer console | |
| `APP_APPLE_PRIVATE_KEY`, Firebase service account | Apple / Google consoles | |
| Wallet pass signing key and certificate | Apple developer -> Pass Type IDs | replace the files in `backend/wallet_certs`, not in git |
| `PROD_SSH_PRIVATE_KEY` | `ssh-keygen -t ed25519` | add the new public key to the server's `authorized_keys`, update the secret, deploy once, remove the old key |
| `APP_SENTRY_DSN` | Sentry -> project -> Client Keys | not really secret (it can only send events); revoke the old key there |

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

Wave 2 adds services to `docker-compose.prod.yml` that the normal deploy
never starts (it recreates only `backend`). The names and addresses below are
the ones the platform work gives them; check `$DC config --services`. Each is off until its setting is
set, and the app runs as before without it. Order for each: deploy the code
(with `migrate`), start the service, then set the setting and recreate the
backend.

| Service | Start | Then set (`PROD_ENV_VARIABLES`) | Turn off |
|---|---|---|---|
| Redis | `services: redis` | `APP_REDIS_URL=redis://redis:6379/0` (rate limits, caches and live fan-out use it) | unset, `recreate_backend` |
| Job worker | `services: worker` (after the migration that makes `jobs`) | `APP_JOBS_WORKER=true` on the API: it then only enqueues | unset first, `recreate_backend`, wait for the queue to drain, `$DC stop worker` |
| Meilisearch | `services: meilisearch` | `APP_SEARCH_URL=http://meilisearch:7700`, `APP_SEARCH_KEY=<master key>` | unset (Postgres search again) |

By hand on the server, the same as the `services` input (changes things):

```bash
$DC config --services                         # read-only: what the file defines
$DC up -d --no-deps redis                     # start or refresh one service
$DC ps
$DC logs --tail=100 worker
```

A service built from `backend/` (the worker) is rebuilt only when its image
is missing; after `install_requirements`, refresh it with
`$DC up -d --no-deps --build worker` (or `--force-recreate`) so it runs the
new image and code too.

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
   default `production`; `APP_SENTRY_TRACES_SAMPLE_RATE=0.05` to trace 5% of
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
new one skips the same parts there.

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
$DC up -d --no-deps --force-recreate backend
```

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
  head`, `up -d --no-deps --force-recreate backend`). The transfer is the
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
