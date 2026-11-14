"""A registration's money counted across all of its orders, not one order at a time.

- A changed amount (picks, a discount, the ticket type) or a changed
  Razorpay account (a club connecting or dropping its own) makes a new order
  for the same registration, and a paid ticket may still open a store-only
  order for picks added later. The registration's paid state comes from all
  of them: a store-only order paid after the ticket never sets the ticket
  back to unpaid or gives its place away.
- A payment on another order of a registration already paid in full bought
  nothing: it is recorded, refunded in full through the account that took
  it (`already_paid`), and the admins and the organiser are told. The
  payment that paid the ticket stands.
- A payment for a registration taken off the list while its guest was
  paying is recorded against it, refunded in full (`registration_removed`)
  and told; the webhook that brought it is done with only once that is
  recorded.
- The same payment arriving again (a second webhook, a verify, the sweep)
  changes nothing and refunds nothing twice.

Razorpay is a fake throughout (tests.test_money_integrity.FakeRazorpay).
"""
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.admin_alerts.models import AdminNotifications
from app.api.events.models import EventRegistrationsLink, Events
from app.api.ledger import book
from app.api.ledger.models import LedgerEntries
from app.api.payments import service as payments
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus, RazorpayWebhookLogs
from app.api.store import service as store
from app.api.store.models import EventStoreOrders
from tests.store_items import shelf_item
from tests.test_money_integrity import FakeRazorpay, rzp_fake  # noqa: F401

NOW = datetime.now(timezone.utc)
_n = itertools.count(1)


@pytest.fixture
def mails(monkeypatch):
    """What notices sends (the guest's and the organiser's mails), after the commit."""
    from app.api.subscriptions import notices

    sent = []
    monkeypatch.setattr(notices, "_send", lambda to, subject, lines, *a: sent.append((to, subject, " ".join(lines))))
    return sent


async def _drained(mails):
    from app.core import after_commit

    await after_commit.drain()
    return mails


async def _world(session, make_club, make_event, make_user, make_registration, *, fee="500.00", merch=False):
    owner, club = await make_club(name=f"Per Reg Club {next(_n)}", email=f"perreg{next(_n)}@example.com")
    event = await make_event(
        club=club, created_by=owner, name=f"Paid Night {next(_n)}", has_fee=True, reg_fee=Decimal(fee),
        event_datetime=NOW + timedelta(days=5), merch_enabled=merch,
    )
    payer = await make_user(email=f"perreg-guest{next(_n)}@example.com")
    reg = await make_registration(event=event, user=payer, actual_amount=Decimal(fee), paid_amount=0)
    tee = shelf_item(session, event, name="Tee", price=Decimal("300.00"), stock=5) if merch else None
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, payer=payer, reg=reg, tee=tee)


async def _order(session, w, **kwargs) -> PaymentOrders:
    return await payments.create_razorpay_order(
        session, source="event_registration",
        payload={"event_id": w.event.id, "event_registration_id": str(w.reg.id)}, user_id=w.payer.id, **kwargs,
    )


async def _pick_tee(session, w, qty=1):
    reg = await session.get(EventRegistrationsLink, w.reg.id)
    event = await session.get(Events, w.event.id)
    order = await store.place_order(session, event, reg, [SimpleNamespace(item_id=w.tee.id, variant_id=None, qty=qty)])
    await session.commit()
    return order


async def _reg(session, reg_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id)
        .execution_options(populate_existing=True, include_deleted=True)
    )


async def _log(session, payment_id) -> PaymentLogs:
    return await session.scalar(
        select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment_id).execution_options(populate_existing=True)
    )


async def _order_row(session, order_id) -> PaymentOrders:
    return await session.scalar(select(PaymentOrders).where(PaymentOrders.id == order_id).execution_options(populate_existing=True))


async def _store_statuses(session, reg_id) -> list[str]:
    return list((await session.scalars(
        select(EventStoreOrders.status).where(EventStoreOrders.registration_id == reg_id)
        .order_by(EventStoreOrders.created_at).execution_options(populate_existing=True)
    )).all())


async def _paid_count(session, event_id) -> int:
    return await session.scalar(select(Events.paid_count).where(Events.id == event_id).execution_options(populate_existing=True))


async def _alerts(session, kind):
    return list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == kind))).all())


async def _unbalanced(session) -> list:
    E = LedgerEntries
    return (await session.execute(select(E.txn_key).group_by(E.txn_key).having(func.sum(E.amount_paise) != 0))).all()


async def _gateway_paise(session, payment_id: str) -> int:
    E = LedgerEntries
    return int(await session.scalar(
        select(func.coalesce(func.sum(E.amount_paise), 0)).where(E.razorpay_payment_id == payment_id, E.account == book.GATEWAY)
    ))


def _webhook(payment: dict, name="payment.captured") -> dict:
    return {"entity": "event", "event": name, "payload": {"payment": {"entity": dict(payment)}}}


# ─── 1. Counted across the registration's orders ─────────────────────────


async def test_a_store_order_paid_after_the_ticket_keeps_the_ticket_paid(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """The ticket's checkout opened, a tee was added from the shelf, the
    ticket's order was paid, then the store-only order: its capture summed
    only its own order, took the tee's share off it, and set the paid ticket
    back to unpaid -- freeing its paid place and turning the guest away at
    the door."""
    w = await _world(session, make_club, make_event, make_user, make_registration, merch=True)
    ticket_order = await _order(session, w)
    assert ticket_order.amount == Decimal("500.00")
    await _pick_tee(session, w)
    ticket_pay = rzp_fake.pay(ticket_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, ticket_order.razorpay_order_id, ticket_pay["id"])
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))
    assert await _store_statuses(session, w.reg.id) == ["pending"]

    store_order = await _order(session, w)
    assert store_order.id != ticket_order.id and store_order.amount == Decimal("300.00")
    store_pay = rzp_fake.pay(store_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, store_order.razorpay_order_id, store_pay["id"])

    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))
    assert await _store_statuses(session, w.reg.id) == ["paid"]
    assert await _paid_count(session, w.event.id) == 1
    assert rzp_fake.refunded() == []
    assert await _alerts(session, "payment_overpaid") == []

    # Applied again (a late webhook for each, the repair sweep): nothing moves.
    for order, pay in ((ticket_order, ticket_pay), (store_order, store_pay)):
        await payments.handle_razorpay_webhook(session, _webhook(pay), event_id=f"evt_{next(_n)}", signature="sig")
        await payments.ensure_applied(session, order.id)
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))
    assert await _store_statuses(session, w.reg.id) == ["paid"]
    assert await _paid_count(session, w.event.id) == 1
    assert rzp_fake.refunded() == []
    assert await _unbalanced(session) == []


async def test_the_tickets_payment_arriving_again_does_not_buy_picks_added_since(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """Applied again by its own order alone, the ticket's payment covered a
    tee picked after it was made, so the tee was 'paid' with the ticket's money."""
    w = await _world(session, make_club, make_event, make_user, make_registration, merch=True)
    ticket_order = await _order(session, w)
    ticket_pay = rzp_fake.pay(ticket_order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, ticket_order.razorpay_order_id, ticket_pay["id"])
    await _pick_tee(session, w)

    # Razorpay's webhook for the same payment, then the app's verify again.
    await payments.handle_razorpay_webhook(session, _webhook(ticket_pay), event_id=f"evt_{next(_n)}", signature="sig")
    await payments.verify_razorpay_payment(session, ticket_order.razorpay_order_id, ticket_pay["id"])
    assert await _store_statuses(session, w.reg.id) == ["pending"]
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))


async def test_payments_on_two_orders_add_up_to_the_ticket(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """The price went up between two checkouts (another ticket type): the
    first order's money and the rest, paid on a second order, are one ticket."""
    w = await _world(session, make_club, make_event, make_user, make_registration)
    first = await _order(session, w)
    first_pay = rzp_fake.pay(first.razorpay_order_id)
    reg = await _reg(session, w.reg.id)
    reg.actual_amount = Decimal("700.00")
    await session.commit()
    await payments.verify_razorpay_payment(session, first.razorpay_order_id, first_pay["id"])
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (False, Decimal("500.00"))

    rest = await _order(session, w)
    assert rest.amount == Decimal("200.00")
    await payments.verify_razorpay_payment(session, rest.razorpay_order_id, rzp_fake.pay(rest.razorpay_order_id)["id"])
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("700.00"))
    assert rzp_fake.refunded() == []


async def test_money_beyond_what_was_owed_is_kept_with_what_it_bought_and_told(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """The tee was dropped after the ₹800 checkout opened, and the ₹800 order
    was paid anyway: the ticket is paid, and the admins hear of the ₹300."""
    w = await _world(session, make_club, make_event, make_user, make_registration, merch=True)
    await _pick_tee(session, w)
    with_tee = await _order(session, w)
    assert with_tee.amount == Decimal("800.00")
    reg = await session.get(EventRegistrationsLink, w.reg.id)
    await store.place_order(session, await session.get(Events, w.event.id), reg, [])
    await session.commit()
    pay = rzp_fake.pay(with_tee.razorpay_order_id)
    log = await payments.verify_razorpay_payment(session, with_tee.razorpay_order_id, pay["id"])
    assert log.status == PaymentStatus.captured and log.refund_reason is None
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))
    [alert] = await _alerts(session, "payment_overpaid")
    assert alert.data["excess"] == "300.00" and alert.data["payment_id"] == pay["id"] and alert.club_id == w.club.id
    assert rzp_fake.refunded() == []


# ─── 2. A payment on another order, for a ticket paid in full ────────────


async def test_a_second_orders_payment_for_a_paid_ticket_is_refunded_and_told(
    session, rzp_fake, make_club, make_event, make_user, make_registration, mails  # noqa: F811
):
    """A discount code made a new order; the first order's UPI collect was
    approved late. Its capture found the ticket paid and was kept: no
    refund, no mark, no alert."""
    w = await _world(session, make_club, make_event, make_user, make_registration)
    first = await _order(session, w)
    reg = await _reg(session, w.reg.id)
    reg.actual_amount = Decimal("400.00")  # the code, applied between the two checkouts
    await session.commit()
    second = await _order(session, w)
    assert second.id != first.id and second.amount == Decimal("400.00")
    kept = rzp_fake.pay(second.razorpay_order_id)
    await payments.verify_razorpay_payment(session, second.razorpay_order_id, kept["id"])
    late = rzp_fake.pay(first.razorpay_order_id)

    log = await payments.verify_razorpay_payment(session, first.razorpay_order_id, late["id"])

    assert (log.status, log.refund_reason) == (PaymentStatus.refunded, "already_paid")
    assert log.amount == Decimal("500.00")
    assert rzp_fake.refunded() == [late["id"]]
    assert (await _log(session, kept["id"])).status == PaymentStatus.captured
    assert (await _order_row(session, first.id)).status == OrderStatus.refunded
    assert (await _order_row(session, second.id)).status == OrderStatus.paid
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("400.00"))
    assert await _paid_count(session, w.event.id) == 1
    [alert] = await _alerts(session, "payment_auto_refunded")
    assert alert.data["reason"] == "already_paid" and alert.data["payment_id"] == late["id"] and alert.club_id == w.club.id
    sent = await _drained(mails)
    assert w.reg.email in [to for to, *_ in sent]
    assert w.owner.email in [to for to, *_ in sent]
    # Taken and given back: in the ledger both ways.
    assert await _gateway_paise(session, late["id"]) == 0
    assert await _unbalanced(session) == []

    # It arrives again -- Razorpay's webhook, the app's verify, the sweep: nothing more goes back.
    await payments.handle_razorpay_webhook(session, _webhook(late), event_id=f"evt_{next(_n)}", signature="sig")
    await payments.verify_razorpay_payment(session, first.razorpay_order_id, late["id"])
    from app.api.payments import sweep

    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert (await sweep.refunds(session, later))["due"] == 0
    assert (await sweep.reconcile(session, later, pause=0))["applied"] == 0
    assert rzp_fake.refunded() == [late["id"]]
    assert len(await _alerts(session, "payment_auto_refunded")) == 1
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("400.00"))


async def test_a_store_only_orders_payment_with_nothing_left_to_buy_goes_back(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """A store-only order paid twice over: the first payment buys the tee,
    the late one on the other order for the same tee buys nothing."""
    w = await _world(session, make_club, make_event, make_user, make_registration, merch=True)
    ticket = await _order(session, w)
    await payments.verify_razorpay_payment(session, ticket.razorpay_order_id, rzp_fake.pay(ticket.razorpay_order_id)["id"])
    await _pick_tee(session, w)
    one_tee = await _order(session, w)
    await _pick_tee(session, w, qty=2)  # changed to two: another order
    two_tees = await _order(session, w)
    assert (one_tee.amount, two_tees.amount) == (Decimal("300.00"), Decimal("600.00"))
    await payments.verify_razorpay_payment(session, two_tees.razorpay_order_id, rzp_fake.pay(two_tees.razorpay_order_id)["id"])
    late = rzp_fake.pay(one_tee.razorpay_order_id)
    log = await payments.verify_razorpay_payment(session, one_tee.razorpay_order_id, late["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.refunded, "already_paid")
    assert rzp_fake.refunded() == [late["id"]]
    assert await _store_statuses(session, w.reg.id) == ["cancelled", "paid"]
    reg = await _reg(session, w.reg.id)
    assert (reg.is_paid, reg.paid_amount) == (True, Decimal("500.00"))


async def test_a_payment_on_the_clubs_account_for_a_ticket_paid_on_ours_goes_back_through_the_clubs(
    session, rzp_fake, monkeypatch, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """The club dropped its own Razorpay between two checkouts: the ticket
    was paid on the platform's order, then the club account's order was paid
    too. That one is refunded by the club's own account, never ours."""
    from cryptography.fernet import Fernet

    from app.api.payment_gateways import crypto
    from app.api.payment_gateways import service as gateways
    from app.api.payment_gateways.models import ClubPaymentGateways
    from app.config import settings

    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    account = FakeRazorpay()
    monkeypatch.setattr(gateways, "make_client", lambda key_id, secret: account)
    w = await _world(session, make_club, make_event, make_user, make_registration)
    gateway = ClubPaymentGateways(
        club_id=w.club.id, key_id="rzp_live_ClubYYYY222222", key_secret_enc=crypto.encrypt("secretsecret"),
        webhook_secret_enc=crypto.encrypt("hook"), webhook_token=f"tok{uuid.uuid4().hex}", status="active",
    )
    session.add(gateway)
    await session.commit()
    on_club = await _order(session, w, checkout_version=2)
    assert on_club.payment_gateway_id == gateway.id
    gateway.status = "disconnected"
    await session.commit()
    on_platform = await _order(session, w, checkout_version=2)
    assert on_platform.payment_gateway_id is None and on_platform.id != on_club.id
    await payments.verify_razorpay_payment(
        session, on_platform.razorpay_order_id, rzp_fake.pay(on_platform.razorpay_order_id)["id"]
    )

    late = account.pay(on_club.razorpay_order_id)
    gateway = await session.get(ClubPaymentGateways, gateway.id, populate_existing=True)
    outcome = await payments.handle_club_webhook(session, gateway, _webhook(late), event_id="evt_club_late", signature="sig")

    assert outcome == "refunded"
    assert account.refunded() == [late["id"]] and rzp_fake.refunded() == []
    log = await _log(session, late["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.refunded, "already_paid")
    reg = await _reg(session, w.reg.id)
    assert reg.is_paid is True
    [alert] = await _alerts(session, "payment_auto_refunded")
    assert alert.data["gateway"] == "club" and alert.club_id == w.club.id
    # The same event again is a duplicate; a different event for it changes nothing.
    gateway = await session.get(ClubPaymentGateways, gateway.id, populate_existing=True)
    assert await payments.handle_club_webhook(session, gateway, _webhook(late), event_id="evt_club_late", signature="sig") == "duplicate"
    gateway = await session.get(ClubPaymentGateways, gateway.id, populate_existing=True)
    await payments.handle_club_webhook(session, gateway, _webhook(late, "order.paid"), event_id="evt_club_late_2", signature="sig")
    assert account.refunded() == [late["id"]]


# ─── 3. A registration taken off the list while its guest was paying ─────


async def test_a_payment_for_a_registration_taken_off_the_list_is_recorded_refunded_and_told(
    session, rzp_fake, make_club, make_event, make_user, make_registration, mails  # noqa: F811
):
    """Its capture looked up live registrations only: 400 'receipt is
    invalid', the webhook marked done, nothing recorded, nothing refunded,
    nobody told -- the guest had neither ticket nor money."""
    from app.api.events.registration.service import take_off_list

    w = await _world(session, make_club, make_event, make_user, make_registration)
    order = await _order(session, w)
    await take_off_list(session, await session.get(EventRegistrationsLink, w.reg.id))
    pay = rzp_fake.pay(order.razorpay_order_id)

    await payments.handle_razorpay_webhook(session, _webhook(pay), event_id="evt_removed", signature="sig")

    row = await session.scalar(select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == "evt_removed").execution_options(populate_existing=True))
    assert row.processed_at is not None and row.last_error is None
    log = await _log(session, pay["id"])
    assert (log.status, log.refund_reason, log.amount) == (PaymentStatus.refunded, "registration_removed", Decimal("500.00"))
    assert log.order_id == order.id
    assert rzp_fake.refunded() == [pay["id"]]
    assert (await _order_row(session, order.id)).status == OrderStatus.refunded
    reg = await _reg(session, w.reg.id)
    assert (reg.is_deleted, reg.is_paid) == (True, False)
    assert await _paid_count(session, w.event.id) == 0
    [alert] = await _alerts(session, "payment_auto_refunded")
    assert alert.data["reason"] == "registration_removed" and alert.club_id == w.club.id
    assert alert.data["registration_id"] == str(w.reg.id)
    sent = await _drained(mails)
    assert w.reg.email in [to for to, *_ in sent] and w.owner.email in [to for to, *_ in sent]
    # In the ledger against the registration, taken and given back.
    registrations = set((await session.scalars(
        select(LedgerEntries.registration_id).where(LedgerEntries.razorpay_payment_id == pay["id"])
    )).all())
    assert registrations == {w.reg.id}
    assert await _gateway_paise(session, pay["id"]) == 0
    assert await _unbalanced(session) == []

    # Again: the same delivery, another event for it, the app's verify.
    await payments.handle_razorpay_webhook(session, _webhook(pay), event_id="evt_removed", signature="sig")
    await payments.handle_razorpay_webhook(session, _webhook(pay, "order.paid"), event_id="evt_removed_2", signature="sig")
    again = await payments.verify_razorpay_payment(session, order.razorpay_order_id, pay["id"])
    assert again.status == PaymentStatus.refunded
    assert rzp_fake.refunded() == [pay["id"]]
    assert len(await _alerts(session, "payment_auto_refunded")) == 1


async def test_a_refund_razorpay_will_not_take_yet_is_still_recorded_before_the_webhook_is_done(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    from app.api.events.registration.service import take_off_list
    from app.api.payments import sweep

    w = await _world(session, make_club, make_event, make_user, make_registration)
    order = await _order(session, w)
    await take_off_list(session, await session.get(EventRegistrationsLink, w.reg.id))
    pay = rzp_fake.pay(order.razorpay_order_id)
    rzp_fake.refund_fails = True

    await payments.handle_razorpay_webhook(session, _webhook(pay), event_id="evt_removed_down", signature="sig")

    log = await _log(session, pay["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.captured, "registration_removed")
    assert log.refund_requested_at is not None
    [failed] = await _alerts(session, "payment_refund_failed")
    assert failed.club_id == w.club.id
    rzp_fake.refund_fails = False
    assert (await sweep.refunds(session, datetime.now(timezone.utc) + timedelta(minutes=5)))["refunded"] == 1
    assert (await _log(session, pay["id"])).status == PaymentStatus.refunded
    # Never counted as a capture to repair.
    assert (await sweep.repair(session, datetime.now(timezone.utc) + timedelta(minutes=10)))["found"] == 0


async def test_the_sweep_finds_and_refunds_a_payment_for_a_removed_registration(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """Neither verify nor the webhook landed: the reconcile sweep finds the
    capture at Razorpay and gives it back."""
    from app.api.events.registration.service import take_off_list
    from app.api.payments import sweep

    w = await _world(session, make_club, make_event, make_user, make_registration)
    order = await _order(session, w)
    await take_off_list(session, await session.get(EventRegistrationsLink, w.reg.id))
    pay = rzp_fake.pay(order.razorpay_order_id)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert (await sweep.reconcile(session, later, pause=0))["applied"] == 1
    log = await _log(session, pay["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.refunded, "registration_removed")
    assert rzp_fake.refunded() == [pay["id"]]
    assert (await sweep.reconcile(session, later, pause=0))["applied"] == 0


async def test_a_paid_guest_taken_off_the_list_keeps_their_payment_when_it_arrives_again(
    session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """Paid, then taken off the list by the organiser: a late webhook for the
    payment that paid the ticket refunds nothing on its own (and no longer
    answers 400)."""
    from app.api.events.registration.service import take_off_list

    w = await _world(session, make_club, make_event, make_user, make_registration)
    order = await _order(session, w)
    pay = rzp_fake.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, pay["id"])
    await take_off_list(session, await session.get(EventRegistrationsLink, w.reg.id))
    await payments.handle_razorpay_webhook(session, _webhook(pay), event_id=f"evt_{next(_n)}", signature="sig")
    log = await payments.verify_razorpay_payment(session, order.razorpay_order_id, pay["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.captured, None)
    assert rzp_fake.refunded() == []
