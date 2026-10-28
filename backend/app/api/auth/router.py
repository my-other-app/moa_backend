import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, NoReturn, Optional
from fastapi import APIRouter, Depends, Form, Request, Response, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import status
import jwt

from app.core.rate_limit import account_or_address, limiter
from app.core.auth.jwt import create_access_token, decode_jwt_token
from app.core.auth.authentication import (
    authenticate_user,
    get_user,
    get_password_hash_async,
)
from app.core.auth.dependencies import DependsAuth, AdminAuth, UserAuth
from app.core.auth.permissions import ImpersonateAuth, ResetCredentialsAuth
from app.response import CustomHTTPException
from app.api.auth.schemas import (
    AuthTokenData,
    AuthUser,
    EmailCodeSent,
    EmailVerified,
    EmailVerifyRequest,
    PasswordForgotRequest,
    PasswordForgotResponse,
    PasswordResetConfirm,
    Token,
    GoogleSignInRequest,
    AppleSignInRequest,
    PasswordResetRequest,
    PasswordResetResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    DashboardGoogleSignInRequest,
    ImpersonationRequest,
    ImpersonationResponse,
)
from app.api.audit import service as audit
from app.api.auth import service
from app.db.core import SessionDep
from app.core.response.base_model import CustomBaseModel
from app.config import settings

logger = logging.getLogger(__name__)

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
# Per caller address: a campus behind one Wi-Fi address signs in at a fest,
# so loose; a script minting accounts or testing tokens meets it.
@limiter.limit("30/minute")
async def google_sign_in(
    request: Request,
    body: GoogleSignInRequest,
    session: SessionDep,
) -> Token:
    user = await service.google_signin(session, body.id_token, body.platform)
    token = await service.create_access_refresh_tokens(user, session=session, request=request)
    await session.commit()
    return token


@router.post("/apple", summary="Sign in with Apple")
@limiter.limit("30/minute")
async def apple_sign_in(
    request: Request,
    body: AppleSignInRequest,
    session: SessionDep,
) -> Token:
    """
    Sign in with Apple.
    
    Receives the identity_token and authorization_code from the Apple Sign In flow.
    On first sign in, user_name and user_email may be provided by Apple.
    """
    user = await service.apple_signin(
        session=session,
        identity_token=body.identity_token,
        authorization_code=body.authorization_code,
        user_name=body.user_name,
        user_email=body.user_email,
    )
    token = await service.create_access_refresh_tokens(user, session=session, request=request)
    await session.commit()
    return token


# ─── The door ────────────────────────────────────────────────────────────
#
# /token (a password) and /google/dashboard (Google) each find the account
# their own way. From there they are one door: the same refusals, kept on
# the record the same way, the same second factor, session, new-device mail
# and cookies. `identifier` is what the lockout and the record key on (what
# was typed, or Google's address); `method` is what the record says.


async def _refuse(session, request: Request, *, identifier: str | None, user, method: str, result: str, status_code: int, message: str, **extra) -> NoReturn:
    """Kept on the record (the lockout counts the failed ones) and committed,
    the place looked up after; then the answer."""
    from app.api.security import service as security

    await security.record(session, request=request, identifier=identifier, user=user, method=method, result=result)
    await session.commit()
    await security.place_later(session, security.client_ip(request))
    raise CustomHTTPException(status_code=status_code, message=message, **extra)


async def _refuse_if_locked(session, request: Request, *, identifier: str, method: str) -> None:
    from app.api.security import service as security

    wait = await security.lockout_remaining(session, identifier, security.client_ip(request))
    if wait:
        await _refuse(
            session, request, identifier=identifier, user=None, method=method, result="blocked",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, message=f"Too many wrong passwords. Try again in {max(1, wait // 60)} min.",
            error_code="LOGIN_LOCKED",
        )


async def _refuse_if_club_waiting(session, request: Request, user, *, identifier: str, method: str) -> None:
    if not user.club.is_verified:
        await _refuse(
            session, request, identifier=identifier, user=user, method=method, result="blocked",
            status_code=status.HTTP_403_FORBIDDEN, message="Your club is waiting for approval. We will email you when it is approved.",
            error_code="CLUB_NOT_VERIFIED",
        )


async def _member_signed_in(session, request: Request, response: Response, member, *, identifier: str, method: str) -> Token:
    """A club team member: a session on the club's own account, with the
    member behind it. Closed while the club's own account is suspended or
    gone (CLUB_UNAVAILABLE)."""
    from app.api.clubs.models import Clubs

    club = await session.get(Clubs, member.club_id)
    owner = await get_user(session, club.user_id) if club is not None and not club.is_deleted and club.user_id else None
    if owner is None or getattr(owner, "is_active", True) is False:
        await _refuse(
            session, request, identifier=identifier, user=None, method=method, result="blocked",
            status_code=status.HTTP_403_FORBIDDEN, message="This club's dashboard is closed right now. Ask the club's owner.",
            error_code="CLUB_UNAVAILABLE",
        )
    return await _signed_in(session, request, response, owner, identifier=member.email, method=method, member=member, account=club.name)


async def _account_signed_in(
    session, request: Request, response: Response, user, *, identifier: str, method: str, totp_code: str | None,
) -> Token:
    """An account itself (a club's own, an admin, an app user): refused when
    suspended, asked for its second factor when it has one, then signed in."""
    # Said plainly at the door: a suspended account used to get tokens here
    # and then a 403 on its first request.
    if getattr(user, "is_active", True) is False:
        await _refuse(
            session, request, identifier=identifier, user=user, method=method, result="blocked",
            status_code=status.HTTP_403_FORBIDDEN, message="This account has been suspended", error_code="ACCOUNT_SUSPENDED",
        )

    # Second factor, where the account has one. The password (or Google) is
    # checked first so an attacker learns nothing new from the order; the
    # distinct error code is what lets the UI show the code field rather than
    # claiming the password was wrong.
    if getattr(user, "totp_enabled", False):
        from app.api.auth import totp as totp_service

        if not totp_code:
            await _refuse(
                session, request, identifier=identifier, user=user, method=method, result="totp_required",
                status_code=status.HTTP_401_UNAUTHORIZED, message="Enter the code from your authenticator app", error_code="TOTP_REQUIRED",
            )
        # A code is good once (a replayed one is refused) and a recovery code
        # is spent: app.api.auth.totp.
        if not await totp_service.check_second_factor(session, user, totp_code):
            await _refuse(
                session, request, identifier=identifier, user=user, method=method, result="totp_invalid",
                status_code=status.HTTP_401_UNAUTHORIZED, message="That code is not valid", error_code="TOTP_INVALID",
            )
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
    return await _signed_in(session, request, response, user, identifier=identifier, method=method)


async def _signed_in(
    session, request: Request, response: Response, user, *, identifier: str, method: str, member=None, account: str | None = None,
) -> Token:
    """The session (a member's rides on the club's account), the record, the
    new-device mail (to the member, for a member) and the cookies."""
    from app.api.security import service as security

    token = await service.create_access_refresh_tokens(
        user, session=session, request=request, member_id=member.id if member is not None else None,
    )
    first_time_here = await security.is_new_device(session, user.id, request)
    await security.record(
        session, request=request, identifier=identifier, user=user, method=method, result="signed_in",
        session_id=security.sid_in(token.access_token),
    )
    if member is not None:
        member.last_seen_at = datetime.now(timezone.utc)
    await session.commit()
    mail = None
    to = member.email if member is not None else user.email
    if first_time_here and getattr(user, "notify_new_device", True) and to:
        device, _ = security.parse_agent(security.user_agent_of(request))
        mail = dict(to=to, account=account or await security.account_name(session, user), device=device, when=datetime.now(timezone.utc))
    # The place is looked up after the commit, never inside the sign-in.
    await security.place_later(session, security.client_ip(request), new_device_mail=mail)
    _set_auth_cookies(request, response, token.access_token, token.refresh_token)
    return token


@router.post("/token", summary="get access token")
# Per caller address (app.core.net). A whole campus behind one Wi-Fi address
# signs in through this at a fest, so the cap is loose; guessing one account's
# password is stopped by the per-account lockout (LOGIN_LOCKED) below.
@limiter.limit("20/minute")
async def login_for_access_token(
    request: Request,
    response: Response,
    session: SessionDep,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    totp_code: Optional[str] = Form(None),
) -> Token:
    username = form_data.username
    await _refuse_if_locked(session, request, identifier=username, method="password")

    # The clubs dashboard asks with scope "club": there, a club team member's
    # own email and password open a session on the club's account. Only
    # there — the same address may be someone's app account too, and the app
    # must keep signing them in as themselves.
    club_scope = "club" in (form_data.scopes or [])
    if club_scope:
        from app.api.club_access import service as club_access

        member = await club_access.authenticate(session, username, form_data.password)
        if member is not None:
            return await _member_signed_in(session, request, response, member, identifier=username, method="password")

    user = None
    if club_scope:
        # On the clubs dashboard the club account comes first: the same
        # address may also be the person's app account, and that one cannot
        # open a club's dashboard. Without the scope (the app) nothing changes.
        from app.core.auth.authentication import authenticate_club_account

        user = await authenticate_club_account(session, username, form_data.password)
        if user is not None:
            await _refuse_if_club_waiting(session, request, user, identifier=username, method="password")
    if user is None:
        from app.api.users.models import UserTypes

        user = await authenticate_user(
            session, username, form_data.password,
            exclude_type=UserTypes.club if club_scope else None,
        )
    if not user:
        await _refuse(
            session, request, identifier=username, user=None, method="password", result="wrong_password",
            status_code=status.HTTP_401_UNAUTHORIZED, message="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"},
        )
    return await _account_signed_in(session, request, response, user, identifier=username, method="password", totp_code=totp_code)


GOOGLE_NO_ACCOUNT_MESSAGE = (
    "No club dashboard / admin account uses this Google account. "
    "Sign in with your email and password, or ask to be invited."
)


@router.post("/google/dashboard", summary="Sign in to the club dashboard or the main portal with Google")
# The password sign-in's limit: this is the same door.
@limiter.limit("20/minute")
async def google_dashboard_sign_in(
    request: Request,
    response: Response,
    body: DashboardGoogleSignInRequest,
    session: SessionDep,
) -> Token:
    """
    The dashboards' "Continue with Google". The ID token is checked as
    `/auth/google` checks the web's (audience APP_GOOGLE_WEB_CLIENT_ID,
    signature, issuer, expiry; Google must have verified the address), and
    its address signs in an account that already exists. None is ever made.

    - `scope: "club"`: an active club team member with the address (a session
      on the club's account, with `member_id`), else the club's own login
      account. 403 CLUB_UNAVAILABLE / CLUB_NOT_VERIFIED as on `/auth/token`.
    - `scope: "admin"`: an admin account only.

    Anything else -- no account, an app user, a guest, the other dashboard's
    account -- is the same 401 GOOGLE_NO_ACCOUNT. Then as `/auth/token`: 403
    ACCOUNT_SUSPENDED; 401 TOTP_REQUIRED when the account has two-factor
    (send the same id_token again with `totp_code`) and TOTP_INVALID; 429
    LOGIN_LOCKED; the same answer, session and cookies. 401
    INVALID_GOOGLE_TOKEN for a token Google would not vouch for; 503
    GOOGLE_UNAVAILABLE when its certificates can't be fetched.
    """
    email = await service.dashboard_google_email(body.id_token)
    await _refuse_if_locked(session, request, identifier=email, method="google")
    member, user = await service.dashboard_account(session, email, body.scope)
    if member is not None:
        return await _member_signed_in(session, request, response, member, identifier=email, method="google")
    if user is None:
        # One answer for every miss: which addresses have which accounts is
        # not for anyone holding a Google account to find out.
        await _refuse(
            session, request, identifier=email, user=None, method="google", result="no_account",
            status_code=status.HTTP_401_UNAUTHORIZED, message=GOOGLE_NO_ACCOUNT_MESSAGE, error_code="GOOGLE_NO_ACCOUNT",
        )
    if body.scope == "club":
        await _refuse_if_club_waiting(session, request, user, identifier=email, method="google")
    return await _account_signed_in(session, request, response, user, identifier=email, method="google", totp_code=body.totp_code)


def _unauthorized(message: str, **extra) -> CustomHTTPException:
    return CustomHTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, message=message, headers={"WWW-Authenticate": "Bearer"}, **extra,
    )


@router.post("/refresh", summary="refresh access token")
@limiter.limit("20/minute")
async def refresh_access_token(
    request: Request,
    response: Response,
    session: SessionDep,
    token: Optional[str] = Form(None),
) -> Token:
    """
    A new access token -- and a NEW refresh token, which replaces the one
    sent: keep the one in the answer (the body, and the `refresh_token`
    cookie). It expires when the one sent would have.

    The one sent stays good until the new one is first used: presented
    again before that (the answer was lost, the app was killed mid-refresh,
    two tabs refreshed together) it is answered with the same new refresh
    token again, however much later. Once the new one has been used, the
    old one -- or anything older -- ends the session (401 SESSION_REUSED):
    two parties holding one session means one of them stole it.
    """
    from app.api.auth import sessions as session_store

    # Fall back to the refresh-token cookie when no form field is sent (the
    # cookie-auth path — see _set_auth_cookies).
    token = token or request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    if not token:
        raise _unauthorized("No refresh token provided")
    try:
        claims = decode_jwt_token(token)
        payload = AuthTokenData(**claims)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token has expired")
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise _unauthorized("Invalid token")
    if payload.token_type != "refresh_token":
        raise _unauthorized("Invalid token type")
    if payload.impersonated_by:
        # An admin's session as someone else is never renewed; one minted
        # before that rule carried a refresh token, which stops here.
        raise _unauthorized("An admin's session as someone else cannot be renewed", error_code="IMPERSONATION_ENDED")
    user = await get_user(session, payload.user_id, allow_unverified_club=True)
    if not user:
        raise _unauthorized("User not found")
    if getattr(user.user_type, "value", user.user_type) == "club" and (user.club is None or not user.club.is_verified):
        raise CustomHTTPException(status_code=status.HTTP_403_FORBIDDEN, message="Club is not verified", error_code="CLUB_NOT_VERIFIED")
    if payload.token_version != user.token_version:
        raise _unauthorized("Token has been revoked, please log in again")
    if payload.member_id:
        from app.api.club_access.models import ClubMembers

        member = await session.get(ClubMembers, payload.member_id)
        if member is None or member.status != "active" or user.club is None or member.club_id != user.club.id:
            raise _unauthorized("Your access to this club's dashboard was removed", error_code="ACCESS_REMOVED")

    if payload.sid:
        outcome, generation = await session_store.rotate(session, payload.sid, int(payload.gen or 0))
        if outcome == "reused":
            from app.api.security import service as security

            # Ended for good, and on the Security page as such.
            await security.record(
                session, request=request, identifier=user.email, user=user, method="refresh", result="refresh_reused",
                session_id=security.sid_in(token),
            )
            await session.commit()
            logger.warning("A spent refresh token was presented for session %s of user %s: session ended", payload.sid, user.id)
            raise _unauthorized("This session was ended because its sign-in was used from two places. Sign in again.", error_code="SESSION_REUSED")
        if outcome == "ended":
            # Ended from the sessions screen. token_version is the blunt
            # instrument that ends every session; this ends one.
            raise _unauthorized("This session was signed out")
        sid = payload.sid
    else:
        # From before sessions existed: a session now, so this token is the
        # last one with no rotation behind it.
        sid = str(await session_store.start(session, user.id, request=request, member_id=payload.member_id))
        generation = 0
    await session.commit()

    # A refresh is the one call every app session makes on its own,
    # weeks after the sign-in that last merged.
    from app.api.users.service import merge_guests_now

    await merge_guests_now(session, user)
    # The new tokens keep what the session is: its id (so ending it from
    # Security ends it now) and the club member behind it.
    access_token = create_access_token(
        data=AuthTokenData(
            user_id=user.id, token_type="access_token", token_version=user.token_version,
            sid=sid, member_id=payload.member_id,
        ).model_dump(),
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    # The new refresh token ends when the one it replaces did: rotation
    # does not stretch a session past REFRESH_TOKEN_EXPIRE_DAYS from its
    # sign-in.
    ends = claims.get("exp")
    lifetime = (
        datetime.fromtimestamp(int(ends), timezone.utc) - datetime.now(timezone.utc)
        if ends else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    refresh_token = create_access_token(
        data=AuthTokenData(
            user_id=user.id, token_type="refresh_token", token_version=user.token_version,
            sid=sid, member_id=payload.member_id, gen=generation,
        ).model_dump(),
        expires_delta=max(lifetime, timedelta(seconds=1)),
    )
    _set_auth_cookies(request, response, access_token, refresh_token)
    return Token(access_token=access_token, refresh_token=refresh_token, token_type="Bearer")


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


@router.post("/email/send-code", summary="Send a code that verifies the account's email")
@limiter.limit("5/minute", key_func=account_or_address)
async def send_email_code(
    request: Request,
    session: SessionDep,
    user: UserAuth,
    background: BackgroundTasks,
) -> EmailCodeSent:
    """
    Six digits to the account's own address, good for fifteen minutes; any
    earlier code stops working. Once a minute and ten times a day per account:
    sooner is a 429 with `errors.retry_after` in seconds. 409 ALREADY_VERIFIED
    when there is nothing to verify. The mail goes out after the response.
    """
    from app.api.auth import verification

    code = await verification.issue(session, user)
    email, name = user.email, user.full_name
    await session.commit()
    background.add_task(verification.send_code_email, email, code=code, name=name)
    return EmailCodeSent(
        email=email,
        expires_in=int(verification.CODE_TTL.total_seconds()),
        retry_after=int(verification.RESEND_AFTER.total_seconds()),
    )


@router.post("/email/verify", summary="Verify the account's email with the code sent to it")
@limiter.limit("10/minute", key_func=account_or_address)
async def verify_email(
    request: Request,
    body: EmailVerifyRequest,
    session: SessionDep,
    user: UserAuth,
) -> EmailVerified:
    """
    On a match the address is verified, every code closed, and whatever was
    booked as a guest under the address is brought into the account. A wrong
    code is a 400 CODE_INVALID with `errors.attempts_left`; the fifth wrong
    one kills the code (CODE_LOCKED); an old, replaced or absent one is
    CODE_EXPIRED.
    """
    from app.api.auth import verification
    from app.api.users.service import merge_guest_into_user

    verified_at = await verification.verify(session, user, body.code)
    # Never raises: a merge that fails is undone on its own and retried by
    # the next profile load, and the verification still stands.
    summary = await merge_guest_into_user(session, user)
    await session.commit()
    return EmailVerified(
        email_verified_at=verified_at,
        merged=bool(summary.get("merged")),
        registrations_transferred=int(summary.get("registrations_transferred") or 0),
    )


def _caller_address(request: Request) -> str:
    """The rate limiter's key for the forgotten-password pair: the caller's
    address as app.core.net reads it (the forwarded headers only from our own
    proxy, right to left), so a caller can neither share everyone's budget
    nor write a fresh address to dodge its own."""
    from app.api.security.service import client_ip

    return client_ip(request) or "unknown"


@router.post("/password/forgot", summary="Send a code to reset a forgotten password")
@limiter.limit("5/minute;20/hour", key_func=_caller_address)
async def forgot_password(
    request: Request,
    body: PasswordForgotRequest,
    background: BackgroundTasks,
) -> PasswordForgotResponse:
    """
    Always 200 `{"sent": true}`, at once, whatever the address: it never says
    whether there is an account. After the response, when the address has an
    app account that signs in with a password, a six-digit code goes to it
    (good for fifteen minutes; at most one a minute and ten a day per
    address, past which nothing goes). Google and Apple accounts get nothing.
    """
    from app.api.auth import password_reset

    background.add_task(password_reset.request_reset, body.email)
    return PasswordForgotResponse()


@router.post("/password/reset", summary="Set a new password with the code from the email, and sign in")
@limiter.limit("10/minute", key_func=_caller_address)
async def reset_password(
    request: Request,
    response: Response,
    body: PasswordResetConfirm,
    session: SessionDep,
) -> Token:
    """
    The code and the new password: on a match the password is set, every
    other session ends, the address counts as verified, whatever was booked
    as a guest under it comes into the account, and the answer is the same
    tokens as `/auth/token` (the person is signed in).

    A wrong code -- or an address with no live code, account or not -- is a
    400 CODE_INVALID with `errors.attempts_left`; the fifth wrong try kills
    the code (CODE_LOCKED); an old, used or replaced one is CODE_EXPIRED. An
    account with two-factor on also needs `totp_code`: 401 TOTP_REQUIRED
    without it, TOTP_INVALID (a wrong try) when it is wrong.
    """
    from app.api.auth import password_reset
    from app.api.security import service as security
    from app.api.users.service import merge_guest_into_user

    user = await password_reset.reset(
        session, email=body.email, code=body.code, new_password=body.new_password, totp_code=body.totp_code,
    )
    # Never raises: a merge that fails is undone on its own and tried again later.
    await merge_guest_into_user(session, user)
    await session.commit()
    await session.refresh(user)
    token = await service.create_access_refresh_tokens(user, session=session, request=request)
    await security.record(
        session, request=request, identifier=user.email, user=user, method="password_reset", result="signed_in",
        session_id=security.sid_in(token.access_token),
    )
    await session.commit()
    await security.place_later(session, security.client_ip(request))
    _set_auth_cookies(request, response, token.access_token, token.refresh_token)
    return token


@router.post("/change-password", summary="Change user password")
@limiter.limit("5/minute", key_func=account_or_address)
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    session: SessionDep,
    current_user: DependsAuth,
) -> ChangePasswordResponse:
    """Five tries a minute per account, and a wrong current password counts
    toward the account's lockout like a wrong sign-in does."""
    from sqlalchemy import select
    from app.api.security import service as security
    from app.api.users.models import Users
    from app.core.auth.authentication import verify_password_async

    identifier = current_user.email or current_user.username
    wait = await security.account_lockout_remaining(session, identifier, security.client_ip(request))
    if wait:
        raise CustomHTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, message=f"Too many wrong passwords. Try again in {max(1, wait // 60)} min.", error_code="LOGIN_LOCKED")
    if not await verify_password_async(body.old_password, current_user.password):
        await security.record(session, request=request, identifier=identifier, user=current_user, method="password", result="wrong_password")
        await session.commit()
        raise CustomHTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            message="Incorrect old password",
        )

    # Re-fetch user on the current active session to avoid detached instance issues
    db_user = (await session.execute(select(Users).where(Users.id == current_user.id))).scalar_one()

    # Hash and set new password, and bump token_version to invalidate any
    # access/refresh tokens issued before this change.
    db_user.password = await get_password_hash_async(body.new_password)
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
    from app.api.users.models import Users, UserTypes
    from app.api.clubs.models import Clubs

    search_term = payload.email_or_username.strip()

    async def one(query, how: str):
        """The one account `query` finds, None for none -- and 409 for more
        than one: an address can hold an app account and a club account, and
        two clubs can share a name. It was scalar_one_or_none: a 500."""
        found = list((await session.scalars(query.limit(2))).unique().all())
        if len(found) > 1:
            raise CustomHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                message=f"More than one account matches that {how}. Use the username or the club's id instead.",
                error_code="AMBIGUOUS_ACCOUNT",
            )
        return found[0] if found else None

    user = None
    # Try different search strategies
    if "@" in search_term:
        # Search by user email. Guests are left out: they have no password
        # to reset, and an address the public form has seen can have several.
        user = await one(select(Users).where(Users.email == search_term, Users.user_type != UserTypes.guest), "address")

    if not user and search_term.isdigit():
        user = await one(select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.id == int(search_term)), "club id")

    if not user:
        user = await one(select(Users).where(Users.username == search_term), "username")

    if not user:
        user = await one(select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.slug == search_term), "club address")

    if not user:
        # Search by club name (case-insensitive)
        user = await one(select(Users).join(Clubs, Users.id == Clubs.user_id).where(Clubs.name.ilike(search_term)), "club name")

    if not user:
        raise CustomHTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            message=f"User or club '{search_term}' not found",
        )

    # Hash and set new password, and bump token_version to invalidate any
    # access/refresh tokens issued before this reset.
    user.password = await get_password_hash_async(payload.new_password)
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
    - it needs a reason, at least IMPERSONATION_REASON_MIN characters, kept
      with the attempt in the log: a session nobody can explain later is
      one nobody can review (422 REASON_REQUIRED without one)

    It was deliberately not built until the audit log existed, because an
    admin acting as a user with no record of it is precisely the thing worth
    refusing.
    """
    from app.api.auth.schemas import IMPERSONATION_REASON_MAX, IMPERSONATION_REASON_MIN

    target = await get_user(session, user_id)
    if not target:
        raise CustomHTTPException(404, message="User not found")

    target_type = getattr(target.user_type, "value", target.user_type)
    if target_type == "admin":
        raise CustomHTTPException(
            403, message="Admins cannot impersonate other admins"
        )

    # Spaces in a row count once, and none at the ends: "          " is not
    # ten characters of reason.
    reason = " ".join(((body.reason if body else None) or "").split())
    if len(reason) < IMPERSONATION_REASON_MIN:
        raise CustomHTTPException(
            422,
            message=f"Say why you need a session as this account: at least {IMPERSONATION_REASON_MIN} characters. It is kept in the activity log.",
            error_code="REASON_REQUIRED",
            errors={"fields": {"reason": f"At least {IMPERSONATION_REASON_MIN} characters."}},
        )
    if len(reason) > IMPERSONATION_REASON_MAX:
        raise CustomHTTPException(
            422,
            message=f"Keep the reason to {IMPERSONATION_REASON_MAX} characters.",
            error_code="REASON_TOO_LONG",
            errors={"fields": {"reason": f"At most {IMPERSONATION_REASON_MAX} characters."}},
        )

    import uuid

    token = create_access_token(
        data=AuthTokenData(
            user_id=target.id,
            token_type="impersonation",
            token_version=target.token_version,
            impersonated_by=admin.id,
            # One exchange only: this becomes the session's id, and a second
            # exchange finds it taken.
            jti=str(uuid.uuid4()),
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
            "reason": reason,
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


def impersonation_minutes() -> int:
    """How long an admin's session as someone else lasts: 30 to 60 minutes."""
    return max(30, min(60, int(settings.IMPERSONATION_SESSION_MINUTES or 30)))


@router.post("/impersonate/exchange", summary="Exchange a hand-off token for a session")
@limiter.limit("10/minute")
async def exchange_impersonation_token(
    response: Response,
    request: Request,
    session: SessionDep,
    token: str = Form(...),
) -> Token:
    """
    Turn the hand-off token into a session as the target account.

    Unauthenticated by design -- the token is the credential. It is accepted
    only if it is of type `impersonation`, still carries the admin that
    minted it, and has not been exchanged before (its `jti` becomes the
    session's id: a second exchange finds it taken). The admin must still be
    an active admin allowed to impersonate.

    The session has a row like any other -- the target sees it on their
    Security page and can end it -- carries `impersonated_by`, ends
    IMPERSONATION_SESSION_MINUTES (30 to 60) after this, and is never
    renewed: there is no refresh token (`refresh_token` is empty, and the
    refresh cookie is cleared). The exchange is in the audit log.
    """
    import uuid

    from app.api.auth import sessions as session_store
    from app.core.auth.dependencies import check_impersonator

    try:
        data = AuthTokenData(**decode_jwt_token(token))
    except Exception:
        raise CustomHTTPException(401, message="Invalid or expired token")

    if data.token_type != "impersonation" or not data.impersonated_by:
        raise CustomHTTPException(401, message="Not an impersonation token")
    try:
        sid = uuid.UUID(str(data.jti))
    except (TypeError, ValueError):
        # Minted before hand-offs were single-use.
        raise CustomHTTPException(401, message="This link has expired. Start the session again.", error_code="HANDOFF_EXPIRED")

    target = await get_user(session, data.user_id)
    if not target or target.token_version != data.token_version:
        raise CustomHTTPException(401, message="Invalid or expired token")
    admin = await check_impersonator(session, data.impersonated_by)

    minutes = impersonation_minutes()
    ends = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    if not await session_store.start_once(
        session, sid, target.id, request=request, impersonated_by=admin.id, expires_at=ends,
    ):
        raise CustomHTTPException(401, message="This link was already used. Start the session again.", error_code="HANDOFF_USED")
    await audit.record(
        session,
        actor=admin,
        action="user.impersonate.exchange",
        entity_type="user",
        entity_id=target.id,
        summary=f"Session as {target.email or target.username} began",
        details={"session_id": str(sid), "ends_at": ends.isoformat(), "minutes": minutes},
        request=request,
    )
    await session.commit()

    access_token = create_access_token(
        data=AuthTokenData(
            user_id=target.id,
            token_type="access_token",
            token_version=target.token_version,
            impersonated_by=admin.id,
            sid=str(sid),
        ).model_dump(),
        expires_delta=timedelta(minutes=minutes),
    )
    cookie_kwargs = dict(httponly=True, secure=request.url.scheme == "https", samesite="lax")
    response.set_cookie(ACCESS_TOKEN_COOKIE_NAME, access_token, max_age=minutes * 60, **cookie_kwargs)
    # Whatever refresh cookie this browser held belongs to another session.
    response.delete_cookie(REFRESH_TOKEN_COOKIE_NAME)
    return Token(access_token=access_token, refresh_token="", token_type="bearer")


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
@limiter.limit("5/minute", key_func=account_or_address)
async def totp_setup(request: Request, session: SessionDep, user: DependsAuth) -> TotpSetupResponse:
    """
    Generates a secret and returns the otpauth:// URI to scan.

    Two steps on purpose: the secret is stored but not enabled until the user
    proves they can read a code from it. Enabling here would lock out anyone
    whose authenticator failed to scan. Stored encrypted when the server has
    a key (app.api.auth.totp).
    """
    from app.api.auth import totp as totp_service
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    if getattr(user, "totp_enabled", False):
        raise CustomHTTPException(400, message="Two-factor is already enabled")

    secret = totp_service.new_secret()
    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    db_user.totp_secret = totp_service.seal_secret(secret)
    db_user.totp_last_step = None
    await session.commit()

    return TotpSetupResponse(
        secret=secret,
        provisioning_uri=totp_service.provisioning_uri(secret, user.email or user.username),
    )


@router.post("/2fa/enable", summary="Confirm and enable two-factor")
@limiter.limit("5/minute", key_func=account_or_address)
async def totp_enable(
    request: Request, body: TotpEnableRequest, session: SessionDep, user: DependsAuth
) -> TotpEnableResponse:
    """The code confirming enrolment is used up like any other: the next
    sign-in needs the next one."""
    from app.api.auth import totp as totp_service
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    if not db_user.totp_secret:
        raise CustomHTTPException(400, message="Start setup first")
    if not await totp_service.accept_code(session, db_user, body.code):
        raise CustomHTTPException(400, message="That code is not valid")

    plaintext, stored = totp_service.generate_recovery_codes()
    db_user.totp_enabled = True
    db_user.totp_recovery_codes = stored
    await session.commit()

    return TotpEnableResponse(enabled=True, recovery_codes=plaintext)


@router.post("/2fa/recovery-codes", summary="Replace the recovery codes")
@limiter.limit("5/minute", key_func=account_or_address)
async def totp_new_recovery_codes(
    request: Request, body: TotpEnableRequest, session: SessionDep, user: DependsAuth
) -> TotpEnableResponse:
    """A fresh set of eight, shown once, for a current authenticator code;
    every earlier one stops working. Also how codes issued before the
    current format are replaced. Never for an admin acting as the account
    (403 NOT_WHILE_IMPERSONATING): the codes get past its second factor."""
    from app.api.auth import totp as totp_service
    from app.api.subscriptions import gates
    from sqlalchemy import select as sa_select
    from app.api.users.models import Users as UsersModel

    if gates.impersonated_by():
        raise CustomHTTPException(
            403, message="Recovery codes are the account's own: they can't be made while acting as it.",
            error_code="NOT_WHILE_IMPERSONATING",
        )

    db_user = await session.scalar(sa_select(UsersModel).where(UsersModel.id == user.id))
    if not db_user.totp_enabled:
        raise CustomHTTPException(400, message="Two-factor is not enabled")
    if not await totp_service.accept_code(session, db_user, body.code):
        raise CustomHTTPException(400, message="That code is not valid")
    plaintext, stored = totp_service.generate_recovery_codes()
    db_user.totp_recovery_codes = stored
    await session.commit()
    return TotpEnableResponse(enabled=True, recovery_codes=plaintext)


@router.post("/2fa/disable", summary="Turn two-factor off")
@limiter.limit("5/minute", key_func=account_or_address)
async def totp_disable(
    request: Request, body: TotpEnableRequest, session: SessionDep, user: DependsAuth
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
    if not await totp_service.accept_code(session, db_user, body.code):
        raise CustomHTTPException(400, message="That code is not valid")

    db_user.totp_enabled = False
    db_user.totp_secret = None
    db_user.totp_recovery_codes = None
    await session.commit()
    return {"message": "Two-factor disabled"}
