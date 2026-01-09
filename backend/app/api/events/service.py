from datetime import datetime, timezone
import io
from typing import Optional
from fastapi import Request, UploadFile
from sqlalchemy import and_, delete, exists, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.dialects.postgresql import INTERVAL

from app.response import CustomHTTPException
from app.api.events.schemas import (
    EventAdditionalDetail,
    EventCategoryCreate,
    EventEdit,
)
from app.api.events.models import (
    EventCategories,
    EventFiles,
    EventInterestsLink,
    EventPageViews,
    EventRegistrationsLink,
    Events,
    EventRatingsLink,
)
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.users.models import Users
from app.core.validations.schema import validate_relations
from app.api.interests.models import Interests
from app.core.utils.keys import generate_slug


async def create_event(
    session: AsyncSession,
    user_id: int,
    name: str,
    event_datetime: datetime,
    duration: float,
    category_id: int,
    poster: UploadFile | None = None,
    has_fee: bool = False,
    reg_fee: Optional[float] = None,
    location_name: Optional[str] = None,
    has_prize: bool = False,
    prize_amount: Optional[float] = None,
    is_online: bool = False,
    reg_startdate: datetime = datetime.now(timezone.utc),
    reg_enddate: Optional[datetime] = None,
    images: list[str] = [],
    about: Optional[str] = None,
    contact_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    url: Optional[str] = None,
    additional_details: Optional[list[EventAdditionalDetail]] = None,
    interest_ids: Optional[list[int]] = None,
    max_participants: Optional[int] = None,
    event_guidelines: Optional[str] = None,
    *args,
    **kwards,
):
    await validate_relations(
        session,
        {
            "category": (EventCategories, category_id),
            "user": (Users, user_id),
        },
    )
    user = await session.execute(
        select(Users).filter(Users.id == user_id).options(selectinload(Users.club))
    )
    user = user.scalar()

    if not user.club:
        raise CustomHTTPException(401, message="Not authorized to create event")
    for field in additional_details:
        if (
            field.field_type.value == "select"
            or field.field_type.value == "radio"
            or field.field_type.value == "checkbox"
        ):
            if not field.options:
                raise CustomHTTPException(
                    400, message="Options required for select, radio, checkbox fields"
                )
    slug = generate_slug(name)
    db_event = Events(
        name=name,
        slug=slug,
        event_datetime=event_datetime,
        duration=duration,
        category_id=category_id,
        has_fee=has_fee,
        reg_fee=reg_fee,
        location_name=location_name,
        has_prize=has_prize,
        prize_amount=prize_amount,
        is_online=is_online,
        reg_startdate=reg_startdate,
        reg_enddate=reg_enddate,
        images=images,
        about=about,
        contact_name=contact_name,
        contact_phone=contact_phone,
        contact_email=contact_email,
        url=url,
        club_id=user.club.id,
        max_participants=max_participants,
        additional_details=[x.model_dump(mode="json") for x in additional_details],
        event_guidelines=event_guidelines,
    )
    if poster:
        content = io.BytesIO(await poster.read())
        db_event.poster = {
            "bytes": content,
            "filename": poster.filename,
        }
    session.add(db_event)
    await session.commit()
    await session.refresh(db_event)
    event_id = db_event.id

    if interest_ids:
        for interest_id in interest_ids:
            interest_exists = await session.scalar(
                select(exists().where(Interests.id == interest_id))
            )
            if interest_exists:
                link = EventInterestsLink(event_id=event_id, interest_id=interest_id)
                session.add(link)

        await session.commit()
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == event_id)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
            selectinload(Events.interests),
        )
        .limit(1)
    )
    return db_event.scalar_one()


async def update_event(
    session: AsyncSession, event: EventEdit, user_id: int, event_id: int
):
    db_event = await session.execute(
        select(Events).options(joinedload(Events.club)).filter(Events.id == event_id)
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    if db_event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to update this event")

    if event.poster:
        content = io.BytesIO(await event.poster.read())
        db_event.poster = {
            "bytes": content,
            "filename": event.poster.filename,
        }

    await session.execute(
        delete(EventInterestsLink).where(EventInterestsLink.event_id == event_id)
    )
    for interest_id in event.interest_ids:
        interest_exists = await session.scalar(
            select(exists().where(Interests.id == interest_id))
        )
        if interest_exists:
            link = EventInterestsLink(event_id=event_id, interest_id=interest_id)
            session.add(link)
    db_event.name = event.name
    db_event.event_datetime = event.event_datetime
    db_event.duration = event.duration
    db_event.category_id = event.category_id
    db_event.has_fee = event.has_fee
    db_event.reg_fee = event.reg_fee
    db_event.location_name = event.location_name
    db_event.has_prize = event.has_prize
    db_event.prize_amount = event.prize_amount
    db_event.is_online = event.is_online
    db_event.reg_startdate = event.reg_startdate
    db_event.reg_enddate = event.reg_enddate
    db_event.about = event.about
    db_event.contact_name = event.contact_name
    db_event.contact_phone = event.contact_phone
    db_event.contact_email = event.contact_email
    db_event.additional_details = [
        x.model_dump(mode="json") for x in event.additional_details
    ]
    db_event.event_guidelines = event.event_guidelines
    db_event.url = event.url
    db_event.max_participants = event.max_participants
    await session.commit()
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == event_id)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
            selectinload(Events.interests),
        )
        .limit(1)
    )
    return db_event.scalar_one()


async def get_event(session: AsyncSession, event_id: str | int, user_id: int | None = None):
    is_event_id = (isinstance(event_id, str) and event_id.isdigit()) or isinstance(
        event_id, int
    )
    if is_event_id:
        event_id = int(event_id)
    ratings_subquery = (
        select(
            func.coalesce(func.avg(EventRatingsLink.rating), 0.0).label("rating"),
            func.coalesce(func.count(EventRatingsLink.id), 0).label("total_rating"),
        )
        .where(EventRatingsLink.event_id == Events.id)
        .subquery()
    )
    db_event = await session.execute(
        select(
            Events,
            ratings_subquery.c.rating.label("rating"),
            ratings_subquery.c.total_rating.label("total_rating"),
        )
        .filter(Events.id == event_id if is_event_id else Events.slug == event_id)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).options(joinedload(Interests.category)),
            selectinload(Events.files),  # Load event files for downloads
        )
    )
    db_event = db_event.first()

    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")

    print(db_event)
    event = db_event[0]
    data = event.__dict__
    data["rating"] = db_event[1]
    data["total_rating"] = db_event[2]
    
    # Get event files (downloads) - available to all users
    data["downloads"] = [
        {"name": f.name, "url": f.file} 
        for f in event.files if not f.is_deleted
    ] if event.files else []

    # User-specific data (only if user is authenticated)
    if user_id:
        # Check registration status
        registration = await session.scalar(
            select(EventRegistrationsLink).where(
                EventRegistrationsLink.event_id == event.id,
                EventRegistrationsLink.user_id == user_id,
                EventRegistrationsLink.is_deleted == False,
            )
        )
        if registration:
            data["is_registered"] = True
            data["is_attended"] = registration.is_attended
            data["ticket_id"] = registration.ticket_id
            data["user_position"] = registration.position
            # Build URLs from ticket_id
            data["ticket_url"] = f"/api/v1/events/tickets/{registration.ticket_id}"
            if registration.is_attended:
                data["certificate_url"] = f"/api/v1/events/certificates/{registration.ticket_id}"
            # Position label based on position
            if registration.position:
                if registration.position == 1:
                    data["position_label"] = "Winner"
                elif registration.position == 2:
                    data["position_label"] = "First Runner Up"
                elif registration.position == 3:
                    data["position_label"] = "Second Runner Up"
                else:
                    data["position_label"] = f"{registration.position}th Place"
        
        # Check user's rating for this event
        user_rating = await session.scalar(
            select(EventRatingsLink).where(
                EventRatingsLink.event_id == event.id,
                EventRatingsLink.user_id == user_id,
                EventRatingsLink.is_deleted == False,
            )
        )
        if user_rating:
            data["user_rating"] = user_rating.rating

    return data


async def list_events(
    session: AsyncSession,
    user_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    is_registered: bool | None = None,
    is_ended: bool | None = None,
    interest_ids: list[int] | None = None,
):
    """List events with filters."""

    # Base query
    query = select(Events).options(
        joinedload(Events.category),
        joinedload(Events.club),
    )

    if interest_ids:
        query = query.join(EventInterestsLink).filter(
            EventInterestsLink.interest_id.in_(interest_ids)
        )

    if is_following and user_id:
        query = (
            query.join(Clubs)
            .join(ClubUsersLink)
            .filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )

    if is_registered is not None and user_id:
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


async def create_event_category(
    session: AsyncSession, category: EventCategoryCreate, user_id: int
):
    db_category = EventCategories(**category.model_dump(), created_by_id=user_id)
    session.add(db_category)
    await session.commit()
    await session.refresh(db_category)
    return db_category


async def list_event_categories(session: AsyncSession):
    result = await session.execute(select(EventCategories))
    return result.scalars().all()


async def rate_event(
    session: AsyncSession,
    event_id: int,
    user_id: int,
    rating: float,
    review: str | None = None,
) -> EventRatingsLink:
    """Rate an event and update the corresponding club's rating."""
    await validate_relations(
        session,
        {
            "event": (Events, event_id),
            "user": (Users, user_id),
        },
    )

    # Check if user has already rated
    existing_rating = await session.scalar(
        select(EventRatingsLink).where(
            EventRatingsLink.event_id == event_id,
            EventRatingsLink.user_id == user_id,
            EventRatingsLink.is_deleted == False,
        )
    )

    # Get event and club
    event = await session.scalar(
        select(Events).where(Events.id == event_id).options(joinedload(Events.club))
    )
    if not event:
        raise CustomHTTPException(404, "Event not found")

    if not event.club:
        raise CustomHTTPException(400, "Event not associated with a club")
    if existing_rating:
        event_rating = existing_rating
        event_rating.rating = rating
        event_rating.review = review
    else:
        # Create rating
        event_rating = EventRatingsLink(
            event_id=event_id,
            user_id=user_id,
            rating=rating,
            review=review,
        )
        session.add(event_rating)

    # Update club rating
    club = event.club
    total_ratings = await session.scalar(
        select(func.count()).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(Events.club_id == club.id)
            ),
            EventRatingsLink.is_deleted == False,
        )
    )
    avg_rating = await session.scalar(
        select(func.avg(EventRatingsLink.rating)).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(Events.club_id == club.id)
            ),
            EventRatingsLink.is_deleted == False,
        )
    )

    club.total_ratings = total_ratings + 1 if total_ratings else 0
    club.rating = avg_rating if avg_rating else 0

    await session.commit()
    await session.refresh(event_rating)
    return event_rating


async def get_ticket_details(session: AsyncSession, ticket_id: str | int):
    is_ticket_id = (isinstance(ticket_id, str) and ticket_id.isdigit()) or isinstance(
        ticket_id, int
    )
    if is_ticket_id:
        ticket_id = int(ticket_id)
    registration = await session.execute(
        select(EventRegistrationsLink)
        .filter(
            EventRegistrationsLink.id == ticket_id
            if is_ticket_id
            else EventRegistrationsLink.ticket_id == ticket_id
        )
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            ),
            joinedload(EventRegistrationsLink.user),
        )
    )
    registration = registration.scalar()
    if registration is None:
        raise CustomHTTPException(404, "Ticket not found")
    return registration


async def delete_event(session: AsyncSession, event_id: int, user_id: int):
    db_event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    if db_event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to delete this event")
    registration_count = await session.scalar(
        select(func.count()).filter(EventRegistrationsLink.event_id == event_id)
    )
    if registration_count > 0:
        raise CustomHTTPException(400, message="Cannot delete event with registrations")
    db_event.soft_delete()
    await session.commit()
    return None


async def increment_event_page_view(
    session: AsyncSession,
    request: Request,
    event_id: int | str,
    user_id: int | None = None,
):
    db_event = await session.execute(
        select(Events)
        .filter(
            Events.id == event_id
            if isinstance(event_id, int)
            else Events.slug == event_id
        )
        .options(joinedload(Events.club))
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    if not user_id or (user_id and user_id != db_event.club.user_id):
        db_event.page_views += 1
        # await session.commit()

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.client.host

    event_view = EventPageViews(
        event_id=event_id,
        user_agent=request.headers.get("User-Agent"),
        ip_address=client_ip,
        user_id=user_id,
    )
    session.add(event_view)

    await session.commit()

    return None
