"""How fast the job queue (app/core/jobs.py) claims and runs jobs, and what
the claim's plan is, with a big history and a backlog in the table.

    BENCH_DB=moa_bench_jobs python -m scripts.bench.jobs_claim \
        --finished 1000000 --scheduled 100000 --due 50000 --runners 4 --concurrency 16 --out results/jobs_claim.json

What it does, on a scratch database of its own (the two tables are dropped
and made again; nothing else is touched):

1. Seeds `--finished` finished rows (the kept history), `--scheduled` rows
   due later (retries in backoff, run_at jobs), a few dead ones, and `--due`
   rows due now, spread over 15 job names. ANALYZE.
2. EXPLAIN (ANALYZE, BUFFERS) of the claim the runner sends: 15 names, four
   slots each, sixteen at most -- the real statement, rolled back.
3. Drains the due rows with `--runners` real Runner objects (each its own
   claims, `--concurrency` jobs in hand, a no-op handler), as that many
   worker processes would: jobs per second, end to end (claim, run, settle).
   Then checks every job ran exactly once.
4. Enqueues for `--seconds` from `--writers` concurrent writers, one job per
   transaction, as requests do: enqueues per second.
5. Both at once: writers at full speed while the runners drain, and the
   oldest due job's wait sampled every 0.2 s -- whether the runners keep up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

from scripts.bench import _env  # noqa: F401  (settings before app)

NAMES = [f"bench.{c}" for c in "abcdefghijklmno"]


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--finished", type=int, default=1_000_000)
    p.add_argument("--scheduled", type=int, default=100_000)
    p.add_argument("--due", type=int, default=50_000)
    p.add_argument("--runners", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--writers", type=int, default=16)
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--out")
    args = p.parse_args()

    from contextlib import asynccontextmanager

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.core import jobs

    engine = create_async_engine(settings.DATABASE_URL, pool_size=args.runners * 4 + args.writers + 8, max_overflow=8)
    make = async_sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():
        async with make() as s:
            yield s

    jobs.session_scope = scope
    settings.JOBS_POLL_SECONDS = 0.05
    ran: dict[str, int] = {}

    for name in NAMES:
        async def noop(payload: dict, _name=name) -> None:
            ran[str(payload["i"])] = ran.get(str(payload["i"]), 0) + 1

        noop.__qualname__ = f"noop_{name}"
        jobs.handler(name, timeout=30.0)(noop)

    out: dict = {"args": vars(args), "postgres": None}
    async with engine.begin() as conn:
        out["postgres"] = await conn.scalar(sa.text("SHOW server_version"))
        await conn.run_sync(lambda c: jobs.JobWorkers.__table__.drop(c, checkfirst=True))
        await conn.run_sync(lambda c: jobs.Jobs.__table__.drop(c, checkfirst=True))
        await conn.run_sync(lambda c: jobs.Jobs.__table__.create(c))
        await conn.run_sync(lambda c: jobs.JobWorkers.__table__.create(c))

    names = "ARRAY[" + ",".join(f"'{n}'" for n in NAMES) + "]"
    t = time.perf_counter()
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"""
            INSERT INTO jobs (name, payload, attempts, run_at, finished_at, created_at, updated_at)
            SELECT ({names})[1 + g % 15], jsonb_build_object('i', -g), 1, now() - interval '1 day', now() - (g % 72) * interval '1 hour',
                   now() - interval '1 day', now()
            FROM generate_series(1, {args.finished}) g
        """))
        await conn.execute(sa.text(f"""
            INSERT INTO jobs (name, payload, attempts, run_at, last_error, created_at, updated_at)
            SELECT ({names})[1 + g % 15], jsonb_build_object('i', -g), 1 + g % 5, now() + (1 + g % 600) * interval '1 minute', 'the provider said no', now(), now()
            FROM generate_series(1, {args.scheduled}) g
        """))
        await conn.execute(sa.text(f"""
            INSERT INTO jobs (name, payload, attempts, dead, last_error, created_at, updated_at)
            SELECT ({names})[1 + g % 15], jsonb_build_object('i', -g), 6, true, 'gave up', now(), now() FROM generate_series(1, 1000) g
        """))
        await conn.execute(sa.text(f"""
            INSERT INTO jobs (name, payload, priority, run_at, created_at, updated_at)
            SELECT ({names})[1 + g % 15], jsonb_build_object('i', g), CASE WHEN g % 50 = 0 THEN 2 WHEN g % 50 = 1 THEN -1 ELSE 0 END,
                   now() - (g % 600) * interval '1 second', now(), now()
            FROM generate_series(1, {args.due}) g
        """))
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(sa.text("VACUUM ANALYZE jobs"))
        sizes = (await conn.execute(sa.text(
            "SELECT pg_size_pretty(pg_relation_size('jobs')), pg_size_pretty(pg_relation_size('ix_jobs_claim')), (SELECT count(*) FROM jobs)"
        ))).one()
    out["seed"] = {"seconds": round(time.perf_counter() - t, 1), "rows": sizes[2], "table": sizes[0], "claim_index": sizes[1]}
    print(f"seeded {sizes[2]:,} rows in {out['seed']['seconds']} s: table {sizes[0]}, claim index {sizes[1]}")

    # 2. The claim's plan, on the real statement, rolled back.
    want = [(n, 4, 60.0) for n in NAMES]
    params = {"names": [w[0] for w in want], "slots": [w[1] for w in want], "leases": [w[2] for w in want], "levels": list(jobs.PRIORITIES), "total": 16, "me": "explain"}
    sql = jobs.CLAIM_SQL.text
    async with engine.connect() as conn:
        trans = await conn.begin()
        plans = []
        for _ in range(5):
            rows = (await conn.execute(sa.text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params)).scalars().all()
            plans.append(rows)
        await trans.rollback()
    plan = plans[-1]
    out["explain"] = plan
    times = [float(next(line for line in p if line.startswith("Execution Time")).split()[2]) for p in plans]
    out["claim_ms"] = {"runs": times, "median": statistics.median(times)}
    print("\n".join(plan))
    print(f"claim of 16 over 15 names: median {statistics.median(times):.2f} ms (5 runs, warm)")

    # 3. Drain the backlog with real runners.
    t = time.perf_counter()
    runners = [jobs.Runner(kind="worker", concurrency=args.concurrency, maintenance=False).start() for _ in range(args.runners)]

    async def done() -> int:
        async with engine.connect() as conn:
            return int(await conn.scalar(sa.text("SELECT count(*) FROM jobs WHERE finished_at IS NULL AND NOT dead AND run_at <= now() AND (payload->>'i')::int > 0")))

    while await done() > 0:
        await asyncio.sleep(0.2)
    while sum(r.handled for r in runners) < args.due:
        await asyncio.sleep(0.05)
    drain = time.perf_counter() - t
    twice = sum(1 for v in ran.values() if v > 1)
    out["drain"] = {
        "jobs": args.due, "seconds": round(drain, 2), "jobs_per_second": round(args.due / drain),
        "per_runner": [r.handled for r in runners], "ran_twice": twice, "ran": len(ran),
    }
    print(f"drained {args.due:,} jobs with {args.runners} runners x {args.concurrency}: {drain:.1f} s, {args.due / drain:,.0f} jobs/s; ran twice: {twice}")

    # 4. Enqueue rate, one job per transaction.
    counter = {"n": 0}
    stop = time.perf_counter() + args.seconds
    #: What the INSERT adds to a request's transaction (its commit is the
    #: request's own, there before the queue too).
    enqueue_ms: list[float] = []

    async def writer(w: int) -> None:
        i = 0
        while time.perf_counter() < stop:
            async with make() as s:
                await s.execute(sa.text("SELECT 1"))  # the request's transaction, already open
                t0 = time.perf_counter()
                await jobs.enqueue(s, NAMES[i % 15], {"i": 10_000_000 + w * 1_000_000 + i})
                enqueue_ms.append((time.perf_counter() - t0) * 1000)
                await s.commit()
            i += 1
            counter["n"] += 1

    for r in runners:
        r._stopping = True
    await asyncio.gather(*(r.stop(timeout=5) for r in runners))
    t = time.perf_counter()
    await asyncio.gather(*(writer(w) for w in range(args.writers)))
    enq = time.perf_counter() - t
    q = statistics.quantiles(enqueue_ms, n=100)
    out["enqueue"] = {
        "writers": args.writers, "jobs": counter["n"], "seconds": round(enq, 2), "per_second": round(counter["n"] / enq),
        "insert_ms": {"p50": round(q[49], 3), "p95": round(q[94], 3), "p99": round(q[98], 3)},
    }
    print(f"enqueued {counter['n']:,} in {enq:.1f} s from {args.writers} writers: {counter['n'] / enq:,.0f}/s; "
          f"the INSERT p50 {q[49]:.2f} ms, p95 {q[94]:.2f} ms, p99 {q[98]:.2f} ms")
    enqueue_ms.clear()

    # 5. Both at once: do the runners keep up with the writers? First the
    # runners empty what step 4 left, so the wait measured is the steady one.
    runners = [jobs.Runner(kind="worker", concurrency=args.concurrency, maintenance=False).start() for _ in range(args.runners)]

    async def backlog() -> int:
        async with engine.connect() as conn:
            return int(await conn.scalar(sa.text("SELECT count(*) FROM jobs WHERE finished_at IS NULL AND NOT dead AND run_at <= now()")))

    t = time.perf_counter()
    while await backlog() > 0:
        await asyncio.sleep(0.1)
    out["enqueue"]["drained_after_seconds"] = round(time.perf_counter() - t, 2)
    counter["n"] = 0
    stop = time.perf_counter() + args.seconds
    lags: list[float] = []

    async def sample() -> None:
        while time.perf_counter() < stop:
            async with engine.connect() as conn:
                lag = await conn.scalar(sa.text(
                    "SELECT coalesce(extract(epoch FROM now() - min(run_at)), 0) FROM jobs WHERE finished_at IS NULL AND NOT dead AND run_at <= now()"
                ))
            lags.append(float(lag))
            await asyncio.sleep(0.2)

    handled_before = sum(r.handled for r in runners)
    t = time.perf_counter()
    await asyncio.gather(sample(), *(writer(w) for w in range(args.writers)))
    both = time.perf_counter() - t
    handled = sum(r.handled for r in runners) - handled_before
    await asyncio.gather(*(r.stop(timeout=5) for r in runners))
    out["steady"] = {
        "enqueued_per_second": round(counter["n"] / both), "handled_per_second": round(handled / both),
        "oldest_due_seconds": {"p50": round(statistics.median(lags), 2) if lags else None, "max": round(max(lags), 2) if lags else None},
    }
    print(f"together: {counter['n'] / both:,.0f} enqueued/s, {handled / both:,.0f} run/s; oldest due job waited p50 {out['steady']['oldest_due_seconds']['p50']} s, max {out['steady']['oldest_due_seconds']['max']} s")

    # 6. An idle queue's claim: nothing due, the scheduled rows still waiting
    # (what a runner polls every second when there is no work).
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(sa.text("VACUUM ANALYZE jobs"))
        still = int(await conn.scalar(sa.text("SELECT count(*) FROM jobs WHERE finished_at IS NULL AND NOT dead AND run_at > now()")))
    async with engine.connect() as conn:
        trans = await conn.begin()
        plans = []
        for _ in range(5):
            plans.append((await conn.execute(sa.text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params)).scalars().all())
        await trans.rollback()
    times = [float(next(line for line in p if line.startswith("Execution Time")).split()[2]) for p in plans]
    out["idle_claim"] = {"scheduled_rows": still, "runs_ms": times, "median_ms": statistics.median(times), "plan": plans[-1]}
    print(f"idle claim with {still:,} rows scheduled for later: median {statistics.median(times):.2f} ms")

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2, default=str)
    await engine.dispose()


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
