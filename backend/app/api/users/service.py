import re
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.users.models import UserAvatars, UserProfiles, UserTypes, Users
from app.api.users.schemas import UserCreate
from app.core.auth.authentication import get_password_hash
from app.api.clubs.models import ClubFollowersLink, Clubs
from app.api.orgs.models import Organizations
from app.core.validations.schema import validate_relations, validate_unique


async def generate_username(user: Users, session: AsyncSession):
    base_username = re.sub(
        r"[^a-zA-Z0-9_-]", "", user.full_name.lower().replace(" ", "_")
    )

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


async def create_user(
    session: AsyncSession,
    full_name: str,
    email: str,
    phone: str,
    password: str,
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
    )

    hashed_password = get_password_hash(user_obj.password)
    user_obj.password = hashed_password

    username = await generate_username(user_obj, session)
    user_obj.username = username

    session.add(user_obj)
    await session.commit()

    query = await session.execute(
        select(Users)
        .where(Users.username == username)
        .options(selectinload(Users.user_profiles))
    )
    return query.scalar()


async def following_clubs(session: AsyncSession, user_id: int):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    query = (
        select(Clubs)
        .join(ClubFollowersLink, ClubFollowersLink.club_id == Clubs.id)
        .where(ClubFollowersLink.user_id == user_id)
    )
    return query


async def create_or_update_profile(
    session: AsyncSession,
    user_id: int,
    whatsapp: str | None = None,
    org_id: str | None = None,
    avatar_id: str | None = None,
):
    await validate_relations(
        session,
        {
            "user_id": (Users, user_id),
            "org_id": (Organizations, org_id),
            "avatar_id": (UserAvatars, avatar_id),
        },
    )
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if profile:
        profile.whatsapp = whatsapp
        profile.org_id = org_id
        profile.avatar_id = avatar_id
    else:
        profile = UserProfiles(
            user_id=user_id, org_id=org_id, whatsapp=whatsapp, avatar_id=avatar_id
        )
        session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile
