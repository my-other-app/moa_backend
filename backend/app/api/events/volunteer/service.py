from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events.volunteer.models import Volunteer
from app.api.users.models import UserProfiles, Users
from app.response import CustomHTTPException


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
        select(Users.id).where(Users.email == email_id, Users.user_type == "app_user")
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


async def remove_volunteer(session: AsyncSession, email_id: str, event_id: int) -> None:
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


async def list_volunteers(session: AsyncSession, event_id: int) -> list[dict]:
    """List all volunteers for an event."""
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
