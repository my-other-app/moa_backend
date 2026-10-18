"""The write path at scale (docs/SCALING.md, section d): timed emails by their
due instant, one copy of each journey email, registrations that hold the
event's row only for the insert, the connection budget, the counter check on
one process in slices, Razorpay off the event loop, and a deleted club's
subscription cancelled.

The concurrency tests run their sessions on connections of their own and
commit, as concurrent requests do; the usual rolled-back test session cannot
show a race. They clean up after themselves.
"""
import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.clubs.models import Clubs
from app.api.emails import service as emails
from app.api.emails.models import EmailStage, EmailTemplates
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.events.registration import service as registration
from app.api.users.models import UserTypes, Users
from app.config import settings
from app.core.mail.schema import EmailTrigger
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from app.response import CustomHTTPException
from tests.conftest import _make_user, auth_header
from tests.test_club_billing import FakeRazorpay, _row, _start, _webhook, razorpay, shop  # noqa: F401

API = "/api/v1"
START = datetime(2030, 3, 1, 10, 0, tzinfo=timezone.utc)


# ─── Timed emails: due_at kept by the database, the sweep reads only it ──


async def _due(session, tpl_id):
    return await session.scalar(select(EmailTemplates.due_at).where(EmailTemplates.id == tpl_id).execution_options(populate_existing=True))


async def _timed(session, tpl, mode, minutes):
    fields = emails.fields_of(tpl)
    fields.trigger = EmailTrigger(mode=mode, minutes=minutes)
    return await emails.update_template(session, tpl, fields)


async def test_due_at_follows_the_template_and_the_event(session, make_club, make_event):
    owner, club = await make_club(name="Due Club", email="due-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Due Night", event_datetime=START, duration=3.0)
    templates = {t.stage: t for t in await emails.ensure_event_templates(session, event)}
    library = await emails.ensure_library(session, club.id)

    pre = await _timed(session, templates[EmailStage.PRE_EVENT], "BEFORE_START", 24 * 60)
    post = await _timed(session, templates[EmailStage.POST_EVENT], "AFTER_END", 90)
    assert await _due(session, pre.id) == START - timedelta(days=1)
    # The end is the start plus the duration, then the minutes.
    assert await _due(session, post.id) == START + timedelta(hours=3, minutes=90)
    # Untimed ones, and the library's, have no instant.
    assert await _due(session, templates[EmailStage.INVITE].id) is None
    for t in library:
        assert await _due(session, t.id) is None

    # The event moves, and lengthens: every timed email moves with it.
    event.event_datetime = START + timedelta(days=7)
    event.duration = 5.0
    await session.flush()
    assert await _due(session, pre.id) == START + timedelta(days=6)
    assert await _due(session, post.id) == START + timedelta(days=7, hours=5, minutes=90)

    # Switched to a manual send: no instant any more. And the Python rule agrees.
    manual = await _timed(session, pre, "MANUAL", None)
    assert await _due(session, manual.id) is None
    assert emails.due_at(event, emails.fields_of(post).trigger) == await _due(session, post.id)


async def test_the_sweep_reads_only_what_is_due_now(session, make_club, make_event):
    owner, club = await make_club(name="Sweep Club", email="sweep-owner@example.com")
    soon = await make_event(club=club, created_by=owner, name="Soon", event_datetime=START, duration=2.0)
    later = await make_event(club=club, created_by=owner, name="Later", event_datetime=START + timedelta(days=30), duration=2.0)
    gone = await make_event(club=club, created_by=owner, name="Gone", event_datetime=START, duration=2.0)
    picked = {}
    for ev in (soon, later, gone):
        tpl = {t.stage: t for t in await emails.ensure_event_templates(session, ev)}[EmailStage.PRE_EVENT]
        picked[ev.id] = await _timed(session, tpl, "BEFORE_START", 60)
    gone.soft_delete()
    await session.flush()

    async def due_ids(now):
        return [t.id for t, _e in (await session.execute(emails.due_query(now))).unique().all()]

    # Due at START - 1 h, for SWEEP_GRACE after that; never before. (The
    # starter reminder, timed just before the doors, comes due alongside.)
    assert picked[soon.id].id not in await due_ids(START - timedelta(hours=2))
    now_due = await due_ids(START - timedelta(minutes=30))
    assert picked[soon.id].id in now_due
    # The event a month off, and the deleted one, are not read at all.
    later_and_gone = {t.id for t in await session.scalars(select(EmailTemplates).where(EmailTemplates.event_id.in_([later.id, gone.id])))}
    assert not later_and_gone & set(now_due)
    assert picked[soon.id].id not in await due_ids(START - timedelta(hours=1) + emails.SWEEP_GRACE + timedelta(minutes=1))
    # Switched off: not read.
    picked[soon.id].enabled = False
    await session.flush()
    assert picked[soon.id].id not in await due_ids(START - timedelta(minutes=30))


async def test_one_process_holds_a_sweep_and_its_commits_do_not_keep_the_lock(_engine):
    from app.db.locks import singleton

    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    key = 990_001
    async with make() as s:
        async with singleton(s, key) as first:
            assert first
            async with singleton(_engine, key) as second:
                assert not second
            # The work commits, as the sweep does; the lock is not on the session's connection.
            await s.execute(text("SELECT 1"))
            await s.commit()
    async with singleton(_engine, key) as again:
        assert again


async def test_sending_does_not_block_the_event_loop(session, make_club, monkeypatch):
    """Storage reads, image downloads, the QR and the render run in threads."""
    _owner, club = await make_club(name="Thread Club", email="thread-owner@example.com")
    where = {}

    def fetch(url):
        where["fetch"] = threading.current_thread() is threading.main_thread()
        return None

    real_render = emails.render_email

    def render(data):
        where["render"] = threading.current_thread() is threading.main_thread()
        return real_render(data)

    monkeypatch.setattr(emails, "fetch_bytes", fetch)
    monkeypatch.setattr(emails, "render_email", render)
    starter = emails.email_starter("PRE_EVENT")
    out = await emails.compose(
        session, emails.sample_event(club), None, starter.subject, starter.design, starter.attachments,
        brand="#123456", logo_url="https://example.com/logo.png",
    )
    assert out.html and where == {"fetch": False, "render": False}


# ─── One copy of each journey email ────────────────────────────────────────


async def _club_and_event(make, tag):
    async with make() as s:
        owner = await _make_user(s, email=f"{tag}-owner@example.com", user_type=UserTypes.club, username=f"{tag}owner")
        club = Clubs(slug=f"{tag}-club", name=f"{tag} Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=tag, created_by_id=owner.id)
        s.add(category)
        await s.flush()
        event = Events(slug=f"{tag}-night", name=f"{tag} Night", images=[], duration=2.0,
                       event_datetime=datetime.now(timezone.utc) + timedelta(days=3), club_id=club.id, category_id=category.id)
        s.add(event)
        await s.commit()
        return dict(owner=owner.id, club=club.id, category=category.id, event=event.id)


async def _drop_world(make, w, extra_users=()):
    async with make() as s:
        await s.execute(delete(EmailTemplates).where(EmailTemplates.club_id == w["club"]))
        await s.execute(delete(EventRegistrationsLink).where(EventRegistrationsLink.event_id.in_(w.get("events", [w["event"]]))))
        await s.execute(delete(Events).where(Events.id.in_(w.get("events", [w["event"]]))))
        await s.execute(delete(EventCategories).where(EventCategories.id == w["category"]))
        from app.api.subscriptions.models import ClubSubscriptions

        await s.execute(delete(ClubSubscriptions).where(ClubSubscriptions.club_id == w["club"]))
        await s.execute(delete(Clubs).where(Clubs.id == w["club"]))
        await s.execute(delete(Users).where(Users.id.in_([w["owner"], *extra_users])))
        await s.commit()


async def test_a_burst_copies_the_clubs_library_once(_engine):
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    w = await _club_and_event(make, "libburst")
    try:
        async def ask():
            async with make() as s:
                rows = await emails.ensure_library(s, w["club"])
                await s.commit()
                return {t.stage: t.id for t in rows}

        answers = await asyncio.gather(*(ask() for _ in range(6)))
        # Every request reads back the same four.
        assert all(a == answers[0] for a in answers) and len(answers[0]) == len(emails.JOURNEY_STAGES)
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(EmailTemplates).where(EmailTemplates.club_id == w["club"])) == 4
    finally:
        await _drop_world(make, w)


async def test_a_second_copy_of_a_journey_stage_is_refused_but_custom_ones_are_not(session, make_club, make_event):
    owner, club = await make_club(name="Unique Club", email="unique-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Unique Night", event_datetime=START)
    await emails.ensure_event_templates(session, event)
    starter = emails.email_starter("CUSTOM")
    for _ in range(2):
        session.add(emails._new(club.id, event.id, EmailStage.CUSTOM, starter))
    await session.flush()
    nested = await session.begin_nested()
    session.add(emails._new(club.id, event.id, EmailStage.PRE_EVENT, emails.email_starter("PRE_EVENT")))
    with pytest.raises(IntegrityError):
        await session.flush()
    await nested.rollback()


# ─── Registrations: no oversell, the row held only for the insert ─────────


async def _people(make, tag, n):
    async with make() as s:
        users = [await _make_user(s, email=f"{tag}{i}@example.com", username=f"{tag}{i}") for i in range(n)]
        await s.commit()
        return [(u.id, u.email) for u in users]


async def _register_all(make, event_id, people, **kw):
    async def one(user_id, email):
        async with make() as s:
            add_loader_criteria(s)
            try:
                data = await registration.register_event(
                    s, full_name="Burst Guest", email=email, event_id=event_id, user_id=user_id, notify=False, **kw
                )
                return ("ok", data["ticket_id"])
            except CustomHTTPException as exc:
                return (exc.status_code, exc.error_code or exc.message)

    return await asyncio.gather(*(one(u, e) for u, e in people))


async def _rows(make, event_id):
    async with make() as s:
        going = await s.scalar(select(Events.going_count).where(Events.id == event_id))
        regs = (await s.execute(
            select(EventRegistrationsLink.admission_no, EventRegistrationsLink.is_paid)
            .where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
        )).all()
        return going, regs


async def test_a_burst_on_a_free_event_never_oversells(_engine):
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    w = await _club_and_event(make, "freeburst")
    people = await _people(make, "freeburst-guest", 12)
    try:
        async with make() as s:
            await s.execute(update(Events).where(Events.id == w["event"]).values(max_participants=5))
            await s.commit()
        outcomes = await _register_all(make, w["event"], people)
        assert sum(1 for o in outcomes if o[0] == "ok") == 5
        assert all(o == (400, "EVENT_FULL") for o in outcomes if o[0] != "ok")
        going, regs = await _rows(make, w["event"])
        assert going == len(regs) == 5
        # Each its own admission number, none skipped: only a registration that got a place took one.
        assert len({r.admission_no for r in regs}) == 5
        async with make() as s:
            assert await s.scalar(select(Events.admission_seq).where(Events.id == w["event"])) == 5
    finally:
        await _drop_world(make, w, extra_users=[u for u, _ in people])


async def test_a_burst_on_a_paid_event_keeps_its_places_for_the_paid(_engine):
    """Unpaid registrations hold no place, as before; once the paid ones fill
    it, the event is full -- and someone who registered unpaid and comes
    back gets their own registration to pay for."""
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    w = await _club_and_event(make, "paidburst")
    people = await _people(make, "paidburst-guest", 10)
    try:
        async with make() as s:
            await s.execute(update(Events).where(Events.id == w["event"]).values(max_participants=3, has_fee=True, reg_fee=100))
            await s.commit()
        outcomes = await _register_all(make, w["event"], people[:8])
        assert [o[0] for o in outcomes] == ["ok"] * 8
        going, regs = await _rows(make, w["event"])
        assert going == 8 and len({r.admission_no for r in regs}) == 8

        # Back for the payment: the same registration, the fee still to pay.
        back = await _register_all(make, w["event"], people[:1])
        assert back == [outcomes[0]]

        async with make() as s:
            await s.execute(
                update(EventRegistrationsLink)
                .where(EventRegistrationsLink.event_id == w["event"], EventRegistrationsLink.user_id.in_([u for u, _ in people[:3]]))
                .values(is_paid=True, paid_amount=100)
            )
            await s.commit()
        late = await _register_all(make, w["event"], people[8:])
        assert late == [(400, "EVENT_FULL")] * 2
    finally:
        await _drop_world(make, w, extra_users=[u for u, _ in people])


async def _free_plan(make, **limits):
    from app.api.subscriptions.models import PlanFeatures, PlanLimits, Plans

    async with make() as s:
        plan = Plans(key="free", name="Free")
        s.add(plan)
        await s.flush()
        s.add(PlanLimits(plan_id=plan.id, **limits))
        s.add(PlanFeatures(plan_id=plan.id))
        await s.commit()
        return plan.id


async def _drop_plan(make, plan_id):
    from app.api.subscriptions.models import PlanFeatures, PlanLimits, Plans

    async with make() as s:
        await s.execute(delete(PlanLimits).where(PlanLimits.plan_id == plan_id))
        await s.execute(delete(PlanFeatures).where(PlanFeatures.plan_id == plan_id))
        await s.execute(delete(Plans).where(Plans.id == plan_id))
        await s.commit()


@pytest.fixture
def no_owner_mail(monkeypatch):
    from app.api.subscriptions import gates

    monkeypatch.setattr(gates, "schedule", lambda coro: coro.close())
    gates.reset()


async def test_a_burst_never_passes_the_plans_cap_for_the_event(_engine, no_owner_mail):
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    plan_id = await _free_plan(make, registrations_per_event=4)
    w = await _club_and_event(make, "capburst")
    people = await _people(make, "capburst-guest", 10)
    try:
        outcomes = await _register_all(make, w["event"], people)
        assert sum(1 for o in outcomes if o[0] == "ok") == 4
        assert all(o == (409, "REGISTRATIONS_CLOSED") for o in outcomes if o[0] != "ok")
        going, regs = await _rows(make, w["event"])
        assert going == len(regs) == 4
    finally:
        await _drop_world(make, w, extra_users=[u for u, _ in people])
        await _drop_plan(make, plan_id)


async def test_a_burst_at_the_monthly_cap_takes_only_what_is_left(_engine, no_owner_mail):
    """Near the club's monthly cap the month is counted again under the
    event's row, so one event's burst cannot run past it."""
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    plan_id = await _free_plan(make, registrations_per_month=5)
    w = await _club_and_event(make, "monthburst")
    people = await _people(make, "monthburst-guest", 10)
    try:
        first = await _register_all(make, w["event"], people[:2])
        assert [o[0] for o in first] == ["ok", "ok"]
        outcomes = await _register_all(make, w["event"], people[2:])
        assert sum(1 for o in outcomes if o[0] == "ok") == 3
        assert all(o == (409, "REGISTRATIONS_CLOSED") for o in outcomes if o[0] != "ok")
        going, regs = await _rows(make, w["event"])
        assert going == len(regs) == 5
    finally:
        await _drop_world(make, w, extra_users=[u for u, _ in people])
        await _drop_plan(make, plan_id)


async def test_a_double_submit_registers_once(_engine):
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    w = await _club_and_event(make, "dblsubmit")
    people = await _people(make, "dblsubmit-guest", 1)
    try:
        outcomes = await _register_all(make, w["event"], people * 4)
        assert sorted(o[0] for o in outcomes if o[0] != "ok") == [400, 400, 400]
        going, regs = await _rows(make, w["event"])
        assert going == len(regs) == 1
    finally:
        await _drop_world(make, w, extra_users=[u for u, _ in people])


async def test_the_badge_metrics_are_one_statement(session, make_club, make_event, make_user):
    from app.api.badges import service as badges
    from sqlalchemy import event as sa_event

    owner, club = await make_club(name="Badge Club", email="badge-owner@example.com")
    u = await make_user(email="badge-u@example.com")
    for i, (attended, position, paid) in enumerate(((True, 1, True), (True, 3, False), (False, None, False))):
        ev = await make_event(club=club, created_by=owner, name=f"Badge {i}", event_datetime=START)
        session.add(EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name="B", email=u.email, ticket_id=f"B-{uuid.uuid4().hex[:10]}",
                                           is_attended=attended, position=position, is_paid=paid))
    await session.flush()
    seen = []
    engine = (await session.connection()).sync_connection.engine

    def count(conn, cursor, statement, *_rest):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        metrics = await badges._get_user_metrics(session, u.id)
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert metrics == {"events_registered": 3, "events_attended": 2, "events_won": 1, "runner_up_count": 0, "podium_count": 1,
                       "events_rated": 0, "events_paid": 1}
    assert len(seen) == 1


async def test_the_monthly_count_still_leaves_out_deleted_events(session, make_club, make_event, make_user):
    """Same answer as before (registrations of the club's live events this
    month), now the deleted-events filter is spelled out so Postgres can use
    ix_events_club_start."""
    from app.api.subscriptions import gates

    owner, club = await make_club(name="Month Club", email="month-owner@example.com")
    live = await make_event(club=club, created_by=owner, name="Live", event_datetime=START)
    dead = await make_event(club=club, created_by=owner, name="Dead", event_datetime=START)
    for ev in (live, dead):
        for i in range(2):
            u = await make_user(email=f"month-{ev.id}-{i}@example.com")
            session.add(EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name="M", email=u.email, ticket_id=f"M-{uuid.uuid4().hex[:10]}"))
    dead.soft_delete()
    await session.flush()
    assert await gates.count(session, club.id, "registrations_per_month") == 2
    from app.api.subscriptions import service

    assert (await service.usage(session, club.id))["registrations_per_month"] == 2


# ─── The connection budget ─────────────────────────────────────────────────


def test_the_budget_says_when_the_workers_pools_do_not_fit():
    from app.db.core import connection_budget

    assert connection_budget(1, 10, 20, 100, 3) is None
    assert connection_budget(3, 10, 20, 100, 3) is None
    problem = connection_budget(4, 10, 20, 100, 3)
    assert "120 connections" in problem and "takes 97" in problem
    assert connection_budget(4, 5, 5, 100, 3) is None


async def test_startup_warns_when_the_pools_could_ask_for_more_than_the_server_takes(_engine, monkeypatch, caplog):
    from app.db.core import check_connection_budget

    max_connections = int(await _scalar(_engine, "SHOW max_connections"))
    monkeypatch.setattr(settings, "WORKERS", 1)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 3)
    with caplog.at_level(logging.WARNING, logger="app.db.budget"):
        assert await check_connection_budget(_engine) is None
    assert not caplog.records
    monkeypatch.setattr(settings, "WORKERS", 4)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", max_connections)
    with caplog.at_level(logging.WARNING, logger="app.db.budget"):
        problem = await check_connection_budget(_engine)
    assert problem and any("connection budget" in r.getMessage() for r in caplog.records)


def test_the_pool_takes_its_size_from_the_settings():
    from app.db.core import engine

    assert engine.sync_engine.pool.size() == settings.DB_POOL_SIZE
    assert engine.sync_engine.pool._max_overflow == settings.DB_MAX_OVERFLOW


async def _scalar(engine, sql):
    async with engine.connect() as c:
        return await c.scalar(text(sql))


# ─── The counter check: one process, every six hours, in slices ──────────


def test_slices_cover_every_id_once():
    from app.api.discover.counters import slices

    assert list(slices(1, 10, 4)) == [(1, 4), (5, 8), (9, 10)]
    assert list(slices(7, 7, 100)) == [(7, 7)]
    assert list(slices(None, None, 4)) == []


async def test_the_check_finds_drift_in_any_slice(session, make_club, make_event, make_user):
    from app.api.discover import counters

    owner, club = await make_club(name="Slice Club", email="slice-owner@example.com")
    events = [await make_event(club=club, created_by=owner, name=f"Slice {i}", event_datetime=START) for i in range(5)]
    u = await make_user(email="slice-u@example.com")
    for ev in events:
        session.add(EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name="S", email=u.email, ticket_id=f"S-{uuid.uuid4().hex[:10]}"))
    await session.flush()
    out = [events[0].id, events[3].id]
    await session.execute(text("UPDATE events SET going_count = 9 WHERE id = ANY(:ids)"), {"ids": out})
    # One event per statement, then two: the same answer.
    for size in (1, 2, counters.EVENT_SLICE):
        assert sorted((await counters.drift(session, event_slice=size, club_slice=1))["events"]) == sorted(out)
    assert await counters.reconcile(session) == {"events": 2, "clubs": 0}
    assert (await counters.drift(session, event_slice=1))["events"] == []


async def test_the_check_runs_on_one_process_every_six_hours(session, _engine):
    from app.api.discover import counters
    from app.api.models import ScheduledRuns

    now = datetime.now(timezone.utc)
    assert await counters.run_if_due(session, now=now) == {"events": 0, "clubs": 0}
    row = await session.scalar(select(ScheduledRuns).where(ScheduledRuns.name == counters.JOB).execution_options(populate_existing=True))
    assert row.started_at == now and row.finished_at is not None and row.result == {"events": 0, "clubs": 0}
    # Not again until six hours have passed, whichever process asks.
    assert await counters.run_if_due(session, now=now + timedelta(hours=5)) is None
    assert await counters.run_if_due(session, now=now + timedelta(hours=6, seconds=1)) is not None
    # Another process holding the check: this one leaves it.
    async with _engine.connect() as other:
        await other.execute(text(f"SELECT pg_advisory_lock({counters.COUNTERS_LOCK})"))
        try:
            assert await counters.run_if_due(session, now=now + timedelta(days=2)) is None
        finally:
            await other.execute(text(f"SELECT pg_advisory_unlock({counters.COUNTERS_LOCK})"))


# ─── Razorpay off the event loop ───────────────────────────────────────────


async def test_a_razorpay_call_runs_in_a_thread_while_the_loop_serves():
    from app.api.payments import rzp

    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    def slow(order, *, amount):
        time.sleep(0.3)
        return {"id": order, "amount": amount, "main": threading.current_thread() is threading.main_thread()}

    task = asyncio.create_task(ticker())
    out = await rzp.call(slow, "order_1", amount=100)
    stop.set()
    await task
    assert out == {"id": "order_1", "amount": 100, "main": False}
    # The loop went on meanwhile: a blocking call would have allowed none.
    assert ticks >= 10


async def test_a_razorpay_call_gives_up_after_its_timeout():
    from app.api.payments import rzp

    started = time.perf_counter()
    with pytest.raises(rzp.RazorpayTimeout):
        await rzp.call(lambda: time.sleep(1.0), timeout=0.05)
    assert time.perf_counter() - started < 0.5


def test_the_clients_requests_time_out(monkeypatch):
    import requests

    from app.api.payments import rzp

    seen = {}

    def request(self, method, url, **kwargs):
        seen.update(kwargs)
        raise requests.ConnectionError("not here")

    monkeypatch.setattr(requests.Session, "request", request)
    client = rzp.new_client("rzp_test_x", "secret")
    with pytest.raises(requests.ConnectionError):
        client.order.fetch("order_1")
    assert seen["timeout"] == settings.RAZORPAY_TIMEOUT


async def test_the_fakes_are_still_what_billing_calls(razorpay):  # noqa: F811
    from app.api.subscriptions import billing

    await billing.cancel_subscription("sub_fake", at_period_end=False)
    assert razorpay.of("subscription", "cancel") == [("sub_fake", {"cancel_at_cycle_end": 0})]


# ─── A deleted club stops paying ───────────────────────────────────────────


async def _active(client, session, shop):  # noqa: F811
    sub_id = (await _start(client, shop, plan_key="growth")).json()["razorpay_subscription_id"]
    await _webhook(session, "subscription.charged", sub_id, current_end=datetime.now(timezone.utc) + timedelta(days=30))
    assert (await _row(session, shop["club"].id)).status.value == "active"
    return sub_id


async def _alerts(session, club_id):
    from app.api.admin_alerts.models import AdminNotifications

    return list((await session.scalars(
        select(AdminNotifications).where(AdminNotifications.club_id == club_id, AdminNotifications.kind == "subscription_cancelled")
    )).all())


async def test_deleting_a_club_cancels_its_subscription(client, session, shop, razorpay, make_admin, mint_token):  # noqa: F811
    sub_id = await _active(client, session, shop)
    admin = auth_header(await mint_token(await make_admin(email="del-admin@example.com")))
    r = await client.delete(f"{API}/clubs/admin/{shop['club'].id}", headers=admin)
    assert r.status_code == 200, r.text
    assert razorpay.of("subscription", "cancel") == [(sub_id, {"cancel_at_cycle_end": 0})]
    row = await _row(session, shop["club"].id)
    assert row.status.value == "cancelled" and row.mrr_paise == 0
    [alert] = await _alerts(session, shop["club"].id)
    assert alert.data["by"] == "club_deleted" and alert.data["razorpay"] == "cancelled"

    # Restoring the club does not bring the subscription back.
    assert (await client.post(f"{API}/clubs/admin/{shop['club'].id}/restore", headers=admin)).status_code == 200
    assert (await _row(session, shop["club"].id)).status.value == "cancelled"
    assert len(razorpay.of("subscription", "cancel")) == 1


async def test_deleting_a_clubs_account_cancels_it_even_when_razorpay_will_not(client, session, shop, razorpay, make_admin, mint_token):  # noqa: F811
    await _active(client, session, shop)

    def refuse(*_a):
        raise RuntimeError("razorpay is down")

    razorpay.handlers[("subscription", "cancel")] = refuse
    razorpay.handlers[("subscription", "fetch")] = refuse
    admin = auth_header(await mint_token(await make_admin(email="del-admin2@example.com")))
    r = await client.delete(f"{API}/user/admin/{shop['owner'].id}", headers=admin)
    assert r.status_code == 200, r.text
    row = await _row(session, shop["club"].id)
    assert row.status.value == "cancelled"
    [alert] = await _alerts(session, shop["club"].id)
    assert alert.data["by"] == "club_deleted" and alert.data["razorpay"] == "razorpay_failed"
    assert "Razorpay dashboard" in alert.body


async def test_a_club_with_nothing_at_razorpay_is_deleted_quietly(client, session, make_club, make_admin, mint_token):
    _owner, club = await make_club(name="Quiet Club", email="quiet-owner@example.com")
    admin = auth_header(await mint_token(await make_admin(email="del-admin3@example.com")))
    assert (await client.delete(f"{API}/clubs/admin/{club.id}", headers=admin)).status_code == 200
    assert await _alerts(session, club.id) == []


async def test_a_registrations_ticket_reads_only_its_own_deliveries(session, make_club, make_event, make_user):
    """send_ticket queued one guest's ticket by reading every delivery the
    event had ever made: a third of a second a registration on a big event."""
    from sqlalchemy import event as sa_event

    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind

    owner, club = await make_club(name="Queue Club", email="queue-owner@example.com")
    ev = await make_event(club=club, created_by=owner, name="Queue Night", event_datetime=START)
    regs = []
    for i in range(3):
        u = await make_user(email=f"queue-{i}@example.com")
        reg = EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name="Q", email=u.email, ticket_id=f"Q-{uuid.uuid4().hex[:10]}")
        session.add(reg)
        regs.append(reg)
    await session.flush()
    everyone = await deliveries.queue(session, ev, DeliveryKind.ticket)
    assert everyone["queued"] == 3 * len(deliveries.CHANNELS)

    seen = []
    engine = (await session.connection()).sync_connection.engine

    def capture(conn, cursor, statement, parameters, *_rest):
        if statement.lstrip().startswith("SELECT deliveries."):
            seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        again = await deliveries.queue(session, ev, DeliveryKind.ticket, registration_ids=[regs[0].id])
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert again["queued"] == len(deliveries.CHANNELS)
    assert seen and all("deliveries.registration_id IN" in s for s in seen)
