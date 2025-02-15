from typing import Annotated
import jwt
from fastapi import Depends, status
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

from app.core.auth.authentication import get_user, oauth2_scheme, ALGORITHM
from app.api.users.models import Users
from app.response import CustomHTTPException
from app.config import settings
from app.api.auth.schemas import AuthTokenData
from app.core.auth.jwt import decode_jwt_token


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    credentials_exception = CustomHTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        message="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_jwt_token(token)
        token_data = AuthTokenData(**payload)
        if token_data.token_type != "access_token":
            raise credentials_exception
        user_id: str = token_data.user_id
        if not user_id:
            raise credentials_exception
    except ExpiredSignatureError:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="Token has expired",
            error_code="TOKEN_EXPIRED",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except InvalidTokenError:
        raise credentials_exception
    user = await get_user(user_id)
    if user is None:
        raise credentials_exception
    return user


DependsAuth = Annotated[Users, Depends(get_current_user)]
