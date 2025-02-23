from typing import Annotated, List, Union
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


def check_user_type(required_roles: Union[str, List[str]]):
    """
    Creates a dependency that checks if the current user has the required role(s).

    Args:
        required_roles: Single role string or list of role strings that are allowed

    Returns:
        Dependency function that validates user roles
    """
    if isinstance(required_roles, str):
        required_roles = [required_roles]

    async def role_checker(
        current_user: Annotated[Users, Depends(get_current_user)]
    ) -> Users:
        if not hasattr(current_user, "user_type"):
            raise CustomHTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="User model has no type attribute",
            )

        user_type = current_user.user_type.value
        if not any(role == user_type for role in required_roles):
            raise CustomHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                message="Not Authorized",
                error_code="INSUFFICIENT_PERMISSIONS",
            )
        if user_type == "club":
            if not current_user.club.is_verified:
                raise CustomHTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    message="Club is not verified",
                    error_code="CLUB_NOT_VERIFIED",
                )

        return current_user

    return role_checker


DependsAuth = Annotated[Users, Depends(check_user_type(["app_user", "club", "admin"]))]
UserAuth = Annotated[Users, Depends(check_user_type(["app_user", "admin"]))]
ClubAuth = Annotated[Users, Depends(check_user_type(["club", "admin"]))]
AdminAuth = Annotated[Users, Depends(check_user_type(["admin"]))]
