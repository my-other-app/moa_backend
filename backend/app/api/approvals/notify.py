"""Telling people about their registration requests (docs/APPROVALS.md).

One job a message (`approvals.notify`), written in the transaction that
decided, so nothing goes for a decision that rolled back. Each run reads the
request again and says only what is still true: a request withdrawn before
its "received" mail went gets none, a hold already paid gets no pay link.

The emails are the club's, like its ticket email: its logo and colour, its
name as the sender, through the designed-email renderer (app/core/mail). They
are listed in the club's email library (`automatic`, emails.library_overview)
but not edited there yet. A free ticket's confirmation is the event's own
ticket email (the deliveries outbox); this sends its push.

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
from app.api.events.models import Events
from app.core import jobs, public_urls

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
#: What these count as against the club's messages (app/api/message_quota): a
#: message about a place the person asked for, as a waiting-list offer's.
MESSAGE_KIND = "ticket"

#: The club's email library lists these (emails.library_overview): what each
#: is, and when it goes.
LIBRARY = [
    {
        "key": "request_received", "name": "Request received",
        "subject": "You're on the waiting list for {{event_name}}",
        "when": "As soon as someone fills the form of an event that confirms each registration.",
    },
    {
        "key": "request_approved", "name": "Confirmed · free ticket",
        "subject": "Your ticket for {{event_name}}",
        "when": "When you confirm a free ticket: the event's own ticket email, with the ticket attached.",
    },
    {
        "key": "request_approved_pay", "name": "Confirmed · pay to get your ticket",
        "subject": "{{org_name}} confirmed your place at {{event_name}}",
        "when": "When you confirm a paid ticket: the pay link, and how long the place is held.",
    },
    {
        "key": "request_declined", "name": "Declined",
        "subject": "Your request for {{event_name}}",
        "when": "When you decline someone and leave “Tell them” on, with your note if you wrote one.",
    },
    {
        "key": "request_lapsed", "name": "Place released",
        "subject": "Your place at {{event_name}} was released",
        "when": "When a confirmed paid ticket isn't paid in time and its place goes back.",
    },
    {
        "key": "request_unanswered", "name": "Not confirmed in time",
        "subject": "Your request for {{event_name}}",
        "when": "When the event ends, or is deleted, while someone's request is still waiting for you: once to each.",
    },
]

FOOTER = (
    "{{org_name}}{% if organiser_email %} · {{organiser_email}}{% endif %}\n"
    "You are receiving this because you asked for a place at {{event_name}}."
)
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


def _values(event, req, type_name: Optional[str]) -> dict:
    from app.api.emails import service as emails_service

    club = getattr(event, "club", None)
    when = ""
    if event.event_datetime is not None:
        local = event.event_datetime.astimezone(IST)
        when = local.strftime("%a %d %b %Y · %I:%M %p").replace(" 0", " ")
    return {
        "first_name": (req.full_name or "").split(" ")[0] or "there",
        "attendee_name": req.full_name or "",
        "event_name": event.name or "",
        "org_name": club.name if club is not None else "The organisers",
        "organiser_email": emails_service.organiser_email(event) or "",
        "event_start": when,
        "venue": (event.location_name or "").strip() or ("Online" if event.is_online else ""),
        "ticket_type": type_name or "",
    }


def _design(kind: str, req, event, *, entry=None, note: Optional[str] = None, reason: Optional[str] = None):
    """(subject, blocks) for one message."""
    from app.core.mail.starters import _button, _details, _heading, _paragraph

    status_url = service.status_url(req.token)
    if kind == service.RECEIVED:
        return "You're on the waiting list for {{event_name}}", [
            _heading("You're on the waiting list, {{first_name}}."),
            _paragraph("Your registration for **{{event_name}}** was received, and you're on the waiting list."),
            _paragraph("{{org_name}} confirms each place and will email your ticket if they confirm yours. Nothing else to do: we'll let you know."),
            _details([{"label": "When", "value": "{{event_start}}"}, {"label": "Where", "value": "{{venue}}"}]),
            _button("See your request", status_url),
            _paragraph(f"Changed your mind? [Withdraw your request]({status_url}?withdraw=1).", **QUIET),
        ]
    if kind == service.APPROVED_PAY:
        amount = _rupees(req.amount)
        pay_by = _when(entry.offer_expires_at if entry is not None else None)
        claim = public_urls.site_url(f"/claim/{entry.offer_token}") if entry is not None and entry.offer_token else status_url
        return "{{org_name}} confirmed your place at {{event_name}}", [
            _heading("Your place is confirmed, {{first_name}}."),
            _paragraph(f"{{{{org_name}}}} confirmed your place at **{{{{event_name}}}}**. Pay {amount} by {pay_by} to get your ticket."),
            _details([
                {"label": "Ticket", "value": "{{ticket_type}}"}, {"label": "To pay", "value": amount},
                {"label": "Pay by", "value": pay_by}, {"label": "When", "value": "{{event_start}}"},
            ]),
            _button(f"Pay {amount}", claim),
            _paragraph("The place is held for you until then. If it isn't paid by then, it goes to someone else.", **QUIET),
        ]
    if kind == service.DECLINED_NOTE:
        blocks = [
            _heading("Sorry, {{first_name}}."),
            _paragraph("Sorry, {{org_name}} couldn't offer you a place at **{{event_name}}** this time."),
        ]
        if note:
            blocks.append(_paragraph(f"A note from {{{{org_name}}}}: “{_safe(note)}”"))
        blocks.append(_paragraph("Thank you for your interest.", **QUIET))
        return "Your request for {{event_name}}", blocks
    if kind == service.LAPSED_NOTE:
        return "Your place at {{event_name}} was released", [
            _heading("Your place was released."),
            _paragraph("Your place at **{{event_name}}** was released because payment didn't arrive in time."),
            _paragraph(f"If you'd still like to go, [ask again from the event's page]({public_urls.event_url(event.slug)}).", **QUIET),
        ]
    if kind == service.UNANSWERED:
        return "Your request for {{event_name}}", [
            _heading("Sorry, {{first_name}}."),
            _paragraph("{{org_name}} wasn't able to confirm your place for **{{event_name}}**."),
            _paragraph("Your request is closed. Nothing was charged. Thank you for your interest.", **QUIET),
        ]
    if kind == service.CANCELLED:
        blocks = [
            _heading("This event is cancelled."),
            _paragraph("{{org_name}} has cancelled **{{event_name}}**."),
        ]
        if reason:
            blocks.append(_paragraph(f"Why: {_safe(reason)}"))
        blocks.append(_paragraph("Your request is closed. Nothing was charged.", **QUIET))
        return "Cancelled: {{event_name}}", blocks
    raise ValueError(kind)


async def _render(session, event, req, kind: str, values: dict, **kw):
    """The message as the club's designed email; a plain one if that fails."""
    from app.api.emails import service as emails_service
    from app.core.email import plain
    from app.core.mail.render import plain as plain_text
    from app.core.mail.schema import EmailAttachments, EmailDesign
    from app.core.mail.starters import THEME

    subject, blocks = _design(kind, req, event, **kw)
    try:
        design = EmailDesign.model_validate({"theme": {**THEME, "footer": FOOTER}, "blocks": blocks})
        return await emails_service.compose(
            session, event, None, subject, design, EmailAttachments(),
            brand=await emails_service.brand_primary(session, event), logo_url=emails_service.logo_url_of(event), values=values,
        )
    except Exception:  # noqa: BLE001 - the message still goes, plainly
        logger.warning("request %s: the designed %s email could not be drawn; sending it plain", req.id, kind, exc_info=True)
        from app.api.emails.service import Outgoing

        texts = [plain_text(b["text"], values) for b in blocks if b.get("kind") in ("heading", "paragraph")]
        button = next(((b["label"], b["href"]) for b in blocks if b.get("kind") == "button"), None)
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
            session, event, req, "request_approved", title=f"{org} confirmed your place",
            body=f"You're in for {event.name}. Tap to see your ticket.", extra={"ticket_id": reg_ticket or ""},
        )
    elif kind == service.APPROVED_PAY:
        if req.status != APPROVED or req.waitlist_entry_id is None:
            return
        entry = await session.get(WaitlistEntries, req.waitlist_entry_id)
        if entry is None or entry.status != "offered" or (entry.offer_expires_at and entry.offer_expires_at <= now):
            return
        await _email(session, event, req, kind, key, values, entry=entry)
        await _push(
            session, event, req, "request_approved_pay", title=f"{org} confirmed your place",
            body=f"Pay {_rupees(req.amount)} by {_when(entry.offer_expires_at)} to get your ticket for {event.name}.",
            extra={"claim_token": entry.offer_token, "claim_url": public_urls.site_url(f"/claim/{entry.offer_token}")},
        )
    elif kind == service.DECLINED_NOTE:
        if req.status != DECLINED:
            return
        await _email(session, event, req, kind, key, values, note=req.decline_note)
        await _push(
            session, event, req, "request_declined", title=f"Your request for {event.name}",
            body=f"Sorry, {org} couldn't offer you a place this time.", inbox=True,
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
