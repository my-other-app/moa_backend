"""The objects a webhook carries in `data.object`: the shape the partner API's
GET returns (feature-register/integrations-api-contract.md, Resources).

The partner API's own serializers (app/api/partner/shapes.py: event_object,
registration_object with its RegistrationContext, request_object,
checkin_object, certificate_object, club_object) are used whenever that
module is there, so a webhook and a GET never disagree. Without it -- this
module on its own -- the fallbacks below give the same fields. Payments are
webhooks' own (the partner API has no payment resource): amounts and status.

Each `load_<kind>(session, id)` reads what it needs by id, soft-deleted rows
included, and returns a JSON-ready dict, with the contact fields (a person's
name, email, phone, answers; a certificate's name) in it; `redact` takes them
out for an endpoint whose owner may not have them. `None`: no such row.

`external_id` is per client: filled in for each endpoint's owner when it is
sent (delivery.py, seams.external_id).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.core import money as money_mod

IST = ZoneInfo("Asia/Kolkata")
TIMEZONE = "Asia/Kolkata"
CURRENCY = "INR"
INCLUDE_DELETED = {"include_deleted": True}

#: The fields `redact` takes out, per object.
CONTACT_FIELDS = {
    "registration": ("person", "answers"),
    "certificate": ("name",),
}

_UNSET = object()
_shapes_module: Any = _UNSET


def partner_shapes():
    """app.api.partner.shapes when the partner API is there, else None."""
    global _shapes_module
    if _shapes_module is _UNSET:
        try:
            from app.api.partner import shapes
        except ImportError:
            shapes = None
        _shapes_module = shapes
    return _shapes_module


def when(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).isoformat()


def paise(value) -> int:
    return money_mod.to_paise(value or 0)


def _money(amount_paise: Optional[int]) -> Optional[dict]:
    return None if amount_paise is None else {"amount": int(amount_paise), "currency": CURRENCY}


def _sid(value) -> Optional[str]:
    return None if value is None else str(value)


def _picture(value) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("original") or next(iter(value.values()), None)
    return None


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


# ─── Events ──────────────────────────────────────────────────────────────


def event_status(event) -> str:
    """draft | published | ended | cancelled, by the event's own status (what
    a change moves; the partner API's GET also reads a past end as ended)."""
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


def _ticket_type(t) -> dict:
    return {
        "id": str(t.id), "name": t.name, "price": _money(paise(t.price)), "capacity": t.capacity,
        "sales_start": when(t.sales_start), "sales_end": when(t.sales_end), "hidden": bool(t.is_hidden),
    }


def event_object(event, ticket_types: list) -> dict:
    from app.core.public_urls import event_url

    shapes = partner_shapes()
    if shapes is not None:
        return shapes.event_object(event, ticket_types=ticket_types)
    status = event_status(event)
    starts = event.event_datetime
    venue = None
    if event.location_name or event.lat is not None:
        venue = {"name": event.location_name, "address": None, "city": None, "lat": event.lat, "lng": event.lng, "map_url": event.location_link}
    return {
        "id": str(event.id),
        "object": "event",
        "club_id": _sid(event.club_id),
        "name": event.name,
        "slug": event.slug,
        "description": event.about,
        "status": status,
        "starts_at": when(starts),
        "ends_at": when(starts + timedelta(hours=float(event.duration or 0)) if starts else None),
        "timezone": TIMEZONE,
        "venue": venue,
        "online": {"url": event.url} if event.is_online and event.url else None,
        "poster_url": _picture(event.poster),
        "capacity": event.max_participants,
        "registration": {
            "opens_at": when(event.reg_startdate), "closes_at": when(event.reg_enddate),
            "requires_approval": bool(event.requires_approval),
        },
        "price": _money(paise(event.reg_fee)) if event.has_fee else None,
        "ticket_types": [_ticket_type(t) for t in ticket_types],
        "external_payments_allowed": bool(getattr(event, "external_payments_allowed", False)),
        "registrations_count": int(event.going_count or 0),
        "external_id": None,
        "public_url": event_url(event.slug) if status in ("published", "ended") else None,
        "livemode": True,
        "created_at": when(event.created_at),
        "updated_at": when(event.updated_at),
    }


async def _event(session, event_id) -> Any:
    from app.api.events.models import Events

    if event_id is None:
        return None
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
    """The event as `event.updated` compares it: the object, and the poster's stored key."""
    event = await _event(session, event_id)
    if event is None:
        return None
    out = event_object(event, await _ticket_types(session, event.id))
    out["_poster_url"] = _stored_key(event.poster)
    return out


# ─── Registrations ───────────────────────────────────────────────────────


def _registration_status(reg, event) -> str:
    if reg.is_deleted:
        return "cancelled"
    if event is not None and event.has_fee and not reg.is_paid:
        return "awaiting_payment"
    return "confirmed"


def _payment_of(reg, event) -> dict:
    if event is None or not event.has_fee:
        return {"status": "not_required", "amount": 0, "currency": CURRENCY}
    if reg.is_paid:
        paid = paise(reg.paid_amount)
        if reg.payment_receipt:
            return {"status": "paid_online", "amount": paid, "currency": CURRENCY}
        if paid > 0:
            return {"status": "paid_at_door", "amount": paid, "currency": CURRENCY}
        return {"status": "not_required", "amount": 0, "currency": CURRENCY}
    return {"status": "awaiting_payment", "amount": paise(reg.actual_amount), "currency": CURRENCY}


def _answers(value) -> dict:
    return value if isinstance(value, dict) else {}


def registration_fallback(reg, event, request=None, type_name: Optional[str] = None) -> dict:
    status = _registration_status(reg, event)
    if reg.is_deleted:
        ticket_status = "cancelled"
    elif reg.ticket_revoked_at is not None:
        ticket_status = "revoked"
    elif status == "awaiting_payment":
        ticket_status = "awaiting_payment"
    else:
        ticket_status = "valid"
    approval = None
    if request is not None:
        approval = {"status": "approved", "decided_at": when(request.decided_at), "request_id": str(request.id)}
    elif event is not None and event.requires_approval:
        approval = {"status": "added_directly", "decided_at": None, "request_id": None}
    return {
        "id": str(reg.id),
        "object": "registration",
        "event_id": str(reg.event_id),
        "status": status,
        "ticket": {"code": reg.ticket_id, "qr_url": None, "status": ticket_status},
        "ticket_type": {"id": str(reg.ticket_type_id), "name": type_name} if reg.ticket_type_id is not None else None,
        "approval": approval,
        "payment": _payment_of(reg, event),
        "checked_in_at": when(reg.attended_on) if reg.is_attended else None,
        "request_id": str(request.id) if request is not None else None,
        "external_id": None,
        "livemode": True,
        "created_at": when(reg.created_at),
        "updated_at": when(reg.updated_at),
        "person": {"name": reg.full_name, "email": reg.email, "phone": reg.phone},
        "answers": _answers(reg.additional_details),
    }


_REQUEST_STATUS = {"pending": "pending_approval", "approved": "pending_approval", "declined": "declined",
                   "withdrawn": "withdrawn", "lapsed": "cancelled", "closed": "cancelled"}


def request_fallback(request, event) -> dict:
    status = _REQUEST_STATUS.get(request.status, "pending_approval")
    approval = {"pending": "pending", "approved": "approved", "declined": "declined", "withdrawn": "withdrawn"}.get(request.status, request.status)
    paid = event is not None and event.has_fee and request.amount
    return {
        "id": str(request.id),
        "object": "registration",
        "event_id": str(request.event_id),
        "status": status,
        "ticket": None,
        "ticket_type": {"id": str(request.ticket_type_id), "name": None} if request.ticket_type_id is not None else None,
        "approval": {"status": approval, "decided_at": when(request.decided_at), "request_id": str(request.id)},
        "payment": ({"status": "awaiting_payment", "amount": paise(request.amount), "currency": CURRENCY} if paid
                    else {"status": "not_required", "amount": 0, "currency": CURRENCY}),
        "checked_in_at": None,
        "request_id": str(request.id),
        "external_id": None,
        "livemode": True,
        "created_at": when(request.submitted_at or request.created_at),
        "updated_at": when(request.updated_at),
        "person": {"name": request.full_name, "email": request.email, "phone": request.phone},
        "answers": _answers(request.answers),
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


async def registration_object(session, reg, event) -> dict:
    shapes = partner_shapes()
    if shapes is not None:
        ctx = await shapes.RegistrationContext.load(session, [reg])
        return shapes.registration_object(reg, event, ctx, contact=True)
    from app.api.ticketing import types as types_mod

    names = await types_mod.names(session, [reg.ticket_type_id]) if reg.ticket_type_id is not None else {}
    return registration_fallback(reg, event, await _request_of(session, reg.id), names.get(reg.ticket_type_id))


async def load_registration(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    if reg is None:
        return None
    return await registration_object(session, reg, await _event(session, reg.event_id))


async def load_request(session, request_id) -> Optional[dict]:
    """A request's registration when it has one, else the request itself."""
    from app.api.approvals.models import RegistrationRequests

    request = await session.get(RegistrationRequests, request_id)
    if request is None:
        return None
    if request.registration_id is not None:
        reg = await _registration(session, request.registration_id)
        if reg is not None:
            return await registration_object(session, reg, await _event(session, reg.event_id))
    event = await _event(session, request.event_id)
    shapes = partner_shapes()
    if shapes is not None:
        return shapes.request_object(request, event, contact=True)
    return request_fallback(request, event)


# ─── Check-ins and certificates ──────────────────────────────────────────


def checkin_object(checkin, *, deleted: bool = False) -> dict:
    shapes = partner_shapes()
    if shapes is not None:
        out = shapes.checkin_object(checkin)
    else:
        out = {
            "id": str(checkin.id),
            "object": "checkin",
            "registration_id": str(checkin.registration_id),
            "event_id": str(checkin.event_id),
            "checked_in_at": when(checkin.checked_in_at),
            "scanned_at": when(checkin.scanned_at),
            "gate": checkin.door_label,
            "via": getattr(checkin.via, "value", checkin.via),
            "duplicate": False,
            "livemode": True,
            "updated_at": when(checkin.updated_at),
        }
    out["deleted"] = deleted
    return out


async def load_checkin(session, checkin_id) -> Optional[dict]:
    from app.api.checkins.models import CheckIns

    checkin = await session.get(CheckIns, checkin_id)
    return None if checkin is None else checkin_object(checkin)


def certificate_object(reg) -> dict:
    shapes = partner_shapes()
    if shapes is not None:
        return shapes.certificate_object(reg, contact=True)
    from app.api.events.certificates import number_of, public_url_of

    return {
        "number": number_of(reg),
        "object": "certificate",
        "registration_id": str(reg.id),
        "event_id": str(reg.event_id),
        "status": "revoked" if reg.certificate_revoked_at else "issued",
        "issued_at": when(reg.certificate_issued_at),
        "revoked_at": when(reg.certificate_revoked_at),
        "verify_url": public_url_of(reg),
        "pdf_url": None,
        "livemode": True,
        "name": reg.full_name,
    }


async def load_certificate(session, reg_id) -> Optional[dict]:
    reg = await _registration(session, reg_id)
    return None if reg is None else certificate_object(reg)


# ─── Payments (webhooks' own) ────────────────────────────────────────────


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
    event_id = payload.get("event_id")
    reg = await _registration(session, reg_id) if reg_id else None
    if reg is not None:
        event_id = reg.event_id
    refunded = await _refunded(session, log)
    amount = paise(log.amount)
    status = getattr(log.status, "value", log.status)
    if refunded >= amount and amount > 0:
        status = "refunded"
    elif refunded > 0:
        status = "partially_refunded"
    return {
        "id": str(log.id),
        "object": "payment",
        "registration_id": _sid(reg_id),
        "event_id": _sid(event_id),
        "status": status,
        "amount": amount,
        "amount_refunded": refunded,
        "currency": (order.currency if order is not None and order.currency else CURRENCY),
        "livemode": True,
        "created_at": when(log.created_at),
        "updated_at": when(log.updated_at),
    }


# ─── The club ────────────────────────────────────────────────────────────


def club_object(club, socials=None) -> dict:
    from app.config import settings

    shapes = partner_shapes()
    if shapes is not None:
        return shapes.club_object(club, socials)
    return {
        "id": str(club.id),
        "object": "club",
        "name": club.name,
        "slug": club.slug,
        "status": "active" if club.is_verified else "pending_approval",
        "tagline": club.tagline,
        "about": club.about,
        "links": {
            "website": getattr(socials, "website", None), "instagram": getattr(socials, "instagram", None),
            "linkedin": getattr(socials, "linkedin", None), "youtube": getattr(socials, "youtube", None),
        },
        "logo_url": _picture(club.logo),
        "public_url": settings.PUBLIC_CLUB_URL.format(slug=club.slug) if club.is_verified and getattr(club, "listed", False) else None,
        "external_id": None,
        "livemode": True,
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
