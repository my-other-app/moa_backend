import logging
import os
from typing import Annotated, Optional
from fastapi import Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.db.listeners import add_loader_criteria
from app.config import settings
from app.db.registry import *
from sqlalchemy import text as sa_text  # after the star import, which re-exports the models' modules' names


def pool_for(config=settings) -> tuple[int, int]:
    """This process's pool, (pool_size, max_overflow): the job worker's own
    (WORKER_DB_POOL_SIZE, WORKER_DB_MAX_OVERFLOW) in `python -m app.worker`,
    the API's (DB_POOL_SIZE, DB_MAX_OVERFLOW) in each of uvicorn's workers."""
    if config.PROCESS == "worker":
        return config.WORKER_DB_POOL_SIZE, config.WORKER_DB_MAX_OVERFLOW
    return config.DB_POOL_SIZE, config.DB_MAX_OVERFLOW


# Each API worker process keeps its own pool: DB_POOL_SIZE connections open
# and up to DB_MAX_OVERFLOW more under load (10 and 20 by default, 30 a
# worker), and the job worker one of its own (pool_for). WORKERS times the
# API's, plus the job worker's, must fit in Postgres's max_connections; four
# workers at 30 each ran a 100-connection server out in every load test
# (docs/SCALING.md), and check_connection_budget says so at startup.
# plan_cache_mode: asyncpg prepares every statement, and after five runs
# Postgres may switch a prepared statement to one "generic" plan made without
# the parameters' values. For a query whose best plan depends on them -- a
# search for "ai" (no trigram to look up) against one for "robotics", or a
# date that is now rather than any date -- the generic plan can be ten times
# slower (scripts/bench/RESULTS.md has one). Planning each run with the real
# values costs a fraction of a millisecond, which is what every statement
# paid for its first five runs anyway.
_POOL_SIZE, _MAX_OVERFLOW = pool_for()
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_recycle=1800,
    pool_pre_ping=True,
    connect_args={"server_settings": {"plan_cache_mode": "force_custom_plan"}},
)

# Shared so tests can build their own session against the same semantics.
# expire_on_commit=False is required, not a preference: with the default True,
# commit() expires every instance in the identity map, and the next attribute
# read on one of them issues a *synchronous* refresh. On an async engine that
# raises MissingGreenlet.
#
# This caused a production outage on GET /events/info/{id}: the router calls
# increment_event_page_view (which commits), then evaluates `user.id` to pass
# into the service -- by then the auth dependency's Users instance was expired,
# so every signed-in request 500'd while anonymous ones succeeded.
SESSION_KWARGS = dict(autocommit=False, autoflush=False, expire_on_commit=False)

AsyncSessionLocal = async_sessionmaker(bind=engine, **SESSION_KWARGS)


async def get_session():
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

_budget_logger = logging.getLogger("app.db.budget")


def connection_budget(
    workers: int, pool_size: int, max_overflow: int, max_connections: int, reserved: int = 0,
    job_worker: Optional[tuple[int, int]] = None,
) -> Optional[str]:
    """What is wrong with this many workers' pools on this server, or None.

    Every API worker may open pool_size + max_overflow connections, and the
    job worker, when there is one, its own (pool, overflow); the server
    takes max_connections, of which `reserved` are kept for superusers."""
    wanted = workers * (pool_size + max_overflow)
    who = f"{workers} worker(s) x (pool {pool_size} + overflow {max_overflow})"
    if job_worker is not None:
        wanted += sum(job_worker)
        who += f" + the job worker (pool {job_worker[0]} + overflow {job_worker[1]})"
    room = max_connections - reserved
    if wanted <= room:
        return None
    return (
        f"{who} = {wanted} connections, "
        f"but Postgres takes {room} ({max_connections} max_connections, {reserved} reserved). "
        "Under load requests will fail with TooManyConnectionsError: lower APP_DB_POOL_SIZE / "
        "APP_DB_MAX_OVERFLOW, APP_WORKER_DB_POOL_SIZE / APP_WORKER_DB_MAX_OVERFLOW or APP_WORKERS, "
        "or put PgBouncer in front."
    )


def job_worker_pool(config=settings) -> Optional[tuple[int, int]]:
    """The job worker's pool when there is one to count: always in the
    worker itself; in the API once it leaves its jobs or its sweeps to one."""
    if config.PROCESS == "worker" or config.JOBS_WORKER or config.SWEEPS_IN_WORKER:
        return config.WORKER_DB_POOL_SIZE, config.WORKER_DB_MAX_OVERFLOW
    return None


async def check_connection_budget(bind=None) -> Optional[str]:
    """Ask the server for max_connections and warn (once per process, at
    startup) when the workers' pools could ask for more. Never raises: a
    server that cannot be asked is not a reason to stop booting."""
    try:
        async with (bind or engine).connect() as conn:
            max_connections = int(await conn.scalar(sa_text("SELECT current_setting('max_connections')::int")))
            reserved = int(await conn.scalar(sa_text("SELECT current_setting('superuser_reserved_connections')::int")))
    except Exception:  # noqa: BLE001
        _budget_logger.warning("could not read max_connections to check the connection budget", exc_info=True)
        return None
    problem = connection_budget(
        settings.WORKERS, settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW, max_connections, reserved,
        job_worker=job_worker_pool(),
    )
    if problem:
        _budget_logger.warning("connection budget: %s", problem)
    return problem

# Every SQL statement with its parameters, to stdout and logs/sql.log -- only
# when asked for (APP_SQL_LOG=true). It used to be always on, in production
# too: the parameters carry people's emails, phones and names, and the file
# grew without bound.
if settings.SQL_LOG:
    logging.basicConfig()
    logger = logging.getLogger("sqlalchemy.engine")
    logger.setLevel(logging.INFO)

    os.makedirs("logs", exist_ok=True)

    file_handler = logging.FileHandler("logs/sql.log")
    file_handler.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
