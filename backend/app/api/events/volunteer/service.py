from datetime import datetime, timezone
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.events.volunteer.models import Volunteer
from app.api.users.models import UserProfiles, Users
from app.response import CustomHTTPException
from app.api.events.models import EventRegistrationsLink, Events
from app.api.clubs.models import Clubs
from app.core.notifications.triggers import notify_user_check_in, notify_user_added_as_volunteer


async def add_volunteer(
    session: AsyncSession, email_id: str, event_id: int, club_id: int
) -> None:
    """Add volunteers to an event."""
    volunteer_exists = await session.scalar(
        select(
            exists(Volunteer).where(
                Volunteer.email == email_id, Volunteer.event_id == event_id
            )
        )
    )
    if volunteer_exists:
        raise CustomHTTPException(
            status_code=400, message="Volunteer already exists for this event."
        )
    user_id = await session.scalar(
        select(Users.id).where(
            Users.email == email_id,
            or_(Users.user_type == "app_user", Users.user_type == "admin"),
        )
    )
    volunteer = Volunteer(
        email=email_id,
        event_id=event_id,
        user_id=user_id,
        club_id=club_id,
        is_approved=True,  # TODO: Change this to manual approval by the requested user.
    )

    session.add(volunteer)
    await session.commit()
    
    # Trigger 5: Send notification to user about being added as volunteer
    if user_id:
        try:
            # Get event and club details for notification
            event = await session.scalar(
                select(Events).where(Events.id == event_id).options(joinedload(Events.club))
            )
            if event and event.club:
                await notify_user_added_as_volunteer(
                    session=session,
                    user_id=user_id,
                    event_id=event_id,
                    event_name=event.name,
                    club_name=event.club.name,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send volunteer notification: {e}")
    
    return None


async def remove_event_volunteer(
    session: AsyncSession, email_id: str, event_id: int
) -> None:
    """Remove volunteers from an event."""
    volunteer = select(Volunteer).where(
        Volunteer.email == email_id, Volunteer.event_id == event_id
    )
    volunteer = await session.execute(volunteer)
    volunteer = volunteer.scalars().first()
    if not volunteer:
        raise CustomHTTPException(
            status_code=404, message="Volunteer does not exist for this event."
        )
    volunteer.soft_delete()
    await session.commit()
    return None


async def remove_club_volunteer(
    session: AsyncSession, email_id: str, club_id: int
) -> None:
    """Remove volunteers from a club."""
    volunteer = select(Volunteer).where(
        Volunteer.email == email_id, Volunteer.club_id == club_id
    )
    volunteer = await session.execute(volunteer)
    volunteer = volunteer.scalars().first()
    if not volunteer:
        raise CustomHTTPException(
            status_code=404, message="Volunteer does not exist for this club."
        )
    volunteer.soft_delete()
    await session.commit()
    return None


async def list_volunteers(
    session: AsyncSession, event_id: int, include_club_volunteers=True
) -> list[dict]:
    """List all volunteers for an event."""
    if include_club_volunteers:
        result = (
            select(Volunteer, UserProfiles)
            .outerjoin(Clubs, Clubs.id == Volunteer.club_id)
            .outerjoin(Events, Events.club_id == Clubs.id)
            .where(
                or_(
                    Volunteer.event_id == event_id,
                    and_(Events.club_id == Volunteer.club_id, Events.id == event_id),
                )
            )
            .outerjoin(UserProfiles, Volunteer.user_id == UserProfiles.user_id)
            .distinct()
        )
    else:
        result = (
            select(Volunteer, UserProfiles)
            .where(Volunteer.event_id == event_id)
            .outerjoin(UserProfiles, Volunteer.user_id == UserProfiles.user_id)
        )
    result = await session.execute(result)
    return [
        jsonable_encoder(volunteer)
        | (
            {"full_name": profile.full_name, "profile_pic": profile.profile_pic}
            if profile
            else {}
        )
        for volunteer, profile in result
    ]


async def checkin_user(
    session: AsyncSession, event_id: int, ticker_id: str, volunteer_id: int
) -> None:
    """Check-in a participant for an event."""

    # First, check if the ticket exists for this specific event
    registration = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.ticket_id == ticker_id,
        )
        .options(joinedload(EventRegistrationsLink.event))
    )
    
    if not registration:
        # Check if the ticket exists for ANY event (wrong event case)
        any_registration = await session.scalar(
            select(EventRegistrationsLink)
            .where(EventRegistrationsLink.ticket_id == ticker_id)
            .options(joinedload(EventRegistrationsLink.event))
        )
        
        if any_registration:
            # Ticket exists but for a different event
            event_name = any_registration.event.name if any_registration.event else "another event"
            raise CustomHTTPException(
                400, 
                f"Incorrect event: This ticket belongs to '{event_name}'"
            )
        else:
            # Ticket doesn't exist at all
            raise CustomHTTPException(400, "Registration not found")
    
    if registration.is_attended:
        raise CustomHTTPException(400, "User already checked-in")
    if registration.event.has_fee and not registration.is_paid:
        raise CustomHTTPException(
            400, "User has not paid the fee, Please verify the payment details."
        )
    registration.is_attended = True
    registration.attended_on = datetime.now(timezone.utc)
    registration.volunteer_id = volunteer_id
    await session.commit()
    
    # Trigger 3: Send check-in confirmation notification
    try:
        await notify_user_check_in(
            session=session,
            user_id=registration.user_id,
            event_id=event_id,
            event_name=registration.event.name,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send check-in notification: {e}")
    
    return True


async def validate_ticket(
    session: AsyncSession, event_id: int, ticket_id: str
) -> dict:
    """
    Validate a ticket without marking attendance.
    Returns ticket details if valid, raises exception if invalid.
    """
    from app.api.users.models import UserProfiles
    
    # First, check if the ticket exists for this specific event
    registration = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.ticket_id == ticket_id,
        )
        .options(joinedload(EventRegistrationsLink.event))
    )
    
    if not registration:
        # Check if the ticket exists for ANY event (wrong event case)
        any_registration = await session.scalar(
            select(EventRegistrationsLink)
            .where(EventRegistrationsLink.ticket_id == ticket_id)
            .options(joinedload(EventRegistrationsLink.event))
        )
        
        if any_registration:
            # Ticket exists but for a different event
            event_name = any_registration.event.name if any_registration.event else "another event"
            raise CustomHTTPException(
                400, 
                f"Incorrect event: This ticket belongs to '{event_name}'"
            )
        else:
            # Ticket doesn't exist at all
            raise CustomHTTPException(400, "Registration not found")
    
    if registration.is_attended:
        raise CustomHTTPException(400, "User already checked-in")
    
    if registration.event.has_fee and not registration.is_paid:
        raise CustomHTTPException(
            400, "User has not paid the fee, Please verify the payment details."
        )
    
    # Get user profile for ticket holder name
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == registration.user_id)
    )
    
    # Return ticket details without marking as attended
    return {
        "valid": True,
        "ticket_id": ticket_id,
        "ticket_holder_name": profile.full_name if profile else "Unknown",
        "event_name": registration.event.name,
        "event_datetime": registration.event.event_datetime.isoformat() if registration.event.event_datetime else None,
        "location": registration.event.location,
        "is_paid": registration.is_paid,
        "has_fee": registration.event.has_fee,
    }


async def is_volunteer(session: AsyncSession, user_id: int, event_id: int):
    """Check if a user is a volunteer for an event."""

    return await session.scalar(
        select(Volunteer)
        .outerjoin(Clubs, Clubs.id == Volunteer.club_id)
        .outerjoin(Events, Events.club_id == Clubs.id)
        .where(
            or_(
                and_(Volunteer.user_id == user_id, Volunteer.event_id == event_id),
                and_(Events.club_id == Volunteer.club_id, Events.id == event_id),
            )
        )
    )


async def get_volunteer_events(session: AsyncSession, user_id: int):
    query = (
        select(Events)
        .join(Volunteer, Volunteer.event_id == Events.id)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Volunteer.user_id == user_id,
            or_(Events.id == Volunteer.event_id, Clubs.id == Volunteer.club_id),
        )
        .options(joinedload(Events.club))
    )
    return await session.scalars(query)
