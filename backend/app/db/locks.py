"""One process at a time: a Postgres advisory lock held on a connection of its own.

Every web worker runs the same background loops (the timed-email sweep, the
status clock, the counter check). Each takes a session-level advisory lock
first, and only the process that gets it does the work.

The lock lives on a connection, so it has to be taken and released on the
same one. A session is the wrong place for it: an AsyncSession hands its
connection back to the pool at every commit, so a lock taken through the
session stayed held by whichever pooled connection had taken it, the unlock
ran on another connection (and released nothing), and the other workers could
not take the lock again until that connection was recycled. `singleton` takes
it on a connection checked out for just this, in autocommit, so no
transaction is left open while the work runs on its own session.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@asynccontextmanager
async def singleton(target, key: int | tuple[int, int]) -> AsyncIterator[bool]:
    """Yields whether this process holds advisory lock `key` for the block.

    `key` is one bigint, or two ints (Postgres's two-key form: a kind of
    lock, and the row it is for).

    `target` is the engine, or the session the work will use: a session on an
    engine gets a separate connection for the lock; a session bound to one
    connection (the tests' transaction) takes it on that connection."""
    bind = target.bind if isinstance(target, AsyncSession) else target
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            async with _held(conn, key) as got:
                yield got
        return
    async with _held(bind, key) as got:
        yield got


@asynccontextmanager
async def _held(conn, key) -> AsyncIterator[bool]:
    keys = tuple(key) if isinstance(key, (tuple, list)) else (key,)
    got = bool(await conn.scalar(select(func.pg_try_advisory_lock(*keys))))
    try:
        yield got
    finally:
        if got:
            await conn.scalar(select(func.pg_advisory_unlock(*keys)))
