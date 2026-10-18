"""Writes piling onto one row, against a running server (serve.py):

- a hot event: N registrations for one event in S seconds (an open loop: the
  i-th is sent at i*S/N whether or not the earlier ones are answered), from
  signed-in app users (--mode app) or the public form (--mode public, fresh
  addresses: a guest account each). Registration locks the event row
  (SELECT … FOR UPDATE) for the rest of its transaction, and the going_count
  trigger updates that row once per registration;
- a hot club: N follows of one club from N users, C at a time; the
  follower_count trigger updates the club's row once per follow.

    BENCH_DB=moa_s1 python -m scripts.bench.hot event --n 500 --seconds 10 --event big --stats-dir /tmp/stats --out ...
    BENCH_DB=moa_s1 python -m scripts.bench.hot follow --n 10000 --concurrency 100 --out ...

Reported: latency percentiles and errors; how long the server's
`SELECT … FOR UPDATE` statements took (the wait for the lock is inside them;
serve.py records them); the most backends seen waiting on a lock at once;
and afterwards whether the counter still equals its rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from datetime import timedelta

from scripts.bench import _env  # noqa: F401  (settings before app)

import asyncpg
import httpx

from scripts.bench.load import pct, reset_server, server_stats

API = "/api/v1"


def _token(user_id: int, version: int) -> str:
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    return create_access_token(data=AuthTokenData(user_id=user_id, token_type="access_token", token_version=version).model_dump(),
                               expires_delta=timedelta(days=1))


async def lock_sampler(db: str, stop: asyncio.Event, out: dict) -> None:
    conn = await asyncpg.connect(database=db, host="localhost")
    try:
        while not stop.is_set():
            n = await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND wait_event_type = 'Lock'", db)
            out["max_lock_waiters"] = max(out.get("max_lock_waiters", 0), n)
            out["samples"] = out.get("samples", 0) + 1
            out["lock_waiting_samples"] = out.get("lock_waiting_samples", 0) + (1 if n else 0)
            try:
                await asyncio.wait_for(stop.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
    finally:
        await conn.close()


def summary(lat: list[float], codes: dict) -> dict:
    return dict(answered=len(lat), p50_ms=round(pct(lat, 0.5) * 1000, 1), p95_ms=round(pct(lat, 0.95) * 1000, 1),
                p99_ms=round(pct(lat, 0.99) * 1000, 1), max_ms=round(max(lat or [0]) * 1000, 1), codes=codes)


async def hot_event(args) -> dict:
    db = _env.DB
    conn = await asyncpg.connect(database=db, host="localhost")
    order = "e.going_count DESC" if args.event == "big" else "e.going_count ASC"
    ev = await conn.fetchrow(
        f"SELECT e.id, e.going_count FROM events e JOIN clubs c ON c.id = e.club_id WHERE NOT e.is_deleted AND e.status = 'published' "
        f"AND c.is_verified AND NOT c.is_deleted AND e.event_datetime > now() + interval '2 days' ORDER BY {order}, e.id LIMIT 1")
    event_id = ev["id"]
    # Open, free and uncapped, so every registration goes the whole way.
    await conn.execute("UPDATE events SET reg_startdate = now() - interval '1 day', reg_enddate = NULL, max_participants = NULL, "
                       "has_fee = false, reg_fee = NULL WHERE id = $1", event_id)
    if args.plan:
        # Under a plan's caps: the club on that plan (as an admin grants one),
        # and the event made now, so it is not grandfathered past the gates.
        # Every registration then also counts the event's and the club's month.
        club_id = await conn.fetchval("SELECT club_id FROM events WHERE id = $1", event_id)
        await conn.execute("UPDATE events SET created_at = now() WHERE id = $1", event_id)
        await conn.execute("DELETE FROM club_subscriptions WHERE club_id = $1", club_id)
        await conn.execute(
            "INSERT INTO club_subscriptions (club_id, plan_id, cycle, status, mrr_paise, created_at, updated_at, is_deleted, "
            "cancel_at_period_end, complimentary) SELECT $1, id, 'monthly', 'active', 0, now(), now(), false, false, false "
            "FROM plans WHERE key = $2", club_id, args.plan)
    people = []
    if args.mode == "app":
        rows = await conn.fetch(
            "SELECT u.id, u.email, u.token_version FROM users u WHERE u.user_type = 'app_user' AND u.id > 1 AND NOT EXISTS "
            "(SELECT 1 FROM event_registrations_link r WHERE r.event_id = $1 AND r.user_id = u.id) ORDER BY random() LIMIT $2", event_id, args.n)
        people = [dict(email=r["email"], name=f"User {r['id']}", token=_token(r["id"], r["token_version"])) for r in rows]
    else:
        tag = int(time.time())
        people = [dict(email=f"hot{tag}-{i}@example.com", name=f"Guest {i}", token=None) for i in range(args.n)]
    before = await conn.fetchval("SELECT going_count FROM events WHERE id = $1", event_id)
    await conn.close()

    lat: list[float] = []
    codes: dict = {}
    stop = asyncio.Event()
    locks: dict = {}
    async with httpx.AsyncClient(base_url=args.url + API, timeout=120, limits=httpx.Limits(max_connections=args.n)) as client:
        await reset_server(client, args.url, args.workers)
        sampler = asyncio.create_task(lock_sampler(db, stop, locks))
        start = time.perf_counter()

        async def one(i: int, who: dict) -> None:
            delay = start + (i * args.seconds / args.n) - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            h = {"X-Forwarded-For": f"49.44.{(i >> 8) & 255}.{i & 255}"}
            if who["token"]:
                h["Authorization"] = f"Bearer {who['token']}"
            t0 = time.perf_counter()
            try:
                r = await client.post(f"/events/registration/{event_id}/register", headers=h,
                                      json=dict(full_name=who["name"], email=who["email"], phone=f"98{random.randint(10**7, 10**8 - 1)}"))
                code = str(r.status_code)
            except httpx.HTTPError as exc:
                code = type(exc).__name__
            lat.append(time.perf_counter() - t0)
            codes[code] = codes.get(code, 0) + 1

        await asyncio.gather(*[one(i, w) for i, w in enumerate(people)])
        wall = time.perf_counter() - start
        await asyncio.sleep(1.5)  # the stats files are written once a second
        stop.set()
        await sampler
    conn = await asyncpg.connect(database=db, host="localhost")
    going, rows, templates = await conn.fetchrow(
        "SELECT e.going_count, (SELECT count(*) FROM event_registrations_link r WHERE r.event_id = e.id AND NOT r.is_deleted), "
        "(SELECT count(*) FROM email_templates t WHERE t.event_id = e.id) FROM events e WHERE e.id = $1", event_id)
    await conn.close()
    out = dict(kind="event", mode=args.mode, event=args.event, plan=args.plan or None, event_id=event_id, registered_before=before, n=args.n, seconds=args.seconds,
               wall_s=round(wall, 2), throughput_per_s=round(len(lat) / wall, 1), **summary(lat, codes),
               going_count_after=going, rows_after=rows, counter_matches=going == rows, journey_templates_on_event=templates,
               max_lock_waiters=locks.get("max_lock_waiters", 0),
               share_of_time_someone_waited_on_a_lock=round(locks.get("lock_waiting_samples", 0) / max(1, locks.get("samples", 1)), 2))
    if args.stats_dir:
        out["server"] = server_stats(args.stats_dir)
    return out


async def hot_follow(args) -> dict:
    db = _env.DB
    conn = await asyncpg.connect(database=db, host="localhost")
    club_id = await conn.fetchval("SELECT id FROM clubs WHERE is_verified AND NOT is_deleted ORDER BY follower_count DESC, id LIMIT 1 OFFSET 3")
    rows = await conn.fetch(
        "SELECT u.id, u.token_version FROM users u WHERE u.user_type = 'app_user' AND u.id > 1 AND NOT EXISTS "
        "(SELECT 1 FROM club_users_link f WHERE f.club_id = $1 AND f.user_id = u.id) ORDER BY random() LIMIT $2", club_id, args.n)
    before = await conn.fetchval("SELECT follower_count FROM clubs WHERE id = $1", club_id)
    await conn.close()
    tokens = [_token(r["id"], r["token_version"]) for r in rows]
    lat: list[float] = []
    codes: dict = {}
    stop = asyncio.Event()
    locks: dict = {}
    queue: asyncio.Queue = asyncio.Queue()
    for t in tokens:
        queue.put_nowait(t)
    async with httpx.AsyncClient(base_url=args.url + API, timeout=120, limits=httpx.Limits(max_connections=args.concurrency)) as client:
        await reset_server(client, args.url, args.workers)
        sampler = asyncio.create_task(lock_sampler(db, stop, locks))
        start = time.perf_counter()

        async def worker() -> None:
            while True:
                try:
                    tok = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                t0 = time.perf_counter()
                try:
                    r = await client.post(f"/clubs/follow/{club_id}", headers={"Authorization": f"Bearer {tok}"})
                    code = str(r.status_code)
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                lat.append(time.perf_counter() - t0)
                codes[code] = codes.get(code, 0) + 1

        await asyncio.gather(*[worker() for _ in range(args.concurrency)])
        wall = time.perf_counter() - start
        await asyncio.sleep(1.5)
        stop.set()
        await sampler
    conn = await asyncpg.connect(database=db, host="localhost")
    count, rows_n = await conn.fetchrow(
        "SELECT k.follower_count, (SELECT count(*) FROM club_users_link f WHERE f.club_id = k.id AND f.is_following AND NOT f.is_deleted) "
        "FROM clubs k WHERE k.id = $1", club_id)
    await conn.close()
    out = dict(kind="follow", club_id=club_id, followers_before=before, n=len(tokens), concurrency=args.concurrency, wall_s=round(wall, 2),
               throughput_per_s=round(len(lat) / wall, 1), **summary(lat, codes), follower_count_after=count, rows_after=rows_n,
               counter_matches=count == rows_n, max_lock_waiters=locks.get("max_lock_waiters", 0),
               share_of_time_someone_waited_on_a_lock=round(locks.get("lock_waiting_samples", 0) / max(1, locks.get("samples", 1)), 2))
    if args.stats_dir:
        out["server"] = server_stats(args.stats_dir)
    return out


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["event", "follow"])
    p.add_argument("--url", default="http://127.0.0.1:8077")
    p.add_argument("--n", type=int, default=500)
    p.add_argument("--seconds", type=float, default=10.0, help="spread the registrations over this long (0: all at once)")
    p.add_argument("--event", choices=["big", "empty"], default="big", help="the most registered upcoming event, or one with none")
    p.add_argument("--mode", choices=["app", "public"], default="app")
    p.add_argument("--plan", default="", help="put the event's club on this plan (e.g. campus) and the event under its caps")
    p.add_argument("--concurrency", type=int, default=100)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--stats-dir", default="")
    p.add_argument("--label", default="")
    p.add_argument("--out")
    args = p.parse_args()
    r = await (hot_event(args) if args.what == "event" else hot_follow(args))
    r["label"] = args.label
    print(json.dumps(r), flush=True)
    if args.out:
        try:
            prior = json.load(open(args.out))
        except (OSError, ValueError):
            prior = []
        prior.append(r)
        with open(args.out, "w") as f:
            json.dump(prior, f, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
