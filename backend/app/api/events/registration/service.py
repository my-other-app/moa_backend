from datetime import timedelta
import logging
import uuid
from fastapi import BackgroundTasks
from pandas import DataFrame
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

logger = logging.getLogger(__name__)

from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.schemas import EventAdditionalDetail
from app.api.service import update_background_task_log
from app.api.users.models import UserProfiles, UserTypes, Users
from app.core.utils.keys import generate_ticket_id
from app.core.validations.schema import validate_relations
from app.response import CustomHTTPException
from app.api.events.access import ensure_event_access
from app.api.templates_audience import RegistrationRole
from app.api.users import service as user_service
from app.api.models import BackgroundTaskLogs


def _field_type(field) -> str:
    """The field's type as a plain string.

    CustomBaseModel sets use_enum_values=True, so a FieldTypes annotation
    validates to the string rather than the enum member. Reading
    `.field_type.value` therefore raised AttributeError on every registration
    for an event with custom fields -- a 500, with the app only able to report
    that something went wrong. Accepting either form keeps this working if
    that config is ever changed back.
    """
    field_type = field.field_type
    return getattr(field_type, "value", field_type)


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
    role: RegistrationRole | None = None,
    notify: bool = True,
):
    """`notify=False` is for a guest the organiser adds or imports: the
    confirmation goes out later through Send tickets, where it is recorded,
    rather than from here where it is not."""
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
            value = additional_details.get(field.key)
            missing = value is None or (isinstance(value, str) and not value.strip())

            if missing:
                # Only a required field is an error when absent. This used to
                # reject any missing key without consulting field.required, so
                # `required: False` meant nothing and every custom field was
                # mandatory -- a client that correctly omits a blank optional
                # field had its registration refused.
                if field.required:
                    errors[field.key] = "This field is required"
                continue

            if _field_type(field) in ("select", "radio", "checkbox"):
                if value not in (field.options or []):
                    errors[field.key] = "Invalid option selected"
            validated_additional_details[field.key] = value
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
            # Omitted means attendee, which is the column default -- passing
            # None explicitly would violate the not-null constraint.
            **({"role": role} if role is not None else {}),
        )
        session.add(registration)

        await session.commit()
        await session.refresh(registration)
        db_event = await session.scalar(
            select(Events)
            .filter(Events.id == registration.event_id)
            .options(joinedload(Events.club))
        )
        if not db_event.has_fee and notify:
            # The ticket goes out as the event's invite email, through the
            # same queue as Send tickets, so the attendee list shows it land.
            try:
                from app.api.deliveries import service as deliveries

                await deliveries.send_ticket(session, db_event, registration, background_tasks)
            except Exception as e:
                logger.exception("Error sending the ticket email")
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

    # Check for new user badges after registration
    try:
        from app.api.badges.service import check_and_award_user_badges
        await check_and_award_user_badges(session, user_id=data.user_id)
    except Exception as e:
        logging.getLogger(__name__).error(f"Badge check failed after registration: {e}")

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
    as_admin: bool = False,
    role: RegistrationRole | None = None,
    delivery: str | None = None,
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    # The filters are built once and applied to both the page and the count,
    # so the two can never disagree.
    conditions = []
    if is_attended is not None:
        conditions.append(EventRegistrationsLink.is_attended == is_attended)
    if role is not None:
        conditions.append(EventRegistrationsLink.role == role)
    if search:
        search_pattern = f"%{search}%"
        conditions.append(
            (EventRegistrationsLink.full_name.ilike(search_pattern))
            | (EventRegistrationsLink.email.ilike(search_pattern))
            | (EventRegistrationsLink.phone.ilike(search_pattern))
            | (EventRegistrationsLink.ticket_id.ilike(search_pattern))
        )
    if delivery == "not_delivered":
        # Nothing sent, nothing that worked, or something that came back. A
        # guest reached on any channel is not here.
        from app.api.deliveries.models import Deliveries, DeliveryKind, DeliveryOutcome, DeliveryStatus

        reached = select(Deliveries.registration_id).where(
            Deliveries.event_id == event_id,
            Deliveries.kind == DeliveryKind.ticket,
            Deliveries.status == DeliveryStatus.sent,
            (Deliveries.outcome.is_(None)) | (Deliveries.outcome.in_((DeliveryOutcome.delivered, DeliveryOutcome.read))),
        )
        conditions.append(EventRegistrationsLink.id.not_in(reached))

    query = (
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRegistrationsLink.user).options(
                joinedload(Users.profile).options(
                    joinedload(UserProfiles.org),
                    joinedload(UserProfiles.avatar)
                )
            ),
            joinedload(EventRegistrationsLink.volunteer),
        )
    )

    for condition in conditions:
        query = query.where(condition)
    # By name, as a list somebody reads from top to bottom.
    query = query.order_by(EventRegistrationsLink.full_name, EventRegistrationsLink.created_at)

    # Get total count before pagination
    # Use a separate count query to avoid issues with joinedload in subqueries
    count_query = select(func.count()).select_from(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    for condition in conditions:
        count_query = count_query.where(condition)

    total = await session.scalar(count_query) or 0

    if limit is not None and offset is not None:
        query = query.limit(limit).offset(offset)

    scalar_result = await session.scalars(query)
    registrations = list(scalar_result)
    # What was sent to each of them, so the list can say who was reached.
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind

    sent = await deliveries.by_registration(
        session, event_id, DeliveryKind.ticket, [r.id for r in registrations]
    )
    
    # Manually serialize to avoid Pydantic validation errors with nested SQLAlchemy models
    serialized_results = []
    for reg in registrations:
        try:
            # Serialize user profile
            user_data = None
            if reg.user:
                profile_data = None
                if reg.user.profile:
                    org_data = None
                    if reg.user.profile.org:
                        org_data = {
                            "id": reg.user.profile.org.id,
                            "name": reg.user.profile.org.name,
                            "type": reg.user.profile.org.type.value if hasattr(reg.user.profile.org.type, 'value') else str(reg.user.profile.org.type),
                            "logo": reg.user.profile.org.logo,
                        }
                    avatar_data = None
                    if reg.user.profile.avatar:
                        avatar_data = {
                            "id": reg.user.profile.avatar.id,
                            "name": reg.user.profile.avatar.name,
                            "image": reg.user.profile.avatar.image,
                        }
                    profile_data = {
                        "org": org_data,
                        "avatar": avatar_data,
                        "profile_pic": reg.user.profile.profile_pic,
                    }
                user_data = {
                    "id": reg.user.id,
                    "full_name": reg.user.full_name or "",
                    "email": reg.user.email or "",
                    "phone": reg.user.phone,
                    "username": reg.user.username or reg.user.email or "",  # Fallback to email if no username
                    "user_type": reg.user.user_type.value if hasattr(reg.user.user_type, 'value') else str(reg.user.user_type),
                    "profile": profile_data,
                }
            
            # Serialize volunteer - skip if soft-deleted
            volunteer_data = None
            if reg.volunteer and not getattr(reg.volunteer, 'is_deleted', False):
                volunteer_data = {
                    "email": reg.volunteer.email or "",
                    "is_approved": reg.volunteer.is_approved,
                    "user_id": reg.volunteer.user_id,
                    "profile_pic": None,
                    "full_name": None,
                }
            
            # Get institution from profile.org
            institution = None
            if reg.user and reg.user.profile and reg.user.profile.org:
                institution = reg.user.profile.org.name
            
            serialized_results.append({
                "id": reg.id,
                "ticket_id": reg.ticket_id or "",
                "event_id": reg.event_id,
                "is_paid": reg.is_paid,
                "full_name": reg.full_name or "",
                "email": reg.email or "",
                "phone": reg.phone,
                "actual_amount": float(reg.actual_amount or 0),
                "paid_amount": float(reg.paid_amount or 0),
                "is_attended": reg.is_attended,
                "is_won": reg.is_won,
                # The placing itself, not just whether they placed -- the
                # dashboard shows 1st/2nd/3rd and the badge metrics key on it.
                "position": reg.position,
                # Which ticket and certificate variant this person is issued.
                # This dict is built by hand, so a column left out of it is
                # silently replaced by the response schema's default -- which
                # reported every speaker as an attendee.
                "role": getattr(reg.role, "value", reg.role),
                "user": user_data,
                "volunteer": volunteer_data,
                "institution": institution,
                "name": reg.full_name or "",
                # Answers to the event's custom questions. Previously omitted,
                # which silently dropped them from the registrations export --
                # the export code spreads this key and had been producing
                # nothing for it.
                "additional_details": reg.additional_details,
                "created_at": reg.created_at,
                "ticket_revoked_at": reg.ticket_revoked_at,
                "ticket_deliveries": sent.get(reg.id, []),
            })
        except Exception as e:
            logger.error(f"Error serializing registration {reg.id}: {e}")
            # Continue with other registrations instead of failing entirely
            continue
    
    return serialized_results, total


async def get_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    as_admin: bool = False,
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

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
    notify: bool = True,
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

        for index, row in df.iterrows():
            full_name = row.get("full_name")
            email = row.get("email")
            # A blank cell arrives as NaN, not "". Said plainly, before any
            # query runs: a bad value reaching the database aborts the
            # transaction and takes every later row down with it.
            if not isinstance(email, str) or not email.strip() or not isinstance(full_name, str) or not full_name.strip():
                registration_logs.append(
                    {"level": "ERROR", "message": f"Row {int(index) + 2}: a name and an email are both required"}
                )
                continue
            email = email.strip()
            full_name = full_name.strip()
            try:
                phone_cell = row.get("phone")
                phone = str(phone_cell).strip() if isinstance(phone_cell, str) and phone_cell.strip() else None
                role = _role_from_sheet(row.get("role"))

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
                    role=role,
                    notify=notify,
                )

                registration_logs.append(
                    {
                        "level": "INFO",
                        "message": f"Registered '{full_name}' <{email}> for '{event.name}'",
                    }
                )

            except Exception as row_error:
                # The failed statement left the transaction aborted; roll it
                # back so the next row, and the log itself, can be written.
                # Rows already registered were committed and stay.
                await session.rollback()
                registration_logs.append(
                    {
                        "level": "ERROR",
                        "message": f"Row {int(index) + 2}: '{full_name}' <{email}> could not be registered — {str(row_error)[:200]}",
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
    as_admin: bool = False,
):
    # 1. Verify Event Access
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

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

    # How many distinct institutions actually turned up, as opposed to the five
    # largest. The dashboard shows this as a headline count, and deriving it
    # from len(top_institutions) would silently cap at 5. "Unknown" is the
    # bucket for registrations that answered no college question, so it is not
    # an institution and is not counted.
    total_institutions = len([k for k in institutions if k != "Unknown"])

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
        "total_institutions": total_institutions,
        "registration_time": {
            "morning": morning,
            "afternoon": afternoon,
            "evening": evening,
            "night": night,
        },
        "attendance_over_time": attendance_over_time,
    }


async def _load_registration_for_event(
    session: AsyncSession, event_id: int, registration_id: str
):
    """
    Resolve a registration by uuid or by ticket id.

    mark_attendance accepts either form -- organisers paste a scanned ticket
    id as readily as a uuid -- so the winner endpoints accept both too rather
    than being the one place that insists on a uuid.
    """
    query = select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )
    if registration_id.startswith("MOA"):
        query = query.where(EventRegistrationsLink.ticket_id == registration_id)
    else:
        try:
            query = query.where(
                EventRegistrationsLink.id == uuid.UUID(registration_id)
            )
        except ValueError:
            raise CustomHTTPException(404, "Registration not found")

    registration = await session.scalar(query.options(
        joinedload(EventRegistrationsLink.user)
    ))
    if not registration:
        raise CustomHTTPException(404, message="Registration not found")
    return registration


async def set_winner(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    position: int | None,
    as_admin: bool = False,
):
    """
    Record or clear a placing for one registration.

    `is_won` and `position` have been columns on every registration since the
    table was written, and the badge checker reads `position` for three of its
    metrics -- events_won (1st), runner_up_count (2nd) and podium_count (3rd).
    Nothing in the API ever wrote either column, so those three badges could
    never be earned by anyone. This is what writes them.

    The two columns are kept consistent: `is_won` means "placed at all" and is
    true exactly when a position is set. Passing None clears both.

    Positions are not required to be unique within an event. Shared places are
    real -- joint winners, two thirds -- and rejecting them would be a
    surprise with no upside; the dashboard shows the placings as given.
    """
    if position is not None and position < 1:
        raise CustomHTTPException(400, message="Position must be 1 or higher")

    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

    registration = await _load_registration_for_event(
        session, event_id, registration_id
    )

    registration.position = position
    registration.is_won = position is not None

    await session.commit()
    # No refresh: sessions are created with expire_on_commit=False, so the
    # instance keeps both the values just written and the eagerly-loaded
    # `user` the response schema requires. Refreshing would reload the columns
    # and drop the relationship, which the response then rejects.

    # The three placing badges can only be earned once a position exists, so
    # re-check here. Mirrors mark_attendance: a badge failure must not fail
    # the write the organiser actually asked for.
    if registration.user_id:
        try:
            from app.api.badges.service import check_and_award_user_badges

            await check_and_award_user_badges(session, user_id=registration.user_id)
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Badge check failed after placing: {e}")

    return registration


async def list_winners(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    as_admin: bool = False,
):
    """Everyone who placed, best placing first."""
    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(event, user_id, as_admin)

    rows = await session.scalars(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.is_won == True,
        )
        .options(joinedload(EventRegistrationsLink.user))
        .order_by(
            EventRegistrationsLink.position.asc().nulls_last(),
            EventRegistrationsLink.full_name.asc(),
        )
    )
    return list(rows)


async def mark_attendance(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    is_attended: bool,
    as_admin: bool = False,
):
    # 1. Verify Event Access
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

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

    if is_attended and registration.ticket_revoked_at is not None:
        raise CustomHTTPException(
            409, message="This ticket was revoked. Reinstate it before checking them in."
        )
    if is_attended:
        if not registration.is_attended:
            registration.is_attended = True
            registration.attended_on = datetime.now(timezone.utc)
    else:
        registration.is_attended = False
        registration.attended_on = None

    await session.commit()
    # Named attributes, not a bare refresh: a bare refresh expires the joined
    # `user` too and does not load it back, and the response schema requires
    # it -- so a check-in on a registration already in the session answered
    # 500 after the write had been made.
    await session.refresh(registration, attribute_names=["is_attended", "attended_on", "updated_at", "user"])

    # Check for new badges after attendance change
    if is_attended and registration.user_id:
        try:
            from app.api.badges.service import check_and_award_user_badges, check_and_award_club_badges
            await check_and_award_user_badges(session, user_id=registration.user_id)
            await check_and_award_club_badges(session, club_id=event.club_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Badge check failed after attendance: {e}")

    return registration


async def set_role(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    role: RegistrationRole | None,
    as_admin: bool = False,
):
    """Record which role one registration is attending as.

    This is what makes the role-specific ticket and certificate designs
    reachable. An event can hold a separate design for speakers or staff,
    but the renderer picks one by the registration's role -- so without a
    way to set it, every variant an organiser designed would sit unused
    while everyone received the event default.

    Passing None resets the registration to `attendee`, which is what every
    registration starts as: the column is not nullable, because "attending
    as nobody" is not a state an event has. An attendee still receives the
    event's default design unless the club has drawn an attendee variant,
    since resolution falls through from the role to the event default.
    """
    if role is None:
        role = RegistrationRole.attendee
    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    ensure_event_access(
        event, user_id, as_admin, message="Not authorized to manage this event"
    )

    registration = await _load_registration_for_event(
        session, event_id, registration_id
    )

    registration.role = role
    await session.commit()
    # No refresh, for the same reason as set_winner: expire_on_commit is off,
    # so the instance keeps the eagerly-loaded `user` the response needs.

    return registration


def _role_from_sheet(value) -> RegistrationRole | None:
    """Read a `role` cell from a bulk-import sheet.

    Optional, so a sheet written before this column existed imports exactly
    as it did. Deliberately forgiving about case and stray spaces, because
    the alternative is failing somebody's whole row over "Speaker " -- and
    an unreadable value falls back to attendee rather than rejecting the
    registration, since the point of the import is to get people in.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"nan", "none"}:
        return None
    try:
        return RegistrationRole(text)
    except ValueError:
        logger.warning("Unknown role %r in bulk import, importing as attendee", value)
        return None


# ---------------------------------------------------------------------------
# The organiser's own changes to the list: a correction, a removal, a guest
# added by hand, and the record of what was imported. Ported from Muster's
# attendees module onto registrations.
# ---------------------------------------------------------------------------


async def update_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    *,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    role: RegistrationRole | None = None,
    additional_details: dict | None = None,
    as_admin: bool = False,
):
    """A correction: the registration's own name, contact and answers. The
    linked user account is left alone -- it is theirs, not the event's."""
    registration = await get_registration(session, user_id, event_id, registration_id, as_admin=as_admin)
    if full_name is not None:
        registration.full_name = full_name.strip()
    if email is not None:
        taken = await session.scalar(
            select(EventRegistrationsLink).where(
                EventRegistrationsLink.event_id == event_id,
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.email == email,
                EventRegistrationsLink.id != registration.id,
            )
        )
        if taken is not None:
            raise CustomHTTPException(409, message="Another registration on this event already uses that email.")
        registration.email = email
    if phone is not None:
        registration.phone = phone.strip() or None
    if role is not None:
        registration.role = role
    if additional_details is not None:
        registration.additional_details = {**(registration.additional_details or {}), **additional_details}
    await session.commit()
    await session.refresh(registration, attribute_names=["full_name", "email", "phone", "role", "additional_details", "updated_at", "user"])
    return registration


async def remove_registration(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    registration_id: str,
    *,
    confirm_slug: str,
    as_admin: bool = False,
):
    """Takes them off the list: the record, the ticket and the check-in with
    it. Soft deleted like everything else, but it is the answer to "take me
    off the list", so it asks for the event's slug to be typed."""
    registration = await get_registration(session, user_id, event_id, registration_id, as_admin=as_admin)
    if (confirm_slug or "").strip() != registration.event.slug:
        raise CustomHTTPException(409, message="Type the event's slug exactly to confirm.", errors={"fields": {"confirm_slug": registration.event.slug}})
    registration.soft_delete()
    await session.commit()
    return registration


async def add_attendee(
    session: AsyncSession,
    user_id: int,
    event_id: int,
    *,
    full_name: str,
    email: str,
    phone: str | None,
    role: RegistrationRole,
    additional_details: dict | None,
    as_admin: bool = False,
):
    """A guest the organiser adds by hand. Finds or makes the guest account
    the way the public form does, registers them, and sends nothing: the
    ticket goes out through Send tickets, where it is recorded."""
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user_id, as_admin, message="Not authorized to manage this event")

    user = await user_service.get_non_club_user_by_email(session, email)
    if user is None:
        user = await user_service.create_user(
            session=session, full_name=full_name, email=email, phone=phone, provider="email", user_type=UserTypes.guest
        )
    if user is None:
        raise CustomHTTPException(400, message="The guest account could not be created")
    data = await register_event(
        session=session, full_name=full_name, email=email, phone=phone, user_id=user.id, event_id=event_id,
        additional_details=additional_details, role=role, notify=False,
    )
    return await get_registration(session, user_id, event_id, str(data["event_registration_id"]), as_admin=as_admin)


def summarise_import(log) -> dict:
    """What an import did, in the words the panel shows."""
    entries = log.logs or []
    imported = sum(1 for e in entries if e.get("level") == "INFO")
    failed = sum(1 for e in entries if e.get("level") in ("ERROR", "CRITICAL"))
    issues = [e.get("message", "") for e in entries if e.get("level") in ("ERROR", "CRITICAL")][:50]
    return {
        "id": log.id, "task_name": log.task_name, "status": log.status, "created_at": log.created_at,
        "imported": imported, "failed": failed, "issues": issues,
    }


async def list_imports(session: AsyncSession, user_id: int, event_id: int, as_admin: bool = False) -> list[dict]:
    from app.api.models import BackgroundTaskLogs

    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user_id, as_admin)
    logs = await session.scalars(
        select(BackgroundTaskLogs)
        .where(BackgroundTaskLogs.event_id == event_id, BackgroundTaskLogs.task_type == "event_registrations_bulk_import")
        .order_by(BackgroundTaskLogs.created_at.desc())
        .limit(20)
    )
    return [summarise_import(log) for log in logs]
