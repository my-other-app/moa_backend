"""The objects a webhook carries in `data.object`: exactly what the partner
API's GET returns, built by its own builders (app/api/partner/shapes.py), so
a webhook and a GET never disagree.

Each `load_<kind>(session, id)` reads the row by id, soft-deleted rows
included, and returns the object with its contact fields in it (a person's
name, email, phone and answers; a certificate's name); `redact` takes them
out for an endpoint whose owner may not have them. `None`: no such row.

A payment is `shapes.payment_log_object` (amounts and status only) with what
a refund needs on top: `amount_refunded`, and `status` refunded or
partially_refunded once money went back.

`external_id` is per client: filled in for each endpoint's owner when it is
sent (delivery.py, seams.external_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core import money as money_mod

IST = ZoneInfo("Asia/Kolkata")
INCLUDE_DELETED = {"include_deleted": True}

#: The fields `redact` takes out, per object.
CONTACT_FIELDS = {
    "registration": ("person", "answers"),
    "certificate": ("name",),
}


def _shapes():
    from app.api.partner import shapes

    return shapes


def when(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).isoformat()


def paise(value) -> int:
    return money_mod.to_paise(value or 0)


def _stored_key(value) -> Optional[str]:
    """The stored key behind a picture: its link is signed afresh on each read
    when the CDN is off, so the key says whether it changed."""
    return (getattr(value, "variations", None) or {}).get("original") if isinstance(value, dict) else None


def redact(object_type: str, obj: Optional[dict]) -> Optional[dict]:
    """The object (or a `previous`) without its contact fields."""
    if not obj:
        return obj
    fields = CONTACT_FIELDS.get(object_type)
    if not fields:
        return obj
    return {k: v for k, v in obj.items() if k not in fields}


def event_status(event) -> str:
    """draft | published | ended | cancelled, by the event's own status: what
    a change moves, so what decides whether one is told (the GET also reads
    a past end as ended, with no change behind it)."""
    from app.api.closeout.models import EventStatus

    if getattr(event, "is_deleted", False):
        return "cancelled"
    status = event.lifecycle or EventStatus.published
    if status is EventStatus.draft:
        return "draft"
    if status is EventStatus.cancelled:
        return "cancelled"
    if status in (EventStatus.closed, EventStatus.archived):
        return "ended"
    return "published"


# ─── Rows by id ──────────────────────────────────────────────────────────


async def _event(session, event_id) -> Any:
    from app.api.events.models import Events

    if event_id is None:
        return None
    return await session.scalar(select(Events).where(Events.id == int(event_id)).execution_options(**INCLUDE_DELETED))


async def _registration(session, reg_id) -> Any:
    from app.api.events.models import EventRegistrationsLink

    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id).execution_options(**INCLUDE_DELETED)
    )


# ─── Events ──────────────────────────────────────────────────────────────


async def _event_object(session, event) -> dict:
    out = await _shapes().event_payload(session, event)
    if getattr(event, "is_deleted", False):
        # Deleted before anyone registered (event.cancelled): the GET finds
        # no such event any more; the webhook says what became of it.
        out["status"] = "cancelled"
        out["public_url"] = None
    return out


async def load_event(session, event_id) -> Optional[dict]:
    event = await _event(session, event_id)
    return None if event is None else await _event_object(session, event)


async def compare_event(session, event_id) -> Optional[dict]:
    """The event as `event.updated` compares it: the object, and the poster's stored key."""
    event = await _event(session, event_id)
    if event is None:
        return None
    out = await _event_object(session, event)
    out["_poster_url"] = _stored_key(event.poster)
    return out


# ─── Registrations ───────────────────────────────────────────────────────


async def load_registration(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    if reg is None:
        return None
    return await _shapes().registration_payload(session, reg, await _event(session, reg.event_id), contact=True)


async def load_request(session, request_id) -> Optional[dict]:
    """A request's registration when it has one, else the request itself
    (shaped as a registration under review, its id the request's)."""
    from app.api.approvals.models import RegistrationRequests

    request = await session.get(RegistrationRequests, request_id)
    if request is None:
        return None
    if request.registration_id is not None:
        reg = await _registration(session, request.registration_id)
        if reg is not None:
            return await _shapes().registration_payload(session, reg, await _event(session, reg.event_id), contact=True)
    return _shapes().request_object(request, await _event(session, request.event_id), contact=True)


# ─── Check-ins and certificates ──────────────────────────────────────────


def checkin_object(checkin, *, deleted: bool = False) -> dict:
    """The check-in, with `deleted` (true for checkin.deleted: the check-in as it was)."""
    out = _shapes().checkin_object(checkin)
    out["deleted"] = deleted
    return out


async def load_checkin(session, checkin_id) -> Optional[dict]:
    from app.api.checkins.models import CheckIns

    checkin = await session.get(CheckIns, checkin_id)
    return None if checkin is None else checkin_object(checkin)


async def load_certificate(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    return None if reg is None else _shapes().certificate_object(reg, contact=True)


# ─── Payments ────────────────────────────────────────────────────────────


async def _refunded(session, log) -> int:
    """Paise refunded of a payment so far: in full when it is marked so, else
    the parts that went (a club's refunds and the admins')."""
    from app.api.admin_ops.models import PaymentRefunds
    from app.api.club_ops.models import ClubRefunds
    from app.api.payments.models import PaymentStatus

    if log.status is PaymentStatus.refunded:
        return paise(log.amount)
    club = await session.scalar(
        select(func.coalesce(func.sum(ClubRefunds.amount), 0)).where(ClubRefunds.payment_log_id == log.id, ClubRefunds.status == "succeeded")
    )
    admin = await session.scalar(
        select(func.coalesce(func.sum(PaymentRefunds.amount), 0)).where(PaymentRefunds.payment_log_id == log.id)
    )
    return min(paise(log.amount), paise(club) + paise(admin))


async def load_payment(session, log_id) -> Optional[dict]:
    """A ticket's payment: amounts and status only, never anyone's details."""
    from app.api.payments.models import PaymentLogs, PaymentOrders

    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log_id).execution_options(**INCLUDE_DELETED))
    if log is None:
        return None
    order = await session.scalar(select(PaymentOrders).where(PaymentOrders.id == log.order_id).execution_options(**INCLUDE_DELETED))
    payload = dict((order.payload if order is not None else None) or {})
    reg_id = payload.get("event_registration_id")
    reg = await _registration(session, reg_id) if reg_id else None
    out = _shapes().payment_log_object(log, reg, event_id=reg.event_id if reg is not None else payload.get("event_id"))
    refunded = await _refunded(session, log)
    out["amount_refunded"] = refunded
    if refunded >= out["amount"] and out["amount"] > 0:
        out["status"] = "refunded"
    elif refunded > 0:
        out["status"] = "partially_refunded"
    return out


# ─── The club ────────────────────────────────────────────────────────────


async def _club(session, club_id) -> tuple[Any, Any]:
    from app.api.clubs.models import Clubs, ClubSocials

    club = await session.scalar(select(Clubs).where(Clubs.id == int(club_id)).execution_options(**INCLUDE_DELETED))
    if club is None:
        return None, None
    socials = await session.scalar(
        select(ClubSocials).where(ClubSocials.club_id == club.id).order_by(ClubSocials.id.desc()).limit(1)
    )
    return club, socials


async def load_club(session, club_id) -> Optional[dict]:
    club, socials = await _club(session, club_id)
    return None if club is None else _shapes().club_object(club, socials)


async def compare_club(session, club_id) -> Optional[dict]:
    club, socials = await _club(session, club_id)
    if club is None:
        return None
    out = _shapes().club_object(club, socials)
    out["_logo_url"] = _stored_key(club.logo)
    # Not in the object, but the profile's: a new cover is a change.
    out["_cover"] = _stored_key(club.cover)
    return out


# ─── By name ─────────────────────────────────────────────────────────────

LOADERS = {
    "event": load_event,
    "registration": load_registration,
    "request": load_request,
    "checkin": load_checkin,
    "certificate": load_certificate,
    "payment": load_payment,
    "club": load_club,
}

#: What `previous` is computed from, per object (the rest compare as they load).
COMPARERS = {"event": compare_event, "club": compare_club}


async def load(session, kind: str, object_id) -> Optional[dict]:
    return await LOADERS[kind](session, object_id)


async def compare_form(session, kind: str, object_id) -> Optional[dict]:
    fn = COMPARERS.get(kind) or LOADERS[kind]
    return await fn(session, object_id)


#: Fields that change by themselves and are never "what changed" alone.
_NOISE = {"updated_at", "registrations_count"}
#: A link signed afresh on each read, compared by the stored key beside it
#: ("_poster_url" for "poster_url"): `previous` names the link as it was.
_SIGNED = ("poster_url", "logo_url")


def diff(before: Optional[dict], after: Optional[dict]) -> dict:
    """The top-level fields that differ, as they were before. A private
    field ("_cover") that differs counts as a change with nothing to name."""
    if not before or not after:
        return {}
    out = {}
    hidden_change = False
    for k in set(before) | set(after):
        if k in _NOISE:
            continue
        if k.startswith("_"):
            if k[1:] not in _SIGNED and before.get(k) != after.get(k):
                hidden_change = True
            continue
        if k in _SIGNED and f"_{k}" in before:
            if before.get(f"_{k}") != after.get(f"_{k}"):
                out[k] = before.get(k)
            continue
        if before.get(k) != after.get(k):
            out[k] = before.get(k)
    if hidden_change and not out:
        out["updated_at"] = before.get("updated_at")
    return out
