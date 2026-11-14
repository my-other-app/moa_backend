"""The payment endpoints' edges.

- POST /payments/verify needs no session and asks Razorpay about the
  payment id it is sent: it is limited per order and, at a ceiling, per
  address (429 RATE_LIMITED), so a flood cannot spend the Razorpay quota
  real checkouts need.
- The platform's webhook checks its signature over the raw body before
  anything in the body is read: bad JSON with a bad signature is the
  signature's 400, never a 500. A signed body that is not a JSON object is a
  400. Without Razorpay's event id, the body's hash names the event, so a
  repeated delivery is still applied once.
- An admin refunding a payment already refunded gets 409 ALREADY_REFUNDED,
  whether it was recorded already or Razorpay refuses because another
  refund got there first (recorded here once); a refund Razorpay refuses for
  another reason is a 502 and changes nothing.

Razorpay is a fake throughout (tests.test_money_integrity.FakeRazorpay).
"""
import hashlib
import hmac
import itertools
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.api.events.models import EventRegistrationsLink
from app.api.ledger import book
from app.api.ledger.models import LedgerEntries
from app.api.payments import service as payments
from app.api.payments.models import PaymentLogs, PaymentStatus, RazorpayWebhookLogs
from app.config import settings
from tests.conftest import auth_header
from tests.test_money_integrity import rzp_fake  # noqa: F401

API = "/api/v1"
NOW = datetime.now(timezone.utc)
_n = itertools.count(1)


async def _paid_guest(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name=f"Edges Club {next(_n)}", email=f"edges{next(_n)}@example.com")
    event = await make_event(club=club, created_by=owner, has_fee=True, reg_fee=Decimal("250.00"), event_datetime=NOW + timedelta(days=3))
    payer = await make_user(email=f"edges-guest{next(_n)}@example.com")
    reg = await make_registration(event=event, user=payer, actual_amount=Decimal("250.00"), paid_amount=0)
    await session.commit()
    order = await payments.create_razorpay_order(
        session, source="event_registration",
        payload={"event_id": event.id, "event_registration_id": str(reg.id)}, user_id=payer.id,
    )
    return event, reg, order


def _signed(body: bytes) -> str:
    return hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ─── Verify is limited ───────────────────────────────────────────────────


async def test_verify_is_limited_per_order(client):
    body = {"razorpay_order_id": "order_flooded", "razorpay_payment_id": "pay_x"}
    for _ in range(20):
        r = await client.post(f"{API}/payments/verify", json=body)
        assert r.status_code == 404, r.text
    r = await client.post(f"{API}/payments/verify", json=body)
    assert r.status_code == 429 and r.json()["error_code"] == "RATE_LIMITED", r.text
    # Another checkout from the same address is not held back by it.
    r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": "order_other", "razorpay_payment_id": "pay_x"})
    assert r.status_code == 404, r.text


async def test_verify_has_a_ceiling_per_address(client, monkeypatch):
    from app.api.payments import router as payments_router

    monkeypatch.setattr(payments_router, "VERIFY_PER_ADDRESS", "3/minute")
    for i in range(3):
        r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": f"order_many_{i}", "razorpay_payment_id": "pay_x"})
        assert r.status_code == 404, r.text
    r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": "order_many_x", "razorpay_payment_id": "pay_x"})
    assert r.status_code == 429 and r.json()["error_code"] == "RATE_LIMITED", r.text


async def test_a_real_checkout_still_verifies(client, session, rzp_fake, make_club, make_event, make_user, make_registration):  # noqa: F811
    _, reg, order = await _paid_guest(session, make_club, make_event, make_user, make_registration)
    pay = rzp_fake.pay(order.razorpay_order_id)
    r = await client.post(f"{API}/payments/verify", json={"razorpay_order_id": order.razorpay_order_id, "razorpay_payment_id": pay["id"]})
    assert r.status_code == 200 and r.json()["payment_status"] == "captured", r.text
    paid = await session.scalar(
        select(EventRegistrationsLink.is_paid).where(EventRegistrationsLink.id == reg.id).execution_options(populate_existing=True)
    )
    assert paid is True


async def test_verify_for_a_registration_taken_off_the_list_is_not_a_success(
    client, session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """Checkout shows any 2xx as paid: the guest whose registration went is
    told it did, while the payment is recorded and refunded."""
    from app.api.events.registration.service import take_off_list

    _, reg, order = await _paid_guest(session, make_club, make_event, make_user, make_registration)
    await take_off_list(session, await session.get(EventRegistrationsLink, reg.id))
    pay = rzp_fake.pay(order.razorpay_order_id)
    body = {"razorpay_order_id": order.razorpay_order_id, "razorpay_payment_id": pay["id"]}
    for _ in range(2):
        r = await client.post(f"{API}/payments/verify", json=body)
        assert r.status_code == 409 and r.json()["error_code"] == "REGISTRATION_REMOVED", r.text
    row = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == pay["id"]).execution_options(populate_existing=True))
    assert (row.status, row.refund_reason) == (PaymentStatus.refunded, "registration_removed")
    assert rzp_fake.refunded() == [pay["id"]]


# ─── The webhook's signature comes first ─────────────────────────────────


async def test_an_unsigned_body_is_refused_before_it_is_read(client, session):
    """request.json() ran before the signature check: a body that is not
    JSON answered 500, whoever sent it."""
    for body, signature in ((b"{not json", "forged"), (b"{not json", ""), (b'{"event": "payment.captured"}', "forged")):
        r = await client.post(f"{API}/payments/webhook", content=body, headers={"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": "evt_x"})
        assert r.status_code == 400 and "signature" in r.text.lower(), r.text
    assert await session.scalar(select(func.count()).select_from(RazorpayWebhookLogs)) == 0


async def test_a_signed_body_that_is_not_an_object_is_a_400(client, session):
    for body in (b"{not json", b"[1, 2]", b"\xff\xfe"):
        r = await client.post(f"{API}/payments/webhook", content=body, headers={"X-Razorpay-Signature": _signed(body), "X-Razorpay-Event-Id": "evt_y"})
        assert r.status_code == 400, r.text
    assert await session.scalar(select(func.count()).select_from(RazorpayWebhookLogs)) == 0


async def test_a_signed_capture_without_an_event_id_is_applied_once(
    client, session, rzp_fake, make_club, make_event, make_user, make_registration  # noqa: F811
):
    """Every event without the header shared the idempotency key "": the
    second such event of any kind was dropped as a duplicate of the first."""
    _, reg, order = await _paid_guest(session, make_club, make_event, make_user, make_registration)
    pay = rzp_fake.pay(order.razorpay_order_id)
    body = json.dumps({"entity": "event", "event": "payment.captured", "payload": {"payment": {"entity": pay}}}).encode()
    for _ in range(2):
        r = await client.post(f"{API}/payments/webhook", content=body, headers={"X-Razorpay-Signature": _signed(body)})
        assert r.status_code == 200, r.text
    keys = list((await session.scalars(select(RazorpayWebhookLogs.event_id))).all())
    assert len(keys) == 1 and keys[0].startswith("body:")
    assert [c for c in rzp_fake.calls if c[0] == "payment.fetch"] == [("payment.fetch", pay["id"])]
    assert await session.scalar(select(func.count()).select_from(PaymentLogs).where(PaymentLogs.razorpay_payment_id == pay["id"])) == 1
    paid = await session.scalar(
        select(EventRegistrationsLink.is_paid).where(EventRegistrationsLink.id == reg.id).execution_options(populate_existing=True)
    )
    assert paid is True


# ─── A second refund ─────────────────────────────────────────────────────


async def _captured(session, rzp_fake, make_club, make_event, make_user, make_registration):  # noqa: F811
    _, reg, order = await _paid_guest(session, make_club, make_event, make_user, make_registration)
    pay = rzp_fake.pay(order.razorpay_order_id)
    log = await payments.verify_razorpay_payment(session, order.razorpay_order_id, pay["id"])
    assert log.status == PaymentStatus.captured
    return reg, log, pay


async def _refund_postings(session, payment_id) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(LedgerEntries)
        .where(LedgerEntries.razorpay_payment_id == payment_id, LedgerEntries.kind == book.REFUND, LedgerEntries.account == book.GATEWAY)
    ))


async def test_refunding_a_refunded_payment_again_is_a_409(
    client, session, rzp_fake, make_club, make_event, make_user, make_registration, make_admin, mint_token  # noqa: F811
):
    _, log, pay = await _captured(session, rzp_fake, make_club, make_event, make_user, make_registration)
    h = auth_header(await mint_token(await make_admin(email=f"edges-admin{next(_n)}@example.com")))
    first = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h)
    assert first.status_code == 200 and first.json()["payment_status"] == "refunded", first.text
    again = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h)
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_REFUNDED", again.text
    assert rzp_fake.refunded() == [pay["id"]]


async def test_a_refund_razorpay_refuses_because_another_got_there_first_is_a_409_recorded_once(
    client, session, rzp_fake, make_club, make_event, make_user, make_registration, make_admin, mint_token  # noqa: F811
):
    """Two admins at once: the second's Razorpay call is refused (refunded
    in full already) and was an unhandled 500."""
    from razorpay.errors import BadRequestError

    reg, log, pay = await _captured(session, rzp_fake, make_club, make_event, make_user, make_registration)
    # The other admin's refund went through at Razorpay and is not recorded here yet.
    rzp_fake.payments[pay["id"]].update(status="refunded", amount_refunded=pay["amount"])

    def refused(payment_id, *a, **kw):
        rzp_fake.calls.append(("payment.refund", payment_id))
        raise BadRequestError("The payment has been fully refunded already")

    rzp_fake.payment.refund = refused
    h = auth_header(await mint_token(await make_admin(email=f"edges-admin{next(_n)}@example.com")))
    r = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_REFUNDED", r.text
    row = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log.id).execution_options(populate_existing=True))
    assert row.status == PaymentStatus.refunded
    paid = await session.scalar(
        select(EventRegistrationsLink.is_paid).where(EventRegistrationsLink.id == reg.id).execution_options(populate_existing=True)
    )
    assert paid is False
    assert await _refund_postings(session, pay["id"]) == 1
    # And again: still the 409, still one refund in the ledger.
    r = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h)
    assert r.status_code == 409, r.text
    assert await _refund_postings(session, pay["id"]) == 1


async def test_a_refund_razorpay_refuses_for_another_reason_is_a_502_and_changes_nothing(
    client, session, rzp_fake, make_club, make_event, make_user, make_registration, make_admin, mint_token  # noqa: F811
):
    from razorpay.errors import BadRequestError

    reg, log, pay = await _captured(session, rzp_fake, make_club, make_event, make_user, make_registration)

    def refused(payment_id, *a, **kw):
        raise BadRequestError("Refund amount exceeds the balance")

    rzp_fake.payment.refund = refused
    h = auth_header(await mint_token(await make_admin(email=f"edges-admin{next(_n)}@example.com")))
    r = await client.post(f"{API}/payments/logs/{log.id}/refund", headers=h)
    assert r.status_code == 502 and r.json()["error_code"] == "REFUND_FAILED", r.text
    row = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log.id).execution_options(populate_existing=True))
    assert row.status == PaymentStatus.captured
    paid = await session.scalar(
        select(EventRegistrationsLink.is_paid).where(EventRegistrationsLink.id == reg.id).execution_options(populate_existing=True)
    )
    assert paid is True
    assert await _refund_postings(session, pay["id"]) == 0
