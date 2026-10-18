"""The public website's own server, told apart from everyone else.

myotherapp.in is rendered on Vercel, and its server reads this API from a
handful of Vercel's shared addresses. Every visitor's page is fetched from
the same few IPs, so without a way to tell the site apart they would all
share one per-address rate-limit budget (429s under ordinary traffic), and
every visitor would be placed in Vercel's datacentre.

The site's server says who it is with `X-Site-Token: <one of
settings.SITE_TOKENS>`. With a valid token, and only then:

  * its public reads count against one bucket of its own, SITE_RATE_LIMIT,
    instead of the shared address's (app/core/rate_limit.py). Only the
    public reads: sign-in, registration, posting and every other limited
    route keep their per-address limits whatever the request carries;
  * `X-Visitor-IP: <the visitor's address>` is believed for placing the
    visitor ("near you", /discover/locate; app/api/discover/geo.py). It is
    never used for a rate-limit key, a sign-in record or an audit row.

Without a valid token neither header means anything: the request is limited
and placed by its own address exactly as before.

Tokens are compared in constant time, on SHA-256 digests so that even the
length of a guess tells nothing, and every configured token is compared with
no early exit, so which one matched does not show in the timing either. A
token shorter than MIN_TOKEN_LENGTH is ignored: a guessable token would hand
anyone the site's bucket and its say over where a visitor is.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-site-token"
VISITOR_HEADER = "x-visitor-ip"
MIN_TOKEN_LENGTH = 32


@functools.lru_cache(maxsize=8)
def _digests(raw: str) -> tuple[bytes, ...]:
    """The configured tokens' digests, worked out once per value of the setting."""
    out = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if len(token) < MIN_TOKEN_LENGTH:
            logger.warning("a site token shorter than %d characters is ignored", MIN_TOKEN_LENGTH)
            continue
        out.append(hashlib.sha256(token.encode()).digest())
    return tuple(out)


def _headers(request):
    return getattr(request, "headers", None) or {}


def token_id(request) -> Optional[str]:
    """A name for the valid site token the request carries ("site:3f9a1c2b4d5e",
    the start of its digest: safe to log), or None for any other request."""
    given = _headers(request).get(TOKEN_HEADER)
    if not given:
        return None
    digests = _digests(settings.SITE_TOKENS or "")
    if not digests:
        return None
    probe = hashlib.sha256(given.strip().encode()).digest()
    found = None
    for digest in digests:
        if hmac.compare_digest(probe, digest):
            found = digest
    return f"site:{found.hex()[:12]}" if found is not None else None


def visitor_ip(request) -> Optional[str]:
    """The address in X-Visitor-IP, when a request with a valid site token
    gives one; None otherwise (no token, a wrong one, or not an address)."""
    raw = _headers(request).get(VISITOR_HEADER)
    if not raw or token_id(request) is None:
        return None
    # One address; were the site to pass a whole X-Forwarded-For, its first is the visitor.
    first = raw.split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(first))
    except ValueError:
        return None
