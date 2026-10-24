"""Mail that goes once, goes in the end, goes at the provider's pace and
leaves the database alone while it goes.

Each test here failed before the change it is named for:

- a notification found its send by an index, and found it after a re-send;
- a send claimed each row, so two clicks or two workers mailed nobody twice;
- the minute resumer finished what a deploy cut off, with backoff, and gave
  up (telling the admins) when the provider stayed down;
- no connection was held while a mail rendered or waited on SES;
- several mails were in flight at once under one rate, and a big reminder
  did not hold up a small one;
- a hard bounce or complaint kept the address out of every event's mail;
- the after-commit mails that must go were written down with their change,
  and shutdown waited for what it had started;
- an audience was counted by the database.

Most run on the test's rolled-back session. Those about real concurrency or
connections run on committed rows of their own (`committed`), on sessions of
their own, and remove what they made.
"""
import asyncio
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.admin_alerts.models import AdminNotifications
from app.api.clubs.models import Clubs
from app.api.deliveries import pipeline
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryAttempts, DeliveryKind, DeliveryOutcome, DeliveryStatus
from app.api.emails import service as emails
from app.api.emails.models import MessageDeliveries, Messages, MessageStatus
from app.api.events.tickets import Document
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.hooks import service as hooks
from app.api.hooks.verdicts import Verdict
from app.api.users.models import Users, UserTypes
from app.config import settings
from app.core import after_commit, jobs
from app.core.email import email as email_mod
from app.core.email import pace, transport
from app.core.mail import suppression
from app.core.mail.schema import EmailAudience
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from tests.conftest import _make_user, auth_header

START = datetime.now(timezone.utc) + timedelta(days=3)


@pytest.fixture(autouse=True)
async def fresh(session, monkeypatch):
    """No job, bucket or client from another test; the jobs a test starts
    finished before its session goes; the bucket wide enough not to slow a
    test that is not about it."""
    pipeline.reset_for_tests()
    pace.reset_for_tests()
    monkeypatch.setattr(settings, "MAIL_SEND_RATE", 500.0)
    yield
    await pipeline.drain(timeout=30)
    pipeline.reset_for_tests()
    pace.reset_for_tests()


def _fake_document(*_a, **_k):
    async def document(session, reg):
        return Document(content=b"%PDF-1.4 fake", filename=f"ticket-{reg.ticket_id}.pdf", design_version=0)

    return document


@pytest.fixture
def no_pdf(monkeypatch):
    """Tickets attached without drawing them: these tests are about sending."""
    monkeypatch.setattr(deliveries, "ticket_document", _fake_document())
    monkeypatch.setattr(emails, "ticket_document", _fake_document())


@pytest.fixture
def outbox(monkeypatch):
    """Every mail handed over, the key it went with, and switches: refuse it,
    defer it (the provider said "not now"), take a while over it."""
    sent: list[dict] = []
    state = {"refuse": None, "defer": None, "delay": 0.0, "in_flight": 0, "most": 0}
    lock = threading.Lock()

    def send(**kwargs):
        with lock:
            state["in_flight"] += 1
            state["most"] = max(state["most"], state["in_flight"])
        try:
            if state["delay"]:
                time.sleep(state["delay"])
            if state["refuse"]:
                raise RuntimeError(state["refuse"])
            if state["defer"]:
                raise transport.MailDeferred(state["defer"])
            with lock:
                sent.append(kwargs)
                return {"MessageId": f"ses-{uuid.uuid4().hex[:12]}", "Provider": "ses"}
        finally:
            with lock:
                state["in_flight"] -= 1

    monkeypatch.setattr(email_mod, "send_email", send)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", send)
    return SimpleNamespace(sent=sent, state=state)


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Integrity", email="integrity-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Integrity Night", event_datetime=START, duration=3.0)
    regs = []
    for i, name in enumerate(["Ananya", "Bala", "Chitra", "Devika"]):
        u = await make_user(email=f"{name.lower()}-i@example.com")
        regs.append(await make_registration(event=event, user=u, full_name=f"{name} Guest"))
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, regs=regs)


async def _rows(session, event_id):
    return {d.registration_id: d for d in await session.scalars(select(Deliveries).where(Deliveries.event_id == event_id).execution_options(populate_existing=True))}


# ─── 1. A notification finds its send, by an index, after a re-send ─────────


async def test_the_provider_id_is_indexed_on_both_send_tables(session):
    found = set(
        await session.scalars(
            text("SELECT indexname FROM pg_indexes WHERE indexname IN ('ix_deliveries_provider_id', 'ix_message_deliveries_provider_id')")
        )
    )
    assert found == {"ix_deliveries_provider_id", "ix_message_deliveries_provider_id"}
    # And a lookup by it uses the index rather than reading the table.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(await session.scalars(text("EXPLAIN SELECT id FROM deliveries WHERE provider_id = 'ses-x'")))
    assert "ix_deliveries_provider_id" in plan


async def test_a_late_bounce_of_a_send_since_repeated_still_lands(session, world, outbox, background, no_pdf):
    a = world.regs[0]
    first = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id])
    await deliveries.run(first["ids"])
    row = (await _rows(session, world.event.id))[a.id]
    first_id = row.provider_id
    # Sent again: the row now carries the second send's id.
    again = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id], force=True)
    await deliveries.run(again["ids"])
    row = (await _rows(session, world.event.id))[a.id]
    assert row.provider_id != first_id and len(outbox.sent) == 2
    attempts = list(await session.scalars(select(DeliveryAttempts).where(DeliveryAttempts.delivery_id == row.id)))
    assert {x.provider_id for x in attempts} == {first_id, row.provider_id}

    # The first send's bounce arrives now: it used to match nothing at all.
    matched = await hooks.record(session, first_id, "email", Verdict(DeliveryOutcome.bounced, "550 user unknown", "General"), datetime.now(timezone.utc), address=a.email)
    assert matched == 1
    row = (await _rows(session, world.event.id))[a.id]
    await session.refresh(a)
    assert row.outcome is DeliveryOutcome.bounced and a.email_bounced_at is not None
    assert await session.get(suppression.EmailSuppressions, a.email.lower()) is not None


async def test_the_bounce_of_a_corrected_address_leaves_the_new_one_alone(session, world, outbox, background, no_pdf):
    a = world.regs[1]
    old = a.email
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id])
    await deliveries.run(q["ids"])
    old_id = (await _rows(session, world.event.id))[a.id].provider_id
    a.email = "bala.fixed-i@example.com"
    await session.commit()
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id], force=True)
    await deliveries.run(q["ids"])

    matched = await hooks.record(session, old_id, "email", Verdict(DeliveryOutcome.bounced, "550 no such user", "General"), datetime.now(timezone.utc), address=old)
    await session.refresh(a)
    assert matched == 0 and a.email_bounced_at is None
    # The dead address itself is never mailed again.
    assert await session.get(suppression.EmailSuppressions, old.lower()) is not None
    assert await session.get(suppression.EmailSuppressions, a.email.lower()) is None


# ─── 3. Claimed once: two clicks, two workers, a timeout ─────────────────────


async def test_two_runs_over_the_same_queue_mail_each_guest_once(session, world, outbox, background, no_pdf):
    outbox.state["delay"] = 0.02
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket)
    # The second click's run starts while the first is still sending.
    await asyncio.gather(deliveries.run(q["ids"]), deliveries.run(q["ids"]))
    assert sorted(m["recipients"][0] for m in outbox.sent) == sorted(r.email for r in world.regs)
    rows = await _rows(session, world.event.id)
    assert all(d.status is DeliveryStatus.sent and d.attempts == 1 for d in rows.values())


async def test_a_second_click_while_sending_resets_nothing_under_the_worker(session, world, outbox, background, no_pdf):
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket)
    # A worker holds the first guest's row right now.
    held = q["ids"][0]
    await session.execute(update(Deliveries).where(Deliveries.id == held).values(status=DeliveryStatus.sending, claimed_at=datetime.now(timezone.utc)))
    await session.commit()
    again = await deliveries.queue(session, world.event, DeliveryKind.ticket, only="all", force=True)
    assert held not in again["ids"]
    assert (await session.get(Deliveries, held, populate_existing=True)).status is DeliveryStatus.sending
    # Everyone still counts as queued: they are.
    assert again["queued"] == len(world.regs)


async def test_a_template_send_goes_once_per_client_ref(client, session, world, outbox, background, mint_token):
    h = auth_header(await mint_token(world.owner))
    journey = (await client.get(f"/api/v1/events/{world.event.id}/emails", headers=h)).json()
    reminder = next(s for s in journey["journey"] if s["stage"] == "REMINDER")["template"]
    ref = str(uuid.uuid4())
    first = await client.post(f"/api/v1/events/{world.event.id}/emails/{reminder['id']}/send", json={"clientRef": ref}, headers=h)
    second = await client.post(f"/api/v1/events/{world.event.id}/emails/{reminder['id']}/send", json={"clientRef": ref}, headers=h)
    assert first.status_code == 200, first.text
    assert second.json()["id"] == first.json()["id"]
    assert await session.scalar(select(func.count()).select_from(Messages).where(Messages.event_id == world.event.id)) == 1
    assert len(outbox.sent) == first.json()["recipients"]
    # No body at all still sends, as the page does today.
    assert (await client.post(f"/api/v1/events/{world.event.id}/emails/{reminder['id']}/send", headers=h)).status_code == 200


async def test_a_double_click_without_a_client_ref_returns_the_send_in_progress(session, world):
    templates = await emails.ensure_event_templates(session, world.event)
    reminder = next(t for t in templates if t.stage.value == "REMINDER")
    first = await emails.send_template(session, world.event, reminder)
    second = await emails.send_template(session, world.event, reminder)
    assert second.id == first.id
    assert await session.scalar(select(func.count()).select_from(Messages).where(Messages.template_id == reminder.id)) == 1


async def test_a_provider_timeout_is_tried_again_later_under_the_same_key(session, world, outbox, background, no_pdf):
    a = world.regs[2]
    outbox.state["defer"] = "Resend did not answer (ReadTimeout); it will be tried again"
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id])
    await deliveries.run(q["ids"])
    row = (await _rows(session, world.event.id))[a.id]
    assert row.status is DeliveryStatus.pending and row.tries == 1 and "will try again" in row.error
    wait = row.next_attempt_at - datetime.now(timezone.utc)
    assert timedelta(seconds=45) < wait < timedelta(seconds=75)
    # Not due yet: the resumer leaves it.
    assert (await deliveries.resume(session))["started"] == 0

    keys: list = []
    real = email_mod.send_email

    def remember(**kwargs):
        keys.append(kwargs.get("idempotency_key"))
        return real(**kwargs)

    outbox.state["defer"] = None
    email_mod.send_email = remember
    try:
        await session.execute(update(Deliveries).where(Deliveries.id == row.id).values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()
        assert (await deliveries.resume(session))["started"] == 1
        await jobs.settle()
        await pipeline.drain()
    finally:
        email_mod.send_email = real
    row = (await _rows(session, world.event.id))[a.id]
    assert row.status is DeliveryStatus.sent and row.tries == 2 and len(outbox.sent) == 1
    assert keys == [f"delivery-{row.id}-{int(row.queued_at.timestamp())}"]


# ─── 4. The resumer ──────────────────────────────────────────────────────────


async def test_a_claim_left_by_a_dead_worker_is_put_back_and_sent(session, world, outbox, background, no_pdf):
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=11)
    await session.execute(update(Deliveries).where(Deliveries.id.in_(q["ids"])).values(status=DeliveryStatus.sending, claimed_at=long_ago, tries=1, attempts=1))
    await session.commit()
    resumed = await deliveries.resume(session)
    assert resumed["stale"] == len(world.regs) and resumed["started"] == 1
    await jobs.settle()
    await pipeline.drain()
    assert len(outbox.sent) == len(world.regs)
    assert all(d.status is DeliveryStatus.sent for d in (await _rows(session, world.event.id)).values())


async def test_a_queue_whose_worker_never_started_is_picked_up(session, world, outbox, background, no_pdf):
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket)
    await session.execute(update(Deliveries).where(Deliveries.id.in_(q["ids"])).values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=3)))
    await session.commit()
    assert (await deliveries.resume(session))["started"] == 1
    await jobs.settle()
    await pipeline.drain()
    assert len(outbox.sent) == len(world.regs)
    # A day-old queue is not sent: it may be for an event now over.
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.execute(update(Deliveries).where(Deliveries.event_id == world.event.id, Deliveries.status == DeliveryStatus.sent).values(status=DeliveryStatus.pending, queued_at=old))
    await session.commit()
    assert (await deliveries.resume(session))["too_old"] == len(world.regs)


async def test_a_reminder_cut_off_by_a_deploy_is_finished_not_sent_again(session, world, outbox, background):
    templates = await emails.ensure_event_templates(session, world.event)
    reminder = next(t for t in templates if t.stage.value == "REMINDER")
    reminder.audience = {"who": "ALL", "roles": []}
    await session.commit()
    msg = await emails.send_template(session, world.event, reminder, scheduled=True)
    # The process stopped after the first guest.
    first = await session.scalar(select(MessageDeliveries).where(MessageDeliveries.message_id == msg.id).limit(1))
    first.status, first.sent_at, first.provider_id = DeliveryStatus.sent, datetime.now(timezone.utc), "ses-before"
    await session.commit()

    resumed = await emails.resume_messages(session)
    assert resumed["started"] == 1
    await jobs.settle()
    await pipeline.drain()
    msg = await session.get(Messages, msg.id, populate_existing=True)
    assert msg.status is MessageStatus.SENT and msg.sent_count == len(world.regs)
    assert len(outbox.sent) == len(world.regs) - 1
    # The once-per-template rule still holds: no second message.
    assert await session.scalar(select(func.count()).select_from(Messages).where(Messages.template_id == reminder.id)) == 1


async def test_a_message_too_late_to_matter_is_closed_not_sent(session, world, outbox, background):
    templates = await emails.ensure_event_templates(session, world.event)
    reminder = next(t for t in templates if t.stage.value == "REMINDER")
    reminder.audience = {"who": "ALL", "roles": []}
    await session.commit()
    msg = await emails.send_template(session, world.event, reminder, scheduled=True)
    await session.execute(update(Messages).where(Messages.id == msg.id).values(created_at=datetime.now(timezone.utc) - timedelta(hours=7)))
    await session.commit()
    assert (await emails.resume_messages(session))["late"] == 1
    msg = await session.get(Messages, msg.id, populate_existing=True)
    assert msg.status is MessageStatus.SENT and msg.failed_count == len(world.regs) and outbox.sent == []


async def test_a_provider_down_for_good_is_given_up_on_and_the_admins_told(session, world, outbox, background, no_pdf, monkeypatch):
    monkeypatch.setattr(settings, "MAIL_MAX_ATTEMPTS", 2)
    a = world.regs[3]
    outbox.state["defer"] = "SES could not take it just now (ServiceUnavailable); it will be tried again"
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id])
    await deliveries.run(q["ids"])
    row = (await _rows(session, world.event.id))[a.id]
    assert row.status is DeliveryStatus.pending and row.tries == 1
    await session.execute(update(Deliveries).where(Deliveries.id == row.id).values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await session.commit()
    await deliveries.resume(session)
    await jobs.settle()
    await pipeline.drain()
    row = (await _rows(session, world.event.id))[a.id]
    assert row.status is DeliveryStatus.dead and row.error.startswith("gave up after 2 tries")
    alerts = list(await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "email_dead_letter")))
    assert len(alerts) == 1 and alerts[0].club_id == world.club.id and alerts[0].data["event_id"] == world.event.id
    # The desk sees it failed, and "retry failed" takes it again.
    log = await deliveries.log(session, world.event, kind="ticket", problem=True)
    assert [x["status"] for x in log["items"]] == ["failed"]
    retry = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id], only="failed")
    assert retry["ids"] == [row.id]


# ─── 5. No connection held while a mail renders or waits on SES ───────────────


@pytest.fixture
async def committed(_engine, monkeypatch):
    """An event of its own with six guests, committed, and workers on
    sessions of a small pool of their own; all of it removed afterwards."""
    tag = uuid.uuid4().hex[:6]
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    async with make() as s:
        owner = await _make_user(s, email=f"ci-owner-{tag}@example.com", user_type=UserTypes.club, username=f"ciowner{tag}")
        club = Clubs(slug=f"ci-club-{tag}", name="Integrity Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=f"CI {tag}", created_by_id=owner.id)
        s.add(category)
        await s.flush()
        event = Events(slug=f"ci-night-{tag}", name="Integrity Night", images=[], duration=2.0, event_datetime=START, club_id=club.id, category_id=category.id)
        s.add(event)
        await s.flush()
        users, regs = [], []
        for i in range(6):
            u = await _make_user(s, email=f"ci-{i}-{tag}@example.com", username=f"ci{i}{tag}")
            users.append(u.id)
            r = EventRegistrationsLink(event_id=event.id, user_id=u.id, full_name=f"Guest {i}", email=u.email, ticket_id=f"CI-{tag}-{i}")
            s.add(r)
            regs.append(r)
        await s.commit()
        world = SimpleNamespace(tag=tag, owner_id=owner.id, club_id=club.id, category_id=category.id, event_id=event.id, user_ids=users, reg_ids=[r.id for r in regs])

    engine = create_async_engine(settings.DATABASE_URL, pool_size=3, max_overflow=0, pool_timeout=5)
    pool_sessions = async_sessionmaker(bind=engine, **SESSION_KWARGS)

    @asynccontextmanager
    async def scope():
        async with pool_sessions() as s:
            add_loader_criteria(s)
            yield s

    monkeypatch.setattr(deliveries, "session_scope", scope)
    world.engine, world.make, world.scope = engine, make, scope
    try:
        yield world
    finally:
        await pipeline.drain(timeout=30)
        await engine.dispose()
        async with make() as s:
            messages = select(Messages.id).where(Messages.event_id == world.event_id)
            await s.execute(delete(DeliveryAttempts).where(DeliveryAttempts.delivery_id.in_(select(Deliveries.id).where(Deliveries.event_id == world.event_id))))
            await s.execute(delete(DeliveryAttempts).where(DeliveryAttempts.message_delivery_id.in_(select(MessageDeliveries.id).where(MessageDeliveries.message_id.in_(messages)))))
            await s.execute(delete(MessageDeliveries).where(MessageDeliveries.message_id.in_(messages)))
            await s.execute(delete(Messages).where(Messages.event_id == world.event_id))
            await s.execute(delete(Deliveries).where(Deliveries.event_id == world.event_id))
            await s.execute(delete(emails.EmailTemplates).where(emails.EmailTemplates.club_id == world.club_id))
            await s.execute(delete(AdminNotifications).where(AdminNotifications.club_id == world.club_id))
            await s.execute(delete(jobs.Jobs))
            await s.execute(delete(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world.event_id))
            await s.execute(delete(Events).where(Events.id == world.event_id))
            await s.execute(delete(EventCategories).where(EventCategories.id == world.category_id))
            await s.execute(delete(Clubs).where(Clubs.id == world.club_id))
            await s.execute(delete(Users).where(Users.id.in_([world.owner_id, *world.user_ids])))
            await s.commit()


async def test_no_connection_is_held_while_a_mail_renders_or_waits_on_ses(committed, outbox, no_pdf, monkeypatch):
    """Each worker reads in one short transaction, lets the connection go,
    renders and sends, then records in another. Thirty sends at once used to
    keep thirty connections idle in a transaction -- the whole pool."""
    monkeypatch.setattr(settings, "MAIL_CONCURRENCY", 1)
    held: dict = {"render": [], "send": []}
    real_render = emails.render_email

    def render(*a, **k):
        held["render"].append(committed.engine.pool.checkedout())
        return real_render(*a, **k)

    real_send = email_mod.send_email

    def send(**kwargs):
        held["send"].append(committed.engine.pool.checkedout())
        return real_send(**kwargs)

    monkeypatch.setattr(emails, "render_email", render)
    monkeypatch.setattr(email_mod, "send_email", send)
    # A message, rendered from its design for every guest...
    async with committed.make() as s:
        event = await s.get(Events, committed.event_id)
        templates = await emails.ensure_event_templates(s, event)
        reminder = next(t for t in templates if t.stage.value == "REMINDER")
        reminder.audience = {"who": "ALL", "roles": []}
        await s.commit()
        msg = await emails.send_template(s, event, reminder)
    await emails.run_message(msg.id)
    # ...and tickets, in the event's designed invite.
    async with committed.make() as s:
        event = await s.get(Events, committed.event_id)
        q = await deliveries.queue(s, event, DeliveryKind.ticket)
    await deliveries.run(q["ids"])
    assert len(outbox.sent) == 12
    assert held["send"] and set(held["send"]) == {0}
    assert held["render"] and set(held["render"]) == {0}


async def test_two_workers_on_their_own_connections_claim_each_row_once(committed, outbox, no_pdf):
    """SKIP LOCKED and the status check, under real concurrency: two runs
    over the same queue, each in transactions of its own."""
    outbox.state["delay"] = 0.03
    async with committed.make() as s:
        event = await s.get(Events, committed.event_id)
        q = await deliveries.queue(s, event, DeliveryKind.ticket)
    await asyncio.gather(deliveries.run(q["ids"]), deliveries.run(list(reversed(q["ids"]))))
    assert len(outbox.sent) == 6 and len({m["recipients"][0] for m in outbox.sent}) == 6


async def test_two_clicks_at_once_queue_each_guest_once(committed):
    """Two first sends at the same moment both found no rows and both
    inserted: the second click was a unique-key error. ON CONFLICT now."""

    async def click():
        async with committed.make() as s:
            event = await s.get(Events, committed.event_id)
            return await deliveries.queue(s, event, DeliveryKind.ticket)

    a, b = await asyncio.gather(click(), click())
    # Neither failed; between them every guest was queued once.
    assert not set(a["ids"]) & set(b["ids"]) and len(set(a["ids"]) | set(b["ids"])) == 6
    async with committed.make() as s:
        assert await s.scalar(select(func.count()).select_from(Deliveries).where(Deliveries.event_id == committed.event_id)) == 6


# ─── 6. Several in flight, one rate, nobody held up ───────────────────────────


async def test_a_message_has_several_mails_in_flight(session, world, outbox, background, monkeypatch, make_user, make_registration):
    monkeypatch.setattr(settings, "MAIL_CONCURRENCY", 4)
    for i in range(8):
        await make_registration(event=world.event, user=await make_user(email=f"extra-{i}-i@example.com"), full_name=f"Extra {i}")
    await session.commit()
    outbox.state["delay"] = 0.1
    msg = await emails.send_broadcast(session, world.event, subject="Doors at six", body="See you.", design=None, attachments=emails.EmailAttachments(), audience=EmailAudience())
    await emails.run_message(msg.id)
    assert len(outbox.sent) == 12
    # Several at once, never more than the limit. (Measured as overlap, not
    # by the clock: a wall-clock bound failed on a loaded machine.)
    assert 2 <= outbox.state["most"] <= 4


async def test_the_bucket_keeps_the_rate_and_slows_when_throttled():
    bucket = pace.Bucket(20)
    started = time.monotonic()
    for _ in range(40):
        await bucket.take()
    # Twenty at once, then twenty more at twenty a second.
    assert 0.85 <= time.monotonic() - started < 2.0
    bucket.throttled()
    assert bucket.rate == 10 and bucket.tokens == 0


async def test_a_big_reminder_does_not_hold_up_a_small_one(session, world, outbox, background, monkeypatch, make_user, make_registration):
    monkeypatch.setattr(settings, "MAIL_CONCURRENCY", 2)
    outbox.state["delay"] = 0.02
    for i in range(20):
        await make_registration(event=world.event, user=await make_user(email=f"crowd-{i}-i@example.com"), full_name=f"Crowd {i}")
    for i in range(2):
        await make_registration(event=world.event, user=await make_user(email=f"speaker-{i}-i@example.com"), full_name=f"Speaker {i}", role="speaker")
    await session.commit()
    done: dict = {}

    real = email_mod.send_email

    def send(**kwargs):
        out = real(**kwargs)
        done[kwargs["subject"]] = time.monotonic()
        return out

    monkeypatch.setattr(email_mod, "send_email", send)
    big = await emails.send_broadcast(session, world.event, subject="Big", body="All of you.", design=None, attachments=emails.EmailAttachments(), audience=EmailAudience())
    small = await emails.send_broadcast(session, world.event, subject="Small", body="Speakers.", design=None, attachments=emails.EmailAttachments(), audience=EmailAudience(who="ALL", roles=["speaker"]))
    emails.start_job(big.id)
    await asyncio.sleep(0.1)
    emails.start_job(small.id)
    await pipeline.drain()
    assert done["Small"] < done["Big"]


# ─── 7. One suppression list for the platform ─────────────────────────────────


async def test_a_hard_bounce_keeps_the_address_out_of_every_events_mail(session, world, outbox, background, no_pdf, make_club, make_event, make_user, make_registration):
    a = world.regs[0]
    q = await deliveries.queue(session, world.event, DeliveryKind.ticket, registration_ids=[a.id])
    await deliveries.run(q["ids"])
    pid = (await _rows(session, world.event.id))[a.id].provider_id
    await hooks.record(session, pid, "email", Verdict(DeliveryOutcome.bounced, "550 5.1.1 user unknown", "General"), datetime.now(timezone.utc), address=a.email.upper())
    row = await session.get(suppression.EmailSuppressions, a.email.lower())
    assert row is not None and row.reason == "bounce"

    # Another club's event, the same address in other letters.
    owner, club = await make_club(name="Elsewhere", email="elsewhere-owner@example.com")
    other = await make_event(club=club, created_by=owner, name="Elsewhere Night", event_datetime=START)
    there = await make_registration(event=other, user=await make_user(email="someone-else-i@example.com"), full_name="Ananya Again", email=a.email.upper())
    await session.commit()
    count = await emails.audience_count(session, other, EmailAudience())
    assert count == {"recipients": 0, "without_email": 0}
    msg = await emails.send_broadcast(session, other, subject="Hello", body="Hi.", design=None, attachments=emails.EmailAttachments(), audience=EmailAudience())
    assert msg.recipients == 0
    before = len(outbox.sent)
    q = await deliveries.queue(session, other, DeliveryKind.ticket, registration_ids=[there.id])
    await deliveries.run(q["ids"])
    ticket = (await _rows(session, other.id))[there.id]
    assert ticket.status is DeliveryStatus.skipped and "bounced" in ticket.error and len(outbox.sent) == before


async def test_a_complaint_suppresses_and_a_soft_bounce_does_not(session, world):
    now = datetime.now(timezone.utc)
    await hooks.record(session, "nobody-1", "email", Verdict(DeliveryOutcome.complained, "Marked as spam", "abuse"), now, address="Annoyed@Example.com")
    await hooks.record(session, "nobody-2", "email", Verdict(DeliveryOutcome.undelivered, "Mailbox full", "MailboxFull"), now, address="full@example.com")
    assert (await session.get(suppression.EmailSuppressions, "annoyed@example.com")).reason == "complaint"
    assert await session.get(suppression.EmailSuppressions, "full@example.com") is None


async def test_the_transports_lookup_reads_the_list(_engine):
    """The warning for account mail reads the list from a worker thread, on a
    synchronous connection of its own."""
    address = f"sync-{uuid.uuid4().hex[:6]}@example.com"
    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)
    async with make() as s:
        await suppression.add(s, address, suppression.BOUNCE, "550")
        await s.commit()
    try:
        assert await asyncio.to_thread(suppression.lookup_sync, [address.upper(), "fine@example.com"]) == {address: "bounce"}
    finally:
        async with make() as s:
            await s.execute(delete(suppression.EmailSuppressions).where(suppression.EmailSuppressions.email == address))
            await s.commit()


# ─── 8. After-commit mails that must go; shutdown that waits ───────────────────


def _alert(i: int = 1) -> dict:
    return {"id": i, "kind": "payment_failed", "title": f"Alert {i}", "body": "", "club_id": None, "club_name": None,
            "subscription_id": None, "data": {}, "created_at": datetime.now(timezone.utc).isoformat()}


async def test_a_mail_that_must_go_is_written_down_with_its_change(session, monkeypatch):
    from app.api.admin_alerts import service as admin_alerts

    sent: list = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", "ops@example.com")
    after_commit.run_after_commit(session, admin_alerts.deliver, _alert(7))
    await session.flush()
    [row] = list(await session.scalars(select(jobs.Jobs)))
    assert row.name == "admin_alerts.deliver" and row.payload["args"][0]["title"] == "Alert 7"
    # Due at once: the runner the commit wakes takes it, and a claim is a
    # lock, so nothing else can send it too -- no grace to wait out.
    assert row.run_at <= datetime.now(timezone.utc) and row.finished_at is None
    # A call that is not on the list stays in memory only.
    after_commit.run_after_commit(session, sent.append, "not durable")
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs)) == 1
    await session.commit()
    await after_commit.drain()
    assert len([m for m in sent if isinstance(m, dict)]) == 1 and "not durable" in sent


async def test_a_mail_cut_off_by_a_restart_is_sent_when_its_lease_runs_out(session, monkeypatch):
    """A process that took the mail and stopped (a deploy) never settles it;
    once its lease is over another runner takes it and sends it."""
    sent: list = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", "ops@example.com")
    from app.api.admin_alerts import service as admin_alerts

    admin_alerts.reset_for_tests()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    cut = jobs.Jobs(name="admin_alerts.deliver", payload={"args": [_alert(8)], "kwargs": {}}, attempts=1, max_attempts=6,
                    locked_by="api:gone:1:abc", locked_until=past, run_at=past)
    session.add(cut)
    await session.commit()
    assert await jobs.run_due(ids=[cut.id]) == 1
    assert len(sent) == 1 and sent[0]["recipients"] == ["ops@example.com"] and "Alert 8" in sent[0]["subject"]
    row = await session.get(jobs.Jobs, cut.id, populate_existing=True)
    assert row.finished_at is not None and row.attempts == 2 and row.locked_by is None
    assert await jobs.run_due(ids=[cut.id]) == 0  # sent once


async def test_a_billing_mail_the_provider_refuses_is_retried_then_given_up_on(session, monkeypatch):
    """The strict twin raises, so an outage is retried with a growing wait,
    and after the last try the job is dead and the admins are told."""
    from app.api.subscriptions import notices

    tries: list = []

    def refuse(**kw):
        tries.append(kw)
        raise RuntimeError("SES is down")

    monkeypatch.setattr(email_mod, "send_email", refuse)
    after_commit.run_after_commit(session, notices._send, "owner@example.com", "Your plan", ["Hello"], None, None)
    await session.commit()
    await after_commit.drain()
    [row] = list(await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == "mail.billing_notice")))
    assert len(tries) == 1 and row.attempts == 1 and not row.dead and "SES is down" in row.last_error
    waits = [(row.run_at - datetime.now(timezone.utc)).total_seconds()]
    for _ in range(row.max_attempts - 1):
        await session.execute(update(jobs.Jobs).where(jobs.Jobs.id == row.id).values(run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()
        await jobs.run_due(ids=[row.id])
        row = await session.get(jobs.Jobs, row.id, populate_existing=True)
        waits.append((row.run_at - datetime.now(timezone.utc)).total_seconds())
    assert len([t for t in tries if t["recipients"] == ["owner@example.com"]]) == row.max_attempts and row.dead is True
    assert 10 <= waits[0] <= 35 and waits[1] > 25 and waits[2] > 55  # 30 s, then about 1, 2 minutes
    from app.api.admin_alerts.models import AdminNotifications

    [alert] = list(await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "job_dead")))
    assert alert.data["name"] == "mail.billing_notice" and alert.data["job_id"] == row.id


async def test_a_mail_that_went_is_crossed_off(_engine, monkeypatch):
    from app.api.admin_alerts import service as admin_alerts

    make = async_sessionmaker(bind=_engine, **SESSION_KWARGS)

    @asynccontextmanager
    async def own():
        async with make() as s:
            yield s

    # Committed rows, so the runner works on sessions of its own too.
    monkeypatch.setattr(jobs, "session_scope", own)
    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", "ops@example.com")
    sent: list = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    admin_alerts.reset_for_tests()
    async with make() as s:
        after_commit.run_after_commit(s, admin_alerts.deliver, _alert(9))
        await s.commit()
    await after_commit.drain()
    try:
        assert len(sent) == 1
        async with make() as s:
            [row] = list(await s.scalars(select(jobs.Jobs).where(jobs.Jobs.name == "admin_alerts.deliver")))
            assert row.finished_at is not None and not row.dead
    finally:
        async with make() as s:
            await s.execute(delete(jobs.Jobs))
            await s.commit()


async def test_the_old_outbox_rows_are_moved_into_jobs_and_sent(session, monkeypatch):
    """A row a process from before the change left in after_commit_jobs is
    adopted by the jobs' maintenance and sent; one it does not know stays."""
    sent: list = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAILS", "ops@example.com")
    # The model is gone, so create_all no longer makes it; a database made
    # before may still have one (dropped here inside the test's transaction).
    await session.execute(text("DROP TABLE IF EXISTS after_commit_jobs"))
    await session.execute(text("""
        CREATE TABLE after_commit_jobs (id uuid PRIMARY KEY, job varchar(200) NOT NULL, payload json NOT NULL,
            attempts integer NOT NULL, next_attempt_at timestamptz NOT NULL, last_error varchar(400), dead boolean NOT NULL,
            created_at timestamptz NOT NULL)
    """))
    past = datetime.now(timezone.utc) - timedelta(minutes=3)
    for job, payload in (
        ("app.api.admin_alerts.service:deliver", {"args": [_alert(11)], "kwargs": {}}),
        ("app.no_such_module:gone", {"args": [], "kwargs": {}}),
    ):
        await session.execute(
            text("INSERT INTO after_commit_jobs VALUES (:id, :job, CAST(:payload AS json), 2, :at, NULL, false, :at)"),
            {"id": uuid.uuid4(), "job": job, "payload": __import__("json").dumps(payload), "at": past},
        )
    await session.commit()
    assert await after_commit.adopt_legacy(session) == 1
    assert await session.scalar(text("SELECT job FROM after_commit_jobs")) == "app.no_such_module:gone"
    [row] = list(await session.scalars(select(jobs.Jobs)))
    assert row.name == "admin_alerts.deliver" and row.attempts == 2
    assert await jobs.run_due(ids=[row.id]) == 1
    assert len(sent) == 1 and "Alert 11" in sent[0]["subject"]


def test_every_durable_call_has_its_job():
    handlers = jobs.handlers()
    for name, (job, runs, _what) in after_commit.DURABLE.items():
        assert callable(after_commit.resolve(name)), name
        assert callable(after_commit.resolve(runs)), runs
        assert job in handlers, job


async def test_shutdown_waits_for_the_loops_the_mail_jobs_and_the_after_commit_mails(monkeypatch):
    import doth

    stopped: list = []

    async def loop(name):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # a loop finishing what it was doing
            stopped.append(name)
            raise

    async def job():
        await asyncio.sleep(0.1)
        stopped.append("job")

    drained: list = []

    async def drain(timeout=None):
        drained.append(timeout)
        return True

    monkeypatch.setattr(after_commit, "drain", drain)
    loops = [asyncio.create_task(loop(n)) for n in ("sweep", "billing")]
    pipeline.spawn(("message", "x"), job)
    await asyncio.sleep(0)
    try:
        await doth.shutdown(loops, budget=3.0)
        assert all(t.done() for t in loops)
        assert sorted(stopped) == ["billing", "job", "sweep"]
        assert drained and drained[0] is not None and drained[0] <= 3.0
        # Once shutting down, no new rows are claimed.
        assert pipeline.stopping()
    finally:
        pipeline.reset_for_tests()


async def test_the_lifespan_ends_with_the_shutdown(monkeypatch):
    import gc

    import doth
    from app.api.badges import seed
    from app.db import core

    async def nothing(*_a, **_k):
        return None

    async def forever():
        await asyncio.sleep(3600)

    loops = (
        ("app.api.emails.service", "sweep_loop"), ("app.api.closeout.service", "sweep_loop"), ("app.api.live.service", "sweep_loop"),
        ("app.api.subscriptions.sweep", "sweep_loop"), ("app.api.discover.counters", "check_loop"), ("app.api.club_domains.sweep", "sweep_loop"),
        ("app.api.store.sweep", "sweep_loop"), ("app.api.payments.sweep", "sweep_loop"), ("app.api.auth.retention", "sweep_loop"),
        ("app.api.ledger.jobs", "daily_loop"),
        ("app.api.search.sync", "loop"), ("app.api.events.doc_cache_sweep", "sweep_loop"),
        ("app.api.club_whatsapp.sweep", "sweep_loop"),
    )
    for module, name in loops:
        monkeypatch.setattr(__import__(module, fromlist=[name]), name, forever)
    monkeypatch.setattr(seed, "seed_badges", nothing)
    monkeypatch.setattr(core, "check_connection_budget", nothing)
    calls: list = []
    runners: list = []

    async def shutdown(loops, budget=doth.SHUTDOWN_BUDGET, runner=None):
        calls.append(loops)
        runners.append(runner)
        for t in loops:
            t.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        if runner is not None:
            await runner.stop(timeout=1.0)

    monkeypatch.setattr(doth, "shutdown", shutdown)
    before = jobs.local()
    try:
        async with doth.lifespan(doth.application):
            # Without APP_JOBS_WORKER this process runs its own jobs.
            assert isinstance(jobs.local(), jobs.Runner) and jobs.local().kind == "api"
    finally:
        gc.unfreeze()
        jobs.use(before)
    assert len(calls) == 1 and len(calls[0]) == len(loops) and all(t.done() for t in calls[0])
    assert isinstance(runners[0], jobs.Runner)


async def test_the_lifespan_leaves_jobs_and_sweeps_to_the_worker_when_told(monkeypatch):
    """APP_JOBS_WORKER: the API runs no job runner. APP_SWEEPS_IN_WORKER: no sweeps."""
    import gc

    import doth
    from app.api.badges import seed
    from app.db import core

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(seed, "seed_badges", nothing)
    monkeypatch.setattr(core, "check_connection_budget", nothing)
    monkeypatch.setattr(settings, "JOBS_WORKER", True)
    monkeypatch.setattr(settings, "SWEEPS_IN_WORKER", True)
    monkeypatch.setattr(doth, "start_loops", lambda: (_ for _ in ()).throw(AssertionError("the API started the sweeps")))
    seen: list = []

    async def shutdown(loops, budget=doth.SHUTDOWN_BUDGET, runner=None):
        seen.append((loops, runner))

    monkeypatch.setattr(doth, "shutdown", shutdown)
    before = jobs.use(None)
    try:
        async with doth.lifespan(doth.application):
            assert jobs.local() is None
    finally:
        gc.unfreeze()
        jobs.use(before)
    assert seen == [([], None)]


# ─── 9. An audience counted by the database ──────────────────────────────────


async def test_counting_an_audience_does_not_load_it(session, world):
    from sqlalchemy import event as sa_event

    seen: list = []
    engine = (await session.connection()).sync_connection.engine

    def capture(conn, cursor, statement, parameters, *_rest):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", capture)
    try:
        count = await emails.audience_count(session, world.event, EmailAudience())
    finally:
        sa_event.remove(engine, "before_cursor_execute", capture)
    assert count == {"recipients": 4, "without_email": 0}
    assert len(seen) == 1 and "count(*)" in seen[0].lower() and "event_registrations_link.full_name" not in seen[0]
