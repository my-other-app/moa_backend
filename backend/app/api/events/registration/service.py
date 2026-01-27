from datetime import timedelta
import logging
import uuid
from fastapi import BackgroundTasks
from pandas import DataFrame
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

logger = logging.getLogger(__name__)

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
    
    # Check if event is deleted
    if db_event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    
    # Check if registration deadline has passed
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if db_event.reg_enddate and db_event.reg_enddate < now:
        raise CustomHTTPException(400, message="Registration deadline has passed")
    
    # Check if registration hasn't started yet
    if db_event.reg_startdate and db_event.reg_startdate > now:
        raise CustomHTTPException(400, message="Registration has not started yet")

    # Check max participants (using FOR UPDATE lock to prevent race condition)
    if db_event.max_participants:
        registered_count = await session.scalar(
            select(func.count()).where(
                EventRegistrationsLink.event_id == db_event.id,
                EventRegistrationsLink.is_deleted == False,
                # For paid events, count only paid registrations
                # For free events, count all registrations
                EventRegistrationsLink.is_paid == True if db_event.has_fee else True,
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
        validated_additional_details = {}
        for field in db_additional_details:
            if field.key not in additional_details.keys():
                errors[field.key] = "This field is required"
                continue
            if field.field_type.value in ("select", "radio", "checkbox"):
                if additional_details[field.key] not in field.options:
                    errors[field.key] = "Invalid option selected"
            validated_additional_details[field.key] = additional_details[field.key]
        if errors:
            raise CustomHTTPException(400, message=errors)
        additional_details = validated_additional_details

    # Check for existing registration by user_id OR email
    registration = await session.scalar(
        select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == db_event.id,
            EventRegistrationsLink.is_deleted == False,
            # Check both user_id and email to prevent duplicates
            (EventRegistrationsLink.user_id == user_id) | (EventRegistrationsLink.email == email),
        )
    )
    if registration:
        if not db_event.has_fee or registration.is_paid:
            raise CustomHTTPException(400, message="Already registered for this event")

        # TODO: add service and api for updating registration info
        # if db_event.has_fee and registration.is_paid:
        #     raise CustomHTTPException(400, message="Already registered for this event")
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
                logger.exception("Error sending registration confirmation email")
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
    is_paid: bool | None = None,  # Changed to None to show all by default
    limit: int = 10,
    offset: int = 0,
    search: str | None = None,
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

    if is_attended is not None:
        query = query.where(EventRegistrationsLink.is_attended == is_attended)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            (EventRegistrationsLink.full_name.ilike(search_pattern)) |
            (EventRegistrationsLink.email.ilike(search_pattern)) |
            (EventRegistrationsLink.ticket_id.ilike(search_pattern))
        )

    # Get total count before pagination
    # Use a separate count query to avoid issues with joinedload in subqueries
    count_query = select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    
    if is_attended is not None:
        count_query = count_query.where(EventRegistrationsLink.is_attended == is_attended)

    if search:
        count_query = count_query.filter(
            (EventRegistrationsLink.full_name.ilike(search_pattern)) |
            (EventRegistrationsLink.email.ilike(search_pattern)) |
            (EventRegistrationsLink.ticket_id.ilike(search_pattern))
        )
        
    total = await session.scalar(count_query) or 0

    if limit is not None and offset is not None:
        query = query.limit(limit).offset(offset)

    scalar_result = await session.scalars(query)
    return list(scalar_result), total


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

    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )

    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            uuid_obj = uuid.UUID(registration_id)
            query = query.where(EventRegistrationsLink.id == uuid_obj)
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")

    scalar_result = await session.scalar(
        query
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


async def get_event_analytics(
    session: AsyncSession,
    user_id: int,
    event_id: int,
):
    # 1. Verify Event Access
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to view this event")

    # 2. Fetch all active registrations
    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    result = await session.scalars(query)
    registrations = result.all()

    total_registrations = len(registrations)
    
    # 3. Calculate Stats
    paid_count = sum(1 for r in registrations if r.is_paid)
    unpaid_count = total_registrations - paid_count
    
    attended_count = sum(1 for r in registrations if r.is_attended)
    absent_count = total_registrations - attended_count
    
    total_revenue = sum(r.actual_amount for r in registrations if r.is_paid) # Or use paid_amount?
    # Usually revenue is based on actual fee if paid, or paid_amount. Let's use paid_amount for accuracy.
    total_revenue = sum(r.paid_amount for r in registrations)

    page_views = event.page_views or 0
    conversion_rate = (total_registrations / page_views * 100) if page_views > 0 else 0.0
    attendance_rate = (attended_count / total_registrations * 100) if total_registrations > 0 else 0.0

    # 4. Institution Distribution
    institutions = {}
    for r in registrations:
        # Assuming institution is stored in additional_details or we need to fetch user profile?
        # The frontend uses `reg.institution || reg.profile`.
        # In `EventRegistrationsLink`, we don't have institution directly.
        # It might be in `additional_details` OR we need to join User profile.
        # For now, let's check `additional_details` for "College Name" or "Institution".
        inst = "Unknown"
        if r.additional_details and isinstance(r.additional_details, dict):
             inst = r.additional_details.get("College Name") or r.additional_details.get("Institution") or r.additional_details.get("college") or "Unknown"
        
        institutions[inst] = institutions.get(inst, 0) + 1

    top_institutions = [
        {"name": k, "value": v}
        for k, v in sorted(institutions.items(), key=lambda item: item[1], reverse=True)[:5]
    ]

    # 5. Registration Time Distribution
    # Morning: 6-12, Afternoon: 12-17, Evening: 17-21, Night: 21-6
    morning = 0
    afternoon = 0
    evening = 0
    night = 0

    from datetime import time

    for r in registrations:
        # created_at is UTC. We should probably convert to local time (IST) or just use UTC?
        # User is likely in IST (GMT+5:30).
        # Let's assume server time is UTC and convert to IST for analytics.
        created_at = r.created_at
        if not created_at:
            continue
            
        # Add 5 hours 30 minutes for IST
        local_time = created_at + timedelta(hours=5, minutes=30)
        t = local_time.time()

        if time(6, 0) <= t < time(12, 0):
            morning += 1
        elif time(12, 0) <= t < time(17, 0):
            afternoon += 1
        elif time(17, 0) <= t < time(21, 0):
            evening += 1
        else:
            night += 1

    # 6. Attendance Over Time (Recent Check-ins)
    # Group by hour? Or just list recent check-ins?
    # User asked for "how many when recent attended".
    # Let's group by hour for the last 24 hours or just all time if event is short.
    # Assuming event is 1-2 days.
    attendance_trend = {}
    for r in registrations:
        if r.is_attended and r.attended_on:
            # Convert to local time (IST)
            local_time = r.attended_on + timedelta(hours=5, minutes=30)
            # Group by hour: "10:00 AM", "11:00 AM"
            hour_key = local_time.strftime("%I:00 %p")
            # We need to sort by time, so maybe store as tuple (datetime, label) first?
            # Or just use a dict and sort keys later?
            # Better: Group by hour and sort by hour.
            
            # Use a sortable key like "YYYY-MM-DD HH"
            sort_key = local_time.strftime("%Y-%m-%d %H")
            label = local_time.strftime("%I %p") # "10 AM"
            
            if sort_key not in attendance_trend:
                attendance_trend[sort_key] = {"label": label, "count": 0, "sort": sort_key}
            attendance_trend[sort_key]["count"] += 1
            
    # Sort by time
    attendance_over_time = [
        {"time": v["label"], "count": v["count"]}
        for k, v in sorted(attendance_trend.items())
    ]

    return {
        "total_registrations": total_registrations,
        "total_revenue": total_revenue,
        "attendance_rate": round(attendance_rate, 1),
        "conversion_rate": round(conversion_rate, 1),
        "payment_status": {"paid": paid_count, "unpaid": unpaid_count},
        "attendance_status": {"attended": attended_count, "absent": absent_count},
        "top_institutions": top_institutions,
        "registration_time": {
            "morning": morning,
            "afternoon": afternoon,
            "evening": evening,
            "night": night,
        },
        "attendance_over_time": attendance_over_time,
    }


async def mark_attendance(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    is_attended: bool,
):
    # 1. Verify Event Access
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to manage this event")

    # 2. Fetch Registration
    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )

    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            uuid_obj = uuid.UUID(registration_id)
            query = query.where(EventRegistrationsLink.id == uuid_obj)
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")
    
    query = query.options(joinedload(EventRegistrationsLink.user))
    registration = await session.scalar(query)

    if not registration:
        raise CustomHTTPException(404, message="Registration not found")

    # 3. Update Attendance
    from datetime import datetime, timezone
    
    if is_attended:
        if not registration.is_attended:
            registration.is_attended = True
            registration.attended_on = datetime.now(timezone.utc)
    else:
        registration.is_attended = False
        registration.attended_on = None

    await session.commit()
    await session.refresh(registration)
    
    return registration
