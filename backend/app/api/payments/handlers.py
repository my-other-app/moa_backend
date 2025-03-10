from datetime import datetime, timedelta
from sqlalchemy import exists, func, select
from app.core.validations.exceptions import RequestValidationError
from app.api.events.models import Events, EventRegistrationsLink
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus
from app.api.events.background_tasks import send_registration_confirmation_email


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
    session: AsyncSession, order: PaymentOrders
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
        print(order.id, event_registration.id)
        print(
            await session.scalar(
                select(PaymentLogs).where(
                    PaymentLogs.order_id == order.id,
                    PaymentLogs.status == PaymentStatus.captured,
                )
            )
        )
        total_paid = (
            await session.scalar(
                select(func.sum(PaymentLogs.amount)).where(
                    PaymentLogs.order_id == order.id,
                    PaymentLogs.status == PaymentStatus.captured,
                )
            )
            or 0
        )
        total_paid /= 100

        print("TOTAL:ACTUAL", total_paid, event_registration.actual_amount)

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
            event_endtime = (
                db_event.event_datetime + timedelta(hours=db_event.duration)
                if db_event.duration
                else None
            )
            email_payload = {
                "ticket_id": event_registration.ticket_id,
                "participant_name": event_registration.full_name,
                "event_name": db_event.name,
                "event_date": db_event.event_datetime.strftime("%d %b %Y"),
                "event_time": (
                    db_event.event_datetime.strftime("%I:%M %p")
                    + (" - " + event_endtime.strftime("%I:%M %p"))
                    if event_endtime
                    else ""
                ),
                "event_location": db_event.location_name,
                "event_prizes": f"Prizes worth ₹{db_event.prize_amount}",
                "organizer_name": db_event.club.name,
                "contact_email": db_event.contact_email,
                "contact_phone": db_event.contact_phone,
            }
            send_registration_confirmation_email(
                recipients=[event_registration.email],
                subject=f"Ticket: {db_event.name} - MyOtherAPP",
                payload=email_payload,
            )
        except Exception as e:
            print("Error sending email", e)
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


async def handle_post_payment(session: AsyncSession, order: PaymentOrders):
    """
    handles post payment operations
    """
    if order.source == "event_registration":
        return await handle_event_registration_payment(session, order)
    raise RequestValidationError(source="source is invalid")
