"""The profiles' rate limits (the contract's numbers), on the shared limiter
(app.core.rate_limit: Redis when set, else this process; off with it).

- /profiles/*: 120 a minute per address; /profiles/by-code: 30 a minute per
  address, so codes cannot be walked. The address is the visitor's: for a
  page the site's own server renders, the one it names behind its token
  (app.core.site), else the connection's.
- Saves (POST /network): 60 a minute and 300 an hour per person, counted on
  every try, so a code cannot be guessed through saves either.

A refusal is 429 RATE_LIMITED with errors.retry_after in seconds.
"""
from __future__ import annotations

import time
from functools import lru_cache

from limits import parse

from app.core.rate_limit import limiter
from app.response import CustomHTTPException

PROFILE_READS = "120/minute"
CODE_READS = "30/minute"
SAVES = ("60/minute", "300/hour")


@lru_cache(maxsize=None)
def _item(rate: str):
    return parse(rate)


def _hit(rate: str, scope: str, who: str) -> None:
    item = _item(rate)
    if limiter.limiter.hit(item, scope, who):
        return
    try:
        reset_at, _ = limiter.limiter.get_window_stats(item, scope, who)
        wait = max(1, int(reset_at - time.time()) + 1)
    except Exception:  # noqa: BLE001 - the refusal is the answer; the wait is a courtesy
        wait = 60
    raise CustomHTTPException(
        429, message="Too many tries in a short time. Wait a moment and try again.",
        error_code="RATE_LIMITED", errors={"retry_after": wait},
    )


def visitor(request) -> str:
    from app.core import site
    from app.core.net import client_address

    return site.visitor_ip(request) or client_address(request) or "unknown"


def profile_read(request) -> None:
    if limiter.enabled:
        _hit(PROFILE_READS, "profiles:read", visitor(request))


def code_read(request) -> None:
    if limiter.enabled:
        _hit(CODE_READS, "profiles:code", visitor(request))


def save(user_id: int) -> None:
    if limiter.enabled:
        for rate in SAVES:
            _hit(rate, f"network:save:{rate}", str(user_id))
