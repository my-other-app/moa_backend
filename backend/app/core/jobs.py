"""Durable jobs: the work a request sets off, written down with the change it
is about, and run once that change has committed -- by this process, or by
the worker.

Mails, renders, push fan-outs and retention runs used to happen in the web
process after the response, held only in memory (app/core/background.py, the
request's BackgroundTasks, after_commit's own outbox): a deploy or a crash in
between lost them or left them to a minute sweep, and they took the event
loop from the requests. Now each is a row in `jobs`:

- `enqueue(session, name, payload)` inserts the row in the caller's
  transaction, so a rolled-back change sets nothing off, and a committed one
  cannot lose its job. `dedupe_key` makes a second enqueue of the same thing
  a no-op while the first is unfinished (a partial unique index, ON CONFLICT
  DO NOTHING).
- A runner claims due rows in batches: `SELECT ... FOR UPDATE SKIP LOCKED`
  over the claim index (name, priority, run_at, id) among unfinished rows,
  so two runners -- two processes, or the API and the worker -- never take
  one row. Each (name, priority) is its own bounded range of the index --
  from the oldest due row to the first not yet due -- so a claim reads only
  rows it may take, however many wait for later (retries in backoff, jobs
  with a run_at): priority is one of PRIORITIES, and the claim walks them
  from the highest down, stopping once it has enough. A claim is a lease: `run_at` and `locked_until` move LEASE
  seconds ahead (less for a handler with a shorter timeout), and `attempts`
  goes up. While the job runs, its runner renews the lease every
  HEARTBEAT_SECONDS. A runner that dies mid-job (a deploy, an OOM) stops
  renewing; when the lease runs out the row is due again and another runner
  takes it -- the visibility timeout, with no sweeper, and within two
  minutes even for a job allowed an hour.
- A job that raises is retried after an exponential backoff with jitter;
  after `max_attempts` it is dead (kept, with its last error) and the admins
  are told once per name in ten minutes (admin alert `job_dead`).
- Settling is fenced by the attempt: a runner whose lease ran out and whose
  job was taken again cannot mark the newer attempt done.
- Each runner keeps at most JOBS_CONCURRENCY jobs in hand, and a handler's
  `concurrency` (or JOBS_LIMITS) caps how many of one name run at once: the
  ones that draw documents, close events out or read a sheet take one or two
  of a runner's slots at most, never all of them.
- A handler says how urgent its jobs are (`priority`: HIGH for the mail or
  push someone is waiting for, NORMAL for a send to many, LOW for work
  nobody watches), and a caller may say otherwise for one job. When a runner
  has room, the most urgent due job of any name goes first, so a ticket is
  not queued behind a hall's survey mails or a close-out report.
- On SIGTERM a runner stops claiming, gives the jobs in hand a while to
  finish, and hands back the rest with the attempt not counted.
- Finished rows are kept JOBS_KEEP_DAYS, dead ones JOBS_DEAD_KEEP_DAYS, then
  deleted in batches by the maintenance pass (one runner at a time).

Who runs them. With APP_JOBS_WORKER unset every API process has a runner of
its own (doth.py's lifespan): the commit that wrote a job wakes it, so the
job runs at once, as before -- now persisted and retried. With it set, the API
only writes jobs, and `python -m app.worker` runs them.

At least once. A runner can stop after a job's work and before its row is
settled; the job then runs again. Every handler is written so that a second
run changes nothing a first one did (a claimed outbox row, an insert that
skips what exists, a forward-only state machine).

Handlers are `async def fn(payload: dict) -> None`, registered with
`@handler(name)`. The payload is JSON: a UUID, a date or a Decimal given to
`enqueue` arrives as a string. A handler opens its own sessions
(`session_scope`): it runs long after the request's is gone.

The test suite installs an InlineRunner (tests/conftest.py): nothing runs in
the background, and the jobs a test's commits set off run on the test's own
session when it waits for them (`settle`) -- the client fixture does after
every request, as the API's runner would right after the response.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import secrets
import socket
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, Column, Identity, Integer, SmallInteger, String, Text, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db import lock_keys

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_TIMEOUT = 120.0
#: A claim's lease: renewed every HEARTBEAT_SECONDS while the job runs, so
#: this is how long a dead runner's job waits before another takes it. A
#: handler whose timeout (plus LEASE_GRACE) is shorter gets that instead.
LEASE = 120.0
LEASE_GRACE = 30.0
#: The wait before try n+1 is about BACKOFF_BASE * 2^(n-1) seconds, half of
#: it random, at most BACKOFF_CAP: 30 s, 1, 2, 4, 8 minutes for the default six.
BACKOFF_BASE = 30.0
BACKOFF_CAP = 3600.0
#: A runner says it is alive this often (job_workers.beat_at).
HEARTBEAT_SECONDS = 15.0
#: A runner not heard from in this long is gone (readiness, the admin page).
ALIVE_WITHIN = timedelta(seconds=60)
#: The maintenance pass: deleting kept rows, adopting the old outbox's rows.
MAINTENANCE_SECONDS = 300.0
CLEANUP_BATCH = 5000
#: Advisory lock: one runner at a time does the maintenance pass.
MAINTENANCE_LOCK = lock_keys.JOBS_MAINTENANCE
#: The priorities a job may have, highest first. Higher goes first among the
#: jobs due of a name; a value outside is taken as the nearest of these.
PRIORITIES = (2, 1, 0, -1, -2)
#: What handlers and callers use. HIGH: a mail or push someone is waiting
#: for -- their ticket, their receipt, an approval, an admin's alert. NORMAL:
#: a send to many (a broadcast, a club's announcement, the certificates of
#: a whole event). LOW: work nobody watches happen -- an event's close-out,
#: a sheet of registrations imported. 2 and -2 are left for a one-off job
#: that must go before, or after, all of these.
HIGH, NORMAL, LOW = 1, 0, -1

UNFINISHED = "finished_at IS NULL AND NOT dead"
DEDUPE_WHERE = f"dedupe_key IS NOT NULL AND {UNFINISHED}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── The tables ──────────────────────────────────────────────────────────


class Jobs(AbstractSQLModel):
    __tablename__ = "jobs"
    __table_args__ = (
        # The claim: per name, highest priority first, then the longest due.
        # Only unfinished rows, so it stays the size of the backlog.
        sa.Index("ix_jobs_claim", "name", "priority", "run_at", "id", postgresql_where=sa.text(UNFINISHED)),
        # One unfinished job per dedupe key; a finished or dead one frees it.
        sa.Index("uq_jobs_dedupe_key", "dedupe_key", unique=True, postgresql_where=sa.text(DEDUPE_WHERE)),
        # The maintenance pass's deletes, and the admin's dead letters.
        sa.Index("ix_jobs_finished_at", "finished_at", postgresql_where=sa.text("finished_at IS NOT NULL")),
        sa.Index("ix_jobs_dead", "updated_at", postgresql_where=sa.text("dead")),
        # The claim walks these levels; a row at any other could never be taken.
        sa.CheckConstraint("priority BETWEEN -2 AND 2", name="ck_jobs_priority"),
    )

    id = Column(BigInteger, Identity(), primary_key=True)
    name = Column(String(120), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    priority = Column(SmallInteger, nullable=False, server_default="0")
    # Not before this. While claimed: when the lease runs out (= locked_until).
    run_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()"))
    # Claims so far, this one included.
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default=str(DEFAULT_MAX_ATTEMPTS))
    locked_by = Column(String(120), nullable=True)
    locked_until = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    dedupe_key = Column(String(200), nullable=True)
    dead = Column(Boolean, nullable=False, server_default=sa.false())
    finished_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.text("statement_timestamp()"))


# A queue churns: vacuumed every few thousand dead rows, as migration
# w2_platform sets it; here for create_all (the test suite).
event.listen(Jobs.__table__, "after_create", sa.DDL(
    "ALTER TABLE jobs SET (autovacuum_vacuum_scale_factor = 0, autovacuum_vacuum_threshold = 2000, "
    "autovacuum_analyze_scale_factor = 0, autovacuum_analyze_threshold = 5000)"
))


class JobWorkers(AbstractSQLModel):
    """Each runner's heartbeat: what /readyz and the admin's jobs page read
    to say whether anything is running the queue."""

    __tablename__ = "job_workers"

    id = Column(String(120), primary_key=True)
    # "api" (an API process's own runner) or "worker".
    kind = Column(String(20), nullable=False)
    host = Column(String(200), nullable=True)
    pid = Column(Integer, nullable=True)
    # Whether this process runs the leader-only loops (SWEEPS_IN_WORKER).
    sweeps = Column(Boolean, nullable=False, server_default=sa.false())
    started_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    beat_at = Column(TZAwareDateTime(timezone=True), nullable=False)
    running = Column(Integer, nullable=False, server_default="0")
    handled = Column(BigInteger, nullable=False, server_default="0")
    failed = Column(BigInteger, nullable=False, server_default="0")


# ─── Handlers ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Handler:
    name: str
    fn: Callable[[dict], Awaitable[None]]
    max_attempts: int
    timeout: float
    concurrency: Optional[int]
    alert_dead: bool
    priority: int = NORMAL


_handlers: dict[str, Handler] = {}


def _qualname(fn) -> str:
    return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', '')}"


def handler(
    name: str, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS, timeout: float = DEFAULT_TIMEOUT,
    concurrency: Optional[int] = None, alert_dead: bool = True, priority: int = NORMAL,
):
    """Register `async def fn(payload: dict) -> None` as the job `name`.

    `max_attempts` tries, each given `timeout` seconds; `concurrency` caps
    how many of this name one runner has in hand at once (JOBS_LIMITS
    overrides it). `priority`: what its jobs are enqueued with unless the
    caller says (HIGH, NORMAL or LOW). `alert_dead=False` only logs a dead
    job -- for the job that delivers admin alerts itself, which must not
    alert about itself."""

    def register(fn):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"job {name}: the handler must be an async function")
        known = _handlers.get(name)
        if known is not None and _qualname(known.fn) != _qualname(fn):
            raise ValueError(f"job {name} has two handlers: {_qualname(known.fn)} and {_qualname(fn)}")
        _handlers[name] = Handler(name, fn, max(1, int(max_attempts)), float(timeout), concurrency, alert_dead, clamp_priority(priority))
        return fn

    return register


def handlers() -> dict[str, Handler]:
    return dict(_handlers)


def _limits_setting() -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (settings.JOBS_LIMITS or "").split(","):
        name, _, n = part.partition("=")
        if name.strip() and n.strip().isdigit():
            out[name.strip()] = int(n)
    return out


def lease_for(h: Handler) -> float:
    """Seconds a claim of this name holds before it must be renewed."""
    return min(LEASE, h.timeout + LEASE_GRACE)


def limit_for(h: Handler, total: int) -> int:
    """How many jobs of this name one runner may have in hand at once."""
    named = _limits_setting().get(h.name)
    cap = named if named is not None else (h.concurrency or total)
    return max(1, min(cap, total))


# ─── Enqueueing ──────────────────────────────────────────────────────────


def _plain(value: Any):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not something a job's payload can hold")


def clamp_priority(priority: int) -> int:
    return max(min(PRIORITIES), min(max(PRIORITIES), int(priority)))


def priority_for(h: Optional[Handler], priority: Optional[int]) -> int:
    """The caller's priority when it gave one, else the handler's."""
    if priority is None:
        priority = h.priority if h is not None else NORMAL
    return clamp_priority(priority)


def as_json(payload: Optional[dict]) -> dict:
    """The payload as the handler will see it: JSON, round-tripped now so a
    value that cannot be stored fails the enqueue, not the job."""
    return json.loads(json.dumps(payload or {}, default=_plain))


async def enqueue(
    session, name: str, payload: dict, *, run_at: Optional[datetime] = None,
    dedupe_key: Optional[str] = None, priority: Optional[int] = None,
) -> None:
    """Write job `name` in the caller's transaction; it runs once that
    commits. With `dedupe_key`, nothing is written while an unfinished job
    has the same key. `run_at`: not before then. Higher `priority` goes first
    (one of PRIORITIES, -2 to 2; the nearest is taken); unset, the handler's."""
    h = _handlers.get(name)
    if h is None:
        logger.warning("job %s is enqueued but no handler is registered in this process", name)
    values: dict[str, Any] = dict(
        name=name, payload=as_json(payload), priority=priority_for(h, priority),
        max_attempts=h.max_attempts if h else DEFAULT_MAX_ATTEMPTS, dedupe_key=dedupe_key,
    )
    if run_at is not None:
        values["run_at"] = run_at
    table = Jobs.__table__
    stmt = pg_insert(table).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[table.c.dedupe_key], index_where=sa.text(DEDUPE_WHERE))
    job_id = (await session.execute(stmt.returning(table.c.id))).scalar_one_or_none()
    if job_id is not None:
        _after_commit(session, job_id)


async def enqueue_many(
    session, name: str, items: Iterable[tuple[dict, Optional[str]]], *, priority: Optional[int] = None, chunk: int = 1000,
) -> int:
    """`enqueue` for many jobs of one name -- (payload, dedupe_key) each -- in
    a statement per `chunk` rather than one each: a survey to a hall is a job
    per guest. Returns how many were written (a key already waiting is not)."""
    h = _handlers.get(name)
    if h is None:
        logger.warning("job %s is enqueued but no handler is registered in this process", name)
    rows = [
        dict(name=name, payload=as_json(payload), priority=priority_for(h, priority),
             max_attempts=h.max_attempts if h else DEFAULT_MAX_ATTEMPTS, dedupe_key=dedupe_key)
        for payload, dedupe_key in items
    ]
    table = Jobs.__table__
    written = 0
    for i in range(0, len(rows), chunk):
        stmt = (
            pg_insert(table).values(rows[i:i + chunk])
            .on_conflict_do_nothing(index_elements=[table.c.dedupe_key], index_where=sa.text(DEDUPE_WHERE))
            .returning(table.c.id)
        )
        for job_id in (await session.execute(stmt)).scalars().all():
            _after_commit(session, job_id)
            written += 1
    return written


def add(session, name: str, payload: dict, *, priority: Optional[int] = None) -> Jobs:
    """`enqueue` for code that cannot await (app.core.after_commit): the row
    is added to the session and written with its next flush. No dedupe."""
    h = _handlers.get(name)
    row = Jobs(name=name, payload=as_json(payload), priority=priority_for(h, priority), max_attempts=h.max_attempts if h else DEFAULT_MAX_ATTEMPTS)
    sync = getattr(session, "sync_session", session)
    sync.add(row)
    _after_commit(session, row)
    return row


_KICKS = "jobs:kicks"
_HOOKED = "jobs:hooked"


def _after_commit(session, ref) -> None:
    """Wake this process's runner when the session's outermost transaction
    commits. A job written in a SAVEPOINT that was then rolled back is kicked
    too and found missing -- harmless, and simpler than tracking it."""
    sync = getattr(session, "sync_session", session)
    sync.info.setdefault(_KICKS, []).append(ref)
    if not sync.info.get(_HOOKED):
        event.listen(sync, "after_commit", _on_commit)
        event.listen(sync, "after_soft_rollback", _on_rollback)
        sync.info[_HOOKED] = True


def _ref_id(ref) -> Optional[int]:
    if isinstance(ref, int):
        return ref
    identity = sa.inspect(ref).identity
    return identity[0] if identity else None


def _on_commit(sync) -> None:
    if sync.in_nested_transaction():
        return
    refs = sync.info.pop(_KICKS, None)
    if refs:
        kick([i for i in (_ref_id(r) for r in refs) if i is not None])


def _on_rollback(sync, previous_transaction) -> None:
    if not previous_transaction.nested:
        sync.info.pop(_KICKS, None)


def kick(ids: Iterable[int] = ()) -> None:
    """New jobs are committed: wake this process's runner, if it has one.
    Under APP_JOBS_WORKER the API has none; the worker is woken through
    Redis when there is one, and finds them at its next poll when not."""
    ids = list(ids)
    runner = _local
    if runner is not None:
        runner.kick(ids)
    elif settings.JOBS_WORKER and settings.REDIS_URL and ids:
        _wake_worker()


WAKE_CHANNEL = "jobs:wake"
_wakes: set[asyncio.Task] = set()


def _wake_worker() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from app.core import redis

    task = loop.create_task(redis.publish(WAKE_CHANNEL, {}))
    _wakes.add(task)
    task.add_done_callback(_wakes.discard)


# ─── Sessions ────────────────────────────────────────────────────────────


@asynccontextmanager
async def _default_scope() -> AsyncIterator[Any]:
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


#: Where the runner, and handlers that have no scope of their own, get a
#: session. The test suite points it at the test's session.
session_scope: Callable[[], Any] = _default_scope


def scope():
    """A session for a handler (looked up at each call, so a test's stand-in is used)."""
    return session_scope()


# ─── Claiming and settling ───────────────────────────────────────────────


@dataclass
class Claimed:
    id: int
    name: str
    payload: dict
    attempts: int
    max_attempts: int


# Per name, per priority (highest first): the oldest due rows, a bounded
# range of the claim index that stops at the first row not yet due. The
# levels are walked in order and the walk stops once a name has its slots.
_CLAIM = """
WITH want AS (
    SELECT * FROM unnest(CAST(:names AS text[]), CAST(:slots AS int[]), CAST(:leases AS float8[])) AS w(name, slots, lease)
), picked AS (
    SELECT j.id, w.lease
    FROM want w
    CROSS JOIN LATERAL (
        SELECT x.id, x.priority, x.run_at
        FROM unnest(CAST(:levels AS int[])) AS l(priority)
        CROSS JOIN LATERAL (
            SELECT jobs.id, jobs.priority, jobs.run_at
            FROM jobs
            WHERE jobs.name = w.name AND jobs.priority = l.priority AND jobs.finished_at IS NULL AND NOT jobs.dead
              AND jobs.run_at <= statement_timestamp() {only}
            ORDER BY jobs.run_at, jobs.id
            LIMIT w.slots
            FOR UPDATE SKIP LOCKED
        ) x
        LIMIT w.slots
    ) j
    ORDER BY j.priority DESC, j.run_at, j.id
    LIMIT :total
)
UPDATE jobs SET
    attempts = jobs.attempts + 1,
    locked_by = :me,
    locked_until = statement_timestamp() + make_interval(secs => picked.lease),
    run_at = statement_timestamp() + make_interval(secs => picked.lease),
    updated_at = statement_timestamp()
FROM picked
WHERE jobs.id = picked.id
RETURNING jobs.id, jobs.name, jobs.payload, jobs.attempts, jobs.max_attempts
"""
CLAIM_SQL = sa.text(_CLAIM.format(only=""))
_CLAIM_IDS = sa.text(_CLAIM.format(only="AND jobs.id = ANY(CAST(:ids AS bigint[]))"))


async def claim(me: str, want: list[tuple[str, int, float]], total: int, *, ids: Optional[list[int]] = None) -> list[Claimed]:
    """Take up to `total` due jobs for runner `me`: for each (name, slots,
    lease seconds) in `want` at most `slots` of that name, highest priority
    and longest due first. `ids`: only among these (the test runner)."""
    if not want or total <= 0:
        return []
    params = {
        "names": [w[0] for w in want], "slots": [int(w[1]) for w in want], "leases": [float(w[2]) for w in want],
        "levels": list(PRIORITIES), "total": int(total), "me": me,
    }
    stmt = CLAIM_SQL
    if ids is not None:
        params["ids"] = list(ids)
        stmt = _CLAIM_IDS
    async with session_scope() as s:
        rows = (await s.execute(stmt, params)).all()
        await s.commit()
    return [Claimed(r.id, r.name, dict(r.payload or {}), r.attempts, r.max_attempts) for r in rows]


_FINISH = sa.text("""
UPDATE jobs SET finished_at = statement_timestamp(), locked_by = NULL, locked_until = NULL, updated_at = statement_timestamp()
WHERE id = :id AND attempts = :attempts AND finished_at IS NULL AND NOT dead
""")
_FINISH_DELETE = sa.text("DELETE FROM jobs WHERE id = :id AND attempts = :attempts AND finished_at IS NULL AND NOT dead")
_FAIL = sa.text("""
UPDATE jobs SET
    last_error = :error, locked_by = NULL, locked_until = NULL, updated_at = statement_timestamp(),
    dead = (attempts >= max_attempts),
    run_at = CASE WHEN attempts >= max_attempts THEN run_at ELSE statement_timestamp() + make_interval(secs => :delay) END
WHERE id = :id AND attempts = :attempts AND finished_at IS NULL AND NOT dead
RETURNING dead
""")
_RENEW = sa.text("""
UPDATE jobs SET locked_until = statement_timestamp() + make_interval(secs => w.lease),
    run_at = statement_timestamp() + make_interval(secs => w.lease)
FROM unnest(CAST(:ids AS bigint[]), CAST(:leases AS float8[])) AS w(id, lease)
WHERE jobs.id = w.id AND jobs.locked_by = :me AND jobs.finished_at IS NULL AND NOT jobs.dead
""")
_RELEASE = sa.text("""
UPDATE jobs SET attempts = GREATEST(attempts - 1, 0), run_at = statement_timestamp(), locked_by = NULL, locked_until = NULL,
    updated_at = statement_timestamp()
WHERE id = ANY(CAST(:ids AS bigint[])) AND locked_by = :me AND finished_at IS NULL AND NOT dead
""")


def backoff(attempts: int) -> float:
    """Seconds before the next try after `attempts` tries: doubling, with
    "equal jitter" (half fixed, half random) so jobs that failed together --
    a provider outage -- do not all come back in the same second."""
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** max(attempts - 1, 0))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


async def _finish(job: Claimed) -> None:
    stmt = _FINISH_DELETE if int(settings.JOBS_KEEP_DAYS) <= 0 else _FINISH
    try:
        async with session_scope() as s:
            await s.execute(stmt, {"id": job.id, "attempts": job.attempts})
            await s.commit()
    except Exception:  # noqa: BLE001 - the lease runs out and it runs again: at least once
        logger.warning("job %s #%s ran but could not be marked done; it will run again", job.name, job.id, exc_info=True)


async def _fail(job: Claimed, error: str) -> bool:
    """Record the failure; True when that was the last try."""
    try:
        async with session_scope() as s:
            dead = (await s.execute(_FAIL, {"id": job.id, "attempts": job.attempts, "error": error[:2000], "delay": backoff(job.attempts)})).scalar_one_or_none()
            await s.commit()
        return bool(dead)
    except Exception:  # noqa: BLE001
        logger.warning("job %s #%s failed and the failure could not be recorded", job.name, job.id, exc_info=True)
        return False


async def release(ids: list[int], me: str) -> None:
    """Hand claimed jobs back, the attempt not counted (a runner stopping)."""
    if not ids:
        return
    try:
        async with session_scope() as s:
            await s.execute(_RELEASE, {"ids": list(ids), "me": me})
            await s.commit()
    except Exception:  # noqa: BLE001 - their leases run out instead
        logger.warning("could not hand back %d job(s); they run again when their leases end", len(ids), exc_info=True)


def _describe(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {text}" if text != exc.__class__.__name__ else text


async def execute(job: Claimed) -> bool:
    """Run one claimed job and record what became of it. True on success."""
    h = _handlers.get(job.name)
    error: Optional[str] = None
    if h is None:
        error = f"no handler for {job.name} in this process"
    elif job.attempts > job.max_attempts:
        # Taken again each time its lease ran out: the process running it
        # stopped every time (an OOM on a huge payload, say). Not run again.
        error = f"the process running it stopped before it finished, {job.attempts - 1} times"
    else:
        try:
            await asyncio.wait_for(h.fn(dict(job.payload)), timeout=h.timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = f"did not finish within {h.timeout:g} s"
            logger.warning("job %s #%s timed out (try %d of %d)", job.name, job.id, job.attempts, job.max_attempts)
        except Exception as exc:  # noqa: BLE001
            error = _describe(exc)
            logger.warning("job %s #%s failed (try %d of %d)", job.name, job.id, job.attempts, job.max_attempts, exc_info=True)
    if error is None:
        await _finish(job)
        return True
    if await _fail(job, error):
        logger.error("job %s #%s is dead after %d tries: %s", job.name, job.id, job.attempts, error)
        await _alert_dead(job, error, h)
    return False


async def _alert_dead(job: Claimed, error: str, h: Optional[Handler]) -> None:
    if h is not None and not h.alert_dead:
        return
    try:
        from app.api.admin_alerts import notify

        async with session_scope() as s:
            await notify(
                s, "job_dead",
                title=f"A background job gave up: {job.name}",
                body=f"Job #{job.id} ({job.name}) failed {job.attempts} times and will not be tried again. Last error: {error}"[:2000],
                data={"job_id": job.id, "name": job.name, "attempts": job.attempts, "error": error[:200]},
                dedupe_on={"name": job.name},
            )
            await s.commit()
    except Exception:  # noqa: BLE001 - the dead row and the log line say it anyway
        logger.warning("could not alert the admins about dead job %s", job.id, exc_info=True)


async def run_due(*, ids: Optional[list[int]] = None, limit: Optional[int] = None, me: str = "inline") -> int:
    """Run due jobs one at a time in this task until none is left (or
    `limit` have run): what the test runner does, and a script's way to work
    the queue by hand. `ids`: only these. Returns how many ran."""
    ran = 0
    while limit is None or ran < limit:
        want = [(h.name, 1, lease_for(h)) for h in _handlers.values()]
        got = await claim(me, want, 1, ids=ids)
        if not got:
            break
        await execute(got[0])
        ran += 1
    return ran


# ─── Runners ─────────────────────────────────────────────────────────────


class Runner:
    """Claims and runs jobs in the background of this process: an API
    process's own (APP_JOBS_WORKER unset) or the worker's."""

    def __init__(self, *, kind: str, concurrency: Optional[int] = None, poll: Optional[float] = None, sweeps: bool = False, maintenance: bool = True):
        self.kind = kind
        self.id = f"{kind}:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"[:120]
        self.concurrency = max(1, int(concurrency or settings.JOBS_CONCURRENCY))
        self.poll = float(poll or settings.JOBS_POLL_SECONDS)
        self.sweeps = sweeps
        self.maintenance = maintenance
        self.started_at = _now()
        self.handled = 0
        self.failed = 0
        self._wake: Optional[asyncio.Event] = None
        self._halt: Optional[asyncio.Event] = None
        self._main_task: Optional[asyncio.Task] = None
        self._house_task: Optional[asyncio.Task] = None
        self._running: dict[asyncio.Task, Claimed] = {}
        self._stopping = False

    # Started and stopped by doth.py's lifespan, or app.worker.
    def start(self) -> "Runner":
        loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._halt = asyncio.Event()
        self._main_task = loop.create_task(self._main(), name=f"jobs:{self.kind}")
        self._house_task = loop.create_task(self._housekeeping(), name=f"jobs:{self.kind}:housekeeping")
        return self

    @property
    def in_hand(self) -> int:
        return len(self._running)

    def kick(self, ids: Iterable[int] = ()) -> None:
        if self._wake is not None and not self._stopping:
            self._wake.set()

    def _want(self) -> tuple[list[tuple[str, int, float]], int]:
        free = self.concurrency - len(self._running)
        if free <= 0:
            return [], 0
        busy = Counter(c.name for c in self._running.values())
        want = []
        for h in _handlers.values():
            room = min(free, limit_for(h, self.concurrency) - busy[h.name])
            if room > 0:
                want.append((h.name, room, lease_for(h)))
        return want, free

    async def _main(self) -> None:
        failures = 0
        while not self._stopping:
            # Cleared before the claim: a commit that lands during it sets it
            # again, and the wait below returns at once.
            self._wake.clear()
            try:
                want, free = self._want()
                got = await claim(self.id, want, free) if want else []
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the database away: try again, a little later each time
                failures += 1
                logger.warning("the job runner could not claim jobs (%d in a row)", failures, exc_info=failures == 1)
                await asyncio.sleep(min(30.0, self.poll * 2 ** failures))
                continue
            for job in got:
                self._start(job)
            if got:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll)
            except asyncio.TimeoutError:
                pass

    def _start(self, job: Claimed) -> None:
        task = asyncio.get_running_loop().create_task(self._run(job), name=f"job:{job.name}:{job.id}")
        self._running[task] = job
        task.add_done_callback(self._done)

    async def _run(self, job: Claimed) -> None:
        ok = await execute(job)
        if ok:
            self.handled += 1
        else:
            self.failed += 1

    def _done(self, task: asyncio.Task) -> None:
        self._running.pop(task, None)
        if self._wake is not None:
            self._wake.set()  # a slot is free

    async def renew(self) -> None:
        """Push the leases of the jobs in hand ahead: they are alive."""
        held = list(self._running.values())
        if not held:
            return
        leases = [lease_for(_handlers[j.name]) if j.name in _handlers else LEASE for j in held]
        async with session_scope() as s:
            await s.execute(_RENEW, {"ids": [j.id for j in held], "leases": leases, "me": self.id})
            await s.commit()

    async def _housekeeping(self) -> None:
        next_maintenance = time.monotonic() + (5.0 if self.maintenance else float("inf"))
        while not self._stopping:
            try:
                await self.renew()
                await self.beat()
                if time.monotonic() >= next_maintenance:
                    next_maintenance = time.monotonic() + MAINTENANCE_SECONDS
                    await maintain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("job runner housekeeping failed", exc_info=True)
            # Asleep until the next beat, or until told to stop.
            try:
                await asyncio.wait_for(self._halt.wait(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def beat(self) -> None:
        now = _now()
        table = JobWorkers.__table__
        values = dict(
            id=self.id, kind=self.kind, host=socket.gethostname()[:200], pid=os.getpid(), sweeps=self.sweeps,
            started_at=self.started_at, beat_at=now, running=len(self._running), handled=self.handled, failed=self.failed,
        )
        stmt = pg_insert(table).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=[table.c.id], set_={k: stmt.excluded[k] for k in ("beat_at", "running", "handled", "failed", "sweeps")})
        async with session_scope() as s:
            await s.execute(stmt)
            await s.commit()

    async def settle(self, timeout: Optional[float] = None) -> bool:
        """Wait for the jobs in hand (not for ones still to be claimed)."""
        tasks = list(self._running)
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def stop(self, timeout: float) -> bool:
        """Stop claiming; give the jobs in hand `timeout` seconds; hand back
        the rest. Returns whether every job in hand finished."""
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
            self._halt.set()
        # The loops finish the statement they are in (a claim, a beat) and
        # stop; cancelling them mid-statement would drop the connection.
        loops = [t for t in (self._main_task, self._house_task) if t is not None and not t.done()]
        if loops:
            _, slow = await asyncio.wait(loops, timeout=min(2.0, max(0.1, timeout)))
            for t in slow:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
        tasks = list(self._running)
        pending: set = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
        cut = [self._running[t] for t in pending if t in self._running]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1.0)
            logger.warning("%d job(s) cut off at shutdown are handed back to the queue", len(cut))
            await release([j.id for j in cut], self.id)
        try:
            async with session_scope() as s:
                await s.execute(sa.delete(JobWorkers).where(JobWorkers.id == self.id))
                await s.commit()
        except Exception:  # noqa: BLE001 - the maintenance pass removes it later
            pass
        return not pending


class InlineRunner:
    """Nothing in the background: the jobs this process was told about run
    when `settle` is awaited, one at a time, on `session_scope`'s session.
    The test suite's runner (tests/conftest.py)."""

    kind = "inline"

    def __init__(self):
        self.id = "inline"
        self.ids: list[int] = []
        self.in_hand = 0

    def kick(self, ids: Iterable[int] = ()) -> None:
        self.ids.extend(ids)

    async def settle(self, timeout: Optional[float] = None) -> bool:
        rounds = 0
        while self.ids and rounds < 100:
            ids, self.ids = self.ids, []
            await run_due(ids=ids, me=self.id)
            rounds += 1
        return True

    async def stop(self, timeout: float) -> bool:
        return True


_local: Optional[Any] = None


def local():
    """This process's runner, if it has one."""
    return _local


def use(runner) -> Any:
    """Make `runner` this process's (None: none). Returns the one before."""
    global _local
    before, _local = _local, runner
    return before


async def settle(timeout: Optional[float] = None) -> bool:
    """Wait for the jobs this process has in hand -- under the test runner,
    run the ones its commits set off. True when nothing was left."""
    runner = _local
    if runner is None:
        return True
    return await runner.settle(timeout)


# ─── Maintenance and figures ─────────────────────────────────────────────


async def _delete_batches(s, where: str, params: dict) -> int:
    total = 0
    for _ in range(20):
        n = (await s.execute(sa.text(f"DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE {where} LIMIT {CLEANUP_BATCH})"), params)).rowcount
        await s.commit()
        total += n or 0
        if (n or 0) < CLEANUP_BATCH:
            break
    return total


async def maintain() -> dict:
    """Delete finished and dead rows past their keeping, forget runners long
    gone, and adopt the rows the old after-commit outbox still holds. One
    runner at a time (an advisory lock); each delete is a batch of its own."""
    from app.db.locks import singleton

    out = {"finished": 0, "dead": 0, "workers": 0, "adopted": 0}
    async with session_scope() as s:
        async with singleton(s, MAINTENANCE_LOCK) as mine:
            if not mine:
                return out
            now = _now()
            keep = max(0, int(settings.JOBS_KEEP_DAYS))
            out["finished"] = await _delete_batches(s, "finished_at IS NOT NULL AND finished_at < :cutoff", {"cutoff": now - timedelta(days=keep)})
            dead_keep = max(1, int(settings.JOBS_DEAD_KEEP_DAYS))
            out["dead"] = await _delete_batches(s, "dead AND updated_at < :cutoff", {"cutoff": now - timedelta(days=dead_keep)})
            out["workers"] = (await s.execute(sa.delete(JobWorkers).where(JobWorkers.beat_at < now - timedelta(days=1)))).rowcount or 0
            await s.commit()
            from app.core import after_commit

            out["adopted"] = await after_commit.adopt_legacy(s)
    if any(out.values()):
        logger.info("jobs maintenance: %s", out)
    return out


async def stats(session) -> dict:
    """The queue at a glance, per name, and the runners heard from lately."""
    rows = (await session.execute(sa.text(f"""
        SELECT name,
               count(*) FILTER (WHERE locked_until IS NULL AND run_at <= statement_timestamp()) AS due,
               count(*) FILTER (WHERE locked_until IS NULL AND run_at > statement_timestamp()) AS scheduled,
               count(*) FILTER (WHERE locked_until IS NOT NULL AND locked_until > statement_timestamp()) AS running,
               count(*) FILTER (WHERE locked_until IS NOT NULL AND locked_until <= statement_timestamp()) AS abandoned,
               extract(epoch FROM statement_timestamp() - min(run_at) FILTER (WHERE run_at <= statement_timestamp())) AS oldest_due_seconds
        FROM jobs WHERE {UNFINISHED} GROUP BY name ORDER BY name
    """))).all()
    dead = dict((await session.execute(sa.text("SELECT name, count(*) FROM jobs WHERE dead GROUP BY name"))).all())
    names = sorted({r.name for r in rows} | set(dead))
    by = {r.name: r for r in rows}
    queues = []
    for name in names:
        r = by.get(name)
        queues.append({
            "name": name, "due": int(r.due) if r else 0, "scheduled": int(r.scheduled) if r else 0,
            "running": int(r.running) if r else 0, "abandoned": int(r.abandoned) if r else 0,
            "oldest_due_seconds": round(float(r.oldest_due_seconds), 1) if r and r.oldest_due_seconds is not None else None,
            "dead": int(dead.get(name, 0)),
        })
    return {"queues": queues, "workers": await alive(session)}


async def alive(executor) -> list[dict]:
    """The runners whose heartbeat is recent. `executor`: a session or a connection."""
    t = JobWorkers.__table__
    rows = (await executor.execute(
        sa.select(t).where(t.c.beat_at >= _now() - ALIVE_WITHIN).order_by(t.c.kind, t.c.started_at)
    )).mappings().all()
    return [dict(r) for r in rows]


def reset_for_tests() -> None:
    use(None)
