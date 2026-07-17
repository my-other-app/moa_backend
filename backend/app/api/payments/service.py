from datetime import datetime
import hashlib
import hmac
import time
import traceback
from fastapi import BackgroundTasks
import razorpay
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

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
        select(PaymentOrders).where(PaymentOrders.receipt == db_receipt)
    )

    if existing_order:
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

    await session.commit()


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
