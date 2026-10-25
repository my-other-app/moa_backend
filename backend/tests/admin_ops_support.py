"""Shared set-up for the admin tools' tests (tests/test_admin_ops_*.py)."""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.users.models import AdminRoles
from tests.conftest import auth_header


async def role_headers(make_admin, mint_token) -> dict[str, dict]:
    """An auth header for an admin of each role."""
    out = {}
    for role in (AdminRoles.viewer, AdminRoles.support, AdminRoles.owner):
        admin = await make_admin(email=f"{role.value}-{uuid.uuid4().hex[:8]}@example.com", role=role)
        out[role.value] = auth_header(await mint_token(admin))
    return out


async def captured_payment(
    session, *, make_user, make_event, make_registration, club, club_user, amount=500, ticket=None,
    gateway_id=None, status=PaymentStatus.captured, event=None,
):
    """A registration paid in full by one captured Razorpay payment on the
    platform's account (or a club's, with `gateway_id`)."""
    ticket = amount if ticket is None else ticket
    if event is None:
        event = await make_event(club=club, created_by=club_user, has_fee=True, reg_fee=float(ticket))
    payer = await make_user(email=f"payer-{uuid.uuid4().hex[:10]}@example.com")
    reg = await make_registration(
        event=event, user=payer, actual_amount=Decimal(str(ticket)), paid_amount=Decimal(str(ticket)), is_paid=True,
    )
    reg.payment_receipt = f"er_{reg.ticket_id}"
    order = PaymentOrders(
        receipt=reg.payment_receipt, razorpay_order_id=f"order_{uuid.uuid4().hex}", amount=Decimal(str(amount)),
        currency="INR", status=OrderStatus.paid, source="event_registration",
        payload={"event_id": event.id, "event_registration_id": str(reg.id)}, user_id=payer.id,
        payment_gateway_id=gateway_id,
    )
    session.add(order)
    await session.flush()
    log = PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{uuid.uuid4().hex[:14]}", status=status,
                      amount=Decimal(str(amount)))
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return event, reg, order, log


class FakeRefunds:
    """Razorpay's payment.refund, answered as Razorpay would; the calls kept."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple] = []
        self.fail = fail

    def __call__(self, payment_id, data=None, **kwargs):
        self.calls.append((payment_id, data))
        if self.fail is not None:
            raise self.fail
        return {"id": f"rfnd_{uuid.uuid4().hex[:12]}", "payment_id": payment_id, "amount": (data or {}).get("amount")}
