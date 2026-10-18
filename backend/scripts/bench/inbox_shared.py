"""The shared inbox (INBOX_SHARED, app/core/notifications/inbox.py) at size:
a reader who follows 200 clubs, against a lakh of broadcasts -- and what the
rows per recipient it replaces cost, to write and to read.

    /opt/homebrew/opt/postgresql@16/bin/createdb moa_bench_push
    BENCH_DB=moa_bench_push python -c "import scripts.bench._env; from alembic.config import Config; \
        from alembic import command; command.upgrade(Config('alembic.ini'), 'head')"
    BENCH_DB=moa_bench_push python -m scripts.bench.inbox_shared --layout spread --out scripts/bench/results/push/spread.json
    BENCH_DB=moa_bench_push python -m scripts.bench.inbox_shared --layout worst --out scripts/bench/results/push/worst.json

On a scratch database at alembic head; the tables it fills are emptied first.

1. Seeds 2,000 clubs and --people (100,000) people. Every one follows the
   big club (a lakh of followers) and four others; the reader follows 200
   clubs and picked six interests under three of 30 topics. --broadcasts
   (100,000) shared club broadcasts over the last year: `spread` puts them
   over all 2,000 clubs (10,000 on the reader's 200), `worst` all on the
   reader's 200 (500 a club). Plus 5,000 by topic. The reader has 300 rows
   of their own and has read 2,000 broadcasts; everyone else has 2 rows and
   read 1 broadcast. ANALYZE.
2. For the reader, EXPLAIN (ANALYZE, BUFFERS) and the time over --runs runs
   (p50, p95) of what the inbox sends: the first page, a page by cursor
   1,000 lines down, a page by offset 100 (the app's), the unread count
   with nothing read all (its worst) and after "read all", read-all's
   newest-line read, and the "can see" probe of a mark-read. Then the whole
   call (inbox.page: the SQL and its ORM loads) on asyncpg.
3. What it replaces: the reader's broadcasts as rows of their own (10,000 or
   a lakh), and the per-recipient inbox's first page and unread count over
   them; then the write of one broadcast to the big club's lakh of
   followers as the INSERT ... SELECT the fan-out sends, against announce's
   one row and one target (both rolled back).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid

from scripts.bench import _env  # noqa: F401  (settings before app)

CLUBS = 2000
TOPICS = 30
READER_CLUBS = 200


def _ms(samples: list[float]) -> dict:
    s = sorted(samples)
    return dict(p50=round(statistics.median(s) * 1000, 3), p95=round(s[int(len(s) * 0.95) - 1] * 1000, 3), runs=len(s))


def seed(c, *, people: int, broadcasts: int, layout: str) -> int:
    """Fills the tables in bulk; returns the reader's id."""
    x = c.execute
    # The follower-count and other triggers, and the foreign keys, are off
    # while seeding: a lakh of follows of one club is otherwise a lakh of
    # updates to one row in one transaction.
    x("SET session_replication_role = replica")
    for t in ("notification_broadcast_states", "notification_broadcast_targets", "notification_broadcasts", "notification_settings",
              "notifications", "user_interests", "club_users_link", "user_device_tokens"):
        x(f"TRUNCATE {t} CASCADE")
    x("DELETE FROM interests")
    x("DELETE FROM interest_categories")
    x("DELETE FROM clubs")
    x("DELETE FROM users")
    x("""INSERT INTO users (id, full_name, username, email, user_type, created_at, updated_at, is_deleted)
         SELECT g, 'Person ' || g, 'p' || g, 'p' || g || '@example.com', 'app_user', now() - interval '2 years', now(), false
         FROM generate_series(1, %s) g""", (people,))
    x("""INSERT INTO clubs (id, slug, name, rating, total_ratings, is_verified, created_at, updated_at, is_deleted)
         SELECT g, 'club-' || g, 'Club ' || g, 0, 0, true, now() - interval '2 years', now(), false FROM generate_series(1, %s) g""", (CLUBS,))
    x("""INSERT INTO interest_categories (id, name, slug, created_at, updated_at, is_deleted)
         SELECT g, 'Topic ' || g, 'topic-' || g, now(), now(), false FROM generate_series(1, %s) g""", (TOPICS,))
    x("""INSERT INTO interests (id, name, slug, category_id, created_at, updated_at, is_deleted)
         SELECT g, 'Interest ' || g, 'interest-' || g, 1 + (g %% %s), now(), now(), false FROM generate_series(1, %s) g""", (TOPICS, TOPICS * 4))
    reader = 1
    # Club 1 is the big club: everyone follows it. Each person follows four
    # more; the reader follows clubs 1..200, from 400 days ago.
    x("""INSERT INTO club_users_link (club_id, user_id, is_following, is_pinned, is_hidden, is_muted, created_at, updated_at, is_deleted)
         SELECT 1, g, true, false, false, false, now() - interval '400 days', now(), false FROM generate_series(2, %s) g""", (people,))
    x("""INSERT INTO club_users_link (club_id, user_id, is_following, is_pinned, is_hidden, is_muted, created_at, updated_at, is_deleted)
         SELECT DISTINCT 2 + ((g * 7919 + k * 104729) %% (%s - 1)), g, true, false, false, false, now() - interval '400 days', now(), false
         FROM generate_series(2, %s) g, generate_series(1, 4) k""", (CLUBS, people))
    x("""INSERT INTO club_users_link (club_id, user_id, is_following, is_pinned, is_hidden, is_muted, followed_at, created_at, updated_at, is_deleted)
         SELECT g, %s, true, false, false, false, CASE WHEN g %% 2 = 0 THEN now() - interval '400 days' END, now() - interval '400 days', now(), false
         FROM generate_series(1, %s) g""", (reader, READER_CLUBS))
    # Interests: everyone two, the reader six under three topics.
    x("""INSERT INTO user_interests (user_id, interest_id, created_at, updated_at, is_deleted)
         SELECT DISTINCT g, 1 + ((g * 31 + k * 17) %% %s), now() - interval '400 days', now(), false
         FROM generate_series(2, %s) g, generate_series(1, 2) k""", (TOPICS * 4, people))
    x("""INSERT INTO user_interests (user_id, interest_id, created_at, updated_at, is_deleted)
         SELECT %s, i, now() - interval '400 days', now(), false FROM unnest(ARRAY[1, 31, 2, 32, 3, 33]) i""", (reader,))
    # The broadcasts: club ones over the last year, then 5,000 by topic.
    club_of = f"1 + (g %% {CLUBS if layout == 'spread' else READER_CLUBS})"
    x(f"""CREATE TEMP TABLE b AS
          SELECT gen_random_uuid() AS id, g, {club_of} AS club_id, now() - (g * interval '1 second' * 31536000 / %s) AS at
          FROM generate_series(1, %s) g""", (broadcasts, broadcasts))
    x("""INSERT INTO notification_broadcasts (id, push_id, type, title, description, from_club_id, data, audience, shared, created_at, updated_at)
         SELECT id, 'club_thread:' || id, 'club_thread', 'Club ' || club_id || ' posted', 'A post', club_id,
                json_build_object('type', 'club_thread', 'thread_id', id, 'club_id', club_id), jsonb_build_object('club', club_id), true, at, at FROM b""")
    x("INSERT INTO notification_broadcast_targets (broadcast_id, target, created_at) SELECT id, 'club:' || club_id, at FROM b")
    x("""CREATE TEMP TABLE tb AS
          SELECT gen_random_uuid() AS id, g, 1 + (g %% %s) AS topic_id, 1 + (g %% %s) AS club_id, now() - (g * interval '1 second' * 31536000 / 5000) AS at
          FROM generate_series(1, 5000) g""", (TOPICS, CLUBS))
    x("""INSERT INTO notification_broadcasts (id, push_id, type, title, description, from_club_id, data, audience, shared, created_at, updated_at)
         SELECT id, 'nearby_event:' || id, 'nearby_event', 'Event in Topic ' || topic_id, 'New event', club_id,
                json_build_object('event_name', 'An event'), jsonb_build_object('topics', jsonb_build_array(topic_id), 'except_club', club_id), true, at, at FROM tb""")
    x("INSERT INTO notification_broadcast_targets (broadcast_id, target, created_at, except_club_id) SELECT id, 'topic:' || topic_id, at, club_id FROM tb")
    # Rows of their own, and read states.
    x("""INSERT INTO notifications (id, user_id, title, description, type, status, created_at, updated_at, is_deleted)
         SELECT gen_random_uuid(), %s, 'Yours ' || g, 'd', 'event_reminder', CASE WHEN g %% 3 = 0 THEN 'unread' ELSE 'read' END::notificationstatus,
                now() - (g * interval '1 day'), now(), false FROM generate_series(1, 300) g""", (reader,))
    x("""INSERT INTO notifications (id, user_id, title, description, type, status, created_at, updated_at, is_deleted)
         SELECT gen_random_uuid(), u, 'Theirs', 'd', 'event_registered', 'unread', now() - (k * interval '3 days'), now(), false
         FROM generate_series(2, %s) u, generate_series(1, 2) k""", (people,))
    x("""INSERT INTO notification_broadcast_states (user_id, broadcast_id, read_at)
         SELECT %s, id, now() FROM b WHERE club_id <= %s AND g %% 3 = 0 LIMIT 2000""", (reader, READER_CLUBS))
    x("""INSERT INTO notification_broadcast_states (user_id, broadcast_id, read_at)
         SELECT DISTINCT ON (u) u, b.id, now() FROM generate_series(2, %s) u JOIN b ON b.g = 1 + (u %% %s)""", (people, broadcasts))
    x("SET session_replication_role = origin")
    for t in ("users", "clubs", "club_users_link", "user_interests", "interests", "notifications", "notification_broadcasts",
              "notification_broadcast_targets", "notification_broadcast_states"):
        x(f"VACUUM ANALYZE {t}")
    return reader


def explain_and_time(c, sql: str, params: dict, runs: int) -> dict:
    from sqlalchemy import text

    plan = "\n".join(r[0] for r in c.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params).all())
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        c.execute(text(sql), params).all()
        samples.append(time.perf_counter() - t0)
    return dict(plan=plan, **_ms(samples))


async def app_call(reader: int, runs: int) -> dict:
    from app.core.notifications import inbox
    from app.db import registry  # noqa: F401  (every model, for the relationships)
    # The app's own engine: its pool and its plan_cache_mode (force_custom_plan).
    from app.db.core import AsyncSessionLocal, engine

    out = {}
    async with AsyncSessionLocal() as s:
        for name, kwargs in (("page_1", {}), ("offset_100", {"offset": 100})):
            samples = []
            for _ in range(runs):
                t0 = time.perf_counter()
                lines = await inbox.page(s, reader, limit=21, **kwargs)
                samples.append(time.perf_counter() - t0)
            out[name] = dict(lines=len(lines), **_ms(samples))
        samples = []
        for _ in range(runs):
            t0 = time.perf_counter()
            n = await inbox.shared_unread(s, reader)
            samples.append(time.perf_counter() - t0)
        out["unread"] = dict(count=n, **_ms(samples))
    await engine.dispose()
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--people", type=int, default=100_000)
    p.add_argument("--broadcasts", type=int, default=100_000)
    p.add_argument("--layout", choices=("spread", "worst"), default="spread")
    p.add_argument("--runs", type=int, default=50)
    p.add_argument("--reuse", action="store_true", help="measure the data the last run seeded")
    p.add_argument("--out")
    args = p.parse_args()

    from sqlalchemy import create_engine, text

    from app.config import settings
    from app.core.notifications import inbox

    engine = create_engine(settings.DATABASE_URL_SYNC, isolation_level="AUTOCOMMIT")
    t0 = time.perf_counter()
    with engine.connect() as c:
        raw = c.connection.cursor()
        reader = 1 if args.reuse else seed(raw, people=args.people, broadcasts=args.broadcasts, layout=args.layout)
    seeded = time.perf_counter() - t0
    result: dict = dict(layout=args.layout, people=args.people, broadcasts=args.broadcasts, seeded_s=round(seeded, 1))
    with engine.connect() as c:
        sizes = c.execute(text("""SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables
                                  WHERE relname IN ('notifications', 'notification_broadcasts', 'notification_broadcast_targets',
                                                    'notification_broadcast_states', 'club_users_link', 'user_interests')""")).all()
        result["tables"] = {r[0]: dict(rows=r[1], size=r[2]) for r in sizes}
        visible = c.execute(text("SELECT count(*) FROM notification_broadcast_targets WHERE target = ANY(ARRAY(SELECT 'club:' || g FROM generate_series(1, :n) g))"), dict(n=READER_CLUBS)).scalar()
        result["reader_club_broadcasts"] = visible
        uid = dict(uid=reader)
        cursor_at = c.execute(text(inbox._page_sql(False).text), dict(uid=reader, k=1000)).all()[-1]
        q = {}
        q["page_1"] = explain_and_time(c, inbox._page_sql(False).text, dict(uid=reader, k=21), args.runs)
        q["page_by_cursor_1000_down"] = explain_and_time(c, inbox._page_sql(True).text, dict(uid=reader, k=21, c_at=cursor_at.created_at, c_id=cursor_at.id), args.runs)
        q["page_by_offset_100"] = explain_and_time(c, inbox._page_sql(False).text, dict(uid=reader, k=121), args.runs)
        q["unread_nothing_read_all"] = explain_and_time(c, inbox._UNREAD.text, dict(uid=reader, cap=inbox.UNREAD_CAP), args.runs)
        q["newest_for_read_all"] = explain_and_time(c, inbox._NEWEST.text, uid, args.runs)
        any_id = c.execute(text("SELECT broadcast_id FROM notification_broadcast_targets WHERE target = 'club:7' LIMIT 1")).scalar()
        q["can_see"] = explain_and_time(c, inbox._CAN_SEE.text, dict(uid=reader, bid=any_id), args.runs)
        c.execute(text("INSERT INTO notification_settings (user_id, read_through) VALUES (:uid, now() - interval '7 days') ON CONFLICT (user_id) DO UPDATE SET read_through = excluded.read_through"), uid)
        q["unread_after_read_all_a_week_ago"] = explain_and_time(c, inbox._UNREAD.text, dict(uid=reader, cap=inbox.UNREAD_CAP), args.runs)
        c.execute(text("DELETE FROM notification_settings WHERE user_id = :uid"), uid)
        result["shared"] = q
    result["app_call"] = asyncio.run(app_call(reader, args.runs))

    # What it replaces: the same broadcasts as rows of the reader's own.
    with engine.connect() as c:
        c.execute(text("""INSERT INTO notifications (id, user_id, title, description, type, from_club_id, status, data, created_at, updated_at, is_deleted)
                          SELECT gen_random_uuid(), :uid, b.title, b.description, b.type, b.from_club_id, 'unread', b.data, b.created_at, b.updated_at, false
                          FROM notification_broadcasts b JOIN notification_broadcast_targets t ON t.broadcast_id = b.id
                          WHERE t.target = ANY(ARRAY(SELECT 'club:' || g FROM generate_series(1, :n) g))"""), dict(uid=reader, n=READER_CLUBS))
        c.execute(text("VACUUM ANALYZE notifications"))
        old_page = "SELECT * FROM notifications WHERE user_id = :uid AND NOT is_deleted ORDER BY created_at DESC LIMIT :k OFFSET :o"
        old_unread = "SELECT count(id) FROM notifications WHERE user_id = :uid AND status = 'unread' AND NOT is_deleted"
        result["per_recipient"] = dict(
            page_1=explain_and_time(c, old_page, dict(uid=reader, k=21, o=0), args.runs),
            page_by_offset_100=explain_and_time(c, old_page, dict(uid=reader, k=21, o=100), args.runs),
            unread=explain_and_time(c, old_unread, dict(uid=reader), args.runs),
        )
        c.execute(text("DELETE FROM notifications WHERE user_id = :uid AND type = 'club_thread'"), dict(uid=reader))

    # The write: one broadcast to the big club's lakh of followers, each in
    # a transaction rolled back.
    with create_engine(settings.DATABASE_URL_SYNC).connect() as c:
        followers = c.execute(text("SELECT count(*) FROM club_users_link WHERE club_id = 1 AND is_following AND NOT is_deleted")).scalar()
        c.rollback()
        fan_out = """INSERT INTO notifications (id, user_id, title, description, type, from_club_id, status, data, is_deleted, created_at, updated_at)
                     SELECT gen_random_uuid(), user_id, 'Club 1 posted', 'A post', 'club_thread', 1, 'unread', '{"type": "club_thread"}'::json, false, now(), now()
                     FROM club_users_link WHERE club_id = 1 AND is_following AND NOT is_deleted AND NOT is_muted"""
        shared_write = """WITH b AS (
                              INSERT INTO notification_broadcasts (id, push_id, type, title, description, from_club_id, data, audience, shared, created_at, updated_at)
                              VALUES (:id, :pid, 'club_thread', 'Club 1 posted', 'A post', 1, '{"type": "club_thread"}'::json, '{"club": 1}'::jsonb, true, now(), now())
                              RETURNING id, created_at)
                          INSERT INTO notification_broadcast_targets (broadcast_id, target, created_at) SELECT id, 'club:1', created_at FROM b"""
        writes = {}
        for name, sql in (("per_recipient_rows", fan_out), ("shared_row", shared_write)):
            samples = []
            for _ in range(5 if name == "per_recipient_rows" else args.runs):
                tx = c.begin()
                t0 = time.perf_counter()
                c.execute(text(sql), dict(id=uuid.uuid4(), pid=f"bench:{uuid.uuid4()}"))
                samples.append(time.perf_counter() - t0)
                tx.rollback()
            writes[name] = _ms(samples)
        writes["followers"] = followers
        writes["fcm_calls_device_by_device"] = -(-followers // 500)
        writes["fcm_calls_by_topic"] = 1
        result["write"] = writes

    text_out = json.dumps(result, indent=2, default=str)
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            f.write(text_out)
    summary = {k: {n: (v.get("p50"), v.get("p95")) for n, v in result[k].items() if isinstance(v, dict)} for k in ("shared", "app_call", "per_recipient", "write")}
    print(json.dumps(dict(layout=args.layout, seeded_s=result["seeded_s"], reader_club_broadcasts=result["reader_club_broadcasts"], ms=summary), indent=1))


if __name__ == "__main__":
    main()
