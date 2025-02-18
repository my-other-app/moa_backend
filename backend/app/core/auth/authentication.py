from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from app.api.users.models import Users
from app.db.core import get_session

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 365


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


async def get_user(username_or_id: str):
    async for session in get_session():
        query = select(Users)
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


async def authenticate_user(username: str, password: str):
    user = await get_user(username)
    if not user:
        return False
    if not verify_password(password, user.password):
        return False
    return user
