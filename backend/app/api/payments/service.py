from datetime import datetime
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


async def create_razorpay_order(
    session: AsyncSession, source: str, payload: dict, user_id: int
):
    amount_in_rupee, db_receipt = await validate_payload(
        session=session, source=source, payload=payload
    )

    # Serialize concurrent order-creation attempts for the same receipt so two
    # near-simultaneous requests can't both miss the existing-order check below
    # and each create a separate Razorpay order (double-charge risk). This lock
    # is transaction-scoped and releases automatically at commit/rollback.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(db_receipt))))

    current_timestamp = int(time.time())
    receipt = f"{db_receipt}#{current_timestamp}"

    existing_order = await session.scalar(
        select(PaymentOrders).where(PaymentOrders.receipt == db_receipt, PaymentOrders.status != OrderStatus.paid, PaymentOrders.status != OrderStatus.refunded).order_by(PaymentOrders.created_at.desc())
    )
    # Reused only while it still asks for the same money: a changed pick
    # from the shelf means a fresh order, not a stale amount.
    if existing_order and float(existing_order.amount) == float(amount_in_rupee):
        return existing_order
        return existing_order

    amount_in_paise = int(amount_in_rupee * 100)

    order_data = {
        "amount": amount_in_paise,
        "currency": "INR",
        "notes": payload,
        "receipt": receipt,
    }

    razorpay_order = razorpay_client.order.create(data=order_data)

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

    if not payment_details:
        payment_details = razorpay_client.payment.fetch(razorpay_payment_id)

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
            payment_details = razorpay_client.payment.fetch(
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
):
    """
    Every payment, for an admin.

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
    """Headline figures for the payments page."""
    captured = await session.scalar(
        select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(
            PaymentLogs.status == PaymentStatus.captured,
            PaymentLogs.is_deleted == False,
        )
    )
    refunded = await session.scalar(
        select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(
            PaymentLogs.status == PaymentStatus.refunded,
            PaymentLogs.is_deleted == False,
        )
    )
    failed = await session.scalar(
        select(func.count(PaymentLogs.id)).where(
            PaymentLogs.status == PaymentStatus.failed,
            PaymentLogs.is_deleted == False,
        )
    )
    return {
        "captured_total": float(captured or 0),
        "refunded_total": float(refunded or 0),
        # Net of refunds: the gross figure alone overstates what was kept.
        "net_total": float((captured or 0) - (refunded or 0)),
        "failed_count": int(failed or 0),
    }


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

    # No `amount` param — Razorpay refunds the full captured amount.
    razorpay_client.payment.refund(db_payment.razorpay_payment_id)

    await _apply_refund(session, db_payment)
    await session.refresh(db_payment)
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
        if db_payment and db_payment.status == PaymentStatus.captured:
            await _apply_refund(session, db_payment)
        return None

    payload = data.get("payload", {}).get("payment", {}).get("entity", None)
    if not payload:
        raise RequestValidationError(payload="Invalid payload")

    payment_id = payload.get("id")
    order_id = payload.get("order_id")

    await verify_razorpay_payment(
        session=session,
        razorpay_order_id=order_id,
        razorpay_payment_id=payment_id,
        payment_details=payload,
        expand_payment_details=False,
    )

    await session.commit()

    return None


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
