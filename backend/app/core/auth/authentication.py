import bcrypt
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import func, select
from app.api.users.models import UserTypes, Users
from app.db.core import SessionDep, get_session
from app.api.clubs.models import Clubs
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

ALGORITHM = "HS256"


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def get_user(session: AsyncSession, username_or_id: str, *, password_holders: bool = False):
    query = select(Users).options(
        joinedload(Users.club), joinedload(Users.profile)
    ).where(Users.is_deleted == False)
    if password_holders:
        # Guests never sign in and have no password, and one address can
        # stand behind several of them: an arbitrary first row must not be a
        # guest standing in front of the account that does have one.
        query = query.where(Users.user_type != UserTypes.guest, Users.password.isnot(None))
    if isinstance(username_or_id, int):  # If searching by ID
        result = (await session.execute(query.where(Users.id == username_or_id))).scalars().first()
    elif "@" in username_or_id:
        # The address as stored first; failing that, ignoring case and stray
        # spaces ("Ravi@Gmail.com" for ravi@gmail.com) -- but only when that
        # finds one account, never a guess between two.
        result = (await session.execute(query.where(Users.email == username_or_id))).scalars().first()
        if result is None:
            loose = (await session.execute(
                query.where(func.lower(func.btrim(Users.email)) == username_or_id.strip().lower()).limit(2)
            )).unique().scalars().all()
            result = loose[0] if len(loose) == 1 else None
    else:  # If searching by username
        result = (await session.execute(query.where(Users.username == username_or_id))).scalars().first()

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
    user = await get_user(session, username, password_holders=True)
    if not user or not user.password:
        return False
    if not verify_password(password, user.password):
        return False
    return user
