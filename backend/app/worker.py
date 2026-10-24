"""The worker: `python -m app.worker` (the `worker` service in
docker-compose.prod.yml).

It runs the job queue (app/core/jobs.py) away from the requests: with
APP_JOBS_WORKER=true on the API, the API only writes jobs and this process
runs them -- mails, renders, push fan-outs, retention runs -- so a slow
provider or a big send never takes the event loop or the database pool from
a page, and a deploy of the API does not cut a job off half way.

With APP_SWEEPS_IN_WORKER=true it also runs the leader-only loops doth.py's
lifespan otherwise starts (the email, closeout, billing, payments... sweeps).
Each still takes its advisory lock, so however many processes start them,
one runs each.

It imports the whole app (doth), so it knows every handler the API can
enqueue. On SIGTERM it stops taking jobs, gives the ones in hand
APP_JOBS_DRAIN_SECONDS to finish, and hands the rest back to the queue;
docker-compose gives it a little longer than that before killing it.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import signal

logger = logging.getLogger("app.worker")


async def _wake_on_redis(runner) -> None:
    """With Redis, an API that only enqueues says so on a channel, and the
    worker takes the job at once instead of at its next poll."""
    from app.core import jobs, redis

    while True:
        try:
            async for _ in redis.subscribe(jobs.WAKE_CHANNEL):
                runner.kick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the poll still finds the jobs
            logger.warning("listening for job wake-ups failed; polling only for a while", exc_info=True)
        await asyncio.sleep(5)


async def main() -> None:
    import doth  # noqa: F401 - registers every handler, as the API does
    from app.config import settings
    from app.core import config_checks, jobs, redis
    from app.db.core import check_connection_budget, engine

    # The settings check's findings, now that Sentry is on (doth started
    # it); under APP_CONFIG_CHECKS=enforce a fatal one already stopped the
    # process before the app was imported (below).
    config_checks.enforce()
    await check_connection_budget()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    runner = jobs.Runner(kind="worker", sweeps=settings.SWEEPS_IN_WORKER).start()
    jobs.use(runner)
    loops = doth.start_loops() if settings.SWEEPS_IN_WORKER else []
    waker = loop.create_task(_wake_on_redis(runner)) if redis.client() is not None else None
    # As the API does after startup: what import built lives as long as the
    # process, so the collector need not walk it (doth.lifespan).
    gc.collect()
    gc.freeze()
    logger.info(
        "worker %s running: %d job(s) at once, %d handler(s)%s",
        runner.id, runner.concurrency, len(jobs.handlers()), ", and the sweeps" if loops else "",
    )
    await stop.wait()
    logger.info("worker %s stopping", runner.id)
    if waker is not None:
        waker.cancel()
        await asyncio.gather(waker, return_exceptions=True)
    # The loops, then the jobs in hand, then Redis (doth.shutdown).
    await doth.shutdown(loops, budget=float(settings.JOBS_DRAIN_SECONDS), runner=runner)
    await engine.dispose()
    logger.info("worker %s stopped", runner.id)


if __name__ == "__main__":
    import os

    # Before anything reads the settings: this process takes the worker's
    # own pool (APP_WORKER_DB_POOL_SIZE / _MAX_OVERFLOW, app.db.core.pool_for)
    # and is counted as the job worker in the connection budget.
    os.environ["APP_PROCESS"] = "worker"
    from app.core import observability

    # APP_LOG_JSON: JSON lines, as the API's (uvicorn is not here to set them up).
    if observability.configure_logging():
        logger.setLevel(logging.INFO)
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # As the API: under APP_CONFIG_CHECKS=enforce, no start on a missing,
    # placeholder or half-set secret in production (app/core/config_checks.py),
    # before the app is imported. Under warn, main() reports them.
    from app.core import config_checks

    config_checks.enforce_or_exit(report=False)
    try:
        import uvloop
    except ImportError:  # pragma: no cover - in requirements.txt, as uvicorn uses it
        uvloop = None
    if uvloop is not None:
        uvloop.run(main())
    else:  # pragma: no cover
        asyncio.run(main())
