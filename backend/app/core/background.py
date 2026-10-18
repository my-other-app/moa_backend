"""Work that runs after the response, on a session of its own.

`dispatch(session, job)` runs `job(session)` as a background task with a
fresh session on the caller's engine, so the request answers without waiting
for it: a place looked up for a sign-in. It never raises into the caller,
and what goes wrong is logged.

Not durable: a job in flight when the process stops is lost. Work that must
happen -- mails, renders, push fan-outs -- is a job in the queue instead
(app/core/jobs.py: written with the change, retried, run by the worker when
there is one); this is left for cheap, best-effort work whose loss costs
nothing but a moment's information.

In the test suite the session is bound to one connection inside a
transaction that is rolled back (tests/conftest.py). There the job runs
inline on that session, as app.db.locks.singleton takes its lock there, so a
test sees what the job wrote. `drain()` waits for jobs started so far.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

logger = logging.getLogger(__name__)

#: Jobs running at once. Each holds a pooled connection only while it reads
#: or writes, but a burst should not take the pool from the requests.
MAX_CONCURRENT = 4

Job = Callable[[AsyncSession], Awaitable[object]]

_running: set[asyncio.Task] = set()
_slots: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_CONCURRENT)
    return _slots


def runs_inline(session: AsyncSession) -> bool:
    """The tests' session: bound to a connection, inside their transaction."""
    return isinstance(session.bind, AsyncConnection)


async def dispatch(session: AsyncSession, job: Job, *, name: str):
    """Run `job` on a session of its own after the caller returns. Inline,
    with its result, on the tests' session; None otherwise. Never raises."""
    if runs_inline(session):
        try:
            return await job(session)
        except Exception:  # noqa: BLE001
            logger.exception("background job %s failed", name)
            return None
    task = asyncio.get_running_loop().create_task(_run(session.bind, job, name), name=f"bg:{name}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return None


async def _run(engine, job: Job, name: str) -> None:
    from app.db.core import SESSION_KWARGS
    from app.db.listeners import add_loader_criteria

    async with _semaphore():
        try:
            async with AsyncSession(bind=engine, **SESSION_KWARGS) as session:
                add_loader_criteria(session)
                await job(session)
        except Exception:  # noqa: BLE001
            logger.exception("background job %s failed", name)


async def drain() -> None:
    """Wait for every job started so far. For tests and shutdown."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)
