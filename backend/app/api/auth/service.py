from datetime import timedelta
import logging
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
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

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
                Users.email == email, 
                Users.user_type == UserTypes.app_user,
                Users.is_deleted == False,
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
        logger.exception("Google signin error")
        raise CustomHTTPException(status_code=500, message="Internal Server Error")


async def apple_signin(
    session: AsyncSession,
    identity_token: str,
    authorization_code: str,
    user_name: str | None = None,
    user_email: str | None = None,
):
    """
    Verify Apple Sign In identity token and create/get user.
    
    Apple's identity token is a JWT signed by Apple that contains:
    - sub: Apple's unique user ID (stable across sign-ins)
    - email: User's email (may be private relay)
    - email_verified: Whether email is verified
    """
    import jwt
    from jwt import PyJWKClient
    
    try:
        # Apple's public keys endpoint
        jwks_client = PyJWKClient("https://appleid.apple.com/auth/keys")
        
        # Decode without verification first to get header
        unverified_header = jwt.get_unverified_header(identity_token)
        signing_key = jwks_client.get_signing_key_from_jwt(identity_token)
        
        # Verify the token
        # audience can be your app's bundle ID or service ID
        decoded = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.APPLE_CLIENT_ID if settings.APPLE_CLIENT_ID else None,
            issuer="https://appleid.apple.com",
            options={"verify_aud": bool(settings.APPLE_CLIENT_ID)}
        )
        
        # Extract user info from token
        apple_user_id = decoded.get("sub")  # Apple's unique user ID
        email = decoded.get("email") or user_email
        
        if not email:
            # Use Apple user ID to create a placeholder email
            email = f"{apple_user_id}@privaterelay.appleid.com"
        
        # Check if user exists by Apple ID or email
        query = await session.execute(
            select(Users).where(
                Users.email == email,
                Users.user_type == UserTypes.app_user,
                Users.is_deleted == False,
            )
        )
        user = query.scalar_one_or_none()
        
        if not user:
            # Create new user
            user = await create_user(
                session=session,
                full_name=user_name or "Apple User",
                email=email,
                phone=None,
                password=None,
                user_type=UserTypes.app_user,
                provider="apple",
            )
        
        return user
        
    except jwt.ExpiredSignatureError:
        raise CustomHTTPException(
            status_code=401, message="Apple token has expired"
        )
    except jwt.InvalidTokenError as e:
        logger.exception(f"Apple token validation error: {e}")
        raise CustomHTTPException(
            status_code=401, message="Invalid Apple authentication token"
        )
    except CustomHTTPException:
        raise
    except Exception as e:
        logger.exception(f"Apple signin error: {e}")
        # Check if it's a database constraint error (duplicate email)
        error_msg = str(e).lower()
        if "unique" in error_msg or "duplicate" in error_msg:
            raise CustomHTTPException(
                status_code=409, 
                message="An account with this email already exists. Please sign in with your original method."
            )
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

