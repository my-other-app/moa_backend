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

Durable jobs. A job in `DURABLE` -- the mails nobody would otherwise know
were lost -- is also written to `after_commit_jobs` in the same transaction
as the change it is about (the outbox pattern). After the commit it runs at
once, as any other, and its row is deleted when it has run. If the process
stops first (a deploy, a crash, a shutdown that ran out of time), the row
stays, and the minute sweep's resumer (`resume`) runs it, with a growing
wait between tries, until it has run or MAX_ATTEMPTS have failed (then it
is marked dead and logged). At least once: a process that stops between the
job finishing and its row going runs it again later. For a mail or a post,
a second copy is better than none.

A job is durable only when it can be run again from the row: a module-level
function, named in DURABLE, whose arguments are JSON (a tuple comes back as
a list). Anything else stays in memory, as before.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Column, Integer, String, delete, event, select, update
from sqlalchemy.orm import Session

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

logger = logging.getLogger(__name__)

_QUEUE = "after_commit:queue"
_HOOKED = "after_commit:hooked"

#: Background runs not yet finished, held so they are not collected mid-run.
_running: set[asyncio.Task] = set()

#: The jobs that must happen, by "module:qualname". Each sends a mail or a
#: post and is safe to run twice. The callers are unchanged: queuing one of
#: these is what makes it durable.
DURABLE: dict[str, str] = {
    "app.api.admin_alerts.service:deliver": "an admin alert's mail and Discord post",
    "app.api.subscriptions.notices:_send": "a billing mail to a club's owner",
    "app.api.club_domains.service:_send": "a club's own-domain mail to its owner",
}

#: How long a durable job's row waits before the resumer may take it: the
#: run straight after the commit normally deletes it within a second.
GRACE = timedelta(minutes=2)
MAX_ATTEMPTS = 6

#: Where rows are settled after a run. app.db.core's sessions unless set.
session_factory: Optional[Callable[[], Any]] = None


class AfterCommitJobs(AbstractSQLModel):
    __tablename__ = "after_commit_jobs"
    __table_args__ = (sa.Index("ix_after_commit_jobs_due", "next_attempt_at", postgresql_where=sa.text("NOT dead")),)

    id = Column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job = Column(String(200), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    last_error = Column(String(400), nullable=True)
    dead = Column(Boolean, nullable=False, default=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _durably(sync: Session, func, args: tuple, kwargs: dict) -> Optional[uuid.UUID]:
    """Write the job's row in the session, if it is one that must happen."""
    name = job_name(func)
    if name not in DURABLE:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # A script with no event loop runs the job in line at the commit.
        return None
    try:
        payload = json.loads(json.dumps({"args": list(args), "kwargs": kwargs}))
    except (TypeError, ValueError):
        logger.warning("after-commit job %s has arguments that are not JSON; it is not durable", name)
        return None
    row = AfterCommitJobs(id=uuid.uuid4(), job=name, payload=payload, attempts=0, next_attempt_at=_now() + GRACE, created_at=_now())
    sync.add(row)
    return row.id


def run_after_commit(session, func: Callable[..., Any], /, *args, **kwargs) -> None:
    """Call `func(*args, **kwargs)` on a worker thread after `session` commits."""
    sync = _sync(session)
    # The innermost transaction it was queued in: rolling that back (or
    # anything enclosing it) un-queues it -- and its durable row with it.
    here = sync.get_nested_transaction() or sync.get_transaction()
    durable_id = _durably(sync, func, args, kwargs)
    sync.info.setdefault(_QUEUE, []).append((here, func, args, kwargs, durable_id))
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
        _dispatch([(func, args, kwargs, durable_id) for _here, func, args, kwargs, durable_id in jobs])


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


def _run(jobs) -> list[tuple[Optional[uuid.UUID], Optional[str]]]:
    """Each job in turn: (its durable row, the error if it raised)."""
    out = []
    for func, args, kwargs, durable_id in jobs:
        try:
            func(*args, **kwargs)
            out.append((durable_id, None))
        except Exception as exc:  # noqa: BLE001
            logger.warning("after-commit job %s failed", getattr(func, "__name__", func), exc_info=True)
            out.append((durable_id, (str(exc) or exc.__class__.__name__)[:400]))
    return out


def _dispatch(jobs) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (a script): run now, in line.
        _run(jobs)
        return
    task = loop.create_task(_run_and_settle(jobs))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _run_and_settle(jobs) -> None:
    results = await asyncio.to_thread(_run, jobs)
    done = [i for i, err in results if i is not None and err is None]
    failed = [(i, err) for i, err in results if i is not None and err is not None]
    if done or failed:
        await _settle(done, failed)


def _session():
    if session_factory is not None:
        return session_factory()
    from app.db.core import AsyncSessionLocal

    return AsyncSessionLocal()


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** max(attempts - 1, 0), 60))


async def _settle(done: list, failed: list) -> None:
    """Delete the rows of jobs that ran; give the ones that raised a later try."""
    try:
        async with _session() as s:
            if done:
                await s.execute(delete(AfterCommitJobs).where(AfterCommitJobs.id.in_(done)))
            for job_id, error in failed:
                await s.execute(
                    update(AfterCommitJobs)
                    .where(AfterCommitJobs.id == job_id)
                    .values(attempts=AfterCommitJobs.attempts + 1, last_error=error, next_attempt_at=_now() + backoff(1))
                )
            await s.commit()
    except Exception:  # noqa: BLE001 - the resumer runs it again; a second copy beats none
        logger.warning("after-commit jobs could not be settled", exc_info=True)


async def drain(timeout: Optional[float] = None) -> bool:
    """Wait for everything queued so far to finish. For tests and shutdown:
    with a timeout, stops waiting then and says whether all of it finished."""

    async def wait_all():
        while _running:
            await asyncio.gather(*list(_running), return_exceptions=True)

    if timeout is None:
        await wait_all()
        return True
    try:
        await asyncio.wait_for(wait_all(), timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("%d after-commit job(s) still running at shutdown; durable ones are resumed later", len(_running))
        return False


async def resume(session, now: Optional[datetime] = None, limit: int = 50) -> int:
    """Run the durable jobs whose process stopped before they ran. Each is
    given its next try's time before it runs, so one that takes the process
    down with it is not taken again at once. Returns how many ran."""
    now = now or _now()
    rows = list(
        await session.scalars(
            select(AfterCommitJobs)
            .where(AfterCommitJobs.dead == False, AfterCommitJobs.next_attempt_at <= now)  # noqa: E712
            .order_by(AfterCommitJobs.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return 0
    taken = []
    for row in rows:
        row.attempts = (row.attempts or 0) + 1
        row.next_attempt_at = now + backoff(row.attempts)
        taken.append((row.id, row.job, row.payload or {}, row.attempts))
    await session.commit()
    ran = 0
    for job_id, name, payload, attempts in taken:
        error = None
        try:
            func = resolve(name)
            await asyncio.to_thread(func, *payload.get("args", []), **payload.get("kwargs", {}))
        except Exception as exc:  # noqa: BLE001
            error = (str(exc) or exc.__class__.__name__)[:400]
            logger.warning("durable after-commit job %s failed (try %d)", name, attempts, exc_info=True)
        if error is None:
            await session.execute(delete(AfterCommitJobs).where(AfterCommitJobs.id == job_id))
            ran += 1
        elif attempts >= MAX_ATTEMPTS:
            await session.execute(update(AfterCommitJobs).where(AfterCommitJobs.id == job_id).values(dead=True, last_error=error))
            logger.error("durable after-commit job %s gave up after %d tries: %s", name, attempts, error)
        else:
            await session.execute(update(AfterCommitJobs).where(AfterCommitJobs.id == job_id).values(last_error=error))
        await session.commit()
    return ran
