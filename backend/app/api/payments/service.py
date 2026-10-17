from datetime import datetime, timezone
import hashlib
import hmac
import time
import traceback
from fastapi import BackgroundTasks
import razorpay
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.api.payments.handlers import handle_post_payment, validate_payload
from app.api.payments.models import (
    OrderStatus,
    PaymentLogs,
    PaymentOrders,
    PaymentStatus,
    RazorpayWebhookLogs,
)
from app.api.events.models import EventRegistrationsLink
from app.core.validations.exceptions import RequestValidationError
from app.response import CustomHTTPException
from app.config import settings
from app.api.payments.background_tasks import send_payment_confirmation_email

import logging

razorpay_client = razorpay.Client(
    auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
)

logger = logging.getLogger(__name__)


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
    """
    from app.api.payment_gateways import service as gateways
    from app.api.payments.handlers import event_for_payload
    from app.core import public_urls

    amount_in_rupee, db_receipt = await validate_payload(
        session=session, source=source, payload=payload
    )
    event = await event_for_payload(session, source, payload)
    club_id = event.club_id if event is not None else None
    checkout = await gateways.for_new_order(session, club_id)
    if checkout.kind == gateways.CLUB and not gateways.supports_club_checkout(checkout_version):
        # Before anything is written or locked: there is nothing to undo.
        raise UpdateAppToPay(public_urls.event_url(event.slug) if event is not None else None)

    # Serialize concurrent order-creation attempts for the same receipt so two
    # near-simultaneous requests can't both miss the existing-order check below
    # and each create a separate Razorpay order (double-charge risk). This lock
    # is transaction-scoped and releases automatically at commit/rollback.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(db_receipt))))

    current_timestamp = int(time.time())
    receipt = f"{db_receipt}#{current_timestamp}"
    amount_in_paise = int(amount_in_rupee * 100)
    order_data = {
        "amount": amount_in_paise,
        "currency": "INR",
        "notes": payload,
        "receipt": receipt,
    }

    # Twice at most: keys the club's account refuses mark it failed, and the
    # second pass makes the order on the platform's instead.
    for attempt in range(2):
        if attempt:
            checkout = await gateways.for_new_order(session, club_id)

        same_account = (
            PaymentOrders.payment_gateway_id.is_(None)
            if checkout.gateway_id is None
            else PaymentOrders.payment_gateway_id == checkout.gateway_id
        )
        existing_order = await session.scalar(
            select(PaymentOrders).where(PaymentOrders.receipt == db_receipt, PaymentOrders.status != OrderStatus.paid, PaymentOrders.status != OrderStatus.refunded, same_account).order_by(PaymentOrders.created_at.desc())
        )
        # Reused only while it still asks for the same money, on the same
        # account: a changed pick from the shelf, or a club that connected or
        # dropped its own Razorpay since, means a fresh order.
        if existing_order and float(existing_order.amount) == float(amount_in_rupee):
            await session.commit()
            return existing_order

        try:
            razorpay_order = checkout.client.order.create(data=order_data)
        except Exception as exc:
            if checkout.kind == gateways.CLUB and attempt == 0 and gateways.is_auth_error(exc):
                await gateways.mark_failed(
                    session, checkout.gateway,
                    f"Razorpay refused the keys when making an order: {gateways.short_reason(exc)}",
                )
                await session.flush()
                continue
            raise
        break

    db_order = PaymentOrders(
        receipt=db_receipt,
        razorpay_receipt=receipt,
        source=source,
        payload=payload,
        razorpay_order_id=razorpay_order["id"],
        amount=amount_in_rupee,
        currency="INR",
        status=OrderStatus.created,
        user_id=user_id,
        payment_gateway_id=checkout.gateway_id,
    )

    session.add(db_order)
    await session.commit()
    await session.refresh(db_order)
    return db_order


async def verify_razorpay_payment(
    session: AsyncSession,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    payment_details: dict | None = None,
    expand_payment_details: bool = True,
    background_tasks: BackgroundTasks | None = None,
    send_receipt: bool = True,
):
    order = await session.scalar(
        select(PaymentOrders).where(
            PaymentOrders.razorpay_order_id == razorpay_order_id
        )
    )

    if not order:
        raise CustomHTTPException(404, "Payment order not found")

    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.razorpay_payment_id == razorpay_payment_id)
        .options(joinedload(PaymentLogs.order))
    )

    if db_payment and db_payment.status == PaymentStatus.captured:
        return db_payment

    if order.status == OrderStatus.paid:
        raise RequestValidationError(razorpay_order_id="Order already paid")

    # The account the order was made on -- the club's own, or the platform's --
    # whatever the club has connected since.
    from app.api.payment_gateways import service as gateways

    account = None

    async def client():
        nonlocal account
        if account is None:
            account = await gateways.for_order(session, order)
        return account.client

    if not payment_details:
        payment_details = (await client()).payment.fetch(razorpay_payment_id)

    # Make sure the payment being verified was actually made against this
    # order, not a different one — otherwise a real captured payment for
    # order A could be used to mark an unrelated order B as paid.
    if payment_details.get("order_id") != razorpay_order_id:
        raise RequestValidationError(
            razorpay_order_id="This payment does not belong to the specified order."
        )

    if not db_payment:
        db_payment = PaymentLogs(
            order_id=order.id,
            razorpay_payment_id=razorpay_payment_id,
            status=PaymentStatus.created,
            amount=0,
        )
        session.add(db_payment)

    payment_status = payment_details.get("status", None)

    payment_method = payment_details.get("method", None)

    payment_method_details = {}

    if expand_payment_details:
        if payment_method in ("card"):
            payment_details = (await client()).payment.fetch(
                razorpay_payment_id, {"expand[]": payment_method}
            )

    if payment_method == "upi":

        payment_method_details = payment_details.get("upi", None)

    elif payment_method == "netbanking":

        payment_method_details = payment_details.get("acquirer_data", {})
        payment_method_details["bank"] = payment_method_details.get("bank", None)

    elif payment_method == "card":

        payment_method_details = payment_method_details.get("card", None)
        if not payment_method_details:
            payment_method_details = payment_details.get("acquirer_data", None)

    elif payment_method == "wallet":

        payment_method_details = payment_details.get("acquirer_data", {})
        payment_method_details["wallet"] = payment_details.get("wallet", None)

    else:

        raise RequestValidationError(payment_method="Invalid payment method")

    if payment_status == "captured":
        db_payment.status = PaymentStatus.captured
        # Razorpay reports amounts in paise; normalize to rupees so this
        # column matches every other money column in the schema.
        db_payment.amount = payment_details.get("amount", 0) / 100
        db_payment.payment_method = payment_method
        db_payment.payment_details = payment_method_details
        order.status = OrderStatus.paid
        await session.commit()
        await session.refresh(order)
        await handle_post_payment(session, order, background_tasks=background_tasks)
        if send_receipt:
            await session.refresh(db_payment)
            await session.refresh(order)
            order = await session.scalar(
                select(PaymentOrders)
                .where(PaymentOrders.id == order.id)
                .options(joinedload(PaymentOrders.user))
            )
            try:
                if order.user:
                    send_payment_confirmation_email(
                        subject="Payment Receipt",
                        payload={
                            "payer_name": order.user.full_name,
                            "payer_email": order.user.email,
                            "payer_phone": order.user.phone or "N/A",
                            "amount": f"₹{order.amount}",
                            "receipt_id": order.receipt,
                            "payment_method": db_payment.payment_method,
                            "timestamp": db_payment.created_at,
                            "purpose": "Event Registration",
                            "notes": "N/A",
                            "current_year": datetime.now().year,
                        },
                        recipients=[order.user.email],
                        background_tasks=background_tasks,
                    )
            except Exception as e:
                logger.exception("Error sending payment confirmation email")
    else:
        db_payment.status = payment_status
        db_payment.amount = payment_details.get("amount", 0) / 100
        db_payment.payment_method = payment_method
        db_payment.payment_details = payment_method_details
        order.status = OrderStatus.attempted
        await session.commit()

    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.razorpay_payment_id == razorpay_payment_id)
        .options(joinedload(PaymentLogs.order))
    )
    return db_payment


async def _apply_refund(session: AsyncSession, db_payment: PaymentLogs) -> None:
    """Shared state-reversal for a captured payment being refunded — used by
    both the admin-triggered refund endpoint and the refund.processed
    webhook branch, so a refund initiated directly from the Razorpay
    dashboard converges on the same state as one triggered from our app.
    """
    db_payment.status = PaymentStatus.refunded
    order = db_payment.order
    order.status = OrderStatus.refunded

    # Re-blocks check-in via the existing has_fee-and-not-is_paid gate in
    # checkin_user/mark_attendance — no changes needed there.
    event_registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.payment_receipt == order.receipt
        )
    )
    if event_registration:
        event_registration.is_paid = False
        event_registration.paid_amount = 0
        # A full refund gives the shelf its pieces back too.
        from app.api.store import service as store

        await store.mark_refunded(session, event_registration.id)

    await session.commit()


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
        return {
            "captured_total": float(cap or 0),
            "refunded_total": float(ref or 0),
            # Net of refunds: the gross figure alone overstates what was kept.
            "net_total": float((cap or 0) - (ref or 0)),
            "failed_count": int(fail or 0),
        }

    platform = figures(row[0], row[1], row[2])
    club = figures(row[3], row[4], row[5])
    overall = figures(
        (row[0] or 0) + (row[3] or 0), (row[1] or 0) + (row[4] or 0), (row[2] or 0) + (row[5] or 0)
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
    # No `amount` param — Razorpay refunds the full captured amount.
    account.client.payment.refund(db_payment.razorpay_payment_id)

    await _apply_refund(session, db_payment)
    # Read back with its order: the caller reports both, and a refresh alone
    # leaves the order to a lazy load that async IO cannot do.
    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.id == db_payment.id)
        .options(joinedload(PaymentLogs.order))
        .execution_options(populate_existing=True)
    )
    return db_payment


async def handle_razorpay_webhook(
    session: AsyncSession, data: dict, event_id: str, signature: str
):
    existing_webhook_event = await session.scalar(
        select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == event_id)
    )
    if existing_webhook_event:
        # Already processed (or a concurrent delivery is processing it) — no-op.
        return None

    webhook_log = RazorpayWebhookLogs(
        event_id=event_id,
        entity=data.get("entity"),
        event=data.get("event"),
        signature=signature,
        payload=data.get("payload"),
    )
    session.add(webhook_log)
    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent deliveries of the same event both passed the check
        # above; whichever loses the unique-constraint race is a no-op too.
        await session.rollback()
        return None

    event_name = data.get("event") or ""

    # Subscription lifecycle. Handled here rather than in its own webhook
    # route because Razorpay posts every event to one URL, and the idempotency
    # log above has to be the single gate for all of them -- a second route
    # would have its own log and could process the same delivery twice.
    if event_name.startswith("subscription."):
        await _handle_subscription_event(session, event_name, data)
        return None

    if event_name == "payment.failed" and await _handle_subscription_payment_failure(session, data):
        return None

    if data.get("event") == "refund.processed":
        # Covers a refund initiated directly from the Razorpay dashboard,
        # outside our app's own /payments/{id}/refund endpoint.
        refund_entity = data.get("payload", {}).get("refund", {}).get("entity", None)
        if not refund_entity:
            raise RequestValidationError(payload="Invalid refund payload")

        db_payment = await session.scalar(
            select(PaymentLogs)
            .where(PaymentLogs.razorpay_payment_id == refund_entity.get("payment_id"))
            .options(joinedload(PaymentLogs.order))
        )
        if db_payment and db_payment.order.payment_gateway_id is not None:
            # Made on a club's own account: the platform's cannot have refunded it.
            logger.warning("Platform webhook refund for a club-gateway payment %s ignored", db_payment.razorpay_payment_id)
            return None
        if db_payment and db_payment.status == PaymentStatus.captured:
            await _apply_refund(session, db_payment)
        return None

    payload = data.get("payload", {}).get("payment", {}).get("entity", None)
    if not payload:
        raise RequestValidationError(payload="Invalid payload")

    payment_id = payload.get("id")
    order_id = payload.get("order_id")

    on_club_account = await session.scalar(
        select(PaymentOrders.payment_gateway_id).where(PaymentOrders.razorpay_order_id == order_id)
    )
    if on_club_account is not None:
        # The order lives on a club's own account; only that account's
        # webhook (handle_club_webhook) may move it.
        logger.warning("Platform webhook for club-gateway order %s ignored", order_id)
        return None

    await verify_razorpay_payment(
        session=session,
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        payment_details=payload,
        expand_payment_details=False,
    )

    await session.commit()

    return None


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
    from sqlalchemy import delete

    from app.api.payment_gateways import service as gateways

    log_key = f"gw{gateway.id}:{event_id}"
    gateway.last_webhook_at = datetime.now(timezone.utc)
    if gateway.status == gateways.ACTIVE and gateway.last_error and "webhook" in gateway.last_error.lower():
        gateway.last_error = None

    existing = await session.scalar(select(RazorpayWebhookLogs.id).where(RazorpayWebhookLogs.event_id == log_key))
    if existing:
        await session.commit()
        return "duplicate"

    event_name = data.get("event") or ""
    session.add(
        RazorpayWebhookLogs(
            event_id=log_key,
            entity=data.get("entity") or "event",
            event=event_name or "unknown",
            signature=signature or "",
            payload=data.get("payload") or {},
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "duplicate"

    try:
        return await _apply_club_event(session, gateway, event_name, data)
    except ClubWebhookRetry:
        # Nothing was changed yet. The log row goes, so Razorpay's next
        # delivery is not mistaken for a duplicate.
        await session.execute(delete(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == log_key))
        await session.commit()
        raise


async def _fetch_payment(session, gateway, client, payment_id: str) -> dict | None:
    """The payment as Razorpay holds it in the club's account; None when
    Razorpay says there is no such payment there. Anything else that stops
    the answer (Razorpay down, the keys refused) is a retry."""
    from razorpay.errors import BadRequestError

    from app.api.payment_gateways import service as gateways

    try:
        return client.payment.fetch(payment_id) or {}
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
        fetched = await _fetch_payment(session, gateway, client, payment_id)
        if not fetched or fetched.get("order_id") != db_payment.order.razorpay_order_id:
            return "ignored"
        amount = int(fetched.get("amount") or 0)
        refunded = int(fetched.get("amount_refunded") or 0)
        # A full refund only: a partial one (the merch, say) leaves the ticket paid.
        if fetched.get("status") == "refunded" or (amount > 0 and refunded >= amount):
            await _apply_refund(session, db_payment)
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
        if session.new or session.dirty or session.deleted:
            await session.rollback()
        return "ignored"
    await session.commit()
    status = getattr(db_payment, "status", None)
    return str(getattr(status, "value", status) or "recorded")


async def _handle_subscription_event(session: AsyncSession, event: str, data: dict):
    """
    Apply a Razorpay subscription event to our mirror of it.

    Matched on Razorpay's own subscription id first, falling back to the
    club_id we put in the subscription's notes -- the id is authoritative, the
    note is what makes a subscription created outside this app findable.
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
            )
            live_elsewhere = (
                candidate is not None
                and candidate.razorpay_subscription_id
                and candidate.razorpay_subscription_id != razorpay_id
                and candidate.status in subscription_service.LIVE_BILLED_STATUSES
            )
            if candidate is not None and not live_elsewhere and razorpay_id:
                subscription = candidate
                subscription.razorpay_subscription_id = razorpay_id
                notes = subscription_billing.notes_from_payload(data)
                noted = await session.scalar(
                    select(Plans).where(Plans.key == notes.get("plan_key"), Plans.is_deleted == False)
                ) if notes.get("plan_key") else None
                if noted is not None:
                    subscription.plan_id = noted.id
                    subscription.plan = noted
                if notes.get("cycle") in {c.value for c in BillingCycle}:
                    subscription.cycle = BillingCycle(notes["cycle"])

    if subscription is None:
        # Logged, not raised: a 500 makes Razorpay retry forever, and an event
        # for a club we cannot find will not resolve itself on a retry.
        logger.warning(
            "Razorpay %s for unknown subscription (razorpay_id=%s)", event, razorpay_id
        )
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
    if subscription_billing.apply_event(subscription, event, data, new_plan=new_plan, new_cycle=new_cycle):
        await session.flush()
        # The admins' alerts and the club's mails, in the same transaction:
        # they are about this change, and go out only if it commits.
        await subscription_notices.after_webhook(session, subscription, event, data, before)
        await session.commit()
    return None


async def _handle_subscription_payment_failure(session: AsyncSession, data: dict) -> bool:
    """
    A failed charge on a club's subscription: say why on the row, and a
    running subscription becomes past due. Returns False when the payment is
    not a subscription's, so the caller handles it as before.
    """
    from app.api.subscriptions import billing as subscription_billing
    from app.api.subscriptions.models import ClubSubscriptions

    razorpay_id = subscription_billing.subscription_id_for_payment(data)
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
        await subscription_notices.after_payment_failure(session, subscription, data, before)
        await session.commit()
    return True
