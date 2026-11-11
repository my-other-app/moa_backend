"""The objects a webhook carries in `data.object`, in the shape the partner
API's GET returns (feature-register/integrations-api-contract.md, Resources).

Defined here so the partner API (app/api/partner) can serve the same: each
`load_<type>(session, id)` reads what it needs by id, soft-deleted rows
included, and returns a plain JSON-ready dict; `None` when there is no such
row. Ids are strings; times ISO 8601 in India's time with the offset; money
in paise with `currency: "INR"`.

Contact fields -- a person's name, email, phone and answers -- are present
here and taken out by `redact` for an owner not allowed them (attendees:read
for a partner; the DPDP tick for a club's own endpoint).

`livemode` is the object's own (a test-mode object, made by a moa_test_ key,
has `livemode: false` once the partner API adds the column) and true
otherwise. `external_id` is per client: filled in for each endpoint's owner
when it is sent (delivery.py, seams.external_id).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core import money

IST = ZoneInfo("Asia/Kolkata")
TIMEZONE = "Asia/Kolkata"
CURRENCY = "INR"
INCLUDE_DELETED = {"include_deleted": True}

#: The fields `redact` takes out, per object.
CONTACT_FIELDS = {
    "registration": ("person", "answers"),
    "certificate": ("recipient_name",),
}


def when(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        from datetime import timezone

        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).isoformat()


def paise(value) -> int:
    return money.to_paise(value or 0)


def _sid(value) -> Optional[str]:
    return None if value is None else str(value)


def _livemode(row) -> bool:
    value = getattr(row, "livemode", True)
    return True if value is None else bool(value)


def redact(object_type: str, obj: Optional[dict]) -> Optional[dict]:
    """The object without its contact fields."""
    if not obj:
        return obj
    fields = CONTACT_FIELDS.get(object_type)
    if not fields:
        return obj
    return {k: v for k, v in obj.items() if k not in fields}


# ─── Events ──────────────────────────────────────────────────────────────


def event_status(event) -> str:
    """draft | published | ended | cancelled (the partner API's filter)."""
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


def _poster(event) -> tuple[Optional[str], Optional[str]]:
    """(url, the stored key behind it): the key says whether it changed --
    the url is signed afresh on every read when the CDN is off."""
    value = event.poster
    if not isinstance(value, dict):
        return None, None
    url = value.get("original") or value.get("large")
    key = (getattr(value, "variations", None) or {}).get("original")
    return url, key


def _ticket_type(t, event, follows: bool) -> dict:
    from app.api.ticketing.types import event_price

    price = event_price(event) if follows else money.rupees(t.price)
    return {
        "id": str(t.id), "name": t.name, "description": t.description,
        "price": {"amount": paise(price), "currency": CURRENCY} if price > 0 else None,
        "capacity": event.max_participants if follows else t.capacity,
        "sales_start": when(t.sales_start), "sales_end": when(t.sales_end),
        "hidden": bool(t.is_hidden), "sold": int(t.taken_count or 0),
    }


def event_object(event, ticket_types: list) -> dict:
    from app.api.ticketing.types import event_price, follows
    from app.core.public_urls import event_url

    starts = event.event_datetime
    ends = starts + timedelta(hours=float(event.duration or 0)) if starts else None
    online = bool(event.is_online)
    price = event_price(event)
    poster_url, _key = _poster(event)
    following = follows(ticket_types)
    return {
        "object": "event",
        "id": str(event.id),
        "club_id": _sid(event.club_id),
        "name": event.name,
        "slug": event.slug,
        "description": event.about,
        "status": event_status(event),
        "starts_at": when(starts),
        "ends_at": when(ends),
        "timezone": TIMEZONE,
        "venue": None if online else {
            "name": event.location_name, "address": None, "city": None,
            "lat": event.lat, "lng": event.lng, "map_url": event.location_link,
        },
        "online": {"url": event.url} if online else None,
        "poster_url": poster_url,
        "capacity": event.max_participants,
        "registration": {
            "opens_at": when(event.reg_startdate), "closes_at": when(event.reg_enddate),
            "requires_approval": bool(event.requires_approval),
        },
        "price": {"amount": paise(price), "currency": CURRENCY} if price > 0 else None,
        "free": not price > 0,
        "ticket_types": [_ticket_type(t, event, following) for t in ticket_types],
        "external_payments_allowed": bool(getattr(event, "external_payments_allowed", False)),
        "external_id": None,
        "public_url": event_url(event.slug),
        "livemode": _livemode(event),
        "created_at": when(event.created_at),
        "updated_at": when(event.updated_at),
    }


async def _event(session, event_id) -> Any:
    from app.api.events.models import Events

    return await session.scalar(select(Events).where(Events.id == int(event_id)).execution_options(**INCLUDE_DELETED))


async def _ticket_types(session, event_id: int) -> list:
    from app.api.ticketing.models import TicketTypes

    return list(await session.scalars(
        select(TicketTypes).where(TicketTypes.event_id == event_id, TicketTypes.is_deleted == False)  # noqa: E712
        .order_by(TicketTypes.sort_order, TicketTypes.id)
    ))


async def load_event(session, event_id) -> Optional[dict]:
    event = await _event(session, event_id)
    if event is None:
        return None
    return event_object(event, await _ticket_types(session, event.id))


async def compare_event(session, event_id) -> Optional[dict]:
    """The event as `event.updated` compares it (load_event, plus the poster's key)."""
    event = await _event(session, event_id)
    if event is None:
        return None
    out = event_object(event, await _ticket_types(session, event.id))
    # The poster's link is signed afresh on each read when the CDN is off:
    # its stored key says whether it changed.
    out["_poster_url"] = _poster(event)[1]
    for t in out["ticket_types"]:
        t.pop("sold", None)
    return out


# ─── Registrations ───────────────────────────────────────────────────────


def registration_status(reg, event) -> str:
    """confirmed | awaiting_payment | cancelled."""
    if reg.is_deleted:
        return "cancelled"
    owes = money.rupees(reg.actual_amount or 0) > 0
    if event is not None and event.has_fee and owes and not reg.is_paid:
        return "awaiting_payment"
    return "confirmed"


def _approval(request) -> Optional[dict]:
    if request is None:
        return None
    return {
        "status": request.status, "request_id": str(request.id),
        "submitted_at": when(request.submitted_at), "decided_at": when(request.decided_at),
    }


def _answers(value) -> dict:
    if isinstance(value, dict):
        return value
    return {}


def registration_object(reg, event, request=None) -> dict:
    from app.core.public_urls import ticket_url

    owed = money.rupees(reg.actual_amount or 0)
    if reg.ticket_revoked_at is not None:
        ticket_status = "revoked"
    elif reg.is_deleted:
        ticket_status = "cancelled"
    else:
        ticket_status = "valid"
    return {
        "object": "registration",
        "id": str(reg.id),
        "event_id": str(reg.event_id),
        "status": registration_status(reg, event),
        "person": {"name": reg.full_name, "email": reg.email, "phone": reg.phone},
        "answers": _answers(reg.additional_details),
        "role": getattr(reg.role, "value", reg.role),
        "ticket_type_id": _sid(reg.ticket_type_id),
        "ticket": {"code": reg.ticket_id, "status": ticket_status, "url": ticket_url(reg.ticket_id)},
        "admission_no": reg.admission_no,
        "payment": {
            "status": "free" if owed <= 0 else ("paid" if reg.is_paid else "unpaid"),
            "amount_due": paise(owed), "amount_paid": paise(reg.paid_amount), "currency": CURRENCY,
        },
        "approval": _approval(request),
        "checked_in_at": when(reg.attended_on) if reg.is_attended else None,
        "external_id": None,
        "livemode": _livemode(event) if event is not None else True,
        "created_at": when(reg.created_at),
        "updated_at": when(reg.updated_at),
        "cancelled_at": when(reg.deleted_at) if reg.is_deleted else None,
    }


def request_object(request, event) -> dict:
    """A registration request with no registration (yet): declined, or
    confirmed but not paid for. Shaped as a registration, its id the
    request's (`approval.request_id` says so)."""
    status = {"pending": "pending_approval", "approved": "awaiting_payment"}.get(request.status, request.status)
    return {
        "object": "registration",
        "id": str(request.id),
        "event_id": str(request.event_id),
        "status": status,
        "person": {"name": request.full_name, "email": request.email, "phone": request.phone},
        "answers": _answers(request.answers),
        "role": "attendee",
        "ticket_type_id": _sid(request.ticket_type_id),
        "ticket": None,
        "admission_no": None,
        "payment": None,
        "approval": _approval(request),
        "checked_in_at": None,
        "external_id": None,
        "livemode": _livemode(event) if event is not None else True,
        "created_at": when(request.created_at),
        "updated_at": when(request.updated_at),
        "cancelled_at": None,
    }


async def _registration(session, reg_id) -> Any:
    from app.api.events.models import EventRegistrationsLink

    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id).execution_options(**INCLUDE_DELETED)
    )


async def _request_of(session, reg_id) -> Any:
    from app.api.approvals.models import RegistrationRequests

    return await session.scalar(
        select(RegistrationRequests).where(RegistrationRequests.registration_id == reg_id)
        .order_by(RegistrationRequests.submitted_at.desc()).limit(1)
    )


async def load_registration(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    if reg is None:
        return None
    return registration_object(reg, await _event(session, reg.event_id), await _request_of(session, reg.id))


async def load_request(session, request_id) -> Optional[dict]:
    """A request's registration when it has one, else the request itself."""
    from app.api.approvals.models import RegistrationRequests

    request = await session.get(RegistrationRequests, request_id)
    if request is None:
        return None
    if request.registration_id is not None:
        reg = await _registration(session, request.registration_id)
        if reg is not None:
            return registration_object(reg, await _event(session, reg.event_id), request)
    return request_object(request, await _event(session, request.event_id))


# ─── Check-ins ───────────────────────────────────────────────────────────


def checkin_object(checkin, event=None, *, deleted: bool = False) -> dict:
    via = getattr(checkin.via, "value", checkin.via)
    return {
        "object": "checkin",
        "id": str(checkin.id),
        "registration_id": str(checkin.registration_id),
        "event_id": str(checkin.event_id),
        "checked_in_at": when(checkin.checked_in_at),
        "scanned_at": when(checkin.scanned_at),
        "via": via,
        "gate": checkin.door_label,
        "device": _sid(checkin.kiosk_device_id),
        "deleted": deleted,
        "livemode": _livemode(event) if event is not None else True,
    }


async def load_checkin(session, checkin_id) -> Optional[dict]:
    from app.api.checkins.models import CheckIns

    checkin = await session.get(CheckIns, checkin_id)
    if checkin is None:
        return None
    return checkin_object(checkin, await _event(session, checkin.event_id))


# ─── Certificates ────────────────────────────────────────────────────────


def certificate_object(reg, event) -> dict:
    from app.core.public_urls import certificate_url

    return {
        "object": "certificate",
        "number": reg.certificate_no,
        "registration_id": str(reg.id),
        "event_id": str(reg.event_id),
        "recipient_name": reg.full_name,
        "issued_at": when(reg.certificate_issued_at),
        "revoked_at": when(reg.certificate_revoked_at),
        "verify_url": certificate_url(reg.certificate_public_id) if reg.certificate_public_id else None,
        "livemode": _livemode(event) if event is not None else True,
    }


async def load_certificate(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    if reg is None:
        return None
    return certificate_object(reg, await _event(session, reg.event_id))


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
    """A ticket's payment: amounts and status only."""
    from app.api.payments.models import PaymentLogs, PaymentOrders

    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == log_id).execution_options(**INCLUDE_DELETED))
    if log is None:
        return None
    order = await session.scalar(select(PaymentOrders).where(PaymentOrders.id == log.order_id).execution_options(**INCLUDE_DELETED))
    payload = dict((order.payload if order is not None else None) or {})
    reg_id = payload.get("event_registration_id")
    event_id = payload.get("event_id")
    reg = await _registration(session, reg_id) if reg_id else None
    if reg is not None:
        event_id = reg.event_id
    event = await _event(session, event_id) if event_id else None
    refunded = await _refunded(session, log)
    amount = paise(log.amount)
    status = getattr(log.status, "value", log.status)
    if refunded >= amount and amount > 0:
        status = "refunded"
    elif refunded > 0:
        status = "partially_refunded"
    return {
        "object": "payment",
        "id": str(log.id),
        "registration_id": _sid(reg_id),
        "event_id": _sid(event_id),
        "status": status,
        "amount": amount,
        "amount_refunded": refunded,
        "currency": (order.currency if order is not None and order.currency else CURRENCY),
        "created_at": when(log.created_at),
        "updated_at": when(log.updated_at),
        "livemode": _livemode(event) if event is not None else True,
    }


# ─── The club ────────────────────────────────────────────────────────────


def club_object(club, socials=None) -> dict:
    from app.config import settings

    logo = club.logo if isinstance(club.logo, dict) else None
    cover = club.cover if isinstance(club.cover, dict) else None
    links = {
        "instagram": getattr(socials, "instagram", None), "linkedin": getattr(socials, "linkedin", None),
        "youtube": getattr(socials, "youtube", None), "website": getattr(socials, "website", None),
    }
    return {
        "object": "club",
        "id": str(club.id),
        "name": club.name,
        "slug": club.slug,
        "tagline": club.tagline,
        "about": club.about,
        "links": links,
        "logo_url": (logo or {}).get("original") or (logo or {}).get("large"),
        "cover_url": (cover or {}).get("original") or (cover or {}).get("large"),
        "location": club.location_name,
        "public_url": settings.PUBLIC_CLUB_URL.format(slug=club.slug),
        "livemode": True,
        "created_at": when(club.created_at),
        "updated_at": when(club.updated_at),
    }


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
    return None if club is None else club_object(club, socials)


async def compare_club(session, club_id) -> Optional[dict]:
    club, socials = await _club(session, club_id)
    if club is None:
        return None
    out = club_object(club, socials)
    # Signed afresh on each read when the CDN is off: the stored keys say whether they changed.
    out["_logo_url"] = (getattr(club.logo, "variations", None) or {}).get("original") if isinstance(club.logo, dict) else None
    out["_cover_url"] = (getattr(club.cover, "variations", None) or {}).get("original") if isinstance(club.cover, dict) else None
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
_NOISE = {"updated_at"}
#: A link signed afresh on each read, compared by the stored key beside it
#: ("_poster_url" for "poster_url"): `previous` names the link as it was.
_SIGNED = ("poster_url", "logo_url", "cover_url")


def diff(before: Optional[dict], after: Optional[dict]) -> dict:
    """The top-level fields that differ, as they were before."""
    if not before or not after:
        return {}
    out = {}
    for k in set(before) | set(after):
        if k in _NOISE or k.startswith("_"):
            continue
        if k in _SIGNED and f"_{k}" in before:
            if before.get(f"_{k}") != after.get(f"_{k}"):
                out[k] = before.get(k)
            continue
        if before.get(k) != after.get(k):
            out[k] = before.get(k)
    return out
