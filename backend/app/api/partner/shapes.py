"""What the Integrations API returns for the real (live) objects: one
function per resource, the same shape every GET, POST and PATCH answers --
and what a webhook's `data.object` carries. Plain functions of a row:
`event_object`, `registration_object` (with a `RegistrationContext`),
`request_object` (a registration under review), `ticket_object`,
`payment_object` (a registration's money), `payment_log_object` (an online
payment: payment.completed / payment.refunded), `checkin_object`,
`certificate_object`, `club_object`; and, with a session,
`event_payload` and `registration_payload`.

Contact data -- a person's name, email, phone and form answers -- is only in
the object when the caller holds attendees:read (`contact=True`).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import select

from app.api.partner.common import CURRENCY, TIMEZONE, iso, money, now, paise_of
from app.config import settings

QR_TTL_SECONDS = 7 * 24 * 3600


def _status_of_event(event) -> str:
    """draft | published | cancelled | ended, from the status clock."""
    life = getattr(getattr(event, "lifecycle", None), "value", getattr(event, "lifecycle", None)) or "published"
    if life == "draft":
        return "draft"
    if life == "cancelled":
        return "cancelled"
    if life in ("closed", "archived"):
        return "ended"
    if event.event_datetime + timedelta(hours=float(event.duration or 0)) < now():
        return "ended"
    return "published"


def picture_url(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, dict):
        return value.get("original") or next(iter(value.values()), None)
    return None


def ticket_type_object(t) -> dict:
    return {
        "id": str(t.id), "name": t.name, "price": money(paise_of(t.price)), "capacity": t.capacity,
        "sales_start": iso(t.sales_start), "sales_end": iso(t.sales_end), "hidden": bool(t.is_hidden),
    }


def event_object(event, *, ticket_types: list = (), external_id: Optional[str] = None) -> dict:
    from app.core import public_urls

    status = _status_of_event(event)
    ends = event.event_datetime + timedelta(hours=float(event.duration or 0))
    venue = None
    if event.location_name or event.lat is not None:
        venue = {"name": event.location_name, "address": None, "city": None, "lat": event.lat, "lng": event.lng, "map_url": event.location_link}
    return {
        "id": str(event.id),
        "object": "event",
        "club_id": str(event.club_id) if event.club_id is not None else None,
        "name": event.name,
        "slug": event.slug,
        "description": event.about,
        "status": status,
        "starts_at": iso(event.event_datetime),
        "ends_at": iso(ends),
        "timezone": TIMEZONE,
        "venue": venue,
        "online": {"url": event.url} if event.is_online and event.url else None,
        "poster_url": picture_url(event.poster),
        "capacity": event.max_participants,
        "registration": {
            "opens_at": iso(event.reg_startdate), "closes_at": iso(event.reg_enddate),
            "requires_approval": bool(event.requires_approval),
        },
        "price": money(paise_of(event.reg_fee)) if event.has_fee else None,
        "ticket_types": [ticket_type_object(t) for t in ticket_types],
        "external_payments_allowed": bool(getattr(event, "external_payments_allowed", False)),
        "registrations_count": int(event.going_count or 0),
        "external_id": external_id,
        "public_url": public_urls.event_url(event.slug) if status in ("published", "ended") else None,
        "livemode": True,
        "created_at": iso(event.created_at),
        "updated_at": iso(event.updated_at),
    }


def qr_url(code: str) -> str:
    from app.core.auth.payloads import sign_payload

    signed = sign_payload({"t": code, "k": "partner_qr"}, QR_TTL_SECONDS)
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/partner/v1/tickets/{code}/qr.png?sig={signed}"


class RegistrationContext:
    """What a page of registrations needs besides their rows, read in a few queries."""

    def __init__(self):
        self.checkins: dict = {}
        self.payments: dict = {}
        self.requests: dict = {}
        self.type_names: dict = {}
        self.cancellations: dict = {}

    @classmethod
    async def load(cls, session, regs: list) -> "RegistrationContext":
        from app.api.approvals.models import RegistrationRequests
        from app.api.checkins.models import CheckIns
        from app.api.partner.models import PartnerExternalPayments
        from app.api.ticketing import types as types_mod

        ctx = cls()
        ids = [r.id for r in regs]
        if not ids:
            return ctx
        ctx.checkins = {r.registration_id: r for r in await session.scalars(select(CheckIns).where(CheckIns.registration_id.in_(ids)))}
        ctx.payments = {p.registration_id: p for p in await session.scalars(select(PartnerExternalPayments).where(PartnerExternalPayments.registration_id.in_(ids)))}
        ctx.requests = {
            q.registration_id: q for q in await session.scalars(select(RegistrationRequests).where(RegistrationRequests.registration_id.in_(ids)))
        }
        ctx.type_names = await types_mod.names(session, list({r.ticket_type_id for r in regs if r.ticket_type_id is not None}))
        return ctx


def payment_object(reg, event, ext=None) -> dict:
    """A registration's money, as its `payment` reads: `ext` is its
    PartnerExternalPayments row when it was paid outside MyOtherApp."""
    if ext is not None:
        return {"status": "paid_externally", "amount": int(ext.amount_paise), "currency": ext.currency or CURRENCY, "reference": ext.reference}
    if not event.has_fee:
        return {"status": "not_required", "amount": 0, "currency": CURRENCY}
    if reg.is_paid:
        paid = paise_of(reg.paid_amount)
        if reg.payment_receipt:
            return {"status": "paid_online", "amount": paid, "currency": CURRENCY}
        if paid > 0:
            return {"status": "paid_at_door", "amount": paid, "currency": CURRENCY}
        return {"status": "not_required", "amount": 0, "currency": CURRENCY}
    return {"status": "awaiting_payment", "amount": paise_of(reg.actual_amount), "currency": CURRENCY}


def _owes(reg, event, ext) -> bool:
    return bool(event.has_fee) and not reg.is_paid and ext is None


def ticket_object(reg, event, ext=None) -> dict:
    """A registration's ticket: its code (what the door scans), a signed QR
    link while it is valid, and whether it is valid, awaiting payment,
    revoked or cancelled."""
    if getattr(reg, "is_deleted", False):
        status = "cancelled"
    elif reg.ticket_revoked_at is not None:
        status = "revoked"
    elif _owes(reg, event, ext):
        status = "awaiting_payment"
    else:
        status = "valid"
    return {"code": reg.ticket_id, "qr_url": qr_url(reg.ticket_id) if status == "valid" else None, "status": status}


def payment_log_object(log, reg=None, *, event_id=None) -> dict:
    """An online payment (payment.completed, payment.refunded): amounts and
    status only, never the payer's details or the provider's ids."""
    eid = event_id if event_id is not None else getattr(reg, "event_id", None)
    return {
        "id": str(log.id),
        "object": "payment",
        "registration_id": str(reg.id) if reg is not None else None,
        "event_id": str(eid) if eid is not None else None,
        "status": getattr(log.status, "value", log.status),
        "amount": paise_of(log.amount),
        "currency": CURRENCY,
        "created_at": iso(log.created_at),
        "updated_at": iso(log.updated_at),
        "livemode": True,
    }


def registration_object(reg, event, ctx: RegistrationContext, *, contact: bool, external_id: Optional[str] = None) -> dict:
    deleted = bool(getattr(reg, "is_deleted", False))
    ext = ctx.payments.get(reg.id)
    owes = _owes(reg, event, ext)
    status = "cancelled" if deleted else ("awaiting_payment" if owes else "confirmed")
    checkin = ctx.checkins.get(reg.id)
    checked_in_at = checkin.checked_in_at if checkin is not None else (reg.attended_on if reg.is_attended else None)
    req = ctx.requests.get(reg.id)
    approval = None
    if req is not None:
        approval = {"status": "approved", "decided_at": iso(req.decided_at), "request_id": str(req.id)}
    elif event.requires_approval:
        approval = {"status": "added_directly", "decided_at": None, "request_id": None}
    out = {
        "id": str(reg.id),
        "object": "registration",
        "event_id": str(reg.event_id),
        "status": status,
        "ticket": ticket_object(reg, event, ext),
        "ticket_type": {"id": str(reg.ticket_type_id), "name": ctx.type_names.get(reg.ticket_type_id)} if reg.ticket_type_id is not None else None,
        "approval": approval,
        "payment": payment_object(reg, event, ext),
        "checked_in_at": iso(checked_in_at),
        "request_id": str(req.id) if req is not None else None,
        "external_id": external_id,
        "livemode": True,
        "created_at": iso(reg.created_at),
        "updated_at": iso(reg.updated_at),
    }
    if contact:
        out["person"] = {"name": reg.full_name, "email": reg.email, "phone": reg.phone}
        out["answers"] = reg.additional_details if isinstance(reg.additional_details, dict) else {}
    return out


_REQUEST_STATUS = {"pending": "pending_approval", "approved": "pending_approval", "declined": "declined",
                   "withdrawn": "withdrawn", "lapsed": "cancelled", "closed": "cancelled"}


def request_object(req, event, *, contact: bool, external_id: Optional[str] = None) -> dict:
    """A registration still under review (or turned down): a request, not yet a registration."""
    status = _REQUEST_STATUS.get(req.status, "pending_approval")
    approval_status = {"pending": "pending", "approved": "approved", "declined": "declined", "withdrawn": "withdrawn"}.get(req.status, req.status)
    out = {
        "id": str(req.id),
        "object": "registration",
        "event_id": str(req.event_id),
        "status": status,
        "ticket": None,
        "ticket_type": {"id": str(req.ticket_type_id), "name": None} if req.ticket_type_id is not None else None,
        "approval": {"status": approval_status, "decided_at": iso(req.decided_at), "request_id": str(req.id)},
        "payment": (
            {"status": "awaiting_payment", "amount": paise_of(req.amount), "currency": CURRENCY}
            if event.has_fee and req.amount else {"status": "not_required", "amount": 0, "currency": CURRENCY}
        ),
        "checked_in_at": None,
        "request_id": str(req.id),
        "external_id": external_id,
        "livemode": True,
        "created_at": iso(req.submitted_at or req.created_at),
        "updated_at": iso(req.updated_at),
    }
    if contact:
        out["person"] = {"name": req.full_name, "email": req.email, "phone": req.phone}
        out["answers"] = req.answers if isinstance(req.answers, dict) else {}
    return out


def checkin_object(row, *, duplicate: bool = False) -> dict:
    return {
        "id": str(row.id),
        "object": "checkin",
        "registration_id": str(row.registration_id),
        "event_id": str(row.event_id),
        "checked_in_at": iso(row.checked_in_at),
        "scanned_at": iso(row.scanned_at),
        "gate": row.door_label,
        "via": getattr(row.via, "value", row.via),
        "duplicate": bool(duplicate),
        "livemode": True,
        "updated_at": iso(row.updated_at),
    }


def certificate_urls(reg) -> tuple[Optional[str], Optional[str]]:
    from app.api.events.certificates import public_url_of

    pdf = None
    if reg.certificate_public_id:
        pdf = f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/verify/certificates/{reg.certificate_public_id}/pdf"
    return public_url_of(reg), pdf


def certificate_object(reg, *, contact: bool) -> dict:
    from app.api.events.certificates import number_of

    verify, pdf = certificate_urls(reg)
    out = {
        "number": number_of(reg),
        "object": "certificate",
        "registration_id": str(reg.id),
        "event_id": str(reg.event_id),
        "status": "revoked" if reg.certificate_revoked_at else "issued",
        "issued_at": iso(reg.certificate_issued_at),
        "revoked_at": iso(reg.certificate_revoked_at),
        "verify_url": verify,
        "pdf_url": pdf if not reg.certificate_revoked_at else None,
        "livemode": True,
    }
    if contact:
        out["name"] = reg.full_name
    return out


def club_object(club, socials=None, *, external_id: Optional[str] = None) -> dict:
    links = {
        "website": getattr(socials, "website", None), "instagram": getattr(socials, "instagram", None),
        "linkedin": getattr(socials, "linkedin", None), "youtube": getattr(socials, "youtube", None),
    }
    return {
        "id": str(club.id),
        "object": "club",
        "name": club.name,
        "slug": club.slug,
        "status": "active" if club.is_verified else "pending_approval",
        "tagline": club.tagline,
        "about": club.about,
        "links": links,
        "logo_url": picture_url(club.logo),
        "public_url": settings.PUBLIC_CLUB_URL.format(slug=club.slug) if club.is_verified and getattr(club, "listed", False) else None,
        "external_id": external_id,
        "livemode": True,
        "updated_at": iso(club.updated_at),
    }


# ─── For a caller with a session and a row (webhooks_out's emitters) ────


async def event_payload(session, event) -> dict:
    """event_object with its ticket types read (no external_id: those are each client's own)."""
    from app.api.ticketing import types as types_mod

    return event_object(event, ticket_types=await types_mod.live(session, event.id))


async def registration_payload(session, reg, event=None, *, contact: bool = True) -> dict:
    """registration_object with its check-in, external payment, request and
    type read; contact data in unless `contact=False` (strip it per endpoint)."""
    from app.api.events.models import Events

    event = event or getattr(reg, "event", None) or await session.get(Events, reg.event_id)
    ctx = await RegistrationContext.load(session, [reg])
    return registration_object(reg, event, ctx, contact=contact)
