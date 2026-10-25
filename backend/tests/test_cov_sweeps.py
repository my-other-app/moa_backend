"""Every background loop: one pass does its work, holding its advisory lock
while it works, and does nothing while another process holds that lock.

Each loop doth.start_loops starts (and the job runner's maintenance pass,
and the claims of the runs that are scheduled in scheduled_runs) is run here
for exactly one pass: its waits return at once and the wait that ends the
pass ends the test's run of it. Its sessions are the test's own, so its locks
are taken on the test's connection, and "another process" is another
connection of the test engine. The work each pass calls is the real one,
watched: the sweeps' own tests (the "does its job" column of each module's
tests) cover what that work does to rows; here, that the loop calls it, once,
under its lock, and lets the lock go.

The loops whose run is claimed under a transaction lock (payments, the
ledger's daily run, discover's cities) hold the lock only while they claim
the run: what keeps a second worker off is the claim's row in
scheduled_runs, which their own tests cover. Here, that a claim another
worker is making keeps this one from claiming.
"""
import asyncio
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest
from sqlalchemy import delete, func, select, text, update

from app.api.events.models import Events
from app.api.models import ScheduledRuns
from app.config import settings
from app.core import jobs
from app.db import lock_keys

#: The wait that ends a pass of a loop that takes its interval as an argument.
TICK = 4321


class _Pass(BaseException):
    """The loop's first pass is over: out of the loop (no `except Exception` catches it)."""


class _Runaway(BaseException):
    """The loop never reached the wait that ends a pass (a failing pass can
    back off to another wait): a failed test, not a hung one."""


class _Clock:
    """The loop module's `asyncio`: every wait returns at once, except the
    one that ends the pass, which ends the loop."""

    def __init__(self, stop_at: float):
        self.stop_at = stop_at
        self.waits = 0

    async def sleep(self, seconds, *args, **kwargs):
        if seconds == self.stop_at:
            raise _Pass
        self.waits += 1
        if self.waits > 500:
            raise _Runaway(f"no wait of {self.stop_at}s after {self.waits} waits")
        return None

    def __getattr__(self, name):
        return getattr(asyncio, name)


async def held_elsewhere(engine, key) -> bool:
    """Whether some other connection holds advisory lock `key` (one bigint or two ints)."""
    keys = tuple(key) if isinstance(key, tuple) else (key,)
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = bool(await conn.scalar(select(func.pg_try_advisory_lock(*keys))))
        if got:
            await conn.scalar(select(func.pg_advisory_unlock(*keys)))
        return not got


@asynccontextmanager
async def taken_elsewhere(engine, key):
    """Another process holding session lock `key` for the block."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.scalar(select(func.pg_advisory_lock(key)))
        try:
            yield
        finally:
            await conn.scalar(select(func.pg_advisory_unlock(key)))


@asynccontextmanager
async def claiming_elsewhere(engine, key):
    """Another worker in the middle of a claim: transaction lock `key` held for the block."""
    async with engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(select(func.pg_advisory_xact_lock(key)))
        try:
            yield
        finally:
            await tx.rollback()


def on_the_test_session(monkeypatch, session) -> None:
    """Every session a loop opens is the test's."""
    from app.api.deliveries import service as deliveries
    from app.api.discover import geo
    from app.db import core

    @asynccontextmanager
    async def scope():
        yield session

    class _Local:  # `async with AsyncSessionLocal() as session`, without closing the test's
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(deliveries, "session_scope", scope)
    monkeypatch.setattr(geo, "session_scope", scope)
    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: _Local())


async def one_pass(monkeypatch, session, module: str, loop: str, *, stop_at: float = TICK, **kwargs) -> None:
    on_the_test_session(monkeypatch, session)
    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, "asyncio", _Clock(stop_at))
    with pytest.raises(_Pass):
        await getattr(mod, loop)(**kwargs)


def watch(monkeypatch, engine, target: tuple[str, str], key, seen: list, stub: Any = None) -> None:
    """Record each call of `target` and whether `key` was held (by the
    caller: no other connection can take it) while it ran. Runs the real
    function unless a stub answer is given."""
    module, name = target
    mod = importlib.import_module(module)
    real = getattr(mod, name)

    async def watched(*args, **kwargs):
        seen.append((name, await held_elsewhere(engine, key)))
        if stub is not None:
            return stub
        return await real(*args, **kwargs)

    monkeypatch.setattr(mod, name, watched)


# ─── The loops doth.start_loops starts, with a session lock each ─────────


def _doc_cache_on(monkeypatch):
    monkeypatch.setattr(settings, "DOC_CACHE_DAYS", 90)


def _account_sweep_tick(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_SWEEP_SECONDS", TICK)


def _search_tick(monkeypatch):
    from app.api.search import sync

    monkeypatch.setattr(sync, "DRAIN_EVERY", 43.21)
    monkeypatch.setattr(sync, "IDLE_EVERY", 43.21)


_DOC_CACHE_DONE = {
    "deleted": 0, "deleted_bytes": 0, "documents": 0, "newer": 0, "unknown": 0, "errors": 0, "capped": False,
    "listed": 0, "unknown_percent": 0, "unknown_samples": [], "error_samples": [],
}


@dataclass
class Loop:
    id: str
    module: str
    loop: str
    key: int
    work: list[tuple[str, str]]
    kwargs: dict = field(default_factory=lambda: {"interval": TICK})
    stop_at: float = TICK
    setup: Optional[Callable] = None
    #: The scheduled_runs rows a pass reads to see whether it is due (cleared first).
    runs: tuple = ()
    stub: Any = None


LOOPS = [
    Loop("timed emails", "app.api.emails.service", "sweep_loop", lock_keys.EMAIL_SWEEP,
         [("app.api.emails.service", "sweep"), ("app.api.emails.service", "whatsapp_reminders"), ("app.api.emails.service", "resume")]),
    Loop("closeout", "app.api.closeout.service", "sweep_loop", lock_keys.CLOSEOUT_SWEEP, [("app.api.closeout.service", "sweep")]),
    Loop("feedback surveys", "app.api.live.service", "sweep_loop", lock_keys.LIVE_SWEEP, [("app.api.live.service", "sweep")]),
    Loop("billing", "app.api.subscriptions.sweep", "sweep_loop", lock_keys.SUBSCRIPTIONS_SWEEP, [("app.api.subscriptions.sweep", "sweep")]),
    Loop("club domains", "app.api.club_domains.sweep", "sweep_loop", lock_keys.CLUB_DOMAINS_SWEEP, [("app.api.club_domains.sweep", "sweep")]),
    Loop("club sites", "app.api.club_domains.sweep", "sweep_loop", lock_keys.SITES_SWEEP, [("app.api.sites.sweep", "refresh")]),
    Loop("store holds", "app.api.store.sweep", "sweep_loop", lock_keys.STORE_SWEEP, [("app.api.store.service", "expire_holds")]),
    Loop("accounts", "app.api.auth.retention", "sweep_loop", lock_keys.AUTH_RETENTION, [("app.api.auth.retention", "purge")],
         kwargs={}, setup=_account_sweep_tick),
    Loop("search outbox", "app.api.search.sync", "loop", lock_keys.SEARCH_SYNC, [("app.api.search.sync", "drain")],
         kwargs={}, stop_at=43.21, setup=_search_tick),
    Loop("kept documents", "app.api.events.doc_cache_sweep", "sweep_loop", lock_keys.DOC_CACHE_SWEEP, [("app.api.events.doc_cache_sweep", "sweep")],
         kwargs={}, setup=_doc_cache_on, runs=("doc_cache_sweep",), stub=_DOC_CACHE_DONE),
    Loop("club whatsapp", "app.api.club_whatsapp.sweep", "sweep_loop", lock_keys.CLUB_WHATSAPP_SWEEP, [("app.api.club_whatsapp.sweep", "sweep")]),
    Loop("club mailbox", "app.api.club_mailbox.sweep", "sweep_loop", lock_keys.CLUB_MAILBOX_SWEEP, [("app.api.club_mailbox.sweep", "sweep")]),
    Loop("waiting lists", "app.api.ticketing.sweep", "sweep_loop", lock_keys.WAITLIST_SWEEP, [("app.api.ticketing.waitlist", "expire")]),
    Loop("popularity", "app.api.discover.counters", "check_loop", lock_keys.DISCOVER_POPULARITY, [("app.api.discover.popularity", "refresh")]),
    Loop("counters", "app.api.discover.counters", "check_loop", lock_keys.DISCOVER_COUNTERS,
         [("app.api.discover.counters", "reconcile"), ("app.api.discover.counters", "reconcile_more")], runs=("counters", "counters-posts")),
]


def _prepare(monkeypatch, spec: Loop):
    if spec.setup:
        spec.setup(monkeypatch)
    if spec.id == "kept documents":
        from app.api.events import doc_cache_sweep

        monkeypatch.setattr(doc_cache_sweep, "LOOK_EVERY_SECONDS", TICK)


async def _clear_runs(session, spec: Loop):
    if spec.id == "kept documents":
        from app.api.events import doc_cache_sweep

        names = (doc_cache_sweep.JOB,)
    else:
        names = spec.runs
    if names:
        await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name.in_(names)))
        await session.commit()


@pytest.mark.parametrize("spec", LOOPS, ids=[s.id for s in LOOPS])
async def test_a_pass_does_its_work_once_under_its_lock_and_lets_it_go(spec, session, _engine, monkeypatch):
    _prepare(monkeypatch, spec)
    await _clear_runs(session, spec)
    seen: list = []
    for target in spec.work:
        watch(monkeypatch, _engine, target, spec.key, seen, stub=spec.stub)

    await one_pass(monkeypatch, session, spec.module, spec.loop, stop_at=spec.stop_at, **spec.kwargs)

    assert sorted(name for name, _ in seen) == sorted(name for _, name in spec.work), seen
    assert all(held for _, held in seen), f"{spec.id}: worked without holding lock {spec.key}: {seen}"
    assert not await held_elsewhere(_engine, spec.key), f"{spec.id}: lock {spec.key} still held after the pass"


@pytest.mark.parametrize("spec", LOOPS, ids=[s.id for s in LOOPS])
async def test_a_pass_while_another_process_holds_the_lock_does_none_of_the_work(spec, session, _engine, monkeypatch):
    _prepare(monkeypatch, spec)
    await _clear_runs(session, spec)
    seen: list = []
    for target in spec.work:
        watch(monkeypatch, _engine, target, spec.key, seen, stub=spec.stub)

    async with taken_elsewhere(_engine, spec.key):
        await one_pass(monkeypatch, session, spec.module, spec.loop, stop_at=spec.stop_at, **spec.kwargs)

    assert seen == [], f"{spec.id}: ran while another process held lock {spec.key}: {seen}"


async def test_a_loop_whose_work_fails_lets_the_lock_go_and_carries_on(session, _engine, monkeypatch, caplog):
    """A pass that raises is logged, the lock is released, and the loop goes
    on to its next wait (it is not left dead with the lock held)."""
    from app.api.closeout import service as closeout

    async def broken(*_a, **_k):
        raise RuntimeError("the database said no")

    monkeypatch.setattr(closeout, "sweep", broken)
    await one_pass(monkeypatch, session, "app.api.closeout.service", "sweep_loop", interval=TICK)
    assert "closeout sweep failed" in caplog.text
    assert not await held_elsewhere(_engine, lock_keys.CLOSEOUT_SWEEP)


# ─── The entry points that take the lock themselves ──────────────────────


async def test_each_sweeps_own_entry_skips_while_another_process_holds_its_lock(session, _engine, monkeypatch):
    """What each loop calls, called directly: None (or nothing done) under
    another's lock; its answer once the lock is free."""
    from app.api.club_domains import sweep as domains
    from app.api.club_whatsapp import sweep as whatsapp
    from app.api.discover import counters, popularity
    from app.api.search import sync
    from app.api.sites import sweep as sites
    from app.api.store import sweep as store

    await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name.in_((counters.JOB, counters.JOB_MORE))))
    await session.commit()
    entries = [
        (store.SWEEP_LOCK, lambda: store.run_sweep(session), None),
        (sites.SITES_LOCK, lambda: sites.run_refresh(session), None),
        (domains.SWEEP_LOCK, lambda: domains.run_sweep(session), None),
        (whatsapp.SWEEP_LOCK, lambda: whatsapp.run_sweep(session), None),
        (popularity.LOCK, lambda: popularity.refresh_if_free(session), None),
        (counters.COUNTERS_LOCK, lambda: counters.run_more_if_due(session), None),
        (sync.SEARCH_LOCK, lambda: sync.tick(session), {}),
    ]
    for key, run, skipped in entries:
        async with taken_elsewhere(_engine, key):
            assert await run() == skipped, key
        assert await run() != skipped, key
        assert not await held_elsewhere(_engine, key), key


async def test_the_posts_counter_check_runs_every_six_hours_on_one_process(session, _engine):
    from app.api.discover import counters

    await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name == counters.JOB_MORE))
    await session.commit()
    now = datetime.now(timezone.utc)
    async with taken_elsewhere(_engine, counters.COUNTERS_LOCK):
        assert await counters.run_more_if_due(session, now=now) is None
    assert await session.get(ScheduledRuns, counters.JOB_MORE) is None  # nothing claimed under another's lock
    assert await counters.run_more_if_due(session, now=now) == {"posts": 0, "poll_options": 0, "questions": 0}
    row = await session.scalar(select(ScheduledRuns).where(ScheduledRuns.name == counters.JOB_MORE).execution_options(populate_existing=True))
    assert row.started_at == now and row.finished_at is not None and row.result == {"posts": 0, "poll_options": 0, "questions": 0}
    assert await counters.run_more_if_due(session, now=now + timedelta(hours=5)) is None
    assert await counters.run_more_if_due(session, now=now + timedelta(hours=6, seconds=1)) is not None


async def test_the_job_queues_maintenance_runs_on_one_runner_at_a_time(session, _engine, monkeypatch):
    """jobs.maintain: another runner holding JOBS_MAINTENANCE means this one
    deletes nothing; free, it deletes what is past keeping."""
    monkeypatch.setattr(settings, "JOBS_KEEP_DAYS", 3)
    now = datetime.now(timezone.utc)
    old = jobs.Jobs(name="test.cov.maintain", payload={}, finished_at=now - timedelta(days=4))
    session.add(old)
    await session.commit()

    async with taken_elsewhere(_engine, jobs.MAINTENANCE_LOCK):
        assert await jobs.maintain() == {"finished": 0, "dead": 0, "workers": 0, "adopted": 0}
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.id == old.id)) == 1

    seen: list = []
    real = jobs._delete_batches

    async def watched(s, where, params):
        seen.append(await held_elsewhere(_engine, jobs.MAINTENANCE_LOCK))
        return await real(s, where, params)

    monkeypatch.setattr(jobs, "_delete_batches", watched)
    out = await jobs.maintain()
    assert out["finished"] >= 1 and seen and all(seen)
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.id == old.id)) == 0
    assert not await held_elsewhere(_engine, jobs.MAINTENANCE_LOCK)


# ─── The runs claimed under a transaction lock ────────────────────────────


async def test_the_payments_sweep_is_not_claimed_while_another_worker_claims_it(session, _engine, monkeypatch):
    from app.api.payments import sweep

    ran: list = []

    async def work(s, now=None, *, deadline=None):
        ran.append(now)
        return {"refunds": {"due": 0}, "repair": {"found": 0}, "reconcile": {"applied": 0}}

    monkeypatch.setattr(sweep, "sweep", work)
    await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name == sweep.JOB))
    await session.commit()
    now = datetime.now(timezone.utc)
    async with claiming_elsewhere(_engine, sweep.SWEEP_LOCK):
        assert await sweep.run_if_due(session, now) is None
    assert ran == [] and await session.get(ScheduledRuns, sweep.JOB) is None
    assert await sweep.run_if_due(session, now) is not None and ran == [now]


async def test_one_pass_of_the_payments_loop_runs_the_sweep_when_due(session, monkeypatch):
    from app.api.payments import sweep

    ran: list = []

    async def work(s, now=None, *, deadline=None):
        ran.append(now)
        return {"refunds": {"due": 0}, "repair": {"found": 0}, "reconcile": {"applied": 0}}

    monkeypatch.setattr(sweep, "sweep", work)
    await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name == sweep.JOB))
    await session.commit()
    await one_pass(monkeypatch, session, "app.api.payments.sweep", "sweep_loop", interval=TICK)
    assert len(ran) == 1
    # The next pass, minutes later, is not due.
    await one_pass(monkeypatch, session, "app.api.payments.sweep", "sweep_loop", interval=TICK)
    assert len(ran) == 1


async def test_the_ledgers_daily_run_is_not_claimed_while_another_worker_claims_it(session, _engine, monkeypatch):
    from app.api.ledger import book
    from app.api.ledger import jobs as ledger_jobs

    ran: list = []

    async def daily(s, now=None, *, previous=None, razorpay=True):
        ran.append(now)
        return {"retried": {}}

    monkeypatch.setattr(ledger_jobs, "run_daily", daily)
    await session.execute(delete(ScheduledRuns).where(ScheduledRuns.name == ledger_jobs.JOB))
    await session.commit()
    today = datetime.now(book.IST).replace(hour=0, minute=0, second=0, microsecond=0)
    at = (today + timedelta(hours=12)).astimezone(timezone.utc)  # well past the daily hour
    async with claiming_elsewhere(_engine, ledger_jobs.LOCK):
        assert await ledger_jobs.run_if_due(session, now=at) is None
    assert ran == [] and await session.get(ScheduledRuns, ledger_jobs.JOB) is None
    assert await ledger_jobs.run_if_due(session, now=at) == {"retried": {}} and ran == [at]
    assert await ledger_jobs.run_if_due(session, now=at + timedelta(hours=1)) is None  # once a day


async def test_one_pass_of_the_ledger_loop_runs_the_daily_job_when_due(session, monkeypatch):
    from app.api.ledger import jobs as ledger_jobs

    ran: list = []

    async def due_now(s, now=None):
        ran.append(now)
        return {"retried": {}}

    monkeypatch.setattr(ledger_jobs, "run_if_due", due_now)
    await one_pass(monkeypatch, session, "app.api.ledger.jobs", "daily_loop", interval=TICK)
    assert len(ran) == 1


async def test_discovers_cities_are_not_counted_while_another_process_counts_them(session, _engine, monkeypatch):
    from app.api.discover import stats

    on_the_test_session(monkeypatch, session)
    await session.execute(delete(stats.DiscoverStats).where(stats.DiscoverStats.name == stats.CITIES))
    await session.commit()
    async with claiming_elsewhere(_engine, stats._LOCK):
        assert await stats.refresh() is None
    assert await session.scalar(select(stats.DiscoverStats.value).where(stats.DiscoverStats.name == stats.CITIES)) is None
    assert await stats.refresh() == 0
    assert await session.scalar(select(stats.DiscoverStats.value).where(stats.DiscoverStats.name == stats.CITIES)) == 0


# ─── Popularity: the refresh the counter loop runs every few minutes ─────


async def test_the_counter_loop_writes_popularity_every_pass_and_skips_while_another_process_does(
    session, _engine, monkeypatch, make_club, make_event,
):
    from app.api.discover import popularity

    owner, club = await make_club(name="Popular Club", email="popular-loop@example.com")
    soon = await make_event(club=club, created_by=owner, name="Soon And Popular", event_datetime=datetime.now(timezone.utc) + timedelta(days=3))
    over = await make_event(club=club, created_by=owner, name="Over Already", event_datetime=datetime.now(timezone.utc) - timedelta(days=3))
    await session.execute(update(Events).where(Events.id.in_([soon.id, over.id])).values(page_views=60, popularity=5.0))
    await session.commit()

    async def stored(ev):
        return await session.scalar(select(Events.popularity).where(Events.id == ev.id).execution_options(populate_existing=True))

    # Another process at it: this pass leaves the numbers alone.
    async with taken_elsewhere(_engine, popularity.LOCK):
        await one_pass(monkeypatch, session, "app.api.discover.counters", "check_loop", interval=TICK)
    assert await stored(soon) == 5.0 and await stored(over) == 5.0

    # Free: the attention, decayed by age; what is over drops below everything on.
    await one_pass(monkeypatch, session, "app.api.discover.counters", "check_loop", interval=TICK)
    created = await session.scalar(select(Events.created_at).where(Events.id == soon.id))
    assert await stored(soon) == pytest.approx(popularity.value(0, 60, created))
    assert await stored(over) == 0

    # More attention: the next pass places it again (and writes only what changed).
    await session.execute(update(Events).where(Events.id == soon.id).values(page_views=600))
    await session.commit()
    await one_pass(monkeypatch, session, "app.api.discover.counters", "check_loop", interval=TICK)
    assert await stored(soon) == pytest.approx(popularity.value(0, 600, created))
    assert await popularity.refresh(session) == 0


async def test_the_popularity_refresh_is_on_the_loops_few_minute_beat():
    from app.api.discover import counters, popularity

    assert popularity.EVERY_SECONDS <= 300
    # check_loop's default wait is the shorter of the counter look and the popularity beat.
    import inspect

    assert "min(LOOK_EVERY_SECONDS, popularity.EVERY_SECONDS)" in inspect.getsource(counters.check_loop)


async def test_a_raw_lock_check_sees_the_test_connection(session, _engine):
    """The harness's own premise: a session lock the test's connection holds
    is seen from another connection, and released is free again."""
    await session.execute(text("SELECT pg_advisory_lock(424243)"))
    try:
        assert await held_elsewhere(_engine, 424243)
    finally:
        await session.execute(text("SELECT pg_advisory_unlock(424243)"))
    assert not await held_elsewhere(_engine, 424243)


def test_every_loop_doth_starts_is_here():
    """A loop added to doth.start_loops without a row here fails, so none is
    left untested. (doth.py is read, not imported. Last in the file: a test
    without the event loop that runs before the async ones leaves them on a
    loop other than the test engine's when this file runs alone.)"""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "doth.py").read_text())
    [fn] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_loops"]
    started = {(n.module, a.name) for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) for a in n.names}
    covered = {(spec.module, spec.loop) for spec in LOOPS}
    # Claimed under a transaction lock: their own tests below.
    covered |= {("app.api.payments.sweep", "sweep_loop"), ("app.api.ledger.jobs", "daily_loop")}
    assert started == covered, started ^ covered
