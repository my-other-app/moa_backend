import logging
import os
from typing import Annotated
from fastapi import Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.db.listeners import add_loader_criteria
from app.config import settings
from app.db.registry import *

# pool_size/max_overflow give each worker process up to 30 concurrent
# connections; check pool_size * settings.WORKERS against Postgres's
# max_connections if WORKERS is ever increased beyond 1.
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
    pool_size=10,
    max_overflow=20,
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
