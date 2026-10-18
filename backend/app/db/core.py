import logging
import os
from typing import Annotated, Optional
from fastapi import Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.db.listeners import add_loader_criteria
from app.config import settings
from app.db.registry import *
from sqlalchemy import text as sa_text  # after the star import, which re-exports the models' modules' names

# Each worker process keeps its own pool: DB_POOL_SIZE connections open and up
# to DB_MAX_OVERFLOW more under load (10 and 20 by default, 30 a worker).
# WORKERS times that must fit in Postgres's max_connections; four workers
# at 30 each ran a 100-connection server out in every load test
# (docs/SCALING.md), and check_connection_budget says so at startup.
# plan_cache_mode: asyncpg prepares every statement, and after five runs
# Postgres may switch a prepared statement to one "generic" plan made without
# the parameters' values. For a query whose best plan depends on them -- a
# search for "ai" (no trigram to look up) against one for "robotics", or a
# date that is now rather than any date -- the generic plan can be ten times
# slower (scripts/bench/RESULTS.md has one). Planning each run with the real
# values costs a fraction of a millisecond, which is what every statement
# paid for its first five runs anyway.
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
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


def connection_budget(workers: int, pool_size: int, max_overflow: int, max_connections: int, reserved: int = 0) -> Optional[str]:
    """What is wrong with this many workers' pools on this server, or None.

    Every worker may open pool_size + max_overflow connections; the server
    takes max_connections, of which `reserved` are kept for superusers."""
    wanted = workers * (pool_size + max_overflow)
    room = max_connections - reserved
    if wanted <= room:
        return None
    return (
        f"{workers} worker(s) x (pool {pool_size} + overflow {max_overflow}) = {wanted} connections, "
        f"but Postgres takes {room} ({max_connections} max_connections, {reserved} reserved). "
        "Under load requests will fail with TooManyConnectionsError: lower APP_DB_POOL_SIZE / "
        "APP_DB_MAX_OVERFLOW or APP_WORKERS, or put PgBouncer in front."
    )


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
    problem = connection_budget(settings.WORKERS, settings.DB_POOL_SIZE, settings.DB_MAX_OVERFLOW, max_connections, reserved)
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
