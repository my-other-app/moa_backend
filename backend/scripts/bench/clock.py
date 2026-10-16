"""Time the closeout status clock (closeout.service.advance_clock), which the
web worker runs every minute, against the seeded database. Each run is
rolled back, so the data is left as it was.

    BENCH_DB=moa_bench python -m scripts.bench.clock
"""
from __future__ import annotations

import asyncio
import logging
import time

from scripts.bench import _env  # noqa: F401  (settings before app)

from sqlalchemy import event


async def main(runs: int = 3) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.closeout import service as closeout
    from app.db.core import SESSION_KWARGS, engine
    from app.db.listeners import add_loader_criteria

    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
    seen = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(*_args):
        seen["n"] += 1

    for i in range(runs):
        async with engine.connect() as conn:
            tx = await conn.begin()
            session = async_sessionmaker(bind=conn, **SESSION_KWARGS)()
            add_loader_criteria(session)
            seen["n"] = 0
            t0 = time.perf_counter()
            moved = await closeout.advance_clock(session)
            took = (time.perf_counter() - t0) * 1000
            print(f"advance_clock run {i + 1}: {took:8.1f} ms  {seen['n']:4d} statements  moved {moved}", flush=True)
            await session.close()
            await tx.rollback()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
