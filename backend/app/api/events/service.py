from datetime import datetime, timezone
import io
from typing import Optional
from fastapi import UploadFile
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.response import CustomHTTPException
from app.api.events.schemas import (
    Event,
    EventAdditionalDetail,
    EventCategoryCreate,
    EventEdit,
)
from app.api.events.models import (
    EventCategories,
    EventInterestsLink,
    EventRegistrationsLink,
    Events,
)
from app.api.clubs.models import Clubs
from app.api.users.models import Users
from app.core.validations.schema import validate_relations
from app.api.interests.models import Interests


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
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    url: Optional[str] = None,
    additional_details: Optional[list[EventAdditionalDetail]] = None,
    interest_ids: Optional[list[int]] = None,
    *args,
    **kwards
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
    db_event = Events(
        name=name,
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
        contact_phone=contact_phone,
        contact_email=contact_email,
        url=url,
        club_id=user.club.id,
        additional_details=[x.model_dump(mode="json") for x in additional_details],
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
            selectinload(Events.interests).options(joinedload(Interests.category)),
        )
        .limit(1)
    )
    return db_event.scalar_one()


async def update_event(session: AsyncSession, event: EventEdit, user_id: int):
    db_event = await session.execute(select(Events).filter(Events.id == event.id))
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    if db_event.created_by_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to update this event")
    db_event.update(event.model_dump())
    await session.commit()
    return db_event


async def get_event(session: AsyncSession, event_id: int):
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == event_id)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).options(joinedload(Interests.category)),
        )
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    return db_event


async def list_events(session: AsyncSession, limit: int = 10, offset: int = 0):
    select_stmt = (
        select(Events)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
            selectinload(Events.interests),
        )
        .order_by(Events.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.scalars(select_stmt)
    print(result)
    return list(result)


async def create_event_category(
    session: AsyncSession, category: EventCategoryCreate, user_id: int
):
    db_category = EventCategories(**category.model_dump(), created_by_id=user_id)
    session.add(db_category)
    await session.commit()
    await session.refresh(db_category)
    return db_category


async def register_event(
    session: AsyncSession,
    event_id: int,
    user_id: int,
    additional_details: dict | None = None,
):
    await validate_relations(
        session,
        {
            "event": (Events, event_id),
            "user": (Users, user_id),
        },
    )
    db_event = await session.execute(select(Events).filter(Events.id == event_id))
    db_event = db_event.scalar()
    db_event = Event.model_validate(db_event, from_attributes=True)

    if db_event.additional_details:
        if not additional_details:
            raise CustomHTTPException(
                400, message="Additional details required for this event"
            )
        errors = {}
        for field in db_event.additional_details:
            if field.key not in additional_details.keys():
                errors[field.key] = "This field is required"
                continue
            if field.field_type.value in ("select", "radio", "checkbox"):
                if additional_details[field.key] not in field.options:
                    errors[field.key] = "Invalid option selected"
        if errors:
            raise CustomHTTPException(400, message=errors)

    registration = await session.scalar(
        select(
            exists()
            .where(EventRegistrationsLink.event_id == event_id)
            .where(EventRegistrationsLink.user_id == user_id)
            .where(EventRegistrationsLink.is_deleted == False)
        )
    )
    if registration:
        raise CustomHTTPException(400, message="Already registered for this event")
    registration = EventRegistrationsLink(
        event_id=event_id, user_id=user_id, additional_details=additional_details
    )
    session.add(registration)
    await session.commit()
    return await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            selectinload(EventRegistrationsLink.event),
            selectinload(EventRegistrationsLink.user),
        )
    )


async def list_event_registrations(
    session: AsyncSession, user_id: int, event_id: int, limit: int = 10, offset: int = 0
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to view this event")

    scalar_result = await session.scalars(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRegistrationsLink.user),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(scalar_result)


async def get_registration(
    session: AsyncSession, user_id: int, event_id: int, registration_id: int
):
    event = await session.execute(
        select(Events).filter(Events.id == event_id).options(joinedload(Events.club))
    )
    event = event.scalar()

    if event is None:
        raise CustomHTTPException(404, message="Event not found")

    if event.club.user_id != user_id:
        raise CustomHTTPException(403, message="Not authorized to view this event")

    scalar_result = await session.scalar(
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.event_id == event_id,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.id == registration_id,
        )
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
