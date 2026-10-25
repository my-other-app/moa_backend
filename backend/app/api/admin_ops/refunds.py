"""Refunds an admin makes beyond a ticket payment in full.

- Part of a ticket: some of what the guest paid for the ticket goes back
  and the ticket stands (a discount after the fact). The registration's
  paid amount and its price both come down by it, so nothing is shown as
  owed afterwards.
- One store order on the payment: its total goes back, the order is
  refunded and its pieces return to the shelf (unless handed over).
- What is left of the payment: the whole remainder, which ends the ticket
  as the existing full refund does (payments.service._apply_refund).
- A plan subscription's charge, in full or in part.

Each goes to Razorpay through the account the payment was taken on, with
no transaction open while it answers; nothing is changed unless Razorpay
took the refund. What changed is posted to the money ledger in the same
transaction (ledger.service.partial_refund, sync_registration) and written
to the admin audit log. MANAGE_MONEY for every refund; any admin may look.

The `refund.processed` webhook that follows a partial refund changes
nothing: a ticket payment is only taken back by it once Razorpay holds it
refunded in full, and a subscription charge's running total is the same
one this already wrote.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.api.admin_ops.models import PaymentRefunds
from app.api.audit import service as audit
from app.api.ledger import book as ledger_book
from app.api.ledger import service as ledger
from app.api.payments import rzp
from app.api.payments.models import PaymentLogs, PaymentStatus
from app.api.store.models import EventStoreOrders
from app.api.users.models import Users
from app.core import money
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import MoneyAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/refunds", tags=["admin-refunds"])

TAKEN = ("paid", "oversold")


def _f(x) -> float:
    return float(money.rupees(x))


def _iso(at):
    return at.isoformat() if at else None


class PaymentRefundIn(BaseModel):
    kind: Literal["ticket", "store_order", "rest"]
    #: Rupees, for part of the ticket.
    amount: Optional[Decimal] = Field(None, gt=0, max_digits=10, decimal_places=2)
    store_order_id: Optional[UUID] = None
    reason: str = Field(..., min_length=3, max_length=300)


class ChargeRefundIn(BaseModel):
    #: Rupees; unset, all that is left.
    amount: Optional[Decimal] = Field(None, gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(..., min_length=3, max_length=300)


async def _log(session, log_id, *, lock: bool = False) -> PaymentLogs:
    q = select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order)).execution_options(populate_existing=True)
    if lock:
        q = q.with_for_update(of=PaymentLogs)
    log = await session.scalar(q)
    if log is None:
        raise CustomHTTPException(404, message="Payment not found", error_code="PAYMENT_NOT_FOUND")
    return log


async def refunded_so_far(session, log_id) -> Decimal:
    """What is given back of a payment in parts, or spoken for: an admin's
    (payment_refunds) and the club's own (club_refunds: done, or still
    being asked of Razorpay)."""
    from app.api.club_ops.models import PENDING, PROCESSING, SUCCEEDED, ClubRefunds

    admin = await session.scalar(
        select(func.coalesce(func.sum(PaymentRefunds.amount), 0)).where(PaymentRefunds.payment_log_id == log_id)
    )
    club = await session.scalar(
        select(func.coalesce(func.sum(ClubRefunds.amount), 0)).where(
            ClubRefunds.payment_log_id == log_id, ClubRefunds.status.in_((PENDING, PROCESSING, SUCCEEDED))
        )
    )
    return money.rupees(admin) + money.rupees(club)


def _order_line(o: EventStoreOrders) -> str:
    return ", ".join(f"{line.qty} × {line.item_name}{f' ({line.variant_label})' if line.variant_label else ''}" for line in (o.lines or []))


async def _names(session, ids: set) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict((await session.execute(select(Users.id, Users.full_name).where(Users.id.in_(ids)).execution_options(include_deleted=True))).all())


async def payment_detail(session, log_id) -> dict:
    from app.api.payments.handlers import registration_for_order

    log = await _log(session, log_id)
    reg = await registration_for_order(session, log.order, include_deleted=True)
    done = await refunded_so_far(session, log.id)
    amount = money.rupees(log.amount)
    refundable = amount - done if log.status == PaymentStatus.captured else Decimal(0)
    orders = []
    if reg is not None:
        rows = (await session.scalars(
            select(EventStoreOrders).where(EventStoreOrders.registration_id == reg.id, EventStoreOrders.status.in_(("paid", "oversold", "refunded")))
            .order_by(EventStoreOrders.created_at)
        )).all()
        orders = [{"id": str(o.id), "status": o.status, "total": _f(o.total), "summary": _order_line(o),
                   "handed_over": o.handed_over_at is not None,
                   "refundable": o.status in TAKEN and log.status == PaymentStatus.captured and money.rupees(o.total) <= refundable}
                  for o in rows]
    refunds = (await session.scalars(
        select(PaymentRefunds).where(PaymentRefunds.payment_log_id == log.id).order_by(PaymentRefunds.created_at)
    )).all()
    names = await _names(session, {r.created_by for r in refunds})
    return {
        "payment": {
            "id": str(log.id), "razorpay_payment_id": log.razorpay_payment_id,
            "status": getattr(log.status, "value", log.status), "amount": _f(amount),
            "refunded": _f(amount if log.status == PaymentStatus.refunded else done), "refundable": _f(refundable),
            "gateway": "club" if log.order and log.order.payment_gateway_id else "platform",
        },
        "ticket": ({"registration_id": str(reg.id), "name": reg.full_name, "paid_amount": _f(reg.paid_amount),
                    "price": _f(reg.actual_amount), "is_paid": bool(reg.is_paid)} if reg is not None else None),
        "store_orders": orders,
        "refunds": [{"id": r.id, "kind": r.kind, "amount": _f(r.amount), "reason": r.reason,
                     "razorpay_refund_id": r.razorpay_refund_id, "created_at": _iso(r.created_at),
                     "by": names.get(r.created_by)} for r in refunds],
    }


@router.get("/payments/{log_id}", summary="What can be refunded of a payment, and what was (admin)")
async def get_payment_refunds(log_id: UUID, session: SessionDep, user: AdminAuth) -> dict:
    return await payment_detail(session, log_id)


def _razorpay_refused(exc: Exception) -> CustomHTTPException:
    words = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return CustomHTTPException(502, message=f"Razorpay did not take the refund: {words}"[:300], error_code="REFUND_FAILED")


@router.post("/payments/{log_id}", summary="Refund part of a payment: some of the ticket, one store order, or the rest (money)")
async def refund_payment_part(log_id: UUID, body: PaymentRefundIn, session: SessionDep, user: MoneyAuth, request: Request) -> dict:
    from app.api.payment_gateways import service as gateways
    from app.api.payments import service as payments
    from app.api.payments.handlers import registration_for_order

    log = await _log(session, log_id)
    if log.status != PaymentStatus.captured:
        raise CustomHTTPException(409, message="Only a captured payment can be refunded", error_code="NOT_REFUNDABLE")
    reg = await registration_for_order(session, log.order)
    done = await refunded_so_far(session, log.id)
    remaining = money.rupees(log.amount) - done
    kind = body.kind
    store_order = None
    if kind == "store_order":
        if body.store_order_id is None or reg is None:
            raise CustomHTTPException(422, message="Name the store order to refund", error_code="STORE_ORDER_REQUIRED")
        store_order = await session.scalar(
            select(EventStoreOrders).where(EventStoreOrders.id == body.store_order_id, EventStoreOrders.registration_id == reg.id)
        )
        if store_order is None:
            raise CustomHTTPException(404, message="That store order is not on this payment", error_code="STORE_ORDER_NOT_FOUND")
        if store_order.status not in TAKEN:
            raise CustomHTTPException(409, message="That store order is not paid for", error_code="STORE_ORDER_NOT_PAID")
        amount = money.rupees(store_order.total)
    elif kind == "ticket":
        if body.amount is None:
            raise CustomHTTPException(422, message="Say how much of the ticket to refund", error_code="AMOUNT_REQUIRED")
        if reg is None:
            raise CustomHTTPException(409, message="This payment pays for no ticket", error_code="NO_TICKET")
        amount = money.rupees(body.amount)
        if amount >= money.rupees(reg.paid_amount):
            raise CustomHTTPException(
                422, message=f"Part of the ticket is less than the ₹{money.rupees(reg.paid_amount)} paid for it. "
                             "To refund the whole ticket, refund the rest of the payment.",
                error_code="AMOUNT_TOO_LARGE",
            )
    else:
        amount = remaining
    if amount <= 0 or amount > remaining:
        raise CustomHTTPException(422, message=f"Only ₹{remaining} of this payment is left to refund", error_code="AMOUNT_TOO_LARGE")
    if amount == remaining:
        kind_done = "rest"
    else:
        kind_done = kind
    account = await gateways.for_order(session, log.order)
    payment_id = log.razorpay_payment_id
    paise = money.to_paise(amount)
    # Nothing held while Razorpay answers.
    await session.commit()
    try:
        answer = await rzp.call(account.client.payment.refund, payment_id, {
            "amount": paise, "notes": {"reason": body.reason[:200], "by": "admin", "kind": kind_done},
        })
    except Exception as exc:  # noqa: BLE001 - nothing was changed; the admin is told why
        logger.warning("Admin refund of %s on %s refused: %s", paise, payment_id, type(exc).__name__)
        raise _razorpay_refused(exc)
    refund_id = payments._refund_id(answer)

    log = await _log(session, log_id, lock=True)
    row = PaymentRefunds(
        payment_log_id=log.id, razorpay_payment_id=payment_id, razorpay_refund_id=refund_id, kind=kind_done,
        store_order_id=store_order.id if store_order is not None else None,
        registration_id=reg.id if reg is not None else None, amount=amount, reason=body.reason.strip(), created_by=user.id,
    )
    if reg is not None:
        from app.api.events.models import Events

        row.club_id = await session.scalar(select(Events.club_id).where(Events.id == reg.event_id))
    session.add(row)
    await session.flush()
    if kind_done == "rest":
        if log.status == PaymentStatus.captured:
            # The whole remainder: the ticket and its orders end, as a full refund ends them.
            await payments._apply_refund(session, log, refund_id=refund_id)
    elif log.status == PaymentStatus.captured:
        await _apply_part(session, log, kind=kind_done, amount=amount, store_order_id=row.store_order_id,
                          refund_id=refund_id or f"admin:{row.id}")
    await audit.record(
        session, actor=user, action="payment.refund_part", entity_type="payment", entity_id=log.id,
        summary=f"Refunded ₹{amount} of {payment_id} ({kind_done})"[:255],
        details={"amount": str(amount), "kind": kind_done, "razorpay_payment_id": payment_id, "refund_id": refund_id,
                 "store_order_id": str(row.store_order_id) if row.store_order_id else None, "reason": row.reason},
        request=request,
    )
    await session.commit()
    return await payment_detail(session, log_id)


async def _apply_part(session, log: PaymentLogs, *, kind: str, amount: Decimal, store_order_id, refund_id: str) -> None:
    """Part of the payment went back: the ticket's price and paid amount come
    down by it, or the store order is refunded and its pieces go back on the
    shelf; the ledger gets both sides."""
    from app.api.payments.handlers import registration_for_order
    from app.api.store import service as store

    order = log.order
    reg = await registration_for_order(session, order, lock=True)
    if reg is not None:
        await ledger.ensure_history(session, reg)
        if kind == "ticket":
            reg.paid_amount = money.rupees(reg.paid_amount) - amount
            reg.actual_amount = max(Decimal(0), money.rupees(reg.actual_amount) - amount)
        elif kind == "store_order" and store_order_id is not None:
            o = await session.scalar(
                select(EventStoreOrders).where(EventStoreOrders.id == store_order_id).with_for_update().execution_options(populate_existing=True)
            )
            if o is not None and o.status in TAKEN:
                if o.status == "paid" and o.handed_over_at is None:
                    for line in store._in_lock_order(o):
                        await store._unsell(session, line)
                o.status = "refunded"
        await session.flush()
        await ledger.sync_registration(session, reg, kind=ledger_book.UNAPPLY, origin_at=log.created_at)
    await ledger.part_refund(session, log, order, registration=reg, paise=money.to_paise(amount), ref=refund_id,
                             refund_id=None if refund_id.startswith("admin:") else refund_id)


# ─── A subscription's charge ─────────────────────────────────────────────


@router.post("/subscription-charges/{charge_id}", summary="Refund a plan subscription's charge, in full or in part (money)")
async def refund_charge(charge_id: int, body: ChargeRefundIn, session: SessionDep, user: MoneyAuth, request: Request) -> dict:
    from app.api.subscriptions import billing
    from app.api.subscriptions import ledger as subscription_ledger
    from app.api.subscriptions.models import ChargeStatus, SubscriptionCharges

    async def charge_row(lock: bool = False):
        q = select(SubscriptionCharges).where(SubscriptionCharges.id == charge_id).execution_options(populate_existing=True)
        row = await session.scalar(q.with_for_update() if lock else q)
        if row is None:
            raise CustomHTTPException(404, message="No such charge", error_code="CHARGE_NOT_FOUND")
        return row

    charge = await charge_row()
    if charge.status == ChargeStatus.failed.value:
        raise CustomHTTPException(409, message="A failed charge took nothing to give back", error_code="NOT_REFUNDABLE")
    left = int(charge.amount_paise) - int(charge.refunded_paise or 0)
    paise = left if body.amount is None else money.to_paise(body.amount)
    if left <= 0:
        raise CustomHTTPException(409, message="This charge is refunded in full already", error_code="NOT_REFUNDABLE")
    if paise <= 0 or paise > left:
        raise CustomHTTPException(422, message=f"Only ₹{money.from_paise(left)} of this charge is left to refund", error_code="AMOUNT_TOO_LARGE")
    payment_id = charge.razorpay_payment_id
    await session.commit()
    try:
        answer = await rzp.call(billing.razorpay_client.payment.refund, payment_id, {
            "amount": paise, "notes": {"reason": body.reason[:200], "by": "admin", "kind": "subscription"},
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Admin refund of charge %s refused: %s", payment_id, type(exc).__name__)
        raise _razorpay_refused(exc)
    from app.api.payments import service as payments

    refund_id = payments._refund_id(answer)
    charge = await charge_row(lock=True)
    if subscription_ledger._set_refunded(charge, int(charge.refunded_paise or 0) + paise):
        await session.flush()
        from app.api.ledger import service as money_ledger

        await money_ledger.charge(session, charge)
    await audit.record(
        session, actor=user, action="subscription.refund", entity_type="club", entity_id=charge.club_id,
        summary=f"Refunded ₹{money.from_paise(paise)} of subscription charge {payment_id}"[:255],
        details={"charge_id": charge.id, "amount_paise": paise, "razorpay_payment_id": payment_id, "refund_id": refund_id,
                 "reason": body.reason.strip()},
        request=request,
    )
    await session.commit()
    return {"id": charge.id, "amount_paise": int(charge.amount_paise), "refunded_paise": int(charge.refunded_paise),
            "status": charge.status, "razorpay_payment_id": payment_id, "refund_id": refund_id}
