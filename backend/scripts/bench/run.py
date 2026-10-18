"""Time the public discover API and the app's event feeds against a seeded
database (see seed.py), in-process through the ASGI app, as the website and
the app call them.

    BENCH_DB=moa_bench python -m scripts.bench.run --label before --out scripts/bench/results/before.json
    BENCH_DB=moa_bench python -m scripts.bench.run --only home --explain plans.txt

For each endpoint: a few warm-up requests, then requests one after another
until `--n` are done or `--budget` seconds pass (at least `--min`). Reported:
p50/p95/p99 of the whole request, the SQL statements each request ran
(counted on the engine, so every statement the app sends is seen) and the
time spent in them. `--explain` re-runs the slowest statements of one
request per endpoint under EXPLAIN (ANALYZE, BUFFERS) with the parameters
the app used.

Requests come from a rotating private address, so neither the per-visitor
rate limit nor the address lookup gets in the way.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional

from scripts.bench import _env  # noqa: F401  (settings before app)

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

API = "/api/v1"


@dataclass
class Capture:
    """Statements seen on the engine while `on` is set."""
    on: bool = False
    statements: list = field(default_factory=list)

    def install(self, engine) -> None:
        @event.listens_for(engine, "before_cursor_execute")
        def _before(conn, cursor, statement, parameters, context, executemany):
            if self.on:
                conn.info.setdefault("_bench_t0", []).append(time.perf_counter())

        @event.listens_for(engine, "after_cursor_execute")
        def _after(conn, cursor, statement, parameters, context, executemany):
            if self.on:
                t0 = conn.info["_bench_t0"].pop()
                self.statements.append((statement, parameters, time.perf_counter() - t0))

    def take(self) -> list:
        out, self.statements = self.statements, []
        return out


class Unsupported(Exception):
    pass


def _write(path: str, label: str, targets: dict, results: list) -> None:
    with open(path, "w") as f:
        json.dump(dict(label=label, targets={k: v for k, v in targets.items() if k != "token_version"}, results=results), f, indent=1, default=str)


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def pick_targets(session) -> dict:
    """The big club, a popular upcoming event, and the bench user's token.
    The most followed club and the most registered event, by the counters the
    triggers keep (equal to counting the rows, and cheap at any size)."""
    big_club = (await session.execute(text(
        "SELECT c.slug FROM clubs c WHERE c.is_verified AND NOT c.is_deleted ORDER BY c.follower_count DESC, c.id LIMIT 1"
    ))).scalar()
    event_slug = (await session.execute(text(
        "SELECT e.slug FROM events e JOIN clubs c ON c.id = e.club_id "
        "WHERE NOT e.is_deleted AND e.status IN ('published', 'live') AND c.is_verified AND NOT c.is_deleted "
        "AND e.event_datetime > now() ORDER BY e.going_count DESC, e.id LIMIT 1"
    ))).scalar()
    interest = (await session.execute(text(
        "SELECT interest_id FROM clubs_interests_link GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    category = (await session.execute(text(
        "SELECT category_id FROM events WHERE event_datetime > now() GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    # An upcoming event in that busiest category: its page's "similar events" walk the category.
    in_big_category = (await session.execute(text(
        "SELECT e.slug FROM events e JOIN clubs c ON c.id = e.club_id "
        "WHERE NOT e.is_deleted AND e.status IN ('published', 'live') AND c.is_verified AND NOT c.is_deleted "
        "AND e.event_datetime > now() AND e.category_id = :cat ORDER BY e.event_datetime, e.id LIMIT 1"
    ), {"cat": category})).scalar()
    user = (await session.execute(text("SELECT id, token_version FROM users WHERE email = 'bench.user@example.com'"))).first()
    return dict(club=big_club, event=event_slug, event_big_category=in_big_category, interest=interest, category=category,
                user_id=user[0], token_version=user[1])


BLR = dict(lat=12.9716, lng=77.5946)
KOCHI = dict(lat=9.9312, lng=76.2673)


def _forget() -> None:
    """Empty the app's shared minute cache (there is none before it was added)."""
    try:
        from app.api.discover import cache
    except ImportError:
        return
    cache.reset()


def _forget_personal() -> None:
    """Empty only what is kept per person (For You's rankings): a new user's
    first request, with the minute's shared lists already made."""
    try:
        from app.api.discover import cache
    except ImportError:
        return
    store = getattr(cache, "personal", None)
    if store is not None:
        store.clear()


def _days() -> dict:
    """Calendar days and months in India, relative to today, for the date filters."""
    today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).date()
    n = today.year * 12 + today.month  # next month, 1-based
    return dict(
        week_out=(today + timedelta(days=7)).isoformat(), week_out_end=(today + timedelta(days=13)).isoformat(),
        today=today.isoformat(), quarter=(today + timedelta(days=90)).isoformat(),
        far=(today + timedelta(days=150)).isoformat(),
        month=f"{today.year:04d}-{today.month:02d}", next_month=f"{n // 12:04d}-{n % 12 + 1:02d}",
        year_back=f"{today.year - 1:04d}-{today.month:02d}",
    )


def endpoints(t: dict) -> list[tuple[str, str, dict, bool]]:
    """(name, path, params, signed_in); a name ending in "(cold)" empties the
    shared cache before every request, so it times the work a cache miss does;
    one ending in "(user cold)" empties only what is kept per person."""
    d = _days()
    return [
        ("home", "/discover/home", {}, False),
        ("home (cold)", "/discover/home", {}, False),
        ("home lat/lng", "/discover/home", BLR, False),
        ("home lat/lng (cold)", "/discover/home", BLR, False),
        ("events", "/discover/events", {}, False),
        ("events q", "/discover/events", {"q": "robotics"}, False),
        ("events q short", "/discover/events", {"q": "ai"}, False),
        ("events q typo", "/discover/events", {"q": "robtics"}, False),
        ("events q relevance", "/discover/events", {"q": "robotics", "sort": "relevance"}, False),
        ("events this_weekend", "/discover/events", {"when": "this_weekend"}, False),
        ("events category", "/discover/events", {"category": t["category"]}, False),
        ("events online", "/discover/events", {"type": "online"}, False),
        ("events free", "/discover/events", {"price": "free"}, False),
        ("events lat/lng (radius 50, date)", "/discover/events", BLR, False),
        ("events distance r=50", "/discover/events", {**BLR, "sort": "distance", "radius_km": 50}, False),
        ("events distance r=300", "/discover/events", {**KOCHI, "sort": "distance", "radius_km": 300}, False),
        ("events popular", "/discover/events", {"sort": "popular"}, False),
        ("events offset=480", "/discover/events", {"offset": 480}, False),
        ("events from/to a week, a week out", "/discover/events", {"from": d["week_out"], "to": d["week_out_end"]}, False),
        ("events from/to the next 90 days", "/discover/events", {"from": d["today"], "to": d["quarter"]}, False),
        ("events from/to one day 150 days out", "/discover/events", {"from": d["far"], "to": d["far"]}, False),
        ("calendar", "/discover/calendar", {"month": d["month"]}, False),
        ("calendar (cold)", "/discover/calendar", {"month": d["month"]}, False),
        ("calendar next month (cold)", "/discover/calendar", {"month": d["next_month"]}, False),
        ("calendar a year back (cold)", "/discover/calendar", {"month": d["year_back"]}, False),
        ("calendar q (cold)", "/discover/calendar", {"month": d["next_month"], "q": "robotics"}, False),
        ("calendar lat/lng (cold)", "/discover/calendar", {"month": d["next_month"], **BLR}, False),
        ("club events past (big)", f"/discover/clubs/{t['club']}/events", {"when": "past"}, False),
        ("club events upcoming (big)", f"/discover/clubs/{t['club']}/events", {"when": "upcoming"}, False),
        ("club events past offset=48 (big)", f"/discover/clubs/{t['club']}/events", {"when": "past", "offset": 48, "limit": 48}, False),
        ("clubs", "/discover/clubs", {}, False),
        ("clubs q", "/discover/clubs", {"q": "robotics"}, False),
        ("clubs q typo", "/discover/clubs", {"q": "robtics"}, False),
        ("clubs interest", "/discover/clubs", {"interest": t["interest"]}, False),
        ("clubs nearby", "/discover/clubs", {**BLR, "sort": "nearby"}, False),
        ("clubs popular", "/discover/clubs", {"sort": "popular"}, False),
        ("club page (big)", f"/discover/clubs/{t['club']}", {}, False),
        ("event page", f"/discover/events/{t['event']}", {}, False),
        ("threads", "/discover/threads", {}, False),
        ("categories", "/discover/categories", {}, False),
        ("sitemap", "/discover/sitemap", {}, False),
        ("sitemap (cold)", "/discover/sitemap", {}, False),
        ("categories (cold)", "/discover/categories", {}, False),
        ("app for-you", "/events/for-you", {}, True),
        ("app list upcoming", "/events/list", {"is_ended": "false"}, True),
        ("app list search", "/events/list", {"search": "robotics"}, True),
        ("app list following", "/events/list", {"is_following": "true"}, True),
        ("app list interests", "/events/list", {"interest_ids": str(t["interest"])}, True),
        # Added for the scale study: deep pages, the app's past list, and the
        # endpoints the discover work in flight adds (recorded as "not
        # offered" until they are on the branch measured).
        ("events offset=4800", "/discover/events", {"offset": 4800}, False),
        ("clubs offset=2400", "/discover/clubs", {"offset": 2400}, False),
        ("app list ended", "/events/list", {"is_ended": "true"}, True),
        ("calendar", "/discover/calendar", {}, False),
        ("club events page 2", f"/discover/clubs/{t['club']}/events", {"offset": 24}, False),
        # Added with perf/discover-scale-2: For You with nothing kept (its
        # ranking is now kept per user for five minutes, so the plain row
        # above is the kept case), a typo with nothing kept, and the paged
        # sitemap (not offered before).
        ("app for-you (cold)", "/events/for-you", {}, True),
        ("app for-you (user cold)", "/events/for-you", {}, True),
        ("app for-you lat/lng (cold)", "/events/for-you", BLR, True),
        ("event page (cold)", f"/discover/events/{t['event']}", {}, False),
        ("event page (busiest category)", f"/discover/events/{t['event_big_category']}", {}, False),
        ("sitemap index (cold)", "/discover/sitemap/index", {}, False),
        ("sitemap events page 1 (cold)", "/discover/sitemap/events/1", {}, False),
        ("sitemap clubs page 1 (cold)", "/discover/sitemap/clubs/1", {}, False),
    ]


def _clip(line: str, n: int = 300) -> str:
    """A plan line or statement, cut short: an IN list of ten thousand ids says nothing more than its first few."""
    return line if len(line) <= n else line[:n] + f" … [{len(line) - n} more characters]"


INDEX_RE = re.compile(r"(?:Index Scan|Index Only Scan|Bitmap Index Scan) (?:Backward )?(?:using|on) (\w+)")


async def explain(engine, statements: list, top: Optional[int] = 2, used: Optional[set] = None) -> list[str]:
    """EXPLAIN (ANALYZE, BUFFERS) of the slowest `top` statements (all with None);
    the names of the indexes the plans touch are added to `used`."""
    out = []
    statements = [st for st in statements if st[0].lstrip().upper().startswith(("SELECT", "WITH"))]
    worst = sorted(statements, key=lambda s: -s[2])[:top] if top else list(statements)
    async with engine.connect() as conn:
        for statement, params, took in worst:
            res = await conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement, params)
            lines = [r[0] for r in res.all()]
            if used is not None:
                for line in lines:
                    used.update(INDEX_RE.findall(line))
            plan = "\n".join(_clip(line) for line in lines)
            out.append(f"-- {took * 1000:.1f} ms in the app\n{_clip(statement, 1500)}\n{plan}\n")
            await conn.rollback()
    return out


async def pages_touched(engine, statements: list) -> dict:
    """Shared buffers hit and read by the statements of one request, from
    EXPLAIN (ANALYZE, BUFFERS) of each (the top node counts its children's).
    A statement that ran after a SET LOCAL (the typo fallback's similarity
    threshold) is re-run without it, so it may match more or less."""
    hit = read = 0
    async with engine.connect() as conn:
        for statement, params, _ in statements:
            if not statement.lstrip().upper().startswith(("SELECT", "WITH")):
                continue
            res = await conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params)
            plan = res.scalar()
            plan = json.loads(plan) if isinstance(plan, str) else plan
            top = plan[0]["Plan"]
            hit += int(top.get("Shared Hit Blocks", 0))
            read += int(top.get("Shared Read Blocks", 0))
            await conn.rollback()
    return dict(pages=hit + read, pages_hit=hit, pages_read=read)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="run")
    p.add_argument("--out")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--min", type=int, default=15)
    p.add_argument("--budget", type=float, default=40.0)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--only", help="comma-separated substrings of endpoint names")
    p.add_argument("--exact", help="comma-separated endpoint names, matched whole (with --only, either)")
    p.add_argument("--skip", help="comma-separated substrings of endpoint names to leave out")
    p.add_argument("--explain", help="write EXPLAIN (ANALYZE, BUFFERS) of the slowest statements here")
    p.add_argument("--explain-all", action="store_true", help="explain every statement of the request, not the slowest two")
    p.add_argument("--indexes", help="write which indexes each endpoint's plans use here (JSON); implies explaining every statement")
    p.add_argument("--buffers", action="store_true",
                   help="re-run every statement of one request per endpoint under EXPLAIN (ANALYZE, BUFFERS) and record the pages it touched "
                        "(shared hit + read): the work a request does, whatever else the machine is doing")
    p.add_argument("--quiet-sql-log", action="store_true", help="turn the app's SQL statement log down to WARNING")
    p.add_argument("--freeze", action="store_true", help="gc.freeze() after import, as the app's startup does (doth.lifespan)")
    args = p.parse_args()

    from doth import application
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token
    from app.db.core import AsyncSessionLocal, engine

    if args.freeze:
        # The in-process client runs no lifespan; do what its startup does to the collector.
        gc.collect()
        gc.freeze()
    if args.quiet_sql_log:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # The app logs every statement; in a benchmark that goes to a file, not the terminal.
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) in (sys.stderr, sys.stdout):
            logging.getLogger().removeHandler(h)

    cap = Capture()
    cap.install(engine.sync_engine)
    async with AsyncSessionLocal() as s:
        targets = await pick_targets(s)
    # Valid for a day: a run against the old code takes hours.
    token = create_access_token(
        data=AuthTokenData(user_id=targets["user_id"], token_type="access_token", token_version=targets["token_version"]).model_dump(),
        expires_delta=timedelta(days=1),
    )

    results = []
    plans: list[str] = []
    index_use: dict[str, list[str]] = {}
    ip = 0
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://bench") as client:
        for name, path, params, signed in endpoints(targets):
            wanted = []
            if args.only:
                wanted.append(any(o.strip() in name for o in args.only.split(",")))
            if args.exact:
                wanted.append(name in [o.strip() for o in args.exact.split(",")])
            if wanted and not any(wanted):
                continue
            if args.skip and any(o.strip() in name for o in args.skip.split(",")):
                continue
            headers = {"Authorization": f"Bearer {token}"} if signed else {}

            async def call():
                nonlocal ip
                ip += 1
                h = {**headers, "X-Forwarded-For": f"10.{(ip >> 16) & 255}.{(ip >> 8) & 255}.{ip & 255}"}
                if name.endswith("(cold)"):
                    _forget()
                elif name.endswith("(user cold)"):
                    _forget_personal()
                cap.take()
                cap.on = True
                t0 = time.perf_counter()
                try:
                    r = await client.get(API + path, params=params, headers=h)
                except Exception as exc:  # noqa: BLE001 — the app raised: recorded, and the run goes on
                    cap.on = False
                    raise Unsupported(f"500 {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
                took = time.perf_counter() - t0
                cap.on = False
                if r.status_code != 200:
                    raise Unsupported(f"{r.status_code} {r.text[:200]}")
                return took, cap.take(), len(r.content)

            try:
                for _ in range(max(1, args.warmup)):
                    await call()
            except Unsupported as exc:
                # e.g. a sort this version of the API does not know (422)
                results.append(dict(name=name, path=path, params=params, unsupported=str(exc)))
                print(f"{name:<36} not answered: {exc}", flush=True)
                continue
            lat, counts, sql_t = [], [], []
            last = []
            size = 0
            start = time.perf_counter()
            while len(lat) < args.n and (len(lat) < args.min or time.perf_counter() - start < args.budget):
                took, stmts, size = await call()
                lat.append(took)
                counts.append(len(stmts))
                sql_t.append(sum(s[2] for s in stmts))
                last = stmts
            row = dict(
                name=name, path=path, params=params, n=len(lat),
                p50=pct(lat, 0.5) * 1000, p95=pct(lat, 0.95) * 1000, p99=pct(lat, 0.99) * 1000, mean=statistics.mean(lat) * 1000,
                statements_min=min(counts), statements_max=max(counts), sql_ms=statistics.mean(sql_t) * 1000, bytes=size,
            )
            if args.buffers:
                row.update(await pages_touched(engine, last))
            results.append(row)
            if args.out:  # as it goes, so a run that dies late keeps what it measured
                _write(args.out, args.label, targets, results)
            print(f"{name:<36} n={row['n']:>4}  p50 {row['p50']:8.1f}  p95 {row['p95']:8.1f}  p99 {row['p99']:8.1f} ms  "
                  f"stmts {row['statements_min']}-{row['statements_max']}  sql {row['sql_ms']:7.1f} ms"
                  + (f"  pages {row['pages']}" if "pages" in row else ""), flush=True)
            if args.explain or args.indexes:
                used: set = set()
                explained = await explain(engine, last, top=None if (args.explain_all or args.indexes) else 2, used=used)
                index_use[name] = sorted(used)
                if args.explain:
                    plans.append(f"==================== {name}  {path} {params}\n")
                    plans.extend(explained)

    if args.out:
        _write(args.out, args.label, targets, results)
    if args.explain:
        with open(args.explain, "w") as f:
            f.write("\n".join(plans))
    if args.indexes:
        by_index: dict[str, list[str]] = {}
        for endpoint, names in index_use.items():
            for n in names:
                by_index.setdefault(n, []).append(endpoint)
        with open(args.indexes, "w") as f:
            json.dump(dict(by_endpoint=index_use, by_index=dict(sorted(by_index.items()))), f, indent=1)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
