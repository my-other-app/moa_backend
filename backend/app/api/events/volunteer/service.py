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
    # TODO: Send email to user
    # Two scenarios in that is user is already registered and user is not registered
    # If user is not registered send the app link to the user
    await session.commit()
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

    registration = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.ticket_id == ticker_id,
        )
        .options(joinedload(EventRegistrationsLink.event))
    )
    if not registration:
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
    return True


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
