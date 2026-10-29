"""Telling people about their registrations under review (docs/APPROVALS.md).

An event that reviews every registration: people register, and the club
confirms some and declines the rest. One job a message (`approvals.notify`),
written in the transaction that decided, so nothing goes for a decision that
rolled back. Each run reads the registration again and says only what is
still true: one cancelled before its "thanks for registering" mail went gets
none, a ticket already paid gets no pay link.

The emails are the club's own: the six request emails of its library
(stages REQUEST_*, docs/APPROVALS.md), edited in Emails like any other and
sent in its logo and colour, with its name as the sender, through the
designed-email renderer (app/core/mail). A club that never opened its
library gets MyOtherApp's defaults (app/core/mail/starters.py). The values
are filled in, never parsed: what the club or the person typed (a decline
note, a name) is text, escaped as every token is. A free ticket's
confirmation is the club's "Confirmed · ticket" email, sent through the
deliveries outbox; this sends its push. Only the cancellation note (the
event called off) is written here, not in the library.

Pushes, to an app account only (a guest has no device): `request_approved`
(opens the ticket), `request_approved_pay` (opens the claim, to pay) and
`request_declined` (an inbox item too), each with the request's `token` and
`event_id`, as `waitlist_offer` pushes carry theirs. WhatsApp: later.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.approvals import service
from app.api.approvals.models import APPROVED, CLOSED, DECLINED, LAPSED, PENDING, RegistrationRequests
from app.api.emails.models import EmailStage
from app.api.events.models import Events
from app.core import jobs, public_urls

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
#: What these count as against the club's messages (app/api/message_quota): a
#: message about the person's own ticket, as a waiting-list offer's.
MESSAGE_KIND = "ticket"

#: The library email each message is (its key: the stage in lower case).
STAGE_OF = {
    service.RECEIVED: EmailStage.REQUEST_RECEIVED,
    service.APPROVED_PAY: EmailStage.REQUEST_APPROVED_PAY,
    service.DECLINED_NOTE: EmailStage.REQUEST_DECLINED,
    service.LAPSED_NOTE: EmailStage.REQUEST_LAPSED,
    service.UNANSWERED: EmailStage.REQUEST_UNANSWERED,
}

QUIET = {"size": 14, "color": "#6B716C"}


def _when(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(IST).strftime("%d %b, %I:%M %p IST").lstrip("0").replace(" 0", " ")


def _safe(text: Optional[str]) -> str:
    """Words a person typed, put in a template's text: never a token of it."""
    return (text or "").replace("{", "(").replace("}", ")").strip()


def _rupees(amount) -> str:
    value = float(amount or 0)
    return f"₹{value:,.0f}" if value == int(value) else f"₹{value:,.2f}"


def _values(event, req, type_name: Optional[str], *, entry=None) -> dict:
    """Every token a request email may name, for this person: the usual ones
    and the request's own (app/core/mail/schema.py REQUEST_TOKENS). The pay
    ones only for a confirmed paid ticket still held (`entry`)."""
    from app.api.emails import service as emails_service

    club = getattr(event, "club", None)
    when = ""
    if event.event_datetime is not None:
        local = event.event_datetime.astimezone(IST)
        when = local.strftime("%a %d %b %Y · %I:%M %p").replace(" 0", " ")
    status_url = service.status_url(req.token)
    claim = entry is not None and entry.offer_token
    return {
        "first_name": (req.full_name or "").split(" ")[0] or "there",
        "attendee_name": req.full_name or "",
        "event_name": event.name or "",
        "org_name": club.name if club is not None else "The organisers",
        "organiser_email": emails_service.organiser_email(event) or "",
        "event_start": when,
        "venue": (event.location_name or "").strip() or ("Online" if event.is_online else ""),
        "ticket_type": type_name or "",
        "status_url": status_url,
        "cancel_url": f"{status_url}?withdraw=1",
        "event_url": public_urls.event_url(event.slug) if getattr(event, "slug", None) else public_urls.site_url("/events"),
        "pay_url": public_urls.site_url(f"/claim/{entry.offer_token}") if claim else status_url,
        "pay_amount": _rupees(req.amount) if entry is not None else "",
        "pay_by": _when(entry.offer_expires_at) if entry is not None else "",
        # The club's words, as typed: a value, never template text.
        "decline_note": (req.decline_note or "").strip(),
    }


def _cancelled(reason: Optional[str]):
    """(subject, design) of the one note the library doesn't hold: the event
    called off while the person was under review."""
    from app.core.mail.schema import EmailDesign
    from app.core.mail.starters import REQUEST_THEME, _heading, _paragraph

    blocks = [
        _heading("This event is cancelled."),
        _paragraph("{{org_name}} has cancelled **{{event_name}}**."),
    ]
    if reason:
        blocks.append(_paragraph(f"Why: {_safe(reason)}"))
    blocks.append(_paragraph("Your registration is closed. Nothing was charged.", **QUIET))
    return "Cancelled: {{event_name}}", EmailDesign.model_validate({"theme": REQUEST_THEME, "blocks": blocks})


async def _message(session, event, kind: str, *, reason: Optional[str] = None):
    """(subject, design): the club's library email for this moment, or the
    cancellation note."""
    from app.api.emails import service as emails_service

    if kind == service.CANCELLED:
        return _cancelled(reason)
    if kind not in STAGE_OF:
        raise ValueError(kind)
    fields = await emails_service.request_template(session, event.club_id, STAGE_OF[kind])
    return fields.subject, fields.design


async def _render(session, event, req, kind: str, values: dict, *, reason: Optional[str] = None):
    """The message as the club's designed email; a plain one if that fails."""
    from app.api.emails import service as emails_service
    from app.core.email import plain
    from app.core.mail.render import plain as plain_text
    from app.core.mail.schema import EmailAttachments

    subject, design = await _message(session, event, kind, reason=reason)
    try:
        return await emails_service.compose(
            session, event, None, subject, design, EmailAttachments(),
            brand=await emails_service.brand_primary(session, event), logo_url=emails_service.logo_url_of(event), values=values,
        )
    except Exception:  # noqa: BLE001 - the message still goes, plainly
        logger.warning("request %s: the designed %s email could not be drawn; sending it plain", req.id, kind, exc_info=True)
        from app.api.emails.service import Outgoing

        texts = [t for t in (plain_text(b.text, values) for b in design.blocks if b.kind in ("heading", "paragraph")) if t.strip()]
        button = next(((plain_text(b.label, values), plain_text(b.href, values)) for b in design.blocks if b.kind == "button"), None)
        return Outgoing(
            subject=plain_text(subject, values), text=plain.text(texts, button=button), html=plain.render(texts, button=button),
        )


async def _email(session, event, req, kind: str, key: str, values: dict, **kw) -> None:
    from app.core.email import email as email_mod

    out = await _render(session, event, req, kind, values, **kw)
    club = getattr(event, "club", None)
    await asyncio.to_thread(
        email_mod.send_email,
        recipients=[req.email], subject=out.subject, text=out.text, html=out.html, attachments=out.attachments,
        sender_name=club.name if club is not None else None, idempotency_key=key,
    )
    try:
        from app.api.message_quota import service as quota

        await quota.count(session, club_id=event.club_id, channel=quota.EMAIL, kind=MESSAGE_KIND, ref=f"{key}:email")
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()


async def _app_user(session, user_id: Optional[int]):
    from app.api.users.models import UserTypes, Users

    if user_id is None:
        return None
    user = await session.scalar(select(Users).where(Users.id == user_id, Users.is_deleted == False))  # noqa: E712
    if user is None or getattr(user.user_type, "value", user.user_type) == UserTypes.guest.value:
        return None
    return user


async def _push(session, event, req, kind: str, *, title: str, body: str, extra: Optional[dict] = None, inbox: bool = False) -> None:
    """A push to the person's app (an account, never a guest), and an inbox
    item when asked. Best effort: the email is what must go."""
    user = await _app_user(session, req.user_id)
    if user is None:
        return
    data = {"type": kind, "token": req.token, "event_id": str(event.id), "url": service.status_url(req.token), **(extra or {})}
    try:
        if inbox:
            from app.api.notifications import service as inbox_service

            line = await inbox_service.create_notification(
                session, user_id=user.id, title=title, description=body, type=kind,
                data={k: v for k, v in data.items() if k != "type"}, from_club_id=event.club_id, event_id=event.id,
            )
            # A tap on the push marks this line read.
            data["notification_id"] = str(line.id)
        from app.core.notifications.service import send_notification_to_user

        await send_notification_to_user(session, user.id, title, body, data=data)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.warning("request %s: the %s push could not be sent", req.id, kind, exc_info=True)


async def tell(session, request_id, kind: str, stamp: str) -> None:
    from app.api.ticketing.models import WaitlistEntries
    from app.api.ticketing import types as types_mod

    req = await session.scalar(
        select(RegistrationRequests).where(RegistrationRequests.id == request_id).execution_options(populate_existing=True)
    )
    if req is None:
        return
    event = await session.scalar(
        select(Events).options(joinedload(Events.club)).where(Events.id == req.event_id).execution_options(include_deleted=True)
    )
    if event is None:
        return
    key = f"approvals:{kind}:{req.id}:{stamp}"
    names = await types_mod.names(session, [req.ticket_type_id] if req.ticket_type_id else [])
    values = _values(event, req, names.get(req.ticket_type_id))
    org = values["org_name"]
    now = datetime.now(timezone.utc)

    if kind == service.RECEIVED:
        if req.status != PENDING:
            return
        await _email(session, event, req, kind, key, values)
    elif kind == service.APPROVED_FREE:
        if req.status != APPROVED or req.registration_id is None:
            return
        await _after_registered(session, req)
        reg_ticket = await _ticket_id(session, req.registration_id)
        await _push(
            session, event, req, "request_approved", title=f"{org} confirmed your registration",
            body=f"You're in for {event.name}. Tap to see your ticket.", extra={"ticket_id": reg_ticket or ""},
        )
    elif kind == service.APPROVED_PAY:
        if req.status != APPROVED or req.waitlist_entry_id is None:
            return
        entry = await session.get(WaitlistEntries, req.waitlist_entry_id)
        if entry is None or entry.status != "offered" or (entry.offer_expires_at and entry.offer_expires_at <= now):
            return
        await _email(session, event, req, kind, key, _values(event, req, names.get(req.ticket_type_id), entry=entry))
        await _push(
            session, event, req, "request_approved_pay", title=f"{org} confirmed your registration",
            body=f"Pay {_rupees(req.amount)} by {_when(entry.offer_expires_at)} to get your ticket for {event.name}.",
            extra={"claim_token": entry.offer_token, "claim_url": public_urls.site_url(f"/claim/{entry.offer_token}")},
        )
    elif kind == service.DECLINED_NOTE:
        if req.status != DECLINED:
            return
        await _email(session, event, req, kind, key, values)
        await _push(
            session, event, req, "request_declined", title=f"Your registration for {event.name}",
            body=f"{org} couldn't confirm your registration this time.", inbox=True,
        )
    elif kind == service.LAPSED_NOTE:
        if req.status != LAPSED:
            return
        await _email(session, event, req, kind, key, values)
    elif kind == service.UNANSWERED:
        # Still closed for that reason (never for one closed another way since).
        if req.status != CLOSED or req.closed_reason not in service.UNANSWERED_REASONS:
            return
        await _email(session, event, req, kind, key, values)
    elif kind == service.CANCELLED:
        if req.status != CLOSED:
            return
        from app.api.club_ops.cancel import cancellation_of

        c = await cancellation_of(session, event.id)
        await _email(session, event, req, kind, key, values, reason=c.reason if c is not None else None)


async def _ticket_id(session, registration_id) -> Optional[str]:
    from app.api.events.models import EventRegistrationsLink

    return await session.scalar(select(EventRegistrationsLink.ticket_id).where(EventRegistrationsLink.id == registration_id))


async def _after_registered(session, req) -> None:
    """What the public form does once someone is registered, done for a
    confirmed request: the club's WhatsApp updates they ticked, and badges.
    Best effort, each."""
    if req.club_updates_whatsapp:
        try:
            from app.api.club_whatsapp import consent

            await consent.from_registration(session, req.registration_id, True)
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.warning("request %s: the WhatsApp updates consent could not be kept", req.id, exc_info=True)
    if req.user_id is not None:
        try:
            from app.api.badges.service import check_and_award_user_badges

            await check_and_award_user_badges(session, user_id=req.user_id)
            await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.warning("request %s: badges could not be checked", req.id, exc_info=True)


@jobs.handler(service.NOTIFY_JOB, max_attempts=5, timeout=120.0, priority=jobs.HIGH)
async def _notify_job(p: dict) -> None:
    async with jobs.scope() as session:
        await tell(session, uuid.UUID(str(p["request_id"])), str(p["kind"]), str(p.get("stamp") or ""))
