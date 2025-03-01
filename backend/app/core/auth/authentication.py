from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from app.api.users.models import UserTypes, Users
from app.db.core import SessionDep, get_session
from app.api.clubs.models import Clubs
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

ALGORITHM = "HS256"


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


async def get_user(session: AsyncSession, username_or_id: str):
    async for session in get_session():
        query = select(Users).options(joinedload(Users.club), joinedload(Users.profile))
        if isinstance(username_or_id, int):  # If searching by ID
            query = query.where(Users.id == username_or_id)
        else:
            if "@" in username_or_id:  # If searching by emai
                query = query.where(Users.email == username_or_id)
            else:  # If searching by username
                query = query.where(Users.username == username_or_id)
        result = await session.execute(query)
        result = result.scalars().first()

        if result and result.user_type == UserTypes.club:
            if not result.club or not result.club.is_verified:
                return None
        return result


async def get_volunteer(username_or_id: str):
    async for session in get_session():
        query = select(Users).options(joinedload(Users.profile))
        if isinstance(username_or_id, int):  # If searching by ID
            query = query.where(Users.id == username_or_id)
        else:
            if "@" in username_or_id:  # If searching by emai
                query = query.where(Users.email == username_or_id)
            else:  # If searching by username
                query = query.where(Users.username == username_or_id)
        result = await session.execute(query)
        result = result.scalars().first()
        return result


async def authenticate_user(session: AsyncSession, username: str, password: str):
    user = await get_user(session, username)
    if not user:
        return False
    if not verify_password(password, user.password):
        return False
    return user
