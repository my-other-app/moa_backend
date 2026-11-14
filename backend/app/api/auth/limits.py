"""Sign-in and refresh limits sized for a venue.

Every limit on /auth was per address. A venue's Wi-Fi, a college network or
a mobile carrier's NAT puts hundreds of phones behind one address, so the
refresh limit of 20 a minute was 20 refreshes a minute for a whole hall:
past it the app's refresh answered 429, and every screen that needed a
fresh token failed until the minute rolled over. The live room had been
rebuilt for exactly this (app/api/live/limits.py); sign-in had not.

Now, as there:

- a refresh is limited per session -- the session id inside the refresh
  token, the same however often it rotates (AUTH_REFRESH_PER_SESSION) -- so
  one phone stuck in a refresh loop meets its own limit and nobody else's;
- each route keeps a per-address limit, sized as a ceiling for a full hall
  rather than for one person: AUTH_REFRESH_PER_ADDRESS for refresh,
  AUTH_SIGN_IN_PER_ADDRESS for Google and Apple, AUTH_PASSWORD_PER_ADDRESS
  for the password and dashboard sign-ins, where guessing one account's
  password is stopped by the per-account lockout (LOGIN_LOCKED).

A rule at the edge (Cloudflare) on /auth has to be at least as loose as
these ceilings: one sized per person brings the shared-address problem back
in front of the API.

The counts live in the limiter's shared store (app.core.rate_limit) and
switch off with it.
"""
from __future__ import annotations

import time
from functools import lru_cache

from limits import parse

from app.config import settings
from app.core.rate_limit import limiter
from app.response import CustomHTTPException


def refresh_per_address() -> str:
    return settings.AUTH_REFRESH_PER_ADDRESS


def sign_in_per_address() -> str:
    return settings.AUTH_SIGN_IN_PER_ADDRESS


def password_per_address() -> str:
    return settings.AUTH_PASSWORD_PER_ADDRESS


@lru_cache(maxsize=16)
def _item(rate: str):
    return parse(rate)


def refresh_session(session_key: str) -> None:
    """Count one refresh of this session (its id, or `user:<id>` for a token
    from before sessions); 429 RATE_LIMITED once AUTH_REFRESH_PER_SESSION is spent."""
    if not limiter.enabled:
        return
    item = _item(settings.AUTH_REFRESH_PER_SESSION)
    if limiter.limiter.hit(item, "auth:refresh:session", session_key):
        return
    try:
        reset_at, _ = limiter.limiter.get_window_stats(item, "auth:refresh:session", session_key)
        wait = max(1, int(reset_at - time.time()) + 1)
    except Exception:  # noqa: BLE001 - the refusal is the answer; the wait is a courtesy
        wait = 60
    raise CustomHTTPException(
        429, message="This session refreshed too often. Wait a moment and try again.",
        error_code="RATE_LIMITED", errors={"retry_after": wait},
    )
