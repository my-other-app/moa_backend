# Scaling MyOtherApp: what it holds today, what breaks first, and the plan

*Scale study, 17 October 2026, on `origin/master` at 76dbb10. The tooling is in
`backend/scripts/bench/` and the raw numbers in
`backend/scripts/bench/results/scale/` (`SUMMARY.md` has every table).*

## The answer

**Will the backend serve 1 lakh clubs, lakhs of active events and 1 crore
users? Not as it is. It can, and the way there is mostly code, not hardware.**

- **The current code and the current single box will not reach 1 lakh clubs.**
  With 1 lakh clubs, 3 lakh upcoming events, 10 lakh past events, 25 lakh
  users and 1.5 crore registrations (9.7 GB of data), the realistic traffic mix
  was served at **5–12 requests a second** with one worker (median **0.8–29
  seconds** as clients went from 10 to 200) and 1–9 with four, where up to a
  third of requests failed. At 15,000 clubs it was 42–45 requests a second. At
  today's size it is 115–190.
- **More uvicorn workers make it worse, not better, until the queries change.**
  At 15,000 clubs, four workers served 21–31 requests a second against 42–45
  for one, and four workers ran Postgres out of connections.
- **The cause is about fifteen queries that read every upcoming event (or every
  club, or every journey email) on every request or every minute.** Their cost
  grows with the size of the data, so they are fine today and fall over first.
  Measured on the 1-lakh-club data, the fixes proposed below take the ones
  tried from 0.1–5 seconds to 0.1–3.4 milliseconds (`s2_whatif.md`).
- **With those fixes, a request costs what it costs today: about 8 ms of Python
  per request, about 120–140 requests a second per worker**, and capacity is
  bought by adding workers and machines. Then:
  - **1 lakh clubs** (about 200–300 requests a second at the evening peak) is
    the Phase 0–1 setup below: Mumbai region, a separate managed Postgres,
    PgBouncer, Redis, CloudFront, a job queue. Roughly **$250–1,200 a month**.
  - **1 crore users** (about 1,500–2,000 requests a second at peak, 10–20 crore
    registrations) needs Phase 2–3: autoscaled API servers, read replicas,
    partitioned registrations, a search engine. Roughly **$4,000–10,000 a
    month**.
- **Three things are broken now, at any size, and should be fixed first:**
  a registration storm on one event stalls the whole API (the ticket PDF is
  rendered on the event loop); the minute email sweep reads every timed email
  ever enabled and overruns its minute at about 33,000 events that have sent
  tickets; and concurrent first registrations copy an event's journey emails
  three to six times, so attendees would get every reminder three to six times.

Every number here was measured on a laptop, not on the EC2 box; read them as
relative and as trends. The traffic figures are assumptions, stated where used.

## How this was measured

**Machine.** Apple M2 (4 performance + 4 efficiency cores), 8 GB of RAM, with
7 GB of swap already in use by other work and, for part of the day, another
engineer's benchmark and test suite on the same machine (runs taken then were
discarded and repeated). Postgres 16 with its defaults: `shared_buffers` 128 MB,
`max_connections` 100, `work_mem` 4 MB. Production runs the `postgres:17.4`
image, and `docker-compose.prod.yml` sets none of these, so production has
the same defaults unless someone edited `postgresql.conf` inside the volume.
Postgres shares the box with the app in production too.

**Data.** `scripts/bench/seed.py`, rewritten for this study: every count is a
flag, rows go in by COPY or by `INSERT … SELECT` from a small plan, triggers and
foreign-key checks are off while loading, and the trigger-kept counters are
written with the rows (checked: no drift). Clubs are spread over 68 Indian
cities by size; events per club, registrations per event and follows per club
are long-tailed.

| | S0 (today's bench) | S1 (10×) | S2 (1 lakh clubs) |
|---|---:|---:|---:|
| clubs | 1,500 | 15,000 | 100,000 |
| upcoming / past events | 12,000 / 25,000 | 120,000 / 250,000 | 300,000 / 1,000,000 |
| users | 60,000 | 600,000 | 2,500,000 |
| registrations | 400,000 | 4,000,000 | 15,000,000 |
| follows | 150,000 | 1,500,000 | 5,000,000 |
| biggest event / median event (registrations) | 5,604 / 2 | 7,600 / 2 | 28,500 / 2 |
| most followed club | 11,053 | 23,852 | 209,602 |
| database size | 0.29 GB | 2.77 GB | 9.67 GB |
| seed time (of which index builds) | 12 s (2 s) | 185 s (34 s) | 42 min (2.4 min) |

The full target (2–3 crore registrations) did not fit the 10 GB limit; S2 is
the largest that did. A registration costs 310 bytes with its indexes, the
same at every size, so storage extrapolates linearly (see Phase 3).

**Tests.**

1. *One request at a time*, in-process (`run.py`): p50/p95/p99 and the SQL
   statements of every discover, search, For You and app-list endpoint, plus
   `EXPLAIN (ANALYZE, BUFFERS)` of the two slowest statements of each.
   `/discover/calendar` and club events paging are not on master yet; `run.py`
   probes them and records them as not offered (404) until they land.
2. *Throughput*: the app under uvicorn as production runs it (`serve.py`,
   access log on), one worker and then four, driven by `load.py` with 10, 50
   and 200 concurrent clients for 40 seconds after a warm-up. The mix: 40% front
   page (half with the browser's point), 25% event search (plain, words, typos,
   filters, distance, popular, later pages), 10% event page, 10% club page,
   10% For You (2,000 signed-in users), 5% the app's lists, from 50,000
   visitor addresses.
3. *Hot rows* (`hot.py`): 500 registrations for one event in 10 seconds (open
   loop), from signed-in users and from the public form, on an event with no
   registrations and on the biggest one; 10,000 follows of one club, 100 at a
   time.
4. *Background work* (`sweeps.py`): the per-minute status clock, the counter
   check each worker runs at boot, and the per-minute timed-email sweep with
   journey emails on 2,000–40,000 events.

Mail never left the machine: sends were replaced by an 80 ms sleep in the
worker thread they already use. wkhtmltopdf is not installed here, so the
ticket PDF was replaced by a blocking sleep of 0 ms or 300 ms (a typical
wkhtmltopdf render is a few hundred milliseconds; production's actual figure
was not measured).

## (a) What the current code and schema handle

### One request at a time

p50 in milliseconds (p95 in `SUMMARY.md`). "Growth" is S2 over S0.

| endpoint | S0 | S1 | S2 | growth |
|---|---:|---:|---:|---:|
| front page, cached (the usual case) | 1.4 | 1.4 | 1.4 | 1× |
| front page, cache miss (once a minute per worker) | 66 | 520 | 2,270 | 34× |
| front page with a point, cache miss | 85 | 689 | 3,170 | 37× |
| `/discover/events` (date order) | 14 | 59 | 244 | 18× |
| search "robotics" | 17 | 76 | 5,260 | 313× |
| search with a typo | 39 | 301 | 19,700 | 500× |
| this weekend | 15 | 29 | 6,340 | 416× |
| popular | 16 | 85 | 2,660 | 168× |
| page 201 (offset 4,800) | 18 | 65 | 7,290 | 417× |
| clubs near Bengaluru | 19 | 250 | 63,300 | 3,390× |
| clubs (popular, search, by interest) | 16–20 | 15–19 | 23–61 | 1–4× |
| club page (the most followed club) | 15 | 17 | 14 | 1× |
| event page | 16 | 20 | 3,410 | 210× |
| For You | 41 | 282 | 4,090 | 99× |
| app list: upcoming / search / interests | 18 / 15 / 20 | 43 / 21 / 77 | 3,510 / 2,430 / 4,820 | 160–240× |
| app list: following | 14 | 17 | 25 | 2× |
| posts feed | 10 | 29 | 43 | 4× |
| sitemap, categories (cached) | 1.1 | 1.1 | 1.2 | 1× |

What it shows: everything keyed to one club, one user or one page of a
followed list stays flat (club page, following, clubs search). Everything that
starts from "all upcoming events" grows with them, and at S2 many of those
queries read the entire 0.8 GB events table on every request (91,000 buffers,
in the plans), which no longer fits in memory next to everything else.

### Throughput

| data | workers | req/s at 10 / 50 / 200 clients | p50 at 10 clients | errors at 200 | what was busy |
|---|---:|---:|---:|---:|---|
| S0 | 1 | 140 / 117 / 113 | 70 ms | 0% | the worker's one core (Python), 100% |
| S0 | 4 | 193 / 166 / 90 | 27 ms | 7.1% | the whole laptop; Postgres out of connections at 200 |
| S1 | 1 | 42 / 43 / 45 | 107 ms | 0% | Postgres (≈4 cores); the worker at 0.5–0.6 of a core |
| S1 | 4 | 21 / 31 / 28 | 228 ms | 2.4% | Postgres; out of connections |
| S2 | 1 | 5 / 10 / 12 | 782 ms | 14% | Postgres, waiting on disk reads |
| S2 | 4 | 1 / 4 / 9 | 12 s | 34% | Postgres; each worker rebuilding its own minute cache |

- **At today's size the app is CPU-bound in Python**: one worker saturates at
  115–140 requests a second (about 8 ms of CPU per request; the event loop
  lags 60–190 ms behind at saturation). Four workers on this laptop gave
  1.4× because the laptop itself ran out of cores; on a dedicated server they
  would scale closer to 4×.
- **From 15,000 clubs the database is the limit, and extra workers hurt**:
  they put more of the heavy queries on Postgres at once, and each worker
  builds its own copy of the minute's cache. At S2 with four workers and 200
  clients a third of all requests failed, nearly all of them front pages
  timing out at 60 s: rebuilding the list of every club's place for "clubs
  near you" takes about 30 seconds at that size (the same query as "clubs
  near Bengaluru" below), and each worker does it every minute.
- **The connection budget is wrong for more than one worker**: each worker may
  open 30 connections (`pool_size=10, max_overflow=20` in `app/db/core.py`);
  Postgres allows 100. Four workers hit `TooManyConnectionsError` in every
  test that pushed them.

### Writes on one hot row

| test | registered before | handled per second | p50 | p95 | errors |
|---|---:|---:|---:|---:|---:|
| 500 registrations in 10 s, empty event, 1 worker (S1, S2) | 0 | 43–45 | 0.05–1.4 s | 2.0–2.2 s | 0 |
| 500 registrations in 10 s, biggest event, 1 worker (S1) | 7,600 | 37 | 3.5 s | 5.3 s | 0 |
| the same at S2 | 28,500 | 34 | 4.1 s | 6.5 s | 0 |
| the same at S1 with 4 workers | 8,600 | 12 | 30 s | 38 s | 29 (connections) |
| 500 in 10 s while 10 people browse, PDF 300 ms on the event loop (S0) | 0 | 4.8 | 38 s | 75 s | 146 (pool timeouts) |
| 10,000 follows of one club, 100 at a time, 1 worker | 24k–60k | 155–165 | 0.5 s | 1–1.8 s | 0–3 |
| the same with 4 workers (S1) | 31k | — | — | 3.2 s | 7,494 (connections) |

- A registration locks the event row (`SELECT … FOR UPDATE`) and keeps it
  through a duplicate check that reads every registration of the event
  (16 ms warm, 153 ms cold at 29,000 registrations), the insert, the trigger's
  counter update and the commit. So one event takes registrations one at a
  time, and the time each holds the lock grows with the event: the lock wait
  alone was 0.7–0.8 s at p95 with one worker and 18 s with four.
- **With a 300 ms ticket PDF on the event loop, a 500-registration burst made
  the worker unusable for everyone**: registrations ran at 4.8 a second, 146
  failed when requests waiting for a connection timed out after 30 s, the
  event loop lagged 259 ms at the median and 0.9 s at worst, and the other
  visitors' throughput halved (99 → 48 requests a second).
- The counters stayed exact in every test (the trigger is correct under
  concurrency). The journey emails did not: a burst on an event with none left
  it with 12–24 journey templates instead of 4.
- Follows hold the club's row only from the counter update to the commit, so
  10,000 follows of one club take about a minute with one worker; with four
  workers three in four failed for want of connections.

### Background work every web worker does

| | S1 | S2 |
|---|---:|---:|
| status clock (`advance_clock`, every minute) | 0.12 s | 3.9 s (812 events moved: the seeded clock had not run for an hour) |
| counter check (`counters.drift`; each worker at boot, then every 6 h) | 3.2 s | 19 s |
| timed-email sweep (every minute), by events with journey emails | 2,000 events: 0.6 s · 10,000: 12–16 s · 40,000: 72 s | 10,000 events: 16–18 s |

The email sweep reads every enabled timed journey email of every event not
deleted, past ones included, with its event and club, and decides in Python
which are due. Any event that has ever sent a free ticket has four journey
emails (two of them timed and on by default), so this list only grows: at
about 33,000 such events the sweep takes longer than its minute, on the event
loop of whichever web worker holds its lock.

## (b) The bottlenecks, in order

1. **Every-upcoming-event queries on every request.** Evidence: at S2 the
   search total, the default page, "this weekend", "popular", the event page's
   "similar events", For You and the app's lists each read most or all of the
   events table per request (91,000–129,000 buffers; 2–20 s). Three causes:
   - *exact totals*: `/discover/events` counts every matching upcoming event
     before choosing the page (`service.py:451`), 2.7–5 s at S2;
   - *date-ordered walks start at the oldest event still marked published*:
     an event whose organiser turned the status clock off stays "published"
     after it ends, and every such event sits at the front of
     `ix_events_shown_start` (the seed gives 3% of past events a clock that is
     off, as the original bench world did; production's share is unknown). At
     S2 every first page reads and throws away 29,500 of them (0.1 s warm,
     seconds cold), and the event page's "similar events" does it again
     (432 ms warm, 3.4 s cold);
   - *For You scores every upcoming event in India* for each user
     (`recommendations.py`: 102,000 candidates at S1, and at S2 a read of the
     whole events table; 0.28 s → 4 s),
     and counts the interests of every event ever held to do it.
2. **"Clubs near you" places clubs without a point of their own by averaging
   their events, for every club, on every request** (`_club_point`, used by
   `search_clubs` and `_club_places`): 19 ms → 250 ms → 63 s. The front page
   rebuilds it once a minute per worker, which is what made the S2 front page
   time out.
3. **The timed-email sweep** (above): unbounded, in the web worker, past its
   minute at about 33,000 events with journey emails. Plus the journey-email
   race (duplicates per event), and four full copies of the club's email
   designs stored per event (about 8 KB an event; gigabytes at lakhs of events).
4. **Hot-event registration**: the event row is locked across work that grows
   with the event (the duplicate check), and the ticket PDF is rendered by
   wkhtmltopdf synchronously on the event loop (`tickets.py:137`, called from
   `ticket_document` in the request's background task), so one hot event slows
   every other request on that worker. One event tops out at 30–45
   registrations a second with one worker, and far fewer with four.
5. **Connections and per-process state**: 30 connections per worker against
   Postgres's 100 means more than three workers fail under load; there is no
   PgBouncer. The minute caches (`discover/cache.py`), the rate limiter
   (`core/rate_limit.py`, in memory) and the counter check's schedule
   (`counters._last_run`) are per process, so four workers do four times the
   cache work and four boot-time scans, and a visitor gets four times the rate
   limit. The limiter's key also trusts the first `X-Forwarded-For` entry,
   which the client sets (nginx appends the real address after it), so anyone
   can step around it.
6. **Postgres untuned and sharing the app's box**: 128 MB of shared buffers in
   front of a 3–10 GB database; at S1 and S2 the busiest waits were
   `DataFileRead` and `LWLock:BufferMapping`. A managed instance sizes this
   to a quarter of its memory by default.
7. **Boot-time and periodic full scans in web workers**: the counter check
   (19 s of full scans at S2) runs in every worker on its first sweep, so every
   deploy starts with four of them; the status clock took 3.9 s a minute at S2.
8. **Distance**: the API is in us-east-1 and the users are in India, about
   280 ms per round trip. A new HTTPS connection costs three round trips
   (≈ 0.85 s) before the first byte, and every sequential call after it
   another 0.28 s. Mumbai is 20–50 ms from most of India. This was not
   measured here; it is arithmetic, and it is the largest single latency
   saving available for users in India today.

## (c) The plan

Traffic assumed: 10% of users active on a day, about 30 API calls each, the
evening peak hour at three times the average, and the public site and
crawlers adding as much again on the discover endpoints. That gives roughly
**200–300 requests a second at 1 lakh clubs (≈ 20 lakh users)** and
**1,500–2,000 at 1 crore users**. A hot event: 10,000 registrations in its
first 10 minutes, with bursts above 100 a second.

Costs are **rough monthly estimates in USD for ap-south-1 on-demand prices**,
before reserved-instance or savings-plan discounts (30–40% off for a year's
commitment) and excluding data transfer beyond what is noted. Check the AWS
calculator before committing.

### Phase 0: now (before marketing, before ~5,000 clubs)

Code first: items 1–8 in section (d) remove the bottlenecks that grow with the
data. Without them no hardware below makes S2 usable; with them, what a
request costs stops growing with the number of clubs and events.

| do | why (evidence) |
|---|---|
| Move to **ap-south-1 (Mumbai)**: API, database, Redis, and the S3 bucket (or CloudFront in front of it). | 280 ms → 20–50 ms per round trip for users in India. |
| **Separate Postgres from the app box**: RDS PostgreSQL 17, db.m7g.large (2 vCPU, 8 GB), gp3 storage, automated backups, single-AZ to start. | The app and the database compete for the same cores (the laptop runs showed it), and RDS sets `shared_buffers` to 25% of memory. |
| **API on one c7g.xlarge (4 vCPU, 8 GB), `WORKERS=3`**, with **PgBouncer** in transaction mode on the same box. | One worker = 115–140 req/s once the queries are fixed. PgBouncer lets workers keep a few connections each; set `pool_size=5, max_overflow=5` per worker and size PgBouncer's server pool to ~40. With transaction pooling, asyncpg's prepared statements need `statement_cache_size=0` (or PgBouncer ≥ 1.21 with `max_prepared_statements`), and `plan_cache_mode` must move from the connection's startup parameters to `ALTER ROLE … SET plan_cache_mode = force_custom_plan`. |
| **Redis (ElastiCache cache.t4g.small)** for the minute caches, the rate limiter (`storage_uri`) and the background schedules. | Process caches multiplied the cache-miss work by the number of workers (S2, 4 workers: a third of requests timed out). |
| **CloudFront** in front of S3 for images, and in front of the API's public `GET /discover/*` with a 30–60 s TTL (cache key: path and query string; requests with `Authorization` bypass it). | The front page, categories and sitemap are already the same for everyone for a minute; the edge can answer them in India without reaching the API. |
| Key the rate limiter on the real client address (nginx `$remote_addr`, or CloudFront's viewer address), not the first `X-Forwarded-For` entry. | The current key is set by the client. |

Cost: **≈ $250–400 a month** (EC2 c7g.xlarge ≈ $90–110; RDS m7g.large single-AZ
≈ $130–150 plus 100 GB gp3 ≈ $15; Redis ≈ $25; CloudFront within its free
tier at first, then ≈ $0.11–0.17 per GB served in India).

**Move to Phase 1 when any of:** peak above 150 requests a second; database
CPU above 60% at peak; an event expected to take more than 5,000
registrations in an hour; or roughly 25,000 clubs / 5 lakh users.

### Phase 1: read replicas and a job queue

| do | why |
|---|---|
| **Two or more API instances behind an ALB** (min 2, so a deploy never takes the site down; production 502s for about a minute on every deploy today). | Headroom and zero-downtime deploys. |
| **RDS Multi-AZ** (m7g.xlarge or r7g.large) plus **one read replica**; route discover, feeds, the sitemap, exports and dashboards' analytics to the replica. | Reads are 95%+ of traffic; the primary keeps its CPU for writes and registrations. |
| **A job queue** (Redis with arq or RQ, or SQS) and a separate worker process for: ticket and certificate PDFs and every email send; the timed-email sweep; the status clock; the counter check; geocoding and map-link backfill; guest merges; imports. | Nothing slow or periodic runs on a web worker's event loop (bottlenecks 3, 4 and 7). |
| A **"hot event" mode**: for events flagged hot (or above N registrations in a minute), registrations go through a queue (or a waiting room) and are confirmed by a single consumer. | One event's row lock serialises its registrations; a queue turns a burst into a steady line instead of timeouts. |

Cost: **≈ $600–1,200 a month**.

**Move to Phase 2 when any of:** registrations above ~10 crore rows or 50 GB;
search p95 above 200 ms after the fixes; peak above 500 requests a second;
database storage growth above 20 GB a month.

### Phase 2: partitioning, search, edge caching, limits

| do | trigger / note |
|---|---|
| **Partition `event_registrations_link`** by hash of `event_id` (16–32 parts), and archive registrations of events closed more than a year (detach old partitions or move them to a cheaper table). | ~10 crore rows. The primary key becomes `(id, event_id)`; the tables that reference a registration by id (deliveries, payments) need the event id with it. Plan it a quarter ahead. |
| **Search**: keep pg_trgm while only upcoming events are searched (fix 13) and p95 stays under 200 ms; then **Meilisearch** (self-hosted, cheap) or **Amazon OpenSearch**, fed from an outbox table by the job queue. | Typos, ranking and facets across lakhs of events are what a search engine is for. |
| **CDN caching of public discover responses** with `Cache-Control: public, s-maxage=30–60, stale-while-revalidate=60`; the per-viewer parts (liked posts) fetched separately. | Takes most anonymous traffic off the API. |
| **Per-user and per-IP rate limits in Redis**, a WAF rule set on CloudFront. | Protects the write paths and the expensive searches. |

Cost: **≈ $1,500–3,500 a month** (RDS r7g.xlarge Multi-AZ ≈ $700–800, a replica
≈ $350–400, 3–4 API instances, Redis ≈ $50–150, search ≈ $50–300, CloudFront).

### Phase 3: 1 crore users

| do | sizing |
|---|---|
| **API autoscaling** (EC2 Auto Scaling or ECS Fargate) behind the ALB across 2–3 availability zones, scaling on CPU (target 60%) and request count. | At ~8 ms of CPU per request, 1,500–2,000 req/s is 12–16 busy cores: 5–8 × c7g.xlarge at peak, 2–3 off-peak. |
| **RDS r7g.2xlarge (64 GB) Multi-AZ** or Aurora PostgreSQL, **2–3 read replicas**, PgBouncer per API instance (or a small pooler tier). | Data at 1 crore users, from the measured bytes per row: 10–20 crore registrations ≈ 30–60 GB, users and profiles ≈ 6 GB, follows (5–10 per user) ≈ 8–17 GB, events ≈ 5–10 GB: **60–120 GB**, of which the working set (upcoming events, recent registrations, active users) should fit in 32–64 GB of RAM. |
| **ElastiCache Redis** Multi-AZ (r7g.large), **OpenSearch** 3 nodes, a dead-letter queue and alerting on the job queue. | |
| **Observability**: metrics, traces and logs with retention (section e). | |

Cost: **≈ $4,000–10,000 a month**, most of it the database tier and its replicas.

## (d) Code-level fixes

Ordered by what they unblock. Expected gains are measured where marked
(✓, from `s2_whatif.md` or the plans) and estimated otherwise. Do not change
behaviour the site depends on without the tests RESULTS.md describes.

1. **Bound every "what is on" query from below.** In `app/api/discover/service.py`,
   `_visible_where` / `_conditions` (and `_public_where` for upcoming), add
   `Events.event_datetime >= now - timedelta(hours=MAX_EVENT_HOURS)` (72 today;
   enforce it as a validation limit on `duration`, or keep the real maximum in
   the cache) next to `_end_expr() >= now`. The date-ordered walks then start
   near today instead of at the oldest event still "published". ✓ event page
   "similar": 432 ms → 1.9 ms; first page of `/discover/events`: 102 ms →
   0.1 ms (S2). Better still, give events that have ended a status the
   partial index leaves out even when their clock is off (or add
   `status_clock` to the index's predicate), so the index holds only what
   can be on.
2. **No exact totals over all upcoming events.** `search_events`
   (`service.py:451, 457`) and `search_clubs` (`:651, :656`): count at most 1,001
   rows (`SELECT count(*) FROM (… LIMIT 1001)`) and return `total` with a
   `total_capped` flag ("1,000+"), or keep exact counts per filter combination
   in the shared cache for a minute. ✓ 2.7–5 s → 3.4 ms at S2.
   `threads_feed` (`:889`) counts every post per request: same treatment.
3. **For You: score a bounded candidate set.** In
   `app/api/events/recommendations.py`, build candidates from indexed paths
   (events of the user's followed and pinned clubs, events with the user's
   interests within the next 30 days, the 200 most popular in the user's
   region, online events) as a `UNION` of small `LIMIT`ed selects, then score
   that set with the same formula. Restrict the `me_shared_interests`
   aggregate (`:101`) to those candidates instead of every event ever held.
   Cache each user's ranked ids in Redis for 5 minutes. Expected 0.28 s (S1)
   / 4 s (S2) → under 30 ms, independent of the number of events.
4. **Store each club's place.** Add `clubs.place_lat` / `place_lng` (its own
   point, else the middle of its in-person events), maintained when an event's
   point or a club's point changes (or recomputed by the job queue nightly),
   with a B-tree on `(place_lat, place_lng)` for verified, not-deleted clubs.
   `search_clubs` (`:639`) and `_club_places` (`:1011`) then use a box query
   like the events' distance search. Expected 63 s (S2) → a few ms; the
   front-page minute rebuild disappears.
5. **Timed-email sweep** (`app/api/emails/service.py:771`): select only
   templates that can be due now, in SQL. Store the due instant on the
   template (`due_at`, set when the template or the event's dates change),
   index `(due_at) WHERE enabled`, select
   `due_at BETWEEN now() - SWEEP_GRACE AND now()`, and keep the existing
   "already sent" check (the message row) for the few rows that returns. Run
   it in the job worker, not a web worker. Expected 72 s at 40,000 events →
   milliseconds at any size.
6. **Journey-email race** (`ensure_event_templates`, `:198`; `ensure_library`,
   `:182`): add unique indexes on `email_templates (event_id, stage) WHERE stage <> 'CUSTOM'`
   and `(club_id, stage) WHERE event_id IS NULL AND stage <> 'CUSTOM'`, and
   insert with `ON CONFLICT DO NOTHING` then re-read. Clean up existing
   duplicates in the migration. Consider copy-on-write: an event points at
   the club's template until the organiser edits it (≈ 8 KB less per event).
7. **Ticket PDF off the event loop.** `ticket_document` (`app/api/events/tickets.py:137`)
   calls `render_ticket_pdf` (wkhtmltopdf in a subprocess) synchronously;
   wrap it in `await asyncio.to_thread(...)` now (one line), and move ticket
   delivery to the job queue in Phase 1. Check the certificate path the same
   way. ✓ (simulated) a 500-registration burst with a 300 ms render on the
   loop: 4.8 registrations a second, 146 failures, 0.9 s loop stalls, browsing
   halved; the same burst with no render time: 37 a second, no failures,
   browsing at 99 req/s. Off the loop, each render still holds a thread (the
   default pool has 12 on an 8-core machine), so the job queue is the real
   fix.
8. **Registration: hold the event's lock for less.** In
   `app/api/events/registration/service.py`:
   - duplicate check (`:266–274`): replace `(user_id = ? OR email = ?)` with two
     lookups, and add `CREATE INDEX CONCURRENTLY ix_registrations_event_email ON event_registrations_link (event_id, lower(email)) WHERE NOT is_deleted`.
     ✓ 15.9 ms warm / 153 ms cold at 29,000 registrations → an index probe;
   - do the checks before taking the lock, and take it last: replace the
     opening `SELECT … FOR UPDATE` (`:189–196`) with an atomic
     `UPDATE events SET admission_seq = admission_seq + 1 WHERE id = :id AND (max_participants IS NULL OR going_count < max_participants) RETURNING …`
     just before the insert (paid events need a paid counter, or keep their
     index-only count, 3.7 ms at 29,000);
   - fold the five badge-metric counts after it (`badges/service.py:24–69`)
     into one grouped query.
   Expected: the lock is held for the insert and the commit only (a few
   milliseconds) and no longer grows with the event, so one event's ceiling
   moves from 30–45 a second to what the workers can process (on the order of
   100–300 a second across a few workers; not measured).
9. **Connection budget** (`app/db/core.py:24–25`): make `pool_size` and
   `max_overflow` settings, and check `WORKERS × (pool_size + max_overflow)`
   against the server's `max_connections` at startup. With PgBouncer, 5 + 5
   per worker.
10. **Shared state in Redis**: the discover cache (`discover/cache.py`, keep the
    same keys and the single-flight build, with a Redis lock), the rate limiter
    (`Limiter(storage_uri=…)`), and `counters._last_run` (or just move the
    counter check to the job worker). Fix the limiter's key
    (`geo.client_ip` takes the first `X-Forwarded-For` hop, which the client
    controls; take the last hop nginx added, or `X-Real-IP`).
11. **Counter check**: run it in the job worker once every 6 hours across all
    processes, not at every worker's first sweep; and compare in slices
    (`event_id` ranges) so it never holds a 19-second full scan.
12. **The cities statistic** (`_cities`, `service.py:919`): `count(DISTINCT lower(trim(location_name)))`
    over every event ever held sorts on disk (`external merge`, 393 ms at S1).
    Store a city on events/clubs or keep the number in a small table updated
    by the job worker.
13. **Search only what is on.** The typo fallback (`_matching`, `service.py:197`)
    and `sort=relevance` search the trigram indexes over every event ever held
    (20 s and 12 s at S2); add the fix-1 bound to each arm. Consider partial
    trigram indexes over upcoming events only, rebuilt by a nightly job, if
    still slow.
14. **Sitemap**: `SITEMAP_MAX = 5000` (`service.py:68`) lists at most 5,000
    events and 5,000 clubs; at 1 lakh clubs most pages are invisible to search
    engines. Serve a sitemap index with pages of 5,000.
15. **Indexes that cost writes and disk and serve no query**: the single-column
    boolean `ix_*_is_deleted` indexes (on registrations at S2: 99 MB, 11 s to
    build), and `ix_event_registrations_link_tracking_link_id` on a mostly null
    column (also 99 MB; make it partial: `WHERE tracking_link_id IS NOT NULL`).
    Check `pg_stat_user_indexes.idx_scan` in production before dropping.
16. **Migrations at size**: index builds on registrations took 10–22 s each at
    S2; plain `CREATE INDEX` blocks writes for that long. Use
    `CREATE INDEX CONCURRENTLY` (outside a transaction in Alembic) for any
    index on events, registrations, follows or users from now on.
17. **Small ones**: `FastAPIDeprecationWarning` for `ORJSONResponse` is printed
    to the log repeatedly (drop `default_response_class` or filter it); add
    `httptools` (uvicorn uses the pure-Python h11 parser without it); the
    uvicorn access log is extra work on every request and nginx already logs
    them (not measured).

## (e) Monitoring and SLOs to add

**Service levels** (server time, measured at the load balancer):

| | target |
|---|---|
| public discover GETs | p95 < 300 ms, p99 < 1 s |
| event page, club page | p95 < 200 ms |
| For You, app lists | p95 < 500 ms |
| registration | p95 < 1 s; 99.9% succeed (excluding "full" and "already registered") |
| 5xx | < 0.5% of requests over 5 minutes |
| availability | 99.9% a month (≈ 43 minutes) |
| background | every sweep finishes within its interval; email queue age < 2 min |

**Measure** (what the load tests needed and production cannot show today):

- per route: requests, latency histogram, status codes (a Prometheus
  middleware or CloudWatch embedded metrics; `ProcessingTimeMiddleware` already
  times each request);
- per worker: event-loop lag (what `serve.py` measures: a 50 ms sleep's
  lateness), DB pool checked-out, overflow and wait time;
- Postgres: `pg_stat_statements` (turn it on; the top statements by total
  time are the next bottleneck list), connections against `max_connections`,
  CPU, buffer cache hit ratio (keep above 99%), `DataFileRead` waits,
  replication lag, long transactions, autovacuum activity and bloat;
  `log_min_duration_statement = 500ms`, `log_lock_waits = on`;
- each sweep's duration and rows (the clock, the email sweep, the counter
  check), and the counter check's "found" (anything above 0 is a bug);
- the job queue's depth, age and failures; Redis memory and hit ratio;
  CloudFront hit ratio.

**Alert on:** an SLO burning for 10 minutes; 5xx above 1% for 5 minutes;
DB connections above 80% of the maximum; replica lag above 30 s; a sweep
longer than its interval; disk above 75%; any `TooManyConnectionsError` or
pool timeout in the logs.

**Keep measuring:** run `seed.py --scale 10` and `load.py` before any release
that touches discover, For You, registration or the sweeps; the S2 data
(`--clubs 100000 --upcoming 300000 --past 1000000 --users 2500000 --registrations 15000000 --follows 5000000`)
before each phase change.

## Reproduce

```
cd backend
createdb moa_s2
BENCH_DB=moa_s2 python -m scripts.bench.seed --clubs 100000 --upcoming 300000 --past 1000000 \
    --users 2500000 --registrations 15000000 --follows 5000000 --threads 300000 --likes 2000000 \
    --comments 600000 --sizes-out scripts/bench/results/scale/s2_seed.json        # 42 min, 9.7 GB
BENCH_DB=moa_s2 python -m scripts.bench.run --label s2 --freeze --n 300 --budget 15 --min 5 \
    --out scripts/bench/results/scale/s2_run.json --explain plans.txt
BENCH_DB=moa_s2 python -m scripts.bench.sweeps --templates 10000 --out scripts/bench/results/scale/s2_sweeps.json
BENCH_DB=moa_s2 python -m scripts.bench.serve --workers 1 --port 8077 --stats-dir /tmp/stats > server.log 2>&1 &
BENCH_DB=moa_s2 python -m scripts.bench.load --concurrency 10,50,200 --duration 40 --warmup 20 \
    --stats-dir /tmp/stats --server-pid $! --workers 1 --out scripts/bench/results/scale/s2_load_w1.json
BENCH_DB=moa_s2 python -m scripts.bench.hot event --n 500 --seconds 10 --event big --stats-dir /tmp/stats \
    --out scripts/bench/results/scale/s2_hot.json
BENCH_DB=moa_s2 python -m scripts.bench.hot follow --n 10000 --concurrency 100 --out scripts/bench/results/scale/s2_hot.json
python -m scripts.bench.scale_report > scripts/bench/results/scale/SUMMARY.md
dropdb moa_s2
```

S0 is `seed.py` with no flags, S1 is `--scale 10`. Stopping a multi-worker
`serve.py`: kill the processes listening on its port, not only the parent
(uvicorn's spawned workers outlive it on macOS).
