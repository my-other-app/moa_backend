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

Full plans: `results/before_plans.txt` (the two slowest statements of each
endpoint, old code; identical SQL to "before") and `results/after_plans.txt`
(every statement, new code).

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
