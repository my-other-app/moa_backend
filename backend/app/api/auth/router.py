from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Form, Request, Response, BackgroundTasks
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
from app.core.auth.permissions import ImpersonateAuth, ResetCredentialsAuth
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
from app.core.response.base_model import CustomBaseModel
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
    # No HTTP Request here -- `request` is the body model -- so the session is
    # recorded without a device or address rather than renaming a public
    # parameter for the sake of two optional fields.
    token = await service.create_access_refresh_tokens(user, session=session)
    await session.commit()
    return token


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
    token = await service.create_access_refresh_tokens(user, session=session)
    await session.commit()
    return token


@router.post("/token", summary="get access token")
@limiter.limit("5/minute")
async def login_for_access_token(
    request: Request,
    response: Response,
    session: SessionDep,
    background: BackgroundTasks,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    totp_code: Optional[str] = Form(None),
) -> Token:
    from app.api.security import service as security

    async def refuse(user, result: str, status_code: int, message: str, **extra):
        await security.record(session, request=request, identifier=form_data.username, user=user, method="password", result=result)
        await session.commit()
        raise CustomHTTPException(status_code=status_code, message=message, **extra)

    wait = await security.lockout_remaining(session, form_data.username, security.client_ip(request))
    if wait:
        await refuse(None, "blocked", status.HTTP_429_TOO_MANY_REQUESTS, f"Too many wrong passwords. Try again in {max(1, wait // 60)} min.", error_code="LOGIN_LOCKED")

    # The clubs dashboard asks with scope "club": there, a club team member's
    # own email and password open a session on the club's account. Only
    # there — the same address may be someone's app account too, and the app
    # must keep signing them in as themselves.
    if "club" in (form_data.scopes or []):
        from app.api.club_access import service as club_access
        from app.api.clubs.models import Clubs

        member = await club_access.authenticate(session, form_data.username, form_data.password)
        if member is not None:
            club = await session.get(Clubs, member.club_id)
            owner = await get_user(session, club.user_id) if club is not None and not club.is_deleted and club.user_id else None
            if owner is None or getattr(owner, "is_active", True) is False:
                await refuse(None, "blocked", status.HTTP_403_FORBIDDEN, "This club's dashboard is closed right now. Ask the club's owner.", error_code="CLUB_UNAVAILABLE")
            token = await service.create_access_refresh_tokens(owner, session=session, request=request, member_id=member.id)
            first_time_here = await security.is_new_device(session, owner.id, request)
            await security.record(session, request=request, identifier=member.email, user=owner, method="password", result="signed_in", session_id=security.sid_in(token.access_token))
            member.last_seen_at = datetime.now(timezone.utc)
            await session.commit()
            if first_time_here and getattr(owner, "notify_new_device", True):
                device, _ = security.parse_agent(security.user_agent_of(request))
                ip = security.client_ip(request)
                background.add_task(
                    security.send_new_device_email, member.email, account=club.name, device=device,
                    place=await security.place_for(session, ip, resolve=False), ip=ip, when=datetime.now(timezone.utc),
                )
            _set_auth_cookies(request, response, token.access_token, token.refresh_token)
            return token

    user = await authenticate_user(session, form_data.username, form_data.password)
    if not user:
        await refuse(None, "wrong_password", status.HTTP_401_UNAUTHORIZED, "Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})

    # Second factor, where the account has one. The password is verified
    # first so an attacker learns nothing new from the order; the distinct
    # error code is what lets the UI show the code field rather than claiming
    # the password was wrong.
    if getattr(user, "totp_enabled", False):
        from app.api.auth import totp as totp_service

        if not totp_code:
            await refuse(user, "totp_required", status.HTTP_401_UNAUTHORIZED, "Enter the code from your authenticator app", error_code="TOTP_REQUIRED")

        if not totp_service.verify(user.totp_secret, totp_code):
            remaining = totp_service.consume_recovery_code(
                totp_code, user.totp_recovery_codes
            )
            if remaining is None:
                await refuse(user, "totp_invalid", status.HTTP_401_UNAUTHORIZED, "That code is not valid", error_code="TOTP_INVALID")
            # A recovery code is single use; leaving it active would turn one
            # leaked code into permanent access.
            user.totp_recovery_codes = remaining
            await session.commit()
    # Merge any guest accounts with matching email into this user
    from app.api.users.service import merge_guest_into_user
    await merge_guest_into_user(session, app_user=user)
    await session.commit()
    # commit() expires user's attributes; refresh explicitly here (a proper
    # awaited ORM op) rather than letting create_access_refresh_tokens's bare
    # attribute access trigger an implicit lazy-load, which can hit a
    # pool_pre_ping/greenlet interaction bug on an async engine (MissingGreenlet).
    await session.refresh(user)
    token = await service.create_access_refresh_tokens(
        user, session=session, request=request
    )
    first_time_here = await security.is_new_device(session, user.id, request)
    await security.record(session, request=request, identifier=form_data.username, user=user, method="password", result="signed_in", session_id=security.sid_in(token.access_token))
    await session.commit()
    if first_time_here and getattr(user, "notify_new_device", True) and user.email:
        device, _ = security.parse_agent(security.user_agent_of(request))
        ip = security.client_ip(request)
        background.add_task(
            security.send_new_device_email, user.email, account=await security.account_name(session, user), device=device,
            place=await security.place_for(session, ip, resolve=False), ip=ip, when=datetime.now(timezone.utc),
        )
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
        from app.api.auth import sessions as session_store

        if not await session_store.is_live(session, payload.sid):
            # Ended from the sessions screen. token_version is the blunt
            # instrument that ends every session; this ends one.
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="This session was signed out",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await session_store.touch(session, payload.sid)
        await session.commit()

        if payload.token_version != user.token_version:
            raise CustomHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                message="Token has been revoked, please log in again",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if payload.member_id:
            from app.api.club_access.models import ClubMembers

            member = await session.get(ClubMembers, payload.member_id)
            if member is None or member.status != "active" or user.club is None or member.club_id != user.club.id:
                raise CustomHTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    message="Your access to this club's dashboard was removed",
                    error_code="ACCESS_REMOVED",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        # The new access token keeps what the session is: its id (so ending
        # it from Security ends it now), whose behalf it acts on, and the club
        # member behind it. Dropping them here quietly turned every session
        # into a plain one an hour after sign-in.
        access_token_data = AuthTokenData(
            user_id=user.id,
            token_type="access_token",
            token_version=user.token_version,
            sid=payload.sid,
            impersonated_by=payload.impersonated_by,
            member_id=payload.member_id,
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
    admin: ResetCredentialsAuth,
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
    admin: ImpersonateAuth,
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


class SessionResponse(CustomBaseModel):
    id: str
    user_agent: str | None = None
    ip_address: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    current: bool = False


@router.get("/sessions", summary="List your active sessions")
async def list_sessions(
    session: SessionDep,
    user: DependsAuth,
    request: Request,
) -> list[SessionResponse]:
    """
    Your own sessions only.

    token_version could already end every session at once, which is right for
    a password change and wrong for "sign out my old laptop" -- there was no
    way to see what existed, let alone end one.
    """
    from app.api.auth import sessions as session_store

    current_sid = None
    token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if token:
        try:
            current_sid = AuthTokenData(**decode_jwt_token(token)).sid
        except Exception:
            current_sid = None

    rows = await session_store.list_for_user(session, user.id)
    return [
        SessionResponse(
            id=str(row.id),
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            last_seen_at=row.last_seen_at,
            created_at=row.created_at,
            # Marked so nobody ends the session they are using by accident.
            current=str(row.id) == current_sid,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", summary="Sign out one session")
async def revoke_session(
    session_id: str,
    session: SessionDep,
    user: DependsAuth,
):
    from app.api.auth import sessions as session_store

    # Scoped to the owner, so one user cannot end another's session.
    if not await session_store.revoke(session, session_id, user.id):
        raise CustomHTTPException(404, message="No such session")
    return {"message": "Session signed out"}


@router.delete("/sessions", summary="Sign out every other session")
async def revoke_all_sessions(
    session: SessionDep,
    user: DependsAuth,
):
    """
    Ends every session including this one, and bumps token_version so already
    issued access tokens stop immediately rather than lasting out their term.
    """
    from app.api.auth import sessions as session_store
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    count = await session_store.revoke_all_for_user(session, user.id)
    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    if db_user:
        db_user.token_version += 1
        await session.commit()
    return {"message": f"Signed out {count} session(s)"}


class TotpSetupResponse(CustomBaseModel):
    secret: str
    provisioning_uri: str


class TotpEnableRequest(CustomBaseModel):
    code: str


class TotpEnableResponse(CustomBaseModel):
    enabled: bool
    # Shown once and never again -- only hashes are stored.
    recovery_codes: list[str]


class TotpStatusResponse(CustomBaseModel):
    enabled: bool
    recovery_codes_remaining: int = 0


@router.get("/2fa", summary="Whether two-factor is on for this account")
async def totp_status(user: DependsAuth) -> TotpStatusResponse:
    return TotpStatusResponse(
        enabled=bool(getattr(user, "totp_enabled", False)),
        recovery_codes_remaining=len(user.totp_recovery_codes or []),
    )


@router.post("/2fa/setup", summary="Start two-factor enrolment")
async def totp_setup(session: SessionDep, user: DependsAuth) -> TotpSetupResponse:
    """
    Generates a secret and returns the otpauth:// URI to scan.

    Two steps on purpose: the secret is stored but not enabled until the user
    proves they can read a code from it. Enabling here would lock out anyone
    whose authenticator failed to scan.
    """
    from app.api.auth import totp as totp_service
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    if getattr(user, "totp_enabled", False):
        raise CustomHTTPException(400, message="Two-factor is already enabled")

    secret = totp_service.new_secret()
    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    db_user.totp_secret = secret
    await session.commit()

    return TotpSetupResponse(
        secret=secret,
        provisioning_uri=totp_service.provisioning_uri(secret, user.email or user.username),
    )


@router.post("/2fa/enable", summary="Confirm and enable two-factor")
async def totp_enable(
    body: TotpEnableRequest, session: SessionDep, user: DependsAuth
) -> TotpEnableResponse:
    from app.api.auth import totp as totp_service
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    if not db_user.totp_secret:
        raise CustomHTTPException(400, message="Start setup first")
    if not totp_service.verify(db_user.totp_secret, body.code):
        raise CustomHTTPException(400, message="That code is not valid")

    plaintext, hashed = totp_service.generate_recovery_codes()
    db_user.totp_enabled = True
    db_user.totp_recovery_codes = hashed
    await session.commit()

    return TotpEnableResponse(enabled=True, recovery_codes=plaintext)


@router.post("/2fa/disable", summary="Turn two-factor off")
async def totp_disable(
    body: TotpEnableRequest, session: SessionDep, user: DependsAuth
):
    """
    Requires a current code, not just a session. Otherwise anyone who walked
    up to an unlocked screen could remove the second factor, which is the one
    thing it exists to prevent.
    """
    from app.api.auth import totp as totp_service
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    if not db_user.totp_enabled:
        raise CustomHTTPException(400, message="Two-factor is not enabled")
    if not totp_service.verify(db_user.totp_secret, body.code):
        raise CustomHTTPException(400, message="That code is not valid")

    db_user.totp_enabled = False
    db_user.totp_secret = None
    db_user.totp_recovery_codes = None
    await session.commit()
    return {"message": "Two-factor disabled"}
