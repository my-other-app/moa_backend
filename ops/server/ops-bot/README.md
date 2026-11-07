# ops-bot: the box on the owner's Telegram

One small Python program (standard library only) that runs on the server
as `moa-ops-bot.service`:

- **Nightly backup at 03:00 IST**, encrypted with `age` before it leaves the
  box, sent to the owner's chat as a file.
- **Alerts**, checked every 60 s: containers, disk, memory, SSH logins,
  error bursts in the logs. One message when something goes wrong, one when
  it recovers.
- **Commands**: `/status`, `/disk`, `/logs <service> [n]`, `/backup`, `/help`.

It long-polls Telegram (`getUpdates`): no webhook, no open port. It answers
one chat id and nobody else.

## How it fits together

```
myotherapp-backup.timer (03:00 Asia/Kolkata)   or  /backup in Telegram
  -> myotherapp-backup.service
       ExecStart      ops/backup/run.sh backup    pg_dump in the backend container -> /var/backups/myotherapp (+ S3)
       ExecStartPost  ops/backup/run.sh deliver   -> run.py deliver-backup:
                        newest dump (fresh, PGDMP) -> age -r <owner's key> -> /var/lib/moa-ops-bot/outbox/*.age
                        -> sendDocument (refuses anything that is not an age file) -> the .age copy deleted
       OnFailure      myotherapp-alert@ -> ops/backup/alert.sh -> run.py notify (Telegram), then the app / Discord

moa-ops-bot.service -> run.py serve
  poller   getUpdates long poll; only TELEGRAM_CHAT_ID is answered
  watcher  every 60 s: docker ps/inspect/logs, /proc, the journal (sshd, kernel)
```

Files: `run.py` (entry), `opsbot/` (the package), `systemd/moa-ops-bot.service`,
`install.sh`, `../env/ops-bot.env.example` (the settings template), `tests/`.

## Settings: `/srv/moa/env/ops-bot.env`

root:root, mode **600** (the bot refuses to start otherwise). Never in git.
`install.sh` creates it from `ops/server/env/ops-bot.env.example` without a
token. The operator fills in:

| Key | Required | What |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather (`/newbot`) |
| `TELEGRAM_CHAT_ID` | yes | the owner's chat id: the only chat answered and written to |
| `OPS_AGE_RECIPIENT` | no | the owner's age public key; default `age17jsckeq68nf0yrz7sgtrrk7y6930uykd3t4je4t9tmnk8jqrwagqmmwu9v` |
| `OPS_HOST_LABEL` | no | the name in every message (default: the hostname) |
| `OPS_WATCH_INTERVAL` | no | seconds between checks (60) |
| `OPS_ERROR_MIN_LINES` | no | error lines in one interval that start an alert (3) |
| `OPS_ERROR_IGNORE_SERVICES` | no | comma list of services not scanned for errors (none) |
| `OPS_MEM_LOW_PCT` | no | alert under this % of memory available (10) |
| `OPS_DISK_PATHS` | no | `/,/srv/moa-data,/var/backups/myotherapp` |
| `OPS_BACKUP_DIR`, `OPS_COMPOSE_PROJECT`, `OPS_DC` | no | `/var/backups/myotherapp`, `moa`, `/srv/moa/dc` |

The chat id: send the new bot any message, then open
`https://api.telegram.org/bot<token>/getUpdates` in a browser and copy
`"chat":{"id":...}`. Only this file is read for settings; the bot never reads
`.env` or any other env file.

## Install (on the server, by hand)

```bash
# 1. the files: either a stack deploy (Deploy to Oracle -> stack), or a checkout
sudo bash /srv/moa/ops-bot/install.sh      # or: sudo bash ops/server/ops-bot/install.sh
#    installs age if missing; the bot and ops/backup files; the settings file
#    (no token); the units; enables the 03:00 timer and the bot.
#    Persistent=true: if today's 03:00 is past, the timer may run one backup at once.
# 2. the token and chat id
sudoedit /srv/moa/env/ops-bot.env
sudo python3 -I -B /srv/moa/ops-bot/run.py check-config    # ok: ... (never prints the token)
sudo python3 -I -B /srv/moa/ops-bot/run.py test-message    # a message arrives
sudo systemctl restart moa-ops-bot                         # "ops bot started" arrives
# 3. prove the backup path once
sudo systemctl start myotherapp-backup.service             # the .age file arrives
journalctl -u myotherapp-backup -n 20 --no-pager           # ... deliver-backup: ... sent encrypted
sudo systemctl start myotherapp-alert@myotherapp-backup.service   # a test alert arrives
systemctl list-timers myotherapp-backup.timer              # NEXT: 21:30 UTC = 03:00 IST
```

Run `install.sh` again after any change to these files; it is idempotent.
To turn it off: `sudo systemctl disable --now moa-ops-bot` and remove
`/srv/moa/env/ops-bot.env`. The backup unit keeps working: with no settings
file its delivery step only says `deliver-backup: Telegram is not set up`.

### Why root, and what it touches

Docker's socket and the full journal are root's, and `/backup` starts a
systemd unit, so the service runs as root -- sandboxed: `ProtectSystem=strict`
(the whole filesystem read-only except `/var/lib/moa-ops-bot`),
`ProtectHome`, `NoNewPrivileges`, `PrivateTmp`, 192 MB and 25% CPU at most.
What it runs: `docker ps/inspect/stats/logs/system df`, `journalctl`,
`systemctl is-active` and `systemctl start --no-block myotherapp-backup.service`
-- argument lists, no shell, each with a timeout. The delivery step
(`deliver-backup`) runs inside the backup unit, not the bot: `age` and one
`moa-dc exec -T backend pg_restore -l` to count the tables.

## Security

- **Encrypted before it leaves**: `age -r <recipient>`; `send_document`
  checks the file starts `age-encryption.org/v1` and refuses anything else.
  The private key is never on the server. The encrypted copy is deleted
  after the upload; the plaintext dump stays where `scripts/backup.py` puts
  it (and prunes it), as before.
- **One chat**: updates from any other chat are not answered, logged at most
  once a minute, and reported to the owner once a day. Commands that arrive
  more than 2 minutes late (the bot was down) are not run.
- **Redaction** (`opsbot/redact.py`) on every log line and alert body:
  bearer tokens, JWTs, `token=`/`key=`/`secret=`/`password=`/`s=`/`k=`
  values, the segment after `/tickets/`, `/certificates/`,
  `/registration-requests/`, `/waitlist/offers/`, `/clubs/access/invite/`,
  `/check-in/`, ticket codes, UUIDs, emails, phone numbers, URL passwords,
  bot tokens; capped at 3,500 characters (Telegram's own cap is 4,096).
- **The token** is read from the settings file only, kept out of `repr()`,
  logs and error messages.
- **Never a flood**: at most 20 messages a minute (then dropped and
  counted), 429 `retry_after` honoured, poll errors back off 2 s -> 5 min
  (10 min when the token is refused).

## Alerts

| What | Alert | Recovery |
|---|---|---|
| a container unhealthy, exited (OOM noted), restarting, paused, dead | at once | when running/healthy |
| a container gone for 3 minutes | once | when it is back |
| restarts | at most once per container per 10 min, with the count | after 10 min without |
| disk over 80%, again over 90% | each once | back under 78% |
| memory: under 10% available, or PSI full avg60 over 20% | once | over 15% and PSI under 5% |
| OOM kills (kernel journal) | at most once per 10 min, with the count | after 10 min without |
| SSH login accepted (journal) | every login: user, IP, method | -- |
| error bursts: `Traceback`, `ERROR`, HTTP 5xx | 3+ lines in a minute starts one; at most once per service per 10 min, with the count and a redacted sample | after 10 min without |
| a stranger writing to the bot | once a day | -- |

Which alerts are open is kept in `/var/lib/moa-ops-bot/state.json`, so a
restart of the bot does not repeat them.

## Restore from a Telegram backup

On the owner's computer, with the private key (`backup-age.key`, the
other half of `OPS_AGE_RECIPIENT`; keep it offline):

```bash
age -d -i backup-age.key -o backup.dump backup-20261107-213000.dump.age
pg_restore -l backup.dump | head            # a pg_dump archive: its table of contents
```

To check it without touching production, restore it into any Postgres 17:
`createdb check && pg_restore --no-owner --no-acl -d check backup.dump`.

Into production -- only after deciding the data now is worse than the dump
(docs/OPERATIONS.md section 4, "Restoring production", is the long form):

```bash
scp backup.dump <admin>@<server>:/tmp/backup.dump
# on the server:
sudo moa-dc stop backend worker                                  # the API is down from here
sudo moa-dc exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | sudo tee /var/backups/myotherapp/before-restore.dump > /dev/null   # keep what is there
sudo docker cp /tmp/backup.dump "$(sudo moa-dc ps -q postgres)":/tmp/restore.dump
sudo moa-dc exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" restored && pg_restore --no-owner --no-acl -U "$POSTGRES_USER" -d restored /tmp/restore.dump'
sudo moa-dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "ALTER DATABASE \"$POSTGRES_DB\" RENAME TO broken_$(date +%Y%m%d)" -c "ALTER DATABASE restored RENAME TO \"$POSTGRES_DB\""'
sudo moa-dc start backend worker
sudo moa-dc exec -T backend alembic current                      # migrate if the dump is older: deploy.sh backend <sha> --migrate
sudo /srv/moa/deploy.sh status
shred -u /tmp/backup.dump; sudo moa-dc exec -T postgres rm /tmp/restore.dump
```

Drop `broken_*` only when sure.

## Tests

```bash
python3 -m pytest ops/server/ops-bot/tests      # pytest is the only dev dependency; no network, Docker or systemd
ruff check ops/server/ops-bot                    # ruff.toml beside this file
```
