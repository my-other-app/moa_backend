"""The money ledger: every money transition posted once, in the transaction
that makes it, as lines that sum to zero; the monthly sums kept with it; the
history backfilled to the same figures the reports show; drift put right;
Razorpay's day checked against it; and the finance and revenue pages the
same read either way. Razorpay is a fake throughout."""
import itertools
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.api.admin_alerts.models import AdminNotifications
from app.api.ledger import book, jobs, recon
from app.api.ledger import service as ledger
from app.api.ledger.models import LedgerEntries, LedgerMonthly, LedgerPlatformMonthly
from app.api.payments import service as payments
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from tests.store_items import shelf_item
from tests.test_club_billing import shop  # noqa: F401  (a fixture: a club and its plans)
from tests.test_money_integrity import FakeRazorpay, _guest, _order, _paid_event, _reg, _webhook

NOW = datetime.now(timezone.utc)
_n = itertools.count(1)


@pytest.fixture
def rzp(session, monkeypatch):
    fake = FakeRazorpay(session)
    monkeypatch.setattr(payments, "razorpay_client", fake)
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kwargs: None)
    return fake


@pytest.fixture(autouse=True)
async def _ledger_whole(request, session):
    """After every test here: no posting failed (unless the test says it
    expects one), every posting sums to zero, and the sums are the entries'."""
    yield
    if "failures_expected" not in request.keywords:
        assert await _alerts(session, "ledger_post_failed") == []
    assert await jobs.unbalanced(session) == []
    await _sums_agree(session)


async def _alerts(session, kind):
    return list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == kind))).all())


async def _sums_agree(session):
    """ledger_monthly is the entries summed; ledger_platform_monthly is ledger_monthly summed."""
    E, LM, LP = LedgerEntries, LedgerMonthly, LedgerPlatformMonthly
    month, origin = jobs.ist_month(E.occurred_at), jobs.ist_month(E.origin_at)
    keys = (func.coalesce(E.club_id, 0), func.coalesce(E.event_id, 0), func.coalesce(E.gateway_id, 0), month, origin, E.account, E.kind)
    entries = {tuple(r[:7]): tuple(int(x) for x in r[7:]) for r in (await session.execute(
        select(*keys, func.sum(E.amount_paise), func.sum(E.units), func.count()).group_by(*keys)
    )).all()}
    monthly = {tuple(r[:7]): tuple(int(x) for x in r[7:]) for r in (await session.execute(
        select(LM.club_id, LM.event_id, LM.gateway_id, LM.month, LM.origin_month, LM.account, LM.kind,
               LM.amount_paise, LM.units, LM.entries).where(LM.entries != 0)
    )).all()}
    assert monthly == entries
    club_gw = LM.gateway_id != 0
    from_monthly = {tuple(r[:5]): tuple(int(x) for x in r[5:]) for r in (await session.execute(
        select(LM.month, LM.origin_month, club_gw, LM.account, LM.kind, func.sum(LM.amount_paise), func.sum(LM.units), func.sum(LM.entries))
        .group_by(LM.month, LM.origin_month, club_gw, LM.account, LM.kind)
    )).all()}
    platform = {tuple(r[:5]): tuple(int(x) for x in r[5:]) for r in (await session.execute(
        select(LP.month, LP.origin_month, LP.club_gateway, LP.account, LP.kind, func.sum(LP.amount_paise), func.sum(LP.units), func.sum(LP.entries))
        .group_by(LP.month, LP.origin_month, LP.club_gateway, LP.account, LP.kind)
    )).all()}
    assert {k: v for k, v in platform.items() if any(v)} == {k: v for k, v in from_monthly.items() if any(v)}


async def _lines(session, **where):
    q = select(LedgerEntries).order_by(LedgerEntries.id)
    for key, value in where.items():
        q = q.where(getattr(LedgerEntries, key) == value)
    return list((await session.scalars(q)).all())


def _shape(lines):
    """(kind, account, paise, units) of each line, in order."""
    return [(x.kind, x.account, x.amount_paise, x.units) for x in lines]


async def _balance(session, account, **where):
    q = select(func.coalesce(func.sum(LedgerEntries.amount_paise), 0)).where(LedgerEntries.account == account)
    for key, value in where.items():
        q = q.where(getattr(LedgerEntries, key) == value)
    return int(await session.scalar(q))


async def _pay(session, rzp, payer, event, reg, **pay):
    order = await _order(session, payer, event, reg)
    payment = rzp.pay(order.razorpay_order_id, **pay)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    return order, payment


# ─── Posting: a capture, once ────────────────────────────────────────────


async def test_a_capture_is_posted_with_what_it_bought_and_summed(session, rzp, make_club, make_event, make_user, make_registration):
    owner, club, event = await _paid_event(make_club, make_event, session, fee="249.99")
    payer, reg = await _guest(session, make_user, make_registration, event)
    order, payment = await _pay(session, rzp, payer, event, reg)

    lines = await _lines(session, registration_id=reg.id)
    assert _shape(lines) == [
        ("capture", "gateway", 24999, 1), ("capture", "unapplied", -24999, 0),
        ("apply", "tickets_online", -24999, 1), ("apply", "unapplied", 24999, 0),
    ]
    capture = [x for x in lines if x.kind == "capture"]
    assert {x.txn_key for x in capture} == {f"capture:{payment['id']}"}
    assert all(x.club_id == club.id and x.event_id == event.id for x in lines)
    assert capture[0].razorpay_order_id == order.razorpay_order_id and capture[0].gateway_id is None
    # Nothing left unapplied: every rupee taken paid for the ticket.
    assert await _balance(session, "unapplied", registration_id=reg.id) == 0

    # The month the capture was posted in (India's), not NOW's: NOW is read
    # at import, and a run that crosses 18:30 UTC on a month's last day
    # posts into the next month.
    month = book.month_of(capture[0].occurred_at)
    rows = {(r.account, r.kind): (r.amount_paise, r.units, r.entries) for r in (await session.scalars(
        select(LedgerMonthly).where(LedgerMonthly.event_id == event.id)
    )).all()}
    assert rows[("gateway", "capture")] == (24999, 1, 1)
    assert rows[("tickets_online", "apply")] == (-24999, 1, 1)
    platform = await session.scalar(
        select(func.sum(LedgerPlatformMonthly.amount_paise)).where(
            LedgerPlatformMonthly.month == month, LedgerPlatformMonthly.account == "gateway",
            LedgerPlatformMonthly.kind == "capture", LedgerPlatformMonthly.club_gateway == False,  # noqa: E712
        )
    )
    assert platform == 24999


async def test_a_replayed_webhook_or_a_second_verify_posts_nothing_more(session, rzp, make_club, make_event, make_user, make_registration):
    owner, club, event = await _paid_event(make_club, make_event, session, fee="100.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    order, payment = await _pay(session, rzp, payer, event, reg)
    before = len(await _lines(session))

    # The webhook for the same capture, twice, under two event ids (payment.captured and order.paid).
    await payments.handle_razorpay_webhook(session, _webhook(payment), "evt_cap_1", "sig")
    await payments.handle_razorpay_webhook(session, _webhook(payment), "evt_cap_1", "sig")
    await payments.handle_razorpay_webhook(session, _webhook(payment, "order.paid"), "evt_paid_1", "sig")
    # The app's verify again, the repair sweep, and the capture posted by hand.
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    await payments.ensure_applied(session, order.id)
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment["id"]))
    assert await ledger.capture(session, log, order) == 0
    await session.commit()

    assert len(await _lines(session)) == before
    assert await _balance(session, "gateway") == 10000
    assert await _balance(session, "tickets_online") == -10000


async def test_a_refund_takes_back_the_ticket_and_the_store_and_pays_out(session, rzp, make_club, make_event, make_user, make_registration):
    from app.api.store import service as store
    from app.api.store.models import EventStoreItems

    owner, club, event = await _paid_event(make_club, make_event, session, fee="300.00", merch_enabled=True)
    tee = shelf_item(session, event, name="Tee", price=Decimal("450.00"), stock=5)
    session.add(tee)
    await session.flush()
    payer, reg = await _guest(session, make_user, make_registration, event)
    await store.place_order(session, event, reg, [SimpleNamespace(item_id=tee.id, variant_id=None, qty=1)])
    await session.commit()
    order, payment = await _pay(session, rzp, payer, event, reg)
    assert payment["amount"] == 75000
    assert await _balance(session, "tickets_online", registration_id=reg.id) == -30000
    assert await _balance(session, "store", registration_id=reg.id) == -45000

    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment["id"]))
    await payments.refund_payment(session, log.id)

    refund = await _lines(session, txn_key=f"refund:{payment['id']}")
    assert _shape(refund) == [("refund", "unapplied", 75000, 0), ("refund", "gateway", -75000, 1)]
    assert refund[0].razorpay_refund_id and refund[0].razorpay_refund_id.startswith("rfnd_")
    # The refund's sale was this payment: its origin is the capture.
    assert refund[0].origin_at == log.created_at
    unapply = [x for x in await _lines(session, registration_id=reg.id) if x.kind == "unapply"]
    assert sorted(_shape(unapply)) == sorted([
        ("unapply", "store", 45000, -1), ("unapply", "tickets_online", 30000, -1), ("unapply", "unapplied", -75000, 0),
    ])
    for account in ("gateway", "unapplied", "tickets_online", "store"):
        assert await _balance(session, account, registration_id=reg.id) == 0, account


# ─── Refunds, in order ───────────────────────────────────────────────────


async def test_a_payment_from_before_the_ledger_is_posted_before_its_refund(session, rzp, monkeypatch, make_club, make_event, make_user, make_registration):
    """Refunded after the ledger began, taken before it: the capture, and the
    ticket as it stood, are posted first, so the refund never takes out more
    than went in."""
    owner, club, event = await _paid_event(make_club, make_event, session, fee="200.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    post = book.post

    async def nothing(session, txn):
        return 0

    monkeypatch.setattr(book, "post", nothing)
    order, payment = await _pay(session, rzp, payer, event, reg)
    monkeypatch.setattr(book, "post", post)
    assert await _lines(session) == []

    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment["id"]))
    await payments.refund_payment(session, log.id)

    lines = await _lines(session, registration_id=reg.id)
    assert [x.kind for x in lines] == ["capture", "capture", "backfill", "backfill", "unapply", "unapply", "refund", "refund"]
    capture, refund = lines[0], lines[-1]
    assert capture.occurred_at == log.created_at and refund.occurred_at >= capture.occurred_at
    for account in ("gateway", "unapplied", "tickets_online"):
        assert await _balance(session, account, registration_id=reg.id) == 0, account
    # The same refund heard again from Razorpay's webhook: nothing more.
    n = len(await _lines(session))
    await payments.handle_razorpay_webhook(session, {"event": "refund.processed", "payload": {"refund": {"entity": {
        "id": "rfnd_again", "payment_id": payment["id"], "amount": payment["amount"]}}}}, "evt_rf_again", "sig")
    assert len(await _lines(session)) == n


async def test_money_not_owed_is_held_unapplied_until_it_goes_back(session, rzp, make_club, make_event, make_user, make_registration):
    """A second payment on a paid order, and a payment that lands after the
    event's last paid place went: taken in, held in `unapplied` (owed back),
    and out again when Razorpay refunds it -- never a ticket."""
    from app.api.payments import sweep

    owner, club, event = await _paid_event(make_club, make_event, session, fee="150.00", max_participants=1)
    first, first_reg = await _guest(session, make_user, make_registration, event)
    second, second_reg = await _guest(session, make_user, make_registration, event)
    first_order = await _order(session, first, event, first_reg)
    second_order = await _order(session, second, event, second_reg)

    # Paid twice for one order.
    p1, p2 = rzp.pay(first_order.razorpay_order_id), rzp.pay(first_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, first_order.razorpay_order_id, p1["id"])
    await payments.verify_razorpay_payment(session, first_order.razorpay_order_id, p2["id"])
    assert _shape(await _lines(session, razorpay_payment_id=p2["id"])) == [
        ("capture", "gateway", 15000, 1), ("capture", "unapplied", -15000, 0),
        ("refund", "unapplied", 15000, 0), ("refund", "gateway", -15000, 1),
    ]

    # The last place gone; Razorpay will not refund at first.
    rzp.refund_fails = True
    p3 = rzp.pay(second_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, second_order.razorpay_order_id, p3["id"])
    assert (await _reg(session, second_reg.id)).is_paid is False
    held = await _lines(session, razorpay_payment_id=p3["id"])
    assert _shape(held) == [("capture", "gateway", 15000, 1), ("capture", "unapplied", -15000, 0)]
    assert await _balance(session, "unapplied", event_id=event.id) == -15000
    # The sweep gets it back.
    rzp.refund_fails = False
    for log in (await session.scalars(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == p3["id"]))).all():
        log.refund_requested_at = NOW - timedelta(minutes=5)
    await session.commit()
    await sweep.refunds(session)
    assert await _balance(session, "unapplied", event_id=event.id) == 0
    assert await _balance(session, "gateway", event_id=event.id) == 15000
    assert await _balance(session, "tickets_online", event_id=event.id) == -15000


# ─── The store: holds post nothing; sold, oversold, fulfilled, refunded ─


async def test_store_holds_post_nothing_until_the_money_lands(session, rzp, make_club, make_event, make_user, make_registration):
    from app.api.store import service as store
    from app.api.store.models import EventStoreItems, EventStoreOrders

    owner, club, event = await _paid_event(make_club, make_event, session, fee="100.00", merch_enabled=True)
    kit = shelf_item(session, event, name="Kit", price=Decimal("450.00"), stock=1)
    session.add(kit)
    await session.flush()
    late, late_reg = await _guest(session, make_user, make_registration, event)
    quick, quick_reg = await _guest(session, make_user, make_registration, event)
    pick = [SimpleNamespace(item_id=kit.id, variant_id=None, qty=1)]

    # Held: nothing moved.
    await store.place_order(session, event, late_reg, pick)
    await session.commit()
    late_order = await _order(session, late, event, late_reg)
    assert await _lines(session) == []
    # The hold runs out and is given back: still nothing.
    held = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == late_reg.id))
    held.held_until = NOW - timedelta(minutes=1)
    await session.commit()
    assert await store.expire_holds(session) == 1
    assert await _lines(session) == []

    # Someone else takes the kit and pays: sold.
    await store.place_order(session, event, quick_reg, pick)
    await session.commit()
    await _pay(session, rzp, quick, event, quick_reg)
    assert await _balance(session, "store", registration_id=quick_reg.id) == -45000

    # The first payment lands after all: the ticket is theirs, the kit was gone -- held, not sold.
    payment = rzp.pay(late_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, late_order.razorpay_order_id, payment["id"])
    assert await _balance(session, "store_held", registration_id=late_reg.id) == -45000
    assert await _balance(session, "store", registration_id=late_reg.id) == 0
    assert await _balance(session, "tickets_online", registration_id=late_reg.id) == -10000

    # Stock comes in and the organiser hands it over: held becomes sold.
    kit_row = await session.get(EventStoreItems, kit.id)
    kit_row.stock = 2
    await session.commit()
    over = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == late_reg.id, EventStoreOrders.status == "oversold"))
    await store.fulfil(session, over)
    fulfil = await _lines(session, kind="fulfil")
    assert sorted(_shape(fulfil)) == sorted([("fulfil", "store", -45000, 1), ("fulfil", "store_held", 45000, -1)])
    assert fulfil[0].origin_at == over.paid_at
    assert await _balance(session, "store", registration_id=late_reg.id) == -45000
    assert await _balance(session, "store_held", registration_id=late_reg.id) == 0


# ─── At zero: a free pick, a complimentary plan ──────────────────────────


async def test_free_picks_and_complimentary_plans_are_posted_at_zero(client, session, make_club, make_event, make_user, make_registration, make_admin, mint_token):
    from app.api.store import service as store
    from app.api.store.models import EventStoreItems
    from tests.conftest import auth_header
    from tests.test_club_billing import make_plan

    owner, club, event = await _paid_event(make_club, make_event, session, fee="100.00", merch_enabled=True)
    sticker = shelf_item(session, event, name="Stickers", price=Decimal("60.00"), stock=10, free_roles=["attendee"])
    session.add(sticker)
    await session.flush()
    payer, reg = await _guest(session, make_user, make_registration, event)
    await store.place_order(session, event, reg, [SimpleNamespace(item_id=sticker.id, variant_id=None, qty=2)])
    await session.commit()
    assert _shape(await _lines(session, registration_id=reg.id)) == [("comp", "store", 0, 1)]

    pro = await make_plan(session, key="pro", monthly=149900)
    await make_plan(session, key="free")
    await session.commit()
    admin = await make_admin(email="ledger-admin@example.com")
    r = await client.put(f"/api/v1/admin/subscriptions/club/{club.id}/plan", headers=auth_header(await mint_token(admin)),
                         json={"plan_id": pro.id, "cycle": "monthly", "reason": "Sponsored"})
    assert r.status_code == 200, r.text
    comp = await _lines(session, kind="comp", account="comps")
    assert _shape(comp) == [("comp", "comps", 0, 1)] and comp[0].club_id == club.id and comp[0].source == "admin_audit_logs"


# ─── At the door ─────────────────────────────────────────────────────────


async def test_a_walk_up_paying_at_the_desk_is_cash(client, session, make_club, make_event, mint_token):
    from tests.conftest import auth_header

    owner, club = await make_club(name="Desk Club", email="desk-ledger@example.com")
    event = await make_event(club=club, created_by=owner, has_fee=True, reg_fee=Decimal("350.00"), event_datetime=NOW + timedelta(hours=2))
    await session.commit()
    r = await client.post(f"/api/v1/events/{event.id}/gate/walkup", headers=auth_header(await mint_token(owner)),
                          json={"name": "Walk In", "email": "walkin-ledger@example.com", "client_ref": uuid.uuid4().hex})
    assert r.status_code == 200 and r.json()["outcome"] == "REGISTERED", r.text
    assert sorted(_shape(await _lines(session, event_id=event.id))) == sorted([("desk", "desk", 35000, 0), ("desk", "tickets_desk", -35000, 1)])


# ─── Append-only ─────────────────────────────────────────────────────────


async def test_the_ledger_refuses_updates_and_deletes(session):
    await book.post(session, book.Txn(
        key="test:append-only", kind=book.COMP, lines=[book.Line(book.COMPS, 0, 1)], occurred_at=NOW,
        source="test", source_id="1",
    ))
    await session.commit()
    for statement in ("UPDATE ledger_entries SET amount_paise = 1", "DELETE FROM ledger_entries", "TRUNCATE ledger_entries"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with session.begin_nested():
                await session.execute(text(statement))
    assert len(await _lines(session)) == 1


@pytest.mark.failures_expected
async def test_an_unbalanced_posting_is_refused_and_named(session):
    assert await book.post(session, book.Txn(
        key="test:unbalanced", kind=book.CAPTURE, lines=[book.Line(book.GATEWAY, 100), book.Line(book.UNAPPLIED, -99)],
        occurred_at=NOW, source="test", source_id="unbalanced",
    )) == 0
    await session.commit()
    assert await _lines(session) == []
    [alert] = await _alerts(session, "ledger_post_failed")
    assert alert.data["source_id"] == "unbalanced" and "Unbalanced" in alert.data["error"]



@pytest.mark.failures_expected
async def test_a_posting_that_fails_never_costs_the_payment_and_is_posted_again(session, rzp, monkeypatch, make_club, make_event, make_user, make_registration):
    """The ledger's write fails (here: its table is not there): the guest is
    still paid and has their ticket, the admins hear, and the daily job posts it."""
    owner, club, event = await _paid_event(make_club, make_event, session, fee="120.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    real = book._add_to_sums

    async def broken(session, txn, lines):
        raise RuntimeError("ledger_monthly is not there")

    monkeypatch.setattr(book, "_add_to_sums", broken)
    await _pay(session, rzp, payer, event, reg)
    monkeypatch.setattr(book, "_add_to_sums", real)
    assert (await _reg(session, reg.id)).is_paid is True
    assert await _lines(session) == []
    failed = await _alerts(session, "ledger_post_failed")
    assert {a.data["source"] for a in failed} == {"payment_logs", "event_registrations_link"}

    done = await jobs.retry_failed(session, NOW - timedelta(hours=1))
    assert done["sources"] == 2 and done["lines"] == 4
    assert await _balance(session, "gateway", registration_id=reg.id) == 12000
    assert await _balance(session, "tickets_online", registration_id=reg.id) == -12000
    assert await _balance(session, "unapplied", registration_id=reg.id) == 0


# ─── Subscriptions ───────────────────────────────────────────────────────

DAY = timedelta(days=1)


async def test_subscription_charges_refunds_and_first_charges_are_posted_once(session, rzp, shop):
    from tests.test_billing_alerts import _paid, _payment_failed
    from tests.test_revenue import _charged, _event

    await _paid(session, shop, sub_id="sub_led")
    feb, jan = NOW - 30 * DAY, NOW - 60 * DAY
    await _charged(session, "sub_led", "pay_s1", when=feb)
    await _charged(session, "sub_led", "pay_s1", when=feb)  # delivered again
    assert _shape(await _lines(session, razorpay_payment_id="pay_s1", kind="sub_charge")) == [
        ("sub_charge", "gateway", 69900, 1), ("sub_charge", "subscriptions", -69900, 0),
    ]
    first = await _lines(session, account="subscribers")
    assert _shape(first) == [("first_charge", "subscribers", 0, 1)] and first[0].origin_at == feb.replace(microsecond=0)

    # Refunded in part, the same refund heard twice, then the rest.
    refund = {"refund": {"entity": {"id": "rfnd_s1", "payment_id": "pay_s1", "amount": 20000}},
              "payment": {"entity": {"id": "pay_s1", "amount_refunded": 20000}}}
    await _event(session, "refund.processed", refund)
    await _event(session, "refund.processed", refund)
    await _event(session, "refund.processed", {"refund": {"entity": {"id": "rfnd_s2", "payment_id": "pay_s1", "amount": 49900}},
                                               "payment": {"entity": {"id": "pay_s1", "amount_refunded": 69900}}})
    refunds = await _lines(session, kind="sub_refund")
    assert _shape(refunds) == [
        ("sub_refund", "subscriptions", 20000, 0), ("sub_refund", "gateway", -20000, 1),
        ("sub_refund", "subscriptions", 49900, 0), ("sub_refund", "gateway", -49900, 0),
    ]
    assert all(x.origin_at == feb.replace(microsecond=0) for x in refunds)  # against the charge's month
    assert await _balance(session, "gateway", razorpay_payment_id="pay_s1") == 0

    # A charge from before, found later: the first charge moves to it.
    await _charged(session, "sub_led", "pay_s0", when=jan)
    marks = await _lines(session, account="subscribers")
    assert [(x.units, x.origin_at) for x in marks] == [
        (1, feb.replace(microsecond=0)), (1, jan.replace(microsecond=0)), (-1, feb.replace(microsecond=0)),
    ]

    # Failed first, then taken after all: posted once, when it was taken.
    await _payment_failed(session, "sub_led", pay_id="pay_s2")
    assert await _lines(session, razorpay_payment_id="pay_s2") == []
    await _charged(session, "sub_led", "pay_s2", when=NOW - DAY)
    assert _shape(await _lines(session, razorpay_payment_id="pay_s2", kind="sub_charge")) == [
        ("sub_charge", "gateway", 69900, 1), ("sub_charge", "subscriptions", -69900, 0),
    ]


# ─── A season, through the app's own paths ───────────────────────────────


async def _world(session, client, rzp, make_club, make_event, make_user, make_registration, make_admin, mint_token):
    """Two clubs' money the way it happens: tickets paid online (with store
    picks), one refunded by an admin, one paid twice, one too late for the
    last place, one guest who never paid, a free event's free and priced
    picks, walk-ups paying at the desk, an oversold kit handed over later,
    expenses and sponsorship, and a club's subscription charged, refunded in
    part and in full, failed then taken; another club given a plan free.
    Events are sold while upcoming, then moved to when they were held."""
    from app.api.closeout.models import EventStatus
    from app.api.events.models import Events
    from app.api.finance import service as finance
    from app.api.finance.schemas import ExpenseCreate
    from app.api.store import service as store
    from app.api.store.models import EventStoreItems, EventStoreOrders
    from app.api.subscriptions.models import ClubSubscriptions
    from tests.conftest import auth_header
    from tests.test_billing_alerts import _payment_failed
    from tests.test_club_billing import make_plan
    from tests.test_revenue import _charged, _event

    owner, club = await make_club(name="Ledger Society", email="ledger-society@example.com")
    rival_owner, rival = await make_club(name="Other Society", email="other-society@example.com")

    async def an_event(c, o, fee, **kw):
        return await make_event(club=c, created_by=o, name=f"Event {next(_n)}", event_datetime=NOW + 20 * DAY,
                                has_fee=Decimal(fee) > 0, reg_fee=Decimal(fee), **kw)

    async def held(event, days_ago):
        await session.execute(update(Events).where(Events.id == event.id).values(
            event_datetime=NOW - days_ago * DAY, lifecycle=EventStatus.closed))
        await session.commit()

    async def guest(event):
        return await _guest(session, make_user, make_registration, event)

    def pick(item, qty=1):
        return [SimpleNamespace(item_id=item.id, variant_id=None, qty=qty)]

    async def refund(payment_id):
        log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment_id))
        await payments.refund_payment(session, log.id)

    # A paid hackathon with a tee on the shelf.
    hack = await an_event(club, owner, "499.00", merch_enabled=True)
    tee = shelf_item(session, hack, name="Tee", price=Decimal("350.00"), stock=20)
    session.add(tee)
    await session.commit()
    for i in range(6):
        payer, reg = await guest(hack)
        if i in (0, 1, 5):
            await store.place_order(session, hack, reg, pick(tee, 2 if i == 0 else 1))
            await session.commit()
        _, payment = await _pay(session, rzp, payer, hack, reg)
        if i == 5:
            await refund(payment["id"])
    await guest(hack)  # registered, never paid
    payer, reg = await guest(hack)  # paid twice on one order
    order = await _order(session, payer, hack, reg)
    p1, p2 = rzp.pay(order.razorpay_order_id), rzp.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, p1["id"])
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, p2["id"])
    await finance.create_expense(session, club, ExpenseCreate(event_id=hack.id, title="Venue", category="venue", amount=Decimal("2000.00")), owner)
    await finance.create_expense(session, club, ExpenseCreate(event_id=hack.id, kind="income", title="Sponsor", category="sponsorship", amount=Decimal("5000.00")), owner)
    await held(hack, 70)

    # One paid place: the second payment is too late and goes back.
    tiny = await an_event(club, owner, "250.00", max_participants=1)
    first, first_reg = await guest(tiny)
    second, second_reg = await guest(tiny)
    second_order = await _order(session, second, tiny, second_reg)
    await _pay(session, rzp, first, tiny, first_reg)
    late = rzp.pay(second_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, second_order.razorpay_order_id, late["id"])
    await held(tiny, 35)

    # A free meetup: free stickers, a cap paid for online.
    meet = await an_event(club, owner, "0", merch_enabled=True)
    stickers = shelf_item(session, meet, name="Stickers", price=Decimal("40.00"), stock=50, free_roles=["attendee"])
    cap = shelf_item(session, meet, name="Cap", price=Decimal("120.00"), stock=5)
    session.add_all([stickers, cap])
    await session.commit()
    _, free_reg = await guest(meet)
    await store.place_order(session, meet, free_reg, pick(stickers))
    await session.commit()
    buyer, buyer_reg = await guest(meet)
    await store.place_order(session, meet, buyer_reg, pick(cap))
    await session.commit()
    await _pay(session, rzp, buyer, meet, buyer_reg)

    # Walk-ups paying at the desk, one with a badge from the shelf.
    door = await an_event(club, owner, "350.00", merch_enabled=True)
    door.event_datetime = NOW + 2 * DAY
    badge = shelf_item(session, door, name="Badge", price=Decimal("50.00"), stock=10)
    session.add(badge)
    await session.commit()
    h = auth_header(await mint_token(owner))
    for name, extra in (("Walk One", {}), ("Walk Two", {"store": [{"item_id": str(badge.id), "qty": 1}]})):
        r = await client.post(f"/api/v1/events/{door.id}/gate/walkup", headers=h, json={
            "name": name, "email": f"{name.replace(' ', '').lower()}@example.com", "client_ref": uuid.uuid4().hex, **extra})
        assert r.status_code == 200 and r.json()["outcome"] == "REGISTERED", r.text

    # A kit oversold, handed over once stock came in.
    over = await an_event(club, owner, "100.00", merch_enabled=True)
    kit = shelf_item(session, over, name="Kit", price=Decimal("450.00"), stock=1)
    session.add(kit)
    await session.commit()
    slow, slow_reg = await guest(over)
    quick, quick_reg = await guest(over)
    await store.place_order(session, over, slow_reg, pick(kit))
    await session.commit()
    slow_order = await _order(session, slow, over, slow_reg)
    hold = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == slow_reg.id))
    hold.held_until = NOW - timedelta(minutes=1)
    await session.commit()
    await store.expire_holds(session)
    await store.place_order(session, over, quick_reg, pick(kit))
    await session.commit()
    await _pay(session, rzp, quick, over, quick_reg)
    slow_pay = rzp.pay(slow_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, slow_order.razorpay_order_id, slow_pay["id"])
    kit_row = await session.get(EventStoreItems, kit.id)
    kit_row.stock = 2
    await session.commit()
    await store.fulfil(session, await session.scalar(select(EventStoreOrders).where(
        EventStoreOrders.registration_id == slow_reg.id, EventStoreOrders.status == "oversold")))
    await held(over, 5)

    # The other club: one paid, one refunded.
    gala = await an_event(rival, rival_owner, "900.00")
    for i in range(2):
        payer, reg = await guest(gala)
        _, payment = await _pay(session, rzp, payer, gala, reg)
        if i:
            await refund(payment["id"])
    await held(gala, 3)
    await finance.create_expense(session, club, ExpenseCreate(title="Domain", category="other", amount=Decimal("300.00")), owner)

    # Plans, last: nothing above is gated by them.
    await make_plan(session, key="free")
    growth = await make_plan(session, key="growth", monthly=69900)
    pro = await make_plan(session, key="pro", monthly=149900)
    session.add(ClubSubscriptions(club_id=club.id, plan_id=growth.id, status="active", razorpay_subscription_id="sub_world",
                                  authenticated_at=NOW - 80 * DAY, activated_at=NOW - 80 * DAY,
                                  current_period_end=NOW + 20 * DAY, mrr_paise=69900))
    await session.commit()
    for pay_id, days in (("pay_w1", 70), ("pay_w2", 40), ("pay_w3", 10)):
        await _charged(session, "sub_world", pay_id, when=NOW - days * DAY)
    await _event(session, "refund.processed", {"refund": {"entity": {"id": "rfnd_w2", "payment_id": "pay_w2", "amount": 20000}},
                                               "payment": {"entity": {"id": "pay_w2", "amount_refunded": 20000}}})
    await _event(session, "refund.processed", {"refund": {"entity": {"id": "rfnd_w3", "payment_id": "pay_w3", "amount": 69900}},
                                               "payment": {"entity": {"id": "pay_w3", "amount_refunded": 69900}}})
    await _payment_failed(session, "sub_world", pay_id="pay_w4")
    await _charged(session, "sub_world", "pay_w4", when=NOW - 2 * DAY)
    admin = await make_admin(email="world-admin@example.com")
    r = await client.put(f"/api/v1/admin/subscriptions/club/{rival.id}/plan", headers=auth_header(await mint_token(admin)),
                         json={"plan_id": pro.id, "cycle": "monthly", "reason": "Sponsored"})
    assert r.status_code == 200, r.text
    return dict(club=club, rival=rival, hack=hack, tiny=tiny, meet=meet, door=door, over=over, gala=gala)


@pytest.fixture
async def world(session, client, rzp, make_club, make_event, make_user, make_registration, make_admin, mint_token):
    return await _world(session, client, rzp, make_club, make_event, make_user, make_registration, make_admin, mint_token)


async def test_both_read_paths_give_the_same_numbers(session, world, monkeypatch):
    """Every club's finance page over its whole history, every event's page,
    and the admin's revenue page: worked out from the rows, and read from
    the ledger's sums -- the same, figure for figure."""
    from app.api.finance import service as finance
    from app.api.ledger import compare, reads
    from app.api.subscriptions import revenue

    done = await compare.compare(session, events=True)
    assert done["differences"] == []
    assert done["clubs"] == 2 and done["club_months"] >= 6

    # The pages themselves, both ways, whole.
    club, frm, to = world["club"], (NOW - 100 * DAY).date(), (NOW + 30 * DAY).date()
    pages = {}
    for flag in (False, True):
        with reads.reading(flag):
            summary = await finance.club_summary(session, club, frm, to)
            summary.pop("generated_at")
            hack = await finance.event_summary(session, world["hack"])
            hack.pop("generated_at")
            statement = await finance.club_statement(session, club, frm, to)
            rows = "".join([chunk async for chunk in finance.event_statement(session, world["hack"])])
            money = await revenue.revenue(session, now=NOW)
            pages[flag] = (summary, hack, statement, rows, money)
    assert pages[False] == pages[True]

    # And the figures are what happened (not merely the same twice).
    figures = pages[True][0]["figures"]
    hack_row = next(e for e in pages[True][0]["by_event"] if e["id"] == world["hack"].id)
    # Six paid (one of them twice, the second given back), one refunded with its tee.
    assert hack_row["tickets"] == 499 * 6 and hack_row["merch"] == 350 * 3 and hack_row["refunds"] == 499 + 350 + 499
    assert hack_row["outstanding"] == 499 and hack_row["other_income"] == 5000 and hack_row["expenses"] == 2000
    door_row = next(e for e in pages[True][0]["by_event"] if e["id"] == world["door"].id)
    assert door_row["cash"] == 700 and door_row["merch"] == 50
    assert figures["cash_count"] == 2
    collected = pages[True][4]["collected"]
    assert collected["all_time_paise"] == 69900 * 4 and collected["refunded_paise"] == 20000 + 69900
    assert sum(m["new_paying"] for m in pages[True][4]["series"]) == 1
    assert sum(m["new_complimentary"] for m in pages[True][4]["series"]) == 1


async def test_the_backfill_posts_history_to_the_same_numbers(session, client, rzp, monkeypatch, make_club, make_event, make_user, make_registration, make_admin, mint_token):
    """The same season made before the ledger existed (nothing posted), then
    backfilled: the ledger reads as the rows do, per club and month; and a
    second backfill writes nothing."""
    from app.api.ledger import backfill, compare

    post = book.post

    async def nothing(session, txn):
        return 0

    monkeypatch.setattr(book, "post", nothing)
    world = await _world(session, client, rzp, make_club, make_event, make_user, make_registration, make_admin, mint_token)
    monkeypatch.setattr(book, "post", post)
    assert await _lines(session) == []

    done = await backfill.backfill(session, batch=3)
    assert done["failed"] == 0 and done["registrations"]["busy_skipped"] == 0
    # Every captured or refunded payment, once: 8 for the hackathon (one paid
    # twice), 2 for the one place, 1 cap, 2 kits, 2 at the gala.
    assert done["payments"]["seen"] == 15
    assert done["charges"]["seen"] == 4 and done["comps"]["seen"] == 1
    assert done["registrations"]["lines"] > 0

    result = await compare.compare(session, events=True)
    assert result["differences"] == []
    # Every registration's money is where it should be: nothing unapplied
    # but what is owed back, and nothing owed back here.
    assert await _balance(session, "unapplied", club_id=world["club"].id) == 0
    again = await backfill.backfill(session)
    assert sum(again[k]["lines"] for k in ("payments", "registrations", "charges", "comps")) == 0


async def test_compare_names_a_paid_registration_deleted_afterwards(session, rzp, make_club, make_event, make_user, make_registration):
    """A guest who paid and then deleted their account: the rows forget the
    money (the registration is gone from the counts), the ledger keeps it
    (the club took it). The compare says exactly that, nothing else."""
    from app.api.ledger import compare

    owner, club, event = await _paid_event(make_club, make_event, session, fee="180.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    await _pay(session, rzp, payer, event, reg)
    row = await _reg(session, reg.id)
    row.soft_delete()
    await session.commit()
    diffs = {(d["what"], d["rows"], d["ledger"]) for d in (await compare.compare(session, club_ids=[club.id]))["differences"]}
    assert ("finance.tickets", 0.0, 180.0) in diffs and ("finance.online", 0.0, 180.0) in diffs
    assert {what.split(".")[-1] for what, _, _ in diffs} <= {"tickets", "online", "online_count", "net", "gross"}


# ─── Drift ───────────────────────────────────────────────────────────────


async def test_drift_in_the_sums_is_found_and_put_right(session, rzp, make_club, make_event, make_user, make_registration):
    owner, club, event = await _paid_event(make_club, make_event, session, fee="99.00")
    for _ in range(2):
        payer, reg = await _guest(session, make_user, make_registration, event)
        await _pay(session, rzp, payer, event, reg)
    LM, LP = LedgerMonthly, LedgerPlatformMonthly
    month = book.month_of(NOW)
    # Something other than a posting writes the sums: one row off, one gone, one made up, the platform's off too.
    await session.execute(update(LM).where(LM.event_id == event.id, LM.account == "gateway").values(amount_paise=LM.amount_paise + 777))
    await session.execute(LM.__table__.delete().where(LM.event_id == event.id, LM.account == "tickets_online"))
    session.add(LM(club_id=club.id, event_id=event.id, gateway_id=0, month=month, origin_month=month, account="store", kind="apply",
                   amount_paise=-5000, units=1, entries=1))
    await session.execute(update(LP).where(LP.account == "gateway", LP.kind == "capture").values(units=LP.units + 3))
    await session.commit()

    done = await jobs.reconcile(session)
    assert done["rows_fixed"] == 3 and done["clubs_fixed"] == [club.id] and done["platform_rows_fixed"] >= 1
    [alert] = await _alerts(session, "ledger_drift")
    assert alert.data["clubs"] == [club.id] and alert.data["rows"] == 3
    # Put right: a second look finds nothing (and the autouse check agrees with the entries).
    again = await jobs.reconcile(session)
    assert again["rows_fixed"] == 0 and again["platform_rows_fixed"] == 0
    assert len(await _alerts(session, "ledger_drift")) == 1


# ─── Razorpay's day against the ledger ───────────────────────────────────


class FakeAccount:
    """One Razorpay account as the daily reconciliation reads it: the day's
    payments and refunds listed, a payment fetched (BadRequestError when the
    account does not have it)."""

    def __init__(self, payments=(), refunds=(), extra=()):
        self.payments = {p["id"]: dict(p) for p in payments}
        self.hidden = {p["id"]: dict(p) for p in extra}  # not in the day's listing, but fetchable
        self.refunds = [dict(r) for r in refunds]
        self.calls = []
        self.payment = SimpleNamespace(all=self._payments, fetch=self._fetch)
        self.refund = SimpleNamespace(all=self._refunds)

    def _page(self, items, data):
        self.calls.append(("list", data["from"], data["to"]))
        skip, count = data.get("skip", 0), data.get("count", 100)
        return {"items": items[skip:skip + count]}

    def _payments(self, data):
        return self._page(list(self.payments.values()), data)

    def _refunds(self, data):
        return self._page(self.refunds, data)

    def _fetch(self, payment_id, *args):
        from razorpay.errors import BadRequestError

        self.calls.append(("fetch", payment_id))
        found = self.payments.get(payment_id) or self.hidden.get(payment_id)
        if found is None:
            raise BadRequestError("The id provided does not exist")
        return dict(found)


async def test_the_day_at_razorpay_is_checked_against_the_ledger(session, rzp, monkeypatch, make_club, make_event, make_user, make_registration):
    from app.api.payment_gateways.models import ClubPaymentGateways

    owner, club, event = await _paid_event(make_club, make_event, session, fee="100.00")
    guests = [await _guest(session, make_user, make_registration, event) for _ in range(5)]
    paid = [(await _pay(session, rzp, payer, event, reg))[1] for payer, reg in guests[:4]]
    ok, wrong_amount, gone, partly_refunded = paid
    # Captured at Razorpay, never in our books (its posting lost).
    post = book.post

    async def nothing(session, txn):
        return 0

    monkeypatch.setattr(book, "post", nothing)
    lost_order, lost = await _pay(session, rzp, *guests[4][:1], event, guests[4][1])
    monkeypatch.setattr(book, "post", post)

    listed = [dict(p, status="captured") for p in (ok, lost)]
    listed.append(dict(wrong_amount, status="captured", amount=12000))
    listed.append(dict(partly_refunded, status="captured", amount_refunded=4000))
    listed.append({"id": "pay_not_ours", "order_id": "order_elsewhere", "status": "captured", "amount": 5000})
    listed.append({"id": "pay_sub_x", "invoice_id": "inv_x", "status": "captured", "amount": 69900})  # a subscription's, not in the ledger
    platform = FakeAccount(payments=listed, refunds=[{"id": "rfnd_p", "payment_id": partly_refunded["id"], "amount": 4000, "status": "processed"}])

    # A club's own account: one of its orders paid and posted; the club's other sales left alone; a refund made in its dashboard.
    gateway = ClubPaymentGateways(club_id=club.id, key_id="rzp_live_clubledger", key_secret_enc="x", webhook_secret_enc="x",
                                  webhook_token=f"tok_{uuid.uuid4().hex}", status="active")
    session.add(gateway)
    await session.flush()
    order = PaymentOrders(receipt=f"er_club_{next(_n)}", razorpay_order_id="order_club_1", amount=Decimal("100.00"), status="paid",
                          source="event_registration", payload={"event_registration_id": str(guests[0][1].id)}, payment_gateway_id=gateway.id)
    session.add(order)
    await session.flush()
    log = PaymentLogs(order_id=order.id, razorpay_payment_id="pay_club_1", status=PaymentStatus.captured, amount=Decimal("100.00"))
    session.add(log)
    await session.flush()
    await ledger.capture(session, log, order)
    await session.commit()
    club_account = FakeAccount(
        payments=[{"id": "pay_club_1", "order_id": "order_club_1", "status": "captured", "amount": 10000, "amount_refunded": 10000},
                  {"id": "pay_club_shop", "order_id": "order_club_shop", "status": "captured", "amount": 99900}],
        refunds=[{"id": "rfnd_club", "payment_id": "pay_club_1", "amount": 10000, "status": "processed"}],
    )

    day = datetime.now(book.IST).date()
    accounts = [recon.Account("platform", None, platform), recon.Account(f"club:{club.id}:gateway:{gateway.id}", gateway, club_account)]
    done = await recon.reconcile_day(session, day, pause=0, only=accounts)

    alerts = {a.data["gateway"]: a for a in await _alerts(session, "ledger_mismatch")}
    found = {(m["kind"], m["payment_id"]) for m in alerts["platform"].data["mismatches"]}
    assert found == {
        ("missing_in_ledger", lost["id"]),
        ("amount", wrong_amount["id"]),
        ("missing_at_razorpay", gone["id"]),
        ("refund_missing_in_ledger", partly_refunded["id"]),
        ("missing_in_ledger", "pay_sub_x"),
    }
    partial = next(m for m in alerts["platform"].data["mismatches"] if m["kind"] == "refund_missing_in_ledger")
    assert partial["partial"] is True and partial["razorpay_refunded_paise"] == 4000 and partial["ledger_refunded_paise"] == 0
    assert done["accounts"]["platform"]["other"] == 1 and done["accounts"]["platform"]["mismatches"] == 5
    club_found = [(m["kind"], m["payment_id"]) for m in alerts[f"club:{club.id}:gateway:{gateway.id}"].data["mismatches"]]
    assert club_found == [("refund_missing_in_ledger", "pay_club_1")]
    assert alerts[f"club:{club.id}:gateway:{gateway.id}"].club_id == club.id
    # Only read: listed and fetched, nothing else asked of Razorpay.
    assert {c[0] for c in platform.calls + club_account.calls} <= {"list", "fetch"}

    # The same day again, everything agreeing: no new alert.
    platform.payments.pop(lost["id"])
    platform.payments.pop("pay_sub_x")
    platform.payments[wrong_amount["id"]]["amount"] = 10000
    platform.payments[partly_refunded["id"]]["amount_refunded"] = 0
    platform.refunds = []
    platform.hidden[gone["id"]] = dict(gone, status="captured")
    club_account.payments["pay_club_1"]["amount_refunded"] = 0
    club_account.refunds = []
    alerted = len(await _alerts(session, "ledger_mismatch"))
    quiet = await recon.reconcile_day(session, day + timedelta(days=0), pause=0, only=accounts)
    assert quiet["accounts"]["platform"]["mismatches"] == 0, quiet
    assert len(await _alerts(session, "ledger_mismatch")) == alerted


# ─── The daily job ───────────────────────────────────────────────────────


async def test_the_daily_job_runs_once_a_day_after_its_hour(session, monkeypatch, rzp, make_club, make_event, make_user, make_registration):
    from app.api.models import ScheduledRuns

    ist = book.IST
    today = datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0)
    assert not jobs.due(None, (today + timedelta(hours=2)).astimezone(timezone.utc))
    assert jobs.due(None, (today + timedelta(hours=3)).astimezone(timezone.utc))
    assert not jobs.due(today + timedelta(hours=2, minutes=31), today + timedelta(hours=9))
    assert jobs.due(today - timedelta(hours=20), today + timedelta(hours=3))

    days = []

    async def razorpay_day(session, day=None, **kwargs):
        days.append(day)
        return {"day": day.isoformat(), "accounts": {}}

    monkeypatch.setattr(recon, "reconcile_day", razorpay_day)
    owner, club, event = await _paid_event(make_club, make_event, session, fee="10.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    await _pay(session, rzp, payer, event, reg)

    # Before the history is backfilled, Razorpay is not asked: everything
    # from before the deploy would read as missing.
    at = (today + timedelta(hours=3)).astimezone(timezone.utc)
    done = await jobs.run_if_due(session, now=at)
    assert done is not None and done["reconcile"]["clubs"] >= 1 and done["unbalanced"] == {"unbalanced": 0}
    assert "skipped" in done["razorpay"] and days == []
    assert await jobs.run_if_due(session, now=at + timedelta(hours=1)) is None
    row = await session.get(ScheduledRuns, jobs.JOB)
    assert row.result["watermark"] == int(await session.scalar(select(func.max(LedgerEntries.id))))

    # Backfilled: the next day's run checks the day before at Razorpay.
    from app.api.ledger import backfill

    assert (await backfill.backfill(session))["failed"] == 0 and await backfill.complete(session)
    done = await jobs.run_if_due(session, now=at + DAY)
    assert days == [today.date()] and done["razorpay"] == {"day": today.date().isoformat(), "accounts": {}}

    # Switched off: never due.
    from app.config import settings

    monkeypatch.setattr(settings, "LEDGER_DAILY_AT", "")
    assert not jobs.due(None, at + 2 * DAY)


@pytest.mark.failures_expected
async def test_a_batch_with_a_bad_posting_still_posts_the_rest(session):
    """The backfill's batches: one posting that cannot be written (here, a
    registration id that is no id) costs only itself; an unbalanced one is
    refused before the batch; the same key twice in a batch is one posting."""
    def comp(key, **extra):
        return book.Txn(key=key, kind=book.COMP, lines=[book.Line(book.COMPS, 0, 1)], occurred_at=NOW,
                        source="test", source_id=key, **extra)

    txns = [comp("batch:1"), comp("batch:1"), comp("batch:bad", registration_id="not-a-uuid"), comp("batch:2"),
            book.Txn(key="batch:unbalanced", kind=book.CAPTURE, lines=[book.Line(book.GATEWAY, 5)], occurred_at=NOW,
                     source="test", source_id="batch:unbalanced")]
    assert await book.post_many(session, txns) == 2
    await session.commit()
    assert sorted(x.txn_key for x in await _lines(session)) == ["batch:1", "batch:2"]
    assert {a.data["source_id"] for a in await _alerts(session, "ledger_post_failed")} == {"batch:bad", "batch:unbalanced"}
    # Again: nothing new.
    assert await book.post_many(session, [comp("batch:1"), comp("batch:2")]) == 0


async def test_drift_is_put_right_for_lines_with_no_club_or_event(session):
    """Money with no club (and no event) has its sums too, under club 0."""
    for i in range(2):
        await book.post(session, book.Txn(
            key=f"test:no-club:{i}", kind=book.CAPTURE, lines=[book.Line(book.GATEWAY, 100), book.Line(book.UNAPPLIED, -100)],
            occurred_at=NOW, source="test", source_id=str(i),
        ))
    await session.commit()
    await session.execute(update(LedgerMonthly).where(LedgerMonthly.club_id == 0, LedgerMonthly.account == "gateway")
                          .values(amount_paise=1))
    await session.commit()
    done = await jobs.reconcile(session, clubs=[0], months=[book.month_of(NOW)])
    assert done["rows_fixed"] == 1 and done["clubs_fixed"] == [0]
    row = await session.scalar(select(LedgerMonthly).where(LedgerMonthly.club_id == 0, LedgerMonthly.account == "gateway"))
    assert (row.amount_paise, row.entries) == (200, 2)


async def test_the_backfill_posts_a_deleted_registrations_money_too(session, rzp, monkeypatch, make_club, make_event, make_user, make_registration):
    """A guest who paid and deleted their account before the ledger existed:
    the club took the money, and the backfill posts it."""
    from app.api.ledger import backfill

    post = book.post

    async def nothing(session, txn):
        return 0

    owner, club, event = await _paid_event(make_club, make_event, session, fee="75.00")
    payer, reg = await _guest(session, make_user, make_registration, event)
    monkeypatch.setattr(book, "post", nothing)
    await _pay(session, rzp, payer, event, reg)
    monkeypatch.setattr(book, "post", post)
    (await _reg(session, reg.id)).soft_delete()
    await session.commit()

    done = await backfill.backfill(session)
    assert done["registrations"]["seen"] == 1 and done["payments"]["seen"] == 1
    assert await _balance(session, "tickets_online", registration_id=reg.id) == -7500
    assert await _balance(session, "unapplied", registration_id=reg.id) == 0


async def test_the_backfill_marks_a_subscriptions_first_charge_once(session, rzp, monkeypatch, shop):
    """Charges stored newest first (a backfill from Razorpay's invoices, say):
    the first-charge marker goes straight to the earliest, not once per charge."""
    from app.api.ledger import backfill
    from tests.test_billing_alerts import _paid
    from tests.test_revenue import _charged

    post = book.post

    async def nothing(session, txn):
        return 0

    await _paid(session, shop, sub_id="sub_newest_first")
    monkeypatch.setattr(book, "post", nothing)
    for i, days in enumerate((5, 35, 65)):
        await _charged(session, "sub_newest_first", f"pay_nf_{i}", when=NOW - days * DAY)
    monkeypatch.setattr(book, "post", post)

    await backfill.backfill(session, batch=1)
    marks = await _lines(session, account="subscribers")
    assert [(x.units, x.origin_at) for x in marks] == [(1, (NOW - 65 * DAY).replace(microsecond=0))]
