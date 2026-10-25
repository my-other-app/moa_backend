"""Sign in with Apple's refresh token: kept at sign-in, revoked when the
account is deleted.

App Review requires an app offering Sign in with Apple to revoke the user's
token when they delete their account (guideline 5.1.1(v)). The app sends
Apple's one-time `authorization_code` with every sign-in; the server used to
verify the identity token and drop the code, so there was nothing to revoke.

At sign-in the code is exchanged for a refresh token (POST /auth/token), which
is kept encrypted on the account (users.apple_refresh_token, Fernet under
APPLE_TOKENS_SECRET -- app/core/keyring.py). When the account deletes itself
the token is revoked (POST /auth/revoke). Both calls authenticate with a
client secret: a short-lived ES256 JWT signed with the Sign in with Apple key
(APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY).

Both are best effort: a sign-in or a deletion never fails because Apple did
not answer. What failed is logged, never the token or the key.
"""
from __future__ import annotations

import logging
import time

import httpx
import jwt

from app.config import settings
from app.core.keyring import Keyring, Unavailable as _Unavailable

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
TOKEN_URL = "https://appleid.apple.com/auth/token"
REVOKE_URL = "https://appleid.apple.com/auth/revoke"
#: How long a client secret is good for. Apple allows up to six months; one
#: is made per call, so five minutes is plenty.
SECRET_LIFETIME_SECONDS = 300


class Unavailable(_Unavailable):
    """No usable APPLE_TOKENS_SECRET, or a value it cannot decrypt."""


_keys = Keyring("APPLE_TOKENS_SECRET", Unavailable, what="Sign in with Apple tokens")


def _private_key() -> str:
    """The .p8 key's PEM text. An env file holds it on one line, so a literal
    \\n is read as a line break."""
    return (settings.APPLE_PRIVATE_KEY or "").strip().replace("\\n", "\n")


def configured() -> bool:
    """Everything the exchange and the revocation need is set."""
    return bool(
        settings.APPLE_CLIENT_ID
        and settings.APPLE_TEAM_ID
        and settings.APPLE_KEY_ID
        and _private_key()
        and _keys.available()
    )


def client_secret(now: float | None = None) -> str:
    """The JWT Apple takes as client_secret, signed with the team's key."""
    issued = int(now if now is not None else time.time())
    return jwt.encode(
        {
            "iss": settings.APPLE_TEAM_ID,
            "iat": issued,
            "exp": issued + SECRET_LIFETIME_SECONDS,
            "aud": APPLE_ISSUER,
            "sub": settings.APPLE_CLIENT_ID,
        },
        _private_key(),
        algorithm="ES256",
        headers={"kid": settings.APPLE_KEY_ID},
    )


async def _post(url: str, data: dict) -> httpx.Response:
    """One form POST to Apple. Tests replace this; nothing here calls Apple
    from the suite."""
    async with httpx.AsyncClient(timeout=settings.APPLE_TIMEOUT_SECONDS) as client:
        return await client.post(url, data=data, headers={"Accept": "application/json"})


async def exchange(authorization_code: str) -> str | None:
    """Apple's refresh token for this one-time code, encrypted for keeping;
    None when it could not be had (not configured, Apple refused or did not
    answer). Never raises."""
    if not authorization_code or not configured():
        return None
    try:
        response = await _post(
            TOKEN_URL,
            {
                "client_id": settings.APPLE_CLIENT_ID,
                "client_secret": client_secret(),
                "code": authorization_code,
                "grant_type": "authorization_code",
            },
        )
        if response.status_code != 200:
            # Apple's error is a code like invalid_grant, never a secret.
            logger.warning("Apple refused the authorization code (%s): %s", response.status_code, _error_of(response))
            return None
        token = (response.json() or {}).get("refresh_token")
        if not token:
            logger.warning("Apple's token response had no refresh token")
            return None
        return _keys.encrypt(token)
    except Exception:
        logger.warning("Sign in with Apple's code could not be exchanged", exc_info=True)
        return None


async def revoke(stored: str | None) -> bool:
    """Revoke the kept refresh token (as stored: encrypted). True when Apple
    accepted it. Never raises: an account's deletion goes on regardless."""
    if not stored:
        return False
    if not configured():
        logger.warning("A Sign in with Apple token was not revoked: Apple's settings are not configured")
        return False
    try:
        token = _keys.decrypt(stored)
        response = await _post(
            REVOKE_URL,
            {
                "client_id": settings.APPLE_CLIENT_ID,
                "client_secret": client_secret(),
                "token": token,
                "token_type_hint": "refresh_token",
            },
        )
        if response.status_code != 200:
            logger.warning("Apple refused to revoke a token (%s): %s", response.status_code, _error_of(response))
            return False
        logger.info("Sign in with Apple token revoked")
        return True
    except Exception:
        logger.warning("Sign in with Apple's token could not be revoked", exc_info=True)
        return False


def _error_of(response: httpx.Response) -> str:
    try:
        return str((response.json() or {}).get("error") or "")[:100]
    except ValueError:
        return ""
