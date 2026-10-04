from datetime import timedelta
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import status
import jwt

from app.core.rate_limit import limiter
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
    AppleSignInRequest,
    PasswordResetRequest,
    PasswordResetResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    ImpersonationRequest,
    ImpersonationResponse,
)
from app.api.audit import service as audit
from app.api.auth import service
from app.db.core import SessionDep
from app.config import settings

router = APIRouter(prefix="/auth")

ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

ACCESS_TOKEN_COOKIE_NAME = "access_token"
REFRESH_TOKEN_COOKIE_NAME = "refresh_token"


def _set_auth_cookies(
    request: Request, response: Response, access_token: str, refresh_token: str
):
    """Set the tokens as httpOnly cookies, in addition to the JSON response
    body. This is additive: the mobile app and other web frontends keep
    reading the JSON body and simply ignore these cookies. Host-only (no
    explicit Domain) since every frontend already calls this same API host.

    The `secure` flag is derived from the actual request scheme rather than
    settings.DEBUG: a Secure cookie is silently dropped by browsers/HTTP
    clients over plain HTTP, and DEBUG isn't a reliable proxy for "is this
    request over HTTPS" (e.g. a non-debug local/staging setup still running
    over http://localhost)."""
    cookie_kwargs = dict(
        httponly=True, secure=request.url.scheme == "https", samesite="lax"
    )
    response.set_cookie(
        ACCESS_TOKEN_COOKIE_NAME,
        access_token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **cookie_kwargs,
    )
    response.set_cookie(
        REFRESH_TOKEN_COOKIE_NAME,
        refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **cookie_kwargs,
    )


def _clear_auth_cookies(response: Response):
    response.delete_cookie(ACCESS_TOKEN_COOKIE_NAME)
    response.delete_cookie(REFRESH_TOKEN_COOKIE_NAME)


@router.post("/google", summary="Sign in with Google")
async def google_sign_in(
    request: GoogleSignInRequest,
    session: SessionDep = SessionDep,
) -> Token:
    user = await service.google_signin(session, request.id_token, request.platform)
    return await service.create_access_refresh_tokens(user)


@router.post("/apple", summary="Sign in with Apple")
async def apple_sign_in(
    request: AppleSignInRequest,
    session: SessionDep = SessionDep,
) -> Token:
    """
    Sign in with Apple.
    
    Receives the identity_token and authorization_code from the Apple Sign In flow.
    On first sign in, user_name and user_email may be provided by Apple.
    """
    user = await service.apple_signin(
        session=session,
        identity_token=request.identity_token,
        authorization_code=request.authorization_code,
        user_name=request.user_name,
        user_email=request.user_email,
    )
    return await service.create_access_refresh_tokens(user)


@router.post("/token", summary="get access token")
@limiter.limit("5/minute")
async def login_for_access_token(
    request: Request,
    response: Response,
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
    # Merge any guest accounts with matching email into this user
    from app.api.users.service import merge_guest_into_user
    await merge_guest_into_user(session, app_user=user)
    await session.commit()
    # commit() expires user's attributes; refresh explicitly here (a proper
    # awaited ORM op) rather than letting create_access_refresh_tokens's bare
    # attribute access trigger an implicit lazy-load, which can hit a
    # pool_pre_ping/greenlet interaction bug on an async engine (MissingGreenlet).
    await session.refresh(user)
    token = await service.create_access_refresh_tokens(user)
    _set_auth_cookies(request, response, token.access_token, token.refresh_token)
    return token


@router.post("/refresh", summary="refresh access token")
@limiter.limit("20/minute")
async def refresh_access_token(
    request: Request,
    response: Response,
    session: SessionDep,
    token: Optional[str] = Form(None),
) -> Token:
    # Fall back to the refresh-token cookie when no form field is sent (the
    # cookie-auth path — see _set_auth_cookies).
    token = token or request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    if not token:
        raise CustomHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            message="No refresh token provided",
            headers={"WWW-Authenticate": "Bearer"},
        )
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
        if payload.token_version != user.token_version:
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="Token has been revoked, please log in again",
                headers={"WWW-Authenticate": "Bearer"},
            )
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token_data = AuthTokenData(
            user_id=user.id,
            token_type="access_token",
            token_version=user.token_version,
        )
        access_token = create_access_token(
            data=access_token_data.model_dump(), expires_delta=access_token_expires
        )
        _set_auth_cookies(request, response, access_token, token)
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


@router.post("/logout", summary="clear auth cookies")
async def logout(response: Response):
    """Clears the httpOnly auth cookies set by /auth/token and /auth/refresh.
    Needed since client-side JS can't clear httpOnly cookies itself (used by
    the cookie-auth web frontends; a no-op for clients that never received
    these cookies in the first place)."""
    _clear_auth_cookies(response)
    return {"success": True}


@router.get("/me", response_model=AuthUser, summary="get current user info")
async def read_users_me(
    current_user: DependsAuth,
):
    return current_user


@router.post("/change-password", summary="Change user password")
async def change_password(
    request: ChangePasswordRequest,
    session: SessionDep,
    current_user: DependsAuth,
) -> ChangePasswordResponse:
    from app.core.auth.authentication import verify_password
    
    # Verify old password
    if not current_user.password or not verify_password(request.old_password, current_user.password):
        raise CustomHTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            message="Incorrect old password",
        )
        
    from sqlalchemy import select
    from app.api.users.models import Users
    
    # Re-fetch user on the current active session to avoid detached instance issues
    stmt = select(Users).where(Users.id == current_user.id)
    result = await session.execute(stmt)
    db_user = result.scalar_one()

    # Hash and set new password, and bump token_version to invalidate any
    # access/refresh tokens issued before this change.
    db_user.password = get_password_hash(request.new_password)
    db_user.token_version += 1
    await session.commit()

    return ChangePasswordResponse(
        success=True,
        message="Password changed successfully"
    )


@router.post("/admin/reset-password", summary="Admin reset user password")
@limiter.limit("5/minute")
async def admin_reset_password(
    request: Request,
    payload: PasswordResetRequest,
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
    search_term = payload.email_or_username.strip()
    
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
    
    # Hash and set new password, and bump token_version to invalidate any
    # access/refresh tokens issued before this reset.
    user.password = get_password_hash(payload.new_password)
    user.token_version += 1
    await session.commit()

    return PasswordResetResponse(
        success=True,
        message="Password reset successfully",
        email=user.email,
        username=user.username,
    )


# How long an impersonation hand-off token is good for. Short on purpose: it
# is exchanged for a session immediately after it is issued, so minutes is
# generous, and a leaked link stops working almost at once.
IMPERSONATION_TTL_SECONDS = 120


@router.post("/admin/impersonate/{user_id}", summary="Start a session as another user")
async def impersonate_user(
    user_id: int,
    session: SessionDep,
    admin: AdminAuth,
    request: Request,
    body: ImpersonationRequest | None = None,
) -> ImpersonationResponse:
    """
    Mint a short-lived hand-off token for another account.

    This is the most dangerous capability in the product, so it is fenced:

    - admins only, and never targeting another admin -- an admin who could
      impersonate a peer could act as them and leave the log pointing at the
      wrong person
    - the token lives for two minutes and is exchanged for a session
      immediately
    - the resulting session carries `impersonated_by`, so it stays
      identifiable for its whole life rather than only at creation
    - the attempt is recorded before the token is returned, so a token that
      is minted and never used is still in the log

    It was deliberately not built until the audit log existed, because an
    admin acting as a user with no record of it is precisely the thing worth
    refusing.
    """
    target = await get_user(session, user_id)
    if not target:
        raise CustomHTTPException(404, message="User not found")

    target_type = getattr(target.user_type, "value", target.user_type)
    if target_type == "admin":
        raise CustomHTTPException(
            403, message="Admins cannot impersonate other admins"
        )

    token = create_access_token(
        data=AuthTokenData(
            user_id=target.id,
            token_type="impersonation",
            token_version=target.token_version,
            impersonated_by=admin.id,
        ).model_dump(),
        expires_delta=timedelta(seconds=IMPERSONATION_TTL_SECONDS),
    )

    await audit.record(
        session,
        actor=admin,
        action="user.impersonate",
        entity_type="user",
        entity_id=target.id,
        summary=f"Started a session as {target.email or target.username}",
        details={
            "target_user_type": target_type,
            "reason": (body.reason if body else None),
        },
        request=request,
    )
    await session.commit()

    return ImpersonationResponse(
        token=token,
        expires_in=IMPERSONATION_TTL_SECONDS,
        user_id=target.id,
        full_name=target.full_name,
        email=target.email,
        user_type=target_type,
    )


@router.post("/impersonate/exchange", summary="Exchange a hand-off token for a session")
async def exchange_impersonation_token(
    response: Response,
    request: Request,
    session: SessionDep,
    token: str = Form(...),
) -> Token:
    """
    Turn the hand-off token into normal session cookies.

    Unauthenticated by design -- the token is the credential. It is accepted
    only if it is of type `impersonation` and still carries the admin that
    minted it, so an ordinary access token cannot be replayed here to launder
    itself into a session that looks real.
    """
    try:
        payload = decode_jwt_token(token)
        data = AuthTokenData(**payload)
    except Exception:
        raise CustomHTTPException(401, message="Invalid or expired token")

    if data.token_type != "impersonation" or not data.impersonated_by:
        raise CustomHTTPException(401, message="Not an impersonation token")

    target = await get_user(session, data.user_id)
    if not target or target.token_version != data.token_version:
        raise CustomHTTPException(401, message="Invalid or expired token")

    claims = AuthTokenData(
        user_id=target.id,
        token_type="access_token",
        token_version=target.token_version,
        impersonated_by=data.impersonated_by,
    ).model_dump()
    access_token = create_access_token(
        data=claims, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_access_token(
        data={**claims, "token_type": "refresh_token"},
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    _set_auth_cookies(request, response, access_token, refresh_token)
    return Token(access_token=access_token, refresh_token=refresh_token, token_type="bearer")
