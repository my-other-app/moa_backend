"""How a send runs: rows claimed, a few in flight, at the provider's pace,
and no database connection held while a mail is rendered or sent.

The tables are the outbox -- `deliveries` for tickets and certificates,
`message_deliveries` for messages -- and there is no other queue:

- A row is queued `pending`. A worker claims it (`pending -> sending`, with
  `claimed_at`) in one UPDATE ... WHERE status = 'pending' over a batch
  picked FOR UPDATE SKIP LOCKED, so two workers, two clicks or two
  processes never take the same row.
- `pump` runs MAIL_CONCURRENCY workers over a claim function. Each worker
  reads what it needs in one short transaction, lets go of the connection,
  takes a token from the process's bucket (app.core.email.pace), renders,
  sends, and records the result in another short transaction. Thirty sends
  at once used to hold thirty connections idle through SES's answer, which
  is the whole pool.
- The provider saying "not now" (`MailDeferred`) puts the row back to
  `pending` with a backoff that doubles from a minute; after
  MAIL_MAX_ATTEMPTS tries it is `dead`, and the admins are told.
- The resumer, every minute in the email sweep, puts back claims older than
  STALE_CLAIM (a worker that died with the process), gives up on rows too
  old to be worth sending, and starts a job for whatever is due.

`Db` is where the short transactions come from: a fresh session from the
scope each time, or -- for a caller that hands its own session over -- that
session, committed after each unit. Either way a session is used by one
worker at a time (`session_lock`), which also lets the tests run the workers on
their one rolled-back session.

Jobs are tracked by key (`spawn`, `run`), so the resumer does not start a
second one for a message already sending in this process, and shutdown can
stop them: no new claims, the ones in hand finished, the unstarted given back.
"""
from __future__ import annotations

import asyncio
import logging
import random
import weakref
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Hashable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: A claim older than this is a worker that stopped (a deploy, a crash).
STALE_CLAIM = timedelta(minutes=10)
#: A row queued and untouched this long lost the worker that was to take it.
ABANDONED = timedelta(minutes=2)


def now() -> datetime:
    return datetime.now(timezone.utc)


def backoff(tries: int) -> timedelta:
    """1, 2, 4, 8, 16 minutes... at most 30, a little jittered so a batch
    that failed together does not come back together."""
    seconds = min(60 * 2 ** max(tries - 1, 0), 30 * 60)
    return timedelta(seconds=seconds * random.uniform(0.9, 1.1))


def max_tries() -> int:
    return max(1, int(settings.MAIL_MAX_ATTEMPTS))


# ─── Short transactions ──────────────────────────────────────────────────

_locks: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


def session_lock(session) -> asyncio.Lock:
    """The lock a task holds while it uses `session`. Held by `Db` around
    each unit, and by anything else that shares a session with workers."""
    lock = _locks.get(session)
    if lock is None:
        lock = _locks[session] = asyncio.Lock()
    return lock


@asynccontextmanager
async def _committing(session):
    try:
        yield session
    except BaseException:
        await asyncio.shield(session.rollback())
        raise
    else:
        await session.commit()


class Db:
    """Short transactions for a worker, and a stand-in for a session for
    code that only reads (the render functions): every call is its own
    transaction, so no connection is held between them -- across a render,
    say, or a provider's answer."""

    def __init__(self, scope: Callable[[], Any], session=None):
        self._scope = scope
        self._session = session

    @asynccontextmanager
    async def tx(self):
        if self._session is not None:
            async with session_lock(self._session):
                async with _committing(self._session) as s:
                    yield s
            return
        async with self._scope() as s:
            async with session_lock(s):
                async with _committing(s) as s2:
                    yield s2

    async def scalar(self, *args, **kwargs):
        async with self.tx() as s:
            return await s.scalar(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        async with self.tx() as s:
            return await s.scalars(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        async with self.tx() as s:
            return await s.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        async with self.tx() as s:
            return await s.get(*args, **kwargs)

    # Every call above already committed.
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None


# ─── Jobs ────────────────────────────────────────────────────────────────

_jobs: dict[Hashable, asyncio.Task] = {}
_stopping = False


def stopping() -> bool:
    return _stopping


def running(key: Hashable) -> bool:
    task = _jobs.get(key)
    return task is not None and not task.done()


async def _guarded(key, factory: Callable[[], Awaitable[Any]]) -> None:
    try:
        await factory()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - one job's trouble is logged, not spread
        logger.exception("mail job %s failed", key)


def spawn(key: Hashable, factory: Callable[[], Awaitable[Any]]) -> asyncio.Task:
    """Start the job unless one with this key is running here; either way, its task."""
    task = _jobs.get(key)
    if task is not None and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(_guarded(key, factory))
    _jobs[key] = task

    def forget(t, key=key):
        if _jobs.get(key) is t:
            _jobs.pop(key, None)

    task.add_done_callback(forget)
    return task


async def run(key: Hashable, factory: Callable[[], Awaitable[Any]]) -> None:
    """Spawn and wait. The wait may be cancelled (a request's background
    task, at shutdown); the job is not, and shutdown deals with it."""
    await asyncio.shield(spawn(key, factory))


async def drain(timeout: Optional[float] = None) -> None:
    """Wait for every job, including those started meanwhile. For tests."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while True:
        live = [t for t in _jobs.values() if not t.done()]
        if not live:
            return
        left = None if deadline is None else max(0.0, deadline - loop.time())
        done, _ = await asyncio.wait(live, timeout=left)
        if not done and left == 0.0:
            return


async def shutdown(timeout: float) -> bool:
    """No new claims; the sends in hand get `timeout` seconds to finish and
    be recorded; what is left is cancelled (its rows are resumed later).
    Returns whether everything finished in time."""
    global _stopping
    _stopping = True
    live = [t for t in _jobs.values() if not t.done()]
    if not live:
        return True
    _, pending = await asyncio.wait(live, timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)
        logger.warning("%d mail job(s) cut off at shutdown; their rows are resumed by the next process", len(pending))
    return not pending


def reset_for_tests() -> None:
    global _stopping
    _stopping = False
    _jobs.clear()


# ─── The pump ────────────────────────────────────────────────────────────


async def pump(
    claim: Callable[[int], Awaitable[list]],
    handle: Callable[[Any], Awaitable[None]],
    *,
    release: Optional[Callable[[list], Awaitable[None]]] = None,
    concurrency: Optional[int] = None,
) -> int:
    """Claim rows `concurrency` at a time and `handle` each, `concurrency`
    at once, until nothing is left to claim or the process is stopping.
    Rows claimed and not yet started when it stops (or is cancelled) are
    handed to `release`. Returns how many were handled."""
    width = max(1, int(concurrency or settings.MAIL_CONCURRENCY))
    buffer: list = []
    gate = asyncio.Lock()
    exhausted = False
    handled = 0

    async def next_row():
        nonlocal exhausted
        async with gate:
            if not buffer and not exhausted and not _stopping:
                rows = await claim(width)
                if rows:
                    buffer.extend(rows)
                else:
                    exhausted = True
            if _stopping or not buffer:
                return None
            return buffer.pop(0)

    async def worker():
        nonlocal handled
        while True:
            row = await next_row()
            if row is None:
                return
            try:
                await handle(row)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the row's own record says what happened
                logger.exception("a mail worker could not finish %r", row)
            handled += 1

    try:
        await asyncio.gather(*(worker() for _ in range(width)))
    finally:
        if buffer and release is not None:
            left, buffer[:] = list(buffer), []
            try:
                await asyncio.shield(release(left))
            except BaseException:  # noqa: BLE001 - the resumer puts them back anyway
                logger.warning("could not give back %d claimed row(s); the resumer will", len(left))
    return handled


async def send_with_backoff(fn, /, **kwargs):
    """`fn(**kwargs)` on the mail threads after a token from the bucket.
    Throttled, it slows the bucket and tries again, twice; then the
    throttling is the caller's to record."""
    from app.core.email import pace, transport

    bucket = pace.bucket()
    for attempt in range(3):
        await bucket.take()
        try:
            return await pace.blocking(fn, **kwargs)
        except transport.MailThrottled:
            bucket.throttled()
            if attempt == 2:
                raise


async def alert_dead(session, *, event_id: int, club_id: Optional[int], event_name: str, what: str, error: str) -> None:
    """Tell the admins a mail was given up on. One alert per event in ten
    minutes, however many went (admin_alerts' own dedupe)."""
    from app.api.admin_alerts import notify

    await notify(
        session,
        "email_dead_letter",
        title=f"Emails for {event_name or 'an event'} were given up on",
        body=f"A {what} was tried {max_tries()} times while the mail provider was failing, and given up on. Last error: {error}"[:2000],
        club=club_id,
        data={"event_id": event_id, "what": what, "error": (error or "")[:200]},
        dedupe_on={"event_id": event_id},
    )
