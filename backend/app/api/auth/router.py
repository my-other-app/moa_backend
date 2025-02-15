from datetime import timedelta
from typing import Annotated
from fastapi import APIRouter, Depends, Form
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import status
import jwt

from app.core.auth.jwt import create_access_token, decode_jwt_token
from app.core.auth.authentication import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
    authenticate_user,
    get_user,
)
from app.core.auth.dependencies import DependsAuth
from app.response import CustomHTTPException
from app.api.users.schemas import User, UserPublic
from app.api.users.models import Users
from app.api.auth.schemas import AuthTokenData, Token

router = APIRouter(prefix="/auth")


@router.post("/token", summary="get access token")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
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


@router.post("/refresh", summary="refresh access token")
async def refresh_access_token(
    token: str = Form(...),
) -> Token:
    try:
        payload = decode_jwt_token(token)
        payload = AuthTokenData(**payload)
        if payload.token_type != "refresh_token":
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="Invalid token type",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await get_user(payload.user_id)
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


@router.get("/me", response_model=User, summary="get current user info")
async def read_users_me(
    current_user: DependsAuth,
):
    return current_user
