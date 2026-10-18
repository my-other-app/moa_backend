"""Taking money for tickets (and the store's picks riding on them).

The rules every path here keeps:

- Money is Decimal end to end (app.core.money): rupees in the database,
  whole paise to and from Razorpay, never a float in between.
- Nothing holds a database transaction, an advisory lock or a pooled
  connection while Razorpay is being asked something. Each path reads what
  it needs and commits (handing the connection back), calls Razorpay, then
  applies the answer in a transaction of its own that re-reads under a lock.
- A captured payment is applied in one transaction: the payment's row, the
  order paid, the registration and the store's picks paid (`apply_payment`).
  It is applied by whichever of verify, the webhooks and the reconciliation
  sweep gets there first; the others find it applied (the order's row lock
  orders them, the payment's unique id keeps one row) and change nothing.
- Razorpay is asked, not believed: a webhook's payment is fetched with the
  keys of the account its order was made on before anything is applied, so a
  leaked webhook secret cannot mint a ticket.
- Money that is not owed goes back on its own, in full: a payment that lands
  when the event's last paid place has gone (`event_full`), and a second
  payment on an order already paid (`duplicate`). The payment is marked in
  the transaction that finds it, Razorpay is asked after it commits, and the
  sweep keeps asking until Razorpay has done it (`settle_refund`).
"""
from datetime import datetime, timedelta, timezone
import asyncio
import logging
import time
import uuid

from fastapi import BackgroundTasks
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.api.ledger import book as ledger_book
from app.api.ledger import service as ledger
from app.api.payments import rzp
from app.api.payments.handlers import (
    Applied,
    handle_post_payment,
    registration_for_order,
    send_ticket_if_paid,
    validate_payload,
)
from app.api.payments.models import (
    OrderStatus,
    PaymentLogs,
    PaymentOrders,
    PaymentStatus,
    RazorpayWebhookLogs,
)
from app.core import jobs, money
from app.core.validations.exceptions import CodedRequestError, RequestValidationError
from app.response import CustomHTTPException
from app.config import settings
from app.api.payments.background_tasks import send_payment_confirmation_email

# The platform's account. Called only through rzp.call: the SDK is
# synchronous, and the event loop must not wait on Razorpay.
razorpay_client = rzp.new_client(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)

logger = logging.getLogger(__name__)

#: Why a captured payment is being given back without anyone asking.
EVENT_FULL = "event_full"
DUPLICATE = "duplicate"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UpdateAppToPay(CustomHTTPException):
    """An older app build, asked to pay an order on a club's own Razorpay
    account: it opens checkout with the platform's key compiled in, which
    cannot pay it. The website can."""

    def __init__(self, web_url: str | None):
        super().__init__(
            status_code=409,
            message="Update the MyOtherApp app to pay for this event, or pay on the website.",
            error_code="UPDATE_APP_TO_PAY",
            errors={"web_url": web_url},
        )


def _mismatch() -> CodedRequestError:
    return CodedRequestError(
        400, "This payment does not belong to the specified order.", "PAYMENT_ORDER_MISMATCH",
        razorpay_order_id="This payment does not belong to the specified order.",
    )


# ─── Orders ──────────────────────────────────────────────────────────────


async def _reusable_order(session, receipt: str, amount, gateway_id: int | None) -> PaymentOrders | None:
    """An open order for this receipt asking for the same money on the same
    account, taken under the receipt's advisory lock (held to the commit,
    which the caller makes before Razorpay is called)."""
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(receipt))))
    same_account = (
        PaymentOrders.payment_gateway_id.is_(None)
        if gateway_id is None
        else PaymentOrders.payment_gateway_id == gateway_id
    )
    existing = await session.scalar(
        select(PaymentOrders)
        .where(
            PaymentOrders.receipt == receipt,
            PaymentOrders.status != OrderStatus.paid,
            PaymentOrders.status != OrderStatus.refunded,
            same_account,
        )
        .order_by(PaymentOrders.created_at.desc())
    )
    # Reused only while it still asks for the same money, on the same
    # account: a changed pick from the shelf, or a club that connected or
    # dropped its own Razorpay since, means a fresh order.
    if existing is not None and money.same(existing.amount, amount):
        return existing
    return None


async def create_razorpay_order(
    session: AsyncSession,
    source: str,
    payload: dict,
    user_id: int,
    checkout_version: int | None = None,
):
    """
    The order checkout opens, on the Razorpay account that sells it: the
    club's own when it has an active one (app/api/payment_gateways), the
    platform's otherwise. `checkout_version` is the client's `checkout` query
    parameter; below 2 the client cannot open a club's account, and is told
    to update (UpdateAppToPay) rather than handed an order it cannot pay.

    Two requests for the same receipt at once must not each hand out an
    order of their own. The receipt's advisory lock is taken to look for one
    to reuse and again to keep the new one, and let go in between, while
    Razorpay makes it: the first order kept wins, and a request that made
    another meanwhile hands out the kept one (an unpaid Razorpay order nobody
    was given charges nobody).
    """
    from app.api.payment_gateways import service as gateways
    from app.api.payments.handlers import event_for_payload
    from app.core import public_urls

    amount_in_rupee, db_receipt = await validate_payload(
        session=session, source=source, payload=payload
    )
    amount = money.rupees(amount_in_rupee)
    event = await event_for_payload(session, source, payload)
    club_id = event.club_id if event is not None else None
    checkout = await gateways.for_new_order(session, club_id)
    if checkout.kind == gateways.CLUB and not gateways.supports_club_checkout(checkout_version):
        # Before anything is written or locked: there is nothing to undo.
        raise UpdateAppToPay(public_urls.event_url(event.slug) if event is not None else None)

    receipt = f"{db_receipt}#{int(time.time())}"
    order_data = {
        "amount": money.to_paise(amount),
        "currency": "INR",
        "notes": payload,
        "receipt": receipt,
    }

    # Twice at most: keys the club's account refuses mark it failed, and the
    # second pass makes the order on the platform's instead.
    for attempt in range(2):
        if attempt:
            checkout = await gateways.for_new_order(session, club_id)
        gateway_id = checkout.gateway_id
        existing = await _reusable_order(session, db_receipt, amount, gateway_id)
        # The lock and the connection go before Razorpay is asked.
        await session.commit()
        if existing is not None:
            return existing
        try:
            razorpay_order = await rzp.call(checkout.client.order.create, data=order_data)
        except Exception as exc:
            if checkout.kind == gateways.CLUB and attempt == 0 and gateways.is_auth_error(exc):
                await gateways.mark_failed(
                    session, checkout.gateway,
                    f"Razorpay refused the keys when making an order: {gateways.short_reason(exc)}",
                )
                await session.commit()
                continue
            raise
        break

    kept = await _reusable_order(session, db_receipt, amount, gateway_id)
    if kept is not None:
        await session.commit()
        return kept
    db_order = PaymentOrders(
        receipt=db_receipt,
        razorpay_receipt=receipt,
        source=source,
        payload=payload,
        razorpay_order_id=razorpay_order["id"],
        amount=amount,
        currency="INR",
        status=OrderStatus.created,
        user_id=user_id,
        payment_gateway_id=gateway_id,
    )

    session.add(db_order)
    await session.commit()
    await session.refresh(db_order)
    return db_order


# ─── Verifying and applying a payment ───────────────────────────────────


def method_details(details: dict) -> dict:
    """What Razorpay says about how it was paid, for the admin's list. Any
    method: a captured payment is applied whatever it was paid with (EMI,
    cardless EMI, pay later, a bank transfer as much as a card or UPI)."""
    method = details.get("method")
    acquirer = dict(details.get("acquirer_data") or {})
    if method == "upi":
        return dict(details.get("upi") or {}) or {**acquirer, "vpa": details.get("vpa")}
    if method == "card":
        return dict(details.get("card") or {}) or {**acquirer, "card_id": details.get("card_id")}
    if method == "netbanking":
        return {**acquirer, "bank": details.get("bank") or acquirer.get("bank")}
    if method == "wallet":
        return {**acquirer, "wallet": details.get("wallet")}
    out = acquirer
    for key in ("bank", "wallet", "vpa", "provider", "card_id", "emi_plan"):
        if details.get(key):
            out[key] = details[key]
    if isinstance(details.get("card"), dict):
        out["card"] = details["card"]
    return out


def _payment_status(value) -> PaymentStatus:
    try:
        return PaymentStatus(value)
    except ValueError:
        return PaymentStatus.failed


async def _log_with_order(session, razorpay_payment_id: str) -> PaymentLogs | None:
    return await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.razorpay_payment_id == razorpay_payment_id)
        .options(joinedload(PaymentLogs.order))
        .execution_options(populate_existing=True)
    )


async def _locked_order(session, order_id) -> PaymentOrders | None:
    return await session.scalar(
        select(PaymentOrders).where(PaymentOrders.id == order_id).with_for_update().execution_options(populate_existing=True)
    )


async def verify_razorpay_payment(
    session: AsyncSession,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    payment_details: dict | None = None,
    expand_payment_details: bool = True,
    background_tasks: BackgroundTasks | None = None,
    send_receipt: bool = True,
):
    """
    A payment checkout says was made: asked of Razorpay (unless the caller
    has just asked, `payment_details`), on the account its order was made
    on, and applied (`apply_payment`). A payment applied before -- by the
    webhook racing this, or an earlier verify -- is not fetched again; what
    it should have done is made sure of (`ensure_applied`).
    """
    order = await session.scalar(
        select(PaymentOrders).where(
            PaymentOrders.razorpay_order_id == razorpay_order_id
        )
    )

    if not order:
        raise CustomHTTPException(404, "Payment order not found", error_code="ORDER_NOT_FOUND")

    known = await _log_with_order(session, razorpay_payment_id)
    if known is not None and known.order_id != order.id:
        raise _mismatch()
    if known is not None and known.status in (PaymentStatus.captured, PaymentStatus.refunded):
        await ensure_applied(session, order.id, background_tasks=background_tasks)
        return await _log_with_order(session, razorpay_payment_id)

    order_id = order.id
    account = None
    if not payment_details or expand_payment_details:
        # The account the order was made on -- the club's own, or the
        # platform's -- whatever the club has connected since.
        from app.api.payment_gateways import service as gateways

        account = await gateways.for_order(session, order)
    # Nothing is held while Razorpay answers.
    await session.commit()

    if not payment_details:
        payment_details = await rzp.call(account.client.payment.fetch, razorpay_payment_id)

    # Make sure the payment being verified was actually made against this
    # order, not a different one — otherwise a real captured payment for
    # order A could be used to mark an unrelated order B as paid.
    if payment_details.get("order_id") != razorpay_order_id:
        raise _mismatch()

    if expand_payment_details and payment_details.get("method") == "card" and not payment_details.get("card"):
        payment_details = await rzp.call(
            account.client.payment.fetch, razorpay_payment_id, {"expand[]": "card"}
        )

    return await apply_payment(
        session, order_id, payment_details, background_tasks=background_tasks, send_receipt=send_receipt
    )


async def apply_payment(
    session: AsyncSession,
    order_id,
    details: dict,
    *,
    background_tasks: BackgroundTasks | None = None,
    send_receipt: bool = True,
) -> PaymentLogs:
    """
    Razorpay's word on one payment, applied to our tables in one transaction
    under the order's row lock. The payment's row is inserted once (ON
    CONFLICT DO NOTHING on its id), so a verify and a webhook arriving
    together each find the other's work and neither fails.

    Captured: the order is paid, and the registration and the store's picks
    with it (handlers.handle_post_payment), in the same transaction. When the
    event's last paid place went meanwhile, or the order was already paid by
    another payment, the payment is marked for a full refund instead, and
    given back once this commits (`settle_refund`).
    """
    payment_id = details.get("id")
    if not payment_id:
        raise RequestValidationError(razorpay_payment_id="Razorpay gave no payment id")
    now = _now()
    order = await _locked_order(session, order_id)
    if order is None:
        raise CustomHTTPException(404, "Payment order not found", error_code="ORDER_NOT_FOUND")
    if details.get("order_id") not in (None, order.razorpay_order_id):
        raise _mismatch()

    table = PaymentLogs.__table__
    await session.execute(
        pg_insert(table)
        .values(
            id=uuid.uuid4(), order_id=order.id, razorpay_payment_id=payment_id, status=PaymentStatus.created,
            amount=0, created_at=now, updated_at=now, is_deleted=False,
        )
        .on_conflict_do_nothing(index_elements=["razorpay_payment_id"])
    )
    log = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.razorpay_payment_id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True, include_deleted=True)
    )
    if log.order_id != order.id:
        raise _mismatch()

    if log.status in (PaymentStatus.captured, PaymentStatus.refunded):
        # Applied already, by whoever got here first.
        await session.commit()
        await ensure_applied(session, order.id, background_tasks=background_tasks)
        return await _log_with_order(session, payment_id)

    status = details.get("status")
    log.amount = money.from_paise(details.get("amount"))
    log.payment_method = details.get("method")
    log.payment_details = method_details(details)

    if status != "captured":
        log.status = _payment_status(status)
        if order.status == OrderStatus.created:
            order.status = OrderStatus.attempted
        await session.commit()
        return await _log_with_order(session, payment_id)

    log.status = PaymentStatus.captured
    if order.status in (OrderStatus.paid, OrderStatus.refunded):
        # The order was paid already, by another payment: this one bought
        # nothing, and goes back whole. The first stands.
        log.refund_reason, log.refund_requested_at = DUPLICATE, now
        # In the ledger as taken and owed back (unapplied), with the capture.
        await ledger.capture(session, log, order)
        await session.commit()
        await settle_refund(session, log.id)
        return await _log_with_order(session, payment_id)

    order.status = OrderStatus.paid
    # The sums below read the payment's row: written first (no autoflush).
    await session.flush()
    # The money into the ledger, and (handle_post_payment) what it bought,
    # in this same transaction.
    await ledger.capture(session, log, order)
    outcome = await handle_post_payment(session, order, background_tasks=background_tasks, commit=False)
    if outcome.full:
        log.refund_reason, log.refund_requested_at = EVENT_FULL, now
    await session.commit()

    if outcome.full:
        await settle_refund(session, log.id)
        return await _log_with_order(session, payment_id)

    await send_ticket_if_paid(session, outcome, background_tasks)
    if send_receipt:
        await _send_receipt(session, order.id, payment_id, background_tasks)
    return await _log_with_order(session, payment_id)


async def _send_receipt(session, order_id, payment_id: str, background_tasks) -> None:
    """The receipt -- a PDF drawn by wkhtmltopdf, then a mail -- as a job after
    the payment's commit (`payments.receipt`): it used to be drawn in the
    request's background, or on the event loop itself when there was no
    request (a webhook), and was lost with the process. One waiting per payment."""
    try:
        await jobs.enqueue(session, "payments.receipt", {"order_id": str(order_id), "payment_id": payment_id}, dedupe_key=f"payments:receipt:{payment_id}")
        await session.commit()
    except Exception:
        # The payment stands either way; the caller reads on after this.
        await session.rollback()
        logger.exception("Error queueing the payment confirmation email")


@jobs.handler("payments.receipt", max_attempts=4, timeout=180.0)
async def _receipt_job(p: dict) -> None:
    async with jobs.scope() as session:
        order = await session.scalar(
            select(PaymentOrders).where(PaymentOrders.id == uuid.UUID(str(p["order_id"]))).options(joinedload(PaymentOrders.user))
        )
        if order is None or not order.user:
            return
        log = await _log_with_order(session, p["payment_id"])
        await session.commit()
        await asyncio.to_thread(
            send_payment_confirmation_email,
            subject="Payment Receipt",
            payload={
                "payer_name": order.user.full_name,
                "payer_email": order.user.email,
                "payer_phone": order.user.phone or "N/A",
                "amount": f"₹{order.amount}",
                "receipt_id": order.receipt,
                "payment_method": log.payment_method if log is not None else None,
                "timestamp": log.created_at if log is not None else None,
                "purpose": "Event Registration",
                "notes": "N/A",
                "current_year": datetime.now().year,
            },
            recipients=[order.user.email],
        )


async def ensure_applied(session: AsyncSession, order_id, *, background_tasks: BackgroundTasks | None = None) -> Applied | None:
    """A paid order's registration and picks brought in line with what it
    captured, again: idempotent, so a payment applied before this deploy (or
    by a path that failed half-way) is finished by the next verify, webhook
    or the repair sweep. None when the order is not paid."""
    order = await _locked_order(session, order_id)
    if order is None or order.status != OrderStatus.paid:
        await session.commit()
        return None
    outcome = await handle_post_payment(session, order, background_tasks=background_tasks, commit=False)
    due = []
    if outcome.full:
        due = list((await session.scalars(
            select(PaymentLogs).where(
                PaymentLogs.order_id == order.id,
                PaymentLogs.status == PaymentStatus.captured,
                PaymentLogs.refund_requested_at.is_(None),
            )
        )).all())
        for log in due:
            log.refund_reason, log.refund_requested_at = EVENT_FULL, _now()
    await session.commit()
    for log in due:
        await settle_refund(session, log.id)
    if not outcome.full:
        await send_ticket_if_paid(session, outcome, background_tasks)
    return outcome


# ─── Refunds ─────────────────────────────────────────────────────────────


async def _apply_refund(session: AsyncSession, db_payment: PaymentLogs, *, refund_id: str | None = None) -> None:
    """Shared state-reversal for a captured payment being refunded — used by
    the admin-triggered refund endpoint, the refund.processed webhook branch
    (a refund initiated directly from the Razorpay dashboard converges on the
    same state as one triggered from our app) and the automatic refunds.

    A duplicate payment's refund touches nothing but itself: the order's
    first payment still stands.

    The ledger gets what the refund took back (the ticket, the store orders)
    and the money going out, in the same transaction; a registration paid
    before the ledger existed is posted as it stood first (ensure_history).
    """
    order = db_payment.order
    registration = None
    if db_payment.refund_reason != DUPLICATE:
        # Re-blocks check-in via the existing has_fee-and-not-is_paid gate in
        # checkin_user/mark_attendance — no changes needed there.
        registration = await registration_for_order(session, order, lock=True)
        if registration:
            await ledger.ensure_history(session, registration)
    db_payment.status = PaymentStatus.refunded
    if db_payment.refund_reason != DUPLICATE:
        order.status = OrderStatus.refunded
        if registration:
            registration.is_paid = False
            registration.paid_amount = 0
            # A full refund gives the shelf its pieces back too (not those
            # already handed over).
            from app.api.store import service as store

            await store.mark_refunded(session, registration.id)
            await ledger.sync_registration(session, registration, kind=ledger_book.UNAPPLY,
                                           origin_at=db_payment.created_at)
    await ledger.refund(session, db_payment, order, registration=registration, refund_id=refund_id)
    if db_payment.refund_reason in (EVENT_FULL, DUPLICATE):
        await _tell_auto_refund(session, db_payment, order, registration)

    await session.commit()


def _refund_id(answer) -> str | None:
    """The refund's id in Razorpay's answer, for the ledger (and the daily
    reconciliation, which matches refunds by it)."""
    return (answer or {}).get("id") if isinstance(answer, dict) else None


async def _already_refunded(account, payment_id: str) -> bool:
    """Whether Razorpay holds the payment as refunded in full (a refund that
    went through, whose answer was lost)."""
    try:
        payment = await rzp.call(account.client.payment.fetch, payment_id) or {}
    except Exception:  # noqa: BLE001
        return False
    amount = int(payment.get("amount") or 0)
    return payment.get("status") == "refunded" or (amount > 0 and int(payment.get("amount_refunded") or 0) >= amount)


async def settle_refund(session: AsyncSession, log_id) -> bool:
    """
    Give back a captured payment marked for an automatic refund
    (`refund_requested_at`): Razorpay is asked, through the account the
    order was made on, with no transaction open; then the refund is applied
    (`_apply_refund`, which tells the guest, the organiser and the admins).
    False when Razorpay did not take it: the admins hear, and the sweep asks
    again. Safe to run twice: a payment already refunded is only recorded.
    """
    from app.api.payment_gateways import service as gateways

    log = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order))
        .execution_options(populate_existing=True)
    )
    if log is None or log.status != PaymentStatus.captured or log.refund_requested_at is None:
        await session.commit()
        return False
    payment_id = log.razorpay_payment_id
    try:
        account = await gateways.for_order(session, log.order)
    except CustomHTTPException as exc:
        await session.rollback()
        await _refund_failed(session, log_id, exc.message)
        return False
    await session.commit()

    refund_id = None
    try:
        refund_id = _refund_id(await rzp.call(account.client.payment.refund, payment_id))
    except Exception as exc:  # noqa: BLE001
        if not await _already_refunded(account, payment_id):
            logger.warning("Automatic refund of %s failed: %s", payment_id, type(exc).__name__)
            await _refund_failed(session, log_id, f"{type(exc).__name__}: {str(exc)[:160]}")
            return False

    log = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order))
        .with_for_update(of=PaymentLogs).execution_options(populate_existing=True)
    )
    if log is not None and log.status == PaymentStatus.captured:
        await _apply_refund(session, log, refund_id=refund_id)
    else:
        await session.commit()
    return True


async def _refund_failed(session, log_id, reason: str) -> None:
    from app.api.admin_alerts import notify

    from app.api.payments.sweep import _alerted

    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order)))
    if log is None or await _alerted(session, "payment_refund_failed", "payment_id", log.razorpay_payment_id):
        await session.commit()
        return
    club_id = await _club_of_order(session, log.order)
    await notify(
        session, "payment_refund_failed",
        title=f"An automatic refund of ₹{log.amount} has not gone through yet",
        body=f"Payment {log.razorpay_payment_id} ({log.refund_reason}): {reason}. The payments sweep keeps trying.",
        club=club_id,
        data={"payment_id": log.razorpay_payment_id, "reason": log.refund_reason, "error": reason[:200]},
        dedupe_on={"payment_id": log.razorpay_payment_id},
    )
    await session.commit()


async def _club_of_order(session, order) -> int | None:
    from app.api.events.models import Events

    reg = await registration_for_order(session, order) if order is not None else None
    if reg is None:
        return None
    return await session.scalar(select(Events.club_id).where(Events.id == reg.event_id))


async def _tell_auto_refund(session, log: PaymentLogs, order: PaymentOrders, registration) -> None:
    """The guest (why their money came back), the admins (an alert) and, for
    an event that filled, the organiser: after the refund commits."""
    from app.api.admin_alerts import notify
    from app.api.events.models import Events
    from app.core.after_commit import run_after_commit

    if registration is None:
        registration = await registration_for_order(session, order)
    event = await session.get(Events, registration.event_id) if registration is not None else None
    name = event.name if event is not None else "the event"
    amount = f"₹{money.rupees(log.amount)}"
    if log.refund_reason == EVENT_FULL:
        title = f"{name} filled up while {registration.full_name if registration else 'a guest'} was paying: {amount} refunded"
        guest = [
            f"Sorry — the last place at {name} went while you were paying.",
            f"We have refunded your payment of {amount} in full. It usually reaches your account in 5–7 working days.",
        ]
    else:
        title = f"A second payment of {amount} for the same order was refunded"
        guest = [
            f"Your payment for {name} went through twice. Your ticket stands on the first payment;",
            f"the second, {amount}, has been refunded in full and usually reaches your account in 5–7 working days.",
        ]
    await notify(
        session, "payment_auto_refunded", title=title,
        body=f"Payment {log.razorpay_payment_id} on order {order.razorpay_order_id} was refunded automatically ({log.refund_reason}).",
        club=event.club_id if event is not None else None,
        data={"payment_id": log.razorpay_payment_id, "reason": log.refund_reason, "amount": str(log.amount),
              "registration_id": str(registration.id) if registration is not None else None},
        dedupe_on={"payment_id": log.razorpay_payment_id},
    )
    from app.api.subscriptions import notices

    if registration is not None and registration.email:
        run_after_commit(session, notices._send, registration.email, f"Your payment for {name} was refunded", guest, None, None)
    if log.refund_reason == EVENT_FULL and event is not None and event.club_id is not None:
        to = await notices.owner_address(session, event.club_id)
        if to:
            run_after_commit(
                session, notices._send, to, title,
                [f"{registration.full_name if registration else 'A guest'} paid {amount} just after the last paid place at {name} went. "
                 "The payment has been refunded to them in full; nothing for you to do."],
                None, None,
            )


async def list_payments(
    session: AsyncSession,
    status: str | None = None,
    search: str | None = None,
    user_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
    gateway: str | None = None,
):
    """
    Every payment, for an admin. `gateway`: "platform" for those paid into
    the platform's Razorpay account, "club" for those paid into a club's own.

    There was no way to list these at all: the only payment route an admin had
    was the refund, which needs a payment id they had no way to look up. A
    platform that takes money could not show a transaction, investigate a
    failed one, or find the payment behind "I paid and got nothing".
    """
    query = (
        select(PaymentLogs)
        .options(
            joinedload(PaymentLogs.order).joinedload(PaymentOrders.user)
        )
        .where(PaymentLogs.is_deleted == False)
    )
    count_query = select(func.count(PaymentLogs.id)).where(
        PaymentLogs.is_deleted == False
    )

    filters = []
    if status:
        filters.append(PaymentLogs.status == status)
    if gateway in ("platform", "club"):
        on_club = PaymentOrders.payment_gateway_id.isnot(None)
        filters.append(
            PaymentLogs.order_id.in_(
                select(PaymentOrders.id).where(on_club if gateway == "club" else ~on_club)
            )
        )
    if user_id is not None:
        # Through the order, which is what carries the payer.
        filters.append(
            PaymentLogs.order_id.in_(
                select(PaymentOrders.id).where(PaymentOrders.user_id == user_id)
            )
        )
    if search and search.strip():
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                PaymentLogs.razorpay_payment_id.ilike(term),
                PaymentLogs.order_id.in_(
                    select(PaymentOrders.id).where(
                        or_(
                            PaymentOrders.razorpay_order_id.ilike(term),
                            PaymentOrders.receipt.ilike(term),
                        )
                    )
                ),
            )
        )

    for condition in filters:
        query = query.where(condition)
        count_query = count_query.where(condition)

    total = await session.scalar(count_query)
    rows = await session.scalars(
        query.order_by(PaymentLogs.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows), (total or 0)


async def get_payment_totals(session: AsyncSession) -> dict:
    """Headline figures for the payments page, overall and split by whose
    Razorpay account the money went to: the platform's, or clubs' own."""
    on_club = PaymentOrders.payment_gateway_id.isnot(None)
    live = PaymentLogs.is_deleted == False  # noqa: E712
    captured = PaymentLogs.status == PaymentStatus.captured
    refunded = PaymentLogs.status == PaymentStatus.refunded
    failed = PaymentLogs.status == PaymentStatus.failed
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(PaymentLogs.amount).filter(captured, ~on_club), 0),
                func.coalesce(func.sum(PaymentLogs.amount).filter(refunded, ~on_club), 0),
                func.count(PaymentLogs.id).filter(failed, ~on_club),
                func.coalesce(func.sum(PaymentLogs.amount).filter(captured, on_club), 0),
                func.coalesce(func.sum(PaymentLogs.amount).filter(refunded, on_club), 0),
                func.count(PaymentLogs.id).filter(failed, on_club),
            )
            .select_from(PaymentLogs)
            .join(PaymentOrders, PaymentOrders.id == PaymentLogs.order_id)
            .where(live)
        )
    ).one()

    def figures(cap, ref, fail) -> dict:
        cap, ref = money.rupees(cap), money.rupees(ref)
        return {
            "captured_total": float(cap),
            "refunded_total": float(ref),
            # Net of refunds: the gross figure alone overstates what was kept.
            "net_total": float(cap - ref),
            "failed_count": int(fail or 0),
        }

    platform = figures(row[0], row[1], row[2])
    club = figures(row[3], row[4], row[5])
    overall = figures(
        money.rupees(row[0]) + money.rupees(row[3]), money.rupees(row[1]) + money.rupees(row[4]), (row[2] or 0) + (row[5] or 0)
    )
    return {**overall, "platform": platform, "club": club}


async def refund_payment(session: AsyncSession, payment_log_id) -> PaymentLogs:
    """Admin-triggered full refund of a captured payment."""
    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.id == payment_log_id)
        .options(joinedload(PaymentLogs.order))
    )
    if not db_payment:
        raise CustomHTTPException(404, "Payment not found")
    if db_payment.status != PaymentStatus.captured:
        raise RequestValidationError(
            payment_log_id="Only a captured payment can be refunded"
        )

    # Through the account the payment was made on: the club's own, or ours.
    from app.api.payment_gateways import service as gateways

    account = await gateways.for_order(session, db_payment.order)
    payment_id = db_payment.razorpay_payment_id
    # Nothing held while Razorpay answers.
    await session.commit()
    # No `amount` param — Razorpay refunds the full captured amount.
    refund_id = _refund_id(await rzp.call(account.client.payment.refund, payment_id))

    db_payment = await session.scalar(
        select(PaymentLogs).where(PaymentLogs.id == payment_log_id).options(joinedload(PaymentLogs.order))
        .with_for_update(of=PaymentLogs).execution_options(populate_existing=True)
    )
    if db_payment.status == PaymentStatus.captured:
        await _apply_refund(session, db_payment, refund_id=refund_id)
    else:
        await session.commit()
    # Read back with its order: the caller reports both, and a refresh alone
    # leaves the order to a lazy load that async IO cannot do.
    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.id == payment_log_id)
        .options(joinedload(PaymentLogs.order))
        .execution_options(populate_existing=True)
    )
    return db_payment


# ─── Webhooks: once each, retried until applied ─────────────────────────

#: How long a delivery may take to apply an event before another delivery of
#: the same event may take it over (the first one died mid-way).
WEBHOOK_LEASE = timedelta(minutes=2)


class WebhookBusy(Exception):
    """Another delivery of the same event is applying it right now: answer
    non-2xx, and Razorpay delivers it again later."""


def _refusal(exc: Exception) -> bool:
    """A refusal that a retry would only repeat (a payment that is not ours,
    a method we cannot read, a payment Razorpay does not have): the event is
    done with, not retried."""
    from razorpay.errors import BadRequestError

    from app.api.payment_gateways import service as gateways

    if isinstance(exc, CustomHTTPException):
        return exc.status_code < 500
    return isinstance(exc, BadRequestError) and not gateways.is_auth_error(exc)


def _describe(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    errors = getattr(exc, "errors", None)
    return f"{type(exc).__name__}: {message}{(' ' + str(errors)) if errors else ''}"[:300]


async def claim_webhook(session: AsyncSession, key: str, *, entity, event, signature, payload) -> str:
    """
    The idempotency gate. "mine": this delivery applies the event (a new
    one, or one a delivery before failed or died applying). "duplicate": it
    was applied. "busy": another delivery is applying it now. Commits.

    The row is kept from the first delivery (evidence of what came), and
    `processed_at` is stamped only once applying it has committed: a failure
    applying it used to leave the row behind as if it had been processed, so
    Razorpay's retry was dropped as a duplicate and the event was lost.
    """
    now = _now()
    table = RazorpayWebhookLogs.__table__
    inserted = (await session.execute(
        pg_insert(table)
        .values(
            id=uuid.uuid4(), event_id=key, entity=entity or "event", event=event or "unknown",
            signature=signature or "", payload=payload or {}, claimed_at=now, attempts=1,
            created_at=now, updated_at=now, is_deleted=False,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(table.c.id)
    )).first()
    if inserted is None:
        taken = (await session.execute(
            update(table)
            .where(
                table.c.event_id == key,
                table.c.processed_at.is_(None),
                or_(table.c.claimed_at.is_(None), table.c.claimed_at < now - WEBHOOK_LEASE),
            )
            .values(claimed_at=now, attempts=table.c.attempts + 1, updated_at=now)
            .returning(table.c.id)
        )).first()
        if taken is None:
            processed = await session.scalar(select(table.c.processed_at).where(table.c.event_id == key))
            await session.commit()
            return "duplicate" if processed is not None else "busy"
    await session.commit()
    return "mine"


async def finish_webhook(session: AsyncSession, key: str, *, error: str | None = None) -> None:
    """Applied (or refused for good, with `error`): later deliveries are duplicates."""
    table = RazorpayWebhookLogs.__table__
    await session.execute(
        update(table).where(table.c.event_id == key)
        .values(processed_at=_now(), claimed_at=None, last_error=error, updated_at=_now())
    )
    await session.commit()


async def release_webhook(session: AsyncSession, key: str, *, error: str) -> None:
    """Applying failed: nothing of it was kept, and Razorpay's next delivery applies it."""
    await session.rollback()
    table = RazorpayWebhookLogs.__table__
    await session.execute(
        update(table).where(table.c.event_id == key)
        .values(claimed_at=None, last_error=error[:300], updated_at=_now())
    )
    await session.commit()


async def _run_claimed(session: AsyncSession, key: str, apply):
    """`apply()` for a claimed event: stamped processed when it commits,
    refused for good when it is a refusal, left for Razorpay's retry when it
    fails any other way (re-raised)."""
    try:
        result = await apply()
    except Exception as exc:
        if _refusal(exc):
            await session.rollback()
            logger.warning("Razorpay webhook %s refused: %s", key, _describe(exc))
            await finish_webhook(session, key, error=_describe(exc))
            return None
        await release_webhook(session, key, error=_describe(exc))
        raise
    await finish_webhook(session, key)
    return result


async def handle_razorpay_webhook(
    session: AsyncSession, data: dict, event_id: str, signature: str
):
    event_name = data.get("event") or ""
    claim = await claim_webhook(
        session, event_id, entity=data.get("entity"), event=event_name, signature=signature, payload=data.get("payload"),
    )
    if claim == "duplicate":
        return None
    if claim == "busy":
        raise WebhookBusy()
    await _run_claimed(session, event_id, lambda: _apply_platform_event(session, event_name, data))
    return None


async def _apply_platform_event(session: AsyncSession, event_name: str, data: dict) -> None:
    # Subscription lifecycle. Handled here rather than in its own webhook
    # route because Razorpay posts every event to one URL, and the idempotency
    # log above has to be the single gate for all of them -- a second route
    # would have its own log and could process the same delivery twice.
    if event_name.startswith("subscription."):
        await _handle_subscription_event(session, event_name, data)
        return

    if event_name == "payment.failed" and await _handle_subscription_payment_failure(session, data):
        return

    if event_name == "refund.processed":
        # Covers a refund initiated directly from the Razorpay dashboard,
        # outside our app's own /payments/{id}/refund endpoint.
        refund_entity = data.get("payload", {}).get("refund", {}).get("entity", None)
        if not refund_entity:
            raise RequestValidationError(payload="Invalid refund payload")
        # A club's subscription payment: the revenue ledger's, not a ticket.
        from app.api.subscriptions import ledger as subscription_ledger

        if await subscription_ledger.apply_refund(session, data):
            return

        db_payment = await session.scalar(
            select(PaymentLogs)
            .where(PaymentLogs.razorpay_payment_id == refund_entity.get("payment_id"))
            .options(joinedload(PaymentLogs.order))
        )
        if db_payment is None or db_payment.status != PaymentStatus.captured:
            return
        if db_payment.order.payment_gateway_id is not None:
            # Made on a club's own account: the platform's cannot have refunded it.
            logger.warning("Platform webhook refund for a club-gateway payment %s ignored", db_payment.razorpay_payment_id)
            return
        payment_id, log_id = db_payment.razorpay_payment_id, db_payment.id
        await session.commit()
        # Razorpay is asked whether it was refunded in full: the webhook's
        # word alone does not take a ticket away.
        fetched = await rzp.call(razorpay_client.payment.fetch, payment_id) or {}
        amount = int(fetched.get("amount") or 0)
        if not (fetched.get("status") == "refunded" or (amount > 0 and int(fetched.get("amount_refunded") or 0) >= amount)):
            return
        db_payment = await session.scalar(
            select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order))
            .with_for_update(of=PaymentLogs).execution_options(populate_existing=True)
        )
        if db_payment is not None and db_payment.status == PaymentStatus.captured:
            await _apply_refund(session, db_payment, refund_id=refund_entity.get("id"))
        return

    entity = data.get("payload", {}).get("payment", {}).get("entity", None)
    if not entity:
        raise RequestValidationError(payload="Invalid payload")

    payment_id = entity.get("id")
    claimed_order = entity.get("order_id")
    order = await session.scalar(
        select(PaymentOrders).where(PaymentOrders.razorpay_order_id == claimed_order)
    ) if claimed_order else None
    if order is None:
        # Not an order we made (a subscription's invoice, say): nothing to do.
        await session.commit()
        return
    if order.payment_gateway_id is not None:
        # The order lives on a club's own account; only that account's
        # webhook (handle_club_webhook) may move it.
        logger.warning("Platform webhook for club-gateway order %s ignored", claimed_order)
        await session.commit()
        return
    await session.commit()

    # Only a hint: the payment is fetched from Razorpay, and what Razorpay
    # says is what is applied.
    fetched = await rzp.call(razorpay_client.payment.fetch, payment_id) or {}
    if fetched.get("order_id") != claimed_order:
        logger.warning("Platform webhook: Razorpay says payment %s is not for order %s", payment_id, claimed_order)
        return
    await verify_razorpay_payment(
        session=session,
        razorpay_order_id=claimed_order,
        razorpay_payment_id=payment_id,
        payment_details=fetched,
        expand_payment_details=False,
    )


class ClubWebhookRetry(Exception):
    """Razorpay could not be asked about the payment: answer non-2xx so
    Razorpay delivers the event again."""


#: What a club's webhook acts on. Anything else is recorded and ignored --
#: subscriptions in particular: a club's own account is not where it pays us.
CLUB_PAYMENT_EVENTS = ("payment.authorized", "payment.captured", "payment.failed", "order.paid")
CLUB_REFUND_EVENTS = ("refund.processed",)


async def handle_club_webhook(session: AsyncSession, gateway, data: dict, event_id: str, signature: str) -> str:
    """
    An event from a club's own Razorpay account, its signature already
    checked against that gateway's webhook secret.

    Only a hint. Nothing is marked paid or refunded on its say-so: the payment
    is fetched from Razorpay with the club's own keys, and what Razorpay says
    is what is applied -- so a leaked webhook secret cannot mint a ticket. Only
    orders made on this gateway are touched.

    Recorded in the same idempotent log as the platform's webhook, keyed by
    gateway: one Razorpay account connected by two clubs delivers the same
    event to both, and each acts on its own orders. Returns what was done.
    """
    from app.api.payment_gateways import service as gateways

    log_key = f"gw{gateway.id}:{event_id}"
    gateway_id = gateway.id
    gateway.last_webhook_at = _now()
    if gateway.status == gateways.ACTIVE and gateway.last_error and "webhook" in gateway.last_error.lower():
        gateway.last_error = None
    await session.commit()

    event_name = data.get("event") or ""
    claim = await claim_webhook(
        session, log_key, entity=data.get("entity") or "event", event=event_name or "unknown",
        signature=signature, payload=data.get("payload") or {},
    )
    if claim == "duplicate":
        return "duplicate"
    if claim == "busy":
        raise WebhookBusy()

    async def apply():
        from app.api.payment_gateways.models import ClubPaymentGateways

        fresh = await session.get(ClubPaymentGateways, gateway_id)
        return await _apply_club_event(session, fresh, event_name, data)

    outcome = await _run_claimed(session, log_key, apply)
    return outcome if outcome is not None else "ignored"


async def _fetch_payment(session, gateway, client, payment_id: str) -> dict | None:
    """The payment as Razorpay holds it in the club's account; None when
    Razorpay says there is no such payment there. Anything else that stops
    the answer (Razorpay down, the keys refused) is a retry. Called with no
    transaction open."""
    from razorpay.errors import BadRequestError

    from app.api.payment_gateways import service as gateways

    try:
        return await rzp.call(client.payment.fetch, payment_id) or {}
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, BadRequestError) and not gateways.is_auth_error(exc):
            logger.warning("Club gateway %s: Razorpay has no payment %s", gateway.id, payment_id)
            return None
        if gateways.is_auth_error(exc):
            await gateways.mark_failed(
                session, gateway, f"Razorpay refused the keys when checking a payment: {gateways.short_reason(exc)}"
            )
            await session.commit()
        logger.warning("Club gateway %s: could not fetch payment %s: %s", gateway.id, payment_id, type(exc).__name__)
        raise ClubWebhookRetry() from None


async def _apply_club_event(session: AsyncSession, gateway, event_name: str, data: dict) -> str:
    from app.api.payment_gateways import crypto
    from app.api.payment_gateways import service as gateways

    if event_name not in CLUB_PAYMENT_EVENTS + CLUB_REFUND_EVENTS:
        return "ignored"
    payload = data.get("payload") or {}

    try:
        client = gateways.client_for(gateway)
    except crypto.Unavailable:
        logger.error("Club gateway %s: keys cannot be read; webhook left for a retry", gateway.id)
        raise ClubWebhookRetry() from None

    if event_name in CLUB_REFUND_EVENTS:
        refund = (payload.get("refund") or {}).get("entity") or {}
        payment_id = refund.get("payment_id")
        if not payment_id:
            return "ignored"
        db_payment = await session.scalar(
            select(PaymentLogs)
            .where(PaymentLogs.razorpay_payment_id == payment_id)
            .options(joinedload(PaymentLogs.order))
        )
        if db_payment is None or db_payment.order.payment_gateway_id != gateway.id:
            return "ignored"
        if db_payment.status != PaymentStatus.captured:
            return "ignored"
        razorpay_order_id, log_id = db_payment.order.razorpay_order_id, db_payment.id
        await session.commit()
        fetched = await _fetch_payment(session, gateway, client, payment_id)
        if not fetched or fetched.get("order_id") != razorpay_order_id:
            return "ignored"
        amount = int(fetched.get("amount") or 0)
        refunded = int(fetched.get("amount_refunded") or 0)
        # A full refund only: a partial one (the merch, say) leaves the ticket paid.
        if fetched.get("status") == "refunded" or (amount > 0 and refunded >= amount):
            db_payment = await session.scalar(
                select(PaymentLogs).where(PaymentLogs.id == log_id).options(joinedload(PaymentLogs.order))
                .with_for_update(of=PaymentLogs).execution_options(populate_existing=True)
            )
            if db_payment.status == PaymentStatus.captured:
                await _apply_refund(session, db_payment, refund_id=refund.get("id"))
            return "refunded"
        return "ignored"

    entity = (payload.get("payment") or {}).get("entity") or {}
    payment_id = entity.get("id")
    if not payment_id:
        return "ignored"
    # Cheap first: a payment for an order we never made on this gateway (the
    # club sells other things on its account) is none of ours.
    claimed_order = entity.get("order_id")
    if claimed_order:
        mine = await session.scalar(
            select(PaymentOrders.id).where(
                PaymentOrders.razorpay_order_id == claimed_order,
                PaymentOrders.payment_gateway_id == gateway.id,
            )
        )
        if mine is None:
            return "ignored"
    await session.commit()

    fetched = await _fetch_payment(session, gateway, client, payment_id)
    if not fetched:
        return "ignored"
    order_id = fetched.get("order_id")
    order = await session.scalar(
        select(PaymentOrders).where(PaymentOrders.razorpay_order_id == order_id)
    ) if order_id else None
    if order is None or order.payment_gateway_id != gateway.id:
        return "ignored"
    try:
        db_payment = await verify_razorpay_payment(
            session=session,
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            payment_details=fetched,
            expand_payment_details=False,
        )
    except RequestValidationError as exc:
        # Already paid by another payment, or a method this app does not
        # read: a retry would say the same.
        logger.warning("Club gateway %s: payment %s not applied: %s", gateway.id, payment_id, exc.errors)
        await session.rollback()
        return "ignored"
    status = getattr(db_payment, "status", None)
    return str(getattr(status, "value", status) or "recorded")


# ─── Subscriptions (the platform's webhook) ─────────────────────────────


async def _handle_subscription_event(session: AsyncSession, event: str, data: dict):
    """
    Apply a Razorpay subscription event to our mirror of it.

    Matched on Razorpay's own subscription id first, falling back to the
    club_id we put in the subscription's notes -- the id is authoritative, the
    note is what makes a subscription created outside this app findable.

    Razorpay does not deliver events in order. One older than the newest
    already applied to the row (`last_event_at`, from Razorpay's own
    `created_at`) changes nothing -- a `charged` among them still goes into
    the revenue ledger, since the money moved whatever the order.

    A charge on a Razorpay subscription that no row holds and none can adopt
    (a second checkout from a double click before the lock, one made
    outside this app) is an orphan: the admins are told, with what was
    charged, and it is cancelled at Razorpay so it charges nobody again.
    """
    from app.api.subscriptions import billing as subscription_billing
    from app.api.subscriptions import service as subscription_service
    from app.api.subscriptions.models import BillingCycle, ClubSubscriptions, Plans

    if event not in subscription_billing.HANDLED_EVENTS:
        logger.info("Ignoring Razorpay event %s", event)
        return None

    razorpay_id = subscription_billing.subscription_id_from_payload(data)
    subscription = None
    if razorpay_id:
        subscription = await session.scalar(
            select(ClubSubscriptions)
            .where(ClubSubscriptions.razorpay_subscription_id == razorpay_id)
            .options(selectinload(ClubSubscriptions.plan))
            # Waits for an admin replacing this subscription with a
            # complimentary plan (which moves the id off the row) and then
            # finds nothing, rather than cancelling the comp it never saw.
            .with_for_update(of=ClubSubscriptions)
        )

    new_plan = new_cycle = None
    if subscription is None and event in subscription_billing.CONFIRMING_EVENTS:
        # A subscription we hold no id for, being authorised or paid: one
        # whose checkout fell back before the club finished it, or one made
        # outside this app. Found by the club in its notes and adopted -- but
        # never over a different subscription that can still charge the club,
        # and only for confirming events: a stray `cancelled` for an old id
        # must not end whatever the club has now.
        club_id = subscription_billing.club_id_from_payload(data)
        if club_id is not None:
            candidate = await session.scalar(
                select(ClubSubscriptions)
                .where(
                    ClubSubscriptions.club_id == club_id,
                    ClubSubscriptions.is_deleted == False,
                )
                .options(selectinload(ClubSubscriptions.plan))
                .with_for_update(of=ClubSubscriptions)
            )
            if candidate is not None and candidate.complimentary:
                # A plan given for nothing: never replaced by a Razorpay one.
                candidate = None
            live_elsewhere = (
                candidate is not None
                and candidate.razorpay_subscription_id
                and candidate.razorpay_subscription_id != razorpay_id
                and candidate.status in subscription_service.LIVE_BILLED_STATUSES
            )
            if candidate is not None and not live_elsewhere and razorpay_id:
                subscription = candidate
                subscription.razorpay_subscription_id = razorpay_id
                # Another Razorpay subscription's events start their own order.
                subscription.last_event_at = None
                notes = subscription_billing.notes_from_payload(data)
                noted = await session.scalar(
                    select(Plans).where(Plans.key == notes.get("plan_key"), Plans.is_deleted == False)
                ) if notes.get("plan_key") else None
                if noted is not None:
                    subscription.plan_id = noted.id
                    subscription.plan = noted
                if notes.get("cycle") in {c.value for c in BillingCycle}:
                    subscription.cycle = BillingCycle(notes["cycle"])
                if subscription.price_paise is None and subscription.plan is not None:
                    subscription.price_paise = subscription_service.price_for(subscription.plan, subscription.cycle)

    if subscription is None:
        if event == "subscription.charged" and razorpay_id:
            await _orphan_charged(session, razorpay_id, data)
            return None
        # Logged, not raised: a 500 makes Razorpay retry forever, and an event
        # for a club we cannot find will not resolve itself on a retry.
        logger.warning(
            "Razorpay %s for unknown subscription (razorpay_id=%s)", event, razorpay_id
        )
        return None

    from app.api.subscriptions import ledger as subscription_ledger

    event_at = subscription_billing.event_time(data)
    if event_at is not None and subscription.last_event_at is not None and event_at < subscription.last_event_at:
        logger.info(
            "Razorpay %s for %s is older than the last event applied; not applied", event, razorpay_id
        )
        if event == "subscription.charged":
            await subscription_ledger.record_charge(session, subscription, data)
        await session.commit()
        return None

    if event == "subscription.updated":
        rp_plan = subscription_billing.razorpay_plan_id_from_payload(data)
        if rp_plan:
            new_plan = await session.scalar(
                select(Plans).where(
                    (Plans.razorpay_plan_id_monthly == rp_plan) | (Plans.razorpay_plan_id_yearly == rp_plan)
                )
            )
            if new_plan is not None:
                new_cycle = (
                    BillingCycle.yearly if new_plan.razorpay_plan_id_yearly == rp_plan else BillingCycle.monthly
                )

    from app.api.subscriptions import notices as subscription_notices

    before = subscription_notices.capture(subscription)
    if event_at is not None:
        subscription.last_event_at = event_at
    if subscription_billing.apply_event(subscription, event, data, new_plan=new_plan, new_cycle=new_cycle):
        await session.flush()
        if event == "subscription.charged":
            # The payment, into the platform's revenue ledger, once.
            await subscription_ledger.record_charge(session, subscription, data)
        # The admins' alerts and the club's mails, in the same transaction:
        # they are about this change, and go out only if it commits.
        await subscription_notices.after_webhook(session, subscription, event, data, before)
    await session.commit()
    return None


async def _orphan_charged(session: AsyncSession, razorpay_id: str, data: dict) -> None:
    """A charge on a Razorpay subscription no row holds: the admins hear,
    with the club (from its notes) and the payment, and the subscription is
    cancelled at Razorpay, best effort, so it charges nobody again. The money
    already taken is for an admin to refund."""
    from app.api.admin_alerts import notify
    from app.api.subscriptions import billing as subscription_billing

    club_id = subscription_billing.club_id_from_payload(data)
    payment = subscription_billing.payment_from_payload(data)
    await session.commit()
    cancelled = "cancelled at Razorpay"
    try:
        await subscription_billing.cancel_subscription(razorpay_id, at_period_end=False)
    except Exception as exc:  # noqa: BLE001
        cancelled = f"could not be cancelled at Razorpay ({type(exc).__name__}): cancel it by hand"
        logger.warning("Orphan subscription %s could not be cancelled", razorpay_id, exc_info=True)
    amount = money.from_paise(payment.get("amount")) if payment.get("amount") is not None else None
    await notify(
        session, "subscription_orphan_charged",
        title=f"Razorpay charged {f'₹{amount}' if amount is not None else 'a club'} on a subscription no club holds",
        body=(
            f"Subscription {razorpay_id}, payment {payment.get('id') or 'unknown'}. The club's own subscription is "
            f"another one; this one was {cancelled}. Refund the payment if it was not meant."
        ),
        club=club_id,
        data={"razorpay_subscription_id": razorpay_id, "payment_id": payment.get("id"),
              "amount_paise": payment.get("amount"), "cancelled": cancelled == "cancelled at Razorpay"},
        dedupe_on={"razorpay_subscription_id": razorpay_id},
    )
    await session.commit()


async def _handle_subscription_payment_failure(session: AsyncSession, data: dict) -> bool:
    """
    A failed charge on a club's subscription: say why on the row, and a
    running subscription becomes past due. Returns False when the payment is
    not a subscription's, so the caller handles it as before.
    """
    from app.api.subscriptions import billing as subscription_billing
    from app.api.subscriptions.models import ClubSubscriptions

    razorpay_id = await subscription_billing.subscription_id_for_payment(data)
    if not razorpay_id:
        return False
    subscription = await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.razorpay_subscription_id == razorpay_id)
        .options(selectinload(ClubSubscriptions.plan))
    )
    if subscription is None:
        logger.warning("Razorpay payment.failed for unknown subscription %s", razorpay_id)
        return True
    from app.api.subscriptions import notices as subscription_notices

    before = subscription_notices.capture(subscription)
    if subscription_billing.apply_payment_failure(subscription, data):
        await session.flush()
        # The attempt, in the revenue ledger as `failed` (never summed).
        from app.api.subscriptions import ledger as subscription_ledger

        await subscription_ledger.record_failure(session, subscription, data)
        await subscription_notices.after_payment_failure(session, subscription, data, before)
        await session.commit()
    return True
