import functools
import importlib
import logging
from typing import Optional

from limits import RateLimitItem, parse_many
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit

from app.config import settings
from app.core import site

logger = logging.getLogger(__name__)

#: The routers whose GET routes are "the public site's reads": what the site's
#: own server, with a valid X-Site-Token, reads in its own bucket rather than
#: its shared address's. Public data only, the same for anyone who asks.
#: Nothing else -- sign-in, registration, posts, likes, invites -- is ever
#: counted there: those keep their per-address limits whatever a request carries.
SITE_READ_ROUTERS = (
    ("app.api.discover.router", "router"),
    ("app.api.subscriptions.router", "public_router"),
)
#: And single public reads on routers that also hold other things.
SITE_READ_ENDPOINTS = frozenset({
    "app.api.interests.router.list_interests",
    "app.api.interests.router.list_topics",
    "app.api.interests.router.get_topic",
})
SITE_SCOPE = "site-reads"
_DEFAULT_SITE_LIMIT = "3000/minute"


class SiteAwareLimiter(Limiter):
    """slowapi's limiter, with one exception: a public read from the site's
    own server (app/core/site.py) counts against the site's bucket,
    settings.SITE_RATE_LIMIT, shared by every one of its reads and every
    visitor it renders for, instead of the per-address limit of the route.

    A bucket rather than an exemption, so the site is still bounded: a site
    deploy stuck in a render loop, or a token that leaked, can cost the API
    at most SITE_RATE_LIMIT of public reads, never an unlimited stream and
    never a looser limit on anything else.

    Hooked in at `_check_request_limit`, the one method every `@limit`-ed
    route calls (slowapi is pinned in requirements.txt; tests/test_site_requests.py
    fails if the hook stops being called).
    """

    def _check_request_limit(self, request, endpoint_func, in_middleware: bool = True) -> None:
        if not in_middleware and endpoint_func is not None and self.enabled:
            bucket = site.token_id(request)
            if bucket is not None and _is_site_read(request, endpoint_func):
                self._hit_site_bucket(request, bucket)
                return
        super()._check_request_limit(request, endpoint_func, in_middleware)

    def _hit_site_bucket(self, request, bucket: str) -> None:
        args = [bucket, SITE_SCOPE]
        if self._key_prefix:
            args = [self._key_prefix, *args]
        for item in _site_limits(settings.SITE_RATE_LIMIT or _DEFAULT_SITE_LIMIT):
            request.state.view_rate_limit = (item, args)
            if not self.limiter.hit(item, *args):
                self.logger.warning("ratelimit %s (%s) exceeded for the site's reads", item, bucket)
                raise RateLimitExceeded(Limit(item, lambda: bucket, SITE_SCOPE, False, None, None, None, 1, False))


@functools.lru_cache(maxsize=4)
def _site_limits(text: str) -> tuple[RateLimitItem, ...]:
    try:
        return tuple(parse_many(text))
    except ValueError:
        logger.error("SITE_RATE_LIMIT %r is not a rate limit; using %s", text, _DEFAULT_SITE_LIMIT)
        return tuple(parse_many(_DEFAULT_SITE_LIMIT))


def _name(func) -> str:
    return f"{func.__module__}.{func.__name__}"


_site_reads: Optional[frozenset] = None


def site_read_names() -> frozenset:
    """Every GET route on SITE_READ_ROUTERS, and SITE_READ_ENDPOINTS, by the
    name slowapi knows each by. Worked out on first use, when every route has
    long been declared."""
    global _site_reads
    if _site_reads is None:
        names = set(SITE_READ_ENDPOINTS)
        for module, attr in SITE_READ_ROUTERS:
            router = getattr(importlib.import_module(module), attr)
            for route in router.routes:
                endpoint = getattr(route, "endpoint", None)
                if endpoint is not None and "GET" in (getattr(route, "methods", None) or ()):
                    names.add(_name(endpoint))
        _site_reads = frozenset(names)
    return _site_reads


def _is_site_read(request, endpoint_func) -> bool:
    return request.method in ("GET", "HEAD") and _name(endpoint_func) in site_read_names()


def storage_uri() -> str:
    """Where the counts live: APP_RATE_LIMIT_STORAGE when it names a store,
    else Redis when APP_REDIS_URL is set, else this process's memory."""
    uri = (settings.RATE_LIMIT_STORAGE or "").strip() or "memory://"
    if uri == "memory://" and (settings.REDIS_URL or "").strip():
        return settings.REDIS_URL.strip()
    return uri


def storage_options(uri: str) -> dict:
    """For Redis: short timeouts (a limit is checked on the event loop, on
    every limited request) and keys of our own."""
    if not uri.startswith(("redis://", "rediss://", "redis+unix://")):
        return {}
    timeout = float(settings.REDIS_TIMEOUT)
    return {"socket_timeout": timeout, "socket_connect_timeout": timeout, "key_prefix": "moa:limits"}


class _FailSafe:
    """The limiter's counter, falling back to this process's memory the
    moment the shared store fails (Redis down, a timeout), so a request is
    limited per process rather than answered with a 500. slowapi's own
    recovery check puts the shared store back once it answers again.

    slowapi falls back only inside its own route check; the site's bucket
    and the live room's limits call the counter directly
    (`limiter.limiter.hit`), and would raise. Every caller goes through this."""

    def __init__(self, owner: "SiteAwareLimiter"):
        self._owner = owner

    def __getattr__(self, name: str):
        owner = self._owner

        def call(*args, **kwargs):
            fallback = owner._fallback_limiter
            if owner._storage_dead and fallback is not None:
                return getattr(fallback, name)(*args, **kwargs)
            try:
                return getattr(owner._limiter, name)(*args, **kwargs)
            except Exception:  # noqa: BLE001
                if fallback is None:
                    raise
                if not owner._storage_dead:
                    logger.warning("rate limit storage unreachable; counting in this process until it answers", exc_info=True)
                owner._storage_dead = True
                return getattr(fallback, name)(*args, **kwargs)

        return call


class SharedLimiter(SiteAwareLimiter):
    @property
    def limiter(self):
        return _FailSafe(self)


# In memory by default: per process, which is exact while WORKERS=1. With
# Redis (APP_REDIS_URL, or a redis:// APP_RATE_LIMIT_STORAGE) every worker
# and host shares one budget instead of each keeping its own, and falls back
# to its own memory while Redis does not answer.
_uri = storage_uri()
limiter = SharedLimiter(
    key_func=get_remote_address, storage_uri=_uri, storage_options=storage_options(_uri), in_memory_fallback_enabled=True,
)


def account_or_address(request) -> str:
    """The limiter's key for an endpoint only a signed-in account calls: that
    account, from its own (verified) token; the address when there is none.

    The address alone is a poor key behind this deployment's proxy: nginx on
    the host reaches the container through Docker's published port, so the
    address the app sees can be the same bridge address for every caller,
    and a per-address limit becomes one budget the whole platform shares --
    one person could spend it for everyone.
    """
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("access_token")
    if token:
        try:
            from app.core.auth.jwt import decode_jwt_token

            user_id = decode_jwt_token(token).get("user_id")
            if user_id:
                return f"user:{user_id}"
        except Exception:
            pass
    return get_remote_address(request)
