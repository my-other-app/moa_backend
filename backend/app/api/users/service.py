import re
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.response import CustomHTTPException
from app.api.users.models import Users
from app.api.users.schemas import UserCreate
from app.core.auth.authentication import get_password_hash
from app.api.clubs.models import ClubFollowersLink, Clubs


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


async def validate_user(user: Users, session: AsyncSession):
    unique_fields = ["email", "phone", "whatsapp"]
    validation_errors = {}
    for field in unique_fields:
        field_value = getattr(user, field)
        if not field_value:
            continue
        query = select(exists().where(getattr(Users, field) == field_value))
        result = await session.execute(query)
        if result.scalar():
            validation_errors[field] = (
                f"{field} already associated with another account."
            )
    return validation_errors


async def create_user(user: UserCreate, session: AsyncSession):
    user_obj = Users(**user.model_dump())
    hashed_password = get_password_hash(user_obj.password)
    user_obj.password = hashed_password
    if validation_errors := await validate_user(user_obj, session):
        raise CustomHTTPException(
            status_code=400,
            message="Unable to create user.",
            errors=validation_errors,
        )
    username = await generate_username(user_obj, session)
    print(user_obj)
    user_obj.username = username
    session.add(user_obj)
    await session.commit()
    query = await session.execute(select(Users).where(Users.username == username))
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
