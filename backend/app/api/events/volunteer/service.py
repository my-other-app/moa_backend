from datetime import datetime, timezone
from sqlalchemy import and_, case, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.events.volunteer.models import Volunteer
from app.api.users.models import UserProfiles, Users
from app.response import CustomHTTPException
from app.api.events.models import EventRegistrationsLink, Events
from app.api.clubs.models import Clubs
from app.core.notifications.triggers import notify_user_added_as_volunteer


async def _assert_club_owns_target(
    session: AsyncSession,
    caller_user_id: int,
    event_id: int | None,
    club_id: int | None,
) -> None:
    """Raise 403 unless the caller (matched by Users.id, same convention as
    registration/service.py's `event.club.user_id != user_id` checks) actually
    owns the event/club being targeted."""
    if event_id is not None:
        event = await session.scalar(
            select(Events)
            .where(Events.id == event_id, Events.is_deleted == False)
            .options(joinedload(Events.club))
        )
        if not event or not event.club or event.club.user_id != caller_user_id:
            raise CustomHTTPException(
                403, "You do not have permission to manage volunteers for this event."
            )
    elif club_id is not None:
        club = await session.scalar(
            select(Clubs).where(Clubs.id == club_id, Clubs.is_deleted == False)
        )
        if not club or club.user_id != caller_user_id:
            raise CustomHTTPException(
                403, "You do not have permission to manage volunteers for this club."
            )


async def add_volunteer(
    session: AsyncSession,
    caller_user_id: int,
    email_id: str,
    event_id: int,
    club_id: int,
    full_name: str | None = None,
    phone: str | None = None,
) -> None:
    """Add volunteers to an event."""
    await _assert_club_owns_target(session, caller_user_id, event_id, club_id)

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
    
    # Check if user exists. Any kind of account, a club's own included, as
    # before; ignoring case, and the app account first when there is one.
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    user = await session.scalar(
        select(Users)
        .where(user_service.same_email(Users.email, email_id), Users.is_deleted == False)
        .order_by(
            case((Users.user_type == UserTypes.app_user, 0), (Users.user_type == UserTypes.guest, 2), else_=1),
            Users.id.desc(),
        )
        .limit(1)
    )

    if not user:
        if not full_name:
            raise CustomHTTPException(
                status_code=400, message="User not found. Please provide full name to add as guest."
            )
        
        # Create guest user
        user = await user_service.create_user(
            session=session,
            full_name=full_name,
            email=email_id,
            phone=phone,
            provider="email",
            user_type=UserTypes.guest,
        )

    volunteer = Volunteer(
        email=email_id,
        event_id=event_id,
        user_id=user.id,
        club_id=club_id,
        is_approved=True,  # TODO: Change this to manual approval by the requested user.
    )

    session.add(volunteer)
    await session.commit()
    
    # Trigger 5: Send notification to user about being added as volunteer
    if user.id:
        try:
            # Get event and club details for notification
            event = await session.scalar(
                select(Events).where(Events.id == event_id).options(joinedload(Events.club))
            )
            if event and event.club:
                await notify_user_added_as_volunteer(
                    session=session,
                    user_id=user.id,
                    event_id=event_id,
                    event_name=event.name,
                    club_name=event.club.name,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Failed to send volunteer notification: {e}")
    
    return None


async def remove_event_volunteer(
    session: AsyncSession, caller_user_id: int, email_id: str, event_id: int
) -> None:
    """Remove volunteers from an event."""
    await _assert_club_owns_target(session, caller_user_id, event_id, None)

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
    session: AsyncSession, caller_user_id: int, email_id: str, club_id: int
) -> None:
    """Remove volunteers from a club."""
    await _assert_club_owns_target(session, caller_user_id, None, club_id)

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
    session: AsyncSession,
    event_id: int,
    include_club_volunteers=True,
    caller_user_id: int | None = None,
) -> list[dict]:
    """List all volunteers for an event."""
    if caller_user_id is not None and event_id is not None:
        await _assert_club_owns_target(session, caller_user_id, event_id, None)

    if include_club_volunteers:
        result = (
            select(Volunteer, UserProfiles)
            .outerjoin(Clubs, Clubs.id == Volunteer.club_id)
            .outerjoin(Events, Events.club_id == Clubs.id)
            .where(
                or_(
                    Volunteer.event_id == event_id,
                    and_(Events.club_id == Volunteer.club_id, Events.id == event_id),
                ),
                Volunteer.is_deleted == False,  # Filter deleted volunteers
            )
            .outerjoin(UserProfiles, Volunteer.user_id == UserProfiles.user_id)
            .distinct()
        )
    else:
        result = (
            select(Volunteer, UserProfiles)
            .where(
                Volunteer.event_id == event_id,
                Volunteer.is_deleted == False,  # Filter deleted volunteers
            )
            .outerjoin(UserProfiles, Volunteer.user_id == UserProfiles.user_id)
        )
    result = await session.execute(result)
    
    # Return only the fields expected by ListVolunteersResponse schema
    return [
        {
            "email": volunteer.email,
            "is_approved": volunteer.is_approved,
            "user_id": volunteer.user_id,
            "full_name": profile.full_name if profile else None,
            "profile_pic": _photo_link(profile.profile_pic) if profile else None,
        }
        for volunteer, profile in result
    ]


def _photo_link(pic) -> str | None:
    """One link for an uploaded photo. The column reads back as its links by
    size, and the response's `profile_pic` is a string: a volunteer with a
    photo turned the whole list into a 500 -- and the app now lets anyone
    upload one."""
    if isinstance(pic, dict):
        return pic.get("medium") or pic.get("thumbnail") or pic.get("original")
    return pic if isinstance(pic, str) and pic else None


async def checkin_user(
    session: AsyncSession, event_id: int, ticker_id: str, volunteer_id: int, background=None
) -> None:
    """Check-in a participant for an event, from the volunteer app.

    Accepts a ticket id (MOA-XXXXX-XXXXX) or a registration UUID -- not an
    admission number, which is not a scan code. Goes through the same
    admission as every door, so the check-in is recorded with who did it;
    the app's error codes are kept.
    """
    from app.api.checkins import service as checkins
    from app.api.checkins.gate_auth import GateActor
    from app.api.checkins.models import CheckInVia

    event = await session.scalar(select(Events).where(Events.id == event_id).options(joinedload(Events.club)))
    if event is None:
        raise CustomHTTPException(404, "Event not found", error_code="EVENT_NOT_FOUND")
    volunteer = await session.scalar(select(Volunteer).where(Volunteer.id == volunteer_id))
    user = await session.scalar(select(Users).where(Users.id == volunteer.user_id)) if volunteer and volunteer.user_id else None
    actor = GateActor(kind="user", name=(user.full_name if user else None) or "Volunteer", user=user, volunteer=volunteer, can_check_in=True)
    result = await checkins.scan(
        session, event, code=ticker_id.strip(), door_label=None, client_ref=None, scanned_at=None, actor=actor, via=CheckInVia.app, background=background
    )
    outcome = result["outcome"]
    if outcome == "CHECKED_IN":
        return True
    if outcome == "WRONG_EVENT":
        name = (result.get("other_event") or {}).get("name") or "another event"
        raise CustomHTTPException(400, f"Incorrect event: This ticket belongs to '{name}'", error_code="TICKET_WRONG_EVENT")
    if outcome == "ALREADY_CHECKED_IN":
        raise CustomHTTPException(400, "User already checked-in", error_code="TICKET_ALREADY_USED")
    if outcome == "UNPAID":
        raise CustomHTTPException(400, "User has not paid the fee, Please verify the payment details.", error_code="FEE_UNPAID")
    if outcome == "REVOKED":
        raise CustomHTTPException(400, "This ticket was revoked.", error_code="TICKET_REVOKED")
    if outcome == "EVENT_NOT_OPEN":
        raise CustomHTTPException(400, "The door is closed for this event.", error_code="EVENT_NOT_OPEN")
    raise CustomHTTPException(400, "Registration not found", error_code="REGISTRATION_NOT_FOUND")


async def validate_ticket(
    session: AsyncSession, event_id: int, ticket_id: str
) -> dict:
    """
    Validate a ticket without marking attendance.
    Returns ticket details if valid, raises exception if invalid.
    Accepts either:
    - ticket_id format: MOA-XXXXX-XXXXX
    - registration UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

    The door's own lookup (checkins.service): the code on this event off the
    ticket id's unique index, and only on a miss the same code anywhere, to
    tell a ticket for another event from one that does not exist. It used to
    read, and log, the ticket ids of the event on every miss.
    """
    from app.api.checkins import service as checkins
    from app.api.users.models import UserProfiles

    ticket_id = ticket_id.strip()
    registration = await checkins.lookup(session, event_id, ticket_id)
    if not registration:
        other = await checkins.elsewhere(session, ticket_id)
        if other is not None and other.event_id != event_id:
            event_name = other.event.name if other.event else "another event"
            raise CustomHTTPException(
                400,
                f"Incorrect event: This ticket belongs to '{event_name}'",
                error_code="TICKET_WRONG_EVENT",
            )
        raise CustomHTTPException(
            400, "Registration not found", error_code="REGISTRATION_NOT_FOUND"
        )

    if registration.is_attended:
        raise CustomHTTPException(
            400, "User already checked-in", error_code="TICKET_ALREADY_USED"
        )

    if registration.event.has_fee and not registration.is_paid:
        raise CustomHTTPException(
            400,
            "User has not paid the fee, Please verify the payment details.",
            error_code="FEE_UNPAID",
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
        "ticket_holder_email": registration.email,
        "event_name": registration.event.name,
        "event_datetime": registration.event.event_datetime.isoformat() if registration.event.event_datetime else None,
        "location": registration.event.location_name,
        "is_paid": registration.is_paid,
        "has_fee": registration.event.has_fee,
    }


async def is_volunteer(session: AsyncSession, user_id: int, event_id: int):
    """This user's volunteer place for the event, or for the club running it.

    The club-wide branch used to match any volunteer row of the event's club
    without asking whose it was, so any signed-in account at all -- a guest
    fresh from the public form included -- could check people in at every
    event of a club with one club-wide volunteer. Same rule as the gate's
    (checkins.gate_auth.actor_for)."""
    club_id = select(Events.club_id).where(Events.id == event_id).scalar_subquery()
    return await session.scalar(
        select(Volunteer)
        .where(
            Volunteer.user_id == user_id,
            Volunteer.is_deleted == False,
            Volunteer.is_approved == True,
            or_(Volunteer.event_id == event_id, Volunteer.club_id == club_id),
        )
        .order_by(Volunteer.event_id.desc().nulls_last())
        .limit(1)
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
