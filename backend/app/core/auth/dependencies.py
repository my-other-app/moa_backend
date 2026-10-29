from typing import Annotated, List, Optional, Union
import jwt
from fastapi import Depends, Request, status
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.authentication import get_user, oauth2_scheme, ALGORITHM
from app.api.users.models import Users
from app.response import CustomHTTPException
from app.api.auth.schemas import AuthTokenData
from app.core.auth.jwt import decode_jwt_token
from app.db.core import SessionDep
from app.api.events.volunteer.models import Volunteer


async def get_current_user(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    session: SessionDep = SessionDep(),
):
    # Each request decides co-host access afresh (app/api/cohosts/access.py).
    from app.api.cohosts import access as cohost_access

    cohost_access.reset()
    # Fall back to the access-token cookie when no Authorization header is
    # present (the cookie-auth web frontends never send one).
    token = token or request.cookies.get("access_token")
    if not token:
        return None
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
    except jwt.InvalidSignatureError:
        raise credentials_exception
    except Exception:
        raise credentials_exception
    # An unapproved club's account is found, so the role check can answer
    # 403 CLUB_NOT_VERIFIED; it was a 401, which every client reads as a dead
    # session and answers by signing out.
    user = await get_user(session, user_id, allow_unverified_club=True)
    if user is None:
        raise credentials_exception
    if user.token_version != token_data.token_version:
        raise credentials_exception
    # An admin acting as someone else: only while the admin still exists, is
    # active and may impersonate. (Such a session also has a hard end on its
    # row, checked below, and no refresh token.)
    if token_data.impersonated_by:
        await check_impersonator(session, token_data.impersonated_by)
    # A session ended from the Security page dies now, not at its next refresh.
    if token_data.sid:
        from app.api.auth import sessions as session_store

        row = await session_store.get_live(session, token_data.sid)
        if row is None:
            raise credentials_exception
        await session_store.touch_throttled(session, row)
    # A club team member's session acts as the club; on every request the
    # member must still have access and their role must allow this request.
    if token_data.member_id:
        from app.api.club_access import service as club_access

        await club_access.check_member(session, request, user, token_data.member_id)
    # A club co-hosting the event this path names acts on it as the host's
    # team does, but for the host's own decisions (app/api/cohosts/access.py).
    await cohost_access.gate(session, request, user)
    # Plan gates let admins through, and an admin acting as a club with them.
    from app.api.subscriptions import gates

    gates.note_actor(user, token_data.impersonated_by)
    # The account a valid token named, for the limits keyed by account
    # (app.core.rate_limit.signed_in_key): read there, not decoded again.
    # Set only here, once every check above has passed.
    request.state.user_id = user.id
    return user


async def check_impersonator(session, admin_id: int):
    """The admin behind an impersonated session, still allowed to be: a live,
    active admin whose role may impersonate. 401 otherwise -- demoting or
    suspending the admin ends every session they are holding as someone
    else, at its next request."""
    from app.api.users.models import UserTypes
    from app.core.auth.permissions import IMPERSONATE, can

    admin = await session.scalar(select(Users).where(Users.id == admin_id, Users.is_deleted == False))  # noqa: E712
    if (
        admin is None
        or getattr(admin.user_type, "value", admin.user_type) != UserTypes.admin.value
        or getattr(admin, "is_active", True) is False
        or not can(admin, IMPERSONATE)
    ):
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="The admin session behind this one has ended",
            error_code="IMPERSONATION_ENDED",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return admin


async def get_current_user_if_valid(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    session: SessionDep = SessionDep(),
) -> Optional[Users]:
    """The signed-in user, or nobody when the token presented is dead.

    Public pages take an optional identity: a visitor whose stored token has
    expired, or was revoked from the Security page, is a visitor still, not a
    refused request. The event site kept a token after a registration and every
    public page went blank once it aged out, because the 401 from here was
    surfacing on endpoints that never needed a sign-in in the first place.
    Suspension and role checks still run on whoever this returns.
    """
    try:
        return await get_current_user(request, token, session)
    except CustomHTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


def check_user_type(required_roles: Union[str, List[str]], optional=False):
    """
    Creates a dependency that checks if the current user has the required role(s).

    Args:
        required_roles: Single role string or list of role strings that are allowed

    Returns:
        Dependency function that validates user roles
    """
    if isinstance(required_roles, str):
        required_roles = [required_roles]

    identify = get_current_user_if_valid if optional else get_current_user

    async def role_checker(
        request: Request,
        current_user: Annotated[Optional[Users], Depends(identify)],
        session: SessionDep,
    ) -> Users:
        if optional:
            if not current_user:
                return None
        else:
            if not current_user:
                raise CustomHTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    message="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )
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
        # A suspension has to bite on every request, not only at sign-in:
        # an already-issued token would otherwise keep working for its full
        # lifetime and the suspension would do nothing until it expired.
        if getattr(current_user, "is_active", True) is False:
            raise CustomHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                message="This account has been suspended",
                error_code="ACCOUNT_SUSPENDED",
            )

        # A club's own routes (ClubAuth, VolunteerAuth) admit an admin too, and
        # behind them an admin may act on any club's records -- so the admin's
        # role must allow what the request does: reading needs VIEW, changing
        # needs MANAGE_CONTENT, deleting needs DESTROY. Without this a viewer
        # could edit or delete any event through the club's own routes.
        if user_type == "admin" and "club" in required_roles and "app_user" not in required_roles:
            from app.core.auth.permissions import capability_for_method, ensure_can

            ensure_can(current_user, capability_for_method(request.method))

        if user_type == "club":
            if current_user.club is None or not current_user.club.is_verified:
                if optional:
                    # A public page: an unapproved club reads it as a visitor.
                    return None
                raise CustomHTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    message="Club is not verified",
                    error_code="CLUB_NOT_VERIFIED",
                )

        # Admins must use two-factor (app.api.admin_ops.two_factor): every
        # route only an admin (or a club) may call refuses one without it
        # once their grace is over. The routes any account may call -- set up
        # two-factor, sign out, /auth/me -- stay open, so they can.
        if user_type == "admin" and "app_user" not in required_roles and not getattr(current_user, "totp_enabled", False):
            from app.api.admin_ops import two_factor

            await two_factor.enforce(session, current_user)

        return current_user

    return role_checker


DependsAuth = Annotated[
    Users, Depends(check_user_type(["guest", "app_user", "club", "admin"]))
]
UserAuth = Annotated[Users, Depends(check_user_type(["app_user", "admin"]))]
GuestAuth = Annotated[Users, Depends(check_user_type(["guest", "app_user", "admin"]))]
ClubAuth = Annotated[Users, Depends(check_user_type(["club", "admin"]))]
AdminAuth = Annotated[Users, Depends(check_user_type(["admin"]))]

VolunteerAuth = Annotated[
    Users, Depends(check_user_type(["volunteer", "club", "admin"]))
]  # TODO: modify


OptionalUserAuth = Annotated[
    Optional[Users],
    Depends(check_user_type(["guest", "app_user", "admin"], optional=True)),
]

OptionalAuth = Annotated[
    Optional[Users],
    Depends(check_user_type(["guest", "app_user", "club", "admin"], optional=True)),
]
