"""The profiles' rate limits (the contract's numbers), on the shared limiter
(app.core.rate_limit: Redis when set, else this process; off with it).

- /profiles/*: 120 a minute per address; /profiles/by-code: 30 a minute per
  address, so codes cannot be walked.
- The website's own server (a valid X-Site-Token, app.core.site) reads from
  a few shared addresses for every visitor, so its reads are not counted
  against its address. Each counts in the site's bucket, the same one its
  other public reads spend (SITE_RATE_LIMIT, app.core.rate_limit), and,
  when it names the visitor (X-Visitor-IP, believed only behind that
  token), against that visitor too, at the route's own rate. Without a
  valid token X-Visitor-IP means nothing: the caller's address counts.
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
    raise _refused(wait)


def _refused(wait: int = 60) -> CustomHTTPException:
    return CustomHTTPException(
        429, message="Too many tries in a short time. Wait a moment and try again.",
        error_code="RATE_LIMITED", errors={"retry_after": wait},
    )


def _read(request, rate: str, scope: str) -> None:
    """One public read: per address, or, from the site's own server, per
    visitor it names and in the site's bucket."""
    from app.core import site
    from app.core.net import client_address
    from app.core.rate_limit import site_bucket_spent

    if not limiter.enabled:
        return
    bucket = site.token_id(request)
    if bucket is None:
        _hit(rate, scope, client_address(request) or "unknown")
        return
    visitor = site.visitor_ip(request)
    if visitor:
        _hit(rate, scope, visitor)
    if site_bucket_spent(bucket):
        raise _refused()


def profile_read(request) -> None:
    _read(request, PROFILE_READS, "profiles:read")


def code_read(request) -> None:
    _read(request, CODE_READS, "profiles:code")


def save(user_id: int) -> None:
    if limiter.enabled:
        for rate in SAVES:
            _hit(rate, f"network:save:{rate}", str(user_id))
