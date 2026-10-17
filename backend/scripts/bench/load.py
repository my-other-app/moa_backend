"""Load a running server (serve.py) with a realistic mix of what the site and
the app ask for, from C concurrent clients for D seconds, and report what it
sustained: requests a second, latency percentiles per kind, errors, and what
was busy (the server's workers, Postgres, this generator) while it ran.

    BENCH_DB=moa_s1 python -m scripts.bench.load --url http://127.0.0.1:8077 --concurrency 10,50,200 \\
        --duration 40 --stats-dir /tmp/stats --server-pid 12345 --out results/scale/s1_load_w1.json

The mix (by request): 40% front page (half with the browser's point, from the
cities' spread), 25% event search (plain, words, typos, filters, distance,
popular, later pages), 10% an event's page (upcoming, by popularity; some
past), 10% a club's page (by followers), 10% For You (2,000 signed-in
users), 5% the app's lists. Each request comes from one of 50,000 visitor
addresses (X-Forwarded-For), so the per-visitor rate limit applies as it
would to that many people. Closed loop: each client sends its next request
when the last one is answered.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import timedelta

from scripts.bench import _env  # noqa: F401  (settings before app)

import asyncpg
import httpx

API = "/api/v1"
MIX = [("home", 40), ("search", 25), ("event", 10), ("club", 10), ("foryou", 10), ("list", 5)]
WORDS = ["robotics", "ai", "music", "hackathon", "dance", "startup", "kochi", "bengaluru", "quiz", "photography", "yoga",
         "workshop", "data science", "chess", "film", "robtics", "hackaton", "photgraphy"]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def targets(db: str) -> dict:
    from scripts.bench.seed import CITIES

    conn = await asyncpg.connect(database=db, host="localhost")
    try:
        events = await conn.fetch(
            "SELECT e.slug, e.going_count FROM events e JOIN clubs c ON c.id = e.club_id "
            "WHERE NOT e.is_deleted AND e.status IN ('published','live') AND c.is_verified AND NOT c.is_deleted "
            "AND e.event_datetime > now() ORDER BY random() LIMIT 20000")
        past = await conn.fetch(
            "SELECT e.slug FROM events e JOIN clubs c ON c.id = e.club_id WHERE NOT e.is_deleted AND e.status IN ('closed','archived') "
            "AND c.is_verified AND NOT c.is_deleted ORDER BY random() LIMIT 2000")
        clubs = await conn.fetch("SELECT slug, follower_count FROM clubs WHERE is_verified AND NOT is_deleted ORDER BY random() LIMIT 10000")
        users = await conn.fetch("SELECT id, token_version FROM users WHERE user_type = 'app_user' ORDER BY random() LIMIT 2000")
        cats = [r[0] for r in await conn.fetch("SELECT id FROM event_categories WHERE NOT is_deleted")]
        interests = [r[0] for r in await conn.fetch("SELECT id FROM interests")]
    finally:
        await conn.close()
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    tokens = [create_access_token(data=AuthTokenData(user_id=u["id"], token_type="access_token", token_version=u["token_version"]).model_dump(),
                                  expires_delta=timedelta(days=1)) for u in users]
    return dict(
        events=[r["slug"] for r in events], event_w=[1 + r["going_count"] for r in events], past=[r["slug"] for r in past],
        clubs=[r["slug"] for r in clubs], club_w=[1 + r["follower_count"] for r in clubs], tokens=tokens, cats=cats, interests=interests,
        cities=[(c[1], c[2]) for c in CITIES], city_w=[c[3] for c in CITIES],
    )


def make_request(rng: random.Random, t: dict) -> tuple[str, str, dict, dict]:
    """(kind, path, params, headers)"""
    kind = rng.choices([k for k, _ in MIX], weights=[w for _, w in MIX])[0]
    h = {"X-Forwarded-For": f"49.{rng.randint(32, 63)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"}
    if kind in ("foryou", "list"):
        h["Authorization"] = f"Bearer {rng.choice(t['tokens'])}"

    def point():
        lat, lng = rng.choices(t["cities"], weights=t["city_w"])[0]
        return dict(lat=round(lat + rng.uniform(-0.15, 0.15), 5), lng=round(lng + rng.uniform(-0.15, 0.15), 5))

    if kind == "home":
        return kind, "/discover/home", (point() if rng.random() < 0.5 else {}), h
    if kind == "search":
        r = rng.random()
        if r < 0.30:
            p = {}
        elif r < 0.50:
            p = {"q": rng.choice(WORDS)}
        elif r < 0.62:
            p = {"category": rng.choice(t["cats"])}
        elif r < 0.72:
            p = {"when": rng.choice(["today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month"])}
        elif r < 0.82:
            p = point()
        elif r < 0.87:
            p = {**point(), "sort": "distance"}
        elif r < 0.92:
            p = {"sort": "popular"}
        else:
            p = dict(rng.choice([{"type": "online"}, {"type": "in_person"}, {"price": "free"}, {"price": "paid"}]))
        if rng.random() < 0.2:
            p["offset"] = rng.choice([24, 48, 96, 240])
        return kind, "/discover/events", p, h
    if kind == "event":
        slug = rng.choice(t["past"]) if (t["past"] and rng.random() < 0.1) else rng.choices(t["events"], weights=t["event_w"])[0]
        return kind, f"/discover/events/{slug}", {}, h
    if kind == "club":
        return kind, f"/discover/clubs/{rng.choices(t['clubs'], weights=t['club_w'])[0]}", {}, h
    if kind == "foryou":
        return kind, "/events/for-you", {}, h
    r = rng.random()
    if r < 0.5:
        p = {"is_ended": "false"}
    elif r < 0.7:
        p = {"search": rng.choice(WORDS)}
    elif r < 0.85:
        p = {"is_following": "true"}
    else:
        p = {"interest_ids": str(rng.choice(t["interests"]))}
    return kind, "/events/list", p, h


def cpu_seconds(pids: set[int] | None = None) -> dict[str, float]:
    """Cumulative CPU seconds by group: the server (pids given and their children), Postgres, this process."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,command="], capture_output=True, text=True).stdout
    groups: dict[str, float] = defaultdict(float)
    me = os.getpid()
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, t, cmd = int(parts[0]), int(parts[1]), parts[2], parts[3]
        rows.append((pid, ppid, t, cmd))
    server = set(pids or ())
    for pid, ppid, t, cmd in rows:  # one level of children (uvicorn's workers, multiprocessing's helpers)
        if ppid in server:
            server.add(pid)
    for pid, ppid, t, cmd in rows:
        m, s = t.split(":", 1)
        secs = int(m) * 60 + float(s)
        if pid == me:
            groups["loadgen"] += secs
        elif pid in server:
            groups["server"] += secs
        elif cmd.startswith("postgres") or "postgres:" in cmd:
            groups["postgres"] += secs
    return dict(groups)


CORES = os.cpu_count() or 8


def machine_busy(seconds: int) -> subprocess.Popen:
    """iostat, once a second for `seconds`: the whole machine's CPU (this laptop runs other things too)."""
    return subprocess.Popen(["iostat", "-n", "0", "-w", "1", "-c", str(seconds)], stdout=subprocess.PIPE, text=True)


def busy_cores(proc: subprocess.Popen, skip: int = 0) -> float:
    out, _ = proc.communicate()
    vals = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit():
            vals.append((100 - int(parts[2])) / 100 * CORES)
    vals = vals[1 + skip:]  # the first line is since boot
    return round(statistics.mean(vals), 2) if vals else float("nan")


async def pg_sampler(conn, db: str, stop: asyncio.Event, out: list) -> None:
    """Who is doing what in Postgres, twice a second, on a connection opened
    before the load (at the peak there may be none left to open)."""
    try:
        while not stop.is_set():
            rows = await conn.fetch(
                "SELECT state, CASE WHEN wait_event_type IN ('Lock', 'LWLock', 'IO') THEN wait_event_type || ':' || wait_event "
                "ELSE coalesce(wait_event_type, '') END AS wt, count(*) AS n FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid() AND backend_type IN ('client backend', 'parallel worker') GROUP BY 1, 2", db)
            out.append({f"{r['state']}|{r['wt']}": r["n"] for r in rows})
            try:
                await asyncio.wait_for(stop.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
    except asyncpg.PostgresError as exc:
        out.append({"sampler_error": str(exc)})


def server_stats(stats_dir: str) -> dict:
    lags, locks, per = [], [], []
    for path in glob.glob(os.path.join(stats_dir, "*.json")):
        try:
            d = json.load(open(path))
        except (OSError, ValueError):
            continue
        lags += d["lags"]
        locks += d["locks"]
        per.append(dict(pid=d["pid"], requests=d["requests"], max_inflight=d["max_inflight"], pool_checked_out=d["pool_checked_out"],
                        pool_overflow=d["pool_overflow"], loop_lag_p99_ms=round(pct(d["lags"], 0.99) * 1000, 1),
                        loop_lag_max_ms=round(max(d["lags"] or [0]) * 1000, 1)))
    return dict(
        workers=per,
        loop_lag_ms=dict(p50=round(pct(lags, 0.5) * 1000, 2), p99=round(pct(lags, 0.99) * 1000, 1), max=round(max(lags or [0]) * 1000, 1)),
        for_update_ms=dict(n=len(locks), p50=round(pct(locks, 0.5) * 1000, 2), p95=round(pct(locks, 0.95) * 1000, 1),
                           p99=round(pct(locks, 0.99) * 1000, 1), max=round(max(locks or [0]) * 1000, 1)),
    )


async def reset_server(client: httpx.AsyncClient, url: str, workers: int) -> None:
    # Each call lands on one worker; enough calls reach them all.
    for _ in range(workers * 8):
        try:
            await client.get(url + "/__bench/reset")
        except httpx.HTTPError:
            pass


async def run_level(url: str, t: dict, concurrency: int, duration: float, warmup: float, seed: int) -> dict:
    lat: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, int] = defaultdict(int)
    errors: dict[str, int] = defaultdict(int)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url + API, limits=limits, timeout=60.0) as client:
        start = time.perf_counter()
        measuring = asyncio.Event()

        async def user(i: int) -> None:
            r = random.Random(seed * 1000 + i)
            while True:
                now = time.perf_counter()
                if now - start > warmup + duration:
                    return
                kind, path, params, headers = make_request(r, t)
                t0 = time.perf_counter()
                try:
                    resp = await client.get(path, params=params, headers=headers)
                    code = str(resp.status_code)
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                took = time.perf_counter() - t0
                if measuring.is_set() and t0 - start <= warmup + duration:
                    lat[kind].append(took)
                    codes[code] += 1
                    if code != "200":
                        errors[f"{kind} {code}"] += 1

        async def flip() -> None:
            await asyncio.sleep(warmup)
            measuring.set()

        await asyncio.gather(flip(), *[user(i) for i in range(concurrency)])
    allv = [x for v in lat.values() for x in v]
    n = len(allv)
    return dict(
        concurrency=concurrency, duration_s=duration, requests=n, rps=round(n / duration, 1),
        error_rate=round(sum(errors.values()) / n, 4) if n else None, codes=dict(codes), errors=dict(errors),
        latency_ms=dict(p50=round(pct(allv, 0.5) * 1000, 1), p95=round(pct(allv, 0.95) * 1000, 1), p99=round(pct(allv, 0.99) * 1000, 1),
                        mean=round(statistics.mean(allv) * 1000, 1) if allv else None),
        by_kind={k: dict(n=len(v), p50=round(pct(v, 0.5) * 1000, 1), p95=round(pct(v, 0.95) * 1000, 1), p99=round(pct(v, 0.99) * 1000, 1))
                 for k, v in sorted(lat.items())},
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8077")
    p.add_argument("--concurrency", default="10,50,200")
    p.add_argument("--duration", type=float, default=40)
    p.add_argument("--warmup", type=float, default=10)
    p.add_argument("--stats-dir", default="")
    p.add_argument("--server-pid", type=int, default=0)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--label", default="load")
    p.add_argument("--out")
    args = p.parse_args()
    db = _env.DB
    t = await targets(db)
    results = []
    for c in [int(x) for x in args.concurrency.split(",")]:
        async with httpx.AsyncClient(timeout=10) as admin:
            await reset_server(admin, args.url, args.workers)
        baseline = machine_busy(6)
        await asyncio.sleep(6.5)
        idle_busy = busy_cores(baseline)
        samples: list = []
        stop = asyncio.Event()
        machine = machine_busy(int(args.warmup + args.duration))
        mark: dict = {}
        pg = await asyncpg.connect(database=db, host="localhost")

        async def after_warmup() -> None:
            await asyncio.sleep(args.warmup)
            mark["cpu0"] = cpu_seconds({args.server_pid} if args.server_pid else None)
            mark["t0"] = time.perf_counter()
            await pg_sampler(pg, db, stop, samples)

        sampler = asyncio.create_task(after_warmup())
        level = await run_level(args.url, t, c, args.duration, args.warmup, seed=c)
        wall = time.perf_counter() - mark["t0"]
        cpu0 = mark["cpu0"]
        cpu1 = cpu_seconds({args.server_pid} if args.server_pid else None)
        stop.set()
        await sampler
        await pg.close()
        samples = [x for x in samples if "sampler_error" not in x]
        busy = busy_cores(machine, skip=int(args.warmup))
        level["cpu_cores_busy"] = {k: round((cpu1.get(k, 0) - cpu0.get(k, 0)) / wall, 2) for k in ("server", "loadgen")}
        level["cpu_cores_busy"].update(machine=busy, machine_before=idle_busy,
                                       postgres_est=round(busy - idle_busy - sum(level["cpu_cores_busy"][k] for k in ("server", "loadgen")), 2))
        keys = sorted({k for s in samples for k in s})
        level["pg_backends"] = {k: dict(mean=round(statistics.mean(s.get(k, 0) for s in samples), 1), max=max(s.get(k, 0) for s in samples))
                                for k in keys} if samples else {}
        if args.stats_dir:
            level["server"] = server_stats(args.stats_dir)
        results.append(level)
        print(json.dumps({k: level[k] for k in ("concurrency", "requests", "rps", "error_rate", "latency_ms", "cpu_cores_busy")}), flush=True)
        print("   by kind:", json.dumps(level["by_kind"]), flush=True)
        if level["errors"]:
            print("   errors:", json.dumps(level["errors"]), flush=True)
        if args.stats_dir:
            print("   server:", json.dumps({k: level["server"][k] for k in ("loop_lag_ms", "for_update_ms")}),
                  json.dumps([{k: w[k] for k in ("requests", "max_inflight", "pool_checked_out", "pool_overflow")} for w in level["server"]["workers"]]), flush=True)
        print("   pg:", json.dumps(level["pg_backends"]), flush=True)
        await asyncio.sleep(3)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(dict(label=args.label, db=db, workers=args.workers, mix=MIX, results=results), f, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
