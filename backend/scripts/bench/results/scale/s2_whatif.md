# What the proposed query fixes buy, measured on S2 (psql, EXPLAIN ANALYZE)

S2: 100,000 clubs, 300,000 upcoming + 1,000,000 past events, 15,000,000 registrations (database 9.67 GiB).
Postgres 16 with its defaults (shared_buffers 128 MB), on the same laptop as the rest of the study.

| query | as the app runs it | with the fix | fix |
|---|---:|---:|---|
| event page "similar" (4 upcoming events in the category, date order) | 432 ms warm, 3.4 s cold; 29,664 rows read and thrown away | 1.9 ms; 42 thrown away | add `event_datetime >= now() - 72 h` (the longest event) so the walk of `ix_events_shown_start` starts near now instead of at the oldest event still marked published |
| first page of /discover/events (24 by date) | 102 ms; 29,480 thrown away | 0.1 ms; 87 thrown away | the same lower bound |
| total for /discover/events (every visible upcoming event counted) | 2.7–3.1 s (parallel), 5.0 s (one process): the whole 0.8 GB events table is read | 3.4 ms warm (156 ms cold) for "1,000+" | count at most 1,001 rows (`LIMIT 1001` in a subquery) and show "1,000+"; or keep exact counts per filter in the shared cache for a minute |
| duplicate-registration check under the event's row lock, at 29,000 registrations | 15.9 ms warm, 153 ms cold (29,000 rows filtered) | (index lookup, < 0.1 ms) | split `user_id = ? OR email = ?` into two indexed lookups and add `(event_id, lower(email)) WHERE NOT is_deleted` |
| capacity count under the lock (capped events) | 3.7 ms at 29,000 (index-only) | 0 (read `going_count` from the locked row) | use the trigger-kept counter |
