from fastapi.encoders import jsonable_encoder
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events.volunteer.models import Volunteer
from app.api.users.models import UserProfiles, Users


async def update_volunteers(
    session: AsyncSession, email_ids: list[str], event_id: int, club_id: int
) -> None:
    """Add volunteers to an event."""

    await session.execute(
        delete(Volunteer).where(
            Volunteer.event_id == event_id, Volunteer.club_id == club_id
        )
    )
    users = await session.execute(
        select(Users.id, Users.email).where(
            Users.email.in_(email_ids), Users.user_type == "app_user"
        )
    )
    users = {email: user_id for user_id, email in users}

    for email in email_ids:
        volunteer = Volunteer(
            email=email,
            event_id=event_id,
            user_id=users.get(email),
            club_id=club_id,
            is_approved=True,  # TODO: Change this to manual approval by the requested user.
        )
        session.add(volunteer)
        # TODO: Send email to user
        # Two scenarios in that is user is already registered and user is not registered
        # If user is not registered send the app link to the user
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
