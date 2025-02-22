from datetime import datetime, timezone
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubFollowersLink
from app.api.events.models import EventInterestsLink, Events
from app.api.interests.models import Interests
from app.api.users.models import UserInterests, Users
from app.core.validations.schema import validate_relations


async def user_related_events(
    session: AsyncSession, user_id: int, limit: int = 10, offset: int = 0
):
    await validate_relations(session, {"user_id": (Users, user_id)})
    current_datetime = datetime.now(timezone.utc)

    user_interests_subquery = (
        select(Interests.id)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(UserInterests.user_id == user_id)
        .scalar_subquery()
    )

    user_interested_events_subquery = (
        select(EventInterestsLink.event_id)
        .where(EventInterestsLink.interest_id.in_(user_interests_subquery))
        .scalar_subquery()
    )

    user_following_clubs_subquery = (
        select(ClubFollowersLink.club_id)
        .where(
            ClubFollowersLink.user_id == user_id, ClubFollowersLink.is_following == True
        )
        .scalar_subquery()
    )

    query = (
        select(Events)
        .where(
            or_(
                Events.club_id.in_(user_following_clubs_subquery),
                Events.id.in_(user_interested_events_subquery),
            ),
            Events.is_deleted == False,
            Events.event_datetime > current_datetime,
        )
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(query))
