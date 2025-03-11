from datetime import timedelta
from fastapi import BackgroundTasks
from pandas import DataFrame
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
import traceback

from app.api.events.registration.background_tasks import (
    send_registration_confirmation_email,
)
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.schemas import EventAdditionalDetail
from app.api.service import update_background_task_log
from app.api.users.models import UserTypes, Users
from app.core.utils.keys import generate_ticket_id
from app.core.validations.schema import validate_relations
from app.response import CustomHTTPException
from app.api.users import service as user_service
from app.api.models import BackgroundTaskLogs


async def register_event(
    session: AsyncSession,
    full_name: str,
    email: str,
    event_id: int | str,
    user_id: int,
    phone: str | None = None,
    background_tasks: BackgroundTasks | None = None,
    additional_details: dict | None = None,
    background_task_log_id: str | None = None,
):
    is_event_id = (isinstance(event_id, str) and event_id.isdigit()) or isinstance(
        event_id, int
    )

    await validate_relations(
        session,
        (
            {
                "event": (Events, int(event_id)),
                "user": (Users, user_id),
            }
            if is_event_id
            else {
                "slug": (Events, event_id, "slug"),
                "user": (Users, user_id),
            }
        ),
    )
    if is_event_id:
        event_id = int(event_id)
        db_event = await session.execute(
            select(Events).filter(Events.id == event_id).with_for_update()
        )
    else:
        db_event = await session.execute(
            select(Events).filter(Events.slug == event_id).with_for_update()
        )

    db_event = db_event.scalar()
    if not db_event:
        raise CustomHTTPException(404, message="Event not found")
    # event = db_event

    # db_event = Event.model_validate(db_event, from_attributes=True)
    if db_event.max_participants:
        registered_count = await session.scalar(
            select(func.count()).where(
                EventRegistrationsLink.event_id == db_event.id,
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.is_paid == db_event.has_fee,
            )
        )
        if registered_count >= db_event.max_participants:
            raise CustomHTTPException(400, message="Event is full")

    if db_event.additional_details:
        if not additional_details:
            raise CustomHTTPException(
                400, message="Additional details required for this event"
            )
        errors = {}
        db_additional_details = [
            EventAdditionalDetail.model_validate(additional)
            for additional in db_event.additional_details
        ]
        for field in db_additional_details:
            if field.key not in additional_details.keys():
                errors[field.key] = "This field is required"
                continue
            if field.field_type.value in ("select", "radio", "checkbox"):
                if additional_details[field.key] not in field.options:
                    errors[field.key] = "Invalid option selected"
        if errors:
            raise CustomHTTPException(400, message=errors)

    registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == db_event.id,
            EventRegistrationsLink.email == email,
            EventRegistrationsLink.is_deleted == False,
        )
    )
    if registration:
        raise CustomHTTPException(400, message="Already registered for this event")
        # TODO: add service and api for updating registration info
        if db_event.has_fee and registration.is_paid:
            raise CustomHTTPException(400, message="Already registered for this event")
    else:
        ticket_id = generate_ticket_id()

        registration = EventRegistrationsLink(
            event_id=db_event.id,
            user_id=user_id,
            ticket_id=ticket_id,
            actual_amount=db_event.reg_fee,
            paid_amount=0,
            additional_details=additional_details,
            full_name=full_name,
            email=email,
            phone=phone,
        )
        session.add(registration)

    event_endtime = (
        db_event.event_datetime + timedelta(hours=db_event.duration)
        if db_event.duration
        else None
    )
    await session.commit()
    await session.refresh(registration)
    db_event = await session.scalar(
        select(Events)
        .filter(Events.id == registration.event_id)
        .options(joinedload(Events.club))
    )
    email_payload = {
        "ticket_id": registration.ticket_id,
        "participant_name": registration.full_name,
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
    if not db_event.has_fee:
        try:
            if background_tasks:
                background_tasks.add_task(
                    send_registration_confirmation_email,
                    recipients=[email],
                    subject=f"Ticket: {db_event.name} - MyOtherAPP",
                    payload=email_payload,
                )
            else:
                send_registration_confirmation_email(
                    recipients=[email],
                    subject=f"Ticket: {db_event.name} - MyOtherAPP",
                    payload=email_payload,
                )
        except Exception as e:
            print(e)
            traceback.print_exc()
            if background_task_log_id:
                await update_background_task_log(
                    session,
                    background_task_log_id,
                    new_logs=[
                        {
                            "level": "ERROR",
                            "message": f"Failed to send email to {email}",
                            "metadata": {"error": str(e)},
                        }
                    ],
                )

    data = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == db_event.id,
            EventRegistrationsLink.email == email,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            selectinload(EventRegistrationsLink.event),
            selectinload(EventRegistrationsLink.user),
        )
    )
    payment_remining = data.actual_amount - data.paid_amount
    return {
        "event_name": db_event.name,
        "event_id": db_event.id,
        "event_registration_id": data.id,
        "pay_amount": payment_remining,
        "event_datetime": db_event.event_datetime,
        "ticket_id": data.ticket_id,
        "full_name": data.full_name,
        "email": data.email,
        "phone": data.phone,
        "additional_details": data.additional_details,
    }


async def list_event_registrations(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    is_attended: bool | None = None,
    is_paid: bool = True,
    limit: int = 10,
    offset: int = 0,
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to view this event")
    query = (
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRegistrationsLink.user),
            joinedload(EventRegistrationsLink.volunteer),
        )
    )

    if limit is not None and offset is not None:
        query = query.limit(limit).offset(offset)

    if event.has_fee and event.reg_fee > 0:
        query = query.where(EventRegistrationsLink.is_paid == is_paid)

    if is_attended is not None:
        query = query.where(EventRegistrationsLink.is_attended == is_attended)

    scalar_result = await session.scalars(query)
    return list(scalar_result)


async def get_registration(
    session: AsyncSession, user_id: int, event_id: int, registration_id: str
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to view this event")

    scalar_result = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.id == registration_id,
        )
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            ),
            joinedload(EventRegistrationsLink.user),
        )
    )
    if not scalar_result:
        raise CustomHTTPException(404, "Registration not found")
    return scalar_result


async def bulk_import_event_registrations(
    session: AsyncSession,
    event_id: Events,
    df: DataFrame,
    background_log: BackgroundTaskLogs,
):
    try:
        # Use select to properly load the event with joined club
        event_query = (
            select(Events)
            .filter(Events.id == event_id)
            .options(joinedload(Events.club))
        )
        event = await session.scalar(event_query)

        if not event:
            await update_background_task_log(
                session,
                background_log.id,
                new_logs=[
                    {
                        "level": "ERROR",
                        "message": f"Event with ID {event.id} not found",
                    }
                ],
                new_status="FAILED",
            )
            return

        # Prepare registration logs
        registration_logs = []

        for _, row in df.iterrows():
            try:
                full_name = row["full_name"]
                email = row["email"]
                phone = str(row.get("phone", "")) if row.get("phone") else None

                # Process additional details
                additional_details = {}
                additional_details_fields = row.get("additional_details_fields", "")
                if additional_details_fields:
                    for key in str(additional_details_fields).split(","):
                        if key in row:
                            additional_details[key] = row[key]

                # Find or create user
                user_query = (
                    select(Users)
                    .filter(
                        Users.email == email,
                        Users.is_deleted == False,
                        Users.user_type != UserTypes.club,
                    )
                    .order_by(Users.created_at.desc())
                    .limit(1)
                )
                user = await session.scalar(user_query)

                if not user:
                    # Create new user if not exists
                    user = await user_service.create_user(
                        session=session,
                        full_name=full_name,
                        email=email,
                        phone=phone,
                        provider="email",
                        user_type=UserTypes.guest,
                    )

                if not user:
                    registration_logs.append(
                        {
                            "level": "ERROR",
                            "message": f"Failed to create or find user for '{full_name}' <{email}>",
                        }
                    )
                    continue

                # Register for event
                await register_event(
                    session=session,
                    full_name=full_name,
                    email=email,
                    phone=phone,
                    user_id=user.id,
                    event_id=event.id,
                    additional_details=additional_details,
                    background_task_log_id=background_log.id,
                )

                registration_logs.append(
                    {
                        "level": "INFO",
                        "message": f"Registered '{full_name}' <{email}> for '{event.name}'",
                    }
                )

            except Exception as row_error:
                registration_logs.append(
                    {
                        "level": "ERROR",
                        "message": f"Failed to process row for '{full_name}' <{email}>",
                        "metadata": {"error": str(row_error)},
                    }
                )

        # Update background log with all collected logs
        if registration_logs:
            await update_background_task_log(
                session,
                background_log.id,
                new_logs=registration_logs,
                new_status=(
                    "COMPLETED"
                    if all(log["level"] == "INFO" for log in registration_logs)
                    else "PARTIAL"
                ),
            )

        # Commit the session
        await session.commit()

    except Exception as global_error:
        # Handle any global unexpected errors
        await update_background_task_log(
            session,
            background_log.id,
            new_logs=[
                {
                    "level": "CRITICAL",
                    "message": "Bulk import failed",
                    "metadata": {"error": str(global_error)},
                }
            ],
            new_status="FAILED",
        )

        # Re-raise the error for upper-level handling
        # raise
