from datetime import timedelta
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from google.oauth2 import id_token
from google.auth.transport import requests
from app.core.auth.jwt import create_access_token
from app.config import settings
from fastapi import HTTPException
from app.api.users.models import Users, UserTypes
from app.api.users.service import create_user, merge_guest_into_user
from app.api.auth.verification import claim_for_provider
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

        if not idinfo.get("email_verified"):
            raise CustomHTTPException(
                status_code=401, message="Google account email is not verified"
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
        else:
            # Google checked the address: an unverified password account
            # under it is taken back from whoever set the password.
            await claim_for_provider(session, user, method="google")

        # Merge any guest accounts with matching email
        await merge_guest_into_user(session, app_user=user)
        await session.commit()
        # commit() expires user's attributes; refresh explicitly (a proper
        # awaited ORM op) so the caller's bare attribute access
        # (create_access_refresh_tokens reading user.id/token_version)
        # doesn't trigger an implicit lazy-load, which can hit a
        # pool_pre_ping/greenlet interaction bug on an async engine.
        await session.refresh(user)

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
    
    logger.info(f"Apple signin attempt - user_email: {user_email}, user_name: {user_name}")
    logger.info(f"APPLE_CLIENT_ID configured: {settings.APPLE_CLIENT_ID}")
    
    try:
        # Apple's public keys endpoint
        logger.info("Fetching Apple JWKS keys...")
        jwks_client = PyJWKClient("https://appleid.apple.com/auth/keys")
        
        # Decode without verification first to get header
        logger.info("Getting signing key from JWT...")
        unverified_header = jwt.get_unverified_header(identity_token)
        signing_key = jwks_client.get_signing_key_from_jwt(identity_token)
        
        # Verify the token
        # audience can be your app's bundle ID or service ID
        logger.info(f"Verifying token with audience: {settings.APPLE_CLIENT_ID}")
        decoded = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.APPLE_CLIENT_ID if settings.APPLE_CLIENT_ID else None,
            issuer="https://appleid.apple.com",
            options={"verify_aud": bool(settings.APPLE_CLIENT_ID)}
        )
        logger.info(f"Token verified successfully. sub: {decoded.get('sub')}, email: {decoded.get('email')}")
        
        # Extract user info from token
        apple_user_id = decoded.get("sub")  # Apple's unique user ID
        # Only the address Apple signed. The one the client sends beside the
        # token is signed by nobody: falling back to it let a token with no
        # address claim sign in to whichever account had the address typed
        # next to it, and guests are now folded into Apple accounts.
        email = decoded.get("email")
        
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
        else:
            # Apple signed the address: see the matching call in google_signin.
            await claim_for_provider(session, user, method="apple")

        # Merge any guest accounts with matching email
        await merge_guest_into_user(session, app_user=user)
        await session.commit()
        # See the matching comment in google_signin: refresh explicitly
        # after commit to avoid a lazy-load hitting the async
        # pool_pre_ping/greenlet MissingGreenlet bug.
        await session.refresh(user)

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


async def create_access_refresh_tokens(
    user: Users, session=None, request=None, sid: str | None = None, member_id: int | None = None
) -> Token:
    """
    `session` and `request` are optional so every existing caller keeps
    working: without them no session row is recorded and the tokens simply
    carry no sid, which the refresh path treats as live.

    `member_id` makes it a club team member's session: `user` is the club's
    own account, and the member rides along in both tokens and the session row.
    """
    if sid is None and session is not None:
        from app.api.auth import sessions

        sid = str(await sessions.start(session, user.id, request=request, member_id=member_id))

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    access_token_data = AuthTokenData(
        user_id=user.id,
        token_type="access_token",
        token_version=user.token_version,
        sid=sid,
        member_id=member_id,
    )
    refresh_token_data = AuthTokenData(
        user_id=user.id,
        token_type="refresh_token",
        token_version=user.token_version,
        sid=sid,
        member_id=member_id,
    )
    access_token = create_access_token(
        data=access_token_data.model_dump(), expires_delta=access_token_expires
    )
    refresh_token = create_access_token(
        data=refresh_token_data.model_dump(), expires_delta=refresh_token_expires
    )
    return Token(
        access_token=access_token, refresh_token=refresh_token, token_type="Bearer"
    )

