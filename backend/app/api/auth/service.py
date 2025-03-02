from datetime import timedelta
import traceback
from sqlalchemy.ext.asyncio import AsyncSession
from google.oauth2 import id_token
from google.auth.transport import requests
from app.core.auth.jwt import create_access_token
from app.config import settings
from fastapi import HTTPException
from app.api.users.models import Users, UserTypes
from app.api.users.service import create_user
from sqlalchemy import select

from app.api.auth.schemas import AuthTokenData, Token
from app.config import settings
from app.response import CustomHTTPException

ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_DAYS = settings.REFRESH_TOKEN_EXPIRE_DAYS


async def google_signin(
    session: AsyncSession, id_token_str: str, platform: str = "web"
):
    try:
        client_id = {
            "web": settings.GOOGLE_WEB_CLIENT_ID,
            "android": settings.GOOGLE_ANDROID_CLIENT_ID,
            "ios": settings.GOOGLE_IOS_CLIENT_ID,
        }.get(platform)

        if not client_id:
            raise HTTPException(status_code=400, detail=f"Invalid platform: {platform}")

        idinfo = id_token.verify_oauth2_token(
            id_token_str, requests.Request(), client_id
        )

        email = idinfo["email"]
        name = idinfo.get("name", "")

        query = await session.execute(
            select(Users).where(
                Users.email == email, Users.user_type == UserTypes.app_user
            )
        )
        user = query.scalar_one_or_none()

        if not user:
            user = await create_user(
                session=session,
                full_name=name,
                email=email,
                phone=None,
                password=None,
                user_type=UserTypes.app_user,
                provider="google",
            )

        return user

    except ValueError as e:
        raise CustomHTTPException(
            status_code=401, message="Invalid authentication credentials"
        )
    except Exception as e:
        traceback.print_exc()
        raise CustomHTTPException(status_code=500, message="Internal Server Error")


async def create_access_refresh_tokens(user: Users) -> Token:
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    access_token_data = AuthTokenData(user_id=user.id, token_type="access_token")
    refresh_token_data = AuthTokenData(user_id=user.id, token_type="refresh_token")
    access_token = create_access_token(
        data=access_token_data.model_dump(), expires_delta=access_token_expires
    )
    refresh_token = create_access_token(
        data=refresh_token_data.model_dump(), expires_delta=refresh_token_expires
    )
    return Token(
        access_token=access_token, refresh_token=refresh_token, token_type="Bearer"
    )
