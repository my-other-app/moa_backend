# The demo: reset, seed, remove

Three scripts for showing MyOtherApp to clubs with real-looking data, and one
for trying People Like You on the day:

| Script | What it does |
|---|---|
| `python -m scripts.demo.reset` | Wipes the database except the admin accounts and the reference data. |
| `python -m scripts.demo.seed` | Seeds the 8 demo clubs in `data/clubs/`, fully public, plus a main demo user. One club, Respawn Room, is the **showcase**: every section of the clubs dashboard has data. |
| `python -m scripts.demo.remove` | Removes the demo and only the demo, when real clubs arrive. |
| `python -m scripts.demo.live_now` | Puts one showcase event live now, with the main demo user and demo members checked in, so People Like You has people in it (section 7). |
| `python -m scripts.demo.fix_pins` | Moves the venue pins of a demo seeded before each venue had its own point (section 8). |

They run on the server, inside the backend container. Every command below starts there:

```sh
cd /var/www/myotherapp-production
DC="sudo docker-compose -f docker-compose.prod.yml"
```

Always use `$DC exec -T backend ...`. Never `docker-compose run`: it starts a second backend.

## Safety, in short

- **Reset and remove are dry runs by default.** They change something only when given all three of:
  - `--execute`;
  - `--confirm <database name>`, which must match the database exactly (the dry run prints the name);
  - `APP_DEMO_RESET_ALLOWED=1` in the environment.
- **Each runs in one transaction,** checked before the commit. If anything is off, nothing changes.
- **The seed sends nothing.** No email, push, SMS, Discord post or admin alert goes out, and it writes no job for the workers to pick up (`quiet.py`).
- **Fake people stay unreachable afterwards.** Every invented person is at `demo.myotherapp.in`. The mailer and the push sender skip that domain permanently (`APP_MAIL_SUPPRESS_DOMAINS`), so reminders, surveys and billing mails never reach them.
- **No real money.** The seed makes no Razorpay order. Paid tickets and store orders are recorded as paid at the desk, in cash, as the walk-up desk records them, with the ledger lines the desk writes.
  - An upcoming paid event is always sold out, so nobody can buy a ticket.
  - The store is shown only on past events.
- **Nobody from the public can register for a demo event.** By default every upcoming demo event is full: its capacity is set to the registrations the seed made, the main user's included. A real student who found one would otherwise register, get a ticket email and turn up at a venue where nothing is happening. The post-seed checks fail if any upcoming demo event still has a place. Pass `--open-registration` only for a private rehearsal where you want to show the registration flow on a free event.
- **The bucket (S3) is never touched** by reset or remove.

## Before you start

1. Deploy this branch. The backend mounts `./backend`, so a `git pull` puts the scripts in the container. The mail guard (`APP_MAIL_SUPPRESS_DOMAINS`) needs the backend restarted to load, which any deploy does.
2. Pick the main demo user's address. Use one at the demo domain, for example `demo@demo.myotherapp.in`. You sign in with its password; no mail is needed or sent.
   - If you give a real address, it receives whatever a real user would: reminders for its tickets, and so on.
   - The seed then also switches off the journey emails on the showcase's upcoming events.
3. Choose two passwords: one for the main user, and one shared by every demo club's dashboard login.

## 1. Back up

Back up first. The reset cannot be undone except from a backup.

```sh
STAMP=$(date +%Y%m%d-%H%M)
sudo mkdir -p /var/backups/myotherapp
sudo docker exec doth-live-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  | sudo tee /var/backups/myotherapp/before-demo-$STAMP.dump > /dev/null
ls -lh /var/backups/myotherapp/before-demo-$STAMP.dump      # it should be several MB, not 0
$DC exec -T backend python scripts/backup.py                # the daily backup too (copied off-site when that is set up)
```

### Restoring the backup

Stop the API and the worker first so nothing writes during the restore, restore, then start them again:

```sh
$DC stop backend worker
sudo docker exec -i doth-live-postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' \
  < /var/backups/myotherapp/before-demo-$STAMP.dump
$DC start backend worker
```

The API returns 502 for about a minute while it starts.

## 2. Reset: the dry run

```sh
$DC exec -T backend python -m scripts.demo.reset
```

It prints every table with its class and how many rows would go:

- **KEEP:** `alembic_version`, the plans (`plans`, `plan_features`, `plan_limits`), the topics and interests (`interest_categories`, `interests`), the avatar choices (`user_avatars`), `badge_definitions`, `discover_stats`, `scheduled_runs` and `job_workers`.
- **KEEP-FILTERED:**
  - `users`: admin accounts only.
  - What the admins sign in with: their profiles, interests, devices, sessions, notification settings, codes, sign-in attempts, and their audit trail.
  - `event_categories`: the event types the product ships, plus any an admin added.
- **WIPE:** everything else. That includes every club, every non-admin user, events, tickets, payments, the ledger, threads, organisations, notifications and queued jobs.

Check the "would go" column. The last lines print the exact command to execute.

If a table is not classified (one added by a newer migration), the reset refuses and names it. Classify it in `reset.py` first.

## 3. Reset: execute

```sh
$DC exec -T -e APP_DEMO_RESET_ALLOWED=1 backend python -m scripts.demo.reset --execute --confirm <database name>
```

It ends with `Done: N rows removed ... 1 admin account(s) kept.`

If it says `lock_timeout`, something held a table. Run it again.

When search is set up (`APP_SEARCH_URL`), rebuild the index:

```sh
$DC exec -T backend python scripts/search_reindex.py
```

## 4. Seed

```sh
$DC exec -T backend python -m scripts.demo.seed \
  --main-email demo@demo.myotherapp.in --main-password '<main password>' --club-password '<club password>'
```

Notes:

- It takes about 5 minutes. It prints a line per club, then a summary and a **CREDENTIALS** block. Copy that block somewhere safe: it is not written anywhere.
- It refuses to run if any club exists. Reset first, or pass `--force` to seed beside real clubs.
- `--showcase <slug>` picks a different showcase club. `--showcase ''` seeds none.
- `--open-registration` leaves the free upcoming events open to anyone. It is off by default, for the reason under "Safety, in short"; don't use it on production.
- The summary ends with `safety checks passed`. If a safety check fails, the data is already written. Remove it (section 6), fix the cause, and seed again.

### What you get

- **8 clubs.** Each has:
  - a public page, cover, logo, team, partners and site at `{slug}.myotherapp.in`;
  - 5 or 6 events, past and upcoming, with posters, agenda, speakers and questions;
  - registrations with answers, check-ins, certificates, feedback and ratings, a live room, a store and swag;
  - posts with polls, likes and comments, and 860 to 2,560 followers;
  - a ticket and a certificate in its own brand.
- **The main user.** Registered for each club's next event, plus its latest past event (attended, with certificate). Follows every club and has an order handed over.
- **Logins that work at once.** The main user, every club owner and the showcase's committee sign in straight away with no email-verification step: the accounts are marked verified, because their mail is suppressed and a code would never arrive.
- **The showcase (Respawn Room).** Everything above, plus:
  - committee logins with roles: admin, editor, door, and one invite still pending;
  - sign-ins and sessions on Security;
  - doors, a kiosk, volunteers checking people in, and ID cards printed;
  - walk-ups paying cash, and one oversold store order;
  - the game jam as a competition (tracks, teams, submissions, judges, finalists, mentor slots);
  - a quiz and live updates;
  - announcements, tracking links with clicks, journey emails, messages and the delivery log;
  - expenses, sponsorship and grant income, the ID card and programme designs, and closeout reports.

## 5. Verify

Do these in a private window.

1. **Public site:** `https://myotherapp.in`. The demo events show on the home page and under each topic.
   - Open a club, for example `https://myotherapp.in/clubs/respawn-room`, and an event, for example `/events/respawn-cup-season-4-finals`.
   - Open a club site: `https://respawn-room.myotherapp.in`.
2. **API:**
   ```sh
   curl -s https://api.myotherapp.in/api/v1/discover/home | head -c 400
   curl -s https://api.myotherapp.in/api/v1/discover/clubs/respawn-room | head -c 400
   curl -s https://api.myotherapp.in/api/v1/discover/events/respawn-cup-season-4-finals | head -c 400
   ```
3. **Clubs dashboard:** `club-dashboard.myotherapp.in`. Sign in as `respawn-room@demo.myotherapp.in` with the club password and walk every section:
   - Dashboard, Events (open a past one: attendees, check-ins, team and doors, store, swag, competition, live, emails, deliveries, ID cards, certificates, closeout);
   - Finance, Followers, Announcements, Emails, Links, Website, Templates, Access, Security, Threads, Merchandise.
   - Sign in as the admin or door member too: the dashboard shows only what their role allows.
4. **App:** sign in as the main user. Check:
   - My tickets: the upcoming ones, in each club's design;
   - past events with certificates (open one: it renders in the club's design);
   - followed clubs, and notifications.
5. **Nothing was sent:** the summary's quiet-mode line shows no mail or push stopped (the oversold order's alert is expected and was stopped). The `jobs` table holds no demo jobs.

## 6. Remove the demo later

When real clubs are on the platform, remove the demo without touching them:

```sh
$DC exec -T backend python -m scripts.demo.remove                       # the dry run: what would go, table by table
$DC exec -T -e APP_DEMO_RESET_ALLOWED=1 backend python -m scripts.demo.remove --execute --confirm <database name>
```

What counts as the demo:

- **Clubs:** a club whose owner signs in as `<slug>@demo.myotherapp.in`.
- **People:** any account (admins excepted) at `demo.myotherapp.in` or a subdomain.
- **Events:** those clubs' events.
- **Everything hanging off them,** found by following the database's foreign keys.
- **The main demo user:** if its address is elsewhere, add `--main-email <address>`.

The ledger's lines for those clubs go too, and the platform's monthly sums are recomputed. Real rows that merely pointed at a demo row (a report about a demo post) lose the pointer, not themselves. Afterwards, rebuild search as in step 3 if it is set up.

## 7. People Like You, live now

People Like You lists the people checked in at an event who share your
interests, from the event's start to an hour after its end. The seeded
events are all past or upcoming, so to try it, put one live now:

```sh
$DC exec -T backend python -m scripts.demo.live_now --dry-run   # what it would do; writes nothing
$DC exec -T backend python -m scripts.demo.live_now             # do it
```

It ends with a summary: the event, until when People Like You is open, who
was checked in, and who is hidden.

What it does, and only inside the demo:

- **The event.** Respawn Room's "Open Night" (`/events/respawn-room-open-night-live`), made the first time. Every run moves it so it started ten minutes ago and runs four hours. It is free, closed to registration and full, so nobody from the public can join it.
- **The people.** The main demo user (`demo@demo.myotherapp.in`), 8 demo members who share at least one of their interests, and 2 who share none. All are registered and checked in at the desk.
- **The switch.** The member sharing the most has "Show me in People Like You" turned off. They are checked in but never listed, which shows the switch working.

Options:

- `--main-email <address>` names a different main user. It must be at `demo.myotherapp.in`.
- `--club <slug>` picks another demo club.
- `--people <n>` sets how many sharing members to check in (default 8).

Safety:

- **It refuses anything outside the demo:** a club whose owner is not at `demo.myotherapp.in`, or a main user who is not. Members are picked only from that domain.
- **It sends nothing.** It runs in quiet mode, writes no job, and the push sender skips the demo domain anyway.
- **No money.** The event is free; it refuses to touch one with a fee.
- **Run it again whenever you like.** The same event moves to now; the people already on it stay; nobody is added twice.

Then sign in to the app as the main demo user:

- the event's page has the **People like you** card;
- Home's DISCOVER tab says where you are;
- the list shows everyone but the hidden member.

If the summary says `networking: OFF`, the club's plan does not include Attendee networking. Give it a plan that does (Admin → Subscriptions; the seed gives demo clubs the top plan, which has it).

## 8. Fix the venue pins of a demo seeded before

A seed made before each venue had its own point pinned every in-person event within about 450 m of its club, whatever the venue said. Systems Weekend at Vagamon (Idukki) showed 2.9 km from Kochi; it is some 73 km away. The data now gives every venue its own point, and a new seed pins each event there. To correct a demo already seeded, without seeding again:

```sh
$DC exec -T backend python -m scripts.demo.fix_pins --dry-run    # read-only: what would move, and how far
$DC exec -T backend python -m scripts.demo.fix_pins              # changes things: moves them, in one transaction
```

- **Deploy first.** The script and the data arrive with the code (`sync_code`; the backend mounts `./backend`). Nothing needs a restart for it.
- **Only the demo clubs' events.** A club counts when its slug is in `data/clubs/` *and* its contact address or its owner's sign-in is at `demo.myotherapp.in`, so a real club that later takes a removed demo club's slug is never touched.
- **Only the pins the seed made.** A pin moves only while it is still the seed's: within about 450 m of its club, with the seed's map link for it and the seed's venue words. A pin, map link or venue someone changed on the dashboard is left alone, and listed under `left alone:` with the reason.
- **It can be run again.** A pin already on its venue is left as it is: a second run says `0 pin(s) moved` and lists every event as already on its venue.
- The dry run ends with `Dry run: nothing changed.`; the real run with `Done, in one transaction.` A moved event gets the map link of its new point, as a new seed writes it. Search and the "near you" cells follow by themselves; the site's pages within a minute.
- Most events move a few hundred metres, onto their venue's point (the campus clubs' onto their campus). The ones away from their club move the most: Vagamon about 73 km, Kakkayam about 37 km, Kunhimangalam about 25 km, Pazhayangadi about 19 km, Kalamassery about 12 km, Fort Kochi and Mattancherry about 7 km.

## Rehearsing on a laptop

```sh
cd backend
createdb moa_demo && APP_DATABASE_URL=postgresql+asyncpg://localhost/moa_demo APP_DATABASE_URL_SYNC=postgresql://localhost/moa_demo \
  python -c 'import alembic.config; alembic.config.main(argv=["upgrade", "head"])'
python -m scripts.demo.seed --main-email demo@demo.myotherapp.in --main-password DemoPass123! --club-password ClubPass123! \
  --local-bucket /tmp/moa-bucket
```

`--local-bucket` keeps the pictures in a folder instead of S3. Never pass it on the server. `--include-example` also seeds `data/clubs/_example.json`, the tests' small club.

## Files

- `schema.py`: what a club file and `people.json` hold, and the rules (an upcoming paid event must be sold out; a contact address must be at the demo domain).
- `seed.py`: the seed, its safety checks, and how dates snap to the weekday an event's words give it.
- `showcase.py`: the extra data for the showcase club.
- `designs.py`: the ticket, certificate, ID card and programme designs, in a club's brand.
- `images.py`: the logos, covers, posters, product shots, portraits and artwork.
- `quiet.py`: quiet mode.
- `reset.py`, `remove.py`: the wipe and the removal.
- `live_now.py`: an event live now, for People Like You.
- `fix_pins.py`: moves a demo seeded before onto its venues' own points (section 8).
- `local_bucket.py`: a folder standing in for S3, for rehearsals and tests.
- `data/`: the content (see `data/README.md`). Run `python3 data/check.py` after editing it.
