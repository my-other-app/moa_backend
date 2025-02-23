from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from google.oauth2 import id_token
from google.auth.transport import requests
from app.core.auth.jwt import create_access_token
from app.config import settings
from fastapi import HTTPException
from app.api.users.models import Users, UserTypes
from app.api.users.service import create_user
from sqlalchemy import select

from app.api.auth.schemas import AuthTokenData
from app.core.auth.authentication import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from app.response import CustomHTTPException


async def google_signin(
    session: AsyncSession, id_token_str: str, platform: str = "web"
) -> dict:
    try:
        client_id = {
            "web": settings.GOOGLE_WEB_CLIENT_ID,
            "android": settings.GOOGLE_ANDROID_CLIENT_ID,
            "ios": settings.GOOGLE_IOS_CLIENT_ID,
        }.get(platform)

        if not client_id:
            raise HTTPException(status_code=400, detail=f"Invalid platform: {platform}")

        # Verify the ID token
        idinfo = id_token.verify_oauth2_token(
            id_token_str, requests.Request(), client_id
        )

        # Get user info from token
        email = idinfo["email"]
        name = idinfo.get("name", "")

        # Check if user exists
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

        access_token_data = AuthTokenData(user_id=user.id, token_type="access_token")
        refresh_token_data = AuthTokenData(user_id=user.id, token_type="refresh_token")

        access_token = create_access_token(
            data=access_token_data.model_dump(),
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )

        refresh_token = create_access_token(
            data=refresh_token_data.model_dump(),
            expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        )

        return access_token, refresh_token

    except ValueError as e:
        raise CustomHTTPException(
            status_code=401, detail="Invalid authentication credentials"
        )
    except Exception as e:
        raise CustomHTTPException(status_code=500, detail="Internal Server Error")
