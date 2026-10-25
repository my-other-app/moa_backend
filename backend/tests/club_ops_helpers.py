"""Helpers for the club tools' tests (app/api/club_ops): a team member's
session, an admin acting as the club, and a payment Razorpay captured."""
import itertools
import uuid
from types import SimpleNamespace

from app.api.auth.schemas import AuthTokenData
from app.api.club_access.models import ClubMembers
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.core.auth.authentication import get_password_hash
from app.core.auth.jwt import create_access_token
from tests.conftest import auth_header

_n = itertools.count(1)
MEMBER_PASSWORD = "Member-pass-1"


async def member_header(client, session, club, role: str, email: str | None = None) -> dict:
    """A team member of `club` with `role`, signed in on the dashboard."""
    email = email or f"{role}{next(_n)}-{uuid.uuid4().hex[:6]}@example.com"
    session.add(ClubMembers(
        club_id=club.id, email=email, full_name=f"{role.title()} Member", role=role, status="active",
        password=get_password_hash(MEMBER_PASSWORD),
    ))
    await session.commit()
    r = await client.post("/api/v1/auth/token", data={"username": email, "password": MEMBER_PASSWORD, "scope": "club"})
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"])


async def acting_as(session, owner, admin) -> dict:
    """An admin's session as the club's account (impersonation)."""
    await session.refresh(owner)
    token = create_access_token(data=AuthTokenData(
        user_id=owner.id, token_type="access_token", token_version=owner.token_version, impersonated_by=admin.id,
    ).model_dump())
    return auth_header(token)


async def captured(session, make_user, make_registration, event, *, amount=100.0, gateway_id=None, ticket=None, method="upi"):
    """A guest registered for `event` whose payment of `amount` Razorpay captured."""
    payer = await make_user(email=f"payer{next(_n)}-{uuid.uuid4().hex[:6]}@example.com")
    ticket_amount = amount if ticket is None else ticket
    reg = await make_registration(event=event, user=payer, actual_amount=ticket_amount, paid_amount=ticket_amount, is_paid=True)
    reg.payment_receipt = f"er_{reg.ticket_id}"
    order = PaymentOrders(
        receipt=reg.payment_receipt, razorpay_order_id=f"order_{uuid.uuid4().hex[:14]}", amount=amount, currency="INR",
        status=OrderStatus.paid, source="event_registration", payload={"event_id": event.id, "event_registration_id": str(reg.id)},
        user_id=payer.id, payment_gateway_id=gateway_id,
    )
    session.add(order)
    await session.flush()
    log = PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{uuid.uuid4().hex[:14]}", status=PaymentStatus.captured,
                      amount=amount, payment_method=method)
    session.add(log)
    await session.commit()
    return SimpleNamespace(payer=payer, reg=reg, order=order, log=log)


class FakeRazorpay:
    """One Razorpay account's payment API: refunds, and what it holds."""

    def __init__(self):
        self.refunds: list[tuple] = []
        self.refunded: dict[str, int] = {}
        self.amounts: dict[str, int] = {}
        self.fail_with: Exception | None = None
        self.lose_answer = False
        self.payment = SimpleNamespace(refund=self._refund, fetch=self._fetch)

    def holds(self, log) -> None:
        self.amounts[log.razorpay_payment_id] = int(round(float(log.amount) * 100))

    def _refund(self, payment_id, data=None, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        paise = (data or {}).get("amount") or self.amounts.get(payment_id, 0) - self.refunded.get(payment_id, 0)
        self.refunds.append((payment_id, paise))
        self.refunded[payment_id] = self.refunded.get(payment_id, 0) + paise
        if self.lose_answer:
            raise TimeoutError("Razorpay did not answer")
        return {"id": f"rfnd_{next(_n)}", "payment_id": payment_id, "amount": paise}

    def _fetch(self, payment_id, *args, **kwargs):
        amount = self.amounts.get(payment_id, 0)
        refunded = self.refunded.get(payment_id, 0)
        return {"id": payment_id, "amount": amount, "amount_refunded": refunded,
                "status": "refunded" if amount and refunded >= amount else "captured"}
