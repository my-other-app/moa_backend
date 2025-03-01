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
)
from app.core.auth.dependencies import DependsAuth
from app.response import CustomHTTPException
from app.api.auth.schemas import (
    AuthTokenData,
    AuthUser,
    Token,
    GoogleSignInRequest,
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
