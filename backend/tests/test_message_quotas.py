"""Monthly email and WhatsApp quotas per plan, and paid overage.

What is checked: every message handed to the provider counts once (and a
skipped, failed or club-number one never); Free holds back what is not a
ticket, certificate or payment message past its limit, a paying plan never
does; the 80% and 100% warnings go once each; a closed month's extra goes on
the club's Razorpay subscription as one add-on, never two, retried when
Razorpay says no and found again when it never answered; complimentary and
Free clubs get a statement and no charge; the dashboards' answers.

Razorpay is LIVE in production: every call here goes to FakeRazorpay
(tests/test_club_billing.py). Meta is a stand-in (tests/test_whatsapp.py),
mail a list.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from razorpay.errors import BadRequestError
from sqlalchemy import func, select

from app.api.deliveries import pipeline
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.message_quota import billing, service as quota
from app.api.message_quota.models import MessageStatements, MessageUsage
from app.config import settings
from app.core import jobs, whatsapp
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header
from tests.test_billing_alerts import _alerts, mail  # noqa: F401  (mail is a fixture)
from tests.test_club_billing import FakeRazorpay, make_plan, razorpay  # noqa: F401  (razorpay is a fixture)
from tests.test_whatsapp import meta  # noqa: F401  (meta is a fixture)

API = "/api/v1"
DAY = timedelta(days=1)
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _last_month():
    """An instant in the month before this one (in India)."""
    start, _ = quota.bounds(quota.period_of(_now()))
    return start - DAY


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    from app.api.subscriptions import gates

    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "MESSAGE_OVERAGE_BILLING", True)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture(autouse=True)
async def _sends_finished(session):
    pipeline.reset_for_tests()
    yield
    await pipeline.drain(timeout=30)


@pytest.fixture
def outbox(monkeypatch):
    """Every mail handed to the provider; `refuse` makes it say no."""
    sent: list[dict] = []
    state = {"refuse": None}

    def send(**kwargs):
        if state["refuse"]:
            raise RuntimeError(state["refuse"])
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    monkeypatch.setattr(email_mod, "send_email", send)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", send)
    return {"sent": sent, "state": state}


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)
    monkeypatch.setattr(quota, "session_scope", scope)


@pytest.fixture
async def tiers(session):
    """Free (3 emails, no WhatsApp), Growth (3 emails, 2 WhatsApp, Razorpay
    billed) and Enterprise (unlimited, set per club): small limits, so a
    handful of guests goes past them."""
    from types import SimpleNamespace

    free = await make_plan(session, key="free", limits={"emails_per_month": 3, "whatsapp_per_month": 0})
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000,
                             limits={"emails_per_month": 3, "whatsapp_per_month": 2}, features=("tickets",))
    pro = await make_plan(session, key="pro", monthly=149900, yearly=1499000,
                          limits={"emails_per_month": 20000, "whatsapp_per_month": 1000}, features=("tickets",))
    enterprise = await make_plan(session, key="enterprise", contact_sales=True, features=("tickets",))
    await session.commit()
    return SimpleNamespace(free=free, growth=growth, pro=pro, enterprise=enterprise)


@pytest.fixture
async def world(client, session, tiers, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Quota Club", email="quota-owner@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night", event_datetime=_now() + 10 * DAY, duration=3.0,
        location_name="Innovation Hall",
    )
    regs = []
    for i, (name, email) in enumerate((
        ("Ananya Iyer", "q-ananya@example.com"), ("Bala Krishnan", "q-bala@example.com"),
        ("Chitra Nair", "q-chitra@example.com"), ("Dev Menon", "q-dev@example.com"), ("Esha Rao", ""),
    )):
        u = await make_user(email=email or f"q-noemail-{i}@example.com")
        regs.append(await make_registration(event=event, user=u, full_name=name, email=email, phone="98765 4321" + str(i),
                                            whatsapp_opt_in=True, whatsapp_opt_in_at=_now()))
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = ticket_starter("classic")
    r = await client.put(
        f"{API}/events/{event.id}/designs/ticket/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {"owner": owner, "club": club, "event": event, "regs": regs, "h": headers, "tiers": tiers}


async def _paying(session, club_id, plan_id, sub_id="sub_quota_live"):
    from app.api.subscriptions.models import ClubSubscriptions

    now = _now()
    session.add(ClubSubscriptions(club_id=club_id, plan_id=plan_id, status="active", razorpay_subscription_id=sub_id,
                                  authenticated_at=now - 40 * DAY, activated_at=now - 40 * DAY,
                                  current_period_end=now + 20 * DAY, mrr_paise=69900, price_paise=69900))
    await session.commit()


async def _usage(session, club_id, channel="email", period=None) -> MessageUsage | None:
    return await session.scalar(
        select(MessageUsage)
        .where(MessageUsage.club_id == club_id, MessageUsage.channel == channel,
               MessageUsage.period == (period or quota.period_of()))
        .execution_options(populate_existing=True)
    )


async def _send_tickets(session, world, *, registration_ids=None, force=False):
    result = await deliveries.queue(session, world["event"], DeliveryKind.ticket, registration_ids=registration_ids,
                                    force=force, email_only=True)
    for did in result["ids"]:
        await deliveries.deliver(session, did)
    return result


async def _broadcast(client, world, subject="Doors at six"):
    r = await client.post(
        f"{API}/events/{world['event'].id}/messages",
        json={"subject": subject, "body": "Parking is behind the hall.", "audience": {"who": "ALL", "roles": []}},
        headers=world["h"],
    )
    assert r.status_code == 200, r.text
    await jobs.settle()
    await pipeline.drain(timeout=30)
    return r.json()


# ─── Counting ────────────────────────────────────────────────────────────


async def test_each_message_handed_over_counts_once_and_skips_and_failures_never(session, world, outbox, background, monkeypatch):
    await _paying(session, world["club"].id, world["tiers"].pro.id)
    await _send_tickets(session, world)
    row = await _usage(session, world["club"].id)
    # Four had an address; the fifth was skipped (no email) and is not counted.
    assert row.used == 4 and row.kinds == {"ticket": 4}
    assert len(outbox["sent"]) == 4

    # A send the provider refused is not counted.
    outbox["state"]["refuse"] = "SES said no"
    await _send_tickets(session, world, registration_ids=[world["regs"][0].id], force=True)
    assert (await _usage(session, world["club"].id)).used == 4
    failed = await session.scalar(select(Deliveries).where(Deliveries.registration_id == world["regs"][0].id)
                                  .execution_options(populate_existing=True))
    assert failed.status is DeliveryStatus.failed

    # Sent again a while later, it is a second message: counted again. (Its
    # ref is the provider's own key, the row and the second it was queued.)
    outbox["state"]["refuse"] = None
    later = _now() + timedelta(minutes=2)
    monkeypatch.setattr(pipeline, "now", lambda: later)
    await _send_tickets(session, world, registration_ids=[world["regs"][0].id], force=True)
    assert (await _usage(session, world["club"].id)).used == 5

    # The same message recorded twice (a retried record, a job run again) counts once.
    d = await session.scalar(select(Deliveries).where(Deliveries.registration_id == world["regs"][0].id)
                             .execution_options(populate_existing=True))
    ref = f"delivery:{d.id}:{int(d.queued_at.timestamp())}"
    again = await quota.record_message(world["club"].id, "email", "ticket", session=session, ref=ref)
    assert again.counted is False
    assert (await _usage(session, world["club"].id)).used == 5


async def test_a_message_through_the_clubs_own_number_is_never_counted(session, world, meta, outbox, monkeypatch, background):
    assert await quota.record_message(world["club"].id, "whatsapp", "ticket", via="club", session=session) is None
    with pytest.raises(ValueError):
        await quota.record_message(world["club"].id, "whatsapp", "ticket", via="someone", session=session)

    # The pipeline counts by what the sender says it went through.
    async def from_the_clubs_number(db, ready):
        return {"MessageId": "wamid.CLUB1", "Provider": "whatsapp", "Address": "+919876543210", "Via": "club"}, 0

    monkeypatch.setattr(deliveries, "_send_whatsapp", from_the_clubs_number)
    result = await deliveries.queue(session, world["event"], DeliveryKind.ticket, registration_ids=[world["regs"][1].id])
    for did in result["ids"]:
        await deliveries.deliver(session, did)
    wa = await session.scalar(select(Deliveries).where(
        Deliveries.registration_id == world["regs"][1].id, Deliveries.channel == DeliveryChannel.whatsapp))
    assert wa.status is DeliveryStatus.sent
    assert await _usage(session, world["club"].id, "whatsapp") is None


async def test_record_message_is_idempotent_by_ref_and_counts_by_kind(session, world):
    club_id = world["club"].id
    first = await quota.record_message(club_id, "email", "announcement", session=session, ref="message:abc")
    assert first.counted and first.used == 1 and first.limit == 3
    assert (await quota.record_message(club_id, "email", "announcement", session=session, ref="message:abc")).counted is False
    await quota.record_message(club_id, "email", "certificate", session=session)
    row = await _usage(session, club_id)
    assert row.used == 2 and row.kinds == {"announcement": 1, "certificate": 1}
    assert (row.quota, row.billing, row.plan_key, row.rate_paise) == (3, "none", "free", 10)


# ─── Holding back on Free ────────────────────────────────────────────────


async def test_free_holds_back_announcements_past_its_limit_but_never_tickets(client, session, world, outbox, background, monkeypatch):
    # One worker: several at once each find room for the last message
    # (quota.held_back says so), and this test counts exactly.
    monkeypatch.setattr(settings, "MAIL_CONCURRENCY", 1)
    msg = await _broadcast(client, world)
    detail = (await client.get(f"{API}/events/{world['event'].id}/messages/{msg['id']}", headers=world["h"])).json()
    rows = {d["name"]: d for d in detail["deliveries"]}
    statuses = sorted(d["status"] for d in rows.values())
    assert statuses == ["sent", "sent", "sent", "skipped", "skipped"]
    held = [d for d in rows.values() if d["status"] == "skipped" and d["error"] != "no email address"]
    assert len(held) == 1
    assert held[0]["error"] == (
        "Not sent: this month's 3 emails on Free are used up. Upgrade to send more; tickets and certificates still go."
    )
    row = await _usage(session, world["club"].id)
    assert (row.used, row.blocked, row.kinds) == (3, 1, {"announcement": 3})

    # Tickets are never held back, and still count.
    await _send_tickets(session, world)
    row = await _usage(session, world["club"].id)
    assert (row.used, row.kinds) == (7, {"announcement": 3, "ticket": 4})

    usage = (await client.get(f"{API}/clubs/messages/usage", headers=world["h"])).json()
    email = usage["channels"]["email"]
    assert (email["used"], email["limit"], email["extra"], email["blocked"], email["state"]) == (7, 3, 4, 1, "blocked")
    # Free has no way to pay: nothing is billed.
    assert email["extra_paise"] == 0 and usage["estimated_extra_paise"] == 0
    assert usage["billing"] == "none" and usage["blocks_past_limit"] is True
    assert usage["upgrade"]["plan_key"] == "pro"


async def test_free_holds_back_whatsapp_reminders_but_tickets_go(session, world, meta, outbox, background):
    reg = world["regs"][0]
    session.add(Deliveries(event_id=world["event"].id, registration_id=reg.id, kind=DeliveryKind.reminder,
                           channel=DeliveryChannel.whatsapp, status=DeliveryStatus.pending, attempts=0, tries=0,
                           design_version=0, queued_at=_now()))
    await session.commit()
    reminder = await session.scalar(select(Deliveries).where(Deliveries.kind == DeliveryKind.reminder))
    await deliveries.deliver(session, reminder.id)
    reminder = await session.get(Deliveries, reminder.id, populate_existing=True)
    assert reminder.status is DeliveryStatus.skipped
    assert reminder.error == "Not sent: WhatsApp reminders aren't included on Free. Upgrade to send them."
    assert meta["messages"] == []

    result = await deliveries.queue(session, world["event"], DeliveryKind.ticket, registration_ids=[reg.id])
    for did in result["ids"]:
        await deliveries.deliver(session, did)
    assert [m["template"]["name"] for m in meta["messages"]] == ["ticket_confirmed"]
    wa = await _usage(session, world["club"].id, "whatsapp")
    assert (wa.used, wa.blocked, wa.kinds) == (1, 1, {"ticket": 1})


async def test_a_paying_club_is_never_held_back_and_its_extra_accrues(client, session, world, outbox, background):
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    msg = await _broadcast(client, world)
    assert msg["recipients"] == 5
    # All four with an address, past the limit of three: nothing held back.
    assert len([m for m in outbox["sent"] if m["subject"] == "Doors at six"]) == 4
    usage = (await client.get(f"{API}/clubs/messages/usage", headers=world["h"])).json()
    email = usage["channels"]["email"]
    assert (email["used"], email["limit"], email["extra"], email["blocked"], email["state"]) == (4, 3, 1, 0, "over")
    assert (email["rate_paise"], email["extra_paise"], email["percent"]) == (10, 10, 133)
    assert usage["billing"] == "razorpay" and usage["blocks_past_limit"] is False
    assert usage["estimated_extra_paise"] == 10
    assert usage["upgrade"]["plan_key"] == "pro"


async def test_a_send_outside_the_pipeline_is_held_back_and_counted_through_whatsapp(session, world, meta, background):
    t = whatsapp.event_update(name="Ananya", club="Quota Club", event="Build Night", update="Doors at six")
    with pytest.raises(whatsapp.Undeliverable) as refused:
        await whatsapp.send("9876543210", t, meter={"club_id": world["club"].id, "kind": "update"})
    assert "aren't included on Free" in refused.value.reason
    assert meta["messages"] == []

    await _paying(session, world["club"].id, world["tiers"].growth.id)
    out = await whatsapp.send("9876543210", t, meter={"club_id": world["club"].id, "kind": "update", "ref": "update:1"})
    assert out["Via"] == "platform"
    await whatsapp.send("9876543210", t, meter={"club_id": world["club"].id, "kind": "update", "ref": "update:1"})
    wa = await _usage(session, world["club"].id, "whatsapp")
    assert (wa.used, wa.kinds) == (1, {"update": 1})


async def test_survey_mails_count_and_are_held_back_past_free(session, world, outbox):
    from app.api.live import service as live
    from app.api.live.models import FeedbackSends

    survey = await live.survey_of(session, world["event"])
    regs = [r for r in world["regs"] if r.email]
    for r in regs:
        session.add(FeedbackSends(survey_id=survey.id, registration_id=r.id))
    await session.commit()
    assert [await live.mail_survey(survey.id, r.id) for r in regs] == [True, True, True, False]
    held = await session.scalar(select(FeedbackSends).where(FeedbackSends.registration_id == regs[3].id)
                                .execution_options(populate_existing=True))
    assert held.mailed_at is None and held.skipped_reason.startswith("Not sent: this month's 3 emails on Free are used up")
    row = await _usage(session, world["club"].id)
    assert (row.used, row.blocked, row.kinds) == (3, 1, {"survey": 3})
    # A job run again finds it mailed: nothing sent, nothing counted.
    assert await live.mail_survey(survey.id, regs[0].id) is False
    assert (await _usage(session, world["club"].id)).used == 3


# ─── The warnings ────────────────────────────────────────────────────────


async def test_the_80_and_100_percent_warnings_go_once_each(client, session, world, mail):  # noqa: F811
    from app.api.subscriptions.models import PlanLimits

    limits = await session.scalar(select(PlanLimits).where(PlanLimits.plan_id == world["tiers"].growth.id))
    limits.emails_per_month = 5
    await session.commit()
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    club_id = world["club"].id
    for _ in range(3):
        await quota.record_message(club_id, "email", "announcement", session=session)
    await session.commit()
    assert (await client.get(f"{API}/clubs/alerts", headers=world["h"])).json()["unread"] == 0

    await quota.record_message(club_id, "email", "announcement", session=session)  # 4 of 5: 80%
    await session.commit()
    await quota.record_message(club_id, "email", "announcement", session=session)  # 5 of 5: 100%
    await session.commit()
    for _ in range(3):  # past it: nothing more
        await quota.record_message(club_id, "email", "announcement", session=session)
    await session.commit()
    await mail.drain()

    bell = (await client.get(f"{API}/clubs/alerts", headers=world["h"])).json()
    assert bell["unread"] == 2
    assert [a["kind"] for a in bell["items"]] == ["message_quota_100", "message_quota_80"]
    month = quota.label(quota.period_of())
    assert bell["items"][0]["title"] == f"You've used all 5 emails for {month}"
    assert bell["items"][1]["title"] == f"You've used 80% of your emails for {month}"
    assert "each extra email is ₹0.10, added to your next bill" in bell["items"][1]["body"]
    assert bell["items"][0]["link"] == "/billing"
    mails = mail.to("quota-owner@example.com")
    assert [m["subject"] for m in mails] == [f"You've used 80% of your emails for {month}", f"You've used all 5 emails for {month}"]
    row = await _usage(session, club_id)
    assert row.warned_80_at is not None and row.warned_100_at is not None

    marked = await client.post(f"{API}/clubs/alerts/read", json={}, headers=world["h"])
    assert marked.json() == {"marked": 2}
    assert (await client.get(f"{API}/clubs/alerts", headers=world["h"])).json()["unread"] == 0


async def test_free_is_warned_that_announcements_stop(session, world, mail):  # noqa: F811
    club_id = world["club"].id
    for _ in range(3):
        await quota.record_message(club_id, "email", "ticket", session=session)
    await session.commit()
    await mail.drain()
    [warning] = [m for m in mail.to("quota-owner@example.com") if "all 3 emails" in m["subject"]]
    assert "announcements, campaigns and reminders stop until next month" in warning["text"]


# ─── Overage on the next invoice ─────────────────────────────────────────


async def _over_last_month(session, world, plan, *, emails=5, whatsapps=4):
    """Last month: past Growth's 3 emails and 2 WhatsApp."""
    at = _last_month()
    for _ in range(emails):
        await quota.record_message(world["club"].id, "email", "announcement", session=session, at=at)
    for _ in range(whatsapps):
        await quota.record_message(world["club"].id, "whatsapp", "ticket", session=session, at=at)
    await session.commit()
    return quota.period_of(at)


def _addons(fake, *, made=None, fail=None):
    """createAddon: answer with an id (or raise `fail(n)` for call n)."""
    made = made if made is not None else []

    def create(sub_id, data):
        n = len(fake.of("subscription", "createAddon")) - 1  # this call is recorded already
        error = fail(n) if fail else None
        if error == "made-then-timeout":
            made.append({"id": f"ao_{n}", "subscription_id": sub_id, "item": data["item"]})
            from app.api.payments.rzp import RazorpayTimeout

            raise RazorpayTimeout("Razorpay did not answer createAddon within 10 s")
        if error is not None:
            raise error
        addon = {"id": f"ao_{n}", "entity": "addon", "subscription_id": sub_id, "item": data["item"], "quantity": data["quantity"]}
        made.append(addon)
        return addon

    fake.handlers[("subscription", "createAddon")] = create
    fake.handlers[("addon", "all")] = lambda data: {"entity": "collection", "count": len(made), "items": list(made)}
    return made


async def _statement(session, club_id) -> MessageStatements:
    return await session.scalar(
        select(MessageStatements).where(MessageStatements.club_id == club_id).execution_options(populate_existing=True)
    )


async def test_a_months_extra_goes_on_the_next_invoice_once(client, session, world, razorpay):  # noqa: F811
    from app.api.audit.models import AdminAuditLogs

    await _paying(session, world["club"].id, world["tiers"].growth.id)
    period = await _over_last_month(session, world, world["tiers"].growth)
    made = _addons(razorpay)

    done = await billing.run(session)
    assert done == {"message_statements": 1, "overage_charged": 1, "overage_failed": 0}
    st = await _statement(session, world["club"].id)
    assert st.period == period
    assert (st.email_used, st.email_limit, st.email_extra, st.whatsapp_used, st.whatsapp_limit, st.whatsapp_extra) == (5, 3, 2, 4, 2, 2)
    # 2 × ₹0.10 + 2 × ₹1.50: the WhatsApp extra at the plan's price (pricing_adjust).
    assert (st.email_rate_paise, st.whatsapp_rate_paise) == (10, 150)
    assert st.amount_paise == 320 and st.status == "charged" and st.razorpay_addon_id == "ao_0"
    [(sub_id, payload)] = razorpay.of("subscription", "createAddon")
    assert sub_id == "sub_quota_live"
    assert payload["quantity"] == 1 and payload["item"]["amount"] == 320 and payload["item"]["currency"] == "INR"
    assert billing.reference(st) in payload["item"]["description"]
    assert len(made) == 1

    # The sweep again, and again: never a second add-on.
    await billing.run(session)
    await billing.run(session, _now() + 3 * DAY)
    assert len(razorpay.of("subscription", "createAddon")) == 1
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "messages.overage_charged"))
    assert audit.details["addon_id"] == "ao_0" and audit.details["amount_paise"] == 320
    assert [a.kind for a in await _alerts(session, "message_overage_charged")] == ["message_overage_charged"]

    history = (await client.get(f"{API}/clubs/messages/history", headers=world["h"])).json()["items"]
    assert [(h["period"], h["status"], h["amount_paise"]) for h in history] == [(quota.key(period), "charged", 320)]


async def test_a_refused_addon_is_tried_again_later_and_charged_once(session, world, razorpay):  # noqa: F811
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    _addons(razorpay, fail=lambda n: BadRequestError("The id provided does not exist") if n == 0 else None)
    now = _now()

    assert (await billing.run(session, now))["overage_failed"] == 1
    st = await _statement(session, world["club"].id)
    assert st.status == "failed" and st.attempts == 1 and st.next_attempt_at > now
    assert "Razorpay refused the add-on" in st.last_error
    assert len(await _alerts(session, "message_overage_failed")) == 1

    # Not due yet: nothing asked.
    await billing.run(session, now + timedelta(minutes=5))
    assert len(razorpay.of("subscription", "createAddon")) == 1
    # Due: asked again, and charged once.
    await billing.run(session, now + 2 * timedelta(hours=1))
    st = await _statement(session, world["club"].id)
    assert st.status == "charged" and st.razorpay_addon_id == "ao_1" and st.attempts == 2
    assert len(razorpay.of("subscription", "createAddon")) == 2


async def test_an_addon_razorpay_made_without_answering_is_found_not_made_again(session, world, razorpay):  # noqa: F811
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    made = _addons(razorpay, fail=lambda n: "made-then-timeout" if n == 0 else None)
    now = _now()

    await billing.run(session, now)
    st = await _statement(session, world["club"].id)
    assert st.status == "unconfirmed" and st.razorpay_addon_id is None
    # Next run: Razorpay's add-ons are searched first; it is there, so it is the charge.
    await billing.run(session, now + timedelta(minutes=15))
    st = await _statement(session, world["club"].id)
    assert st.status == "charged" and st.razorpay_addon_id == "ao_0"
    assert len(razorpay.of("subscription", "createAddon")) == 1 and len(made) == 1


async def test_an_addon_never_made_is_asked_for_again_after_checking(session, world, razorpay):  # noqa: F811
    from app.api.payments.rzp import RazorpayTimeout

    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    made = _addons(razorpay, fail=lambda n: RazorpayTimeout("no answer") if n == 0 else None)
    now = _now()
    await billing.run(session, now)
    assert (await _statement(session, world["club"].id)).status == "unconfirmed"
    await billing.run(session, now + timedelta(minutes=15))
    st = await _statement(session, world["club"].id)
    assert st.status == "charged" and st.razorpay_addon_id == "ao_1"
    assert razorpay.of("addon", "all") and len(made) == 1


async def test_a_charge_left_half_way_by_a_stopped_sweep_is_found_out(session, world, razorpay):  # noqa: F811
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    made = _addons(razorpay)
    await billing.close_months(session)
    st = await _statement(session, world["club"].id)
    # A sweep claimed it, Razorpay made the add-on, and the sweep died before writing it down.
    st.status, st.charging_since, st.attempts, st.razorpay_subscription_id = "charging", _now() - timedelta(hours=1), 1, "sub_quota_live"
    await session.commit()
    made.append({"id": "ao_lost", "subscription_id": "sub_quota_live",
                 "item": {"description": f"2 extra emails {billing.reference(st)}"}})

    await billing.run(session)
    st = await _statement(session, world["club"].id)
    assert st.status == "charged" and st.razorpay_addon_id == "ao_lost"
    assert razorpay.of("subscription", "createAddon") == []


async def test_given_up_after_its_tries_then_an_admin_can_retry(client, session, world, razorpay, make_admin, mint_token):  # noqa: F811
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    state = {"refuse": True}
    _addons(razorpay, fail=lambda n: BadRequestError("Subscription is not active") if state["refuse"] else None)
    when = _now()
    for _ in range(billing.MAX_ATTEMPTS):
        await billing.run(session, when)
        when += timedelta(days=2)
    st = await _statement(session, world["club"].id)
    assert st.status == "gave_up" and st.attempts == billing.MAX_ATTEMPTS
    await billing.run(session, when + 5 * DAY)
    assert len(razorpay.of("subscription", "createAddon")) == billing.MAX_ATTEMPTS

    admin = auth_header(await mint_token(await make_admin(email="quota-admin@example.com")))
    state["refuse"] = False
    r = await client.post(f"{API}/admin/messages/statements/{st.id}/retry", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "charged"
    # Charged now: a second retry is refused.
    again = await client.post(f"{API}/admin/messages/statements/{st.id}/retry", headers=admin)
    assert again.status_code == 409


async def test_a_club_whose_subscription_ended_is_left_to_invoice_by_hand(session, world, razorpay):  # noqa: F811
    from app.api.subscriptions.models import ClubSubscriptions

    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    row = await session.scalar(select(ClubSubscriptions).where(ClubSubscriptions.club_id == world["club"].id))
    row.status = "cancelled"
    row.current_period_end = _now() - DAY
    await session.commit()
    _addons(razorpay)
    await billing.run(session)
    st = await _statement(session, world["club"].id)
    # Sent under Growth (Razorpay), so billed at Growth's limits -- but nothing left to add it to.
    assert (st.billing, st.amount_paise, st.status) == ("razorpay", 320, "manual")
    assert razorpay.of("subscription", "createAddon") == []
    assert len(await _alerts(session, "message_overage_manual")) == 1


async def test_a_complimentary_club_gets_a_statement_and_no_charge(session, world, razorpay):  # noqa: F811
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import BillingCycle

    await subs.grant_complimentary(session, club_id=world["club"].id, plan=world["tiers"].growth, cycle=BillingCycle.monthly,
                                   reason="Fest partner", ends_at=None, granted_by=None)
    await session.commit()
    await _over_last_month(session, world, world["tiers"].growth)
    _addons(razorpay)
    done = await billing.run(session)
    assert done["overage_charged"] == 0
    st = await _statement(session, world["club"].id)
    assert (st.billing, st.amount_paise, st.status) == ("manual", 320, "manual")
    assert razorpay.of("subscription", "createAddon") == []
    [alert] = await _alerts(session, "message_overage_manual")
    assert "invoice ₹3.20 by hand" in alert.title


async def test_free_past_its_limit_is_not_billable_and_a_tiny_extra_is_waived(session, world, razorpay, make_club):  # noqa: F811
    at = _last_month()
    for _ in range(5):
        await quota.record_message(world["club"].id, "email", "ticket", session=session, at=at)
    _, other = await make_club(name="Tiny Extra", email="tiny@example.com")
    await _paying(session, other.id, world["tiers"].growth.id, sub_id="sub_tiny")
    for _ in range(4):  # one extra email: ₹0.10, under Razorpay's ₹1
        await quota.record_message(other.id, "email", "announcement", session=session, at=at)
    await session.commit()
    _addons(razorpay)
    await billing.run(session)
    free = await _statement(session, world["club"].id)
    assert (free.billing, free.email_extra, free.amount_paise, free.status) == ("none", 2, 20, "not_billable")
    tiny = await _statement(session, other.id)
    assert (tiny.amount_paise, tiny.status) == (10, "waived")
    assert razorpay.of("subscription", "createAddon") == []


async def test_nothing_is_charged_while_overage_billing_is_switched_off(session, world, razorpay, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "MESSAGE_OVERAGE_BILLING", False)
    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    _addons(razorpay)
    await billing.run(session)
    assert (await _statement(session, world["club"].id)).status == "pending"
    assert razorpay.of("subscription", "createAddon") == []


async def test_the_hourly_billing_sweep_runs_the_message_steps(session, world, razorpay):  # noqa: F811
    from app.api.subscriptions import sweep

    await _paying(session, world["club"].id, world["tiers"].growth.id)
    await _over_last_month(session, world, world["tiers"].growth)
    _addons(razorpay)
    done = await sweep.sweep(session)
    assert (done["message_statements"], done["overage_charged"]) == (1, 1)


# ─── What the dashboards read ────────────────────────────────────────────


async def test_the_plans_and_entitlements_carry_the_limits_and_prices(client, session, world):
    plans = {p["key"]: p for p in (await client.get(f"{API}/plans")).json()["plans"]}
    assert plans["growth"]["limits"]["emails_per_month"] == 3 and plans["growth"]["limits"]["whatsapp_per_month"] == 2
    assert plans["enterprise"]["limits"]["emails_per_month"] is None
    assert (plans["growth"]["email_overage_paise"], plans["growth"]["whatsapp_overage_paise"]) == (10, 150)
    ent = (await client.get(f"{API}/clubs/entitlements", headers=world["h"])).json()
    assert ent["limits"]["emails_per_month"] == 3 and ent["limits"]["whatsapp_per_month"] == 0
    assert (ent["email_overage_paise"], ent["whatsapp_overage_paise"]) == (10, 150)


async def test_the_usage_answer_has_its_shape(client, session, world):
    await quota.record_message(world["club"].id, "email", "ticket", session=session)
    await session.commit()
    body = (await client.get(f"{API}/clubs/messages/usage", headers=world["h"])).json()
    assert set(body) == {"period", "period_label", "period_start", "period_end", "plan_key", "plan_name", "billing",
                         "blocks_past_limit", "custom", "trial", "channels", "estimated_extra_paise", "upgrade"}
    assert body["period"] == quota.key(quota.period_of()) and body["plan_key"] == "free"
    assert body["channels"]["email"] == {
        "used": 1, "limit": 3, "extra": 0, "blocked": 0, "rate_paise": 10, "extra_paise": 0, "percent": 33,
        "state": "ok", "kinds": {"ticket": 1}, "warned": [],
    }
    assert body["channels"]["whatsapp"]["state"] == "not_included" and body["channels"]["whatsapp"]["limit"] == 0
    assert body["upgrade"] is None
    assert (await client.get(f"{API}/clubs/messages/history", headers=world["h"])).json() == {"items": []}


async def test_the_free_trial_uses_pros_limits_and_holds_back_past_them(session, world):
    from app.api.subscriptions import service as subs

    await subs.start_free_trial(session, world["club"].id)
    await session.commit()
    terms = await quota.terms_for(session, world["club"].id)
    assert (terms.plan_key, terms.email_limit, terms.whatsapp_limit, terms.trial, terms.billing) == (
        "pro", 20000, 1000, "free_trial", "none")


async def test_an_admin_sees_a_clubs_usage_and_sets_enterprise_limits(client, session, world, make_admin, mint_token):
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import BillingCycle

    admin = auth_header(await mint_token(await make_admin(email="quota-admin2@example.com")))
    club_id = world["club"].id
    await quota.record_message(club_id, "email", "ticket", session=session)
    await session.commit()
    view = (await client.get(f"{API}/admin/messages/clubs/{club_id}", headers=admin)).json()
    assert view["club_id"] == club_id and view["usage"]["channels"]["email"]["used"] == 1
    assert view["own_limits"] is None and view["own_limits_apply"] is False and view["history"] == []

    body = {"emails_per_month": 50000, "whatsapp_per_month": None, "whatsapp_overage_paise": 80, "note": "Fest deal"}
    set_on_free = await client.put(f"{API}/admin/messages/clubs/{club_id}/limits", json=body, headers=admin)
    assert set_on_free.status_code == 200, set_on_free.text
    # Kept, but it applies only on Enterprise.
    assert set_on_free.json()["own_limits_apply"] is False
    assert set_on_free.json()["usage"]["channels"]["email"]["limit"] == 3

    await subs.assign_plan(session, club_id=club_id, plan=world["tiers"].enterprise, cycle=BillingCycle.yearly)
    await session.commit()
    view = (await client.get(f"{API}/admin/messages/clubs/{club_id}", headers=admin)).json()
    assert view["own_limits_apply"] is True and view["usage"]["custom"] is True
    assert view["usage"]["channels"]["email"]["limit"] == 50000
    assert view["usage"]["channels"]["whatsapp"]["limit"] is None
    assert view["usage"]["channels"]["whatsapp"]["rate_paise"] == 80
    assert view["usage"]["billing"] == "manual"
    assert view["own_limits"]["note"] == "Fest deal"

    # The club's own entitlements say the same.
    ent = (await client.get(f"{API}/clubs/entitlements", headers=world["h"])).json()
    assert (ent["limits"]["emails_per_month"], ent["limits"]["whatsapp_per_month"]) == (50000, None)
    assert (ent["email_overage_paise"], ent["whatsapp_overage_paise"]) == (10, 80)

    cleared = await client.delete(f"{API}/admin/messages/clubs/{club_id}/limits", headers=admin)
    assert cleared.json()["own_limits"] is None and cleared.json()["usage"]["channels"]["email"]["limit"] is None


async def test_setting_a_clubs_limits_needs_the_money_permission(client, session, world, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    support = auth_header(await mint_token(await make_admin(email="quota-support@example.com", role=AdminRoles.support)))
    r = await client.put(f"{API}/admin/messages/clubs/{world['club'].id}/limits", json={}, headers=support)
    assert r.status_code == 403
    assert (await client.get(f"{API}/clubs/messages/usage")).status_code == 401


async def test_the_plan_editor_keeps_the_message_limits_an_old_form_leaves_out(client, session, world, make_admin, mint_token):
    admin = auth_header(await mint_token(await make_admin(email="quota-plans@example.com")))
    growth = world["tiers"].growth
    plan = (await client.get(f"{API}/admin/plans/{growth.id}", headers=admin)).json()
    old_form = {k: plan[k] for k in ("key", "name", "tagline", "price_monthly_paise", "price_yearly_paise", "trial_days",
                                     "visibility", "sort_order", "features")}
    old_form["limits"] = {k: v for k, v in plan["limits"].items() if k not in ("emails_per_month", "whatsapp_per_month")}
    saved = await client.put(f"{API}/admin/plans/{growth.id}", json=old_form, headers=admin)
    assert saved.status_code == 200, saved.text
    assert saved.json()["limits"]["emails_per_month"] == 3 and saved.json()["email_overage_paise"] == 10

    new_form = {**old_form, "limits": {**plan["limits"], "emails_per_month": 9000, "whatsapp_per_month": None},
                "email_overage_paise": 15, "whatsapp_overage_paise": 120}
    saved = await client.put(f"{API}/admin/plans/{growth.id}", json=new_form, headers=admin)
    body = saved.json()
    assert (body["limits"]["emails_per_month"], body["limits"]["whatsapp_per_month"]) == (9000, None)
    assert (body["email_overage_paise"], body["whatsapp_overage_paise"]) == (15, 120)

    created = await client.post(f"{API}/admin/plans", json={"key": "campus2", "name": "Campus 2"}, headers=admin)
    assert (created.json()["email_overage_paise"], created.json()["whatsapp_overage_paise"]) == (10, 150)


async def test_the_migration_seeds_the_owners_limits(session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.api.subscriptions.models import PlanLimits, Plans
    from tests.test_trial_pricing import _migration

    plans_seed, trial_pricing, message_quotas = _migration("club_plans"), _migration("trial_pricing"), _migration("message_quotas")
    connection = await session.connection()

    def run(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans_seed.seed_plans()
            trial_pricing.retire_plan_trials(sync)
            trial_pricing.fold_campus(sync)
            return message_quotas.seed_limits(sync)

    assert await connection.run_sync(run) == 4
    await session.commit()
    rows = (await session.execute(
        select(Plans.key, PlanLimits.emails_per_month, PlanLimits.whatsapp_per_month, Plans.email_overage_paise,
               Plans.whatsapp_overage_paise).join(PlanLimits, PlanLimits.plan_id == Plans.id)
    )).all()
    # The schema here is the models' (create_all), whose WhatsApp default is
    # pricing_adjust's ₹1.50; on a real database message_quotas added it at ₹1.
    assert {k: (e, w, ep, wp) for k, e, w, ep, wp in rows} == {
        "free": (200, 0, 10, 150), "starter": (1000, 50, 10, 150), "growth": (5000, 250, 10, 150),
        "pro": (20000, 1000, 10, 150), "campus": (None, None, 10, 150), "enterprise": (None, None, 10, 150),
    }


async def test_pricing_adjust_changes_only_what_the_owner_has_not_edited(session):
    """The pricing_adjust migration on the seeded plans as production has
    them (₹1 a WhatsApp extra): ₹1.50 where the price is still ₹1, a
    promotional cap of 20% of each plan's WhatsApp, the trial's allowances on
    Pro; an owner's own price is kept; a second run changes nothing."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.api.subscriptions.models import PlanLimits, Plans
    from tests.test_trial_pricing import _migration

    plans_seed, trial_pricing, message_quotas, pricing = (
        _migration("club_plans"), _migration("trial_pricing"), _migration("message_quotas"), _migration("pricing_adjust"))
    assert pricing.down_revision == "club_whatsapp_reach"
    connection = await session.connection()

    def run(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans_seed.seed_plans()
            trial_pricing.retire_plan_trials(sync)
            trial_pricing.fold_campus(sync)
            message_quotas.seed_limits(sync)
            # As message_quotas left production: ₹1 everywhere ...
            sync.execute(text("UPDATE plans SET whatsapp_overage_paise = 100"))
            # ... and the owner's own edits since: Growth's price, Starter's allowance.
            sync.execute(text("UPDATE plans SET whatsapp_overage_paise = 120 WHERE key = 'growth'"))
            sync.execute(text("UPDATE plan_limits SET whatsapp_per_month = 80 "
                              "WHERE plan_id = (SELECT id FROM plans WHERE key = 'starter')"))
            first = pricing.seed(sync)
            return first, pricing.seed(sync)

    first, second = await connection.run_sync(run)
    # Five plans at ₹1 (Growth's ₹1.20 is the owner's), four with a WhatsApp allowance, four trial values.
    assert first == {"whatsapp_overage": 5, "marketing_caps": 4, "trial_limits": 4}
    assert second == {"whatsapp_overage": 0, "marketing_caps": 0, "trial_limits": 0}
    await session.commit()
    rows = (await session.execute(
        select(Plans.key, Plans.whatsapp_overage_paise, Plans.email_overage_paise, PlanLimits.whatsapp_per_month,
               PlanLimits.whatsapp_marketing_per_month).join(PlanLimits, PlanLimits.plan_id == Plans.id)
    )).all()
    assert {k: (wp, ep, w, m) for k, wp, ep, w, m in rows} == {
        "free": (150, 10, 0, 0), "starter": (150, 10, 80, 16), "growth": (120, 10, 250, 50),
        "pro": (150, 10, 1000, 200), "campus": (150, 10, None, None), "enterprise": (150, 10, None, None),
    }
    trial = (await session.execute(
        select(PlanLimits.trial_emails_per_month, PlanLimits.trial_whatsapp_per_month,
               PlanLimits.trial_whatsapp_marketing_per_month, PlanLimits.trial_ai_actions_per_month)
        .join(Plans, Plans.id == PlanLimits.plan_id).where(Plans.key == "pro")
    )).one()
    assert tuple(trial) == (2000, 50, 10, 50)
    others = await session.scalar(
        select(func.count(PlanLimits.id)).join(Plans, Plans.id == PlanLimits.plan_id)
        .where(Plans.key != "pro", PlanLimits.trial_emails_per_month.isnot(None)))
    assert others == 0


# ─── Promotional WhatsApp and the trial's allowances (pricing_adjust) ────


async def _cap_marketing(session, plan, cap):
    from sqlalchemy import update

    from app.api.subscriptions.models import PlanLimits

    await session.execute(update(PlanLimits).where(PlanLimits.plan_id == plan.id).values(whatsapp_marketing_per_month=cap))
    await session.commit()


def test_what_counts_as_promotional_whatsapp():
    # By the template's category when the sender knows it ...
    assert quota.is_marketing("whatsapp", "update", "MARKETING") and quota.is_marketing("whatsapp", "update", "marketing")
    assert not quota.is_marketing("whatsapp", "announcement", "UTILITY")
    # ... else by what the message is.
    assert all(quota.is_marketing("whatsapp", k) for k in ("promotion", "announcement", "campaign"))
    assert not any(quota.is_marketing("whatsapp", k) for k in ("reminder", "update", "journey", "survey"))
    # Never a ticket, certificate or payment message, whatever it is called; never an email.
    assert not any(quota.is_marketing("whatsapp", k, "MARKETING") for k in ("ticket", "certificate", "payment"))
    assert not quota.is_marketing("email", "promotion", "MARKETING")


async def test_promotional_whatsapp_past_the_cap_is_held_back_but_tickets_go(session, world, meta, background):
    """Growth, which Razorpay bills: past its promotional cap a MARKETING
    WhatsApp is held back (never sold as an extra); utility messages and
    tickets go on, past the WhatsApp limit too, as extras."""
    growth = world["tiers"].growth
    await _cap_marketing(session, growth, 1)
    await _paying(session, world["club"].id, growth.id)
    club_id = world["club"].id
    promo = whatsapp.event_update(name="Ananya", club="Quota Club", event="Build Night", update="20% off merch")

    out = await whatsapp.send("9876543210", promo, meter={"club_id": club_id, "kind": "update", "category": "MARKETING",
                                                           "ref": "promo:1"})
    assert out["Via"] == "platform"
    with pytest.raises(whatsapp.Undeliverable) as held:
        await whatsapp.send("9876543211", promo, meter={"club_id": club_id, "kind": "update", "category": "MARKETING",
                                                         "ref": "promo:2"})
    assert held.value.reason == (
        "Not sent: this month's 1 promotional WhatsApp message on Growth are used up. "
        "Tickets, certificates and reminders still go."
    )
    # A utility update is not promotional: it goes, and so do tickets, past the limit of 2.
    await whatsapp.send("9876543212", promo, meter={"club_id": club_id, "kind": "update", "category": "UTILITY",
                                                     "ref": "update:1"})
    result = await deliveries.queue(session, world["event"], DeliveryKind.ticket, registration_ids=[world["regs"][0].id])
    for did in result["ids"]:
        await deliveries.deliver(session, did)
    assert [m["template"]["name"] for m in meta["messages"]].count("ticket_confirmed") == 1
    assert len(meta["messages"]) == 3
    wa = await _usage(session, club_id, "whatsapp")
    assert (wa.used, wa.marketing, wa.blocked) == (3, 1, 1)
    assert wa.kinds == {"update": 2, "ticket": 1}

    usage = (await quota.usage(session, club_id))["channels"]["whatsapp"]
    assert usage["marketing"] == {"used": 1, "limit": 1}
    assert (usage["used"], usage["limit"], usage["extra"], usage["state"]) == (3, 2, 1, "over")


async def test_free_sends_no_promotional_whatsapp_and_a_club_number_is_never_capped(session, world, meta, background):
    await _cap_marketing(session, world["tiers"].free, 0)
    club_id = world["club"].id
    terms = await quota.terms_for(session, club_id)
    assert terms.whatsapp_marketing_limit == 0
    reason = await quota.held_back(session, terms, "whatsapp", "promotion")
    assert reason == "Not sent: promotional WhatsApp messages aren't included on Free."
    # A ticket is never held back, a club's own number never capped or counted.
    assert await quota.held_back(session, terms, "whatsapp", "ticket", category="MARKETING") is None
    assert await quota.held_back(session, terms, "whatsapp", "promotion", via="club") is None
    assert await quota.record_message(club_id, "whatsapp", "promotion", via="club", session=session) is None


async def test_certificates_go_past_a_spent_promotional_cap(session, world, background):
    growth = world["tiers"].growth
    await _cap_marketing(session, growth, 1)
    await _paying(session, world["club"].id, growth.id)
    club_id = world["club"].id
    await quota.record_message(club_id, "whatsapp", "promotion", session=session, ref="p:1")
    terms = await quota.terms_for(session, club_id)
    assert await quota.held_back(session, terms, "whatsapp", "campaign") is not None
    assert await quota.held_back(session, terms, "whatsapp", "certificate") is None
    assert await quota.held_back(session, terms, "whatsapp", "ticket") is None
    assert await quota.held_back(session, terms, "whatsapp", "reminder") is None


async def test_the_free_trial_has_its_own_allowances_and_pros_features(client, session, world, monkeypatch):
    """Pro with trial allowances: a club on the free trial is held to them
    (emails, WhatsApp, promotional WhatsApp, AI actions), keeps Pro's
    features, and sees them on its entitlements and the pricing page."""
    from sqlalchemy import update

    from app.api.subscriptions import gates
    from app.api.subscriptions import service as subs
    from app.api.subscriptions.models import PlanLimits

    pro = world["tiers"].pro
    await session.execute(update(PlanLimits).where(PlanLimits.plan_id == pro.id).values(
        whatsapp_marketing_per_month=200, ai_actions_per_month=500, trial_emails_per_month=2000,
        trial_whatsapp_per_month=50, trial_whatsapp_marketing_per_month=10, trial_ai_actions_per_month=50))
    await session.commit()
    club_id = world["club"].id
    await subs.start_free_trial(session, club_id)
    await session.commit()

    terms = await quota.terms_for(session, club_id)
    assert (terms.plan_key, terms.email_limit, terms.whatsapp_limit, terms.whatsapp_marketing_limit, terms.trial) == (
        "pro", 2000, 50, 10, "free_trial")
    assert terms.blocks  # no card: past 2,000 emails, announcements are held back
    gates.reset()
    assert await gates.cap(session, club_id, "ai_actions_per_month") == 50
    resolved = await subs.resolve_entitlements(session, club_id)
    assert resolved.allows("tickets")

    ent = (await client.get(f"{API}/clubs/entitlements", headers=world["h"])).json()
    assert (ent["limits"]["emails_per_month"], ent["limits"]["whatsapp_per_month"],
            ent["limits"]["whatsapp_marketing_per_month"], ent["limits"]["ai_actions_per_month"]) == (2000, 50, 10, 50)
    body = (await client.get(f"{API}/plans")).json()
    assert body["trial"]["limits"] == {"emails_per_month": 2000, "whatsapp_per_month": 50,
                                       "whatsapp_marketing_per_month": 10, "ai_actions_per_month": 50}
    # The plan itself, as the pricing page and a paying Pro club have it, is untouched.
    pro_card = next(p for p in body["plans"] if p["key"] == "pro")
    assert (pro_card["limits"]["emails_per_month"], pro_card["limits"]["whatsapp_marketing_per_month"]) == (20000, 200)


async def test_the_plan_editor_sets_the_cap_and_the_trial_allowances(client, session, world, make_admin, mint_token):
    admin = auth_header(await mint_token(await make_admin(email="quota-trial-editor@example.com")))
    pro = world["tiers"].pro
    plan = (await client.get(f"{API}/admin/plans/{pro.id}", headers=admin)).json()
    assert plan["trial_limits"] == {"emails_per_month": None, "whatsapp_per_month": None,
                                    "whatsapp_marketing_per_month": None, "ai_actions_per_month": None}
    form = {k: plan[k] for k in ("key", "name", "tagline", "price_monthly_paise", "price_yearly_paise", "trial_days",
                                 "visibility", "sort_order", "features")}
    form["limits"] = {**plan["limits"], "whatsapp_marketing_per_month": 200}
    form["trial_limits"] = {"emails_per_month": 2000, "whatsapp_per_month": 50, "whatsapp_marketing_per_month": 10,
                            "ai_actions_per_month": 50}
    saved = (await client.put(f"{API}/admin/plans/{pro.id}", json=form, headers=admin)).json()
    assert saved["limits"]["whatsapp_marketing_per_month"] == 200
    assert saved["trial_limits"] == form["trial_limits"]

    # An editor that sends neither keeps both.
    older = {**form, "limits": {k: v for k, v in plan["limits"].items() if k != "whatsapp_marketing_per_month"}}
    older.pop("trial_limits")
    kept = (await client.put(f"{API}/admin/plans/{pro.id}", json=older, headers=admin)).json()
    assert kept["limits"]["whatsapp_marketing_per_month"] == 200 and kept["trial_limits"]["emails_per_month"] == 2000

    # A copy carries them.
    copy = (await client.post(f"{API}/admin/plans/{pro.id}/duplicate", headers=admin)).json()
    assert copy["limits"]["whatsapp_marketing_per_month"] == 200 and copy["trial_limits"]["ai_actions_per_month"] == 50


def test_every_team_role_reads_the_bell_and_the_months_messages():
    from app.api.club_access import policy

    for role in ("admin", "editor", "door"):
        assert policy.allows(role, "GET", "/api/v1/clubs/alerts"), role
        # Marking a notice read changes nothing about the club: door staff may too.
        assert policy.allows(role, "POST", "/api/v1/clubs/alerts/read"), role
        assert policy.allows(role, "GET", "/api/v1/clubs/messages/usage"), role
        assert policy.allows(role, "GET", "/api/v1/clubs/messages/history"), role
