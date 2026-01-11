from datetime import timedelta
from typing import Annotated
from fastapi import APIRouter, Depends, Form
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import status
import jwt

from app.core.auth.jwt import create_access_token, decode_jwt_token
from app.core.auth.authentication import (
    authenticate_user,
    get_user,
    get_password_hash,
)
from app.core.auth.dependencies import DependsAuth, AdminAuth
from app.response import CustomHTTPException
from app.api.auth.schemas import (
    AuthTokenData,
    AuthUser,
    Token,
    GoogleSignInRequest,
    PasswordResetRequest,
    PasswordResetResponse,
)
from app.api.auth import service
from app.db.core import SessionDep
from app.config import settings

router = APIRouter(prefix="/auth")

ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


@router.post("/google", summary="Sign in with Google")
async def google_sign_in(
    request: GoogleSignInRequest,
    session: SessionDep = SessionDep,
) -> Token:
    user = await service.google_signin(session, request.id_token, request.platform)
    return await service.create_access_refresh_tokens(user)


@router.post("/token", summary="get access token")
async def login_for_access_token(
    session: SessionDep,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    user = await authenticate_user(session, form_data.username, form_data.password)
    if not user:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await service.create_access_refresh_tokens(user)


@router.post("/refresh", summary="refresh access token")
async def refresh_access_token(session: SessionDep, token: str = Form(...)) -> Token:
    try:
        payload = decode_jwt_token(token)
        payload = AuthTokenData(**payload)
        if payload.token_type != "refresh_token":
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="Invalid token type",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await get_user(session, payload.user_id)
        if not user:
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token_data = AuthTokenData(user_id=user.id, token_type="access_token")
        access_token = create_access_token(
            data=access_token_data.model_dump(), expires_delta=access_token_expires
        )
        return Token(
            access_token=access_token, refresh_token=token, token_type="Bearer"
        )
    except jwt.ExpiredSignatureError:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/me", response_model=AuthUser, summary="get current user info")
async def read_users_me(
    current_user: DependsAuth,
):
    return current_user


@router.post("/admin/reset-password", summary="Admin reset user password")
async def admin_reset_password(
    request: PasswordResetRequest,
    session: SessionDep,
    admin: AdminAuth,
) -> PasswordResetResponse:
    """
    Reset a user's password. Admin only.
    
    Can search by:
    - User email (contains @)
    - Username
    - Club name
    - Club slug
    - Club ID (numeric)
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from app.api.users.models import Users
    from app.api.clubs.models import Clubs
    
    user = None
    search_term = request.email_or_username.strip()
    
    # Try different search strategies
    if "@" in search_term:
        # Search by user email
        query = select(Users).where(Users.email == search_term)
        result = await session.execute(query)
        user = result.scalar_one_or_none()
    
    if not user and search_term.isdigit():
        # Search by club ID
        club_id = int(search_term)
        query = select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.id == club_id)
        result = await session.execute(query)
        user = result.scalar_one_or_none()
    
    if not user:
        # Search by username
        query = select(Users).where(Users.username == search_term)
        result = await session.execute(query)
        user = result.scalar_one_or_none()
    
    if not user:
        # Search by club slug
        query = select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.slug == search_term)
        result = await session.execute(query)
        user = result.scalar_one_or_none()
    
    if not user:
        # Search by club name (case-insensitive)
        query = select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.name.ilike(search_term))
        result = await session.execute(query)
        user = result.scalar_one_or_none()
    
    if not user:
        raise CustomHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            message=f"User or club '{search_term}' not found",
        )
    
    # Hash and set new password
    user.password = get_password_hash(request.new_password)
    await session.commit()
    
    return PasswordResetResponse(
        success=True,
        message="Password reset successfully",
        email=user.email,
        username=user.username,
    )

