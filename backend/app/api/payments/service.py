from datetime import datetime
import hashlib
import hmac
import time
import traceback
from fastapi import BackgroundTasks
import razorpay
from sqlalchemy import select
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
from app.core.validations.exceptions import RequestValidationError
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

    db_payment = await session.scalar(
        select(PaymentLogs)
        .where(PaymentLogs.razorpay_payment_id == razorpay_payment_id)
        .options(joinedload(PaymentLogs.order))
    )

    if db_payment and db_payment.status == PaymentStatus.captured:
        return db_payment

    if order.status == OrderStatus.paid:
        raise RequestValidationError(razorpay_order_id="Order already paid")

    if not db_payment:
        db_payment = PaymentLogs(
            order_id=order.id,
            razorpay_payment_id=razorpay_payment_id,
            status=PaymentStatus.created,
            amount=0,
        )
        session.add(db_payment)

    if not payment_details:
        payment_details = razorpay_client.payment.fetch(razorpay_payment_id)

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
        db_payment.amount = payment_details.get("amount", 0)
        db_payment.payment_method = payment_method
        db_payment.payment_details = payment_method_details
        order.status = OrderStatus.paid
        await session.commit()
        await session.refresh(order)
        await handle_post_payment(session, order)
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
        db_payment.amount = payment_details.get("amount", 0)
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


async def handle_razorpay_webhook(
    session: AsyncSession, data: dict, event_id: str, signature: str
):
    webhook_log = RazorpayWebhookLogs(
        event_id=event_id,
        entity=data.get("entity"),
        event=data.get("event"),
        signature=signature,
        payload=data.get("payload"),
    )
    session.add(webhook_log)
    await session.commit()

    webhook_event = await session.scalar(
        select(RazorpayWebhookLogs).where(RazorpayWebhookLogs.event_id == event_id)
    )
    if webhook_event:
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
