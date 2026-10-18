# Push by FCM topics, and the shared inbox

*Wave 2, branch `feat/w2-push` (backend) and `app/w2-push` (app), 18 October 2026.*

## What changes

A **broadcast** is a push that says the same thing to everyone it reaches,
to an audience the phone itself can name (`app/core/notifications/delivery.py`):

| kind | FCM topic | who |
|---|---|---|
| `club_thread`, `new_event` | `club_{id}` | the club's followers who have not muted it |
| `nearby_event` | `topic_{slug}`, as the condition `(topic_a \|\| …) && !club_{id}` | people with an interest under the event's topics, not the club's followers |
| `platform` | `all` | every device |

Everything else (tickets, payments, reminders, check-ins, certificates,
roles, announcements to an event's registrants, live updates) stays device by
device, and so does any kind nobody has classified.

Before: a post from a club with a lakh of followers was a lakh of rows in
`notifications` and 200 FCM multicasts. After: one row and one FCM request.

| setting | default | what it does |
|---|---|---|
| `APP_PUSH_TOPICS` | off | broadcasts go to their FCM topic (a `push.topic` job, retried) |
| `APP_PUSH_TOPICS_LEGACY` | on | with `PUSH_TOPICS`, devices that have not reported topic support still get broadcasts one by one (`push.devices` jobs); a device never gets both |
| `APP_INBOX_SHARED` | off | a broadcast's inbox line is one shared row read through follows and interests, not a row per recipient |
| `APP_INTEREST_TOPIC_PUSH_MAX_PER_DAY` | 3 | with either switch on, new-event pushes per topic per day (a topic push cannot be capped per person) |

All off: every push and every inbox row exactly as before.

## Duplicates

Every broadcast push carries `push_id` (`club_thread:<uuid>`, `new_event:42`)
in its data, the same for every send of it, and it is also Android's
notification tag and APNs' `apns-collapse-id`:

- the server sends a device one way only: the topic, or (an app without topic
  support) the device itself;
- a job run again sends the same `push_id`: the system tray replaces rather
  than stacks, and the app (foreground) shows it once and opens it once;
- the app reports `topics: 1` with its token only after it has applied its
  topic list for that token, so a device is counted as reached by topic only
  once it is subscribed.

## The safe order

1. **Deploy the backend** with migration `w2_push` (`migrate=true`), every
   switch off. Nothing changes for anyone. The migration adds four empty
   tables and three columns (catalogue only, `lock_timeout` 3 s; if it times
   out on a busy table, run the deploy again).
2. **Release the app** with topic subscriptions and `push_id` de-duplication
   (`app/w2-push`). It subscribes whether or not the server sends by topic,
   and registers its token with `topics: 1` once subscribed.
3. **Watch adoption**: `GET /api/v1/notifications/topics/adoption` (admins)
   gives live devices, those with topic support, the share, and by platform.
4. **`APP_INBOX_SHARED=true`** can go on any time after step 1: the inbox
   answers in the same shape (no app release needed). It takes the biggest
   write off the database.
5. **`APP_PUSH_TOPICS=true`** once most devices report topic support (say
   80–90%); `PUSH_TOPICS_LEGACY` stays on, so older apps are still sent to.
6. **`APP_PUSH_TOPICS_LEGACY=false`** once the older apps are gone (the share
   is ~100%, or a forced update): a broadcast is then exactly one FCM request.

Each is an env line on the server (in the `PROD_ENV_VARIABLES` secret, which
`sync_env` writes over the server's `.env`: check it is current first) and a
recreate of the processes that read it. A `restart` keeps the old
environment, so it must be a recreate, and every recreate builds:

```bash
# On the server, in /var/www/myotherapp-production. Changes things: recreates them.
sudo docker-compose -f docker-compose.prod.yml up -d --build --no-deps backend worker < /dev/null
```

Never a plain `up -d` without `--build`: on 2026-10-17 one recreated the
backend from an image that lacked packages installed by hand, and production
was down for 20 minutes. `--no-deps` keeps Postgres out of it; `< /dev/null`
keeps the command from reading the rest of a script it is run from. Leave
`worker` out while the worker is not running (docs/OPERATIONS.md, section
6: not on a box that cannot hold it). From the Live Deployment workflow,
`sync_env` + `recreate_backend` does the same: it builds (from the cache when
nothing changed), then recreates the backend and a running worker. Turning a
switch off
takes effect for the next broadcast; jobs already written still run.
`INBOX_SHARED` turned off hides the shared lines written meanwhile until it
is on again (they are kept; no rows per recipient are made for them).

## Mutes and switches

- `PUT/DELETE /api/v1/notifications/mutes/clubs/{id}`: still following, no
  pushes or inbox lines from the club.
- `PATCH /api/v1/notifications/settings` `{clubs, topics, platform}`.
- Either drops the topics from `GET /api/v1/notifications/topics` (the list
  and its version, which the app reconciles to) and takes the person's topic
  devices out of them server-side (`push.unsubscribe`), and leaves the person
  out of the device-by-device fan-out too, whatever `PUSH_TOPICS` is.

## iOS and Android

`FirebaseMessaging.subscribeToTopic` works on both. On iOS it needs the APNs
token first (the app waits for it, as it does for the FCM token), and topic
messages reach iOS through APNs like any other, so the APNs key uploaded to
the Firebase project is all it needs. A notification an iPhone receives in
the background is shown by iOS without the app; `apns-collapse-id` is what
keeps a duplicate from showing twice there. FCM allows 2,000 topics per app
install; the list stops at 1,900 (the most recently followed clubs first).

## The inbox, measured

`backend/scripts/bench/inbox_shared.py`, raw in
`backend/scripts/bench/results/push/`. A reader who follows 200 clubs and
three topics, 1 lakh club broadcasts plus 5,000 by topic, 1 lakh people
(each following the big club and four more), 2 lakh other inbox rows. Apple
M2, Postgres 16, 30 runs. `spread`: the broadcasts over 2,000 clubs (10,000
on the reader's clubs). `worst`: all 1 lakh on the reader's 200 clubs.

| read (ms, p50 / p95) | spread | worst | rows per recipient instead |
|---|---|---|---|
| first page (SQL) | 3.1 / 3.9 | 3.0 / 3.2 | 0.3 / 0.8 |
| page by cursor, 1,000 lines down | 3.0 / 3.3 | 3.3 / 3.8 | — |
| page by offset 100 | 5.2 / 5.7 | 11.9 / 18.2 | 0.3 / 0.4 |
| unread count, nothing ever read | 2.3 / 2.5 | 4.4 / 7.4 | 1.2 / 1.3 (spread), 18.2 / 24.9 (worst) |
| unread count after "read all" a week ago | 1.8 / 1.9 | 2.7 / 5.4 | — |
| whole call, first page (SQL + ORM loads, asyncpg) | 5.3 / 7.3 | 6.1 / 8.8 | — |

| write: one post to the big club's 1 lakh followers | per recipient | shared |
|---|---|---|
| inbox write (rolled back) | 3,310–3,570 ms, 1 lakh rows | 1.4–1.5 ms, 2 rows |
| FCM requests | 200 multicasts | 1 send |

Every read is index ranges: one range of
`ix_notification_broadcast_targets_feed` per target, index-only, and the
reader's dismissed and read lines are a hashed `NOT IN` over their own state
rows. The unread count of club lines stops at 999 (`UNREAD_CAP`). The shared
first page costs about 3 ms more than the per-recipient one; it saves seconds
of writes and a lakh rows per broadcast.
