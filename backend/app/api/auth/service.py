import asyncio
import re
import threading
import time
from datetime import timedelta
import logging
import httpx
import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from google.auth import exceptions as google_exceptions
from google.oauth2 import id_token
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


def _refuse_if_suspended(user) -> None:
    """Said at the door, as the password sign-in does: a suspended account
    was handed tokens here and refused on its first request."""
    if getattr(user, "is_active", True) is False:
        raise CustomHTTPException(status_code=403, message="This account has been suspended", error_code="ACCOUNT_SUSPENDED")


class _GoogleCerts:
    """The transport verify_oauth2_token fetches Google's signing
    certificates through: kept for as long as Google's Cache-Control max-age
    says (an hour if it says nothing), fetched again only after.

    It fetched them on every sign-in, with a fresh connection, on the event
    loop. Called from the worker thread verification runs on, so a fetch here
    never holds the loop; one lock, so a burst of sign-ins at the moment the
    certificates go stale makes one fetch."""

    DEFAULT_TTL = 3600.0

    class _Response:
        def __init__(self, status: int, data: bytes, headers=None):
            self.status, self.data, self.headers = status, data, headers or {}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, bytes]] = {}

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()

    @staticmethod
    def _max_age(cache_control: str | None) -> float:
        m = re.search(r"max-age=(\d+)", cache_control or "")
        return float(m.group(1)) if m else _GoogleCerts.DEFAULT_TTL

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        with self._lock:
            hit = self._cache.get(url)
            if hit is not None and hit[0] > time.monotonic():
                return self._Response(200, hit[1])
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                self._cache[url] = (time.monotonic() + self._max_age(r.headers.get("cache-control")), r.content)
            return self._Response(r.status_code, r.content, dict(r.headers))


_google_certs = _GoogleCerts()


async def _verify_google(id_token_str: str, client_id: str | list[str]) -> dict:
    """Google's checks (signature, audience, issuer, expiry) on a worker
    thread, against the cached certificates. `client_id` may list several
    accepted audiences."""
    return await asyncio.to_thread(id_token.verify_oauth2_token, id_token_str, _google_certs, client_id)


async def google_identity(id_token_str: str, client_id: str | list[str]) -> dict:
    """The claims of a Google ID token Google vouches for, with `client_id`
    (or one of them) as its audience (_verify_google). One it would not vouch for is 401
    INVALID_GOOGLE_TOKEN; Google's certificates out of reach, 503."""
    try:
        return await _verify_google(id_token_str, client_id)
    except (google_exceptions.TransportError, httpx.HTTPError):
        logger.warning("Google's certificates could not be fetched", exc_info=True)
        raise CustomHTTPException(status_code=503, message="Google sign-in is unavailable right now. Try again in a minute.", error_code="GOOGLE_UNAVAILABLE")
    except (ValueError, google_exceptions.GoogleAuthError, jwt.PyJWTError):
        # A wrong issuer is a GoogleAuthError, not a ValueError: it was a 500.
        raise CustomHTTPException(status_code=401, message="Invalid authentication credentials", error_code="INVALID_GOOGLE_TOKEN")


async def dashboard_google_email(id_token_str: str) -> str:
    """The address, lower-cased, on a Google ID token from the club
    dashboard's or the main portal's button: checked as /auth/google checks
    the web's (audience APP_GOOGLE_WEB_CLIENT_ID), and Google must have
    verified the address."""
    if not settings.GOOGLE_WEB_CLIENT_ID:
        raise CustomHTTPException(status_code=503, message="Google sign-in is not set up on this server.", error_code="GOOGLE_UNAVAILABLE")
    idinfo = await google_identity(id_token_str, settings.GOOGLE_WEB_CLIENT_ID)
    email = (idinfo.get("email") or "").strip().lower()
    if not email or str(idinfo.get("email_verified")).lower() != "true":
        raise CustomHTTPException(status_code=401, message="Google has not verified this account's email address.", error_code="INVALID_GOOGLE_TOKEN")
    return email


async def dashboard_account(session: AsyncSession, email: str, scope: str):
    """Who a Google address signs in as on a dashboard, as `(member, account)`.

    scope "club", in /auth/token's order: an active club team member with
    the address (member, None); else the club's own login account (None,
    account), approved or not. scope "admin": an admin account. Nothing
    else: an app user, a guest or another type is (None, None), as is no
    account at all. Google vouched for the address, so no password is asked."""
    from app.core.auth.authentication import find_account, find_club_account

    if scope == "club":
        from app.api.club_access import service as club_access

        member = await club_access.active_member(session, email)
        if member is not None:
            return member, None
        return None, await find_club_account(session, email)
    if scope == "admin":
        return None, await find_account(session, email, UserTypes.admin)
    return None, None


async def google_signin(
    session: AsyncSession, id_token_str: str, platform: str = "web"
):
    try:
        client_ids = {
            "web": settings.GOOGLE_WEB_CLIENT_ID,
            "android": settings.GOOGLE_ANDROID_CLIENT_ID,
            "ios": settings.GOOGLE_IOS_CLIENT_ID,
        }

        if not client_ids.get(platform):
            raise CustomHTTPException(status_code=400, message=f"Invalid platform: {platform}", error_code="INVALID_PLATFORM")

        # Any of our own clients is accepted, whichever platform the app
        # names: the app says "web" on both phones (its serverClientId), but
        # Google gives an iPhone's token the iOS client as its audience.
        audiences = list(dict.fromkeys([client_ids[platform], *(c for c in client_ids.values() if c)]))
        idinfo = await google_identity(id_token_str, audiences)

        if not idinfo.get("email_verified"):
            raise CustomHTTPException(
                status_code=401, message="Google account email is not verified", error_code="EMAIL_NOT_VERIFIED"
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
            _refuse_if_suspended(user)
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

    except (CustomHTTPException, HTTPException):
        # Refusals made above on purpose (an unverified Google address, an
        # unknown platform) are answers, not crashes; they used to come back 500.
        raise
    except ValueError:
        raise CustomHTTPException(
            status_code=401, message="Invalid authentication credentials", error_code="INVALID_GOOGLE_TOKEN"
        )
    except Exception:
        logger.exception("Google signin error")
        raise CustomHTTPException(status_code=500, message="Internal Server Error")


APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
_apple_keys = None


def _apple_jwks():
    """One key client for the process, keeping Apple's keys for an hour: it
    was a new client, and a fetch of the keys, on every sign-in. Made on first
    use (and again if the class it was made from is swapped, as tests do)."""
    global _apple_keys
    if _apple_keys is None or not isinstance(_apple_keys, jwt.PyJWKClient):
        _apple_keys = jwt.PyJWKClient(APPLE_KEYS_URL, cache_keys=True, lifespan=3600, timeout=5)
    return _apple_keys


def _verify_apple(identity_token: str) -> dict:
    """Apple's signature, audience and issuer checks. Blocking (a key fetch
    when the cache is cold): run on a worker thread."""
    signing_key = _apple_jwks().get_signing_key_from_jwt(identity_token)
    return jwt.decode(
        identity_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.APPLE_CLIENT_ID if settings.APPLE_CLIENT_ID else None,
        issuer="https://appleid.apple.com",
        options={"verify_aud": bool(settings.APPLE_CLIENT_ID)},
    )


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
    try:
        decoded = await asyncio.to_thread(_verify_apple, identity_token)

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
            _refuse_if_suspended(user)
            # Apple signed the address: see the matching call in google_signin.
            await claim_for_provider(session, user, method="apple")

        # Merge any guest accounts with matching email
        await merge_guest_into_user(session, app_user=user)
        # Apple's refresh token, so deleting the account can revoke it. Best
        # effort: None (not configured, or Apple did not answer) keeps
        # whatever was kept before, and the sign-in goes on.
        from app.api.auth import apple_tokens

        kept = await apple_tokens.exchange(authorization_code)
        if kept:
            user.apple_refresh_token = kept
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
    except jwt.PyJWKClientConnectionError:
        logger.warning("Apple's keys could not be fetched", exc_info=True)
        raise CustomHTTPException(status_code=503, message="Sign in with Apple is unavailable right now. Try again in a minute.", error_code="APPLE_UNAVAILABLE")
    except jwt.PyJWKClientError:
        # Signed with a key Apple does not have.
        raise CustomHTTPException(status_code=401, message="Invalid Apple authentication token")
    except Exception as e:
        logger.exception(f"Apple signin error: {e}")
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
        # A new session's first refresh token: sessions.rotate moves it on.
        gen=0 if sid else None,
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

