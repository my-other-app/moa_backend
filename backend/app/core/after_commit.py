"""Work that should happen only once a transaction has committed.

An email that says "your plan has changed" must not go out for a change that
was then rolled back, and a request should not wait on the mail provider to
answer. `run_after_commit` queues a blocking call on the session; when the
session's outermost transaction commits, the queue runs on a worker thread in
the background. A rollback drops it -- and a SAVEPOINT rolled back drops
what was queued inside it, keeping the rest.

A queued call never raises into anything: whatever goes wrong is logged.
Tests call `drain()` to wait for the queue before checking what was sent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_QUEUE = "after_commit:queue"
_HOOKED = "after_commit:hooked"

#: Background runs not yet finished, held so they are not collected mid-run.
_running: set[asyncio.Task] = set()


def _sync(session) -> Session:
    return getattr(session, "sync_session", session)


def run_after_commit(session, func: Callable[..., Any], /, *args, **kwargs) -> None:
    """Call `func(*args, **kwargs)` on a worker thread after `session` commits."""
    sync = _sync(session)
    # The innermost transaction it was queued in: rolling that back (or
    # anything enclosing it) un-queues it.
    here = sync.get_nested_transaction() or sync.get_transaction()
    sync.info.setdefault(_QUEUE, []).append((here, func, args, kwargs))
    if not sync.info.get(_HOOKED):
        event.listen(sync, "after_commit", _on_commit)
        event.listen(sync, "after_soft_rollback", _on_rollback)
        sync.info[_HOOKED] = True


def _on_commit(sync: Session) -> None:
    # A SAVEPOINT committing fires this too; only the outermost one counts.
    if sync.in_nested_transaction():
        return
    jobs = sync.info.pop(_QUEUE, None)
    if jobs:
        _dispatch([(func, args, kwargs) for _here, func, args, kwargs in jobs])


def _within(queued_in, rolled_back) -> bool:
    if queued_in is None:
        return rolled_back.parent is None
    while queued_in is not None:
        if queued_in is rolled_back:
            return True
        queued_in = queued_in.parent
    return False


def _on_rollback(sync: Session, previous_transaction) -> None:
    queue = sync.info.get(_QUEUE)
    if queue:
        sync.info[_QUEUE] = [job for job in queue if not _within(job[0], previous_transaction)]


def _run(jobs) -> None:
    for func, args, kwargs in jobs:
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("after-commit job %s failed", getattr(func, "__name__", func), exc_info=True)


def _dispatch(jobs) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (a script): run now, in line.
        _run(jobs)
        return
    task = loop.create_task(asyncio.to_thread(_run, jobs))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def drain() -> None:
    """Wait for everything queued so far to finish. For tests and shutdown."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)
