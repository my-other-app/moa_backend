"""“Can't come? Give your ticket back”: the link in a guest's ticket emails
(feature-register/ticket-self-cancel-contract.md).

The link (`cancel_ticket_url`, https://myotherapp.in/tickets/cancel?t=…)
carries the registration's id and an HMAC of it under the link-signing key
(app/core/mail/links.py, which rotates), for this purpose only
(`ticket-cancel:`): no login, no expiry before the event starts, and in
effect single use (a second press finds the ticket given back already).

Only a free ticket, before the event starts, not checked in, on an event
whose club leaves "Let guests give their ticket back" on
(events.allow_self_cancel). Anything paid -- the ticket, a payment on it, a
store order -- is refused as `paid` and the page names the organiser's
email: no money ever moves here (Razorpay is live; refunds are the club's).

Giving it back takes the guest off the list exactly as the club's "Take
someone off the list" does (registration.service.take_off_list): the
registration is soft deleted, its place is free and the waiting list is
offered it. The club's bell says so, the guest gets a short email, and the
club sees it in the event's list of tickets given back.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.api.closeout.models import EventStatus
from app.api.events.models import Events, EventRegistrationsLink
from app.api.ticket_cancel.models import TicketCancellations
from app.core import jobs, public_urls
from app.core.mail import links
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PURPOSE = "ticket-cancel"
EMAIL_JOB = "tickets.cancelled_email"
REASON_MAX = 300

PAID, STARTED, CHECKED_IN, ALREADY_CANCELLED, NOT_ALLOWED, INVALID = (
    "paid", "started", "checked_in", "already_cancelled", "not_allowed", "invalid",
)
REASONS = (PAID, STARTED, CHECKED_IN, ALREADY_CANCELLED, NOT_ALLOWED, INVALID)
#: The 409's message for each refusal (the website words its own).
MESSAGES = {
    PAID: "This ticket was paid for, so it can't be cancelled here. Write to the organiser for a refund.",
    STARTED: "The event has started, so this ticket can't be given back now.",
    CHECKED_IN: "This ticket was used at the door already.",
    ALREADY_CANCELLED: "This ticket was given back already.",
    NOT_ALLOWED: "The organiser doesn't take tickets back here. Write to them if you can't come.",
    INVALID: "This link is not valid.",
}
#: An event in one of these no longer takes changes from its guests.
_SETTLED = (EventStatus.cancelled, EventStatus.closed, EventStatus.archived)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def token_for(registration_id) -> str:
    rid = uuid.UUID(str(registration_id)).hex
    return f"{rid}.{links.sign(f'{PURPOSE}:{rid}')}"


def url_for(registration_id) -> str:
    return public_urls.site_url(f"/tickets/cancel?t={token_for(registration_id)}")


def registration_id_of(token: Optional[str]) -> Optional[uuid.UUID]:
    """The registration a well-signed token names; None for anything else."""
    rid, _, sig = (token or "").partition(".")
    if len(rid) != 32 or not sig:
        return None
    try:
        parsed = uuid.UUID(hex=rid)
    except ValueError:
        return None
    return parsed if links.verify(f"{PURPOSE}:{parsed.hex}", sig) else None


def mask_code(code: Optional[str]) -> Optional[str]:
    """The ticket code with its middle hidden: enough to tell two tickets apart, not to use one."""
    if not code:
        return None
    if len(code) <= 4:
        return "•" * len(code)
    if len(code) <= 8:
        return f"{code[:1]}{'•' * (len(code) - 3)}{code[-2:]}"
    return f"{code[:4]}{'•' * (len(code) - 8)}{code[-4:]}"


async def _load(session, token: str, *, lock: bool = False):
    rid = registration_id_of(token)
    if rid is None:
        return None, None
    q = (
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == rid)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .execution_options(include_deleted=True, populate_existing=True)
    )
    if lock:
        q = q.with_for_update(of=EventRegistrationsLink)
    reg = await session.scalar(q)
    if reg is None or reg.event is None or reg.event.is_deleted:
        return None, None
    return reg, reg.event


async def _paid(session, reg: EventRegistrationsLink) -> bool:
    """Any money on it: the ticket, a payment made or started for it, or a store order."""
    from app.api.store.models import EventStoreOrders

    if reg.is_paid or (reg.paid_amount or 0) > 0 or (reg.actual_amount or 0) > 0 or reg.payment_receipt:
        return True
    orders = await session.scalar(
        select(func.count()).select_from(EventStoreOrders)
        .where(EventStoreOrders.registration_id == reg.id, EventStoreOrders.status.in_(("pending", "paid")))
    )
    return bool(orders)


async def _checked_in(session, reg: EventRegistrationsLink) -> bool:
    from app.api.checkins.models import CheckIns

    if reg.is_attended:
        return True
    return bool(await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.registration_id == reg.id)))


async def refusal(session, reg: Optional[EventRegistrationsLink], event: Optional[Events], now: Optional[datetime] = None) -> Optional[str]:
    """Why this ticket can't be given back now, or None when it can."""
    now = now or _now()
    if reg is None or event is None:
        return INVALID
    if reg.is_deleted or reg.ticket_revoked_at is not None:
        return ALREADY_CANCELLED
    if await _checked_in(session, reg):
        return CHECKED_IN
    if event.event_datetime is not None and now >= event.event_datetime:
        return STARTED
    if await _paid(session, reg):
        return PAID
    if not getattr(event, "allow_self_cancel", True) or (event.lifecycle or EventStatus.published) in _SETTLED:
        return NOT_ALLOWED
    return None


def _organiser_email(event: Optional[Events]) -> Optional[str]:
    if event is None:
        return None
    from app.api.emails import service as emails_service

    return emails_service.organiser_email(event) or None


def _poster(event: Events) -> Optional[str]:
    p = event.poster if isinstance(event.poster, dict) else None
    return (p or {}).get("medium") or (p or {}).get("original")


async def preview(session, token: str) -> dict:
    reg, event = await _load(session, token)
    reason = await refusal(session, reg, event)
    return {
        "event": {
            "id": event.id, "name": event.name, "slug": event.slug,
            "starts_at": event.event_datetime.isoformat() if event.event_datetime else None, "poster": _poster(event),
        } if event is not None else None,
        "attendee_name": reg.full_name if reg is not None else None,
        "ticket_code_masked": mask_code(reg.ticket_id) if reg is not None else None,
        "cancellable": reason is None,
        "reason": reason,
        "organiser_email": _organiser_email(event),
    }


async def cancel(session, token: str, reason_text: Optional[str]) -> dict:
    """Give the ticket back, or 409 with why not (the reason code as error_code)."""
    from app.api.club_alerts import service as club_alerts
    from app.api.events.registration import service as registrations

    reg, event = await _load(session, token, lock=True)
    reason = await refusal(session, reg, event)
    if reason is not None:
        raise CustomHTTPException(
            409, message=MESSAGES[reason], error_code=reason,
            errors={"reason": reason, "organiser_email": _organiser_email(event)} if reason == PAID else {"reason": reason},
        )
    note = (reason_text or "").strip()[:REASON_MAX] or None
    session.add(TicketCancellations(
        registration_id=reg.id, event_id=event.id, user_id=reg.user_id, full_name=reg.full_name, email=reg.email,
        ticket_id=reg.ticket_id, reason=note, cancelled_at=_now(),
    ))
    await club_alerts.notify(
        session, event.club_id, "ticket_cancelled", title=f"{reg.full_name} gave their ticket back"[:200],
        body=f"{event.name}" + (f" — “{note}”" if note else ""), link=f"/events/{event.id}/registrations",
        data={"event_id": event.id, "registration_id": str(reg.id)}, dedupe_key=f"ticket_cancelled:{reg.id}",
    )
    await jobs.enqueue(session, EMAIL_JOB, {
        "to": reg.email, "name": reg.full_name, "event_name": event.name,
        "org": event.club.name if event.club is not None else "The organisers",
        "organiser_email": _organiser_email(event), "idempotency_key": f"ticket-cancelled-{reg.id}",
    }, dedupe_key=f"tickets:cancelled:{reg.id}")
    # Off the list exactly as the club's "Take someone off the list" takes them (commits).
    await registrations.take_off_list(session, reg)
    logger.info("registration %s given back by its guest (event %s)", reg.id, event.id)
    return {"cancelled": True}


async def given_back(session, event_id: int, *, limit: int = 500) -> list[dict]:
    """The event's tickets given back by their guests, newest first."""
    rows = (await session.scalars(
        select(TicketCancellations).where(TicketCancellations.event_id == event_id)
        .order_by(TicketCancellations.cancelled_at.desc()).limit(limit)
    )).all()
    return [
        {"registration_id": str(r.registration_id), "full_name": r.full_name, "email": r.email, "ticket_id": r.ticket_id,
         "reason": r.reason, "cancelled_at": r.cancelled_at.isoformat() if r.cancelled_at else None, "cancelled_by": "attendee"}
        for r in rows
    ]


# ─── The guest's email ───────────────────────────────────────────────────


@jobs.handler(EMAIL_JOB, max_attempts=4, timeout=60.0, priority=jobs.HIGH)
async def _email_job(p: dict) -> None:
    await asyncio.to_thread(send_cancelled_email, p)


def send_cancelled_email(p: dict) -> None:
    from app.core.email import email as email_mod

    name = (p.get("name") or "").split(" ")[0] or "there"
    event, org = p.get("event_name") or "the event", p.get("org") or "The organisers"
    contact = p.get("organiser_email")
    write = f" If that wasn't you, write to {contact}." if contact else ""
    text = (
        f"Hi {name},\n\nYour ticket for {event} is cancelled, as you asked. Your place goes to someone else, "
        f"and {org} has been told.{write}\n\nThanks for letting them know.\n"
    )
    safe = html_mod.escape
    html = (
        f"<p>Hi {safe(name)},</p><p>Your ticket for <strong>{safe(event)}</strong> is cancelled, as you asked. "
        f"Your place goes to someone else, and {safe(org)} has been told.{safe(write)}</p><p>Thanks for letting them know.</p>"
    )
    email_mod.send_email(recipients=[p["to"]], subject=f"Your ticket for {event} is cancelled", text=text, html=html,
                         idempotency_key=p.get("idempotency_key"))
