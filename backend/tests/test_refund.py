"""Covers the new refund flow: an admin-triggered full refund (and the
refund.processed webhook, for refunds initiated directly on Razorpay's
dashboard) must reverse the registration's paid state, which re-blocks
check-in via the existing has_fee/is_paid gate."""
import uuid

from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.payments.service import handle_razorpay_webhook, razorpay_client
from tests.conftest import auth_header


async def _make_captured_payment(session, make_user, make_club, make_event, make_registration):
    club_user, club = await make_club(name="Refund Club", email=f"refundclub{uuid.uuid4()}@example.com")
    event = await make_event(club=club, created_by=club_user, has_fee=True, reg_fee=100.0)
    payer = await make_user(email=f"refundpayer{uuid.uuid4()}@example.com")
    registration = await make_registration(
        event=event, user=payer, actual_amount=100.0, paid_amount=100.0, is_paid=True
    )
    registration.payment_receipt = f"er_{registration.ticket_id}"

    order = PaymentOrders(
        receipt=registration.payment_receipt,
        razorpay_order_id=f"order_{uuid.uuid4()}",
        amount=100.0,
        currency="INR",
        status=OrderStatus.paid,
        source="event_registration",
        payload={},
        user_id=payer.id,
    )
    session.add(order)
    await session.flush()

    payment = PaymentLogs(
        order_id=order.id,
        razorpay_payment_id=f"pay_{uuid.uuid4()}",
        status=PaymentStatus.captured,
        amount=100.0,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)

    return club_user, club, event, registration, order, payment


async def test_admin_refund_blocks_checkin(
    session, client, monkeypatch, make_user, make_club, make_event, make_registration, make_admin, mint_token
):
    club_user, club, event, registration, order, payment = await _make_captured_payment(
        session, make_user, make_club, make_event, make_registration
    )
    admin = await make_admin(email=f"refundadmin{uuid.uuid4()}@example.com")

    monkeypatch.setattr(razorpay_client.payment, "refund", lambda *a, **kw: {"id": "rfnd_test"})

    club_token = await mint_token(club_user)
    add_volunteer = await client.post(
        "/api/v1/events/volunteer/add",
        json={"email_id": club_user.email, "event_id": event.id, "full_name": "Vol Unteer"},
        headers=auth_header(club_token),
    )
    assert add_volunteer.status_code == 200

    refund_response = await client.post(
        f"/api/v1/payments/logs/{payment.id}/refund", headers=auth_header(await mint_token(admin))
    )
    assert refund_response.status_code == 200
    assert refund_response.json()["payment_status"] == "refunded"
    assert refund_response.json()["order_status"] == "refunded"

    await session.refresh(registration)
    assert registration.is_paid is False
    assert float(registration.paid_amount) == 0

    checkin_response = await client.post(
        f"/api/v1/events/volunteer/checkin/{event.id}",
        json={"ticket_id": registration.ticket_id},
        headers=auth_header(club_token),
    )
    assert checkin_response.status_code == 400


async def test_refund_via_webhook_reaches_same_state(
    session, monkeypatch, make_user, make_club, make_event, make_registration
):
    club_user, club, event, registration, order, payment = await _make_captured_payment(
        session, make_user, make_club, make_event, make_registration
    )
    # Razorpay is asked whether it was refunded, and says so.
    monkeypatch.setattr(
        razorpay_client.payment, "fetch",
        lambda payment_id, *a, **kw: {"id": payment_id, "order_id": order.razorpay_order_id, "status": "refunded", "amount": 10000, "amount_refunded": 10000},
    )

    webhook_data = {
        "entity": "event",
        "event": "refund.processed",
        "payload": {
            "refund": {
                "entity": {
                    "id": "rfnd_webhook_test",
                    "payment_id": payment.razorpay_payment_id,
                }
            }
        },
    }

    await handle_razorpay_webhook(session, webhook_data, event_id="evt_refund_1", signature="sig")

    await session.refresh(payment)
    await session.refresh(order)
    await session.refresh(registration)

    assert payment.status.value == "refunded"
    assert order.status.value == "refunded"
    assert registration.is_paid is False
