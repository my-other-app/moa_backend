import io
from fastapi import UploadFile
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.clubs.models import ClubFollowersLink, Clubs, Notes
from app.api.clubs.schemas import CreateClub, EditClub
from app.api.users.models import UserTypes, Users
from app.api.users.service import create_user


async def create_club(
    session: AsyncSession,
    name: str,
    email: str,
    password: str,
    phone: str | None = None,
    about: str | None = None,
    org_id: int | None = None,
    location_name: str | None = None,
    logo: UploadFile | None = None,
):
    user = await create_user(session, name, email, phone, password, UserTypes.club)
    if not user:
        raise CustomHTTPException(
            500,
            "An unexpected error occured while creating club. (ERR: Unable to create user)",
        )

    db_club = Clubs(
        name=name,
        about=about,
        org_id=org_id,
        location_name=location_name,
        user_id=user.id,
    )
    if logo:
        content = io.BytesIO(await logo.read())
        db_club.logo = {
            "bytes": content,
            "filename": logo.filename,
        }
    session.add(db_club)
    await session.commit()
    await session.refresh(db_club)
    return db_club


async def update_club(club: EditClub, session: AsyncSession, user_id: int):
    db_club = await session.get(Clubs, club.id)
    if not db_club:
        raise CustomHTTPException(404, "Club not found")
    if db_club.created_by_id != user_id:
        raise CustomHTTPException(403, "Not authorized to update this club")
    if club.logo:
        content = io.BytesIO(await club.logo.read())
        if db_club.logo:
            db_club.logo.delete()
        db_club.logo = {
            "bytes": content,
            "filename": club.logo.filename,
        }
    db_club.update(club.model_dump())
    await session.commit()
    await session.refresh(db_club)
    return db_club


async def get_club(club_id: int, session: AsyncSession):
    db_club = await session.get(Clubs, club_id)
    if not db_club:
        raise CustomHTTPException(404, "Club not found")
    return db_club


async def get_all_clubs(
    session: AsyncSession, org_id: int = None, limit: int = 10, offset: int = 0
):
    query = select(Clubs).limit(limit).offset(offset)
    if org_id:
        query = query.where(org_id=org_id)
    return list(await session.scalars(query))


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


async def list_notes(
    session: AsyncSession, club_id: int, limit: int = 10, offset: int = 0
):
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    query = (
        select(Notes)
        .where(Notes.club_id == club_id)
        .order_by(Notes.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(query))
