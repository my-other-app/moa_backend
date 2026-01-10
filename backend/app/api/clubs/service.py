import io
from fastapi import UploadFile
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, exists, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, aliased, joinedload
import secrets


from app.core.utils.keys import generate_slug
from app.response import CustomHTTPException
from app.api.clubs.models import (
    ClubInterestsLink,
    ClubUsersLink,
    Clubs,
    Notes,
    ClubSocials,
)
from app.api.clubs.schemas import ClubSocialsCreate, CreateClub
from app.api.users.models import UserProfiles, UserTypes, Users
from app.api.users.service import create_user
from app.api.interests.models import Interests
from app.core.validations.schema import validate_relations
from app.api.orgs.models import Organizations
from app.api.events.models import Events


async def create_club(
    session: AsyncSession,
    name: str,
    email: str,
    password: str | None = None,
    phone: str | None = None,
    about: str | None = None,
    org_id: int | None = None,
    location_name: str | None = None,
    logo: UploadFile | None = None,
    interest_ids: list[int] | None = None,
    is_admin_created: bool = False,
) -> Clubs:
    """Create a new club."""
    if not is_admin_created and not password:
        raise CustomHTTPException(400, "Password is required")

    existing_club = await session.scalar(
        select(Clubs).where(
            func.lower(func.replace(Clubs.name, " ", ""))
            == func.lower(func.replace(name, " ", "")),
            Clubs.is_deleted == False,
        )
    )
    if existing_club:
        raise CustomHTTPException(400, "Club with this name already exists")

    slug = generate_slug(name)

    if is_admin_created:
        password = secrets.token_urlsafe(12)  # Generate a secure random password

    club_user = await create_user(
        session,
        full_name=name,
        email=email,
        password=password,
        phone=phone,
        user_type=UserTypes.club,
    )

    club = Clubs(
        name=name,
        slug=slug,
        about=about,
        org_id=org_id,
        location_name=location_name,
        user_id=club_user.id,
        is_verified=is_admin_created,
        # Note: initial_password removed for security - use password reset flow instead
    )

    if logo:
        content = io.BytesIO(await logo.read())
        club.logo = {
            "bytes": content,
            "filename": logo.filename,
        }

    session.add(club)
    await session.commit()
    await session.refresh(club)

    if interest_ids:
        interests = await session.scalars(
            select(Interests).where(Interests.id.in_(interest_ids))
        )
        for interest in interests:
            link = ClubInterestsLink(club_id=club.id, interest_id=interest.id)
            session.add(link)

    await session.commit()
    await session.refresh(club)
    return club


async def update_club(session: AsyncSession, club_id: int, club: CreateClub):
    await validate_relations(
        session,
        {
            "org_id": (Organizations, club.org_id),
        },
    )
    db_club = (
        select(Clubs).where(Clubs.id == club_id).options(selectinload(Clubs.interests))
    )
    db_club = await session.scalar(db_club)

    if not db_club:
        raise CustomHTTPException(404, "Club not found")

    if club.logo:
        content = io.BytesIO(await club.logo.read())
        if db_club.logo:
            db_club.logo.delete()
        db_club.logo = {
            "bytes": content,
            "filename": club.logo.filename,
        }

    db_club.name = club.name
    db_club.about = club.about
    db_club.location_name = club.location_name
    db_club.location_link = club.location_link
    db_club.org_id = club.org_id
    db_club.contact_phone = club.contact_phone
    db_club.contact_email = club.contact_email

    await session.execute(
        delete(ClubInterestsLink).where(ClubInterestsLink.club_id == db_club.id)
    )

    if club.interest_ids:
        interests = await session.scalars(
            select(Interests).where(Interests.id.in_(club.interest_ids))
        )
        for interest in interests:
            link = ClubInterestsLink(club_id=db_club.id, interest_id=interest.id)
            session.add(link)
    await session.commit()
    await session.refresh(db_club)
    return db_club


async def get_club(club_id: int, session: AsyncSession):
    db_club = await session.scalar(
        select(Clubs).where(Clubs.id == club_id, Clubs.is_deleted == False)
    )
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
    is_pinned: bool | None = None,
    is_hidden: bool | None = None,
    interest_ids: list[int] | None = None,
):
    """Get all clubs with optional filters and followers count, including is_following status."""
    ClubUsersLinkPinned = aliased(ClubUsersLink)
    ClubUsersLinkHidden = aliased(ClubUsersLink)
    ClubUsersLinkFollowers = aliased(ClubUsersLink)
    ClubUsersLinkFollowing = aliased(ClubUsersLink)

    query = (
        select(
            Clubs,
            func.count(func.distinct(ClubUsersLinkFollowers.id)).label(
                "followers_count"
            ),
            func.bool_or(ClubUsersLinkFollowing.id.isnot(None)).label("is_following"),
        )
        .outerjoin(
            ClubUsersLinkFollowers,
            and_(
                ClubUsersLinkFollowers.club_id == Clubs.id,
                ClubUsersLinkFollowers.is_following == True,
                ClubUsersLinkFollowers.is_deleted == False,
            ),
        )
        .outerjoin(
            ClubUsersLinkFollowing,
            and_(
                ClubUsersLinkFollowing.club_id == Clubs.id,
                ClubUsersLinkFollowing.user_id == user_id,
                ClubUsersLinkFollowing.is_following == True,
                ClubUsersLinkFollowing.is_deleted == False,
            ),
        )
        .options(selectinload(Clubs.interests))
    )

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

    if is_pinned is not None and user_id:
        query = query.join(ClubUsersLinkPinned).filter(
            ClubUsersLinkPinned.user_id == user_id,
            ClubUsersLinkPinned.is_pinned == is_pinned,
            ClubUsersLinkPinned.is_deleted == False,
        )

    if is_hidden is not None and user_id:
        query = query.join(ClubUsersLinkHidden).filter(
            ClubUsersLinkHidden.user_id == user_id,
            ClubUsersLinkHidden.is_hidden == is_hidden,
            ClubUsersLinkHidden.is_deleted == False,
        )

    query = (
        query.group_by(Clubs.id)
        .order_by(Clubs.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    results = await session.execute(query)

    # Convert results to a list of dictionaries
    return [
        jsonable_encoder(club)
        | {"followers_count": followers_count, "is_following": is_following}
        for club, followers_count, is_following in results
    ]


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

    if not follow or not follow.is_following:
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
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id, Clubs.is_deleted == False)))
    if not club_exists:
        raise CustomHTTPException(status_code=404, message="Club not found")

    query = (
        select(Notes)
        .where(Notes.club_id == club_id, Notes.is_deleted == False)
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
        .options(
            joinedload(ClubUsersLink.user).options(
                joinedload(Users.profile).options(joinedload(UserProfiles.avatar))
            )
        )
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
            joinedload(Clubs.socials),
            joinedload(Clubs.org),
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
    club_dict = jsonable_encoder(club)
    club_dict["followers_count"] = followers_count
    club_dict["user_data"] = user_data
    club_dict["interests"] = [interest.__dict__ for interest in club.interests]
    return club_dict


async def update_club_logo(
    session: AsyncSession,
    club_id: int,
    logo: UploadFile,
) -> dict:
    """Update club logo."""
    club = await session.get(Clubs, club_id)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    if club.logo:
        club.logo.delete()

    content = io.BytesIO(await logo.read())
    club.logo = {
        "bytes": content,
        "filename": logo.filename,
    }
    await session.commit()
    return {"message": "Club logo updated successfully"}


async def create_or_update_club_socials(
    session: AsyncSession,
    club_id: int,
    socials: ClubSocialsCreate,
) -> ClubSocials:
    """Create club social links."""
    club = await session.get(Clubs, club_id)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    db_socials = await session.scalar(
        select(ClubSocials).where(
            ClubSocials.club_id == club_id,
            ClubSocials.is_deleted == False,
        )
    )
    if db_socials:
        db_socials.instagram = socials.instagram
        db_socials.linkedin = socials.linkedin
        db_socials.youtube = socials.youtube
        db_socials.website = socials.website
    else:
        db_socials = ClubSocials(
            club_id=club_id,
            instagram=socials.instagram,
            linkedin=socials.linkedin,
            youtube=socials.youtube,
            website=socials.website,
        )
        session.add(db_socials)
    await session.commit()
    await session.refresh(db_socials)
    return db_socials


async def get_club_events(
    session: AsyncSession, club_id: int, limit: int = 10, offset: int = 0
):
    """Get events of a club."""
    query = (
        select(Events)
        .where(Events.club_id == club_id, Events.is_deleted == False)
        .options(
            joinedload(Events.category),
        )
        .order_by(Events.event_datetime.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(query))
