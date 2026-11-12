"""Who is calling /partner/v1, what they may do, and how often.

`Authorization: Bearer <key or access token>`:

- a club's API key (`moa_live_...`, `moa_test_...`): not revoked, not
  expired, from an address its allowlist names (when it has one);
- a partner app's access token (`moa_pat_...`): its app not suspended, its
  connection not ended. A connection's token acts for its club; the app's
  own token (client credentials) acts for no club -- or, with
  `MOA-Club-Id: <club id>`, for that one of its connected clubs, with that
  connection's scopes (how a partner works for the clubs it created).

The club must still exist, be approved and be allocated API access, now:
none of that is trusted from when the key was made. The caller acts as the
club's own account, within the scopes it holds -- for a partner, the scopes
the club granted that the app may still ask for.

Then, in order: the rate limit (per client and club: reads and writes a
minute by the club's tier, or the app's for its own token; 429 RATE_LIMITED
with Retry-After), the change's origin for webhooks (app/core/api_origin.py),
and the Idempotency-Key (idempotency.py).

`require_scope(*names)` is the dependency every route takes: a caller with
none of the scopes named is 403 FORBIDDEN_SCOPE. Agent B's webhook routes
take the same (app/api/webhooks_out/partner.py's make_router).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from fastapi import Depends, Request
from limits import RateLimitItemPerMinute

from app.api.partner import allocation, idempotency
from app.api.partner import keys as keys_mod
from app.api.partner import oauth
from app.api.partner import scopes as catalogue
from app.api.partner.common import aware, now
from app.api.partner.errors import PartnerError
from app.config import settings
from app.core import api_origin
from app.core.net import client_address
from app.db.core import SessionDep

logger = logging.getLogger(__name__)

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: With an app's own token (client credentials): the connected club it acts for.
CLUB_HEADER = "MOA-Club-Id"
_TOUCH_EVERY = timedelta(minutes=1)


@dataclass
class PartnerCaller:
    """The contract's PartnerCaller(club_id, client_id, client_type, scopes,
    livemode), and what this module needs besides."""

    club_id: Optional[int]
    client_id: str
    client_type: str  # api_key | partner_app
    scopes: frozenset
    livemode: bool
    name: str = ""
    club: object = None
    owner: object = None
    key: object = None
    app: object = None
    connection: object = None
    rate_limit: dict = field(default_factory=dict)
    session: object = None
    request: object = None

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def contact(self) -> bool:
        """Whether attendees' names, contacts and answers may be returned."""
        return "attendees:read" in self.scopes

    def require(self, *names: str) -> None:
        if names and not any(n in self.scopes for n in names):
            raise PartnerError(
                "FORBIDDEN_SCOPE", f"This needs the {' or '.join(names)} scope, which this credential does not have.",
                details={"needed": list(names), "granted": sorted(self.scopes, key=catalogue.ALL.index)},
            )

    def require_club(self):
        if self.club is None:
            raise PartnerError("FORBIDDEN_SCOPE", "An app's own token acts for no club: use the token a club's connection gave you.")
        return self.club

    def origin(self) -> api_origin.Origin:
        return api_origin.Origin(type=self.client_type, client_id=self.client_id)

    def audit_details(self, **more) -> dict:
        return {
            "via": "partner_api", "client_type": self.client_type, "client_id": self.client_id, "client_name": self.name,
            "livemode": self.livemode, **more,
        }


def _bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    if header[:7].lower() != "bearer ":
        return None
    return header[7:].strip() or None


def _unauthenticated(message: str) -> PartnerError:
    return PartnerError("UNAUTHENTICATED", message, headers={"WWW-Authenticate": 'Bearer realm="partner"'})


async def _club_and_owner(session, club_id: int):
    """The club as it is now, and the account every change is made as."""
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    club = await session.get(Clubs, club_id)
    if club is None or club.is_deleted:
        raise PartnerError("CLUB_NOT_ACTIVE", "This club no longer exists on MyOtherApp.")
    if not club.is_verified:
        raise PartnerError("CLUB_NOT_ACTIVE", "This club is waiting for the MyOtherApp team's approval.")
    owner = await session.get(Users, club.user_id) if club.user_id else None
    if owner is None or owner.is_deleted or getattr(owner, "is_active", True) is False:
        raise PartnerError("CLUB_NOT_ACTIVE", "This club's account is closed right now.")
    alloc = await allocation.of(session, club.id)
    if not alloc.allocated:
        raise PartnerError("NOT_ALLOCATED", "API access isn't turned on for this club. The club can ask the MyOtherApp team.")
    return club, owner, alloc


async def _from_key(session, request, raw: str) -> PartnerCaller:
    row = await keys_mod.find(session, raw)
    if row is None or row.revoked_at is not None:
        raise _unauthenticated("This API key is not valid, or was revoked.")
    if keys_mod.expired(row):
        raise _unauthenticated("This API key has expired.")
    address = client_address(request)
    if not keys_mod.ip_allowed(row.ip_allowlist, address):
        raise PartnerError("IP_NOT_ALLOWED", f"This key may not be used from {address or 'this address'}.")
    club, owner, alloc = await _club_and_owner(session, row.club_id)
    await keys_mod.touch(session, row, address)
    return PartnerCaller(
        club_id=club.id, client_id=row.client_id, client_type="api_key", scopes=frozenset(catalogue.parse(row.scopes)),
        livemode=bool(row.livemode), name=row.name, club=club, owner=owner, key=row, rate_limit=alloc.rate_limit,
    )


async def _from_token(session, request, raw: str) -> PartnerCaller:
    found = await oauth.access_token(session, raw)
    if found is None:
        raise _unauthenticated("This access token is not valid, has expired or was revoked.")
    token, app, conn = found
    if app.status != "active":
        raise PartnerError("APP_SUSPENDED", "This app is suspended by MyOtherApp.")
    allowed = set(catalogue.parse(app.allowed_scopes))
    granted = set(catalogue.parse(token.scopes)) & allowed
    if conn is None:
        club_header = (request.headers.get(CLUB_HEADER) or "").strip()
        if not club_header:
            return PartnerCaller(
                club_id=None, client_id=app.client_id, client_type="partner_app",
                scopes=frozenset(granted & oauth.APP_TOKEN_SCOPES), livemode=bool(token.livemode), name=app.name, app=app,
                rate_limit=allocation.limits_for(app.rate_tier),
            )
        # The app's own token, for one of its connected clubs: that
        # connection's scopes, as the connection's own token would carry.
        from app.api.partner import apps as apps_mod

        if not club_header.isdigit():
            raise PartnerError("VALIDATION", f"{CLUB_HEADER} is a club's id.", details={"fields": {CLUB_HEADER: "A number."}})
        conn = await apps_mod.live_connection(session, app.id, int(club_header), bool(token.livemode))
        if conn is None:
            raise PartnerError("NOT_CONNECTED", "This app is not connected to that club (in this mode).")
        granted = set(catalogue.parse(conn.scopes)) & allowed
    else:
        granted &= set(catalogue.parse(conn.scopes))
    club, owner, alloc = await _club_and_owner(session, conn.club_id)
    seen = aware(conn.last_used_at)
    if seen is None or now() - seen > _TOUCH_EVERY:
        conn.last_used_at = now()
        await session.commit()
    return PartnerCaller(
        club_id=club.id, client_id=app.client_id, client_type="partner_app", scopes=frozenset(granted - oauth.APP_TOKEN_SCOPES),
        livemode=bool(token.livemode), name=app.name, club=club, owner=owner, app=app, connection=conn,
        rate_limit=alloc.rate_limit,
    )


def _limit(request: Request, caller: PartnerCaller) -> None:
    """Count this request; 429 when it is one too many. The numbers go on the
    answer's X-RateLimit headers either way."""
    from app.core.rate_limit import limiter

    write = request.method in WRITES
    per_minute = int(caller.rate_limit.get("write_per_min" if write else "read_per_min") or 0) or (120 if write else 600)
    item = RateLimitItemPerMinute(per_minute)
    who = (caller.client_id, str(caller.club_id or 0), "write" if write else "read")
    allowed, remaining, reset = True, per_minute, int(time.time()) + 60
    if limiter.enabled:
        try:
            store = limiter.limiter
            allowed = store.hit(item, "partner", *who)
            stats = store.get_window_stats(item, "partner", *who)
            remaining, reset = max(0, int(stats.remaining)), int(math.ceil(stats.reset_time))
        except Exception:  # noqa: BLE001 - a limiter that cannot count lets the request through
            logger.warning("partner rate limit could not be counted", exc_info=True)
    request.state.partner_rate = (per_minute, remaining, reset)
    if not allowed:
        wait = max(1, reset - int(time.time()))
        raise PartnerError(
            "RATE_LIMITED", f"Too many {'writes' if write else 'reads'}: {per_minute} a minute. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)}, details={"limit": per_minute, "retry_after": wait},
        )


async def _resolve(session, request, raw: str) -> PartnerCaller:
    if keys_mod.looks_like_key(raw):
        return await _from_key(session, request, raw)
    if raw.startswith(oauth.ACCESS):
        return await _from_token(session, request, raw)
    raise _unauthenticated("That is not a MyOtherApp API key or access token.")


async def credential_valid(session, request: Request) -> bool:
    """Whether the request's bearer is a credential /partner/v1 would let in
    now (a club key, or an app or OAuth token, live or test), without
    counting it as a request: how the OpenAPI document opens to callers."""
    raw = _bearer(request)
    if not raw:
        return False
    try:
        await _resolve(session, request, raw)
    except PartnerError:
        return False
    return True


async def partner_caller(request: Request, session: SessionDep) -> PartnerCaller:
    if not settings.PARTNER_API_ENABLED:
        raise PartnerError("NOT_FOUND", "Not found.")
    raw = _bearer(request)
    if not raw:
        raise _unauthenticated("Send an API key or access token: Authorization: Bearer <it>.")
    caller = await _resolve(session, request, raw)
    caller.session = session
    caller.request = request
    request.state.partner_caller = caller
    request.state.partner_session = session
    _limit(request, caller)
    request.state.partner_origin_token = api_origin.set_origin(caller.origin())
    await idempotency.begin(session, request, caller.client_id)
    return caller


def require_scope(*names: str):
    """A dependency giving the PartnerCaller, refused without any of `names`."""

    async def dependency(caller: PartnerCaller = Depends(partner_caller)) -> PartnerCaller:
        caller.require(*names)
        return caller

    # Read by openapi.py: each operation's `x-scopes`.
    dependency.partner_scopes = tuple(names)
    return dependency


def any_caller():
    return require_scope()
