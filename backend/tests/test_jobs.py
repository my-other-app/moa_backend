"""The job queue (app/core/jobs.py): written with the change, run once, retried,
given up on, and never run twice at the same time.

Most tests run on committed rows (`queue`), through sessions of their own,
as the API's runners and the worker do -- contention and leases are about
separate transactions -- and remove every job they made. A few run on the
test's rolled-back session, as the rest of the suite's jobs do.
"""
import asyncio
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.admin_alerts.models import AdminNotifications
from app.config import settings
from app.core import jobs
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria

#: What the test handlers saw: job name -> list of payloads, in order.
SEEN: dict[str, list] = {}
STATE = {"fail": 0, "delay": 0.0, "in_flight": 0, "most": 0}
#: Every job's name as it started, all names together, in order.
ORDER: list[str] = []
#: Per name: how many run right now, and the most that ever ran at once.
LIVE: Counter = Counter()
PEAK: Counter = Counter()


def _record(name):
    async def fn(payload: dict) -> None:
        ORDER.append(name)
        LIVE[name] += 1
        PEAK[name] = max(PEAK[name], LIVE[name])
        STATE["in_flight"] += 1
        STATE["most"] = max(STATE["most"], STATE["in_flight"])
        try:
            if STATE["delay"]:
                await asyncio.sleep(STATE["delay"])
            if STATE["fail"]:
                STATE["fail"] -= 1
                raise RuntimeError("the provider said no")
            SEEN.setdefault(name, []).append(payload)
        finally:
            STATE["in_flight"] -= 1
            LIVE[name] -= 1

    fn.__qualname__ = f"record_{name.replace('.', '_')}"
    return fn


jobs.handler("test.record", max_attempts=3, timeout=5.0)(_record("test.record"))
jobs.handler("test.single", max_attempts=3, timeout=5.0, concurrency=1)(_record("test.single"))
jobs.handler("test.slow", max_attempts=3, timeout=0.2)(_record("test.slow"))
# As the real ones are: a mail someone waits for, and a close-out nobody watches (one at a time).
jobs.handler("test.urgent", max_attempts=3, timeout=5.0, priority=jobs.HIGH)(_record("test.urgent"))
jobs.handler("test.later", max_attempts=3, timeout=5.0, concurrency=1, priority=jobs.LOW)(_record("test.later"))


@pytest.fixture(autouse=True)
def _fresh():
    SEEN.clear()
    ORDER.clear()
    LIVE.clear()
    PEAK.clear()
    STATE.update(fail=0, delay=0.0, in_flight=0, most=0)
    yield
    SEEN.clear()


@pytest.fixture
async def queue(_engine, monkeypatch):
    """Committed rows, sessions of their own for the runners; every job and
    heartbeat this test made removed after it."""
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)

    @asynccontextmanager
    async def scope():
        async with make() as s:
            add_loader_criteria(s)
            yield s

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(settings, "JOBS_POLL_SECONDS", 0.05)
    started: list = []
    # Committed jobs another test left behind are not this test's to run or count.
    async with make() as s:
        await s.execute(delete(jobs.Jobs))
        await s.execute(delete(jobs.JobWorkers))
        await s.commit()

    def runner(kind="worker", **kw):
        r = jobs.Runner(kind=kind, maintenance=False, **kw).start()
        started.append(r)
        return r

    try:
        yield SimpleNamespace(make=make, scope=scope, runner=runner)
    finally:
        for r in started:
            await r.stop(timeout=1.0)
        async with make() as s:
            await s.execute(delete(jobs.Jobs))
            await s.execute(delete(jobs.JobWorkers))
            await s.execute(delete(AdminNotifications).where(AdminNotifications.kind == "job_dead"))
            await s.commit()


async def _rows(make, **where):
    async with make() as s:
        q = select(jobs.Jobs).order_by(jobs.Jobs.id)
        for k, v in where.items():
            q = q.where(getattr(jobs.Jobs, k) == v)
        return list((await s.scalars(q)).all())


async def _until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await check():
            return True
        await asyncio.sleep(0.02)
    return False


# ─── Enqueueing ──────────────────────────────────────────────────────────


async def test_a_job_is_written_with_the_change_and_goes_with_its_rollback(session):
    await jobs.enqueue(session, "test.record", {"n": 1})
    async with session.begin_nested():
        await jobs.enqueue(session, "test.record", {"n": 2})
    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await jobs.enqueue(session, "test.record", {"n": 3})
            raise RuntimeError("the audit line could not be written")
    assert [r.payload["n"] for r in (await session.scalars(select(jobs.Jobs).order_by(jobs.Jobs.id))).all()] == [1, 2]
    # Nothing runs before the commit; after it, each runs once.
    assert await jobs.settle() and SEEN == {}
    await session.commit()
    await jobs.settle()
    assert [p["n"] for p in SEEN["test.record"]] == [1, 2]
    assert await jobs.run_due(ids=await _ids(session)) == 0


async def _ids(session) -> list[int]:
    """This test's jobs (committed ones other tests left are not its to run)."""
    return list((await session.scalars(select(jobs.Jobs.id))).all())


async def test_a_payload_is_json_and_what_is_not_fails_the_enqueue(session):
    rid, when = uuid.uuid4(), datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)
    await jobs.enqueue(session, "test.record", {"id": rid, "at": when, "amount": Decimal("12.50"), "ids": (1, 2)})
    await session.commit()
    await jobs.settle()
    assert SEEN["test.record"] == [{"id": str(rid), "at": when.isoformat(), "amount": "12.50", "ids": [1, 2]}]
    with pytest.raises(TypeError):
        await jobs.enqueue(session, "test.record", {"session": object()})


async def test_a_dedupe_key_keeps_one_unfinished_job(session):
    for _ in range(3):
        await jobs.enqueue(session, "test.record", {"n": 1}, dedupe_key="send:event:7")
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs)) == 1
    await session.commit()
    await jobs.settle()
    # Finished, the key is free again: the next change sets it off once more.
    await jobs.enqueue(session, "test.record", {"n": 2}, dedupe_key="send:event:7")
    await session.commit()
    await jobs.settle()
    assert [p["n"] for p in SEEN["test.record"]] == [1, 2]


async def test_run_at_and_priority(session):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    await jobs.enqueue(session, "test.record", {"n": "later"}, run_at=later)
    await jobs.enqueue(session, "test.record", {"n": "low"}, priority=-5)
    await jobs.enqueue(session, "test.record", {"n": "normal"})
    await jobs.enqueue(session, "test.record", {"n": "urgent"}, priority=10)
    await session.commit()
    await jobs.run_due(ids=await _ids(session))
    assert [p["n"] for p in SEEN["test.record"]] == ["urgent", "normal", "low"]


async def test_a_handler_gives_its_jobs_their_priority_and_a_caller_may_say_otherwise(session):
    await jobs.enqueue(session, "test.later", {"n": 1})
    await jobs.enqueue(session, "test.urgent", {"n": 2})
    await jobs.enqueue(session, "test.urgent", {"n": 3}, priority=jobs.LOW)
    await jobs.enqueue(session, "test.record", {"n": 4})
    await jobs.enqueue_many(session, "test.urgent", [({"n": 5}, None)])
    jobs.add(session, "test.later", {"n": 6})
    await jobs.enqueue(session, "test.nobody.handles", {"n": 7})
    await session.flush()
    rows = (await session.scalars(select(jobs.Jobs))).all()
    assert {r.payload["n"]: r.priority for r in rows} == {1: -1, 2: 1, 3: -1, 4: 0, 5: 1, 6: -1, 7: 0}
    await session.execute(delete(jobs.Jobs).where(jobs.Jobs.name == "test.nobody.handles"))


async def test_a_high_job_is_claimed_before_low_ones_queued_earlier(queue):
    """One slot, three close-outs and a broadcast waiting: the ticket that
    came last goes first, then the broadcast, then the close-outs."""
    async with queue.make() as s:
        for i in range(3):
            await jobs.enqueue(s, "test.later", {"n": i})
        await jobs.enqueue(s, "test.record", {"n": "broadcast"})
        await jobs.enqueue(s, "test.urgent", {"n": "ticket"})
        await s.commit()
    r = queue.runner(concurrency=1)

    async def settled():
        return r.handled >= 5

    assert await _until(settled)
    assert ORDER == ["test.urgent", "test.record", "test.later", "test.later", "test.later"]


async def test_per_name_limits_hold_while_other_names_run(queue):
    """A name capped at one keeps to one however much of it waits, and
    leaves the runner's other slots to the rest."""
    async with queue.make() as s:
        for i in range(4):
            await jobs.enqueue(s, "test.later", {"n": i})
        for i in range(6):
            await jobs.enqueue(s, "test.record", {"n": i})
        await s.commit()
    STATE["delay"] = 0.05
    r = queue.runner(concurrency=4)

    async def settled():
        return r.handled >= 10

    assert await _until(settled)
    # The capped one held one slot; the rest went to the others at once.
    assert PEAK["test.later"] == 1 and PEAK["test.record"] >= 3 and STATE["most"] <= 4


def test_the_platforms_jobs_have_the_priorities_and_limits_chosen(monkeypatch):
    """Every job the app registers, with the urgency and the per-runner cap
    chosen for it (on the API's four slots). A new job must be placed here:
    left NORMAL and uncapped by default, it would queue beside broadcasts."""
    import doth  # noqa: F401

    H, N, L = jobs.HIGH, jobs.NORMAL, jobs.LOW
    chosen = {
        # A mail or push someone is waiting for.
        "admin_alerts.deliver": (H, 4), "mail.billing_notice": (H, 4), "mail.club_domain_notice": (H, 4),
        "clubs.approved_email": (H, 4), "checkins.after": (H, 4),
        "payments.receipt": (H, 2),  # a wkhtmltopdf process each
        # Sends to many. deliveries.send is HIGH for one guest's ticket or certificate (its caller says).
        "deliveries.send": (N, 2), "emails.send_message": (N, 2),
        "notifications.new_event": (N, 2), "notifications.interest": (N, 2), "notifications.push": (N, 2),
        "threads.tell_followers": (N, 2),
        "notifications.broadcast": (N, 4), "push.topic": (N, 4), "push.devices": (N, 4), "push.unsubscribe": (N, 4),
        "live.survey_mail": (N, 4),
        # Nobody watches these happen.
        "closeout.retention": (L, 1), "registrations.import": (L, 1),
    }
    monkeypatch.setattr(settings, "JOBS_LIMITS", "")
    found = {n: (h.priority, jobs.limit_for(h, 4)) for n, h in jobs.handlers().items() if not n.startswith("test.")}
    assert found == chosen
    # APP_JOBS_LIMITS still has the last word, within the runner's own slots.
    monkeypatch.setattr(settings, "JOBS_LIMITS", "closeout.retention=3,deliveries.send=9")
    h = jobs.handlers()
    assert jobs.limit_for(h["closeout.retention"], 4) == 3 and jobs.limit_for(h["deliveries.send"], 4) == 4


# ─── Two runners, one job ────────────────────────────────────────────────


async def test_two_runners_never_run_one_job_twice(queue):
    """Two workers (separate runners and connections) over 300 jobs: every
    job runs exactly once, and both did some of the work."""
    async with queue.make() as s:
        for i in range(300):
            await jobs.enqueue(s, "test.record", {"n": i})
        await s.commit()
    STATE["delay"] = 0.002
    a, b = queue.runner(concurrency=8), queue.runner(concurrency=8)

    async def settled():
        return a.handled + b.handled >= 300

    assert await _until(settled, timeout=30)
    seen = [p["n"] for p in SEEN["test.record"]]
    assert sorted(seen) == list(range(300)), "a job ran twice or not at all"
    assert a.handled > 0 and b.handled > 0 and a.handled + b.handled == 300
    rows = await _rows(queue.make)
    assert all(r.finished_at is not None and r.attempts == 1 and r.locked_by is None for r in rows)


async def _done(n: int) -> bool:
    return len(SEEN.get("test.record", [])) >= n


async def test_concurrent_claims_take_disjoint_rows(queue):
    """The claim itself, ten at once from ten connections: no row twice."""
    async with queue.make() as s:
        for i in range(100):
            await jobs.enqueue(s, "test.record", {"n": i})
        await s.commit()
    want = [("test.record", 10, 30.0)]
    got = await asyncio.gather(*(jobs.claim(f"r{i}", want, 10) for i in range(10)))
    ids = [c.id for batch in got for c in batch]
    assert len(ids) == 100 and len(set(ids)) == 100


# ─── Leases, backoff, dead letters ───────────────────────────────────────


async def test_a_job_whose_runner_died_is_taken_again_when_its_lease_ends(queue):
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
    # Runner A claims it and dies (never settles).
    [claimed] = await jobs.claim("dead-runner", [("test.record", 1, 30.0)], 1)
    [row] = await _rows(queue.make)
    assert row.locked_by == "dead-runner" and row.attempts == 1 and row.run_at > datetime.now(timezone.utc) + timedelta(seconds=25)
    # Inside the lease nobody else may take it.
    assert await jobs.claim("b", [("test.record", 1, 30.0)], 1) == []
    async with queue.make() as s:
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.execute(update(jobs.Jobs).values(run_at=past, locked_until=past))
        await s.commit()
    assert await jobs.run_due(me="b") == 1
    [row] = await _rows(queue.make)
    assert row.finished_at is not None and row.attempts == 2 and SEEN["test.record"] == [{"n": 1}]
    # The dead runner's late settle is fenced off by the attempt it held.
    await jobs._fail(claimed, "too late")
    [row] = await _rows(queue.make)
    assert row.finished_at is not None and not row.dead and row.last_error is None


async def test_a_long_job_keeps_its_lease_while_it_runs(queue, monkeypatch):
    """The lease is short (a dead runner's job is taken again within two
    minutes) and renewed while the job runs, so a job allowed an hour is
    never taken by a second runner while the first is alive."""
    monkeypatch.setattr(jobs, "LEASE", 0.6)
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 0.1)
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
    STATE["delay"] = 2.0
    a = queue.runner(concurrency=1)
    assert await _until(lambda: _in_hand(a))
    b = queue.runner(concurrency=1)  # polls every 0.05 s through the run

    async def settled():
        return a.handled + b.handled >= 1

    assert await _until(settled, timeout=10)
    await asyncio.sleep(0.3)
    assert SEEN["test.record"] == [{"n": 1}] and a.handled == 1 and b.handled == 0
    [row] = await _rows(queue.make)
    assert row.attempts == 1 and row.finished_at is not None


async def test_a_failing_job_backs_off_then_dies_and_the_admins_are_told(queue):
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
    STATE["fail"] = 99
    waits = []
    [row] = await _rows(queue.make)
    for attempt in range(1, 4):
        assert await jobs.run_due(ids=[row.id]) == 1
        [row] = await _rows(queue.make, name="test.record")
        assert row.attempts == attempt and "the provider said no" in row.last_error and row.locked_by is None
        waits.append((row.run_at - datetime.now(timezone.utc)).total_seconds())
        if attempt < 3:
            assert not row.dead and await jobs.run_due() == 0  # not before its time
            async with queue.make() as s:
                await s.execute(update(jobs.Jobs).values(run_at=datetime.now(timezone.utc)))
                await s.commit()
    assert row.dead is True
    # 30 s then 60 s ceilings, each with half of it random.
    assert 14 <= waits[0] <= 31 and 29 <= waits[1] <= 61
    async with queue.make() as s:
        [alert] = (await s.scalars(select(AdminNotifications).where(AdminNotifications.kind == "job_dead"))).all()
    assert alert.data["name"] == "test.record" and alert.data["attempts"] == 3
    # Dead is dead: not claimed again.
    async with queue.make() as s:
        await s.execute(update(jobs.Jobs).where(jobs.Jobs.id == row.id).values(run_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await s.commit()
    assert await jobs.run_due(ids=[row.id]) == 0


def test_backoff_doubles_with_jitter():
    for attempts, ceiling in ((1, 30), (2, 60), (3, 120), (5, 480), (20, 3600)):
        seen = [jobs.backoff(attempts) for _ in range(200)]
        assert all(ceiling / 2 <= w <= ceiling for w in seen)
        assert max(seen) - min(seen) > ceiling / 10  # spread, not one instant


async def test_a_job_past_its_timeout_is_a_failure(queue):
    async with queue.make() as s:
        await jobs.enqueue(s, "test.slow", {"n": 1})
        await s.commit()
    STATE["delay"] = 1.0
    await jobs.run_due()
    [row] = await _rows(queue.make)
    assert row.attempts == 1 and "did not finish within 0.2 s" in row.last_error and not row.dead


async def test_a_job_whose_runner_died_every_time_is_given_up_on(queue):
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
        # Claimed max_attempts times, the lease running out each time.
        await s.execute(update(jobs.Jobs).values(attempts=3, run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()
    await jobs.run_due()
    [row] = await _rows(queue.make, name="test.record")
    assert row.dead and "stopped before it finished" in row.last_error and "test.record" not in SEEN


# ─── Runners: concurrency, waking, stopping ──────────────────────────────


async def test_a_name_with_a_concurrency_of_one_runs_one_at_a_time(queue):
    async with queue.make() as s:
        for i in range(6):
            await jobs.enqueue(s, "test.single", {"n": i})
        await s.commit()
    STATE["delay"] = 0.05
    r = queue.runner(concurrency=4)

    async def settled():
        return r.handled >= 6

    assert await _until(settled)
    assert STATE["most"] == 1 and len(SEEN["test.single"]) == 6


async def test_jobs_limits_overrides_a_names_concurrency(queue, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_LIMITS", "test.record=2")
    async with queue.make() as s:
        for i in range(8):
            await jobs.enqueue(s, "test.record", {"n": i})
        await s.commit()
    STATE["delay"] = 0.05
    queue.runner(concurrency=6)
    assert await _until(lambda: _done(8))
    assert STATE["most"] == 2


async def test_the_api_runs_its_own_jobs_at_once_after_the_commit(queue, monkeypatch):
    """APP_JOBS_WORKER unset: the commit wakes this process's runner, so a
    job runs right after it, not at the next poll -- as the in-memory
    after-commit work did, but written down first."""
    monkeypatch.setattr(settings, "JOBS_POLL_SECONDS", 30.0)
    r = queue.runner(kind="api")
    before = jobs.use(r)
    try:
        await asyncio.sleep(0.05)  # its first look found nothing; it now waits up to 30 s
        async with queue.make() as s:
            await jobs.enqueue(s, "test.record", {"n": 1})
            await s.commit()
        assert await _until(lambda: _done(1), timeout=2.0)
    finally:
        jobs.use(before)


async def test_with_a_worker_the_api_only_enqueues(queue, monkeypatch):
    """APP_JOBS_WORKER: no runner in the API; the job waits for the worker."""
    monkeypatch.setattr(settings, "JOBS_WORKER", True)
    before = jobs.use(None)
    try:
        async with queue.make() as s:
            await jobs.enqueue(s, "test.record", {"n": 1})
            await s.commit()
        await asyncio.sleep(0.2)
        assert "test.record" not in SEEN
        [row] = await _rows(queue.make)
        assert row.attempts == 0 and row.finished_at is None
        w = queue.runner(kind="worker")
        assert await _until(lambda: _done(1))
        assert w.handled == 1
    finally:
        jobs.use(before)


async def test_stopping_hands_back_what_is_in_hand_without_counting_it(queue):
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
    STATE["delay"] = 3.0
    r = queue.runner(concurrency=2)
    assert await _until(lambda: _in_hand(r))
    assert await r.stop(timeout=0.1) is False
    [row] = await _rows(queue.make)
    assert row.attempts == 0 and row.locked_by is None and row.finished_at is None
    assert row.run_at <= datetime.now(timezone.utc)
    # The next runner takes it at once.
    STATE["delay"] = 0.0
    queue.runner()
    assert await _until(lambda: _done(1))


async def _in_hand(r) -> bool:
    return r.in_hand > 0


async def test_a_runner_beats_and_is_forgotten_when_it_stops(queue):
    r = queue.runner(kind="worker", sweeps=True)
    await r.beat()
    async with queue.make() as s:
        alive = await jobs.alive(s)
    assert [(w["id"], w["kind"], w["sweeps"]) for w in alive] == [(r.id, "worker", True)]
    await r.stop(timeout=0.5)
    async with queue.make() as s:
        assert await jobs.alive(s) == []


# ─── Maintenance and figures ─────────────────────────────────────────────


async def test_maintenance_deletes_what_is_past_keeping(queue, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_KEEP_DAYS", 3)
    monkeypatch.setattr(settings, "JOBS_DEAD_KEEP_DAYS", 30)
    now = datetime.now(timezone.utc)
    async with queue.make() as s:
        s.add_all([
            jobs.Jobs(name="test.record", payload={"n": "old"}, finished_at=now - timedelta(days=4)),
            jobs.Jobs(name="test.record", payload={"n": "recent"}, finished_at=now - timedelta(days=1)),
            jobs.Jobs(name="test.record", payload={"n": "dead long ago"}, dead=True, updated_at=now - timedelta(days=31)),
            jobs.Jobs(name="test.record", payload={"n": "dead lately"}, dead=True, updated_at=now - timedelta(days=2)),
            jobs.Jobs(name="test.record", payload={"n": "waiting"}),
        ])
        await s.commit()
    out = await jobs.maintain()
    assert out["finished"] == 1 and out["dead"] == 1
    assert sorted(r.payload["n"] for r in await _rows(queue.make)) == ["dead lately", "recent", "waiting"]


async def test_stats_per_name(queue):
    now = datetime.now(timezone.utc)
    async with queue.make() as s:
        s.add_all([
            jobs.Jobs(name="test.record", payload={}, run_at=now - timedelta(minutes=2)),
            jobs.Jobs(name="test.record", payload={}, run_at=now + timedelta(hours=1)),
            jobs.Jobs(name="test.record", payload={}, locked_by="w", locked_until=now + timedelta(minutes=1), run_at=now + timedelta(minutes=1), attempts=1),
            jobs.Jobs(name="test.single", payload={}, dead=True),
        ])
        await s.commit()
        out = await jobs.stats(s)
    by = {q["name"]: q for q in out["queues"]}
    assert (by["test.record"]["due"], by["test.record"]["scheduled"], by["test.record"]["running"]) == (1, 1, 1)
    assert by["test.record"]["oldest_due_seconds"] >= 110 and by["test.single"]["dead"] == 1


async def test_jobs_finished_are_deleted_at_once_with_no_keeping(queue, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_KEEP_DAYS", 0)
    async with queue.make() as s:
        await jobs.enqueue(s, "test.record", {"n": 1})
        await s.commit()
    await jobs.run_due()
    assert SEEN["test.record"] == [{"n": 1}] and await _rows(queue.make) == []


# ─── Every enqueue has its handler ───────────────────────────────────────


def test_every_job_the_code_enqueues_has_a_handler():
    """A name with no handler is never claimed: it would wait for ever.
    Every literal name passed to enqueue in app/ must be registered once the
    app is imported, as the worker imports it."""
    import re
    from pathlib import Path

    import doth  # noqa: F401

    root = Path(__file__).resolve().parents[1] / "app"
    names = set()
    for path in root.rglob("*.py"):
        names |= set(re.findall(r"""jobs\.enqueue\(\s*\w+,\s*["']([\w.]+)["']""", path.read_text()))
    assert names, "no enqueue found: the pattern above is out of date"
    missing = sorted(n for n in names if n not in jobs.handlers())
    assert missing == []
