from datetime import datetime, timezone
from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects.postgresql import INTERVAL

from app.api.events.models import Events, EventRegistrationsLink, EventInterestsLink
from app.api.clubs.models import Clubs, ClubUsersLink, ClubInterestsLink, Notes
from app.api.users.models import UserInterests, Users
from app.api.interests.models import Interests


async def suggest_events(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    is_registered: bool | None = None,
    is_ended: bool | None = None,
):
    """Suggest events based on user interests and filters."""
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
    # Base query
    query = (
        select(Events)
        .options(joinedload(Events.club), joinedload(Events.category))
        .where(
            or_(
                Events.id.in_(user_interested_events_subquery),
                Events.club_id.in_(
                    select(ClubUsersLink.club_id)
                    .where(
                        ClubUsersLink.user_id == user_id,
                        ClubUsersLink.is_following == True,
                        ClubUsersLink.is_deleted == False,
                    )
                    .scalar_subquery()
                ),
            )
        )
    )

    if is_following:
        query = (
            query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )

    if is_registered is not None:
        if is_registered:
            query = query.join(EventRegistrationsLink).filter(
                EventRegistrationsLink.user_id == user_id,
                EventRegistrationsLink.is_deleted == False,
            )
        else:
            query = query.outerjoin(
                EventRegistrationsLink,
                and_(
                    EventRegistrationsLink.event_id == Events.id,
                    EventRegistrationsLink.user_id == user_id,
                    EventRegistrationsLink.is_deleted == False,
                ),
            ).filter(EventRegistrationsLink.id == None)

    if is_ended is not None:
        if is_ended:
            query = query.filter(
                Events.event_datetime
                > (
                    func.now()
                    + func.cast(func.concat(Events.duration, " HOURS"), INTERVAL)
                )
            )
        else:
            query = query.filter(
                Events.event_datetime
                <= (
                    func.now()
                    + func.cast(func.concat(Events.duration, " HOURS"), INTERVAL)
                )
            )

    query = query.order_by(Events.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return result.scalars().all()


async def suggest_clubs(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
):
    """Suggest clubs based on user interests and filters."""

    user_interests_subquery = (
        select(Interests.id)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(UserInterests.user_id == user_id)
        .scalar_subquery()
    )

    query = (
        select(Clubs)
        .distinct()
        .where(
            Clubs.id.in_(
                select(ClubInterestsLink.club_id)
                .where(ClubInterestsLink.interest_id.in_(user_interests_subquery))
                .scalar_subquery()
            )
        )
    )

    if is_following is not None:
        if is_following:
            query = query.join(ClubUsersLink).filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        else:
            query = query.outerjoin(
                ClubUsersLink,
                and_(
                    ClubUsersLink.club_id == Clubs.id,
                    ClubUsersLink.user_id == user_id,
                    ClubUsersLink.is_following == True,
                    ClubUsersLink.is_deleted == False,
                ),
            ).filter(ClubUsersLink.id == None)

    query = query.order_by(Clubs.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return result.scalars().all()


async def suggest_notes(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
):
    """Suggest notes based on user interests and filters."""
    query = select(Notes).distinct().options(joinedload(Notes.club))

    if is_following:
        query = (
            query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )

    query = query.order_by(Notes.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(query)
    return result.scalars().all()


async def global_search(
    session: AsyncSession,
    search_query: str,
    user_id: int,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    interest_ids: list[int] | None = None,
):
    """Global search across events, clubs, and notes."""
    # Search events
    events_query = (
        select(Events)
        .distinct(Events.id)
        .options(
            joinedload(Events.club, innerjoin=True),
            joinedload(Events.category, innerjoin=True),
        )
        .filter(
            or_(
                Events.name.ilike(f"%{search_query}%"),
                Events.about.ilike(f"%{search_query}%"),
            )
        )
    )

    # Search clubs
    clubs_query = (
        select(Clubs)
        .distinct(Clubs.id)
        .filter(
            or_(
                Clubs.name.ilike(f"%{search_query}%"),
                Clubs.about.ilike(f"%{search_query}%"),
            )
        )
    )

    # Search notes
    notes_query = (
        select(Notes)
        .distinct(Notes.id)
        .options(joinedload(Notes.club, innerjoin=True))
        .filter(
            or_(
                Notes.title.ilike(f"%{search_query}%"),
                Notes.note.ilike(f"%{search_query}%"),
            )
        )
    )

    # Apply interest filters
    if interest_ids:
        events_query = events_query.join(EventInterestsLink).filter(
            EventInterestsLink.interest_id.in_(interest_ids)
        )
        clubs_query = clubs_query.join(ClubInterestsLink).filter(
            ClubInterestsLink.interest_id.in_(interest_ids)
        )

    # Apply following filter
    if is_following:
        events_query = (
            events_query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )
        clubs_query = clubs_query.join(ClubUsersLink).filter(
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
        notes_query = (
            notes_query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )

    # Execute queries
    events = await session.execute(events_query.limit(limit).offset(offset))
    clubs = await session.execute(clubs_query.limit(limit).offset(offset))
    notes = await session.execute(notes_query.limit(limit).offset(offset))

    return {
        "events": events.scalars().all(),
        "clubs": clubs.scalars().all(),
        "notes": notes.scalars().all(),
    }
