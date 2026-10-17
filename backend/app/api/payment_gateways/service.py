"""Which Razorpay account a payment goes to, and a club connecting its own.

Everything a club sells (tickets, and the store's picks riding on the same
payment) is paid into the club's own Razorpay account when it has an
`active` gateway, and into the platform's otherwise, as before. The club
paying us for its plan (subscriptions/billing.py) always uses the platform's.

`for_new_order` picks the account for an order about to be made;
`for_order` gives back the account an existing order was made on, which is
what verifying it and refunding it must use whatever the club has connected
since. Neither ever hands out a key secret: `Checkout` carries a client
built from it, and the publishable key id.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import razorpay
from razorpay.errors import BadRequestError
from sqlalchemy import func, or_, select

from app.api.payment_gateways import crypto
from app.api.payment_gateways.models import (
    ACTIVE,
    DISABLED,
    DISCONNECTED,
    FAILED,
    LIVE,
    MODE_KEYS,
    PROVIDER_RAZORPAY,
    REPLACED,
    STATUSES,
    ClubPaymentGateways,
)
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PLATFORM = "platform"
CLUB = "club"

#: The `checkout` query parameter a client sends once it opens checkout with
#: the order's own `key_id`. An older build has the platform's key compiled
#: in and cannot pay an order made on a club's account.
CHECKOUT_VERSION = 2

#: What the club ticks when it adds the webhook in Razorpay.
WEBHOOK_EVENTS = [
    "payment.authorized",
    "payment.captured",
    "payment.failed",
    "order.paid",
    "refund.processed",
]

_KEY_ID = re.compile(r"^rzp_(live|test)_[A-Za-z0-9]{6,40}$")
_KEY_SECRET = re.compile(r"^[\x21-\x7e]{8,128}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
#: Seconds to wait for Razorpay when checking keys.
CHECK_TIMEOUT = 10
#: First key of the advisory lock a club's connect takes.
_CONNECT_LOCK = 7_420_377


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: dict | None = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=code, message=message, error_code=error_code, errors=errors)


def unavailable_error() -> CustomHTTPException:
    return _refuse(
        503,
        "Connecting your own Razorpay account isn't available yet.",
        "GATEWAY_UNAVAILABLE",
    )


def webhook_url(token: str) -> str:
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/payments/webhook/club/{token}"


def token_hint(token: str | None) -> str:
    """Enough of a webhook token to tell two apart in a log, not enough to use."""
    return f"{(token or '')[:6]}…"


def supports_club_checkout(checkout: int | None) -> bool:
    return (checkout or 0) >= CHECKOUT_VERSION


# ─── Which account ──────────────────────────────────────────────────────


@dataclass
class Checkout:
    kind: str  # PLATFORM or CLUB
    key_id: str
    client: Any
    gateway: ClubPaymentGateways | None = None

    @property
    def gateway_id(self) -> int | None:
        return self.gateway.id if self.gateway is not None else None


def platform() -> Checkout:
    # Read from the module each time: tests replace it there.
    from app.api.payments import service as payments

    return Checkout(PLATFORM, settings.RAZORPAY_KEY_ID, payments.razorpay_client)


def make_client(key_id: str, key_secret: str):
    """A Razorpay client on a club's account. The suite replaces this: no test reaches Razorpay."""
    return razorpay.Client(auth=(key_id, key_secret))


def client_for(gateway: ClubPaymentGateways):
    if gateway.mode != MODE_KEYS:
        raise crypto.Unavailable(f"{gateway.mode} gateways are not supported yet")
    return make_client(gateway.key_id, crypto.decrypt(gateway.key_secret_enc))


async def live_gateway(session, club_id: int) -> ClubPaymentGateways | None:
    """The club's connection: active, or failed and waiting to be fixed."""
    return await session.scalar(
        select(ClubPaymentGateways).where(
            ClubPaymentGateways.club_id == club_id, ClubPaymentGateways.status.in_(LIVE)
        )
    )


async def latest_gateway(session, club_id: int) -> ClubPaymentGateways | None:
    return await session.scalar(
        select(ClubPaymentGateways)
        .where(ClubPaymentGateways.club_id == club_id)
        .order_by(ClubPaymentGateways.created_at.desc(), ClubPaymentGateways.id.desc())
        .limit(1)
    )


async def routing_gateway(session, club_id: int | None) -> ClubPaymentGateways | None:
    """The account a new order for this club goes to; None for the platform's."""
    if club_id is None or not crypto.available():
        return None
    return await session.scalar(
        select(ClubPaymentGateways).where(
            ClubPaymentGateways.club_id == club_id, ClubPaymentGateways.status == ACTIVE
        )
    )


async def payments_to(session, club_id: int | None) -> str:
    return CLUB if await routing_gateway(session, club_id) is not None else PLATFORM


async def for_new_order(session, club_id: int | None) -> Checkout:
    gateway = await routing_gateway(session, club_id)
    if gateway is None:
        return platform()
    try:
        return Checkout(CLUB, gateway.key_id, client_for(gateway), gateway)
    except crypto.Unavailable:
        # PAYMENT_KEYS_SECRET changed without keeping the old key. The club's
        # account cannot be used, so the platform's takes the payment and the
        # admins hear about it.
        await mark_failed(session, gateway, "The saved keys can't be read any more. Paste them again.")
        return platform()


async def for_order(session, order) -> Checkout:
    """The account an existing order was made on, whatever the club has now."""
    if order.payment_gateway_id is None:
        return platform()
    gateway = await session.get(ClubPaymentGateways, order.payment_gateway_id)
    try:
        return Checkout(CLUB, gateway.key_id, client_for(gateway), gateway)
    except crypto.Unavailable:
        logger.error("Payment gateway %s cannot be used: its keys cannot be read", gateway.id)
        raise _refuse(
            503,
            "This payment went to the club's own Razorpay account, and its keys can't be read right now.",
            "GATEWAY_UNAVAILABLE",
        )


async def checkout_fields(session, order) -> dict:
    """What checkout opens with for this order: the account's publishable key."""
    if order.payment_gateway_id is None:
        return {"key_id": settings.RAZORPAY_KEY_ID, "gateway": PLATFORM}
    key_id = await session.scalar(
        select(ClubPaymentGateways.key_id).where(ClubPaymentGateways.id == order.payment_gateway_id)
    )
    return {"key_id": key_id, "gateway": CLUB}


def is_auth_error(exc: Exception) -> bool:
    return isinstance(exc, BadRequestError) and "authentication" in str(exc).lower()


# ─── Checking keys ──────────────────────────────────────────────────────


class KeysRefused(Exception):
    """Razorpay said no to these keys. The message is Razorpay's reason."""


class RazorpayUnreachable(Exception):
    """Razorpay could not be asked."""


def short_reason(exc: Exception, *hide: str) -> str:
    """Razorpay's reason, short, and never with a secret in it."""
    text = str(exc) or type(exc).__name__
    for value in hide:
        if value:
            text = text.replace(value, "•••")
    return " ".join(text.split())[:200]


async def check_keys(key_id: str, key_secret: str) -> None:
    """A harmless read on the account: its latest order, if any."""

    def probe():
        make_client(key_id, key_secret).order.all({"count": 1}, timeout=CHECK_TIMEOUT)

    try:
        await asyncio.to_thread(probe)
    except BadRequestError as exc:
        raise KeysRefused(short_reason(exc, key_secret) or "Razorpay refused these keys") from None
    except Exception as exc:  # noqa: BLE001
        raise RazorpayUnreachable(short_reason(exc, key_secret)) from None


# ─── State changes ──────────────────────────────────────────────────────


async def _club_name(session, club_id: int) -> str:
    from app.api.clubs.models import Clubs

    return await session.scalar(select(Clubs.name).where(Clubs.id == club_id)) or f"Club {club_id}"


async def mark_failed(session, gateway: ClubPaymentGateways, reason: str, *, alert: bool = True) -> None:
    """The keys stopped working. A live row becomes `failed` (new orders go
    to the platform until the owner fixes it); any other keeps its status."""
    from app.api.admin_alerts import notify

    if gateway.status in LIVE:
        gateway.status = FAILED
    gateway.last_error = reason[:300]
    if alert:
        name = await _club_name(session, gateway.club_id)
        await notify(
            session,
            "payment_gateway_failed",
            title=f"{name}'s own Razorpay account stopped working",
            body=f"{reason} New ticket and merch payments go to the platform's account until the club fixes it.",
            club=gateway.club_id,
            data={"gateway_id": gateway.id, "key_id": gateway.key_id, "reason": reason[:200]},
        )


def _check_format(key_id: str, key_secret: str) -> None:
    if not _KEY_ID.match(key_id) or not _KEY_SECRET.match(key_secret):
        raise _refuse(
            422,
            "That isn't a Razorpay key id and secret. Copy both from Razorpay's Dashboard, under Account & Settings → API Keys.",
            "INVALID_KEYS",
            {"reason": "format"},
        )
    if key_id.startswith("rzp_test_") and not settings.ALLOW_TEST_GATEWAY_KEYS:
        raise _refuse(
            422,
            "Those are test-mode keys, which take no real money. Use your live keys (rzp_live_…).",
            "TEST_KEYS_NOT_ALLOWED",
        )
    if key_id == settings.RAZORPAY_KEY_ID:
        raise _refuse(422, "Those are MyOtherApp's own keys. Use your club's.", "INVALID_KEYS", {"reason": "platform_keys"})


async def admin_disabled(session, club_id: int) -> ClubPaymentGateways | None:
    """The club's latest gateway, when a platform admin switched it off. It
    holds until an admin lifts it (`enable`): the club cannot paste new keys
    round it, nor check or disconnect it."""
    latest = await latest_gateway(session, club_id)
    return latest if latest is not None and latest.status == DISABLED else None


async def _refuse_if_disabled(session, club_id: int) -> None:
    if await admin_disabled(session, club_id) is not None:
        raise _refuse(409, "MyOtherApp has turned off your own Razorpay. Talk to us.", "GATEWAY_DISABLED")


async def connect(session, club, user, key_id: str, key_secret: str, *, request=None) -> ClubPaymentGateways:
    """Save a club's keys as its new active gateway, once Razorpay accepts them."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    await _refuse_if_disabled(session, club.id)
    if not crypto.available():
        raise unavailable_error()
    key_id = (key_id or "").strip()
    key_secret = (key_secret or "").strip()
    _check_format(key_id, key_secret)
    try:
        await check_keys(key_id, key_secret)
    except KeysRefused as exc:
        reason = str(exc)
        raise _refuse(422, f"Razorpay didn't accept these keys: {reason}", "INVALID_KEYS", {"reason": reason}) from None
    except RazorpayUnreachable:
        raise _refuse(502, "Couldn't reach Razorpay to check the keys. Try again in a minute.", "RAZORPAY_UNREACHABLE") from None

    # Two saves at once must not both insert an active row.
    await session.execute(select(func.pg_advisory_xact_lock(_CONNECT_LOCK, club.id)))
    now = _now()
    previous = await live_gateway(session, club.id)
    if previous is not None:
        previous.status = REPLACED
        previous.ended_at = now
        # Before the insert: the partial unique index allows one live row.
        await session.flush()
    gateway = ClubPaymentGateways(
        club_id=club.id,
        provider=PROVIDER_RAZORPAY,
        mode=MODE_KEYS,
        key_id=key_id,
        key_secret_enc=crypto.encrypt(key_secret),
        webhook_secret_enc=crypto.encrypt(secrets.token_urlsafe(32)),
        webhook_token=secrets.token_urlsafe(24),
        status=ACTIVE,
        verified_at=now,
        created_by=getattr(user, "id", None),
    )
    session.add(gateway)
    await session.flush()
    await audit.record(
        session,
        actor=user,
        action="payment_gateway.connect",
        entity_type="club",
        entity_id=club.id,
        summary=f"Connected Razorpay account {key_id}",
        details={
            "gateway_id": gateway.id,
            "key_id": key_id,
            "mode": MODE_KEYS,
            "replaced_gateway_id": previous.id if previous is not None else None,
        },
        request=request,
    )
    await notify(
        session,
        "payment_gateway_connected",
        title=f"{club.name} connected its own Razorpay account",
        body=f"Key {key_id}. New ticket and merch payments go to the club's account.",
        club=club,
        data={"gateway_id": gateway.id, "key_id": key_id, "by": "club"},
        dedupe=False,
    )
    await session.commit()
    return gateway


async def _recheck(session, gateway: ClubPaymentGateways) -> str:
    """Ask Razorpay whether a live gateway's saved keys work, and say so on
    the row: `ok` (active), `refused` or `unreadable` (failed, alerted),
    `unreachable` (the status stays: not the keys' fault)."""
    try:
        secret = crypto.decrypt(gateway.key_secret_enc)
    except crypto.Unavailable:
        await mark_failed(session, gateway, "The saved keys can't be read any more. Paste them again.")
        return "unreadable"
    try:
        await check_keys(gateway.key_id, secret)
    except KeysRefused as exc:
        await mark_failed(session, gateway, f"Razorpay refused the keys: {exc}")
        return "refused"
    except RazorpayUnreachable:
        gateway.last_error = "Couldn't reach Razorpay to check the keys. Try again in a minute."
        return "unreachable"
    gateway.status = ACTIVE
    gateway.verified_at = _now()
    gateway.last_error = None
    return "ok"


async def verify(session, club, user, *, request=None) -> ClubPaymentGateways:
    """Ask Razorpay again whether the saved keys work."""
    from app.api.audit import service as audit

    await _refuse_if_disabled(session, club.id)
    gateway = await live_gateway(session, club.id)
    if gateway is None:
        raise _refuse(404, "Connect your Razorpay account first.", "NO_GATEWAY")
    if not crypto.available():
        raise unavailable_error()
    was = gateway.status
    result = await _recheck(session, gateway)
    await audit.record(
        session,
        actor=user,
        action="payment_gateway.verify",
        entity_type="club",
        entity_id=club.id,
        summary=f"Checked Razorpay account {gateway.key_id}: {result}",
        details={"gateway_id": gateway.id, "key_id": gateway.key_id, "result": result,
                 "previous_status": was, "status": gateway.status},
        request=request,
    )
    await session.commit()
    return gateway


async def disconnect(session, club, user, *, request=None) -> ClubPaymentGateways | None:
    """New orders go to the platform. Orders already made keep their gateway."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    await _refuse_if_disabled(session, club.id)
    gateway = await live_gateway(session, club.id)
    if gateway is None:
        return None
    gateway.status = DISCONNECTED
    gateway.ended_at = _now()
    await audit.record(
        session,
        actor=user,
        action="payment_gateway.disconnect",
        entity_type="club",
        entity_id=club.id,
        summary=f"Disconnected Razorpay account {gateway.key_id}",
        details={"gateway_id": gateway.id, "key_id": gateway.key_id},
        request=request,
    )
    await notify(
        session,
        "payment_gateway_disconnected",
        title=f"{club.name} disconnected its own Razorpay account",
        body="New ticket and merch payments go to the platform's account again.",
        club=club,
        data={"gateway_id": gateway.id, "key_id": gateway.key_id, "by": "club"},
        dedupe=False,
    )
    await session.commit()
    return gateway


async def disable(session, club_id: int, admin, *, request=None) -> ClubPaymentGateways:
    """A platform admin switches a club's account off; the club falls back to the platform."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    gateway = await live_gateway(session, club_id)
    if gateway is None:
        raise _refuse(404, "This club has no Razorpay account connected.", "NO_GATEWAY")
    gateway.status = DISABLED
    gateway.ended_at = _now()
    name = await _club_name(session, club_id)
    who = getattr(admin, "full_name", None) or getattr(admin, "email", None) or "An admin"
    await audit.record(
        session,
        actor=admin,
        action="payment_gateway.disable",
        entity_type="club",
        entity_id=club_id,
        summary=f"Disabled {name}'s Razorpay account {gateway.key_id}",
        details={"gateway_id": gateway.id, "key_id": gateway.key_id},
        request=request,
    )
    await notify(
        session,
        "payment_gateway_disconnected",
        title=f"{who} disabled {name}'s own Razorpay account",
        body="New ticket and merch payments go to the platform's account.",
        club=club_id,
        data={"gateway_id": gateway.id, "key_id": gateway.key_id, "by": "admin"},
        dedupe=False,
    )
    await session.commit()
    return gateway


async def enable(session, club_id: int, admin, *, request=None) -> ClubPaymentGateways:
    """
    An admin lifts their disable. The stored keys are checked again: still
    good, the gateway is active and takes the club's payments; refused,
    unreadable or unchecked, it is `failed` with the reason, and the club can
    check it again or paste new keys.
    """
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit

    gateway = await admin_disabled(session, club_id)
    if gateway is None:
        if await latest_gateway(session, club_id) is None:
            raise _refuse(404, "This club has never connected a Razorpay account.", "NO_GATEWAY")
        raise _refuse(409, "This club's own Razorpay isn't turned off.", "NOT_DISABLED")
    if not crypto.available():
        raise unavailable_error()
    await session.execute(select(func.pg_advisory_xact_lock(_CONNECT_LOCK, club_id)))
    if await live_gateway(session, club_id) is not None:
        raise _refuse(409, "This club has another Razorpay account connected.", "NOT_DISABLED")

    # Live again (failed first: one live row per club), then checked.
    gateway.status = FAILED
    gateway.ended_at = None
    await session.flush()
    result = await _recheck(session, gateway)
    name = await _club_name(session, club_id)
    who = getattr(admin, "full_name", None) or getattr(admin, "email", None) or "An admin"
    await audit.record(
        session,
        actor=admin,
        action="payment_gateway.enable",
        entity_type="club",
        entity_id=club_id,
        summary=f"Turned {name}'s Razorpay account {gateway.key_id} back on: {result}",
        details={"gateway_id": gateway.id, "key_id": gateway.key_id, "result": result, "status": gateway.status},
        request=request,
    )
    await notify(
        session,
        "payment_gateway_connected",
        title=f"{who} turned {name}'s own Razorpay account back on",
        body=(
            "The keys still work: new ticket and merch payments go to the club's account."
            if gateway.status == ACTIVE
            else f"The keys didn't pass the check ({gateway.last_error}). Payments stay on the platform's account until the club fixes them."
        ),
        club=club_id,
        data={"gateway_id": gateway.id, "key_id": gateway.key_id, "by": "admin", "status": gateway.status},
        dedupe=False,
    )
    await session.commit()
    return gateway


# ─── What is shown ──────────────────────────────────────────────────────


async def gateway_out(session, club_id: int, *, owner: bool = True) -> dict:
    """The club's page. The webhook secret only to the owner, and only while
    connected: they paste it into Razorpay. The key secret never. A team
    member sees the rest, and cannot change it."""
    available = crypto.available()
    out = {
        "available": available,
        "connected": False,
        "status": None,
        "mode": None,
        "key_id": None,
        "webhook_url": None,
        "webhook_secret": None,
        "webhook_events": list(WEBHOOK_EVENTS),
        "verified_at": None,
        "last_webhook_at": None,
        "last_error": None,
        "can_manage": bool(owner and available),
    }
    row = await live_gateway(session, club_id) or await latest_gateway(session, club_id)
    if row is None:
        return out
    connected = row.status in LIVE
    out.update(
        connected=connected,
        status=row.status,
        mode=row.mode,
        key_id=row.key_id,
        verified_at=row.verified_at,
        last_webhook_at=row.last_webhook_at,
        last_error=row.last_error,
    )
    if row.status == DISABLED:
        # Until an admin lifts it (enable).
        out["can_manage"] = False
    if connected:
        out["webhook_url"] = webhook_url(row.webhook_token)
        if owner:
            try:
                out["webhook_secret"] = crypto.decrypt(row.webhook_secret_enc)
            except crypto.Unavailable:
                out["webhook_secret"] = None
    return out


def admin_item(row: ClubPaymentGateways) -> dict:
    club = row.club
    return {
        "id": row.id,
        "club": {"id": club.id, "name": club.name, "slug": club.slug} if club is not None else None,
        "status": row.status,
        "mode": row.mode,
        "key_id": row.key_id,
        "verified_at": row.verified_at,
        "last_webhook_at": row.last_webhook_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "ended_at": row.ended_at,
    }


async def admin_list(session, *, status: str | None, q: str | None, offset: int, limit: int) -> tuple[list, int]:
    from sqlalchemy.orm import contains_eager

    from app.api.clubs.models import Clubs

    filters = []
    if status:
        if status not in STATUSES:
            raise _refuse(422, f"status is one of {', '.join(STATUSES)}", "INVALID_STATUS")
        filters.append(ClubPaymentGateways.status == status)
    else:
        # A replaced row is history: the club's current one follows it.
        filters.append(ClubPaymentGateways.status != REPLACED)
    if q and q.strip():
        term = f"%{q.strip()}%"
        filters.append(or_(Clubs.name.ilike(term), Clubs.slug.ilike(term), ClubPaymentGateways.key_id.ilike(term)))
    base = select(ClubPaymentGateways).join(Clubs, Clubs.id == ClubPaymentGateways.club_id).where(*filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(
        base.options(contains_eager(ClubPaymentGateways.club))
        .order_by(ClubPaymentGateways.created_at.desc(), ClubPaymentGateways.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows), int(total or 0)


# ─── Webhooks ───────────────────────────────────────────────────────────


async def by_webhook_token(session, token: str) -> ClubPaymentGateways | None:
    if not token or not _TOKEN.match(token):
        return None
    return await session.scalar(select(ClubPaymentGateways).where(ClubPaymentGateways.webhook_token == token))


def signature_ok(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


async def bad_signature(session, gateway: ClubPaymentGateways) -> None:
    """Most likely the secret pasted into Razorpay is not the one we gave.
    Said on the club's page, and to the admins at most once in ten minutes."""
    from app.api.admin_alerts import notify

    gateway.last_error = "A webhook arrived with a signature that doesn't match. Check the webhook secret in Razorpay."
    name = await _club_name(session, gateway.club_id)
    await notify(
        session,
        "payment_gateway_failed",
        title=f"A webhook for {name}'s Razorpay account had a bad signature",
        body="Either the secret in the club's Razorpay webhook is wrong, or someone else is calling its webhook address.",
        club=gateway.club_id,
        data={"gateway_id": gateway.id, "key_id": gateway.key_id, "reason": "bad_webhook_signature"},
    )

