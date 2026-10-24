"""A club connecting its own WhatsApp Business number, its quality, and which
number a WhatsApp send goes from.

Connecting is pasting four things from the club's own Meta app -- the
WhatsApp Business Account id, the phone number id, a permanent access token
(a system user's) and the app secret -- and, optionally, the app id (needed
to submit a template with a document header). They are checked with Meta
before anything is kept: the number is read (its display number, verified
name, quality rating and messaging limit), and it must be in that business
account. The token and secret are then encrypted (crypto.py) and the club is
given its webhook address and a verify token to paste into its app.

Changing the keys of a live connection keeps its webhook address and verify
token, so the club does not set the webhook up again. Disconnecting wipes
the keys; the row stays, for the history and the audit log, and the inbox
keeps its messages for the retention period.

`route_for` is what the deliveries ask: whether a ticket, certificate or
reminder for this club goes from its own number -- the connection is active
with routing on, the plan has `whatsapp_own`, the keys can be read, and the
club's account has that template approved. Otherwise it goes from the
platform's number, exactly as for any other club. A send from the club's
number that Meta refuses for the account (the token, a block, a payment
problem) marks the connection failed and tells the club, and that send goes
from the platform's number instead (`after_failure`).
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select

from app.api.club_whatsapp import alerts, crypto, graph
from app.api.club_whatsapp.models import (
    ACTIVE,
    DISCONNECTED,
    FAILED,
    LIVE,
    MODE_KEYS,
    QUALITY,
    ClubWhatsAppAccounts,
    ClubWhatsAppQualityLog,
    ClubWhatsAppTemplates,
)
from app.config import settings
from app.core import whatsapp
from app.db import lock_keys
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

FEATURE = "whatsapp_own"
#: The fields the club ticks when it subscribes its app's webhook.
WEBHOOK_FIELDS = ["messages", "message_template_status_update", "phone_number_quality_update", "account_update"]

_ID = re.compile(r"^\d{5,25}$")
_TOKEN = re.compile(r"^[\x21-\x7e]{20,2048}$")
_SECRET = re.compile(r"^[0-9a-fA-F]{32}$")
_HOOK_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
#: First key of the advisory lock a club's connect, change and disconnect take.
_CONNECT_LOCK = lock_keys.CLUB_WHATSAPP_CONNECT

#: Meta's codes that are about the account or the token, not one message:
#: the connection is failed and the club told; the send goes from the platform.
ACCOUNT_CODES = frozenset({"0", "3", "10", "190", "200", "131005", "131031", "131042", "131045", "133000", "133010", "368"})
#: Codes about the template on the club's account (missing, paused, disabled,
#: or not the shape ours sends): that send goes from the platform's number.
TEMPLATE_CODES = frozenset({"132000", "132001", "132005", "132012", "132015", "132016"})
#: Codes about the guest that only the club's number meets (is_club_only).
CLUB_ONLY_CODES = frozenset({"131021", "131030"})

TIER_ORDER = ("TIER_50", "TIER_250", "TIER_1K", "TIER_2K", "TIER_10K", "TIER_100K", "TIER_UNLIMITED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def unavailable_error() -> CustomHTTPException:
    return _refuse(503, "Connecting your own WhatsApp number isn't available yet.", "WHATSAPP_UNAVAILABLE")


def webhook_url(token: str) -> str:
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/hooks/whatsapp/club/{token}"


def token_hint(token: Optional[str]) -> str:
    """Enough of a webhook token to tell two apart in a log, not enough to use."""
    return f"{(token or '')[:6]}…"


# ─── Reading ─────────────────────────────────────────────────────────────


async def club_of(session, user):
    from app.api.clubs.models import Clubs

    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id))
    if club is None:
        raise _refuse(404, "No club for this account", "NOT_A_CLUB")
    return club


async def live_account(session, club_id: int) -> Optional[ClubWhatsAppAccounts]:
    """The club's connection: active, or failed and waiting to be fixed."""
    return await session.scalar(
        select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.club_id == club_id, ClubWhatsAppAccounts.status.in_(LIVE))
    )


async def latest_account(session, club_id: int) -> Optional[ClubWhatsAppAccounts]:
    return await session.scalar(
        select(ClubWhatsAppAccounts)
        .where(ClubWhatsAppAccounts.club_id == club_id)
        .order_by(ClubWhatsAppAccounts.created_at.desc(), ClubWhatsAppAccounts.id.desc())
        .limit(1)
    )


def keys_of(account: ClubWhatsAppAccounts) -> graph.Keys:
    """The account's keys, decrypted for a call. crypto.Unavailable when they cannot be."""
    return graph.Keys(
        token=crypto.decrypt(account.access_token_enc),
        app_secret=crypto.decrypt(account.app_secret_enc) if account.app_secret_enc else None,
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
        app_id=account.app_id,
    )


async def require_keys(session, account: ClubWhatsAppAccounts) -> graph.Keys:
    """keys_of, or the account marked failed and a 503 when they cannot be read."""
    try:
        return keys_of(account)
    except crypto.Unavailable:
        await mark_failed(session, account, "The saved keys can't be read any more. Paste them again.", "KEYS")
        await session.commit()
        raise _refuse(503, "Your WhatsApp keys can't be read any more. Paste them again.", "WHATSAPP_KEYS_UNREADABLE") from None


async def plan_allows(session, club_id: int, actor=None) -> bool:
    from app.api.subscriptions import gates

    return await gates.allows(session, club_id, FEATURE, actor=actor)


async def require_plan(session, club_id: int, actor=None) -> None:
    from app.api.subscriptions import gates

    await gates.require_feature(session, club_id, FEATURE, actor=actor)


# ─── Checking keys with Meta ─────────────────────────────────────────────


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def check_format(waba_id: str, phone_number_id: str, token: str, app_secret: str, app_id: Optional[str]) -> None:
    def bad(field: str, message: str):
        raise _refuse(422, message, "INVALID_WHATSAPP_KEYS", {"field": field})

    if not _ID.match(waba_id):
        bad("waba_id", "That isn't a WhatsApp Business Account ID: it's a long number, in WhatsApp Manager or your app's API setup.")
    if not _ID.match(phone_number_id):
        bad("phone_number_id", "That isn't a phone number ID: it's a long number under your number in the app's API setup, not the phone number itself.")
    if phone_number_id == waba_id:
        bad("phone_number_id", "The phone number ID and the WhatsApp Business Account ID are two different numbers.")
    if not _TOKEN.match(token):
        bad("access_token", "That isn't an access token. Paste the whole permanent token of your system user.")
    if not _SECRET.match(app_secret):
        bad("app_secret", "That isn't an app secret: it's 32 letters and digits, under App settings → Basic.")
    if app_id and not _ID.match(app_id):
        bad("app_id", "That isn't an app ID: it's a long number at the top of your app's dashboard.")
    if phone_number_id == (settings.WHATSAPP_PHONE_NUMBER_ID or "").strip() or token == (settings.WHATSAPP_ACCESS_TOKEN or "").strip():
        bad("phone_number_id", "That's MyOtherApp's own WhatsApp number. Use your club's.")


#: The connect form's field each refusal is about, so the dashboard marks it.
FIELD_OF = {
    "WHATSAPP_TOKEN_INVALID": "access_token",
    "WHATSAPP_PERMISSION_MISSING": "access_token",
    "WHATSAPP_APP_SECRET_WRONG": "app_secret",
    "WHATSAPP_NUMBER_NOT_FOUND": "phone_number_id",
    "WHATSAPP_NUMBER_NOT_IN_WABA": "phone_number_id",
    "WHATSAPP_NUMBER_IN_USE": "phone_number_id",
    "WHATSAPP_NUMBER_NOT_REGISTERED": "phone_number_id",
}


def refusal(exc: graph.GraphError) -> CustomHTTPException:
    status, code, message = graph.explain(exc)
    errors = {"meta_code": exc.code, "reason": exc.message} if exc.code else {}
    if code in FIELD_OF:
        errors["field"] = FIELD_OF[code]
    return _refuse(status, message, code, errors or None)


async def check_keys(keys: graph.Keys) -> dict:
    """Meta's answer for the number, once it is sure the keys work and the
    number is in that business account. CustomHTTPException when not."""
    try:
        number = await graph.phone_number(keys)
        numbers = await graph.waba_numbers(keys)
    except graph.GraphError as exc:
        raise refusal(exc) from None
    if keys.phone_number_id not in numbers:
        raise _refuse(
            422,
            "That phone number isn't in that WhatsApp Business Account. Copy both IDs from the same app's API setup.",
            "WHATSAPP_NUMBER_NOT_IN_WABA",
            {"field": FIELD_OF["WHATSAPP_NUMBER_NOT_IN_WABA"]},
        )
    return number


# ─── Quality ─────────────────────────────────────────────────────────────


def _rank(rating: Optional[str]) -> Optional[int]:
    return QUALITY.index(rating) if rating in QUALITY else None


def _tier_rank(tier: Optional[str]) -> Optional[int]:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else None


def dropped(before: Optional[str], after: Optional[str]) -> bool:
    a, b = _rank(before), _rank(after)
    return a is not None and b is not None and b > a


def tier_dropped(before: Optional[str], after: Optional[str]) -> bool:
    a, b = _tier_rank(before), _tier_rank(after)
    return a is not None and b is not None and b < a


async def record_quality(session, account: ClubWhatsAppAccounts, number: dict, source: str, now: Optional[datetime] = None) -> bool:
    """What Meta said of the number: its display number and name, its quality
    rating and messaging limit. A change is logged; a drop tells the club and
    the admins. True when the rating or the limit changed."""
    from app.api.admin_alerts import notify

    now = now or _now()
    rating = (str(number.get("quality_rating") or "").upper() or None) if "quality_rating" in number else account.quality_rating
    tier = (str(number.get("messaging_limit_tier") or "").upper() or None) if "messaging_limit_tier" in number else account.messaging_limit_tier
    if number.get("display_phone_number"):
        account.display_phone_number = str(number["display_phone_number"])[:32]
    if number.get("verified_name"):
        account.verified_name = str(number["verified_name"])[:200]
    account.quality_checked_at = now
    before_rating, before_tier = account.quality_rating, account.messaging_limit_tier
    if rating == before_rating and tier == before_tier:
        return False
    account.quality_rating, account.messaging_limit_tier = rating, tier
    session.add(ClubWhatsAppQualityLog(
        account_id=account.id, club_id=account.club_id, quality_rating=rating, messaging_limit_tier=tier,
        previous_rating=before_rating, previous_tier=before_tier, source=source, created_at=now,
    ))
    if dropped(before_rating, rating) or tier_dropped(before_tier, tier):
        number_name = account.display_phone_number or "Your WhatsApp number"
        what = (
            f"its quality rating dropped from {before_rating} to {rating}"
            if dropped(before_rating, rating)
            else f"its messaging limit dropped from {limit_words(before_tier)} to {limit_words(tier)}"
        )
        await alerts.tell_owner(
            session, account.club_id, "quality",
            subject=f"{number_name}: WhatsApp quality dropped",
            paragraphs=[
                f"Meta says {what}.",
                "A lower rating comes from people blocking or reporting the number. Send messages people expect, "
                "to people who asked for them, and keep templates short and useful. A RED number can have its "
                "messaging limit lowered or be restricted by Meta.",
                "Your dashboard shows the rating and limit as they stand.",
            ],
            now=now,
        )
        await notify(
            session, "club_whatsapp_quality",
            title=f"A club's own WhatsApp number dropped to {rating or tier}",
            body=f"Meta says {what}.",
            club=account.club_id,
            data={"account_id": account.id, "previous": before_rating, "rating": rating, "previous_tier": before_tier, "tier": tier},
        )
    return True


def limit_words(tier: Optional[str]) -> str:
    """TIER_1K as "1,000 people a day"."""
    words = {
        "TIER_50": "50", "TIER_250": "250", "TIER_1K": "1,000", "TIER_2K": "2,000", "TIER_10K": "10,000",
        "TIER_100K": "100,000",
    }
    if tier == "TIER_UNLIMITED":
        return "no limit"
    return f"{words[tier]} people a day" if tier in words else (tier or "not set")


async def mark_failed(session, account: ClubWhatsAppAccounts, reason: str, code: Optional[str] = None, *, alert: bool = True) -> None:
    """The keys or the account stopped working. A live row becomes failed:
    nothing is sent from it until the club fixes it."""
    from app.api.admin_alerts import notify

    if account.status in LIVE:
        account.status = FAILED
    account.last_error = reason[:300]
    account.last_error_code = (code or None) and str(code)[:16]
    if not alert:
        return
    number = account.display_phone_number or "Your club's WhatsApp number"
    await alerts.tell_owner(
        session, account.club_id, "failed",
        subject=f"{number} stopped working on MyOtherApp",
        paragraphs=[
            f"Meta refused your club's WhatsApp number: {reason}",
            "Until it's fixed, your tickets, certificates and reminders go from MyOtherApp's WhatsApp number, "
            "and nothing can be sent from your inbox.",
            "Fix it in Meta, then click Check again in your dashboard, or paste new keys.",
        ],
    )
    await notify(
        session, "club_whatsapp_failed",
        title=f"A club's own WhatsApp number stopped working ({code or 'error'})",
        body=reason[:300],
        club=account.club_id,
        data={"account_id": account.id, "code": code},
    )


# ─── Connecting ──────────────────────────────────────────────────────────


@dataclass
class KeysIn:
    waba_id: str
    phone_number_id: str
    access_token: str
    app_secret: str
    app_id: Optional[str] = None


async def connect(session, club, user, keys_in: KeysIn, *, member=None, request=None) -> ClubWhatsAppAccounts:
    """Check the keys with Meta and make the number the club's. A live
    connection has its keys changed in place, keeping its webhook address."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    await require_plan(session, club.id, actor=user)
    if not crypto.available():
        raise unavailable_error()
    waba_id, phone_id = _clean(keys_in.waba_id), _clean(keys_in.phone_number_id)
    token, secret, app_id = _clean(keys_in.access_token), _clean(keys_in.app_secret), _clean(keys_in.app_id) or None
    check_format(waba_id, phone_id, token, secret, app_id)
    taken = await session.scalar(
        select(ClubWhatsAppAccounts.id).where(
            ClubWhatsAppAccounts.phone_number_id == phone_id,
            ClubWhatsAppAccounts.status.in_(LIVE),
            ClubWhatsAppAccounts.club_id != club.id,
        )
    )
    if taken is not None:
        raise _refuse(409, "That number is connected to another club on MyOtherApp.", "WHATSAPP_NUMBER_IN_USE",
                      {"field": FIELD_OF["WHATSAPP_NUMBER_IN_USE"]})
    keys = graph.Keys(token=token, app_secret=secret, waba_id=waba_id, phone_number_id=phone_id, app_id=app_id)
    # Nothing held while Meta answers.
    await session.commit()
    number = await check_keys(keys)
    subscribed = True
    try:
        await graph.subscribe_app(keys)
    except graph.GraphError:
        subscribed = False

    # Two saves at once must not both insert a live row.
    await session.execute(select(func.pg_advisory_xact_lock(_CONNECT_LOCK, club.id)))
    now = _now()
    account = await live_account(session, club.id)
    changed = account is not None
    previous = {"waba_id": account.waba_id, "phone_number_id": account.phone_number_id} if changed else None
    if account is None:
        account = ClubWhatsAppAccounts(
            club_id=club.id, mode=MODE_KEYS, webhook_token=secrets.token_urlsafe(24),
            verify_token_enc=crypto.encrypt(secrets.token_urlsafe(24)),
            created_by=getattr(user, "id", None), created_by_member_id=getattr(member, "id", None),
        )
        session.add(account)
    if account.verify_token_enc is None:
        account.verify_token_enc = crypto.encrypt(secrets.token_urlsafe(24))
    if previous and previous["phone_number_id"] != phone_id:
        # Another number: what Meta had approved on the old account says nothing of the new one.
        await session.execute(delete(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id))
        account.quality_rating = account.messaging_limit_tier = None
    account.waba_id, account.phone_number_id, account.app_id = waba_id, phone_id, app_id
    account.access_token_enc = crypto.encrypt(token)
    account.app_secret_enc = crypto.encrypt(secret)
    account.token_last4 = token[-4:]
    account.status = ACTIVE
    account.verified_at = now
    account.ended_at = None
    account.last_error = None if subscribed else (
        "Meta didn't let us subscribe your app to the account's webhooks. In your app, open WhatsApp → Configuration and subscribe to messages."
    )
    account.last_error_code = None
    await session.flush()
    await record_quality(session, account, number, "connect", now)
    await audit.record(
        session, actor=user,
        action="club_whatsapp.keys_changed" if changed else "club_whatsapp.connect",
        entity_type="club", entity_id=club.id,
        summary=f"{'Changed the keys of' if changed else 'Connected'} WhatsApp number {account.display_phone_number or phone_id}",
        details={
            "account_id": account.id, "waba_id": waba_id, "phone_number_id": phone_id, "app_id": app_id,
            "display_phone_number": account.display_phone_number, "token_last4": account.token_last4,
            "member_id": getattr(member, "id", None), "previous": previous, "subscribed": subscribed,
        },
        request=request,
    )
    if not changed:
        await notify(
            session, "club_whatsapp_connected",
            title=f"{club.name} connected its own WhatsApp number",
            body=f"{account.display_phone_number or phone_id} ({account.verified_name or 'no verified name yet'}).",
            club=club, data={"account_id": account.id}, dedupe=False,
        )
    await session.commit()
    # What the account has approved, for its tickets; best effort -- the
    # templates page and Meta's webhook keep it current.
    from app.api.club_whatsapp import templates

    try:
        await templates.sync(session, account, keys)
        await session.commit()
    except (graph.GraphError, crypto.Unavailable):
        await session.rollback()
    return account


async def recheck(session, account: ClubWhatsAppAccounts, source: str = "check") -> str:
    """Ask Meta again whether the keys work and how the number stands:
    `ok` (active, quality recorded), `refused` or `unreadable` (failed, the
    club told), `unreachable` (the status stays: not the keys' fault)."""
    try:
        keys = keys_of(account)
    except crypto.Unavailable:
        await mark_failed(session, account, "The saved keys can't be read any more. Paste them again.", "KEYS")
        return "unreadable"
    try:
        number = await graph.phone_number(keys)
    except graph.GraphError as exc:
        if exc.unreachable:
            account.last_error = "Couldn't reach Meta to check the number. Try again in a minute."
            return "unreachable"
        _status, _code, words = graph.explain(exc)
        await mark_failed(session, account, words, exc.code)
        return "refused"
    account.status = ACTIVE
    account.verified_at = _now()
    account.last_error = None
    account.last_error_code = None
    await record_quality(session, account, number, source)
    return "ok"


async def verify(session, club, user, *, request=None) -> ClubWhatsAppAccounts:
    from app.api.audit import service as audit

    account = await live_account(session, club.id)
    if account is None:
        raise _refuse(404, "Connect your WhatsApp number first.", "NO_WHATSAPP")
    if not crypto.available():
        raise unavailable_error()
    was = account.status
    await session.commit()
    result = await recheck(session, account)
    await audit.record(
        session, actor=user, action="club_whatsapp.verify", entity_type="club", entity_id=club.id,
        summary=f"Checked WhatsApp number {account.display_phone_number or account.phone_number_id}: {result}",
        details={"account_id": account.id, "result": result, "previous_status": was, "status": account.status},
        request=request,
    )
    await session.commit()
    return account


async def update_settings(session, club, user, *, routing: Optional[bool], retention_months: Optional[int], request=None) -> ClubWhatsAppAccounts:
    from app.api.audit import service as audit

    account = await live_account(session, club.id) or await latest_account(session, club.id)
    if account is None:
        raise _refuse(404, "Connect your WhatsApp number first.", "NO_WHATSAPP")
    before = {"routing": account.routing, "retention_months": account.retention_months}
    if routing is not None:
        account.routing = bool(routing)
    if retention_months is not None:
        account.retention_months = int(retention_months)
    after = {"routing": account.routing, "retention_months": account.retention_months}
    if before != after:
        await audit.record(
            session, actor=user, action="club_whatsapp.settings", entity_type="club", entity_id=club.id,
            summary="Changed the WhatsApp settings", details={"account_id": account.id, "before": before, "after": after},
            request=request,
        )
    await session.commit()
    return account


async def disconnect(session, club, user, *, request=None) -> Optional[ClubWhatsAppAccounts]:
    """The keys are wiped; nothing more is sent from the number. The inbox
    keeps its messages until the retention period ends."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    await session.execute(select(func.pg_advisory_xact_lock(_CONNECT_LOCK, club.id)))
    account = await live_account(session, club.id)
    if account is None:
        return None
    account.status = DISCONNECTED
    account.ended_at = _now()
    account.access_token_enc = None
    account.app_secret_enc = None
    account.verify_token_enc = None
    account.token_last4 = None
    await session.execute(delete(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id))
    await audit.record(
        session, actor=user, action="club_whatsapp.disconnect", entity_type="club", entity_id=club.id,
        summary=f"Disconnected WhatsApp number {account.display_phone_number or account.phone_number_id}; its keys were wiped",
        details={"account_id": account.id, "waba_id": account.waba_id, "phone_number_id": account.phone_number_id},
        request=request,
    )
    await notify(
        session, "club_whatsapp_disconnected",
        title=f"{club.name} disconnected its own WhatsApp number",
        body="Its tickets go from the platform's number again.",
        club=club, data={"account_id": account.id}, dedupe=False,
    )
    await session.commit()
    return account


# ─── Which number a send goes from ───────────────────────────────────────


@dataclass
class Route:
    """A send from the club's own number: its Sender, and whose it is.
    `cache` is the send's own (route_for), which a refusal updates."""

    sender: whatsapp.Sender
    account_id: int
    club_id: int
    template: str
    cache: Optional[dict] = field(default=None, repr=False, compare=False)

    def forget(self, exc: Exception) -> None:
        """The club's number refused a send: the rest of this send asks no
        more of it -- none of the club's templates after an account problem,
        not this one after a template problem."""
        if self.cache is None:
            return
        if is_account_problem(exc):
            self.cache[(self.club_id, None)] = False
        elif is_template_problem(exc):
            self.cache[(self.club_id, self.template)] = None


async def routing_account(session, club_id: Optional[int]) -> Optional[ClubWhatsAppAccounts]:
    if club_id is None or not crypto.available():
        return None
    return await session.scalar(
        select(ClubWhatsAppAccounts).where(
            ClubWhatsAppAccounts.club_id == club_id,
            ClubWhatsAppAccounts.status == ACTIVE,
            ClubWhatsAppAccounts.routing == True,  # noqa: E712
        )
    )


async def can_route(session, club_id: Optional[int]) -> bool:
    """Whether this club's WhatsApp sends may go from its own number at all
    (an active connection with routing on, on a plan that has it)."""
    account = await routing_account(session, club_id)
    return account is not None and await plan_allows(session, club_id)


async def approved_language(session, account_id: int, name: str) -> Optional[str]:
    """The language the club's account has `name` approved in, the platform's preferred."""
    rows = (await session.execute(
        select(ClubWhatsAppTemplates.language).where(
            ClubWhatsAppTemplates.account_id == account_id,
            ClubWhatsAppTemplates.name == name,
            ClubWhatsAppTemplates.status == "APPROVED",
        )
    )).scalars().all()
    if not rows:
        return None
    wanted = (settings.WHATSAPP_TEMPLATE_LANGUAGE or "en").strip()
    for language in (wanted, "en", "en_US", "en_GB"):
        if language in rows:
            return language
    return sorted(rows)[0]


async def route_for(session, club_id: Optional[int], template: str, *, cache: Optional[dict] = None) -> Optional[Route]:
    """The club's own number for this template, or None for the platform's.
    `cache` is a send's own ((club, template) -> Route or None), so a
    thousand guests decide it once; a refusal from the club's number takes
    the answer back (Route.forget)."""
    if cache is not None:
        if cache.get((club_id, None)) is False:
            return None
        if (club_id, template) in cache:
            return cache[(club_id, template)]
    route = await _route_for(session, club_id, template)
    if route is not None:
        route.cache = cache
    if cache is not None:
        cache[(club_id, template)] = route
    return route


async def _route_for(session, club_id: Optional[int], template: str) -> Optional[Route]:
    account = await routing_account(session, club_id)
    if account is None or not await plan_allows(session, club_id):
        return None
    language = await approved_language(session, account.id, template)
    if language is None:
        return None
    try:
        keys = keys_of(account)
    except crypto.Unavailable:
        await mark_failed(session, account, "The saved keys can't be read any more. Paste them again.", "KEYS")
        return None
    sender = whatsapp.Sender(phone_number_id=account.phone_number_id, token=keys.token, language=language,
                             app_secret=keys.app_secret, account_id=account.id)
    return Route(sender=sender, account_id=account.id, club_id=account.club_id, template=template)


def is_account_problem(exc: Exception) -> bool:
    return isinstance(exc, whatsapp.Refused) and (exc.code or "") in ACCOUNT_CODES


def is_template_problem(exc: Exception) -> bool:
    return isinstance(exc, whatsapp.Refused) and (exc.code or "") in TEMPLATE_CODES


def is_club_only(exc: Exception) -> bool:
    """A refusal only a send from the club's own number meets -- the guest
    isn't on the test list of a club account that isn't live yet (131030),
    or the guest's number is the club's own (131021) -- which says nothing
    of the guest's number: it is not marked, and the platform's may send."""
    return isinstance(exc, whatsapp.Undeliverable) and (exc.code or "") in CLUB_ONLY_CODES


def falls_back(exc: Exception) -> bool:
    """A refusal from the club's number that the platform's may not have."""
    return is_account_problem(exc) or is_template_problem(exc) or is_club_only(exc)


async def after_failure(session, route: Route, exc: Exception) -> None:
    """What a refused send from the club's number says about it: the account
    failed (and the club told), or that template unusable until Meta says
    otherwise; nothing for a refusal only the club's number meets. Either
    way the caller sends from the platform's number."""
    if is_club_only(exc):
        return
    account = await session.get(ClubWhatsAppAccounts, route.account_id)
    if account is None:
        return
    if is_account_problem(exc):
        await mark_failed(session, account, getattr(exc, "reason", str(exc)), getattr(exc, "code", None))
        return
    if is_template_problem(exc):
        rows = (await session.scalars(
            select(ClubWhatsAppTemplates).where(
                ClubWhatsAppTemplates.account_id == account.id, ClubWhatsAppTemplates.name == route.template,
            )
        )).all()
        for row in rows:
            row.status = "UNUSABLE"
            row.rejected_reason = (getattr(exc, "reason", "") or "")[:200]


# ─── What is shown ───────────────────────────────────────────────────────


async def upgrade_payload(session) -> dict:
    from app.api.subscriptions import gates

    return (await gates.feature_locked(session, FEATURE)).errors or {}


async def account_out(session, club_id: int, *, manage: bool, inbox: bool, actor=None) -> dict:
    """The club's WhatsApp page. The verify token only to those who manage
    the connection, and only while connected; the access token and the app
    secret never."""
    allowed = await plan_allows(session, club_id, actor=actor)
    out = {
        "available": crypto.available(),
        "plan_allows": allowed,
        "upgrade": None if allowed else await upgrade_payload(session),
        "connected": False,
        "status": None,
        "mode": None,
        "waba_id": None,
        "phone_number_id": None,
        "app_id": None,
        "display_phone_number": None,
        "verified_name": None,
        "token_last4": None,
        "quality_rating": None,
        "messaging_limit_tier": None,
        "quality_checked_at": None,
        "verified_at": None,
        "last_webhook_at": None,
        "last_error": None,
        "last_error_code": None,
        "routing": True,
        "retention_months": 12,
        "webhook_url": None,
        "verify_token": None,
        "webhook_fields": list(WEBHOOK_FIELDS),
        "can_manage": bool(manage and crypto.available()),
        "can_inbox": bool(inbox),
    }
    row = await live_account(session, club_id) or await latest_account(session, club_id)
    if row is None:
        return out
    connected = row.status in LIVE
    out.update(
        connected=connected, status=row.status, mode=row.mode, waba_id=row.waba_id, phone_number_id=row.phone_number_id,
        app_id=row.app_id, display_phone_number=row.display_phone_number, verified_name=row.verified_name,
        token_last4=row.token_last4, quality_rating=row.quality_rating, messaging_limit_tier=row.messaging_limit_tier,
        quality_checked_at=row.quality_checked_at, verified_at=row.verified_at, last_webhook_at=row.last_webhook_at,
        last_error=row.last_error, last_error_code=row.last_error_code, routing=row.routing,
        retention_months=row.retention_months,
    )
    if connected:
        out["webhook_url"] = webhook_url(row.webhook_token)
        if manage:
            try:
                out["verify_token"] = crypto.decrypt(row.verify_token_enc)
            except crypto.Unavailable:
                out["verify_token"] = None
    return out


async def admin_out(session, club_id: int) -> dict:
    """What a platform admin sees on the club: whether its own number is
    connected, its status and quality. Never a message, never a key."""
    from app.api.club_whatsapp.models import ClubWhatsAppConversations

    row = await live_account(session, club_id) or await latest_account(session, club_id)
    out = {
        "club_id": club_id,
        "connected": False,
        "status": None,
        "display_phone_number": None,
        "verified_name": None,
        "quality_rating": None,
        "messaging_limit_tier": None,
        "quality_checked_at": None,
        "verified_at": None,
        "last_webhook_at": None,
        "last_error": None,
        "routing": None,
        "connected_at": None,
        "conversations": 0,
        "plan_allows": await plan_allows(session, club_id),
    }
    if row is None:
        return out
    out.update(
        connected=row.status in LIVE, status=row.status, display_phone_number=row.display_phone_number,
        verified_name=row.verified_name, quality_rating=row.quality_rating, messaging_limit_tier=row.messaging_limit_tier,
        quality_checked_at=row.quality_checked_at, verified_at=row.verified_at, last_webhook_at=row.last_webhook_at,
        last_error=row.last_error, routing=row.routing, connected_at=row.created_at,
    )
    out["conversations"] = int(await session.scalar(
        select(func.count()).select_from(ClubWhatsAppConversations).where(ClubWhatsAppConversations.club_id == club_id)
    ) or 0)
    return out


# ─── Webhook ─────────────────────────────────────────────────────────────


async def by_webhook_token(session, token: str) -> Optional[ClubWhatsAppAccounts]:
    if not token or not _HOOK_TOKEN.match(token):
        return None
    return await session.scalar(select(ClubWhatsAppAccounts).where(ClubWhatsAppAccounts.webhook_token == token))


#: What the club's page says while webhooks arrive with signatures that don't match.
BAD_SIGNATURE = "A webhook arrived with a signature that doesn't match. Check the app secret saved here is your app's."
#: How often the admins hear of a webhook trouble that repeats with every delivery.
HOURLY = timedelta(hours=1)


async def bad_signature(session, account: ClubWhatsAppAccounts) -> None:
    """Most likely the app secret saved is not the app's. Said on the club's
    page -- unless the page already says something else, which matters
    more -- and to the admins at most once an hour."""
    if account.last_error in (None, BAD_SIGNATURE):
        account.last_error = BAD_SIGNATURE
    await alerts.tell_admins(
        session, "club_whatsapp_failed",
        title="A webhook for a club's own WhatsApp number had a bad signature",
        body="Either the club's saved app secret is wrong, or someone else is calling its webhook address.",
        club_id=account.club_id, data={"account_id": account.id, "code": "bad_signature"}, every=HOURLY,
    )


async def keys_unreadable(session, account: ClubWhatsAppAccounts) -> None:
    """A webhook whose app secret can't be decrypted: the server's
    WHATSAPP_KEYS_SECRET, not the club. Meta is answered 503 (it delivers
    again) and the admins told at most once an hour."""
    logger.error("club whatsapp account %s: a webhook came and its app secret can't be decrypted (APP_WHATSAPP_KEYS_SECRET)", account.id)
    await alerts.tell_admins(
        session, "club_whatsapp_failed",
        title="A club's WhatsApp webhooks can't be checked: its keys can't be read",
        body="APP_WHATSAPP_KEYS_SECRET is missing or isn't the key the club's keys were saved with. Meta keeps delivering "
             "for 7 days; restore the key and they are taken.",
        club_id=account.club_id, data={"account_id": account.id, "code": "keys_unreadable"}, every=HOURLY,
    )
