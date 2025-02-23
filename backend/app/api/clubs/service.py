import io
from fastapi import UploadFile
from sqlalchemy import and_, exists, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.clubs.models import ClubInterestsLink, ClubUsersLink, Clubs, Notes
from app.api.clubs.schemas import EditClub
from app.api.users.models import UserTypes, Users
from app.api.users.service import create_user
from app.api.interests.models import Interests


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
    interest_ids: list[int] | None = None,
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

    if interest_ids:
        for interest_id in interest_ids:
            print(interest_id)
            if not await session.scalar(
                select(
                    exists().where(
                        Interests.id == interest_id,
                        Interests.is_deleted == False,
                    )
                )
            ):
                continue
            print("here")
            link = ClubInterestsLink(club_id=db_club.id, interest_id=interest_id)
            session.add(link)
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
    session: AsyncSession,
    user_id: int | None = None,
    org_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    interest_ids: list[int] | None = None,
):
    """Get all clubs with optional filters."""
    query = select(Clubs).distinct()

    if org_id:
        query = query.filter(Clubs.org_id == org_id)

    if interest_ids:
        query = query.join(ClubInterestsLink).filter(
            ClubInterestsLink.interest_id.in_(interest_ids)
        )

    if is_following is not None and user_id:
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
    return list(await session.scalars(query))


async def follow_club(session: AsyncSession, club_id: int, user_id: int):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    follow = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.user_id == user_id, ClubUsersLink.club_id == club_id
        )
    )
    if follow:
        if follow.is_following:
            raise CustomHTTPException(409, "already following")
        follow.is_following = True
    else:
        follow = ClubUsersLink(user_id=user_id, club_id=club_id, is_following=True)
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
        select(ClubUsersLink).where(
            ClubUsersLink.user_id == user_id, ClubUsersLink.club_id == club_id
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


async def toggle_pin_club(
    session: AsyncSession, club_id: int, user_id: int
) -> ClubUsersLink:
    """Toggle pin status for a club."""
    link = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_deleted == False,
        )
    )
    if not link:
        link = ClubUsersLink(club_id=club_id, user_id=user_id)
        session.add(link)

    link.is_pinned = not link.is_pinned
    await session.commit()
    await session.refresh(link)
    return link


async def toggle_hide_club(
    session: AsyncSession, club_id: int, user_id: int
) -> ClubUsersLink:
    """Toggle hide status for a club."""
    link = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_deleted == False,
        )
    )
    if not link:
        link = ClubUsersLink(club_id=club_id, user_id=user_id)
        session.add(link)

    link.is_hidden = not link.is_hidden
    await session.commit()
    await session.refresh(link)
    return link


async def list_club_followers(
    session: AsyncSession, club_id: int, limit: int = 10, offset: int = 0
):
    """List followers of a club."""
    query = (
        select(ClubUsersLink)
        .where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
        .options(selectinload(ClubUsersLink.user))
        .order_by(ClubUsersLink.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return result.scalars().all()


async def get_club_details(
    session: AsyncSession, club_id: int, user_id: int | None = None
):
    """Get detailed information about a club."""
    query = (
        select(Clubs)
        .where(Clubs.id == club_id)
        .options(
            selectinload(Clubs.interests),
        )
    )
    club = await session.scalar(query)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    # Get followers count
    followers_count = await session.scalar(
        select(func.count()).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
    )

    # Get user-specific data if user_id provided
    user_data = None
    if user_id:
        user_link = await session.scalar(
            select(ClubUsersLink).where(
                ClubUsersLink.club_id == club_id,
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_deleted == False,
            )
        )
        if user_link:
            user_data = {
                "is_following": user_link.is_following,
                "is_pinned": user_link.is_pinned,
                "is_hidden": user_link.is_hidden,
            }

    # Convert to dict for adding additional fields
    club_dict = club.__dict__
    club_dict["followers_count"] = followers_count
    club_dict["user_data"] = user_data
    club_dict["interests"] = [interest.__dict__ for interest in club.interests]

    return club_dict
