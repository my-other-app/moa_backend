from datetime import datetime, timedelta
import logging
from fastapi import BackgroundTasks
from sqlalchemy import exists, func, select
from app.core.validations.exceptions import RequestValidationError
from app.api.events.models import Events, EventRegistrationsLink
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

logger = logging.getLogger(__name__)


async def validate_event_registration_payload(session: AsyncSession, payload: dict):
    """
    validate payment payload of an event registration
    """
    try:
        errors = {}
        if "event_id" not in payload:
            errors["event_id"] = "event_id is required"
        if "event_registration_id" not in payload:
            errors["event_registration_id"] = "event_registration_id is required"

        if errors:
            raise RequestValidationError(**errors)

        event_exists = await session.scalar(
            select(exists().where(Events.id == payload["event_id"]))
        )
        if not event_exists:
            raise RequestValidationError(event_id="event_id is invalid")

        event_registration = await session.scalar(
            select(EventRegistrationsLink).where(
                EventRegistrationsLink.id == payload["event_registration_id"]
            )
        )

        if not event_registration:
            raise RequestValidationError(
                event_registration_id="event_registration_id is invalid"
            )

        if event_registration.is_paid:
            raise RequestValidationError(event_registration_id="already paid")

        amount_to_pay = (
            event_registration.actual_amount - event_registration.paid_amount
        )

        if not amount_to_pay:
            raise RequestValidationError(event_registration_id="nothing to pay")

        if not isinstance(amount_to_pay, float) and not isinstance(amount_to_pay, int):
            raise RequestValidationError(event_registration_id="invalid amount to pay")

        receipt = f"er_{event_registration.ticket_id}"
        event_registration.payment_receipt = receipt
        await session.commit()

        return (float(amount_to_pay), receipt)
    except Exception as e:
        raise e


async def handle_event_registration_payment(
    session: AsyncSession,
    order: PaymentOrders,
    background_tasks: BackgroundTasks | None = None,
):
    """
    handles payment for event registration
    """
    try:
        event_registration = await session.scalar(
            select(EventRegistrationsLink).where(
                EventRegistrationsLink.payment_receipt == order.receipt
            )
        )


        if not event_registration:
            raise RequestValidationError(receipt="receipt is invalid")
        
        # PaymentLogs.amount is already in rupees (Numeric column), matching
        # every other money column — no paise conversion needed here anymore.
        total_paid = (
            await session.scalar(
                select(func.sum(PaymentLogs.amount)).where(
                    PaymentLogs.order_id == order.id,
                    PaymentLogs.status == PaymentStatus.captured,
                )
            )
            or 0
        )

        event_registration.is_paid = total_paid >= event_registration.actual_amount
        event_registration.paid_amount = total_paid
        await session.commit()
        await session.refresh(event_registration)
        if not event_registration.is_paid:
            return True
        try:
            db_event = await session.scalar(
                select(Events)
                .filter(Events.id == event_registration.event_id)
                .options(joinedload(Events.club))
            )
            # The paid-for ticket goes out as the event's invite email,
            # through the same queue as Send tickets.
            from app.api.deliveries import service as deliveries

            await deliveries.send_ticket(session, db_event, event_registration, background_tasks)
        except Exception:
            logger.exception("Error sending the ticket email after payment")
        return True
    except Exception as e:
        raise e


"""

HANDLERS ENTRY POINT

"""


async def validate_payload(session: AsyncSession, source: str, payload: dict):
    """
    validates payment payload based on the source
    """
    if source == "event_registration":
        return await validate_event_registration_payload(session, payload)
    raise RequestValidationError(source="source is invalid")


async def handle_post_payment(
    session: AsyncSession,
    order: PaymentOrders,
    background_tasks: BackgroundTasks | None = None,
):
    """
    handles post payment operations
    """
    if order.source == "event_registration":
        return await handle_event_registration_payment(
            session, order, background_tasks=background_tasks
        )
    raise RequestValidationError(source="source is invalid")
