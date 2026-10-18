"""Money taken for tickets, exactly and once: paise that survive the trip to
Razorpay, a paid event's last place taken by one capture only, captures
applied in one transaction by whichever path gets there first, webhooks
retried until applied, money not owed given back on its own, and the sweep
that finds what every other path missed. Razorpay is a fake throughout."""
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.api.admin_alerts.models import AdminNotifications
from app.api.events.models import EventRegistrationsLink, Events
from app.api.payments import service as payments
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus, RazorpayWebhookLogs

NOW = datetime.now(timezone.utc)
_n = itertools.count(1)


class FakeRazorpay:
    """The platform's account: orders made on it, payments in it, refunds,
    and whether the caller had a transaction open when it was asked."""

    def __init__(self, session=None):
        self.session = session
        self.orders: dict[str, int] = {}
        self.payments: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.open_transaction_seen: list[str] = []
        self.refund_fails = False
        self.order = SimpleNamespace(create=self._order_create, payments=self._order_payments)
        self.payment = SimpleNamespace(fetch=self._payment_fetch, refund=self._payment_refund)

    def _note(self, name):
        self.calls.append(name)
        if self.session is not None and self.session.in_transaction():
            self.open_transaction_seen.append(name[0])

    def _order_create(self, data=None, **kwargs):
        order_id = f"order_fake_{next(_n)}"
        self._note(("order.create", order_id, data["amount"]))
        self.orders[order_id] = data["amount"]
        return {"id": order_id, "amount": data["amount"], "status": "created"}

    def _order_payments(self, order_id, *args, **kwargs):
        self._note(("order.payments", order_id))
        return {"items": [dict(p) for p in self.payments.values() if p["order_id"] == order_id]}

    def _payment_fetch(self, payment_id, *args, **kwargs):
        self._note(("payment.fetch", payment_id))
        return dict(self.payments[payment_id])

    def _payment_refund(self, payment_id, *args, **kwargs):
        self._note(("payment.refund", payment_id))
        if self.refund_fails:
            raise ConnectionError("Razorpay is down")
        payment = self.payments[payment_id]
        payment["status"], payment["amount_refunded"] = "refunded", payment["amount"]
        return {"id": f"rfnd_{next(_n)}", "payment_id": payment_id}

    def pay(self, order_id, paise=None, *, method="upi", status="captured", **extra) -> dict:
        payment_id = f"pay_fake_{next(_n)}"
        self.payments[payment_id] = {
            "id": payment_id, "order_id": order_id, "status": status, "method": method,
            "amount": self.orders[order_id] if paise is None else paise, "amount_refunded": 0, **extra,
        }
        return self.payments[payment_id]

    def refunded(self):
        return [c[1] for c in self.calls if c[0] == "payment.refund"]


@pytest.fixture
def rzp_fake(session, monkeypatch):
    fake = FakeRazorpay(session)
    monkeypatch.setattr(payments, "razorpay_client", fake)
    # The payer's receipt is a PDF; not what these tests are about.
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kwargs: None)
    return fake


async def _paid_event(make_club, make_event, session, *, fee="100.00", **overrides):
    owner, club = await make_club(name=f"Money Club {next(_n)}", email=f"money{next(_n)}@example.com")
    event = await make_event(club=club, created_by=owner, has_fee=True, reg_fee=Decimal(fee), event_datetime=NOW + timedelta(days=3), **overrides)
    await session.commit()
    return owner, club, event


async def _guest(session, make_user, make_registration, event, *, fee=None):
    payer = await make_user(email=f"guest{next(_n)}@example.com")
    reg = await make_registration(event=event, user=payer, actual_amount=Decimal(fee if fee is not None else event.reg_fee), paid_amount=0)
    await session.commit()
    return payer, reg


async def _order(session, payer, event, reg) -> PaymentOrders:
    return await payments.create_razorpay_order(
        session, source="event_registration",
        payload={"event_id": event.id, "event_registration_id": str(reg.id)}, user_id=payer.id,
    )


async def _reg(session, reg_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id).execution_options(populate_existing=True)
    )


async def _alerts(session, kind):
    return list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == kind))).all())


def _webhook(payment: dict, name="payment.captured") -> dict:
    return {"entity": "event", "event": name, "payload": {"payment": {"entity": dict(payment)}}}


# ─── 1. Paise ────────────────────────────────────────────────────────────


async def test_every_price_to_a_thousand_rupees_reaches_paise_exactly(session):
    """int(float(amount) * 100) charged 4,586 of the prices from ₹0.01 to
    ₹999.99 a paisa short (₹19.99 became 1998 paise)."""
    from app.core import money

    for paise in range(1, 100_000):
        rupees = Decimal(paise) / 100
        assert money.to_paise(rupees) == paise
        assert money.to_paise(float(rupees)) == paise
        assert money.from_paise(paise) == rupees
    assert money.to_paise(Decimal("19.995")) == 2000  # half a paisa rounds up


@pytest.mark.parametrize("fee", ["19.99", "0.29", "1.13", "999.99", "4.35"])
async def test_a_price_float_would_lose_a_paisa_on_is_charged_and_settled_exactly(
    session, rzp_fake, make_club, make_event, make_user, make_registration, fee
):
    _, _, event = await _paid_event(make_club, make_event, session, fee=fee)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    assert rzp_fake.orders[order.razorpay_order_id] == int(Decimal(fee) * 100)
    assert order.amount == Decimal(fee)
    # The same money asked for again reuses the order (compared as Decimals).
    assert (await _order(session, payer, event, reg)).id == order.id

    payment = rzp_fake.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    row = await _reg(session, reg.id)
    assert row.is_paid is True and row.paid_amount == Decimal(fee)


# ─── Methods ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,extra", [
    ("emi", {"emi_plan": {"duration": 6}, "card": {"last4": "4242", "network": "Visa"}}),
    ("cardless_emi", {"provider": "zestmoney"}),
    ("paylater", {"provider": "getsimpl"}),
    ("bank_transfer", {"acquirer_data": {"bank_transaction_id": "UTR123"}}),
    ("card", {"card": {"last4": "1111", "network": "Mastercard", "type": "credit"}}),
])
async def test_a_captured_payment_is_applied_whatever_it_was_paid_with(
    session, rzp_fake, make_club, make_event, make_user, make_registration, method, extra
):
    """Only card, UPI, netbanking and wallet were read: a captured EMI,
    cardless EMI, pay-later or bank-transfer payment raised before it was
    applied, and the guest who paid had no ticket."""
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    payment = rzp_fake.pay(order.razorpay_order_id, method=method, **extra)
    log = await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    assert log.status == PaymentStatus.captured and log.payment_method == method
    assert (await _reg(session, reg.id)).is_paid is True
    if method == "card":
        # The card's details, not the empty dict they used to be read from.
        assert log.payment_details["last4"] == "1111"


# ─── 5. One transaction, and no transaction across Razorpay ─────────────


async def test_nothing_is_held_open_while_razorpay_answers(session, rzp_fake, make_club, make_event, make_user, make_registration):
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    payment = rzp_fake.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    assert [c[0] for c in rzp_fake.calls] == ["order.create", "payment.fetch"]
    assert rzp_fake.open_transaction_seen == []


async def test_a_failure_after_the_capture_leaves_nothing_half_done_and_the_retry_finishes_it(
    session, rzp_fake, make_club, make_event, make_user, make_registration, monkeypatch
):
    """The order was committed paid before the registration was touched: a
    failure in between left a paid order whose guest had no ticket, and
    every retry answered 'already paid'."""
    from app.api.payments import handlers

    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    payment = rzp_fake.pay(order.razorpay_order_id)

    real = handlers.handle_event_registration_payment

    async def broken(*args, **kwargs):
        raise RuntimeError("the database went away")

    order_id, razorpay_order_id, reg_id = order.id, order.razorpay_order_id, reg.id
    monkeypatch.setattr(handlers, "handle_event_registration_payment", broken)
    with pytest.raises(RuntimeError):
        await payments.verify_razorpay_payment(session, razorpay_order_id, payment["id"])
    await session.rollback()
    fresh = await session.scalar(select(PaymentOrders).where(PaymentOrders.id == order_id).execution_options(populate_existing=True))
    assert fresh.status != OrderStatus.paid
    assert await session.scalar(select(PaymentLogs.id).where(PaymentLogs.razorpay_payment_id == payment["id"])) is None

    monkeypatch.setattr(handlers, "handle_event_registration_payment", real)
    log = await payments.verify_razorpay_payment(session, razorpay_order_id, payment["id"])
    assert log.status == PaymentStatus.captured
    assert (await _reg(session, reg_id)).is_paid is True


async def test_verify_and_the_webhook_racing_apply_the_payment_once(session, rzp_fake, make_club, make_event, make_user, make_registration):
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    payment = rzp_fake.pay(order.razorpay_order_id)
    # The webhook's apply inserted the payment's row and has not finished.
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=payment["id"], status=PaymentStatus.created, amount=0))
    await session.commit()
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, payment["id"])
    await payments.handle_razorpay_webhook(session, _webhook(payment), event_id=f"evt_{next(_n)}", signature="sig")
    logs = list((await session.scalars(select(PaymentLogs).where(PaymentLogs.order_id == order.id))).all())
    assert [(log.status, log.amount) for log in logs] == [(PaymentStatus.captured, Decimal("100.00"))]
    assert (await _reg(session, reg.id)).is_paid is True


async def test_a_second_payment_on_a_paid_order_is_recorded_and_refunded(
    session, rzp_fake, make_club, make_event, make_user, make_registration
):
    """It raised 409 ALREADY_PAID and was never recorded: the guest's second
    payment sat in the account with nothing pointing at it."""
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    first = rzp_fake.pay(order.razorpay_order_id)
    second = rzp_fake.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, first["id"])
    log = await payments.verify_razorpay_payment(session, order.razorpay_order_id, second["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.refunded, "duplicate")
    assert rzp_fake.refunded() == [second["id"]]
    # The first stands: the order is paid and so is the ticket.
    assert log.order.status == OrderStatus.paid
    assert (await _reg(session, reg.id)).is_paid is True
    assert len(await _alerts(session, "payment_auto_refunded")) == 1


# ─── Paid places ─────────────────────────────────────────────────────────


async def test_the_last_paid_place_goes_to_one_capture_and_the_other_is_refunded(
    session, rzp_fake, make_club, make_event, make_user, make_registration, monkeypatch
):
    """Capacity was checked without a lock at registration and order time,
    never at capture: everyone at checkout for the last place could pay."""
    from app.api.subscriptions import notices

    sent = []
    monkeypatch.setattr(notices, "_send", lambda to, subject, *a: sent.append((to, subject)))
    _, club, event = await _paid_event(make_club, make_event, session, max_participants=1)
    payer_a, reg_a = await _guest(session, make_user, make_registration, event)
    payer_b, reg_b = await _guest(session, make_user, make_registration, event)
    order_a = await _order(session, payer_a, event, reg_a)
    order_b = await _order(session, payer_b, event, reg_b)  # both at checkout for the one place

    pay_a = rzp_fake.pay(order_a.razorpay_order_id)
    pay_b = rzp_fake.pay(order_b.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order_a.razorpay_order_id, pay_a["id"])
    log_b = await payments.verify_razorpay_payment(session, order_b.razorpay_order_id, pay_b["id"])

    assert (await _reg(session, reg_a.id)).is_paid is True
    late = await _reg(session, reg_b.id)
    assert (late.is_paid, late.paid_amount) == (False, Decimal("0.00"))
    assert (log_b.status, log_b.refund_reason) == (PaymentStatus.refunded, "event_full")
    assert log_b.order.status == OrderStatus.refunded
    assert rzp_fake.refunded() == [pay_b["id"]]
    assert await session.scalar(select(Events.paid_count).where(Events.id == event.id)) == 1
    alerts = await _alerts(session, "payment_auto_refunded")
    assert len(alerts) == 1 and alerts[0].data["reason"] == "event_full" and alerts[0].club_id == club.id
    from app.core import after_commit

    await after_commit.drain()
    assert reg_b.email in [to for to, _ in sent]  # the guest is told why
    assert any("filled up" in subject for _, subject in sent)  # and the organiser


async def test_the_plans_per_event_cap_holds_for_paid_events_at_capture(
    session, rzp_fake, make_club, make_event, make_user, make_registration
):
    """caps = [] for a paid event: the plan's registrations_per_event never
    held the paid places."""
    from app.api.subscriptions.models import PlanFeatures, PlanLimits, Plans

    plan = Plans(key="free", name="Free", price_monthly_paise=0, price_yearly_paise=0)
    session.add(plan)
    await session.flush()
    session.add(PlanLimits(plan_id=plan.id, registrations_per_event=1))
    session.add(PlanFeatures(plan_id=plan.id, tickets=True))
    await session.commit()
    _, _, event = await _paid_event(make_club, make_event, session)
    payer_a, reg_a = await _guest(session, make_user, make_registration, event)
    payer_b, reg_b = await _guest(session, make_user, make_registration, event)
    order_a = await _order(session, payer_a, event, reg_a)
    order_b = await _order(session, payer_b, event, reg_b)
    await payments.verify_razorpay_payment(session, order_a.razorpay_order_id, rzp_fake.pay(order_a.razorpay_order_id)["id"])
    log_b = await payments.verify_razorpay_payment(session, order_b.razorpay_order_id, rzp_fake.pay(order_b.razorpay_order_id)["id"])
    assert log_b.refund_reason == "event_full"
    assert (await _reg(session, reg_b.id)).is_paid is False


async def test_paid_count_is_kept_by_every_writer(session, make_club, make_event, make_user, make_registration):
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)

    async def count():
        return await session.scalar(select(Events.paid_count).where(Events.id == event.id).execution_options(populate_existing=True))

    reg.is_paid = True  # the desk, the organiser marking someone paid
    await session.commit()
    assert await count() == 1
    reg.is_deleted = True
    await session.commit()
    assert await count() == 0
    from app.api.discover import counters

    assert await counters.drift(session) == {"events": [], "clubs": []}
    # Put out by something writing with the triggers off: found, and recounted.
    await session.execute(update(Events.__table__).where(Events.__table__.c.id == event.id).values(paid_count=7))
    assert (await counters.drift(session))["events"] == [event.id]
    await counters.reconcile(session)
    assert await count() == 0


# ─── 4. Webhooks retried until applied ──────────────────────────────────


async def test_a_platform_webhook_that_fails_is_applied_by_razorpays_retry(
    session, rzp_fake, make_club, make_event, make_user, make_registration, monkeypatch
):
    """The idempotency row was committed before the event was applied: a
    failure applying it made Razorpay's retry look already processed."""
    from app.api.payments import handlers

    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    payment = rzp_fake.pay(order.razorpay_order_id)
    real = handlers.handle_event_registration_payment

    async def broken(*args, **kwargs):
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(handlers, "handle_event_registration_payment", broken)
    with pytest.raises(RuntimeError):
        await payments.handle_razorpay_webhook(session, _webhook(payment), event_id="evt_retry_me", signature="sig")
    row = await session.scalar(select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == "evt_retry_me").execution_options(populate_existing=True))
    assert row.processed_at is None and row.claimed_at is None and "deadlock" in row.last_error

    monkeypatch.setattr(handlers, "handle_event_registration_payment", real)
    await payments.handle_razorpay_webhook(session, _webhook(payment), event_id="evt_retry_me", signature="sig")
    assert (await _reg(session, reg.id)).is_paid is True
    row = await session.scalar(select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == "evt_retry_me").execution_options(populate_existing=True))
    assert row.processed_at is not None and row.attempts == 2
    # Applied: a third delivery is the duplicate.
    await payments.handle_razorpay_webhook(session, _webhook(payment), event_id="evt_retry_me", signature="sig")
    assert len([c for c in rzp_fake.calls if c[0] == "payment.fetch"]) == 2


async def test_a_delivery_applying_now_turns_the_next_one_away(session, rzp_fake):
    assert await payments.claim_webhook(session, "evt_busy", entity="event", event="payment.captured", signature="s", payload={}) == "mine"
    assert await payments.claim_webhook(session, "evt_busy", entity="event", event="payment.captured", signature="s", payload={}) == "busy"
    # One that died mid-way is taken over once its claim is old.
    from app.api.payments.models import RazorpayWebhookLogs as Logs

    row = await session.scalar(select(Logs).where(Logs.event_id == "evt_busy"))
    row.claimed_at = NOW - payments.WEBHOOK_LEASE - timedelta(seconds=1)
    await session.commit()
    assert await payments.claim_webhook(session, "evt_busy", entity="event", event="payment.captured", signature="s", payload={}) == "mine"
    await payments.finish_webhook(session, "evt_busy")
    assert await payments.claim_webhook(session, "evt_busy", entity="event", event="payment.captured", signature="s", payload={}) == "duplicate"


async def test_a_club_webhook_that_fails_applying_is_applied_by_the_retry(session, monkeypatch, make_club, make_event, make_user, make_registration):
    from cryptography.fernet import Fernet

    from app.api.payment_gateways import crypto
    from app.api.payment_gateways import service as gateways
    from app.api.payment_gateways.models import ClubPaymentGateways
    from app.api.payments import handlers
    from app.config import settings

    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    account = FakeRazorpay()
    monkeypatch.setattr(gateways, "make_client", lambda key_id, secret: account)
    monkeypatch.setattr(payments, "send_payment_confirmation_email", lambda **kwargs: None)
    _, club, event = await _paid_event(make_club, make_event, session)
    gateway = ClubPaymentGateways(
        club_id=club.id, key_id="rzp_live_ClubXXXX111111", key_secret_enc=crypto.encrypt("secretsecret"),
        webhook_secret_enc=crypto.encrypt("hook"), webhook_token=f"tok{uuid.uuid4().hex}", status="active",
    )
    session.add(gateway)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await payments.create_razorpay_order(
        session, source="event_registration", payload={"event_id": event.id, "event_registration_id": str(reg.id)},
        user_id=payer.id, checkout_version=2,
    )
    assert order.payment_gateway_id == gateway.id
    payment = account.pay(order.razorpay_order_id)
    real = handlers.handle_event_registration_payment

    async def broken(*args, **kwargs):
        raise RuntimeError("connection reset")

    gateway_id, reg_id = gateway.id, reg.id
    monkeypatch.setattr(handlers, "handle_event_registration_payment", broken)
    with pytest.raises(RuntimeError):
        await payments.handle_club_webhook(session, gateway, _webhook(payment), event_id="evt_club_retry", signature="sig")
    monkeypatch.setattr(handlers, "handle_event_registration_payment", real)
    # Razorpay's retry: a new request, the gateway read afresh.
    gateway = await session.get(ClubPaymentGateways, gateway_id, populate_existing=True)
    assert await payments.handle_club_webhook(session, gateway, _webhook(payment), event_id="evt_club_retry", signature="sig") == "captured"
    assert (await _reg(session, reg_id)).is_paid is True


async def test_a_forged_platform_webhook_mints_nothing(session, rzp_fake, make_club, make_event, make_user, make_registration):
    """The platform's webhook trusted its own payload: a leaked webhook
    secret could mark any order paid."""
    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    # Razorpay holds the payment as merely authorized; the webhook claims captured.
    payment = rzp_fake.pay(order.razorpay_order_id, status="authorized")
    await payments.handle_razorpay_webhook(session, _webhook({**payment, "status": "captured"}), event_id=f"evt_{next(_n)}", signature="sig")
    assert (await _reg(session, reg.id)).is_paid is False
    assert ("payment.fetch", payment["id"]) in rzp_fake.calls


async def test_the_app_will_not_start_without_a_webhook_secret(monkeypatch):
    from pydantic import ValidationError

    from app.config import AppConfig

    monkeypatch.setenv("APP_RAZORPAY_WEBHOOK_SECRET", "  ")
    with pytest.raises(ValidationError, match="RAZORPAY_WEBHOOK_SECRET"):
        AppConfig()


# ─── The sweep ───────────────────────────────────────────────────────────


async def test_the_sweep_finds_a_payment_nobody_verified(session, rzp_fake, make_club, make_event, make_user, make_registration):
    """The app was killed after paying and the webhook failed: nothing ever
    asked Razorpay again."""
    from app.api.payments import sweep

    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    rzp_fake.pay(order.razorpay_order_id)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    done = await sweep.reconcile(session, later, pause=0)
    assert done == {"asked": 1, "applied": 1, "failed": 0}
    assert (await _reg(session, reg.id)).is_paid is True
    assert rzp_fake.open_transaction_seen == []
    # Run again: nothing more to do, nothing applied twice.
    assert (await sweep.reconcile(session, later, pause=0))["applied"] == 0


async def test_the_sweep_repairs_a_capture_whose_registration_was_never_paid(
    session, rzp_fake, make_club, make_event, make_user, make_registration
):
    from app.api.payments import sweep

    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    # What a failure after the old commit left: paid and captured, registration unpaid.
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{next(_n)}", status=PaymentStatus.captured, amount=Decimal("100.00"), payment_method="upi"))
    order.status = OrderStatus.paid
    await session.commit()
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert (await sweep.repair(session, later))["repaired"] == 1
    assert (await _reg(session, reg.id)).is_paid is True
    alerts = await _alerts(session, "payment_unapplied")
    assert len(alerts) == 1 and alerts[0].data["repaired"] is True
    assert (await sweep.repair(session, later))["found"] == 0


async def test_an_automatic_refund_razorpay_refused_is_retried(session, rzp_fake, make_club, make_event, make_user, make_registration):
    from app.api.payments import sweep

    _, _, event = await _paid_event(make_club, make_event, session)
    payer, reg = await _guest(session, make_user, make_registration, event)
    order = await _order(session, payer, event, reg)
    first, second = rzp_fake.pay(order.razorpay_order_id), rzp_fake.pay(order.razorpay_order_id)
    await payments.verify_razorpay_payment(session, order.razorpay_order_id, first["id"])
    rzp_fake.refund_fails = True
    log = await payments.verify_razorpay_payment(session, order.razorpay_order_id, second["id"])
    assert (log.status, log.refund_reason) == (PaymentStatus.captured, "duplicate")
    assert len(await _alerts(session, "payment_refund_failed")) == 1
    rzp_fake.refund_fails = False
    assert (await sweep.refunds(session, datetime.now(timezone.utc) + timedelta(minutes=5)))["refunded"] == 1
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log.id).execution_options(populate_existing=True))
    assert log.status == PaymentStatus.refunded


async def test_the_sweep_runs_on_one_worker_every_ten_minutes(session, rzp_fake):
    from app.api.payments import sweep

    now = datetime.now(timezone.utc)
    assert await sweep.run_if_due(session, now) is not None
    assert await sweep.run_if_due(session, now + timedelta(minutes=5)) is None
    assert await sweep.run_if_due(session, now + timedelta(minutes=11)) is not None
