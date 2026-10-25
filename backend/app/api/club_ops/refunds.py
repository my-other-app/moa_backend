"""A club giving a guest's money back: the event's payments, and a refund of
all or part of one, through the Razorpay account that took it.

The account is the order's (payment_gateways.for_order): MyOtherApp's, or
the club's own Razorpay -- whichever the payment was made on, even after the
club changed or disconnected its keys. The admin portal's refund
(payments.service.refund_payment) is the same path for a whole payment;
this one adds a part refund and a record of who asked and why.

How one goes (`execute`), safe to run twice:

1. a club_refunds row, pending, committed with the request (so the money
   is spoken for: a second refund can only ask for what is left);
2. claimed (processing) and committed, then Razorpay asked for exactly the
   amount, with no transaction open while it answers;
3. Razorpay refused: failed, with its reason, listed for a retry. Razorpay
   did it but the answer was lost (a timeout): found by asking Razorpay how
   much it has refunded, and recorded as done -- never asked twice;
4. done: everything given back -> the payment refunded in full, exactly as
   the admin's refund leaves it (payments.service._apply_refund: the ticket
   unpaid, the shelf's pieces back, the ledger); a part -> the ticket's paid
   amount less the part, posted to the ledger (ledger.part_refund);
5. the guest told, once (email, counted as a payment message; app push).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from starlette.concurrency import run_in_threadpool

from app.api.club_ops.models import (
    CANCELLATION,
    CLUB,
    FAILED,
    PENDING,
    PROCESSING,
    SUCCEEDED,
    ClubRefunds,
)
from app.api.events.models import EventRegistrationsLink as Reg
from app.api.events.models import Events
from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.core import jobs, money
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: A refund left processing this long (its run died) may be tried again.
STALE = timedelta(minutes=5)
NOTICE_JOB = "club_ops.refund_notice"
REFUND_JOB = "club_ops.refund"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _rupees_text(paise: int) -> str:
    whole, part = divmod(int(paise), 100)
    return f"₹{whole:,}" if part == 0 else f"₹{whole:,}.{part:02d}"


# ─── The event's payments ─────────────────────────────────────────────────


def payments_query(event_id: int):
    """Every payment for the event's registrations, with its order and the
    registration (by the receipt the order was made with, as finance reads them)."""
    return (
        select(PaymentLogs, PaymentOrders, Reg)
        .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
        .join(Reg, Reg.payment_receipt == PaymentOrders.receipt)
        .where(Reg.event_id == event_id, Reg.is_deleted == False, PaymentLogs.is_deleted == False)  # noqa: E712
    )


async def refunds_of(session, log_ids: list) -> dict:
    """The club's refunds on these payments, oldest first, by payment."""
    out: dict = {}
    if not log_ids:
        return out
    rows = await session.scalars(
        select(ClubRefunds).where(ClubRefunds.payment_log_id.in_(log_ids)).order_by(ClubRefunds.created_at, ClubRefunds.id)
    )
    for r in rows.all():
        out.setdefault(r.payment_log_id, []).append(r)
    return out


def _paise(value) -> int:
    return money.to_paise(value)


def refunded_paise(log: PaymentLogs, rows: list) -> int:
    """What has gone back on the payment: all of it once it is refunded (by
    the club, an admin or Razorpay's dashboard), else the club's parts."""
    if log.status == PaymentStatus.refunded:
        return _paise(log.amount)
    return sum(_paise(r.amount) for r in rows if r.status == SUCCEEDED)


def refundable_paise(log: PaymentLogs, rows: list) -> int:
    """What may still be asked for: a captured payment less what went back
    and what is being given back now."""
    if log.status != PaymentStatus.captured:
        return 0
    asked = sum(_paise(r.amount) for r in rows if r.status in (PENDING, PROCESSING))
    return max(0, _paise(log.amount) - refunded_paise(log, rows) - asked)


def _status_of(log: PaymentLogs, rows: list) -> str:
    status = log.status.value if hasattr(log.status, "value") else str(log.status)
    if log.status == PaymentStatus.captured and refunded_paise(log, rows) > 0:
        return "partially_refunded"
    return status


def refund_out(r: ClubRefunds) -> dict:
    return {
        "id": r.id, "amount": float(money.rupees(r.amount)), "reason": r.reason,
        # Being asked right now reads as pending: it is not settled yet.
        "status": PENDING if r.status == PROCESSING else r.status,
        "source": r.source, "error": r.error, "created_at": r.created_at, "by": r.requested_by_name,
        "attempts": r.attempts or 0,
    }


def payment_out(log: PaymentLogs, order: PaymentOrders, reg: Optional[Reg], rows: list) -> dict:
    return {
        "id": log.id, "registration_id": reg.id if reg is not None else None,
        "ticket_id": reg.ticket_id if reg is not None else None,
        "guest_name": reg.full_name if reg is not None else None, "guest_email": reg.email if reg is not None else None,
        "amount": float(money.rupees(log.amount)),
        "refunded": float(money.from_paise(refunded_paise(log, rows))),
        "refundable": float(money.from_paise(refundable_paise(log, rows))),
        "method": log.payment_method, "status": _status_of(log, rows), "paid_at": log.created_at,
        "gateway": "club" if order.payment_gateway_id else "platform",
        "refunds": [refund_out(r) for r in rows],
    }


async def event_payments(session, event: Events) -> dict:
    rows = (await session.execute(payments_query(event.id).order_by(PaymentLogs.created_at.desc()))).all()
    refunds = await refunds_of(session, [log.id for log, _, _ in rows])
    items = [payment_out(log, order, reg, refunds.get(log.id, [])) for log, order, reg in rows]
    captured = sum(_paise(log.amount) for log, _, _ in rows if log.status in (PaymentStatus.captured, PaymentStatus.refunded))
    refunded = sum(refunded_paise(log, refunds.get(log.id, [])) for log, _, _ in rows)
    return {
        "items": items,
        "totals": {"payments": len(items), "captured": float(money.from_paise(captured)), "refunded": float(money.from_paise(refunded))},
    }


async def payment_row(session, event_id: int, payment_log_id, *, lock: bool = False):
    q = payments_query(event_id).where(PaymentLogs.id == payment_log_id)
    if lock:
        q = q.with_for_update(of=PaymentLogs)
    return (await session.execute(q.execution_options(populate_existing=True))).first()


async def payment_view(session, event_id: int, payment_log_id) -> Optional[dict]:
    row = await payment_row(session, event_id, payment_log_id)
    if row is None:
        return None
    log, order, reg = row
    return payment_out(log, order, reg, (await refunds_of(session, [log.id])).get(log.id, []))


# ─── Asking for one ──────────────────────────────────────────────────────


def _refuse(status_code: int, code: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(status_code, message=message, error_code=code)


async def request_refund(
    session, event: Events, payment_log_id, *, amount: Optional[Decimal], reason: str, actor, client_ref=None, request=None,
) -> ClubRefunds:
    """A club's refund of all (`amount` None) or part of one payment, asked
    and carried out now. The same `client_ref` again answers with the first."""
    from app.api.audit import service as audit

    if client_ref is not None:
        earlier = await session.scalar(select(ClubRefunds).where(ClubRefunds.client_ref == client_ref))
        if earlier is not None:
            if earlier.event_id != event.id or earlier.payment_log_id != payment_log_id:
                raise _refuse(409, "CLIENT_REF_USED", "That refund was already asked for another payment.")
            return earlier

    row = await payment_row(session, event.id, payment_log_id, lock=True)
    if row is None:
        raise _refuse(404, "PAYMENT_NOT_FOUND", "Payment not found")
    log, order, reg = row
    rows = (await refunds_of(session, [log.id])).get(log.id, [])
    left = refundable_paise(log, rows)
    if left <= 0:
        message = (
            "Nothing is left to refund on this payment." if log.status in (PaymentStatus.captured, PaymentStatus.refunded)
            else "Only a payment that went through can be refunded."
        )
        raise _refuse(409, "NOT_REFUNDABLE", message)
    want = left if amount is None else _paise(amount)
    if want <= 0:
        raise _refuse(422, "AMOUNT_TOO_LOW", "Refund more than ₹0.")
    if want > left:
        raise _refuse(422, "AMOUNT_TOO_HIGH", f"At most {_rupees_text(left)} is left to refund on this payment.")
    ticket = _paise(reg.paid_amount) if reg is not None else 0
    if want < left and want > ticket:
        # A part comes off the ticket; the shelf's share goes back only with all of it.
        raise _refuse(
            422, "AMOUNT_TOO_HIGH",
            f"A part refund can be at most the ticket's {_rupees_text(ticket)}. Refund all of it to give back the rest too.",
        )

    refund = ClubRefunds(
        id=uuid.uuid4(), club_id=event.club_id, event_id=event.id, registration_id=reg.id if reg is not None else None,
        payment_log_id=log.id, amount=money.from_paise(want), reason=reason.strip()[:500], source=CLUB, status=PENDING,
        client_ref=client_ref, requested_by=actor.user_id, member_id=actor.member_id, impersonated_by=actor.impersonated_by,
        requested_by_name=actor.name, attempts=0,
    )
    session.add(refund)
    try:
        await session.flush()
    except IntegrityError:
        # The same dialog's other click, a moment ago.
        await session.rollback()
        earlier = await session.scalar(select(ClubRefunds).where(ClubRefunds.client_ref == client_ref)) if client_ref else None
        if earlier is None:
            raise
        return earlier
    await audit.record(
        session, actor=actor.user, action="club.refund", entity_type="payment", entity_id=log.id,
        summary=f"Refund of {_rupees_text(want)} to {reg.full_name if reg is not None else 'a guest'} for {event.name}"[:255],
        details={"event_id": event.id, "refund_id": str(refund.id), "amount": str(money.from_paise(want)), "reason": refund.reason,
                 "whole": want == _paise(log.amount) - refunded_paise(log, rows), **actor.details()},
        request=request,
    )
    refund_id = refund.id
    await session.commit()
    await execute(session, refund_id)
    return await session.scalar(select(ClubRefunds).where(ClubRefunds.id == refund_id).execution_options(populate_existing=True))


async def retry(session, event: Events, refund_id, *, actor, request=None) -> ClubRefunds:
    """A failed refund (or one whose run died) asked again."""
    from app.api.audit import service as audit

    r = await session.scalar(
        select(ClubRefunds).where(ClubRefunds.id == refund_id, ClubRefunds.event_id == event.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if r is None:
        raise _refuse(404, "REFUND_NOT_FOUND", "Refund not found")
    stale = r.status == PROCESSING and (_aware(r.updated_at) or _now()) <= _now() - STALE
    if r.status == SUCCEEDED:
        await session.commit()
        return r
    if r.status not in (FAILED, PENDING) and not stale:
        await session.commit()
        return r
    r.status = PENDING
    await audit.record(
        session, actor=actor.user, action="club.refund.retry", entity_type="payment", entity_id=r.payment_log_id,
        summary=f"Tried a refund of ₹{money.rupees(r.amount)} again for {event.name}"[:255],
        details={"event_id": event.id, "refund_id": str(r.id), **actor.details()}, request=request,
    )
    await session.commit()
    await execute(session, refund_id, retrying=True)
    return await session.scalar(select(ClubRefunds).where(ClubRefunds.id == refund_id).execution_options(populate_existing=True))


# ─── Carrying it out ─────────────────────────────────────────────────────


async def _succeeded_paise(session, log_id) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(ClubRefunds.amount), 0)).where(
            ClubRefunds.payment_log_id == log_id, ClubRefunds.status == SUCCEEDED,
        )
    )
    return _paise(total or 0)


async def _refunded_at_razorpay(account, payment_id: str, at_least: int) -> bool:
    """Whether Razorpay has given back at least `at_least` paise on the
    payment (a refund that went through whose answer was lost)."""
    from app.api.payments import rzp

    try:
        payment = await rzp.call(account.client.payment.fetch, payment_id) or {}
    except Exception:  # noqa: BLE001
        return False
    amount = int(payment.get("amount") or 0)
    refunded = int(payment.get("amount_refunded") or 0)
    return refunded >= at_least or (payment.get("status") == "refunded" and amount > 0 and amount >= at_least)


def _describe(exc: Exception) -> str:
    message = str(getattr(exc, "message", None) or exc or exc.__class__.__name__).strip()
    return f"Razorpay didn't take the refund: {message}"[:300]


async def execute(session, refund_id, *, retrying: bool = False) -> Optional[ClubRefunds]:
    from app.api.payment_gateways import service as gateways
    from app.api.payments import rzp
    from app.api.payments import service as payments

    r = await session.scalar(
        select(ClubRefunds).where(ClubRefunds.id == refund_id).with_for_update().execution_options(populate_existing=True)
    )
    if r is None or r.status != PENDING:
        await session.commit()
        return r
    log = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.id == r.payment_log_id).options(joinedload(PaymentLogs.order))
        .execution_options(populate_existing=True)
    )
    if log is None or log.status != PaymentStatus.captured:
        r.status, r.error = FAILED, "This payment was already refunded in full."
        await session.commit()
        return r
    try:
        account = await gateways.for_order(session, log.order)
    except CustomHTTPException as exc:
        r.status, r.error = FAILED, (exc.message or "The payment's Razorpay account can't be used right now.")[:300]
        r.attempts = (r.attempts or 0) + 1
        await session.commit()
        return r
    payment_id, paise = log.razorpay_payment_id, _paise(r.amount)
    before = await _succeeded_paise(session, log.id)
    r.status, r.attempts, r.error = PROCESSING, (r.attempts or 0) + 1, None
    await session.commit()  # nothing held while Razorpay answers

    rz_refund = None
    error = None
    if retrying and await _refunded_at_razorpay(account, payment_id, before + paise):
        pass  # it went through last time; the answer was lost
    else:
        try:
            answer = await rzp.call(
                account.client.payment.refund, payment_id,
                {"amount": paise, "notes": {"reason": r.reason[:200], "ref": str(r.id), "source": r.source}},
            )
            rz_refund = payments._refund_id(answer)
        except Exception as exc:  # noqa: BLE001
            if await _refunded_at_razorpay(account, payment_id, before + paise):
                logger.warning("refund %s: Razorpay's answer was lost, but it has refunded the payment", refund_id)
            else:
                logger.warning("refund %s of %s failed: %s", refund_id, payment_id, type(exc).__name__)
                error = _describe(exc)

    r = await session.scalar(
        select(ClubRefunds).where(ClubRefunds.id == refund_id).with_for_update().execution_options(populate_existing=True)
    )
    if error is not None:
        r.status, r.error = FAILED, error
        await session.commit()
        return r
    log = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.id == r.payment_log_id).options(joinedload(PaymentLogs.order))
        .with_for_update(of=PaymentLogs).execution_options(populate_existing=True)
    )
    r.status, r.razorpay_refund_id, r.processed_at, r.error = SUCCEEDED, rz_refund, _now(), None
    await session.flush()
    await _apply(session, r, log, rz_refund)
    await jobs.enqueue(session, NOTICE_JOB, {"refund_id": str(r.id)}, dedupe_key=f"{NOTICE_JOB}:{r.id}")
    await session.commit()
    return r


async def _apply(session, r: ClubRefunds, log: PaymentLogs, rz_refund: Optional[str]) -> None:
    """What the refund changes here, once Razorpay has done it."""
    from app.api.ledger import book as ledger_book
    from app.api.ledger import service as ledger
    from app.api.payments import service as payments
    from app.api.payments.handlers import registration_for_order

    if log.status != PaymentStatus.captured:
        return  # refunded in full meanwhile (the webhook got here first)
    if await _succeeded_paise(session, log.id) >= _paise(log.amount):
        # Everything is back: as the admin's refund leaves a payment. Commits.
        await payments._apply_refund(session, log, refund_id=rz_refund)
        return
    registration = await registration_for_order(session, log.order, lock=True)
    if registration is not None:
        await ledger.ensure_history(session, registration)
        left = money.rupees(registration.paid_amount) - money.rupees(r.amount)
        registration.paid_amount = left if left > 0 else Decimal("0.00")
        await ledger.sync_registration(session, registration, kind=ledger_book.UNAPPLY, origin_at=log.created_at)
    await ledger.part_refund(
        session, log, log.order, registration=registration, paise=_paise(r.amount), ref=str(r.id), refund_id=rz_refund,
    )


# One Razorpay call each; a cancelled event queues many: two at a time on a runner.
@jobs.handler(REFUND_JOB, max_attempts=3, timeout=120.0, priority=jobs.NORMAL, concurrency=2)
async def _refund_job(p: dict) -> None:
    async with jobs.scope() as session:
        await execute(session, uuid.UUID(p["refund_id"]))


# ─── Telling the guest ───────────────────────────────────────────────────


async def tell_guest(session, refund_id) -> bool:
    """The guest hears their money is on its way back: by email (a payment
    message, counted; never held back) and in the app. Once: the refund is
    marked told before anything goes."""
    from app.api.message_quota import service as quota
    from app.api.subscriptions import notices
    from app.core.notifications.triggers import notify_event_participants

    r = await session.scalar(
        select(ClubRefunds).where(ClubRefunds.id == refund_id).with_for_update().execution_options(populate_existing=True)
    )
    if r is None or r.status != SUCCEEDED or r.notified_at is not None:
        await session.commit()
        return False
    r.notified_at = _now()
    await session.commit()

    reg = await session.get(Reg, r.registration_id) if r.registration_id else None
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == r.event_id))
    if reg is None or event is None:
        return False
    amount = _rupees_text(_paise(r.amount))
    club = event.club.name if event.club is not None else "The organiser"
    subject = f"{amount} refunded for {event.name}"
    why = "the event was cancelled" if r.source == CANCELLATION else (r.reason or "").strip()
    lines = [
        f"Hi {(reg.full_name or '').split(' ')[0] or 'there'},",
        f"{club} has refunded {amount} of your payment for {event.name}.",
    ]
    if why:
        lines.append(f"Why: {why}")
    lines.append("It goes back the way you paid and usually reaches your account in 5–7 working days.")
    if reg.email and reg.email_bounced_at is None:
        try:
            await run_in_threadpool(notices.send, reg.email, subject, lines, None, None)
            await quota.count(session, club_id=event.club_id, channel=quota.EMAIL, kind="payment", ref=f"refund:{r.id}")
        except Exception:  # noqa: BLE001 - told is told; a mail that fails is logged
            logger.warning("refund %s: the guest's email could not be sent", r.id, exc_info=True)
    if reg.user_id:
        try:
            await notify_event_participants(
                session, event.id, title=subject, body=f"{club} refunded {amount}. It reaches your account in 5–7 working days.",
                user_ids=[reg.user_id], club_id=event.club_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("refund %s: the push could not be sent", r.id, exc_info=True)
    await session.commit()
    return True


@jobs.handler(NOTICE_JOB, max_attempts=3, timeout=120.0, priority=jobs.HIGH)
async def _notice_job(p: dict) -> None:
    async with jobs.scope() as session:
        await tell_guest(session, uuid.UUID(p["refund_id"]))
