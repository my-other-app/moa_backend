"""Covers the payments fixes: inverted webhook idempotency check, the
missing order/payment cross-check that let a payment for one order be used
to mark a different order paid, and the Float->Numeric precision fix."""
import itertools
from decimal import Decimal

from app.api.payments.service import (
    create_razorpay_order,
    handle_razorpay_webhook,
    verify_razorpay_payment,
    razorpay_client,
)
from app.api.payments.models import PaymentOrders, PaymentLogs, RazorpayWebhookLogs
from app.api.events.models import EventRegistrationsLink
from app.core.validations.exceptions import RequestValidationError
from sqlalchemy import select

_order_counter = itertools.count()


async def _make_paid_registration(session, make_user, make_club, make_event, make_registration):
    club_user, club = await make_club(
        name=f"Payments Club {next(_order_counter)}", email=f"payclub{next(_order_counter)}@example.com"
    )
    event = await make_event(club=club, created_by=club_user, has_fee=True, reg_fee=100.0)
    payer = await make_user(email=f"payer{next(_order_counter)}@example.com")
    registration = await make_registration(
        event=event, user=payer, actual_amount=100.0, paid_amount=0.0
    )
    await session.commit()
    return payer, event, registration


async def test_webhook_replay_is_idempotent(
    session, monkeypatch, make_user, make_club, make_event, make_registration
):
    payer, event, registration = await _make_paid_registration(
        session, make_user, make_club, make_event, make_registration
    )

    def _fake_create(data):
        return {"id": f"order_fake_{next(_order_counter)}"}

    monkeypatch.setattr(razorpay_client.order, "create", _fake_create)

    order = await create_razorpay_order(
        session,
        source="event_registration",
        payload={"event_id": event.id, "event_registration_id": str(registration.id)},
        user_id=payer.id,
    )

    webhook_data = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_replay_test",
                    "order_id": order.razorpay_order_id,
                    "status": "captured",
                    "method": "upi",
                    "amount": 10000,
                    "upi": {},
                }
            }
        },
    }

    await handle_razorpay_webhook(session, webhook_data, event_id="evt_replay_1", signature="sig")
    await handle_razorpay_webhook(session, webhook_data, event_id="evt_replay_1", signature="sig")

    webhook_logs = (
        await session.execute(
            select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == "evt_replay_1")
        )
    ).scalars().all()
    assert len(webhook_logs) == 1

    payment_logs = (
        await session.execute(
            select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == "pay_replay_test")
        )
    ).scalars().all()
    assert len(payment_logs) == 1
    assert payment_logs[0].status.value == "captured"


async def test_verify_rejects_payment_from_a_different_order(
    session, monkeypatch, make_user, make_club, make_event, make_registration
):
    payer_a, event_a, registration_a = await _make_paid_registration(
        session, make_user, make_club, make_event, make_registration
    )
    payer_b, event_b, registration_b = await _make_paid_registration(
        session, make_user, make_club, make_event, make_registration
    )

    def _fake_create(data):
        return {"id": f"order_fake_{next(_order_counter)}"}

    monkeypatch.setattr(razorpay_client.order, "create", _fake_create)

    order_a = await create_razorpay_order(
        session,
        source="event_registration",
        payload={"event_id": event_a.id, "event_registration_id": str(registration_a.id)},
        user_id=payer_a.id,
    )
    order_b = await create_razorpay_order(
        session,
        source="event_registration",
        payload={"event_id": event_b.id, "event_registration_id": str(registration_b.id)},
        user_id=payer_b.id,
    )

    # A real captured payment that actually belongs to order A...
    def _fake_fetch(payment_id, *args, **kwargs):
        return {
            "id": payment_id,
            "order_id": order_a.razorpay_order_id,
            "status": "captured",
            "method": "upi",
            "amount": 10000,
            "upi": {},
        }

    monkeypatch.setattr(razorpay_client.payment, "fetch", _fake_fetch)

    # ...must not be usable to mark order B as paid.
    try:
        await verify_razorpay_payment(
            session,
            razorpay_order_id=order_b.razorpay_order_id,
            razorpay_payment_id="pay_belongs_to_a",
        )
        assert False, "expected RequestValidationError"
    except RequestValidationError:
        pass

    await session.refresh(order_b)
    assert order_b.status.value != "paid"


async def test_create_order_is_idempotent_for_the_same_receipt(
    session, monkeypatch, make_user, make_club, make_event, make_registration
):
    payer, event, registration = await _make_paid_registration(
        session, make_user, make_club, make_event, make_registration
    )

    def _fake_create(data):
        return {"id": f"order_fake_{next(_order_counter)}"}

    monkeypatch.setattr(razorpay_client.order, "create", _fake_create)

    payload = {"event_id": event.id, "event_registration_id": str(registration.id)}
    order_1 = await create_razorpay_order(
        session, source="event_registration", payload=payload, user_id=payer.id
    )
    order_2 = await create_razorpay_order(
        session, source="event_registration", payload=payload, user_id=payer.id
    )

    assert order_1.id == order_2.id

    all_orders = (
        await session.execute(
            select(PaymentOrders).where(PaymentOrders.receipt == order_1.receipt)
        )
    ).scalars().all()
    assert len(all_orders) == 1


async def test_fractional_rupee_amount_survives_exactly(
    session, monkeypatch, make_user, make_club, make_event, make_registration
):
    """Regression test for the Float->Numeric migration: a fractional rupee
    amount (the classic binary-float representation trap) must be stored
    and compared exactly, not drift by a fraction of a paisa."""
    club_user, club = await make_club(
        name=f"Precision Club {next(_order_counter)}", email=f"precisionclub{next(_order_counter)}@example.com"
    )
    event = await make_event(club=club, created_by=club_user, has_fee=True, reg_fee=99.99)
    payer = await make_user(email=f"precisionpayer{next(_order_counter)}@example.com")
    registration = await make_registration(
        event=event, user=payer, actual_amount=99.99, paid_amount=0.0
    )
    await session.commit()

    def _fake_create(data):
        return {"id": f"order_fake_{next(_order_counter)}"}

    monkeypatch.setattr(razorpay_client.order, "create", _fake_create)

    order = await create_razorpay_order(
        session,
        source="event_registration",
        payload={"event_id": event.id, "event_registration_id": str(registration.id)},
        user_id=payer.id,
    )
    assert order.amount == Decimal("99.99")

    webhook_data = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_precision_test",
                    "order_id": order.razorpay_order_id,
                    "status": "captured",
                    "method": "upi",
                    "amount": 9999,  # paise
                    "upi": {},
                }
            }
        },
    }
    await handle_razorpay_webhook(session, webhook_data, event_id="evt_precision_1", signature="sig")

    payment = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == "pay_precision_test")
    )
    assert payment.amount == Decimal("99.99")

    await session.refresh(registration)
    assert registration.paid_amount == Decimal("99.99")
    assert registration.is_paid is True
