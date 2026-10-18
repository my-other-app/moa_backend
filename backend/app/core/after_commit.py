"""Work that should happen only once a transaction has committed.

An email that says "your plan has changed" must not go out for a change that
was then rolled back, and a request should not wait on the mail provider to
answer. `run_after_commit` queues a blocking call on the session; when the
session's outermost transaction commits, the queue runs on a worker thread in
the background. A rollback drops it -- and a SAVEPOINT rolled back drops
what was queued inside it, keeping the rest.

A queued call never raises into anything: whatever goes wrong is logged.
Tests call `drain()` to wait for the queue before checking what was sent;
shutdown calls it with a timeout.

Durable calls. A call in `DURABLE` -- the mails nobody would otherwise know
were lost -- is not kept in memory at all: it becomes a job (app/core/jobs.py)
written in the same transaction as the change it is about, run once that
commits by this process's job runner (or the worker), retried with a growing
wait when the mail does not go, and given up on -- the admins told -- after
its last try. Each runs a strict twin of the function queued, one that raises
when the mail was not taken, so a provider outage is retried rather than
logged and forgotten.

This module used to keep its own outbox for these, `after_commit_jobs`, with
a resumer in the minute email sweep: one more queue beside the job queue,
with its own claim, backoff and dead letters. It is folded into the jobs now
(one mechanism); `adopt_legacy` moves any row still in the old table -- one
written by a process from before the change -- into `jobs`, and the table
can be dropped once it stays empty.

A call is durable only when it can be run again from its row: a module-level
function named in DURABLE, whose arguments are JSON (a tuple comes back as a
list). Anything else stays in memory, as before.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core import jobs

logger = logging.getLogger(__name__)

_QUEUE = "after_commit:queue"
_HOOKED = "after_commit:hooked"

#: Background runs not yet finished, held so they are not collected mid-run.
_running: set[asyncio.Task] = set()

#: The calls that must happen, by the function callers queue
#: ("module:qualname"): the job each becomes, the function that job runs
#: (raising when the mail did not go, so it is retried), and what it is.
DURABLE: dict[str, tuple[str, str, str]] = {
    "app.api.admin_alerts.service:deliver": (
        "admin_alerts.deliver", "app.api.admin_alerts.service:deliver", "an admin alert's mail and Discord post",
    ),
    "app.api.subscriptions.notices:_send": (
        "mail.billing_notice", "app.api.subscriptions.notices:send", "a billing mail to a club's owner, or a refund mail to a guest",
    ),
    "app.api.club_domains.service:_send": (
        "mail.club_domain_notice", "app.api.club_domains.service:send", "a club's own-domain mail to its owner",
    ),
}
#: The job each durable call becomes, by job name: the function it runs.
_JOB_RUNS: dict[str, str] = {job: runs for job, runs, _what in DURABLE.values()}


def _sync(session) -> Session:
    return getattr(session, "sync_session", session)


def job_name(func) -> str:
    return f"{getattr(func, '__module__', '')}:{getattr(func, '__qualname__', '')}"


def resolve(name: str) -> Callable:
    module, _, qualname = name.partition(":")
    target: Any = importlib.import_module(module)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _durably(session, func, args: tuple, kwargs: dict) -> bool:
    """Write the call as a job in the session, if it is one that must happen."""
    entry = DURABLE.get(job_name(func))
    if entry is None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # A script with no event loop runs the call in line at the commit.
        return False
    try:
        payload = json.loads(json.dumps({"args": list(args), "kwargs": kwargs}))
    except (TypeError, ValueError):
        logger.warning("after-commit call %s has arguments that are not JSON; it is not durable", job_name(func))
        return False
    jobs.add(session, entry[0], payload)
    return True


def run_after_commit(session, func: Callable[..., Any], /, *args, **kwargs) -> None:
    """Call `func(*args, **kwargs)` on a worker thread after `session` commits."""
    if _durably(session, func, args, kwargs):
        return
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
    queued = sync.info.pop(_QUEUE, None)
    if queued:
        _dispatch([(func, args, kwargs) for _here, func, args, kwargs in queued])


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
        sync.info[_QUEUE] = [item for item in queue if not _within(item[0], previous_transaction)]


def _run(calls) -> None:
    for func, args, kwargs in calls:
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("after-commit call %s failed", getattr(func, "__name__", func), exc_info=True)


def _dispatch(calls) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (a script): run now, in line.
        _run(calls)
        return
    task = loop.create_task(asyncio.to_thread(_run, calls))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def drain(timeout: Optional[float] = None) -> bool:
    """Wait for everything queued so far to finish: the in-memory calls, and
    the durable ones this process's job runner has in hand (under the test
    suite's runner, until they have run). For tests and shutdown: with a
    timeout, stops waiting then and says whether all of it finished."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout

    def left() -> Optional[float]:
        return None if deadline is None else max(0.0, deadline - loop.time())

    async def wait_all():
        while _running:
            await asyncio.gather(*list(_running), return_exceptions=True)

    try:
        await asyncio.wait_for(wait_all(), left())
    except asyncio.TimeoutError:
        logger.warning("%d after-commit call(s) still running at shutdown", len(_running))
        return False
    return await jobs.settle(left())


# ─── The jobs the durable calls become ───────────────────────────────────


async def _call(job: str, payload: dict) -> None:
    func = resolve(_JOB_RUNS[job])
    await asyncio.to_thread(func, *payload.get("args", []), **payload.get("kwargs", {}))


# The admin alert delivers itself: when it dies, there is nobody to tell
# but the log (and its row in the bell, which is already there).
@jobs.handler("admin_alerts.deliver", max_attempts=6, timeout=60.0, alert_dead=False)
async def _admin_alert(payload: dict) -> None:
    await _call("admin_alerts.deliver", payload)


@jobs.handler("mail.billing_notice", max_attempts=6, timeout=60.0)
async def _billing_notice(payload: dict) -> None:
    await _call("mail.billing_notice", payload)


@jobs.handler("mail.club_domain_notice", max_attempts=6, timeout=60.0)
async def _club_domain_notice(payload: dict) -> None:
    await _call("mail.club_domain_notice", payload)


# ─── The old outbox ──────────────────────────────────────────────────────


async def adopt_legacy(session, batch: int = 500) -> int:
    """Move the live rows of the old `after_commit_jobs` outbox into `jobs`,
    if that table is still there. Its attempts carry over, short of the last
    so each is tried at least once more; a call this code does not know is
    left where it is. Returns how many moved."""
    if await session.scalar(sa.text("SELECT to_regclass('after_commit_jobs')")) is None:
        return 0
    known = list(DURABLE)
    names = [DURABLE[k][0] for k in known]
    moved = (await session.execute(sa.text("""
        WITH map AS (
            SELECT * FROM unnest(CAST(:funcs AS text[]), CAST(:names AS text[])) AS m(func, name)
        ), moved AS (
            DELETE FROM after_commit_jobs a
            USING map
            WHERE a.job = map.func AND a.id IN (
                SELECT id FROM after_commit_jobs WHERE NOT dead AND job = ANY(CAST(:funcs AS text[]))
                ORDER BY next_attempt_at LIMIT :batch FOR UPDATE SKIP LOCKED
            )
            RETURNING map.name, a.payload, a.attempts, a.next_attempt_at, a.created_at
        )
        INSERT INTO jobs (name, payload, attempts, max_attempts, run_at, created_at, updated_at)
        SELECT name, CAST(CAST(payload AS text) AS jsonb), LEAST(attempts, :most - 1), :most, next_attempt_at, created_at, statement_timestamp()
        FROM moved
    """), {"funcs": known, "names": names, "batch": batch, "most": jobs.DEFAULT_MAX_ATTEMPTS})).rowcount or 0
    await session.commit()
    if moved:
        logger.info("moved %d row(s) of the old after-commit outbox into jobs", moved)
    return moved
