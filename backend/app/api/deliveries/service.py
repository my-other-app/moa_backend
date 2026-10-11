"""Sending documents to guests, and remembering what happened.

Every channel honours one contract: never re-send a channel already SENT
unless asked to, record a result for every attempt, and treat "no address
on this channel" as SKIPPED -- never as an error.
"""
import asyncio
import html
import io
import logging
import re
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, literal, select, union_all
from sqlalchemy.orm import joinedload

from app.api.deliveries.models import (
    Deliveries,
    DeliveryChannel,
    DeliveryKind,
    DeliveryOutcome,
    DeliveryStatus,
)
from app.api.events.access import ensure_event_access, is_admin_user
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import ticket_document
from app.core.email import email as email_mod
from app.db.core import AsyncSessionLocal
from app.db.listeners import add_loader_criteria
from app.core import providers as providers_mod
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

# The channels a send goes out on. WhatsApp has no provider wired in, so it
# is not offered: a row marked sent for a message that never left would hide
# every real email failure behind a success that never happened.
CHANNELS = [DeliveryChannel.email]

PROBLEM_OUTCOMES = (DeliveryOutcome.bounced, DeliveryOutcome.complained, DeliveryOutcome.undelivered)


@asynccontextmanager
async def session_scope():
    """A session for work that outlives the request. Tests swap this for one
    on their own transaction."""
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


async def event_for_club(session, user, event_id: int) -> Events:
    """The event, once the caller is allowed to manage it."""
    event = await session.scalar(
        select(Events)
        .options(joinedload(Events.club))
        .where(Events.id == event_id, Events.is_deleted == False)
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(
        event, user.id, as_admin=is_admin_user(user), message="Not authorized to manage this event"
    )
    return event


def _active(event_id: int):
    return select(EventRegistrationsLink).where(
        EventRegistrationsLink.event_id == event_id,
        EventRegistrationsLink.is_deleted == False,
    )


async def _registration(session, event_id: int, registration_id) -> EventRegistrationsLink:
    try:
        rid = registration_id if isinstance(registration_id, uuid.UUID) else uuid.UUID(str(registration_id))
    except ValueError:
        raise CustomHTTPException(404, message="Registration not found")
    reg = await session.scalar(_active(event_id).where(EventRegistrationsLink.id == rid))
    if reg is None:
        raise CustomHTTPException(404, message="Registration not found")
    return reg


# --- queueing ---------------------------------------------------------------


async def queue(
    session,
    event: Events,
    kind: DeliveryKind,
    *,
    registration_ids: Optional[list] = None,
    only: str = "unsent",
    force: bool = False,
) -> dict:
    """Put a pending row in place for every guest and channel that should be
    sent, and answer with what was queued and why the rest was not. Nothing is
    sent here; `run` does that after the response."""
    q = _active(event.id)
    if registration_ids:
        q = q.where(EventRegistrationsLink.id.in_(list(registration_ids)))
    regs = list(await session.scalars(q.order_by(EventRegistrationsLink.created_at)))

    existing = {
        (d.registration_id, d.channel): d
        for d in await session.scalars(
            select(Deliveries).where(Deliveries.event_id == event.id, Deliveries.kind == kind)
        )
    }
    ids: list[uuid.UUID] = []
    already_sent = 0
    revoked = 0
    for reg in regs:
        if kind is DeliveryKind.ticket and reg.ticket_revoked_at is not None:
            revoked += 1
            continue
        if kind is DeliveryKind.certificate and (reg.certificate_issued_at is None or reg.certificate_revoked_at is not None):
            # Not issued one, or had it taken back: nothing to send.
            revoked += 1
            continue
        for channel in CHANNELS:
            prior = existing.get((reg.id, channel))
            if prior is not None and prior.status is DeliveryStatus.sent and not force:
                already_sent += 1
                continue
            if only == "failed" and (
                prior is None or prior.status not in (DeliveryStatus.failed, DeliveryStatus.skipped)
            ):
                continue
            if prior is None:
                prior = Deliveries(
                    id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, kind=kind, channel=channel
                )
                session.add(prior)
                existing[(reg.id, channel)] = prior
            prior.status = DeliveryStatus.pending
            prior.error = None
            prior.outcome = None
            prior.outcome_at = None
            prior.reason = None
            prior.reason_code = None
            ids.append(prior.id)
    await session.commit()
    return {
        "queued": len(ids),
        "already_sent": already_sent,
        "revoked": revoked,
        "channels": [c.value for c in CHANNELS],
        "ids": ids,
    }


# --- sending ----------------------------------------------------------------


def _when(event: Events) -> str:
    dt = event.event_datetime.astimezone(ZoneInfo(EVENT_TIMEZONE))
    return dt.strftime("%a %-d %b %Y, %-I:%M %p")


def copy_for(kind: DeliveryKind, reg: EventRegistrationsLink, event: Events, org_name: str) -> dict:
    """The words for each kind of attachment, so one channel serves both."""
    first = (reg.full_name or "").split(" ")[0] or "there"
    e = html.escape
    if kind is DeliveryKind.certificate:
        return {
            "subject": f"Your certificate for {event.name}",
            "text": (
                f"Hi {first},\n\nThank you for attending {event.name}. Your certificate is attached "
                f"({reg.ticket_id}).\n\nThe QR code on it lets anyone confirm it is genuine.\n\n{org_name}"
            ),
            "html": (
                f"<p>Hi {e(first)},</p><p>Thank you for attending <strong>{e(event.name)}</strong>. "
                f"Your certificate is attached (<code>{e(reg.ticket_id)}</code>).</p>"
                f"<p>The QR code on it lets anyone confirm it is genuine.</p><p>{e(org_name)}</p>"
            ),
        }
    when = _when(event)
    venue = f" · {event.location_name}" if event.location_name else ""
    return {
        "subject": f"Your ticket for {event.name}",
        "text": (
            f"Hi {first},\n\nYour ticket for {event.name} is attached ({reg.ticket_id}).\n{when}{venue}\n\n"
            f"Show the QR code at the door, on your phone or printed.\n\n{org_name}"
        ),
        "html": (
            f"<p>Hi {e(first)},</p><p>Your ticket for <strong>{e(event.name)}</strong> is attached "
            f"(<code>{e(reg.ticket_id)}</code>).</p><p>{e(when)}{e(venue)}</p>"
            f"<p>Show the QR code at the door, on your phone or printed.</p><p>{e(org_name)}</p>"
        ),
    }


async def _document(session, kind: DeliveryKind, reg: EventRegistrationsLink):
    if kind is DeliveryKind.ticket:
        return await ticket_document(session, reg)
    from app.api.events.certificates import certificate_document

    return await certificate_document(session, reg)


async def _designed(session, kind: DeliveryKind, reg: EventRegistrationsLink, event: Events):
    if kind is not DeliveryKind.ticket:
        return None
    from app.api.emails import service as emails

    return await emails.invite_email(session, reg, event)


async def deliver(session, delivery_id: uuid.UUID) -> Optional[Deliveries]:
    """Worker: one guest, one channel. Records whatever happened."""
    d = await session.scalar(
        select(Deliveries)
        .options(
            joinedload(Deliveries.registration).options(
                joinedload(EventRegistrationsLink.event).options(joinedload(Events.club)),
                joinedload(EventRegistrationsLink.user),
            )
        )
        .where(Deliveries.id == delivery_id)
    )
    if d is None or d.status is DeliveryStatus.sent:
        return d
    reg = d.registration
    event = reg.event
    org_name = event.club.name if event.club else ""

    status, provider_id, error, version = DeliveryStatus.skipped, None, None, 0
    if d.kind is DeliveryKind.ticket and reg.ticket_revoked_at is not None:
        error = "ticket revoked — reinstate it to send again"
    elif d.kind is DeliveryKind.certificate and (reg.certificate_issued_at is None or reg.certificate_revoked_at is not None):
        error = "no valid certificate — issue or reinstate it to send"
    elif d.channel is DeliveryChannel.whatsapp:
        error = providers_mod.NO_WHATSAPP
    elif not reg.email:
        error = "no email address"
    elif reg.email_bounced_at is not None:
        # A dead mailbox is not worth another try, and trying costs the sending domain's name.
        error = providers_mod.BOUNCED
    else:
        try:
            # A ticket goes out in the event's designed invite when there is
            # one switched on; otherwise in the plain words below.
            designed = await _designed(session, d.kind, reg, event)
            if designed is not None:
                version = designed.design_version
                response = await asyncio.to_thread(
                    email_mod.send_email,
                    recipients=[reg.email],
                    subject=designed.subject,
                    text=designed.text,
                    html=designed.html,
                    attachments=designed.attachments,
                    sender_name=org_name or None,
                )
            else:
                doc = await _document(session, d.kind, reg)
                version = doc.design_version
                copy = copy_for(d.kind, reg, event, org_name)
                # boto3 is synchronous; the send must not hold the event loop.
                response = await asyncio.to_thread(
                    email_mod.send_email_with_attachment,
                    recipients=[reg.email],
                    subject=copy["subject"],
                    body_text=copy["text"],
                    body_html=copy["html"],
                    attachment_bytes=doc.content,
                    attachment_filename=doc.filename,
                    sender_name=org_name or None,
                )
            status = DeliveryStatus.sent
            provider_id = (response or {}).get("MessageId")
        except Exception as exc:  # the row is the record; the reason goes in it
            status = DeliveryStatus.failed
            error = (str(exc) or exc.__class__.__name__)[:400]
            logger.warning("delivery %s failed: %s", d.id, error)

    d.status = status
    d.provider_id = provider_id
    d.error = error
    d.attempts = (d.attempts or 0) + 1
    d.sent_at = datetime.now(timezone.utc) if status is DeliveryStatus.sent else None
    d.design_version = version
    await session.commit()
    return d


async def run(delivery_ids: list) -> None:
    """The background entry point: every queued row, one after another."""
    if not delivery_ids:
        return
    async with session_scope() as session:
        for did in delivery_ids:
            try:
                await deliver(session, did)
            except Exception:
                logger.exception("delivery %s could not be processed", did)
                await session.rollback()


async def send_ticket(session, event: Events, registration: EventRegistrationsLink, background_tasks=None) -> None:
    """The ticket email at registration or payment: queued like any other
    send, so the attendee list shows it landing, and sent after the response
    when there is a response to wait for. Nothing is re-sent if the guest
    already has one."""
    result = await queue(session, event, DeliveryKind.ticket, registration_ids=[registration.id], only="unsent")
    if not result["ids"]:
        return
    if background_tasks is not None:
        background_tasks.add_task(run, result["ids"])
        return
    for did in result["ids"]:
        await deliver(session, did)


# --- the ticket itself --------------------------------------------------------


async def set_revoked(session, event: Events, registration_id, revoked: bool, reason: Optional[str] = None):
    reg = await _registration(session, event.id, registration_id)
    reg.ticket_revoked_at = datetime.now(timezone.utc) if revoked else None
    await session.commit()
    if reason:
        logger.info("ticket %s revoked: %s", reg.ticket_id, reason)
    return reg


def _safe_name(s: str) -> str:
    return (re.sub(r"[^A-Za-z0-9 _.-]+", "", s or "").strip()[:60]) or "guest"


async def bundle_zip(session, event: Events) -> bytes:
    """Every issued ticket PDF, one file each, named by ticket id and guest."""
    regs = list(
        await session.scalars(
            _active(event.id)
            .options(
                joinedload(EventRegistrationsLink.event).options(joinedload(Events.club)),
                joinedload(EventRegistrationsLink.user),
            )
            .where(EventRegistrationsLink.ticket_revoked_at.is_(None))
            .order_by(EventRegistrationsLink.ticket_id)
        )
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if not regs:
            z.writestr("README.txt", "No tickets have been issued for this event yet.")
        for reg in regs:
            doc = await ticket_document(session, reg)
            z.writestr(f"{reg.ticket_id} {_safe_name(reg.full_name)}.pdf", doc.content)
    return buf.getvalue()


# --- reading back -------------------------------------------------------------


async def summary(session, event: Events, kind: DeliveryKind) -> dict:
    rows = (
        await session.execute(
            select(Deliveries.channel, Deliveries.status, func.count())
            .where(Deliveries.event_id == event.id, Deliveries.kind == kind)
            .group_by(Deliveries.channel, Deliveries.status)
        )
    ).all()
    counts = {(c, s): n for c, s, n in rows}
    registrations = await session.scalar(
        select(func.count()).select_from(_active(event.id).subquery())
    ) or 0
    revoked = await session.scalar(
        select(func.count()).select_from(
            _active(event.id).where(EventRegistrationsLink.ticket_revoked_at.isnot(None)).subquery()
        )
    ) or 0
    channels = []
    for channel in DeliveryChannel:
        n = lambda s: counts.get((channel, s), 0)  # noqa: E731
        with_row = sum(n(s) for s in DeliveryStatus)
        channels.append({
            "channel": channel.value,
            "sent": n(DeliveryStatus.sent),
            "failed": n(DeliveryStatus.failed),
            "skipped": n(DeliveryStatus.skipped),
            "pending": n(DeliveryStatus.pending),
            "unsent": max(0, registrations - with_row),
        })
    return {"kind": kind.value, "registrations": registrations, "revoked": revoked, "channels": channels, "providers": providers_mod.providers()}


def _sends(event_id: int):
    """Ticket and certificate sends and message deliveries, as one list.

    They live in two tables because they are made differently, but nobody
    asking "who never got their ticket" cares which table it is in. Enums are
    cast to text so the two halves line up."""
    from app.api.emails.models import MessageDeliveries, MessageKind, Messages

    documents = select(
        Deliveries.id.label("id"),
        Deliveries.registration_id.label("registration_id"),
        cast(Deliveries.kind, String).label("kind"),
        literal(None, type_=String).label("what"),
        cast(Deliveries.channel, String).label("channel"),
        cast(Deliveries.status, String).label("status"),
        cast(Deliveries.outcome, String).label("outcome"),
        Deliveries.reason.label("reason"),
        Deliveries.error.label("error"),
        Deliveries.attempts.label("attempts"),
        Deliveries.sent_at.label("sent_at"),
        Deliveries.outcome_at.label("outcome_at"),
        Deliveries.created_at.label("created_at"),
    ).where(Deliveries.event_id == event_id)
    messages = (
        select(
            MessageDeliveries.id.label("id"),
            MessageDeliveries.registration_id.label("registration_id"),
            literal("message", type_=String).label("kind"),
            Messages.subject.label("what"),
            cast(MessageDeliveries.channel, String).label("channel"),
            cast(MessageDeliveries.status, String).label("status"),
            cast(MessageDeliveries.outcome, String).label("outcome"),
            MessageDeliveries.reason.label("reason"),
            MessageDeliveries.error.label("error"),
            literal(1).label("attempts"),
            MessageDeliveries.sent_at.label("sent_at"),
            MessageDeliveries.outcome_at.label("outcome_at"),
            MessageDeliveries.created_at.label("created_at"),
        )
        .join(Messages, Messages.id == MessageDeliveries.message_id)
        .where(Messages.event_id == event_id, Messages.kind != MessageKind.TEST)
    )
    return union_all(documents, messages).subquery("sends")


async def log(
    session,
    event: Events,
    *,
    kind: Optional[str] = None,
    channel: Optional[DeliveryChannel] = None,
    problem: bool = False,
    search: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Every send to every guest, newest first: tickets, certificates and
    messages together. The tally is always the whole event's, so narrowing
    to the failures never makes the number look smaller than it is."""
    s = _sends(event.id)
    problems = [o.value for o in PROBLEM_OUTCOMES]
    base = select(s, EventRegistrationsLink).join(EventRegistrationsLink, EventRegistrationsLink.id == s.c.registration_id)
    if kind is not None:
        base = base.where(s.c.kind == kind)
    if channel is not None:
        base = base.where(s.c.channel == channel.value)
    if problem:
        base = base.where((s.c.status == DeliveryStatus.failed.value) | (s.c.outcome.in_(problems)))
    if search:
        pat = f"%{search}%"
        base = base.where(
            EventRegistrationsLink.full_name.ilike(pat)
            | EventRegistrationsLink.email.ilike(pat)
            | EventRegistrationsLink.ticket_id.ilike(pat)
        )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = (
        await session.execute(
            base.order_by(s.c.sent_at.desc().nulls_last(), s.c.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    items = []
    for row in rows:
        reg = row.EventRegistrationsLink
        items.append({
            "id": row.id,
            "registration_id": reg.id,
            "ticket_id": reg.ticket_id,
            "name": reg.full_name or "",
            "address": reg.email if row.channel == DeliveryChannel.email.value else reg.phone,
            "kind": row.kind,
            "what": row.what,
            "channel": row.channel,
            "status": row.status,
            "outcome": row.outcome,
            "reason": row.reason or row.error,
            "attempts": row.attempts or 0,
            "sent_at": row.sent_at,
            "outcome_at": row.outcome_at,
        })

    tally_q = select(s.c.status, s.c.outcome, func.count())
    if kind is not None:
        tally_q = tally_q.where(s.c.kind == kind)
    counts = (await session.execute(tally_q.group_by(s.c.status, s.c.outcome))).all()
    n = lambda f: sum(c for st, o, c in counts if f(st, o))  # noqa: E731
    sent, failed, skipped, pending = (x.value for x in (DeliveryStatus.sent, DeliveryStatus.failed, DeliveryStatus.skipped, DeliveryStatus.pending))
    tally = {
        "sent": n(lambda st, o: st == sent),
        "delivered": n(lambda st, o: o in (DeliveryOutcome.delivered.value, DeliveryOutcome.read.value)),
        # Handed over and nothing heard back -- not the same as delivered.
        "awaiting": n(lambda st, o: st == sent and o is None),
        "bounced": n(lambda st, o: o == DeliveryOutcome.bounced.value),
        "complained": n(lambda st, o: o == DeliveryOutcome.complained.value),
        "failed": n(lambda st, o: st == failed),
        "skipped": n(lambda st, o: st == skipped),
        "pending": n(lambda st, o: st == pending),
    }
    return {"items": items, "total": total, "page": page, "page_size": page_size, "tally": tally}


async def by_registration(session, event_id: int, kind: DeliveryKind, registration_ids: list) -> dict:
    """The rows for a page of the attendee list, keyed by registration."""
    if not registration_ids:
        return {}
    out: dict = {}
    for d in await session.scalars(
        select(Deliveries).where(
            Deliveries.event_id == event_id,
            Deliveries.kind == kind,
            Deliveries.registration_id.in_(list(registration_ids)),
        )
    ):
        out.setdefault(d.registration_id, []).append({
            "channel": d.channel.value,
            "status": d.status.value,
            "error": d.error,
            "outcome": d.outcome.value if d.outcome else None,
            "reason": d.reason,
            "attempts": d.attempts or 0,
            "sent_at": d.sent_at,
        })
    return out
