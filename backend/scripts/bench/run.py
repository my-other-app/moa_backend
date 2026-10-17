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
from datetime import timedelta
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
    """The big club, a popular upcoming event, and the bench user's token."""
    big_club = (await session.execute(text(
        "SELECT c.slug FROM clubs c JOIN club_users_link f ON f.club_id = c.id AND f.is_following AND NOT f.is_deleted "
        "WHERE c.is_verified AND NOT c.is_deleted GROUP BY c.id ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    event_slug = (await session.execute(text(
        "SELECT e.slug FROM events e JOIN clubs c ON c.id = e.club_id JOIN event_registrations_link r ON r.event_id = e.id AND NOT r.is_deleted "
        "WHERE NOT e.is_deleted AND e.status IN ('published', 'live') AND c.is_verified AND NOT c.is_deleted "
        "AND e.event_datetime > now() GROUP BY e.id ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    interest = (await session.execute(text(
        "SELECT interest_id FROM clubs_interests_link GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    category = (await session.execute(text(
        "SELECT category_id FROM events WHERE event_datetime > now() GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
    ))).scalar()
    user = (await session.execute(text("SELECT id, token_version FROM users WHERE email = 'bench.user@example.com'"))).first()
    return dict(club=big_club, event=event_slug, interest=interest, category=category, user_id=user[0], token_version=user[1])


BLR = dict(lat=12.9716, lng=77.5946)
KOCHI = dict(lat=9.9312, lng=76.2673)


def _forget() -> None:
    """Empty the app's shared minute cache (there is none before it was added)."""
    try:
        from app.api.discover import cache
    except ImportError:
        return
    cache.reset()


def endpoints(t: dict) -> list[tuple[str, str, dict, bool]]:
    """(name, path, params, signed_in); a name ending in "(cold)" empties the
    shared cache before every request, so it times the work a cache miss does."""
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


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="run")
    p.add_argument("--out")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--min", type=int, default=15)
    p.add_argument("--budget", type=float, default=40.0)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--only", help="comma-separated substrings of endpoint names")
    p.add_argument("--skip", help="comma-separated substrings of endpoint names to leave out")
    p.add_argument("--explain", help="write EXPLAIN (ANALYZE, BUFFERS) of the slowest statements here")
    p.add_argument("--explain-all", action="store_true", help="explain every statement of the request, not the slowest two")
    p.add_argument("--indexes", help="write which indexes each endpoint's plans use here (JSON); implies explaining every statement")
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
            if args.only and not any(o.strip() in name for o in args.only.split(",")):
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
            results.append(row)
            if args.out:  # as it goes, so a run that dies late keeps what it measured
                _write(args.out, args.label, targets, results)
            print(f"{name:<36} n={row['n']:>4}  p50 {row['p50']:8.1f}  p95 {row['p95']:8.1f}  p99 {row['p99']:8.1f} ms  "
                  f"stmts {row['statements_min']}-{row['statements_max']}  sql {row['sql_ms']:7.1f} ms", flush=True)
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
