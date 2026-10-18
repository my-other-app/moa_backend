# Discover at scale: measurements

What the public discover API (the site at myotherapp.in) and the app's event
feeds cost at the size the owner expects (10,000+ events, 1,000+ active
clubs), before and after the branch `perf/discover-scale`. Every number here
was measured on the machine below; the commands to measure them again are at
the end.

## The machine and the data

- Apple M2, 8 GB, macOS. Postgres 16.10 (Homebrew) on the same machine;
  production runs 17.4. Python 3.11. The app runs in-process behind
  `httpx.ASGITransport`, one request at a time, with nothing else running.
- The app's SQL statement log (`app/db/core.py` logs every statement and its
  parameters at INFO, as production does) stayed on in every run.
- `scripts/bench/seed.py` fills the database through the models' tables, in
  bulk:

  | table | rows | spread |
  |---|---:|---|
  | clubs | 1,500 | 20 Indian cities, weighted by size; 80% with their own point, the rest placed only by their events; 95% verified, 1% deleted; 85% with a logo |
  | events | 37,000 | 12,000 to come (most within six weeks, 2% under way) and 25,000 over the last three years; 15% online, 10% in person without a point; 30% paid; 60% capped; 4% drafts; 3% of past ones still "published" (clock off); 2% deleted; 90% with a poster |
  | users | 61,500 | 60,000 app users with profiles and 0–6 interests, plus the club owners |
  | registrations | 400,020 | long-tailed: 40 events with over 1,000 (the largest 5,716), median 3; 5% cancelled |
  | follows | 150,000 | long-tailed: the most followed club has ~11,000 |
  | posts | 20,000 | 120,000 likes, 40,000 comments, 10% polls |

  Of the 12,000 events to come, 10,357 can be shown (published or live, by a
  verified club, not over).
- Per endpoint: a few warm-up requests, then one request after another until
  300 are done or 20 seconds pass (at least 3–5). The old code took up to
  200 s a request, so its columns rest on 3–10 requests: read them as "at
  least this slow". *stmts* is every statement the app sent for one request,
  counted on the engine.
- Three runs: **before** (origin/master at 43f0d71), **signing fixed** (the
  same code with only `app/core/storage/fields.py` changed; it shows how much
  was image signing and how much was the algorithms), and **after** (this
  branch, migrated, with the startup's `gc.freeze()` applied). "(cold)" rows
  empty the minute cache before every request: they time what a cache miss
  costs, which each process pays once a minute per key.

## Results

| endpoint | before p50 / p95 ms | stmts | signing fixed p50 / p95 ms | stmts | after p50 / p95 ms | stmts |
|---|---:|---:|---:|---:|---:|---:|
| home | 90 s / 96 s (n=3) | 41 | 9.0 s / 9.3 s (n=5) | 41 | 1.4 / 1.5 (n=300) | 0 |
| home (cold) | — | — | — | — | 68.9 / 81.6 (n=286) | 17 |
| home lat/lng | 88 s / 102 s (n=3) | 42 | 8.7 s / 8.9 s (n=5) | 42 | 1.7 / 1.9 (n=300) | 0 |
| home lat/lng (cold) | — | — | — | — | 88.3 / 103 (n=225) | 26 |
| events | 77 s / 84 s (n=3) | 4 | 8.3 s / 9.1 s (n=5) | 4 | 14.7 / 15.7 (n=300) | 3 |
| events q | 2.1 s / 2.6 s (n=10) | 4 | 54.4 / 65.1 (n=300) | 4 | 15.2 / 16.5 (n=300) | 3 |
| events q short | 28 s / 29 s (n=3) | 4 | 244 / 437 (n=66) | 4 | 15.7 / 16.7 (n=300) | 3 |
| events q typo | 40.2 / 47.1 (n=300) | 3 | 45.3 / 49.4 (n=300) | 3 | 34.7 / 56.5 (n=300) | 5 |
| events q relevance | not offered (422) | — | not offered (422) | — | 24.2 / 27.7 (n=300) | 3 |
| events this_weekend | 6.1 s / 6.2 s (n=4) | 4 | 66.0 / 211 (n=251) | 4 | 13.4 / 16.0 (n=300) | 3 |
| events category | 7.2 s / 7.8 s (n=3) | 4 | 74.0 / 227 (n=218) | 4 | 15.8 / 18.8 (n=300) | 3 |
| events online | 11 s / 11 s (n=3) | 4 | 105 / 272 (n=145) | 4 | 14.8 / 16.2 (n=300) | 3 |
| events free | 54 s / 54 s (n=3) | 4 | 5.9 s / 6.0 s (n=5) | 4 | 14.8 / 15.9 (n=300) | 3 |
| events lat/lng (radius 50, date) | 74 s / 78 s (n=3) | 4 | 8.4 s / 8.7 s (n=5) | 4 | 15.1 / 16.2 (n=300) | 3 |
| events distance r=50 | 78 s / 79 s (n=3) | 4 | 8.4 s / 8.7 s (n=5) | 4 | 24.9 / 27.4 (n=300) | 4 |
| events distance r=300 | 76 s / 77 s (n=3) | 4 | 8.5 s / 8.7 s (n=5) | 4 | 24.5 / 26.6 (n=300) | 4–6 |
| events popular | 77 s / 77 s (n=3) | 4 | 8.5 s / 8.9 s (n=5) | 4 | 16.0 / 18.3 (n=300) | 3 |
| events offset=480 | 76 s / 79 s (n=3) | 4 | 8.4 s / 8.4 s (n=5) | 4 | 15.2 / 20.0 (n=300) | 3 |
| clubs | 6.1 s / 6.3 s (n=4) | 7 | 83.0 / 249 (n=202) | 7 | 18.5 / 20.2 (n=300) | 6 |
| clubs q | 138 / 295 (n=130) | 7 | 41.9 / 47.8 (n=300) | 7 | 24.2 / 26.2 (n=300) | 6 |
| clubs q typo | 6.2 / 10.4 (n=300) | 2 | 11.3 / 13.3 (n=300) | 2 | 21.7 / 25.0 (n=300) | 8 |
| clubs interest | 411 / 588 (n=44) | 7 | 39.4 / 44.4 (n=300) | 7 | 18.0 / 22.2 (n=300) | 6 |
| clubs nearby | 6.1 s / 6.7 s (n=4) | 7 | 62.4 / 214 (n=267) | 7 | 19.8 / 22.3 (n=300) | 6 |
| clubs popular | 6.1 s / 6.7 s (n=4) | 7 | 83.3 / 250 (n=200) | 7 | 18.5 / 20.1 (n=300) | 6 |
| club page (big) | 109 / 156 (n=169) | 14 | 52.3 / 60.3 (n=300) | 14 | 17.8 / 20.0 (n=300) | 14 |
| event page | 108 / 162 (n=169) | 11 | 37.5 / 43.3 (n=300) | 11 | 24.2 / 27.1 (n=300) | 11 |
| threads | 132 / 289 (n=133) | 4 | 14.8 / 17.6 (n=300) | 4 | 10.7 / 12.4 (n=300) | 4 |
| categories | 16.4 / 28.3 (n=300) | 2 | 15.8 / 17.9 (n=300) | 2 | 1.1 / 1.4 (n=300) | 0 |
| sitemap | 49.8 / 192 (n=275) | 2 | 46.7 / 186 (n=295) | 2 | 1.0 / 1.3 (n=300) | 0 |
| sitemap (cold) | — | — | — | — | 57.3 / 60.7 (n=300) | 2 |
| categories (cold) | — | — | — | — | 8.1 / 8.6 (n=300) | 2 |
| app for-you | 203 s / 204 s (n=3) | 2 | — | — | 36.3 / 43.1 (n=300) | 3 |
| app list upcoming | 116 / 147 (n=160) | 3 | 41.5 / 44.3 (n=300) | 3 | 17.2 / 19.4 (n=300) | 4 |
| app list search | 130 / 154 (n=146) | 3 | 29.2 / 32.7 (n=300) | 3 | 17.1 / 18.3 (n=300) | 4 |
| app list following | 112 / 217 (n=162) | 3 | 26.7 / 31.1 (n=300) | 3 | 14.0 / 14.8 (n=300) | 4 |
| app list interests | fails (500) | — | fails (500) | — | 19.9 / 21.8 (n=300) | 4 |

Notes on the table:

- **stmts 0** on a warm front page, categories or sitemap: served from the
  minute cache. The cold rows are what a miss costs; each process pays it at
  most once a minute per key (per 0.25° cell for a front page with a place).
- **events q typo / clubs q typo**: before, a typo found nothing, cheaply;
  now it returns the near matches (5 and 8 statements: the substring count,
  the threshold, the similarity search, the page).
- **app for-you** was not rerun with signing fixed: its 203 s were all SQL.
- **app list interests** failed before (a 500; see below).
- **events distance r=300**: 4–6 statements because a page that runs past
  the placed events also reads the unplaced ones.
- The "before" run's later rows (club page onward) overlapped a test-suite
  run on the same machine, so they are somewhat high; the "signing fixed"
  column, run alone on the same SQL, is the cleaner baseline for them.

Every endpoint is now under 60 ms at p95 (most under 30 ms); the front page
takes under 2 ms warm and 82–103 ms p95 with nothing cached; and the number
of statements a request runs no longer depends on how many events there are
(`test_statements_per_request_do_not_grow_with_the_number_of_events`
checks 5 against 60 events).

The status clock (`closeout.service.advance_clock`, run every minute on the
web worker by the closeout sweep):

| | before | signing fixed | after |
|---|---:|---:|---:|
| one sweep of `advance_clock` | 34.1 s, 616 statements | 8.3 s, 616 statements | 26 ms, 3 statements (179 ms on the first, cold run) |

It read every event on the clock (12,700) and queried each closed one's
retention job. It now reads only the events whose status can move this
minute (started; past the end and the grace; closed and scrubbed or a month
on) and their jobs in one query. The same rules decide; a test checks each
case against them. (The sweep moved 47 events in the "before" runs and 49
later: time had passed.)

## Where the time went

1. **Image signing.** Reading a row with an image made a new boto3 client
   for each image column (≈4 ms: it loads and validates the S3 service
   model) and then signed four or five URLs (≈70 µs each). The front page
   read every upcoming event and every club, so 90 s of its 90 s was this.
   Fixing only this ("signing fixed") brings the big lists to 6–9 s.
2. **Everything read, then sorted in Python.** Every list read all 10,357
   showable events (or all 1,419 clubs) as ORM rows with their club and
   category, sorted them in Python and sliced the page. The plan for
   `/discover/events` sorted 10,312 rows of 1.7 kB each, spilling to disk
   (`Sort Method: external merge  Disk: 5848kB`), then counted registrations
   for all 10,222 ids at once (an `IN` list of 10,222 parameters, each
   logged).
3. **Correlated subqueries per event.** For You scored every upcoming event
   with six correlated subqueries; two of them read the user's registrations
   with no index on `user_id`: a sequential scan of 400,000 rows per
   candidate. 203 s a request.
4. **Generic plans.** asyncpg prepares every statement; after five runs
   Postgres may switch a prepared statement to one plan made without the
   parameters' values. For a word search that plan went through the trigram
   index with an unknown pattern; for "ai" (no trigram to look up) it read
   all 36,000 entries: 94 ms against 10 ms planned with the value.
5. **The status clock** read every event on the clock (12,700 rows) each
   minute and ran a query per closed one: 34 s a minute before, 8 s with
   signing fixed, all on the event loop that serves requests.
6. **Python's collector.** With the above gone, the remaining tail (p95
   120–160 ms on requests that allocate a lot: the sitemap's 6,500 entries,
   the front page with nothing cached) was full garbage collections walking
   the ~740,000 objects import builds. `gc.freeze()` at startup takes them
   out of its reach: the sitemap with nothing cached went from p95 160 ms to
   51–61 ms, the cold front page from p99 120 ms to 85–93 ms.

## What changed, and why

### Pages are chosen in SQL, on narrow rows

Each list is a count and a page. The page is chosen by a subquery that
carries only the id and the sort keys through the sort, with LIMIT/OFFSET;
only the page's rows are then read with their club and category
(`service._page`). Sorting 10,000 narrow rows instead of wide ones took
"popular" from 35 ms to 16 ms on its own. Default date order walks
`ix_events_shown_start` (a partial index on what can be shown) in order and
stops after the page.

### Popularity: counters kept by triggers

"Popular" needs a going count for every candidate, not just the page. On
this data, the first page sorted by popularity (EXPLAIN ANALYZE, warm):

| first page by popularity | warm |
|---|---:|
| events: a grouped count of all 400,000 registrations, joined | 78 ms |
| events: a count per candidate (LATERAL, on the `(event_id, user_id, is_deleted)` index) | 21 ms |
| events: the trigger-kept counter | 6.7 ms |
| clubs: a grouped count of all 150,000 follows, joined | 20 ms |
| clubs: the trigger-kept counter | 0.45 ms |

(b), a ranking cached for a few minutes, would be fast but stale and would
not page consistently with the filters. A counter is exact and costs one
row update per registration.

So `events.going_count` and `clubs.follower_count`, each kept by a trigger
on the rows it counts (registrations not deleted; follows that follow and are
not deleted). A trigger and not application code because registrations are
written from the public form, the app, the desk, the kiosk, imports, the
guest merge (including the new merge into verified-address accounts),
refunds and the retention scrub, and raw SQL in admin tools: a trigger sees
every one, in the writer's own transaction. Registration already locks the
event row (`SELECT … FOR UPDATE`), so the trigger's update adds no new
waiting. Cards show the same counters, read as plain columns for the page's
ids (a primary-key read each): counting rows for a page of popular events
read thousands of rows each.

`counters.reconcile` finds any counter that disagrees with its rows and
recounts it under the row's lock (a write in flight either lands first and
is counted, or waits and adds itself). It runs every six hours from the
closeout sweep and on demand at `POST /admin/discover/reconcile-counters`; it
should only ever find something after a write with triggers off (a
data-only restore with `--disable-triggers`). Tests drive every write path
(the app's register and remove-from-list endpoints, follow/unfollow, the
guest merge both ways, ORM, bulk and raw SQL inserts, updates, moves and
deletes) and check every counter against its rows.

### Distance: a box on an index, then the same haversine

`geo.bbox` is the smallest latitude/longitude box holding the circle on the
sphere `geo.km` measures: latitude ± r/R, longitude ± asin(sin(r/R)/cos φ)
(the d/cos φ approximation falls short away from the equator), two
longitude ranges across the antimeridian, all longitudes when a pole is
inside, nothing when the circle covers the sphere; padded by 1e-7°. A test
throws points at random bearings around the edge of circles at the poles,
across the antimeridian and up to 19,000 km and checks every one inside the
circle is inside the box. `ix_events_shown_point` (lat, lng) finds the box;
`geo.km_sql` then computes the same haversine as `geo.km`, step for step
(equal to 1e-9 km in a test), to filter and order. `sort=distance` pages
through the placed events nearest first (box, index) and then the rest
(online, no point) by date, as one list.

The cube/earthdistance extensions with a GiST index were measured on the
"near you" pool query (in-person events within 70 km of Bengaluru, 1,078
candidates): 1.8 ms against 3.8 ms for the B-tree box. That query runs once
per 0.25° cell per minute, so two milliseconds did not earn two extensions
and a second representation of every point. Worth revisiting at ten times
the events.

Clubs without their own point are placed by the middle of their in-person
events, as before, computed in SQL by a lateral aggregate that only runs for
those clubs (`service._club_point`, served by `ix_events_club_start`): about
10 ms for all 1,419 clubs. The front page reads that list once a minute; the
clubs search runs it per request (19 ms p50 in all). A maintained column
would need a trigger on every event write that touches a point; not worth it
at 1,500 clubs.

### Words: substrings as before; trigrams for typos

The `q` match is what it was: case-blind substrings of the event's name or
place, its club's name or its category's name, with `%`, `_` and `\` taken
literally. The OR across tables became one per-row filter on what is on,
with the club and category sides as hashed lookups of the few matching ids.

Trigram (pg_trgm) over tsvector: the queries are fragments of short names
and places ("hack", "robotics lab", "kochi") and typos, which trigrams answer
and a full-text index does not (it matches whole lexemes, and stems names).
Where the trigram GIN indexes pay is not the plain substring search: matching
through them over every event, past ones included, measured 97 ms for "ai"
against 16 ms filtering the 10,357 showable events, because what is on is
already a small, indexed set. They serve:

- the typo fallback: a search with 3+ characters that finds nothing by
  substring tries `word_similarity` (pg_trgm's `%>`, threshold 0.5, set per
  transaction: the default 0.6 misses "robtics" for "robotics" at 0.55,
  while 0.5 still leaves out "Robots" at 0.38). As a filter `%>` costs a few
  microseconds a row (36 ms over what is on); through the indexes it finds
  the candidates first;
- the app's `/events/list?search=`, which searches every event;
- `sort=relevance` (new, opt-in): best word similarity of the name, then the
  club's name, place and category, weighted 1, 0.8, 0.6, 0.5; date and id
  break ties.

### The front page: a minute's cache that cannot answer differently

Each section is its own LIMITed query. What is the same for everyone (this
week, online, popular, categories, the most followed clubs, the stats, the
newest posts without the reader's likes or votes) is kept for the minute in a
process-local dict with a 2,048-entry cap (`discover/cache.py`; there is no
Redis in this stack). Keys carry the minute, so nothing is more than a
minute old and a newly published event is on the front page within the
minute. Concurrent misses on one key wait for the first.

"Near you" and the nearest clubs depend on the exact point, and a cache per
0.25° cell answering with the cell's centre would change both. So each cell
keeps a pool that is provably enough for any point in it:

- events: everything within 50 km + *reach* of the centre (*reach* is the
  farthest corner, ~19.5 km), soonest first, stopping at the 12th that is
  within 50 km − *reach* (that one is near every point in the cell, so nothing
  later can be anyone's first 12);
- clubs: everything within R + 2·*reach* of the centre, where R is the
  distance to the centre's 8th nearest club.

The visitor's own point then picks from the pool with `geo.km`, exactly as
before. A test compares the cached and uncached front page with the old
computation at 34 points, cell corners included. The reader's likes and
votes are read per request (two queries) and laid over copies.

Categories, top interests and the sitemap are kept per minute; the sitemap's
body is serialised once a minute. The "cities" stat (distinct places of every
event ever held) is kept for ten minutes.

### For You and the app's lists

For You keeps its formula. The user's side (interests, clubs, history) is
read once as small grouped subqueries joined to the candidates by key;
popularity reads the going counter; ranking runs on narrow rows; the page is
read in full. A test compares ids and scores with the old query for eight
users and two pages each.

`/events/list` chooses its page on ids, then reads the page. Its interest
filter was a join plus `DISTINCT` over every column, which Postgres refuses
for `json[]`: a 500 whenever the app filtered by interest (before: "500" in
the table). It is a semi-join now.

### Plans made with the parameters

`plan_cache_mode = force_custom_plan` on the app's connections: every
statement is planned with its parameters, as the first five runs of each
already were. Planning these queries takes 0.1–0.5 ms. Partial-index
predicates are also written into the SQL as constants
(`service._in`), so they hold even under a generic plan.

## Indexes and what uses them

From `results/after_indexes.json` (every statement of one request per
endpoint, under EXPLAIN):

| index | definition | used by |
|---|---|---|
| `ix_events_shown_start` | events (event_datetime, id) WHERE NOT is_deleted AND status IN ('published','live') | every date-ordered list: home, events (any filter), sitemap |
| `ix_events_ends_at` | events (end instant: `timezone('UTC', timezone('UTC', event_datetime) + duration * 1 hour)`) WHERE NOT is_deleted | every count of what is on, popular, distance, For You, the app's upcoming list, categories |
| `ix_events_club_start` | events (club_id, event_datetime) WHERE NOT is_deleted | club page, more from the club, clubs' points and upcoming counts, the app's following list |
| `ix_events_category_start` | events (category_id, event_datetime) WHERE NOT is_deleted | category filter, similar events, the app's search |
| `ix_events_shown_point` | events (lat, lng) WHERE shown AND lat/lng NOT NULL | near you, sort=distance |
| `ix_events_name_trgm`, `ix_events_location_trgm` | GIN (… gin_trgm_ops) WHERE NOT is_deleted | typo fallback; the app's search (name) |
| `ix_clubs_name_trgm`, `ix_clubs_tagline_trgm`, `ix_clubs_location_trgm` | GIN (… gin_trgm_ops) WHERE NOT is_deleted | club names in event search, typo fallback for clubs |
| `ix_registrations_user` | event_registrations_link (user_id) WHERE NOT is_deleted | For You, all the app's lists |
| `ix_club_users_user` | club_users_link (user_id) WHERE NOT is_deleted | For You, the app's following list |
| `ix_event_interests_event`, `ix_event_interests_interest` | event_interests_link (event_id) / (interest_id, event_id) WHERE NOT is_deleted | event page; For You, the app's interest filter |
| `ix_club_interests_club`, `ix_club_interests_interest` | clubs_interests_link (club_id) / (interest_id, club_id) WHERE NOT is_deleted | club cards; clubs by interest |
| `ix_user_interests_user`, `ix_user_profiles_user` | (user_id) | For You; the app's lists |
| `ix_club_threads_newest` | club_threads (created_at DESC, id) WHERE NOT is_deleted | front page posts, the threads feed |
| `ix_events_span` (`discovery_polish`) | events GiST (tstzrange(start, GREATEST(start, end), '[]')) | the calendar; `from`/`to` on the event search (see the last section) |

Extension: `pg_trgm`.

Added and then dropped because no plan used them: registrations
`(event_id) WHERE NOT is_deleted` and follows `(club_id) WHERE is_following
AND NOT is_deleted` (counts now come from the counters; the existing
`(event_id, user_id, is_deleted)` unique index serves the rest), and partial
indexes for the map-link backfill (it now looks once a minute, after the
response).

## Plans (excerpts)

**`/discover/events`, before** (the read of every showable event, sorted
in the database only to be sorted again in Python, then a count over every
id):

```
Gather Merge  (actual time=27.599..33.410 rows=10312 loops=1)
  ->  Sort  (actual time=25.253..26.505 rows=5156 loops=2)
        Sort Key: events.event_datetime, events.id
        Sort Method: external merge  Disk: 5848kB
        ->  Hash Left Join ... ->  Parallel Seq Scan on events (rows=5546 loops=2)
Execution Time: 35.856 ms        -- 97.9 ms in the app, then 8 s building 10,312 rows and cards

Finalize HashAggregate  (rows=10222)
  ->  Parallel Seq Scan on event_registrations_link
        Filter: ... (event_id = ANY ('{4407,2116,6551,…10,222 ids…}'))
        Rows Removed by Filter: 95408
Execution Time: 27.636 ms        -- 87.5 ms in the app
```

**`/discover/events`, after**: the total, then the page walked in date order
until 24 are found, then the 24 read in full:

```
Aggregate  (actual time=5.937..5.938 rows=1)
  ->  Hash Join  (rows=10300)
        ->  Bitmap Heap Scan on events  (rows=11079)
              ->  Bitmap Index Scan on ix_events_ends_at  (rows=11624)
Execution Time: 5.954 ms

Incremental Sort  (rows=24)
  ->  Nested Loop ... ->  Limit  (rows=24)
        ->  Index Scan using ix_events_shown_start on events events_1  (rows=28)
              Rows Removed by Filter: 815          -- past events still "published", clock off
        ->  Index Scan using events_pkey on events  (loops=24)
Execution Time: 0.712 ms
```

**`sort=distance`, 50 km around Bengaluru, after**: the box on the point
index, the exact haversine on what it finds, the nearest 24:

```
Limit  (rows=24)
  ->  Sort  Sort Key: ('12742.0176' * asin(least(1, sqrt(power(sin((radians(lat) - …) * 0.5), 2) + …))))
        ->  Bitmap Heap Scan on events events_1  (rows=1073)
              ->  Bitmap Index Scan on ix_events_shown_point  (rows=1168)
                    Index Cond: ((lat >= 12.5219…) AND (lat <= 13.4213…) AND (lng >= 77.1332…) AND (lng <= 78.0560…))
Execution Time: 3.320 ms
```

**A generic plan, before `force_custom_plan`** (the count for `q=ai`, run
as a prepared statement after five executions):

```
generic:  Bitmap Index Scan on ix_events_name_trgm  (rows=36234)   -- no trigram in '%ai%': every entry
          Bitmap Index Scan on ix_events_location_trgm  (rows=36234)
          Execution Time: 98.928 ms
custom:   Bitmap Index Scan on ix_events_ends_at  (rows=11601), then the filter
          Execution Time: 10.338 ms
```

**For You, after** (22 ms: the 11,300 candidates from `ix_events_ends_at`,
anti-joined with the user's registrations and hidden clubs
(`ix_registrations_user`, `ix_club_users_user`), hash-joined with the user's
grouped interests (`ix_event_interests_interest`), clubs and history, scored
and top-N sorted on narrow rows). Before, each of the 11,300 candidates ran
six correlated subqueries, two of them sequential scans of the 400,000
registrations: 201 s of SQL.

Full plans: `--explain FILE` writes them (the two slowest statements of each
endpoint, or every statement); they are not kept in the repository.

## The migration on this data

`discover_perf` on the benchmark's 37,000 events, 400,000 registrations and
150,000 follows: 3.45 s in all (columns, triggers, the counter fill and 19
indexes, one transaction). Plain `CREATE INDEX`: each blocks writes to its
table while it builds; at production's size, milliseconds.

## What behaves differently

- A search with 3+ characters that finds nothing by substring now returns
  near matches (typos) instead of nothing. Anything that matched before
  still matches, in the same order.
- `sort=relevance` is new and opt-in; the default is still date.
- The front page's shared parts, categories, top interests and the sitemap
  can be up to a minute old (a card's "status" and "going" included); the
  "cities" stat up to ten minutes.
- Image URLs are reused for 30 minutes (each still valid for at least 30
  more), so an image keeps its address across page views.
- The map-link backfill looks for rows once a minute, after the response,
  rather than on every front page.
- `/events/list`: the interest filter works (it failed); events at the same
  instant now come in id order on every request.
- Cards' "going" and "followers" come from the counters (equal to the rows).

## Reproduce

```
createdb moa_bench
BENCH_DB=moa_bench python -m scripts.bench.seed          # schema by alembic, then ~45 s of data
BENCH_DB=moa_bench python -m scripts.bench.run --label after --freeze --out scripts/bench/results/after.json
BENCH_DB=moa_bench python -m scripts.bench.run --warmup 2 --min 3 --n 5 --budget 1 --explain plans.txt --indexes indexes.json
BENCH_DB=moa_bench python -m scripts.bench.clock
python -m scripts.bench.report scripts/bench/results/before.json scripts/bench/results/signing.json scripts/bench/results/after.json
```

The "before" run used the same scripts on origin/master 43f0d71 (and
`signing fixed`, that commit plus `fields.py`) against the same database
before `alembic upgrade discover_perf`.

## Calendar, date ranges and a club's events (`discovery_polish`)

Three new reads on the public site: `/discover/calendar?month=` (events per
day of a month, in India), `from`/`to` on `/discover/events` (days in
India, an event matches if it touches them), and
`/discover/clubs/{slug}/events` (a club's past or upcoming events, paged).

`moa_bench` had been dropped, so these were measured on a fresh copy made
the same way (`BENCH_DB=moa_bench_polish python -m scripts.bench.seed`:
schema by alembic, `discovery_polish` included, the same seeded data). The
machine was running other test suites at the time (load average 3–12), so
the timings are indicative; the plans and buffer counts are not affected.
October 2026 is the densest month of the data: 5,325 public events touch
it, 5,826 event-days.

| endpoint | p50 / p95 ms (n=200) | stmts |
|---|---:|---:|
| calendar (warm: the minute cache) | 1.2 / 1.4 | 0 |
| calendar, this month (cold) | 15.9 / 22.5 | 1 |
| calendar, next month (cold) | 15.2 / 17.7 | 1 |
| calendar, a year back (cold; held events) | 8.8 / 10.0 | 1 |
| calendar, `q=robotics` (cold) | 10.7 / 14.1 | 1 |
| calendar, within 50 km of Bengaluru (cold) | 12.4 / 15.7 | 1 |
| events `from`/`to`: a week, a week out (1,913 events) | 14.8 / 17.7 | 3 |
| events `from`/`to`: the next 90 days (10,071) | 21.9 / 30.0 | 3 |
| events `from`/`to`: one day 150 days out (1) | 6.7 / 9.2 | 3 |
| club events, past (the biggest club) | 7.3 / 16.1 | 4 |
| club events, upcoming (the biggest club) | 7.3 / 7.9 | 4 |
| club events, past, offset beyond the end | 3.4 / 4.0 | 2 |

(The search's third statement is the page's going counters; its categories
come from the minute cache. A club's events: the club, the count, the page,
the counters; past the end only the first two.)

### One index for "touches these days": `ix_events_span`

"An event touches [a, b)" is `start < b AND end > a`: two one-sided ranges
on two different expressions, which no B-tree serves as one range. For the
calendar's past months it is worse: `ix_events_shown_start` only holds
published and live events, and `end > a` on `ix_events_ends_at` is
everything that ended after `a`. So a GiST index on each event as a range,
`tstzrange(start, GREATEST(start, end), '[]')`, and `span && [a, b)`, with
the exact rule (`start < b AND (end > a OR start >= a)`: an event ending at
midnight is not on the next day, one of no length is on the day it starts)
as a filter on what the index finds. `GREATEST` keeps the range from being
inverted, which would make a write with a negative duration fail.

The index is **not partial**. With it partial (`WHERE NOT is_deleted AND
status IN (…)`) the planner ignored its statistics (Postgres only reads
expression statistics from indexes without a predicate) and estimated every
stretch at 513 rows, whatever its length. The 90-day page then went through
the span index and a top-N sort of 10,830 rows (19.1 ms) instead of walking
`ix_events_shown_start` in order and stopping after 24 (0.48 ms now). With
the statistics, a week's estimate is 2,213 (actual 2,218) and 90 days'
11,522 (actual 11,674). The migration ends with `ANALYZE events` so the
statistics are there from the start. On writes it costs about 70 µs per
event whose times change (5,000 start-time changes: 628 ms with it, 261 ms
without); the going-count updates the registration trigger makes change
no column it covers, so they only touch it when a row cannot be updated in
place (no HOT update). Size: 8.9 MB for 37,000 events.

**A week, a week out**: the span index and the date index ANDed, 2,071 rows
read, the 24 kept by a top-N sort:

```
Aggregate  (actual time=4.135..4.136 rows=1)
  ->  Nested Loop  (rows=1913)
        ->  Bitmap Heap Scan on events  (rows=2071)  Heap Blocks: exact=1756
              ->  BitmapAnd
                    ->  Bitmap Index Scan on ix_events_span  (rows=2218)
                          Index Cond: (tstzrange(event_datetime, GREATEST(…), '[]') && '["2026-10-24 00:00:00+05:30","2026-10-31 00:00:00+05:30")')
                    ->  Bitmap Index Scan on ix_events_shown_start  (rows=5456)
                          Index Cond: (event_datetime < '2026-10-31 00:00:00+05:30')
        ->  Memoize -> Index Scan using clubs_pkey on clubs  (Hits: 1306  Misses: 765)
Execution Time: 4.155 ms
```

**The next 90 days**: the count reads what is on through the span and
end indexes (11.6 ms, two workers); the page walks `ix_events_shown_start`
in order and stops at 24 (0.48 ms).

**One day 150 days out**: the span index alone, 1 row (0.03 ms).

### The calendar: one statement, the month's events once

```sql
SELECT days.day, count(*)
FROM (SELECT start, end FROM events JOIN clubs … WHERE <public> AND span && [month) AND <exact>) AS month_events
JOIN generate_series(greatest(day_of(start), first), least(day_of(last instant on), last), '1 day') AS days(day) ON true
GROUP BY days.day
```

The month's events are found once through `ix_events_span`; each gives
only the days it touches (a series of its own, cut to the month), and those
are counted. The first version joined every event to every day of the month
by the overlap rule: 31 × 4,972 rows tested (148,306 thrown away) and the
end expression worked out for each pair, 21.8 ms; per event, 11.5 ms for the
same month (best of 5, EXPLAIN ANALYZE). The densest month, as it runs now:

```
Finalize GroupAggregate  (actual time=8.179..9.156 rows=29)
  ->  Gather Merge  Workers Launched: 2
        ->  Partial HashAggregate  (rows=29 loops=3)
              ->  Nested Loop  (rows=1942 loops=3)
                    ->  Nested Loop  (rows=1657 loops=3)
                          ->  Parallel Bitmap Heap Scan on events  (rows=1775 loops=3)  Heap Blocks: exact=1432
                                ->  BitmapAnd
                                      ->  Bitmap Index Scan on ix_events_span  (rows=5657)
                                      ->  BitmapOr
                                            ->  Bitmap Index Scan on ix_events_shown_start  (rows=5722)
                                            ->  Bitmap Index Scan on ix_events_ends_at  (rows=24633)
                          ->  Index Scan using clubs_pkey on clubs  (loops=5325)
                    ->  Function Scan on generate_series days  (rows=1 loops=4972)
Execution Time: 9.180 ms
```

(The `BitmapOr` is the planner serving the visibility rule's two arms, on
now or still to come / held and over, from the two existing indexes. A year
back the same plan reads 706 events, 1.7 ms.) The answer is kept for the
minute per month, filters and place (`("calendar", …, minute)` in the
shared cache); a typo in `q` that finds nothing tries trigrams once more,
as the search does.

### A club's events

`ix_events_club_start (club_id, event_datetime) WHERE NOT is_deleted`
serves both directions: past is walked backwards from now and stops after
the page (the same rule as the club page's six), upcoming forwards. Four
statements, whatever the club holds.

### Reproduce

```
createdb moa_bench_polish
BENCH_DB=moa_bench_polish python -m scripts.bench.seed
BENCH_DB=moa_bench_polish python -m scripts.bench.run --label polish --freeze --only "from/to,calendar,club events" --n 200 --budget 15
BENCH_DB=moa_bench_polish python -m scripts.bench.run --only "from/to,calendar,club events" --warmup 2 --min 3 --n 5 --budget 1 --explain plans.txt --indexes indexes.json
```

`discovery_polish` on this data: about 2 s of SQL (marking 27,694 points as
from links 1.3 s, the index 0.3 s, `ANALYZE events` 0.4 s); 11.8 s for the
whole alembic run with the machine loaded. At production's size,
milliseconds; the `ALTER TABLE` holds the events table for the length of
the migration's one transaction.

## Larger worlds

`seed.py` now takes every count as a flag (`--scale 10`, or `--clubs`,
`--upcoming`, `--past`, `--users`, `--registrations`, `--follows`,
`--threads` …) and loads by COPY; with no flags it builds the world above
(same sizes and spreads, new draws: the numbers above were reproduced within
noise). The scale study at 15,000 and 100,000 clubs, with a load generator,
hot-row writes and the background sweeps, is in `docs/SCALING.md` at the
repository root; its raw results are in `results/scale/`.


## Read paths at a lakh of clubs (`perf/discover-scale-2`, 18 October 2026)

The read-path fixes from `docs/SCALING.md` section (d): 1 (a lower bound on
what is on), 2 (capped totals), 3 (For You on a bounded candidate set), 4
(each club's place kept on the club), 12 (the cities number), 13 (the typo
fallback and `sort=relevance` look only at what can be on) and 14 (a paged
sitemap). Migration `discover_scale`.

### What changed

- **What is on, bounded from below.** Every list of what is on or to come
  asks for `start >= floor` beside `end >= now`; the floor is now minus 72
  hours (`MAX_EVENT_HOURS`, now a limit on an event's length, checked on
  create, edit and copy; an event saved before it keeps its length through
  an edit that leaves it alone). While an event saved before the limit and
  longer than it is still running, the floor reaches back to its start
  (`discover/upcoming.py`: one lookup on `ix_events_long_ends_at`, which
  holds only those, kept for the minute). So the floor never leaves out an
  event that is on, and the date-ordered walks of `ix_events_shown_start`
  start near now instead of at the oldest event still marked published.
  Applied in the discover lists (`_visible_where`, `_conditions`, the
  upcoming arm of `_public_where` where a caller has it), the club pages,
  the event page's "more" and "similar", For You and the app's
  `/events/list?is_ended=false`. Club-scoped dashboard lists are bounded by
  the club and were left alone.
- **Totals that stop.** `/discover/events`, `/discover/clubs` and
  `/discover/threads` count at most 1,001 rows; past 1,000 they answer
  `total: 1000, total_capped: true` and the pages go on (a page is asked
  for, not worked out from the total). `sort=distance` counts the placed
  part only as far as the page needs. The front page's "upcoming events" is
  the sum of the per-category counts it already makes, not a count of its
  own.
- **For You.** Scores the UNION of: the soonest 200 events of the clubs the
  user follows or pinned; the soonest 200 within 30 days sharing one of the
  user's interests (skipped for a user with none); the 200 most popular in
  the user's one-degree region (everywhere when unknown) and the 100 most
  popular online events, both the same for everyone and kept for the minute.
  The same formula; `me_shared_interests` counts the candidates only. Each
  user's first 200 ids and scores are kept five minutes (`cache.personal`,
  the in-process cache's interface, ready for a Redis store), forgotten on
  follow, unfollow, pin, hide and a change of interests, and checked against
  the rows before they are served (registered since, hidden, over or closed
  ones drop out). The region comes from `lat`/`lng` on the request, else
  from the address lookup if it already knows the address; the lookup is
  never waited for here.
- **Club places.** `clubs.place_lat` / `place_lng`, kept by triggers on
  clubs and events in the writer's transaction (`discover/places.py`), with
  `ix_clubs_place` over the listed clubs that have one. `search_clubs` boxes
  the radius on it; the front page's "clubs near you" widens circles of 100
  and 1,000 km on it until it has eight, instead of reading every club's
  place every minute. `POST /admin/discover/recompute-club-places` and
  `scripts/recompute_club_places.py` put right anything written with
  triggers off.
- **Cities** is kept in `discover_stats` and counted again in the
  background, one process at a time, at most every 30 minutes.
- **Search only what is on.** The typo fallback's three arms keep only shown
  events starting from the floor, and find the event arm's candidates in new
  trigram indexes over shown events only (`ix_events_shown_name_trgm`,
  `ix_events_shown_location_trgm`; the status clock moves what is over out
  of them, so no nightly rebuild). A search whose letters appear nowhere,
  in any event, club or category of any date (a trigram probe, with
  sequential scans off for that one statement), skips the substring pass,
  which as a filter on what is on had to read every event on to find none.
  `sort=relevance` ranks what the bounded conditions admit.
- **The sitemap.** `GET /discover/sitemap/index` lists pages of 5,000 (events
  then clubs, in id order, each with its size and newest change); `GET
  /discover/sitemap/{events|clubs}/{page}` serves one, 404 past the last.
  Every public event of any age (held ones stay indexable) and every listed
  club. One statement numbers the rows and gives each page its first id (the
  club rule as "not one of the unlisted clubs", a hashed list, so the events
  are read once); a page is then a primary-key range. The boundaries are
  kept an hour. `GET /discover/sitemap` is unchanged.
- **For the site's feeds:** cards and the event page carry `summary` (the
  description as one plain line, at most 200 characters, cut at a word),
  `created_at` and `updated_at` (no publish time is kept; `created_at` stands
  in), and `sort=newest` walks the new `ix_events_shown_created`.
- **`ix_clubs_user_id`.** Every signed-in request loads the account's club by
  `clubs.user_id`, which had no index: a read of the whole clubs table
  (1,800 pages at 15,000 clubs) on every app request.

### The measurements

The same laptop as the scale study (Apple M2, 8 GB, Postgres 16 with its
defaults), this time shared: another engineer's test suite and benchmark ran
throughout (load average 5–17, swap in use, the events table pushed out of
memory between runs). Timings of the same code moved by up to 20× between
rounds, so each table gives, beside p50 / p95, the **pages** one request
touches: shared buffers hit plus read, summed over `EXPLAIN (ANALYZE,
BUFFERS)` of every statement it ran (`run.py --buffers`, new). Pages do not
depend on what else the machine is doing; they are the work. Before and
after ran back to back on the same data (`before` is `origin/master` at
54839ed, from `git archive`). "(cold)" empties both caches before every
request: the front page, For You and the sitemap index as a cache miss.
"(user cold)" empties only what is kept per person: a user's first For You
with the minute's shared lists already made, which is the usual miss in
production.

Worlds:

- **S1**: `seed.py --scale 10` (15,000 clubs, 120,000 upcoming and 250,000
  past events, 600,000 users, 4,000,000 registrations), as in the scale study.
- **S2e**: S2's clubs and events (100,000 clubs, 300,000 upcoming and
  1,000,000 past events) with fewer people (300,000 users, 1,500,000
  registrations, 1,000,000 follows, 50,000 posts): 2.8 GB. The full S2
  (9.7 GB) did not fit the disk left on the shared machine. The read paths
  measured here depend on the events and clubs; For You and the app's lists
  read one user's rows, which are the same size in both.

**S2e**, before and after. p50 / p95 over n requests; the before column's
slow rows rest on two or three requests each (one took 90 s), so read them as
"at least this slow". Pages are per request.

| endpoint | before p50 / p95 ms | stmts | pages | after p50 / p95 ms | stmts | pages |
|---|---:|---:|---:|---:|---:|---:|
| home (cold) | 34 s / 46 s (n=2) | 17–44 | 601,587 | 1.4 s / 1.4 s (n=3) | 17 | 85,993 |
| home lat/lng (cold) | 3.4 s / 3.5 s (n=2) | 26 | 857,774 | 760 / 1.0 s (n=5) | 27 | 177,556 |
| events | 2.1 s / 3.1 s (n=3) | 3 | 137,163 | 33.0 / 57.4 (n=30) | 3 | 5,670 |
| events q typo | 7.4 s / 12 s (n=2) | 5 | 149,695 | 124 / 162 (n=30) | 7 | 12,916 |
| events q relevance | 4.0 s / 4.2 s (n=2) | 3 | 222,948 | 201 / 303 (n=19) | 6 | 49,264 |
| events distance r=50 | 6.2 s / 9.1 s (n=2) | 4–6 | 100,800 | 121 / 182 (n=30) | 4 | 87,421 |
| events popular | 5.7 s / 6.3 s (n=2) | 3 | 208,726 | 240 / 326 (n=16) | 3 | 29,993 |
| clubs | 60.2 / 83.0 (n=30) | 6 | 12,535 | 54.5 / 86.6 (n=30) | 6 | 12,651 |
| clubs nearby | 86 s / 91 s (n=2) | 7 | 606,680 | 55.7 / 74.0 (n=30) | 6 | 14,573 |
| event page | 3.9 s / 4.5 s (n=2) | 11 | 29,908 | 17.1 / 34.5 (n=30) | 11 | 108 |
| threads | 32.1 / 78.9 (n=30) | 4 | 8,000 | 14.0 / 20.1 (n=30) | 4 | 3,976 |
| sitemap (cold) | 3.4 s / 3.5 s (n=2) | 2 | 114,062 | 3.4 s / 4.0 s (n=2) | 2 | 120,897 |
| app for-you | 4.9 s / 4.9 s (n=2) | 3 | 135,558 | 16.5 / 35.1 (n=30) | 3 | 1,708 |
| events offset=4800 | 13 s / 15 s (n=2) | 3–5 | 157,158 | 40.3 / 98.6 (n=30) | 3 | 29,954 |
| app for-you (cold) | 4.5 s / 5.6 s (n=2) | 3 | 135,558 | 463 / 575 (n=9) | 7 | 110,767 |
| app for-you (user cold) | 5.4 s / 5.8 s (n=2) | 3 | 135,558 | 53.7 / 95.9 (n=30) | 4 | 44,298 |
| app for-you lat/lng (cold) | 5.3 s / 5.5 s (n=2) | 3 | 135,558 | 909 / 1.5 s (n=5) | 7 | 173,334 |
| event page (busiest category) | 4.5 s / 4.8 s (n=2) | 11 | 29,948 | 10.3 / 12.7 (n=30) | 11 | 130 |
| events newest | — | — | — | 28.8 / 37.1 (n=30) | 3 | 5,586 |
| sitemap index (cold) | not offered | — | — | 3.0 s / 3.8 s (n=2) | 2 | 149,057 |
| sitemap events page 1 (cold) | not offered | — | — | 2.6 s / 2.7 s (n=2) | 2 | 154,106 |
| sitemap events page 1 | — | — | — | 32.2 / 38.1 (n=30) | 1 | 17,037 |

**S1**, before and after. (Measured before the last two changes: the typo
arms on the shown-only trigram indexes, and the sitemap's boundaries as a
hashed club list. S1 was dropped to make room for S2e.)

| endpoint | before p50 / p95 ms | stmts | pages | after p50 / p95 ms | stmts | pages |
|---|---:|---:|---:|---:|---:|---:|
| home (cold) | 716 / 1.7 s (n=6) | 17 | 82,897 | 145 / 230 (n=33) | 17 | 25,952 |
| home lat/lng (cold) | 970 / 1.1 s (n=6) | 26 | 166,403 | 191 / 284 (n=25) | 27 | 52,828 |
| events | 89.8 / 120 (n=55) | 3 | 19,509 | 13.7 / 18.0 (n=51) | 3 | 3,191 |
| events q typo | 308 / 410 (n=16) | 5–7 | 23,926 | 117 / 326 (n=31) | 7 | 11,842 |
| events q relevance | 145 / 275 (n=30) | 3 | 24,565 | 79.9 / 199 (n=49) | 6 | 17,812 |
| events distance r=50 | 86.1 / 172 (n=51) | 4 | 58,512 | 30.8 / 61.6 (n=60) | 4 | 26,936 |
| events popular | 89.4 / 196 (n=47) | 3 | 21,575 | 47.6 / 102 (n=60) | 3 | 12,748 |
| clubs | 21.5 / 34.8 (n=60) | 6 | 4,687 | 17.7 / 47.7 (n=60) | 6 | 3,989 |
| clubs nearby | 330 / 521 (n=14) | 6 | 146,839 | 16.3 / 29.8 (n=60) | 6 | 6,971 |
| event page | 14.1 / 18.8 (n=60) | 11 | 1,022 | 14.1 / 20.1 (n=60) | 11 | 115 |
| threads | 36.6 / 65.8 (n=60) | 4 | 9,446 | 8.7 / 12.3 (n=60) | 4 | 2,912 |
| sitemap (cold) | 356 / 710 (n=14) | 2 | 38,820 | 206 / 947 (n=19) | 2 | 38,820 |
| app for-you | 426 / 627 (n=12) | 3 | 19,858 | 35.9 / 144 (n=60) | 3 | 1,494 |
| events offset=4800 | 85.4 / 161 (n=52) | 3 | 34,934 | 14.2 / 19.4 (n=60) | 3 | 18,570 |
| app for-you (cold) | 416 / 5.3 s (n=7) | 3 | 19,858 | 601 / 1.2 s (n=8) | 7 | 36,933 |
| app for-you (user cold) | 482 / 907 (n=10) | 3 | 19,858 | 102 / 384 (n=39) | 4 | 16,428 |
| app for-you lat/lng (cold) | 488 / 851 (n=10) | 3 | 19,858 | 248 / 960 (n=14) | 7 | 58,582 |
| event page (busiest category) | 26.4 / 63.0 (n=60) | 11 | 7,949 | 11.1 / 15.7 (n=60) | 11 | 132 |
| sitemap index (cold) | not offered | — | — | 114 / 163 (n=42) | 2 | 32,095 |
| sitemap events page 1 (cold) | not offered | — | — | 200 / 633 (n=20) | 2 | 33,040 |

The seven the study asked about, at S2e:

| | before | after | pages before → after |
|---|---:|---:|---:|
| front page, cache miss | 34 s | 1.4 s | 601,587 → 85,993 |
| front page with a point, cache miss | 3.4 s | 0.76 s | 857,774 → 177,556 |
| `/discover/events`, first page | 2.1 s | 33 ms | 137,163 → 5,670 |
| distance sort, 50 km | 6.2 s | 121 ms | 100,800 → 87,421 |
| search with a typo | 7.4 s | 124 ms | 149,695 → 12,916 |
| `/discover/clubs` near | 86 s | 56 ms | 606,680 → 14,573 |
| For You (a user's first, the minute's lists made) | 5.4 s | 54 ms | 135,558 → 44,298 |
| For You (kept) | 4.9 s | 17 ms | 135,558 → 1,708 |
| event page, "similar" in the busiest category | 4.5 s | 10 ms | 29,948 → 130 |

### What the pages say

- **Bounded by the data, not growing with it**: the first page of events
  (5,670 pages at S2e against 3,191 at S1, for 2.5 times the events on), the
  event page (108–130), For You kept (1,500–1,700), clubs near (7,000 →
  14,600 for 6.7 times the clubs: the box holds more clubs, not every club).
  Before, each of these read most of the events or clubs table.
- **The event page's "similar"** walked the category's past events before
  reaching one that is on (29,948 pages); it now starts at the floor (130).
- **For You** scores 400–700 candidates. A user's first ranking reads
  44,000 pages at S2e, most of them the interests arm walking the soonest
  upcoming events until it has 200 matches (proportional to how rare the
  user's interests are, not to the number of events) and the candidates'
  rows; kept, 1,700. "(cold)", which also rebuilds the region's and the
  online popular lists (once a minute per process in production, shared by
  everyone), is 110,000–173,000.
- **Still proportional to what is on, once a minute per process**: the
  front page's category counts and its "popular" (86,000 pages at S2e, a
  sort of every upcoming event), "near you" (the events in a 70 km box,
  ~90,000 more with a point), the distance sort (every in-person event in
  the radius, 87,000), `sort=relevance` (every match on, 49,000), and For
  You's shared popular lists. They are computed once a minute per process
  and shared, which Redis (Phase 0) makes once a minute in all. The
  substring search for a word that does occur is a filter on what is on:
  cheap when the word is common (the capped count stops at 1,001), a read of
  every event on when it is rare but present.
- **The sitemap index** reads every event once (149,000 pages, 3 s at S2e)
  and is kept an hour; a page is a primary-key range (17,000 pages, 32 ms).
  The one-page `/discover/sitemap` is unchanged.

### The migration

`discover_scale` on S1 (370,000 events, 15,000 clubs): 8.2 s. On S2e
(1,300,000 events, 100,000 clubs): 71.6 s with the machine at load 16, of
which the place backfill (one grouped pass over the events with a point),
the cities count (a distinct over every event) and the concurrent index
builds (each a pass over the events) are the parts. Locks: the first
transaction (columns, table, triggers) is catalogue-only and short; the
backfill holds each club row it rewrites until it commits (a follow of such a
club waits for it, seconds at a lakh of clubs); the indexes are built
concurrently and block nothing. 99,863 of 100,000 clubs got a place. The
backfill rewrites most clubs' rows; the migration vacuums clubs after it.

### What is left

- The minute's shared computations above (front page, near you, For You's
  popular lists) are per process: Redis (`cache.shared` and
  `cache.personal` have the interface for it) makes them once a minute in
  all, and the per-user rankings shared between workers.
- A rare word that does occur somewhere still reads every event on in the
  substring pass; a search engine (Phase 2) or substring matching through
  the shown-only trigram indexes when the word is rare would bound it.
- The front page's "near you" pool reads every event in its box before
  keeping the soonest; streaming it in date order would stop early.

## The write path (`perf/write-path`, 18 October 2026)

Registrations, the timed-email sweep and the counter check, before and after
the branch `perf/write-path` (migration `write_path`; `docs/SCALING.md`
section d, items 5, 6, 8, 9, 11, 15–18). Raw results in
`results/write_path/`.

**Data**: `seed.py --scale 10` (S1: 15,000 clubs, 370,000 events, 615,000
users, 4,000,020 registrations, 1.5 million follows; 2.8 GB). The before runs
used origin/master at 54839ed on the schema it migrates to; the after runs
this branch after `alembic upgrade head`. **Machine**: the M2 laptop of the
scale study, but shared this time with other engineers' test suites and
benchmarks (on the same Postgres) and an Android build: load average 5–20
while these ran. So every burst was run interleaved -- before, then after,
the same configuration, each on a freshly started server (killed and its
connections gone before the next), the same harness and data -- and the
pairs are what to compare, not the absolute numbers. The first before runs,
on a quieter machine (`hot_before_quiet.json`), took 19–20 registrations a
second at one worker on both events without a failure -- when the biggest
had 7,600 registrations; it has grown by 500 with every run since, and with
it the ticket email's read of the event's deliveries (below) -- and 8–9 a
second with 13–56 failures at four workers or under the campus plan.

### 500 registrations in 10 seconds

`hot.py event --n 500 --seconds 10`, signed-in app users, free and uncapped
event; "campus" puts the club on that plan (20,000 a event, 1.5 lakh a
month) with the event made today, so both plan caps are counted on every
registration. *Waited for the row*: how long the statement that takes the
event's row took (`SELECT … FOR UPDATE` before, the `UPDATE … RETURNING`
of `take_place` after), for those that got it. *Held it*: from then to the
commit. Errors are all 500s from the pool (30 s waiting for a connection)
or, at four workers, Postgres out of connections.

| | registered before | per second | p50 / p95 | errors | waited for the row p50 / p95 | held it p50 / p95 | loop lag p99 |
|---|---:|---:|---|---:|---|---|---:|
| **1 worker, empty event**, before | 0 | 13.8 | 11.0 s / 25.1 s | 0 | 637 ms / 1.1 s | 13 / 90 ms | 213 ms |
| after | 0 | 17.8 | 8.6 s / 18.4 s | 0 | 224 ms / 1.5 s | 8 / 57 ms | 143 ms |
| again, before | 0 | 13.5 | 16.4 s / 28.9 s | 0 | 107 ms / 3.8 s | 3 / 89 ms | 163 ms |
| after | 0 | 20.0 | 7.1 s / 14.7 s | 0 | 118 ms / 602 ms | 8 / 44 ms | 168 ms |
| **1 worker, the biggest event**, before | 11,934 | 7.1 | 30.1 s / 62.8 s | 301 | 3.5 s / 16.9 s | 105 / 963 ms | 301 ms |
| after | 12,165 | 17.1 | 9.5 s / 18.3 s | 0 | 139 ms / 614 ms | 8 / 57 ms | 141 ms |
| again, before | 12,665 | 6.4 | 50.9 s / 68.7 s | 230 | 2.5 s / 4.1 s | 71 / 316 ms | 428 ms |
| after | 13,090 | 18.1 | 9.0 s / 18.3 s | 0 | 159 ms / 978 ms | 10 / 62 ms | 146 ms |
| **1 worker, biggest, on campus**, before | 13,590 | 6.8 | 30.2 s / 66.6 s | 295 | 3.3 s / 17.5 s | 93 / 998 ms | 358 ms |
| after | 13,838 | 11.5 | 8.9 s / 30.2 s | 0 | 192 ms / 1.0 s | 10 / 70 ms | 281 ms |
| **4 workers, biggest**, before | 15,192 | 7.9 | 30.0 s / 58.1 s | 266 | 7.3 s / 22.6 s | 81 / 1,076 ms | 223 ms |
| after | 15,438 | 22.0 | 6.2 s / 14.2 s | 35 | 1.1 s / 5.4 s | 13 / 73 ms | 67 ms |
| after, `APP_DB_POOL_SIZE=5 APP_DB_MAX_OVERFLOW=15` | 15,903 | 26.3 | 5.9 s / 11.0 s | 0 | 758 ms / 4.7 s | 12 / 76 ms | 82 ms |
| **4 workers, empty event**, before | 0 | 19.3 | 7.5 s / 17.9 s | 34 | 1.3 s / 5.1 s | 14 / 88 ms | 73 ms |
| after | 0 | 38.2 | 1.2 s / 6.0 s | 142 | 829 ms / 5.3 s | 11 / 58 ms | 81 ms |
| after, pool 5 + 15 | 0 | 28.6 | 4.5 s / 8.8 s | 0 | 616 ms / 4.8 s | 9 / 60 ms | 92 ms |

What it shows:

- **The event's row is held for the insert and the commit only: 8–13 ms at
  the median, 44–76 ms at p95, the same on an empty event and on one with
  15,000 registrations.** Before, a registration held it 70–120 ms (p50) and
  up to a second (p95) on the big event -- through the duplicate check over
  every registration, the plan's counts, and the rest -- so the queue for it
  grew with the event.
- **On the biggest event one worker went from 6–7 registrations a second,
  with 230–300 of 500 failing, to 17–18 a second with none failing**; the
  p95 from a minute to 18 s (the burst arrives at 50 a second, faster than
  one worker on this machine can take any event's registrations). Part of
  that is not the lock: each registration's ticket email
  (`deliveries.queue`, called by `send_ticket`) read every delivery the
  event had ever made -- about 3,700 rows on this event, a third of a second
  of the worker's time per registration -- and now reads the guest's own.
- Four workers: the pool settings are what keep them inside Postgres's 100
  connections (four pools of 10 + 20 is 120; 5 + 15 is 80). With them, no
  failures and 26–29 registrations a second, where before four workers made
  the biggest event slower than one (7.9 a second, 266 failures).
- The counters stayed equal to their rows and every event kept four journey
  emails in every run, before and after.

### The minute email sweep

`sweeps.py --templates 2000,10000,40000`: that many events given the four
journey emails (two timed and on), inside a transaction rolled back; what the
sweep reads, twice (the first cold). Before: every timed email ever enabled,
then decided in Python. After: `emails.due_query`, an index range read of
`due_at` (`ix_email_templates_due`). In both, none of these was due at the
moment measured; after, a due one is one more index entry.

| events with journey emails | rows read before | before: query + Python | after: query |
|---:|---:|---:|---:|
| 2,000 | 4,000 | 0.39–3.0 s + 0.08 s | 0.6–8.3 ms |
| 10,000 | 20,000 | 11.5–15.2 s + 0.37 s | 1.2–1.5 ms |
| 40,000 | 80,000 | 60–84 s + 2.0 s | 1.3–13.8 ms |

The price is on the write: inserting the 160,000 templates took 27.5 s
against 18.8 s (the trigger looks up the event, and three more indexes),
0.17 ms a template, paid four times per event, once.

### The counter check

`counters.drift`, three runs (the first cold). Before: two statements, one
a scan of every registration (`GROUP BY event_id`), in every worker at its
first sweep. After: 29 statements of 20,000 events or 2,000 clubs each, the
events' read from `ix_registrations_event_created` alone; on one process,
every six hours (`scheduled_runs`), with 50 ms between slices.

| | total | statements | longest statement |
|---|---:|---:|---:|
| before | 3.2 / 5.5 / 7.5 s | 2 | ≈ the total |
| after | 7.8 (cold) / 1.9 / 0.86 s | 29 | 2.9 s (cold) / 280 / 59 ms |

### The statements behind it

- The plan's monthly count (`gates.count`, every registration to a capped
  club), for the club of the biggest event: before, a parallel scan of the
  whole events table (the soft-delete filter's `is_deleted IS false` cannot
  use `ix_events_club_start`, which says `WHERE NOT is_deleted`) and a heap
  read of every registration: 37–62 ms warm, 8.9 s cold. After, the
  deleted-events filter spelled out and the club's 85 events each probed in
  `ix_registrations_event_created`, index-only: 8.8 ms warm (0.4 s cold). It
  grows with the club's events, not the platform's.
- The duplicate check: `user_id = ? OR email = ?` read every registration
  of the event (on the biggest, 16,400 of them: 7.5 ms warm, 340 ms cold);
  now two index probes (the address one 0.04 ms warm, 0.6–5.6 ms cold).
- A paid event's places: an index-only count of its paid registrations.

### The migration on this data

`write_path` on S1 with 40,000 events' journey emails committed (176,000
templates, 16,000 of them duplicates from the race, 4,000 messages sent
from duplicates): **45 s**, nearly all of it the concurrent index builds,
during which writes go on.

| step | time |
|---|---:|
| add `due_at`, the functions and triggers, `scheduled_runs` | 1.1 s |
| backfill `due_at` (176,000 templates, 20,000 events a statement) | 8.4 s |
| find and remove the duplicates, re-point 4,000 messages and the copies' `source_id` | 1.6 s |
| `uq_email_templates_*`, `ix_email_templates_due` | 1.0 s |
| `ix_registrations_event_email` (4 million registrations) | 22.2 s |
| `ix_registrations_event_created` | 8.5 s |
| the partial tracking-link index, the full one dropped | 1.2 s |

Per row: the backfill about 21,000 templates a second; the two
registration indexes about 7.7 µs a registration together; the dedupe is
one pass over the journey templates. At production's size today (a small
fraction of S1) the whole migration is a few seconds; at a lakh of clubs
(S2: 1.5 crore registrations) the registration indexes would take about two
minutes, concurrently.

### Reproduce

```
cd backend
git archive origin/master backend | tar -x -C /tmp/before     # the "before" code
createdb moa_bench_s2e
(cd /tmp/before/backend && BENCH_DB=moa_bench_s2e python -m scripts.bench.seed --clubs 100000 --upcoming 300000 \
    --past 1000000 --users 300000 --registrations 1500000 --follows 1000000 --threads 50000 --likes 100000 \
    --comments 30000 --sizes-out s2e_seed.json)
KEYS="home (cold),home lat/lng (cold),events,events distance r=50,events q typo,events q relevance,events popular,\
events offset=4800,clubs,clubs nearby,app for-you (cold),app for-you (user cold),app for-you lat/lng (cold),app for-you,\
event page,event page (busiest category),threads,sitemap (cold),sitemap index (cold),sitemap events page 1 (cold),\
sitemap events page 1,events newest"
(cd /tmp/before/backend && BENCH_DB=moa_bench_s2e python -m scripts.bench.run --label before --freeze --n 30 --budget 4 \
    --min 2 --warmup 1 --exact "$KEYS" --buffers --out before.json)   # copy this branch's run.py there first
BENCH_DB=moa_bench_s2e python -c "from scripts.bench import _env; from alembic.config import Config; \
    from alembic import command; command.upgrade(Config('alembic.ini'), 'head')"
BENCH_DB=moa_bench_s2e python -m scripts.bench.run --label after --freeze --n 30 --budget 4 --min 2 --warmup 1 \
    --exact "$KEYS" --buffers --out after.json
python -m scripts.bench.report before.json after.json
dropdb moa_bench_s2e
```

Raw results: `results/read_paths/` (`s1_*.json`, `s2e_*.json`, the seeds' sizes).

### Reproduce (the write path)

```
cd backend
createdb moa_bench_sw
BENCH_DB=moa_bench_sw python -m scripts.bench.seed --scale 10           # at the commit to measure "before" on
BENCH_DB=moa_bench_sw python -m scripts.bench.sweeps --templates 2000,10000,40000 --runs 3 --out sweeps.json
BENCH_DB=moa_bench_sw python -m scripts.bench.serve --workers 1 --port 8093 --stats-dir /tmp/s1 > server.log 2>&1 &
BENCH_DB=moa_bench_sw python -m scripts.bench.hot event --url http://127.0.0.1:8093 --n 500 --seconds 10 --event big \
    --stats-dir /tmp/s1 --out hot.json                                 # --event empty; --plan campus
# stop the server: kill -9 whatever listens on the port (uvicorn's workers outlive their parent on macOS)
# and wait until pg_stat_activity shows none of its connections, or the next run waits on its row locks
BENCH_DB=moa_bench_sw python -c "from scripts.bench import _env; from alembic.config import Config; \
    from alembic import command; command.upgrade(Config('alembic.ini'), 'head')"   # then the same runs again
dropdb moa_bench_sw
```

`serve.py` now also records how long the event's row is held, to the commit
(`row_held_ms` in the results), and `hot.py --plan KEY` puts the event under
a plan's caps.


## Integrity and the live room (`fix/discover-integrity`, 18 October 2026)

An audit of discover, threads, the app's club lists, the live room and
tracking links found counters that lost writes, things the public could see
that it should not, and reads that grew with the room or the platform.
Migration `discover_integrity` (revises `discover_scale`).

### What changed

- **The live room** (`app/api/live/room.py`, `limits.py`). Limits are per
  person in the room (the browser's `X-Live-Key`, the player's
  `X-Live-Player`) with a ceiling per address sized for a hall (join 10 a
  minute a phone and 3,000 an address; answers 30 / 20,000; asks 20 an hour /
  2,000; votes 120 / 30,000; poll answers 60 / 20,000); they were per address
  only, so a venue's Wi-Fi was one person (about 30 joins a minute for the
  whole hall). The dashboard's "send survey" and "push update" are limited per
  account (through the clubs gateway every club shared Vercel's addresses).
  The room's feed is one snapshot per event made at most once a second
  (single-flight, discover.cache's store), kept with its JSON made; a browser
  that voted for nothing, asked nothing and answered no poll gets that body
  as it is, one that did gets a copy with its flags, from what it did (read
  once, kept 30 s, kept current by its own taps). Poll results are a GROUP
  BY (index-only on `ix_live_poll_responses_counts`); they were every
  response read as rows. The quiz is one snapshot per quiz a second: every
  player by key with their score, the scores in order (rank by binary
  search), the top ten (read through `ix_live_quiz_players_board`), the open
  question's answers; an answer or a join updates it in place. A player's
  poll runs no statement. The host's actions forget the snapshots at once.
  The room is closed (404) for a draft, a deleted event or one of a club that
  is not listed; it served them by id.
- **Counters.** A post's likes, comments and poll votes are kept by triggers
  on their rows (`THREAD_COUNTS_SQL`), a live question's votes and a quiz
  score by one `UPDATE … SET x = x + n`, and the rows are written `INSERT … ON
  CONFLICT`: a double tap is answered (a toggle, a 409 for a second quiz
  answer or follow), never a 500, and never a count off by one. Views are
  recorded in the process and written in batches (`discover/visitors.py`):
  one statement inserts the batch's rows and moves each counter by the rows
  it inserted, at most every 5 s or 500 views; reading a post or an event
  page no longer writes. A post counts a person once (an anonymous one once a
  day); an event page counts a visitor once a day. `counters.reconcile_more`
  checks posts, poll options and live questions every six hours (its own
  `scheduled_runs` row), and the admin's reconcile reports them.
- **What the public sees.** `clubs.listed` (verified, not deleted, not under a
  blocked organisation), kept by triggers on clubs and organizations, is in
  every public rule: discover's lists, pages and sitemap, For You, threads,
  the live room, the app's club list and club page. A blocked organisation's
  clubs and events were public everywhere but the app's club list and home.
- **Addresses.** Thread views, link visitors and event page views take the
  address the proxy saw (`app.core.net.client_address`), not the first
  `X-Forwarded-For` hop; anonymous visitors are an HMAC of address and browser
  under a key that changes daily, not `sha256(ip|ua)`.
- **The app's club list** reads `clubs.follower_count` and an EXISTS on the
  person's own row (it joined every follow of every club and counted them,
  with a DISTINCT total over the join); its total stops at 1,000 ("1,000+",
  the next page always offered). `/clubs/info` counts events once and reads
  the counter. Follow, pin and hide are upserts on the unique (club, user,
  deleted). A club's followers page by cursor on `ix_club_users_followers`.
  New: `ix_clubs_listed_popular`, `ix_clubs_listed_newest`.
- **Threads.** The follower's feed is a LATERAL top-k per followed club on
  `ix_club_threads_club_newest`, merged, with a cursor and a capped total;
  comments page by cursor on `ix_thread_comments_thread_created` (200 a page,
  `X-Next-Cursor`); insights count reactions by week in SQL; followers are
  told after the post is answered, 1,000 at a time; a comment is hidden when
  its open reports weigh `REPORTS_TO_HIDE` (3), a report from an account under
  a week old weighing half.
- **Discover.** `offset` stops at 1,000; `after=` (each list's `next_cursor`)
  pages events (every sort, distance included), clubs and threads as ranges
  of their indexes. Every word of a query must match (an AND of per-word
  ORs; the typo fallback too, per word); a word of two letters matches a
  whole word, a single character is ignored; with words the default order is
  relevance. `events.popularity` (attention decayed by age: `ln(1 + going +
  views) + day_made / 30`, the ranking of attention × e^(−age/30 days)),
  rewritten every three minutes for what changed and indexed over what can be
  shown (`ix_events_shown_popular`), is what "popular" sorts by, on the site
  and in For You's candidates. The caches serve a value for a minute past its
  time while one task rebuilds it (stale while revalidate), each family has
  its own capacity, the calendar is kept per quarter-degree cell. "Near you"
  reads its box in date order in growing chunks and stops when settled. The
  map-link scan reads `link_checked_at` through a partial index with a LIMIT
  (it read every row wanting a link every minute). The address lookup is
  never waited for when the browser gave a point, one request per address at
  a time, ten minutes of none after the provider refuses one, and a local
  GeoLite2 database when `GEOIP_MMDB_PATH` names one. "Cities" counts places
  on the map within 25 km of each other as one city (it counted venue
  strings). For You shows at most three of one club in its first twenty.
- **The survey's "send now"** claims its recipients in `feedback_sends`, so
  sending again reaches only the guests not sent it yet (it re-sent to
  everyone, its docstring notwithstanding), and the mails go after the
  commit; the minute sweep reads due surveys by `feedback_surveys.due_at`
  (kept by triggers) on `ix_feedback_surveys_due`.
- **Tracking links.** The dashboard's figures are counted by the database,
  grouped by link and by day in India from the period's start (it loaded
  every click, follow and registration); link previews, crawlers and HEAD
  requests are not clicks; `/l/{code}` is limited per address (1,200 a
  minute); `ix_tracking_clicks_link_created` is declared on the model.

### The measurements

**Data**: `seed.py --clubs 15000 --upcoming 120000 --past 60000 --users 60000
--registrations 600000 --follows 300000 --threads 20000` (0.68 GB), seeded
with the code before (`perf/discover-scale-2` at c146433, schema
`discover_scale`), measured, then `alembic upgrade head` with this branch and
measured again. The room: `live.py setup --voters 5000 --players 2000` on one
of its events (120 questions on the board with 9,881 votes, a live poll with
2,500 answers, ten updates, a running quiz of 2,000 players with the open
question half answered). **Machine**: the M2 laptop, shared again (load
average 6–8 throughout: three other engineers' test suites). Before and
after ran back to back; the server is `serve.py`, one worker, access log on,
as production.

**The live room**, closed loop, 20 s per level after 3 s of warm-up, the
load spread over 3–4 client processes. "Serves" is what that rate sustains
at the clients' own polling: a phone every 5 s (the event site's live.jsx),
a player every 2 s (play.jsx).

| | clients | req/s | p50 / p95 ms | serves | statements a poll |
|---|---:|---:|---|---:|---:|
| room feed, before | 32 | 44.8 | 663 / 1,553 | 224 phones | 7–8 (one reads all 2,500 poll answers as rows) |
| | 128 | 30.9 | 3,921 / 7,847 | 154 phones | |
| room feed, after | 32 | 538 | 48 / 123 | 2,690 phones | 0 |
| | 64 | 545–558 | 92 / 230–241 | 2,720–2,790 phones | |
| | 128 | 446 | 180 / 768 | 2,230 phones | |
| quiz poll, before | 32 | 261 | 117 / 156 | 522 players | 7 (board sorted, rank counted) |
| | 128 | 274 | 456 / 1,123 | 548 players | |
| quiz poll, after | 32 | 661 | 44 / 72 | 1,320 players | 0 |
| | 64 | 688 | 86 / 145 | 1,376 players | |

Both before and after, the worker's one core is the limit (21–23 s of CPU in
20 s); Postgres used 1–4 s. Before, that core went to building each feed
(2,500 ORM rows of poll answers and 120 question rows, a model, the JSON) and
to the quiz's seven statements; after, it goes to the request itself: the
middleware stack, routing, the access log, about 1.5–1.8 ms a poll. So one
worker now holds a room of about 2,700 phones (was about 220) and a quiz of
about 1,350 players (was about 520). A 5,000-person room or a 2,000-player
quiz needs a second worker (each keeps its own second-long snapshots) or a
slower poll: what is left per poll is the HTTP request.

**What server-sent events would add** (not built): one connection per
phone, with the snapshot pushed when it changes (the room) or once a second
(the quiz board), instead of a request per phone per poll. The per-poll
request cost above, the part one worker cannot get under, goes; a room is
then bounded by sockets held open and bytes sent (34 KB a room snapshot;
diffs would cut it). It needs the snapshot published across workers (Redis
pub/sub, or one process owning the room) and proxies that keep the stream
open (nginx's `proxy_buffering off`, the gateway's timeouts).

**"Popular" and the front page**, in process (`run.py --buffers`), p50 / p95
over 30 requests and the pages (buffers) one request touches:

| endpoint | before ms | pages | after ms | pages |
|---|---|---:|---|---:|
| `/discover/events?sort=popular` | 59.7 / 83.4 | 12,542 | 9.0–21.6 / 13.1–29.1 | 2,852–2,955 |
| the front page's "popular" section (one statement, EXPLAIN) | 78.0 | 22,827 | 0.18 | 113 |
| `/discover/clubs` (most followed) | 17.0 / 18.8 | 3,458 | 7.9–14.6 / 11.8–20.1 | 807 |
| For You, cold (the minute's popular lists made) | 184 / 234 | 36,950 | 31.8–65.8 / 46.7–87.6 | 18,575–18,623 |
| front page with a point, cold: "near you" (one statement, EXPLAIN) | 21.4 | 8,197 | 8.2 | 5,341 |
| `/discover/events?q=ai` | 20.3 / 27.6 | 6,874 | 142 / 170 | 27,896 |

The popular sort walks `ix_events_shown_popular` and stops at the page; it
sorted every event on (U of them) for each page. Its cost no longer grows
with U. "Near you" drops the per-event club lookup (the rule is "not one of
the unlisted clubs", a hashed list from `ix_clubs_unlisted`); in this world
the planner still reads the whole box in the first chunk, so the chunked,
date-ordered read only stops early where the planner walks the date order.
`q=ai` costs more on purpose: a two-letter word is now a whole word, so it
no longer matches "Chair" or "Mumbai", and the rarer match reads further
into what is on (the capped count and, with words, the relevance order
that is now the default). `sort=relevance` itself is unchanged (29,083 →
29,093 pages).

### The migration

`discover_integrity` on this data (15,000 clubs, 180,000 events, 600,000
registrations, 300,000 follows, 20,000 posts with 120,000 likes and 40,000
comments): **21.6 s**, of which the backfills (listed for every club and a
vacuum, every post's counters recounted, popularity for 114,691 shown
events) and fourteen concurrent index builds are nearly all. Locks: the
first transaction (columns without a rewrite, the table, functions and
triggers) is catalogue-only and short; each backfill holds the rows it
rewrites until it commits; the indexes block nothing. At a lakh of clubs
the popularity backfill and the index builds on events scale with the shown
events (about a minute at S2's 300,000 upcoming, by the per-row cost here).

### What is left

- The live room's snapshots, the discover caches and the view buffers are
  per process: with more workers, each makes its own (once a second for a
  room, once a minute for a front page). Redis would make them once.
- Server-sent events for the room (above).
- A rare word, and now a two-letter one, still reads every event on in the
  substring pass; a search engine (SCALING.md, Phase 2) bounds it.
- Views are lost if a process dies with up to 5 s of them unwritten.

### Reproduce

```
cd backend
git archive c146433 backend | tar -x -C /tmp/before                  # the code before
createdb moa_bench_live
(cd /tmp/before/backend && BENCH_DB=moa_bench_live python -m scripts.bench.seed --clubs 15000 --upcoming 120000 \
    --past 60000 --users 60000 --registrations 600000 --follows 300000 --threads 20000)
BENCH_DB=moa_bench_live python -m scripts.bench.live setup --voters 5000 --players 2000 --out room.json
(cd /tmp/before/backend && BENCH_DB=moa_bench_live python -m scripts.bench.serve --workers 1 --port 8093 --stats-dir /tmp/sb &)
BENCH_DB=moa_bench_live python -m scripts.bench.live room --url http://127.0.0.1:8093 --room room.json \
    --concurrency 32,64,128 --duration 20 --procs 4 --stats-dir /tmp/sb --server-pid <pid> --label before --out live.json
#   ... quiz the same; then kill -9 whatever listens on 8093
KEYS="events popular,home (cold),home lat/lng (cold),clubs,events q,events q short,events q relevance,app for-you (cold)"
(cd /tmp/before/backend && BENCH_DB=moa_bench_live python -m scripts.bench.run --label before --freeze --n 30 \
    --budget 6 --min 3 --warmup 1 --exact "$KEYS" --buffers --out before.json)
BENCH_DB=moa_bench_live python -c "from scripts.bench import _env; from alembic.config import Config; \
    from alembic import command; command.upgrade(Config('alembic.ini'), 'head')"
#   ... the same runs with this branch's serve.py and run.py
dropdb moa_bench_live
```

Raw results: `results/integrity/` (the live runs, both read runs, the seed's sizes).

## Search and geo, Wave 2 (`feat/w2-search`, 18 October 2026)

### What changed

- **search_text** on events, clubs and users: everything a row is found by,
  lowercased and unaccented, in one column under one trigram index, kept by
  triggers (`app/api/search/text.py`). An event is found by its interests,
  topics, tag and its club's city as well as its name, place, club and
  category. Discover's searches, the app's lists and the admin's user search
  test each word against it: one LIKE (a typo: one `%>`) on one column,
  where it was an OR over four columns of three tables.
- **Grid cells** (`app/api/discover/cells.py`): `events.geocell` and
  `clubs.geocell`, the 0.5° cell of an in-person event's point or a club's
  place, kept by triggers, indexed with the date
  (`ix_events_shown_cell (geocell, event_datetime, id)`). "Near you" reads the
  cells that cover the radius -- an IN over 4-16 for 50 km, ranges further
  out, the old box only past that -- then the exact haversine.
- **Meilisearch**, off unless `APP_SEARCH_URL` is set (`app/core/search.py`,
  `app/api/search/`): a transactional outbox, drained on one process; the
  indexes built under another name and swapped in; a six-hourly reconcile;
  searches with words routed to it behind a circuit breaker, every page read
  back under Postgres's own conditions. Not measured here (no instance on
  this machine); its correctness against Postgres is a test
  (`tests/test_search_routing.py`, also runnable against a real instance).

### The measurements

**Data**: `seed.py --clubs 100000 --upcoming 300000 --past 200000 --users
100000 --registrations 300000 --follows 300000 --threads 3000` (1.36 GB),
seeded with master (39f6cc7, schema `topics`), measured, then `w2_search`
applied in place and measured again. `clubs.listed` set first: the seeder
left it false for every club (it loads with triggers off), so every public
list was empty -- `seed.py` now sets it, and search_text and the cells. After
the migration had rewritten every row, the tables were compacted (`VACUUM
FULL`) so that both sides read tables of their natural size. **Machine**: the
M2 laptop with 8 GB, shared with four other builders' suites and
benchmarks (load 5-8, swapping): times swing by tens of times between runs,
so the pages each call touches (shared buffers, from EXPLAIN (ANALYZE,
BUFFERS) of the call's own statements) are the measure; times are p50.

**"Near you"**, in process (`near.py`), master's code and this branch's on
the same data, back to back; p50 ms / pages:

| place | call | before ms | pages | after ms | pages |
|---|---|---:|---:|---:|---:|
| Bengaluru | front page "near you" pool | 4,090 | 18,151 | 46.6 | 11,473 |
| Mumbai | front page "near you" pool | 3,945 | 16,192 | 34.0 | 13,588 |
| Kochi | front page "near you" pool | 15.9 | 12,456 | 32.4 | 12,118 |
| Leh, Port Blair (nothing near) | front page "near you" pool | 1.6-2.8 | 5-58 | 2.4 | 36 |
| Mumbai | events within 50 km, by distance | 346 | 57,622 | 65.3 | 15,698 |
| Kochi | events within 50 km, by distance | 56.8 | 33,394 | 84.2 | 12,078 |
| Bengaluru | events within 50 km, by distance | 101 | 17,767 | 69.1 | 17,393 |
| any | events within 50 km, by date | 23-91 | 4,353-11,639 | 20-58 | 4,316-7,077 |
| any | clubs near (pool, radius, nearest first) | 15-56 | 6,028-13,972 | 19-71 | 7,409-14,848 |

Of the "near you" pool's pages, 6,000-7,000 are the list of unlisted clubs
(`NOT IN`, read from `ix_clubs_unlisted` and the clubs' rows), the same
before and after; the events' own part went from 11,000-12,000 pages to
about 4,000-5,000. The plans (Bengaluru):

```
before: Bitmap Heap Scan on events  (rows=15867)          -- every event in the latitude strip
          -> Bitmap Index Scan on ix_events_shown_point  (rows=17199)
        Sort (top-N)  -> Limit 64                        25,158 buffers
after:  Nested Loop over 12 cells
          -> Limit 64 -> Index Scan using ix_events_shown_cell  (rows=21 loops=12)
               Index Cond: geocell = cell AND event_datetime >= since
        Sort (top-N) -> Limit 64                         11,498 buffers, 6,400 of them the unlisted clubs
```

What it shows: the latitude strip was the cost where a city sits in a busy
band (Mumbai, Kochi: three to four times the pages for distance) and in the
front page's pool, which read every event to come in the strip to keep the
first dozen; a cell walk reads a chunk per cell. Where little is near (Leh,
Port Blair) the strip was already empty, and both are a few pages. The date
order within a radius and the clubs near a point cost the same: they were
already bounded by their own walks, and the clubs' box already sat on
`ix_clubs_place`. The cell walk needs bitmap scans off for its statements
(`SET LOCAL`, then `RESET`): the planner, unable to see how few of a cell's
rows fail the filters, expected 7 rows a cell where there were 1,318 and
fetched all of them to sort (18,643 heap pages). Testing the rows after
reading them needs no setting, but reads past every row a test fails --
events over and not yet closed by the status clock, which had not run on
this data -- and took 25,000-32,000 pages.

**Words**, through the API (`run.py --buffers`); before is master on the
data as seeded, after is this branch after the migration and the compaction:

| endpoint | before ms | pages | after ms | pages |
|---|---:|---:|---:|---:|
| `/discover/events?q=robotics kochi` | 500 | 83,368 | 25.2 | 6,865 |
| `/discover/events?q=sculpture showcase` (rare words) | 1,982 | 84,667 | 35.8 | 8,845 |
| `/discover/calendar?q=robotics` (cold) | 180 | 41,673 | 47.1 | 31,696 |
| `/discover/events?q=robtics` (typo) | 543 | 12,089 | 404 | 29,957 |
| `/discover/events?q=bengaluru` | 341 | 41,579 | 113 | 91,761 |
| `/discover/events?q=robotics` (relevance) | 782 | 48,922 | 1,551 | 78,227 |
| `/discover/events?q=ai` | 2,325 | 92,408 | 2,207 | 105,892 |
| `/events/list?search=robotics` (the app) | 38 | 7,544 | 10,860 | 28,592 |
| `/discover/clubs?q=robotics` | 10.5 | 3,587 | 11.7 | 1,580 |
| `/clubs/list?search=robotics` (the app) | 26.9 | 4,184 | 20.3 | 3,609 |

Two words and rare words are where one column pays: every word is a range of
one trigram index and the AND narrows inside it (12 times fewer pages, 20-55
times faster). A common word now matches more -- "robotics" is in 31,968
events' search_text (their interest is Robotics) against 8,061 names -- and
Postgres's cost for a ranked or date-ordered page of a common word is the
cost of its matches: the relevance score of each, or the sort. That is what
Meilisearch is for (it answers a page of a common word without visiting
every match), and why it is wired in now; until it is on, those searches
cost about as many pages more as they find more rows. (The app list's 10.9 s
is this machine reading 28,592 pages from a disk shared with four other
suites; its plan is master's -- a bitmap of the word's rows, a top-N sort.)

### The migration

`w2_search` on this data, with a writer updating random events and clubs
every 10 ms throughout (51,313 writes): **31 minutes**, 0 failed writes,
writes p50 5.6 ms, p99 179 ms. The time is the backfill rewriting 500,000
events, 100,000 clubs and 200,000 users: 1.75 ms an event row on this
machine at that moment, where a bare `UPDATE events SET popularity =
popularity` of the same rows took 1.4 ms (the row and its twenty indexes);
computing the text costs 0.27 ms of it. Production has a few thousand events:
seconds. The first transaction (columns without a default, the outbox table,
functions, triggers) holds its locks for well under a second, each under
`lock_timeout 3s`; the eight index builds and drops are concurrent.

148 writes waited over a second (one 20 s): every one of them, in a rerun on
20,000 events, had touched a row inside the batch then being rewritten,
which holds its rows until it commits -- 2,000 rows were 3.5-29 s on this
machine. The backfill now takes exact rows and sizes each batch to take
about 0.2 s; on the same slice, with the writer aimed at it: writes p99 27 s
→ 65 ms, writes over 250 ms 3% → 0.2%, batch p50 0.2 s (one stalled 12 s on
the shared disk).

### What is left

- Meilisearch measured against a real instance (none here; Docker was off
  and the disk short): the parity battery runs against one with
  `MEILI_TEST_URL`.
- The unlisted-clubs list read by every "near you" chunk (6,000 pages at a
  lakh of clubs): a `NOT IN` over `ix_clubs_unlisted` that the session's
  soft-delete filter turns into a heap read.
- A common word through Postgres costs its matches (above).

### Reproduce

```
cd backend
git worktree add --detach /tmp/master 39f6cc7                         # the code before
createdb moa_w2s_bench
(cd /tmp/master/backend && BENCH_DB=moa_w2s_bench python -m scripts.bench.seed --clubs 100000 --upcoming 300000 \
    --past 200000 --users 100000 --registrations 300000 --follows 300000 --threads 3000 --likes 3000 --comments 3000)
psql moa_w2s_bench -c "UPDATE clubs SET listed = listed"               # what master's seeder leaves out
cp scripts/bench/run.py scripts/bench/near.py /tmp/master/backend/scripts/bench/
ONLY="home lat/lng,events lat/lng,events distance,clubs nearby,clubs r=50,events q,clubs q,app list search,app clubs search,calendar q"
(cd /tmp/master/backend && BENCH_DB=moa_w2s_bench python -m scripts.bench.run --label before --only "$ONLY" --buffers --freeze --out before.json)
BENCH_DB=moa_w2s_bench python -c "from scripts.bench import _env; from alembic.config import Config; \
    from alembic import command; command.upgrade(Config('alembic.ini'), 'head')"
psql moa_w2s_bench -c "VACUUM FULL ANALYZE events" -c "VACUUM FULL ANALYZE clubs" -c "VACUUM FULL ANALYZE users"
(cd /tmp/master/backend && BENCH_DB=moa_w2s_bench python -m scripts.bench.near --label before --n 8 --out near_before.json)
BENCH_DB=moa_w2s_bench python -m scripts.bench.near --label after --n 8 --out near_after.json
BENCH_DB=moa_w2s_bench python -m scripts.bench.run --label after --only "$ONLY" --buffers --freeze --out after.json
dropdb moa_w2s_bench
```
