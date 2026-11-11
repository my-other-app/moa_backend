"""The one door to the platforms' APIs, and what their refusals mean.

`client()` is every call's httpx client; `TRANSPORT` is what it sends
through -- None is the network, and the suite puts a fake platform there
(tests/conftest.py makes every call fail as an unreachable host would unless
a test stands in for the platform). Nothing here logs a token, a code or a
body we sent: an error carries the platform's status, its code and its own
short words about the call.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

from app.config import settings

#: What calls go through: None is the network. Tests put a fake platform here.
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

# What a refusal means for the post, and so what the club is told to do.
AUTH = "auth"              # the token is dead or revoked: reconnect
PERMISSION = "permission"  # signed in, but not allowed (not a Page admin, a permission missing)
RATE = "rate"              # a limit: try again later by itself
TRANSIENT = "transient"    # the platform is down or slow: retry with growing gaps
IMAGE = "image"            # the picture was refused: fix it
REFUSED = "refused"        # the platform said no to this post (duplicate, policy): share by hand
UNKNOWN = "unknown"        # sent, but no answer came back: look it up before sending again
BILLING = "billing"        # X's pay-per-use account cannot pay

_TOKENISH = re.compile(r"(?i)(access_token|refresh_token|client_secret|code|token)=([^&\s\"']+)")


def client(timeout: Optional[float] = None) -> httpx.AsyncClient:
    seconds = float(timeout or settings.SOCIAL_TIMEOUT_SECONDS or 20.0)
    return httpx.AsyncClient(timeout=httpx.Timeout(seconds, connect=5.0), transport=TRANSPORT, follow_redirects=False)


def scrub(text: Optional[str]) -> str:
    """A platform's words, safe to keep: no `token=...` that may echo a URL we called."""
    return _TOKENISH.sub(lambda m: f"{m.group(1)}=[filtered]", str(text or ""))[:300]


class PlatformError(Exception):
    """A platform said no, or could not be asked. `kind` is one of the
    constants above; `retry_after` (seconds) when the platform said when."""

    def __init__(self, kind: str, message: str, *, code: Optional[str] = None, status: int = 0,
                 retry_after: Optional[float] = None):
        super().__init__(scrub(message))
        self.kind = kind
        self.message = scrub(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return f"PlatformError({self.kind!r}, {self.code!r}, {self.status})"


class LookupUnsupported(Exception):
    """The platform gives no way to find a post we may have made (LinkedIn's
    member posts need a permission LinkedIn keeps to approved partners)."""


def unreachable(exc: Exception, *, sent: bool = False) -> PlatformError:
    """A network failure. `sent`: the request may have reached the platform
    (a read timeout, a dropped connection after the body went), so its
    outcome is unknown rather than a plain failure."""
    if sent and isinstance(exc, (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)):
        return PlatformError(UNKNOWN, "The platform did not answer after the post was sent.", code="no_answer")
    return PlatformError(TRANSIENT, f"The platform could not be reached ({exc.__class__.__name__}).", code="unreachable")


def retry_after(r: httpx.Response) -> Optional[float]:
    raw = r.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def by_status(r: httpx.Response, message: str, code: Optional[str] = None) -> PlatformError:
    """The kind a bare HTTP status says, when the body says nothing better."""
    s = r.status_code
    if s == 401:
        return PlatformError(AUTH, message, code=code or "unauthorized", status=s)
    if s == 402:
        return PlatformError(BILLING, message, code=code or "payment_required", status=s)
    if s == 403:
        return PlatformError(PERMISSION, message, code=code or "forbidden", status=s)
    if s == 429:
        return PlatformError(RATE, message, code=code or "rate_limited", status=s, retry_after=retry_after(r))
    if s >= 500:
        return PlatformError(TRANSIENT, message, code=code or f"http_{s}", status=s)
    return PlatformError(REFUSED, message, code=code or f"http_{s}", status=s)


def json_of(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
