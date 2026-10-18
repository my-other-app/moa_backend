"""Time "near you" alone -- the front page's pool of events near a point, the
clubs near it, the search's radius and its sort by distance -- in process,
with the pages (shared buffers) each call touches. Runs with any version of
the code that has these functions, so two versions can be compared on the
same data, interleaved.

    BENCH_DB=moa_bench python -m scripts.bench.near --label after --out near.json

Places: dense cities (Bengaluru, Mumbai), a town (Kochi), and places with
nothing seeded within a hundred kilometres (Leh, Port Blair), where a walk
in date order read every event still to come before giving up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone

from scripts.bench import _env  # noqa: F401  (settings before app)

PLACES = {
    "Bengaluru": (12.9716, 77.5946), "Mumbai": (19.0760, 72.8777), "Kochi": (9.9312, 76.2673),
    "Leh": (34.1526, 77.5771), "Port Blair": (11.6234, 92.7265),
}


async def pages_replayed(engine, statements: list) -> dict:
    """Shared buffers hit and read by one call's statements, from EXPLAIN
    (ANALYZE, BUFFERS) of each, in one transaction with the call's own SET
    LOCALs replayed before them (run.pages_touched leaves those out, and a
    statement planned without them may be planned otherwise)."""
    hit = read = 0
    async with engine.connect() as conn:
        await conn.begin()
        for statement, params, _ in statements:
            head = statement.lstrip().upper()
            if head.startswith(("SET ", "RESET ")):
                await conn.exec_driver_sql(statement, params)
                continue
            if not head.startswith(("SELECT", "WITH")):
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
    p.add_argument("--n", type=int, default=15)
    args = p.parse_args()

    from app.api.discover import cache, geo, places, service, upcoming
    from app.db.core import AsyncSessionLocal, engine
    from scripts.bench.run import Capture

    cap = Capture()
    cap.install(engine.sync_engine)
    now = datetime.now(timezone.utc)
    # The minute's shared answers kept, as in production: a search's category
    # and topic counts are made once, and each case times its own statements.
    cache.reset(enabled=True)

    def cases(here):
        cell = geo.cell(*here)
        centre, reach = geo.cell_centre(cell), geo.cell_reach(cell)
        return [
            ("near-you pool", None),  # (run below, with the minute's lower bound)
            ("clubs pool (nearest 8, then around)", lambda s: _club_pool(s, places, centre, reach)),
            ("events within 50 km, by date", lambda s: service.search_events(
                s, dict(lat=here[0], lng=here[1]), q=None, when="any", category=None, type_="any", price="any", radius_km=50.0,
                sort="date", offset=0, limit=24, now=now)),
            ("events within 50 km, by distance", lambda s: service.search_events(
                s, dict(lat=here[0], lng=here[1]), q=None, when="any", category=None, type_="any", price="any", radius_km=50.0,
                sort="distance", offset=0, limit=24, now=now)),
            ("clubs within 50 km, nearest first", lambda s: service.search_clubs(
                s, dict(lat=here[0], lng=here[1]), q=None, interest=None, radius_km=50.0, sort="nearby", offset=0, limit=24, now=now)),
        ]

    async def _club_pool(s, places_, centre, reach):
        r, every = await places_.nearest(s, centre, 8)
        if r is not None:
            return await places_.around(s, centre, r + 2 * reach)
        return every

    results = []
    for place, here in PLACES.items():
        for name, call in cases(here):
            async with AsyncSessionLocal() as s:
                since = await upcoming.since(s, now)

                async def run():
                    if name == "near-you pool":
                        cell = geo.cell(*here)
                        return await service._near_pool(s, now, since, geo.cell_centre(cell), geo.cell_reach(cell))
                    return await call(s)

                for _ in range(2):
                    await run()
                    await s.rollback()
                lat = []
                last = []
                for _ in range(args.n):
                    cap.take()
                    cap.on = True
                    t0 = time.perf_counter()
                    await run()
                    lat.append((time.perf_counter() - t0) * 1000)
                    cap.on = False
                    last = cap.take()
                    await s.rollback()
            pages = await pages_replayed(engine, last)
            row = dict(place=place, case=name, n=len(lat), p50=statistics.median(lat), p95=sorted(lat)[int(len(lat) * 0.95) - 1],
                       statements=len(last), **pages)
            results.append(row)
            print(f"{place:<11} {name:<38} p50 {row['p50']:8.1f}  p95 {row['p95']:8.1f} ms  stmts {row['statements']:>2}  pages {row['pages']:>7}", flush=True)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(dict(label=args.label, results=results), f, indent=1)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
