from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.events.schemas import EventCategoryCreate, EventCreate, EventEdit
from app.api.events.models import EventCategories, Events
from app.api.clubs.models import Clubs


async def create_event(session: AsyncSession, event: EventCreate, created_by_id: int):
    db_event = Events(**event.model_dump(), created_by_id=created_by_id)
    session.add(db_event)
    await session.commit()
    await session.refresh(db_event)
    db_event = await session.execute(
        select(Events)
        .filter(Events.id == db_event.id)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.created_by),
            selectinload(Events.org),
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
            selectinload(Events.created_by),
            selectinload(Events.org),
        )
    )
    db_event = db_event.scalar()
    if db_event is None:
        raise CustomHTTPException(404, message="Event not found")
    return db_event


async def list_events(session: AsyncSession):
    select_stmt = (
        select(Events)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.org),
        )
        .order_by(Events.created_at.desc())
    )
    return select_stmt


async def create_event_category(
    session: AsyncSession, category: EventCategoryCreate, user_id: int
):
    db_category = EventCategories(**category.model_dump(), created_by_id=user_id)
    session.add(db_category)
    await session.commit()
    await session.refresh(db_category)
    return db_category
