from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.clubs.models import ClubFollowersLink, Clubs, Notes
from app.api.clubs.schemas import CreateClub, EditClub
from app.api.users.models import Users


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


async def follow_club(session: AsyncSession, club_id: int, user_id: int):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    follow = await session.scalar(
        select(ClubFollowersLink).where(
            ClubFollowersLink.user_id == user_id, ClubFollowersLink.club_id == club_id
        )
    )
    if follow:
        if follow.is_following:
            raise CustomHTTPException(409, "already following")
        follow.is_following = True
    else:
        follow = ClubFollowersLink(user_id=user_id, club_id=club_id, is_following=True)
        session.add(follow)
    await session.commit()
    await session.refresh(follow)
    return follow


async def unfollow_club(session: AsyncSession, club_id: int, user_id: int):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    follow = await session.scalar(
        select(ClubFollowersLink).where(
            ClubFollowersLink.user_id == user_id, ClubFollowersLink.club_id == club_id
        )
    )

    if not follow:
        raise CustomHTTPException(409, "not following the club")

    follow.is_following = False

    await session.commit()
    await session.refresh(follow)
    return follow


async def create_note(
    session: AsyncSession, club_id: int, user_id: int, title: str, note: str
):
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    note = Notes(club_id=club_id, user_id=user_id, title=title, note=note)

    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


async def list_notes(session: AsyncSession, club_id: int):
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    query = (
        select(Notes).where(Notes.club_id == club_id).order_by(Notes.created_at.desc())
    )
    return query
