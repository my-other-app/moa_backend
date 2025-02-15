from sqlalchemy import select
from app.response import CustomHTTPException
from app.api.clubs.models import Clubs
from app.api.clubs.schemas import CreateClub, EditClub
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


async def create_club(club: CreateClub, user_id: int, session: AsyncSession):
    db_club = Clubs(**club.model_dump(), created_by_id=user_id)
    session.add(db_club)
    await session.commit()
    await session.refresh(db_club)

    result = await session.execute(
        select(Clubs)
        .options(selectinload(Clubs.created_by))
        .filter(Clubs.id == db_club.id)
    )
    db_club_with_relations = result.scalar_one()

    return db_club_with_relations


async def update_club(club: EditClub, session: AsyncSession, user_id: int):
    db_club = await session.get(Clubs, club.id)
    if not db_club:
        raise CustomHTTPException(404, "Club not found")
    if db_club.created_by_id != user_id:
        raise CustomHTTPException(403, "Not authorized to update this club")
    db_club.update(club.model_dump())
    await session.commit()
    await session.refresh(db_club)
    return db_club


async def get_club(club_id: int, session: AsyncSession):
    db_club = await session.get(Clubs, club_id)
    if not db_club:
        raise CustomHTTPException(404, "Club not found")
    return db_club


async def get_all_clubs(session: AsyncSession, org_id: int = None):
    query = select(Clubs).options(selectinload(Clubs.created_by))
    if org_id:
        query = query.where(org_id=org_id)
    return query
