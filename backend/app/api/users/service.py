from datetime import datetime, timezone
import io
import re
from fastapi import File, UploadFile
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.response import CustomHTTPException
from app.api.users.models import (
    UserAvatars,
    UserInterests,
    UserProfiles,
    UserTypes,
    Users,
)
from app.api.users.schemas import UserCreate
from app.core.auth.authentication import get_password_hash
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.orgs.models import Organizations
from app.core.validations.schema import validate_relations, validate_unique
from app.api.interests.models import Interests
from app.api.events.models import EventInterestsLink, EventRegistrationsLink, Events


async def generate_username(user: Users, session: AsyncSession, is_guest: bool = False):
    base_username = re.sub(
        r"[^a-zA-Z0-9_-]", "", user.full_name.lower().replace(" ", "_")
    )
    if is_guest:
        base_username = f"guest_{base_username}"

    if not base_username:
        base_username = "user"

    username = base_username
    count = 1

    while (
        await session.execute(select(exists().where(Users.username == username)))
    ).scalar():
        username = f"{base_username}_{count}"
        count += 1
    user.username = username
    return username


async def get_non_club_user_by_email(session: AsyncSession, email: str):
    query = (
        select(Users)
        .where(Users.email == email, Users.user_type != UserTypes.club)
        .order_by(Users.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def create_user(
    session: AsyncSession,
    full_name: str,
    email: str,
    phone: str | None = None,
    password: str | None = None,
    provider: str = "email",
    user_type: UserTypes = UserTypes.app_user,
):
    await validate_unique(
        session,
        unique_together=[
            {"email": (Users, email), "user_type": (Users, user_type)},
            {"phone": (Users, phone), "user_type": (Users, user_type)},
        ],
    )
    user_obj = Users(
        full_name=full_name,
        email=email,
        phone=phone,
        password=password,
        user_type=user_type,
        provider=provider,
    )
    if user_type != UserTypes.guest and provider == "email":
        if not password:
            raise CustomHTTPException(400, "Password is required")
        hashed_password = get_password_hash(user_obj.password)
        user_obj.password = hashed_password

    username = await generate_username(
        user_obj, session, is_guest=user_type == UserTypes.guest
    )
    user_obj.username = username

    session.add(user_obj)
    await session.commit()

    query = await session.execute(
        select(Users)
        .where(Users.username == username)
        .options(selectinload(Users.profile))
    )
    return query.scalar()


async def following_clubs(
    session: AsyncSession, user_id: int, limit: int = 10, offset: int = 0
):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    query = (
        select(Clubs)
        .join(ClubUsersLink, ClubUsersLink.club_id == Clubs.id)
        .where(ClubUsersLink.user_id == user_id, ClubUsersLink.is_following == True)
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(query))


async def create_user_profile(
    session: AsyncSession,
    user_id: int,
    full_name: str,
    whatsapp: str | None = None,
    org_id: str | None = None,
):
    await validate_relations(
        session,
        {
            "user_id": (Users, user_id),
            "org_id": (Organizations, org_id),
        },
    )

    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if profile:
        raise CustomHTTPException(400, "Profile already exists")
    else:
        await validate_unique(
            session,
            unique_together=[{"whatsapp": (UserProfiles, whatsapp)}],
        )
        profile = UserProfiles(
            user_id=user_id, org_id=org_id, whatsapp=whatsapp, full_name=full_name
        )
        session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def update_user_profile(
    session: AsyncSession,
    user_id: int,
    full_name: str,
    whatsapp: str | None = None,
    org_id: str | None = None,
):
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if not profile:
        raise CustomHTTPException(400, "Profile not found.")

    await validate_relations(
        session,
        {
            "user_id": (Users, user_id),
            "org_id": (Organizations, org_id),
        },
    )

    if profile.whatsapp != whatsapp:
        await validate_unique(
            session,
            unique_together=[{"whatsapp": (UserProfiles, whatsapp)}],
        )
    profile.whatsapp = whatsapp
    profile.org_id = org_id
    profile.full_name = full_name

    await session.commit()
    await session.refresh(profile)
    return profile


async def list_interests(session: AsyncSession, user_id: int):
    query = (
        select(Interests)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(UserInterests.user_id == user_id)
        .options(joinedload(Interests.category))
    )
    return await session.scalars(query)


async def select_interests(
    session: AsyncSession, user_id: int, interest_ids: list[int]
):
    delete_exisisting = delete(UserInterests).where(UserInterests.user_id == user_id)
    await session.execute(delete_exisisting)
    for id in interest_ids:
        if await session.scalar(select(exists().where(Interests.id == id))):
            link = UserInterests(user_id=user_id, interest_id=id)
            session.add(link)
    await session.commit()
    return None


async def select_avatar(session: AsyncSession, user_id: int, avatar_id: int | None):
    await validate_relations(
        session, {"user_id": (Users, user_id), "avatar_id": (UserAvatars, avatar_id)}
    )
    user_profile = await session.scalar(
        select(UserProfiles)
        .where(UserProfiles.user_id == user_id, UserProfiles.is_deleted == False)
        .options(
            joinedload(UserProfiles.avatar),
        )
    )
    if not user_profile:
        raise CustomHTTPException(400, "user profile does not exists")

    user_profile.avatar_id = avatar_id
    await session.commit()
    await session.refresh(user_profile)
    return user_profile.avatar


async def get_user_profile(session: AsyncSession, user_id: int):
    await validate_relations(session, {"user_id": (Users, user_id)})

    query = (
        select(Users)
        .where(Users.id == user_id)
        .options(
            joinedload(Users.profile).options(
                joinedload(UserProfiles.avatar), joinedload(UserProfiles.org)
            ),
            selectinload(Users.interests),
        )
    )

    return await session.scalar(query)


async def update_profile_picture(
    session: AsyncSession,
    user_id: int,
    profile_picture: UploadFile,
) -> dict:
    """Update user's profile picture."""
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if not profile:
        profile = UserProfiles(user_id=user_id)
        session.add(profile)

    if profile.profile_pic:
        profile.profile_pic.delete()

    content = io.BytesIO(await profile_picture.read())
    profile.profile_pic = {
        "bytes": content,
        "filename": profile_picture.filename,
    }
    await session.commit()
    await session.refresh(profile)
    return profile.profile_pic


async def create_user_avatar(session: AsyncSession, name: str, file: UploadFile):
    avatar = UserAvatars(name=name)
    avatar.image = {"bytes": io.BytesIO(await file.read()), "filename": file.filename}
    session.add(avatar)
    await session.commit()
    await session.refresh(avatar)
    return avatar


async def list_avatars(session: AsyncSession):
    query = select(UserAvatars)
    return await session.scalars(query)


async def delete_user(session: AsyncSession, user_id: int):
    await validate_relations(session, {"user_id": (Users, user_id)})
    user = await session.scalar(select(Users).where(Users.id == user_id))
    user.soft_delete()
    # delete user registrations
    follows = select(ClubUsersLink).where(ClubUsersLink.user_id == user_id)
    for follow in await session.scalars(follows):
        follow.soft_delete()
    registrations = select(EventRegistrationsLink).where(
        EventRegistrationsLink.user_id == user_id
    )
    for registration in await session.scalars(registrations):
        registration.soft_delete()
    interests = select(UserInterests).where(UserInterests.user_id == user_id)
    for interest in await session.scalars(interests):
        interest.soft_delete()
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if profile:
        profile.soft_delete()
    await session.commit()
    return None


async def list_registered_events(
    session: AsyncSession,
    user_id: int,
    is_attended: bool | None = None,
    is_paid: bool | None = None,
    is_won: bool | None = None,
    limit: int = 10,
    offset: int = 0,
):
    query = (
        select(EventRegistrationsLink)
        .where(EventRegistrationsLink.user_id == user_id)
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            )
        )
        .limit(limit)
        .offset(offset)
    )
    if is_paid is not None:
        query = query.where(EventRegistrationsLink.is_paid == is_paid)
    if is_attended is not None:
        query = query.where(EventRegistrationsLink.is_attended == is_attended)
    if is_won is not None:
        query = query.where(EventRegistrationsLink.is_won == is_won)
    return (await session.scalars(query)).all()
