"""Sending documents to guests, and remembering what happened.

Every channel honours one contract: never re-send a channel already SENT
unless asked to, record a result for every attempt, and treat "no address
on this channel" as SKIPPED -- never as an error. WhatsApp is stricter: it
goes once per guest, kind and channel, asked or not (a second ticket on
someone's phone is worse than none), and only to a guest who ticked the box
for it at registration, with a number it can reach.

How a send runs -- the claim, the workers, the pace, the resumer -- is in
app/api/deliveries/pipeline.py; this module says what a ticket or
certificate send is.

A club on Enterprise with its own WhatsApp number (app/api/club_whatsapp)
has its WhatsApp sends go from that number -- when it is connected and
active, routing is on, and the club's account has the template approved --
and from the platform's number otherwise, as for every other club. A send
from the club's number that Meta refuses for the account or the template
goes from the platform's number instead, and the club is told.
"""
import html
import io
import logging
import re
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import String, and_, case, cast, exists, func, literal, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.deliveries import pipeline
from app.api.deliveries.models import (
    SHOWN,
    SHOWN_STATUSES,
    UNFINISHED,
    Deliveries,
    DeliveryAttempts,
    DeliveryChannel,
    DeliveryKind,
    DeliveryOutcome,
    DeliveryStatus,
)
from app.api.events.access import ensure_event_access, is_admin_user
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import ticket_document
from app.api.message_quota import service as quota
from app.core import jobs
from app.core import providers as providers_mod
from app.core import whatsapp
from app.core.email import email as email_mod
from app.core.email import transport
from app.core.mail import suppression
from app.core.utils.db_fields import TZAwareDateTime
from app.db.core import AsyncSessionLocal
from app.db.listeners import add_loader_criteria
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def channels() -> list[DeliveryChannel]:
    """The channels a send goes out on: email, and WhatsApp once the server
    holds its keys. Without them no WhatsApp row is made at all: a row
    marked sent for a message that never left would hide every real failure
    behind a success that never happened."""
    return [DeliveryChannel.email, *([DeliveryChannel.whatsapp] if whatsapp.configured() else [])]


#: What each document is, to the club's message quota (app/api/message_quota).
QUOTA_KIND = {DeliveryKind.ticket: "ticket", DeliveryKind.certificate: "certificate", DeliveryKind.reminder: "reminder"}


def whatsapp_via(route) -> str:
    """Which number a WhatsApp send goes from: the club's own (quota.CLUB)
    when it has a route there (app/api/club_whatsapp), which the club pays
    Meta for and is never counted or held back; otherwise the platform's
    (quota.PLATFORM), counted against the club's plan. What is counted is
    the answer's {"Via"}: a send that fell back to the platform's number
    says "platform"."""
    return quota.CLUB if route is not None else quota.PLATFORM


async def channels_for(session, event: Events) -> list[DeliveryChannel]:
    """`channels()`, and WhatsApp too when the event's club sends from its
    own number though the platform has none configured."""
    from app.api.club_whatsapp import service as club_whatsapp

    out = channels()
    if DeliveryChannel.whatsapp not in out and await club_whatsapp.can_route(session, event.club_id):
        out.append(DeliveryChannel.whatsapp)
    return out


def _wants_whatsapp() -> list:
    """The guests a WhatsApp row is made for: they asked, and left a number."""
    return [
        EventRegistrationsLink.whatsapp_opt_in == True,  # noqa: E712
        func.coalesce(func.btrim(EventRegistrationsLink.phone), "") != "",
    ]


PROBLEM_OUTCOMES = (DeliveryOutcome.bounced, DeliveryOutcome.complained, DeliveryOutcome.undelivered)

#: A row queued longer ago than this and never sent is not sent by the
#: resumer: a ticket from yesterday's queue may be for an event now over.
RESUME_WINDOW = timedelta(hours=24)

# What a send may put back in the queue. In flight (pending, sending) is left
# alone: a second click must not reset what a worker is sending right now.
AGAIN = (DeliveryStatus.failed, DeliveryStatus.skipped, DeliveryStatus.dead)
AGAIN_FORCED = (DeliveryStatus.sent, *AGAIN)


@asynccontextmanager
async def session_scope():
    """A session for work that outlives the request. Tests swap this for one
    on their own transaction."""
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


def _db(session=None) -> pipeline.Db:
    # session_scope is looked up at each call, so a test's stand-in is used.
    return pipeline.Db(lambda: session_scope(), session=session)


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


def _eligible(kind: DeliveryKind):
    """The registrations this kind of document can be sent to."""
    if kind is DeliveryKind.certificate:
        # Not issued one, or had it taken back: nothing to send.
        return and_(EventRegistrationsLink.certificate_issued_at.isnot(None), EventRegistrationsLink.certificate_revoked_at.is_(None))
    # A ticket, or the reminder of one.
    return EventRegistrationsLink.ticket_revoked_at.is_(None)


# --- queueing ---------------------------------------------------------------


async def queue(
    session,
    event: Events,
    kind: DeliveryKind,
    *,
    registration_ids: Optional[list] = None,
    only: str = "unsent",
    force: bool = False,
    send: bool = False,
    email_only: bool = False,
) -> dict:
    """Put a pending row in place for every guest and channel that should be
    sent, and answer with what was queued and why the rest was not. Nothing is
    sent here. With `send`, the job that sends them (`start`) is written in
    the same transaction as the rows, so the rows cannot be committed without
    it; otherwise the caller sends them (`run`, `deliver`). A send for one
    guest -- their ticket at registration or payment, one sent again -- is
    someone waiting, and goes before a send to the whole event (HIGH).

    One statement per channel: INSERT ... ON CONFLICT DO UPDATE ... WHERE the
    row may be queued again. Two clicks at once both succeed and queue each
    guest once; a row a worker holds (pending, sending) is never reset under
    it -- it counts as queued, since it is.

    `email_only`: a send on the public's behalf (a ticket sent again to whoever
    typed the address) is mail alone; WhatsApp is never set off by it."""
    scope = [EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False]  # noqa: E712
    if registration_ids:
        scope.append(EventRegistrationsLink.id.in_(list(registration_ids)))
    eligible_ids = select(EventRegistrationsLink.id).where(*scope, _eligible(kind))

    revoked = await session.scalar(
        select(func.count()).select_from(EventRegistrationsLink).where(*scope, sa.not_(_eligible(kind)))
    ) or 0
    # What was sent before, for these guests only when the send names them:
    # a registration's own ticket (send_ticket) once read every delivery the
    # event ever made -- some 3,700 rows a registration on a 7,600-guest event.
    before = (
        await session.execute(
            select(Deliveries.status, func.count())
            .where(Deliveries.event_id == event.id, Deliveries.kind == kind, Deliveries.registration_id.in_(eligible_ids))
            .group_by(Deliveries.status)
        )
    ).all()
    before = {status: n for status, n in before}
    already_sent = 0 if force else before.get(DeliveryStatus.sent, 0)
    in_flight = sum(before.get(s, 0) for s in UNFINISHED)

    now = pipeline.now()
    again = AGAIN_FORCED if force else AGAIN
    reset = dict(
        status=DeliveryStatus.pending, error=None, outcome=None, outcome_at=None, reason=None, reason_code=None,
        tries=0, queued_at=now, claimed_at=None, next_attempt_at=None, updated_at=now,
    )
    table = Deliveries.__table__
    ids: list[uuid.UUID] = []
    active = [DeliveryChannel.email] if email_only else await channels_for(session, event)
    for channel in active:
        by_whatsapp = channel is DeliveryChannel.whatsapp
        if only == "failed":
            # Retry what failed, was skipped or was given up on; nothing new.
            stmt = (
                update(table)
                .where(table.c.event_id == event.id, table.c.kind == kind, table.c.channel == channel,
                       table.c.status.in_(AGAIN), table.c.registration_id.in_(eligible_ids))
                .values(**reset)
                .returning(table.c.id)
            )
        else:
            source = select(
                func.gen_random_uuid(), literal(event.id), EventRegistrationsLink.id,
                cast(literal(kind.name), table.c.kind.type), cast(literal(channel.name), table.c.channel.type),
                cast(literal(DeliveryStatus.pending.name), table.c.status.type),
                literal(0), literal(0), literal(0), literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()),
            ).where(*scope, _eligible(kind), *(_wants_whatsapp() if by_whatsapp else []))
            stmt = (
                pg_insert(table)
                .from_select(
                    ["id", "event_id", "registration_id", "kind", "channel", "status", "attempts", "tries", "design_version",
                     "queued_at", "created_at", "updated_at"],
                    source,
                )
                # A WhatsApp message already sent is never sent again, even
                # when the email is (`force`): once per guest, kind and channel.
                .on_conflict_do_update(
                    constraint="uq_deliveries_registration_kind_channel", set_=reset,
                    where=table.c.status.in_(AGAIN if by_whatsapp else again),
                )
                .returning(table.c.id)
            )
        ids.extend((await session.execute(stmt)).scalars().all())
    if send and ids:
        one_guest = registration_ids is not None and len(registration_ids) == 1
        await start(session, event.id, kind, priority=jobs.HIGH if one_guest else None)
    await session.commit()
    return {
        "queued": len(ids) + in_flight,
        "already_sent": already_sent,
        "revoked": revoked,
        "channels": [c.value for c in active],
        "ids": ids,
    }


# --- what a send says --------------------------------------------------------


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


def _venue(event: Events) -> str:
    """Where, for the reminder's "starts ... at {{4}}"."""
    if (event.location_name or "").strip():
        return event.location_name.strip()
    return "online" if event.is_online else "the venue"


#: The template each kind is sent with: the platform's, or the club's own of that name.
TEMPLATE_OF = {
    DeliveryKind.ticket: "ticket_confirmed",
    DeliveryKind.reminder: "event_reminder",
    DeliveryKind.certificate: "certificate_ready",
}


def whatsapp_skip(reg: EventRegistrationsLink, *, own: bool = False, own_missing: Optional[str] = None) -> Optional[str]:
    """Why this guest gets nothing by WhatsApp, or None when they may.
    `own`: the club's own number will send it (no platform number needed);
    `own_missing`: why the club's number can't, when it is the only one.

    The demo's invented people (MAIL_SUPPRESS_DOMAINS) are left alone here as
    their mail is at the transport: nobody behind them asked for anything."""
    if not whatsapp.configured() and not own:
        return own_missing or providers_mod.NO_WHATSAPP
    if not reg.whatsapp_opt_in:
        return providers_mod.NO_WHATSAPP_OPT_IN
    if reg.wa_bounced_at is not None:
        return providers_mod.WA_BOUNCED
    user = reg.__dict__.get("user")
    if suppression.at_suppressed_domain(reg.email) or (user is not None and suppression.at_suppressed_domain(user.email)):
        return providers_mod.DEMO_ACCOUNT
    number, why = whatsapp.normalise(reg.phone)
    if number is None:
        return f"no WhatsApp number: {why}"
    return None


async def _send_whatsapp(db: pipeline.Db, ready: "_Ready") -> tuple[dict, int]:
    """The approved template for this kind, with its document when it has one."""
    d, reg, event = ready.d, ready.reg, ready.event
    when = _when(event)
    if d.kind is DeliveryKind.reminder:
        message = whatsapp.event_reminder(name=reg.full_name, event=event.name, when=when, venue=_venue(event), ticket_id=reg.ticket_id)
        return await _whatsapp_send(db, ready, message, None), 0
    doc = await _document(db, d.kind, reg)
    if d.kind is DeliveryKind.ticket:
        message = whatsapp.ticket_confirmed(name=reg.full_name, event=event.name, when=when, ticket_id=reg.ticket_id)
    else:
        from app.api.events.certificates import number_of

        # The certificate's own number, which the public verifier takes -- never the ticket id the door scans.
        message = whatsapp.certificate_ready(name=reg.full_name, event=event.name, club=ready.org_name, number=number_of(reg))
    return await _whatsapp_send(db, ready, message, (doc.content, doc.filename)), doc.design_version


async def _note(db: pipeline.Db, delivery_id, what: str, write) -> None:
    """`write(session)` in a transaction of its own, which nothing that goes
    wrong in it -- opening it, the write, the commit -- can turn into the
    send's outcome: a message Meta took must be recorded as sent, or
    "retry failed" sends the guest a second copy. The write is in a
    SAVEPOINT (flushed as it ends), so a bad row is undone there and a
    shared session (deliver()'s) is left as it was."""
    try:
        async with db.tx() as s:
            try:
                async with s.begin_nested():
                    await write(s)
            except Exception as failure:  # noqa: BLE001 - the send matters more than the note
                logger.warning("delivery %s: %s (%s)", delivery_id, what, type(failure).__name__)
    except Exception as failure:  # noqa: BLE001 - nor can opening or committing its transaction
        logger.warning("delivery %s: %s (%s)", delivery_id, what, type(failure).__name__)


async def _whatsapp_send(db: pipeline.Db, ready: "_Ready", message: whatsapp.Template, document) -> dict:
    """From the club's own number when `ready.route` names it, else the
    platform's. A refusal from the club's number about its account or its
    template marks that (the club is told), and so does one only the club's
    number meets (its test list, the guest being that number); the send then
    goes from the platform's number when there is one -- held back first, as
    any platform send, when the club's plan has no room for it. Any other
    refusal is the send's."""
    from app.api.club_whatsapp import inbox as club_inbox
    from app.api.club_whatsapp import service as club_whatsapp

    route, reg, delivery_id, kind = ready.route, ready.reg, ready.d.id, ready.d.kind.value
    if route is None:
        return await whatsapp.send(reg.phone, message, document=document)
    try:
        out = await whatsapp.send(reg.phone, message, document=document, sender=route.sender)
    except whatsapp.WhatsAppError as exc:
        if not club_whatsapp.falls_back(exc):
            raise
        route.forget(exc)
        await _note(db, delivery_id, "the club's number's failure could not be noted",
                    lambda s: club_whatsapp.after_failure(s, route, exc))
        if not whatsapp.configured():
            raise
        if ready.quota_kind is not None and ready.event is not None:
            # Counted as the platform's now: the club's plan decides, as for any club.
            async with db.tx() as s:
                ready.terms, held = await quota.check(
                    s, ready.event.club_id, ready.d.channel.value, ready.quota_kind, via=quota.PLATFORM, terms=ready.terms,
                )
            if held:
                raise whatsapp.Undeliverable(held)
        logger.info("delivery %s: the club's own number refused it (%s); sent from the platform's", delivery_id, exc.code)
        return await whatsapp.send(reg.phone, message, document=document)

    async def note_it(s) -> None:
        await club_inbox.record_delivery_send(
            s, route, out.get("Address") or "", message, out.get("MessageId"), delivery_id=delivery_id, kind=kind,
        )

    # In the club's inbox, as what its number sent that person.
    await _note(db, delivery_id, "not added to the club's inbox", note_it)
    return out


# --- one row: claim, read, render, send, record -------------------------------


@dataclass
class _Claim:
    id: uuid.UUID
    claimed_at: datetime
    tries: int


@dataclass
class _Ready:
    """What reading the row found: enough to render and send without the database."""

    d: Deliveries
    reg: EventRegistrationsLink
    event: Optional[Events]
    org_name: str
    skip: Optional[str] = None
    # The event's designed invite (subject, design, attachments, brand), or
    # None for the plain words.
    invite: Optional[dict] = None
    # The club's message terms, and what this send is to its quota (None:
    # not counted -- no club).
    terms: Optional[quota.Terms] = None
    quota_kind: Optional[str] = None
    # A WhatsApp send from the club's own number (app/api/club_whatsapp), or
    # None for the platform's.
    route: Optional[object] = None


@dataclass
class _Run:
    """What one job keeps between rows: each event's invite and brand, and
    the pictures its emails carry, read or fetched once."""

    invites: dict = field(default_factory=dict)
    assets: dict = field(default_factory=dict)
    # Each club's message terms (app/api/message_quota), read once a job.
    terms: dict = field(default_factory=dict)
    # Which number each club's template goes from (club_whatsapp.route_for),
    # decided once a job and forgotten when the club's number refuses one.
    routes: dict = field(default_factory=dict)


def _claim_stmt(where: list, n: int, at: datetime):
    picked = (
        select(Deliveries.id)
        .where(*where, Deliveries.status == DeliveryStatus.pending)
        .limit(n)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return (
        update(Deliveries)
        .where(Deliveries.id.in_(picked), Deliveries.status == DeliveryStatus.pending)
        .values(
            status=DeliveryStatus.sending, claimed_at=at, attempts=Deliveries.attempts + 1, tries=Deliveries.tries + 1, updated_at=at,
        )
        .returning(Deliveries.id, Deliveries.tries)
        .execution_options(synchronize_session=False)
    )


async def _claim(db: pipeline.Db, where: list, n: int) -> list[_Claim]:
    """Up to n pending rows, each taken by this worker alone."""
    at = pipeline.now()
    rows = (await db.execute(_claim_stmt(where, n, at))).all()
    return [_Claim(id=r[0], claimed_at=at, tries=r[1]) for r in rows]


async def _release(db: pipeline.Db, claims: list[_Claim]) -> None:
    """Claimed and never started (shutdown): back in the queue, the try not counted."""
    if not claims:
        return
    at = pipeline.now()
    await db.execute(
        update(Deliveries)
        .where(Deliveries.id.in_([c.id for c in claims]), Deliveries.status == DeliveryStatus.sending)
        .values(status=DeliveryStatus.pending, claimed_at=None, attempts=Deliveries.attempts - 1, tries=Deliveries.tries - 1, updated_at=at)
        .execution_options(synchronize_session=False)
    )


async def _read(db: pipeline.Db, c: _Claim, run: _Run) -> Optional[_Ready]:
    """One short transaction: the row, the guest, the event, and whether to send at all."""
    from app.api.emails import service as emails

    async with db.tx() as s:
        d = await s.scalar(
            select(Deliveries)
            .options(
                joinedload(Deliveries.registration).options(
                    joinedload(EventRegistrationsLink.event).options(joinedload(Events.club)),
                    joinedload(EventRegistrationsLink.user),
                )
            )
            .where(Deliveries.id == c.id)
            # As the claim left it: a session that queued it again (a send on
            # the caller's session) holds the row as it was before, and its
            # queued_at is the send's idempotency key and its quota ref.
            .execution_options(populate_existing=True)
        )
        if d is None:
            return None
        reg = d.registration
        event = reg.event
        if event is None:
            # Removed since it was queued: nothing to send for it.
            return _Ready(d=d, reg=reg, event=None, org_name="", skip="the event was removed")
        ready = _Ready(d=d, reg=reg, event=event, org_name=event.club.name if event.club else "")
        if d.kind in (DeliveryKind.ticket, DeliveryKind.reminder) and reg.ticket_revoked_at is not None:
            ready.skip = "ticket revoked — reinstate it to send again"
        elif d.kind is DeliveryKind.certificate and (reg.certificate_issued_at is None or reg.certificate_revoked_at is not None):
            ready.skip = "no valid certificate — issue or reinstate it to send"
        elif d.kind is DeliveryKind.reminder and event.event_datetime <= pipeline.now():
            ready.skip = "the event had started — too late for a reminder"
        elif d.channel is DeliveryChannel.whatsapp:
            from app.api.club_whatsapp import service as club_whatsapp

            template = TEMPLATE_OF.get(d.kind)
            ready.route = await club_whatsapp.route_for(s, event.club_id, template, cache=run.routes) if template else None
            missing = None
            if ready.route is None and not whatsapp.configured() and template and await club_whatsapp.can_route(s, event.club_id):
                missing = f"your WhatsApp number has no approved {template} template yet"
            ready.skip = whatsapp_skip(reg, own=ready.route is not None, own_missing=missing)
        elif d.kind is DeliveryKind.reminder:
            ready.skip = "the reminder goes by WhatsApp only"
        elif not reg.email:
            ready.skip = "no email address"
        elif reg.email_bounced_at is not None:
            # A dead mailbox is not worth another try, and trying costs the sending domain's name.
            ready.skip = providers_mod.BOUNCED
        else:
            suppressed = (await suppression.reasons(s, [reg.email])).get(suppression.normal(reg.email))
            if suppressed is not None:
                # Bounced or complained at another event or club: the same address, the same answer.
                ready.skip = suppression.why(suppressed)
        if ready.skip is None and event.club_id is not None:
            # The club's plan: a reminder past Free's limit is held back,
            # with the reason on the row; a ticket or certificate always goes.
            ready.quota_kind = QUOTA_KIND[d.kind]
            via = whatsapp_via(ready.route) if d.channel is DeliveryChannel.whatsapp else quota.PLATFORM
            ready.terms, ready.skip = await quota.check(s, event.club_id, d.channel.value, ready.quota_kind, via=via, cache=run.terms)
        if ready.skip is None and d.kind is DeliveryKind.ticket and d.channel is DeliveryChannel.email:
            # A ticket goes out in the event's designed invite when there is
            # one switched on; otherwise in the plain words.
            if event.id not in run.invites:
                run.invites[event.id] = await emails.invite_for(s, event)
            ready.invite = run.invites[event.id]
        return ready


async def _send(db: pipeline.Db, c: _Claim, ready: _Ready, run: _Run) -> tuple[dict, int]:
    """Render and send, with no transaction open. The provider's answer and the design version."""
    from app.api.emails import service as emails

    d, reg, event = ready.d, ready.reg, ready.event
    if d.channel is DeliveryChannel.whatsapp:
        return await _send_whatsapp(db, ready)
    key = f"delivery-{d.id}-{int((d.queued_at or d.created_at).timestamp())}"
    if ready.invite is not None:
        out = await emails.compose_invite(db, event, reg, ready.invite, cache=run.assets)
        response = await pipeline.send_with_backoff(
            email_mod.send_email, recipients=[reg.email], subject=out.subject, text=out.text, html=out.html,
            attachments=out.attachments, sender_name=ready.org_name or None, idempotency_key=key, check_suppression=False,
        )
        return response or {}, out.design_version
    doc = await _document(db, d.kind, reg)
    copy = copy_for(d.kind, reg, event, ready.org_name)
    response = await pipeline.send_with_backoff(
        email_mod.send_email_with_attachment, recipients=[reg.email], subject=copy["subject"], body_text=copy["text"],
        body_html=copy["html"], attachment_bytes=doc.content, attachment_filename=doc.filename,
        sender_name=ready.org_name or None, idempotency_key=key, check_suppression=False,
    )
    return response or {}, doc.design_version


async def _record(
    db: pipeline.Db, c: _Claim, ready: Optional[_Ready], values: dict, *,
    attempt: Optional[dict] = None, dead: bool = False, dead_number: Optional[str] = None,
    meter: Optional[dict] = None,
) -> None:
    """One short transaction: the result on the row (if this worker still
    holds it), the attempt the provider took, the admins told of a dead one,
    -- `dead_number` -- the guest's number marked as one WhatsApp cannot
    reach, so later sends skip it until it is corrected, and -- `meter` --
    the message counted against the club's monthly quota (once per send:
    its ref is the row and when it was queued, the provider's own key)."""
    async with db.tx() as s:
        res = await s.execute(
            update(Deliveries)
            .where(Deliveries.id == c.id, Deliveries.status == DeliveryStatus.sending, Deliveries.claimed_at == c.claimed_at)
            .values(claimed_at=None, updated_at=pipeline.now(), **values)
            .execution_options(synchronize_session=False)
        )
        if not res.rowcount:
            logger.warning("delivery %s: the result came after its claim was given up; recorded the attempt only", c.id)
        if attempt and attempt.get("provider_id"):
            await s.execute(
                pg_insert(DeliveryAttempts.__table__)
                .values(id=uuid.uuid4(), delivery_id=c.id, created_at=pipeline.now(), **attempt)
                .on_conflict_do_nothing(index_elements=["provider_id"])
            )
        if dead_number and ready is not None:
            await s.execute(
                update(EventRegistrationsLink)
                .where(EventRegistrationsLink.id == ready.reg.id)
                .values(wa_bounced_at=pipeline.now(), wa_bounce_reason=dead_number[:300])
                .execution_options(synchronize_session=False)
            )
        if dead and res.rowcount and ready is not None and ready.event is not None:
            await pipeline.alert_dead(
                s, event_id=ready.event.id, club_id=ready.event.club_id, event_name=ready.event.name,
                what=ready.d.kind.value, error=values.get("error") or "",
            )
        if meter is not None:
            # Counted even when the claim was given up meanwhile: the provider took it.
            await quota.count(s, **meter)


async def _handle(db: pipeline.Db, c: _Claim, run: _Run) -> None:
    """Worker: one guest, one channel. Records whatever happened."""
    ready = await _read(db, c, run)
    if ready is None:
        return
    if ready.skip is not None:
        await _record(db, c, ready, dict(status=DeliveryStatus.skipped, error=ready.skip, provider_id=None, sent_at=None, next_attempt_at=None))
        return
    try:
        response, version = await _send(db, c, ready, run)
    except whatsapp.Undeliverable as exc:
        # A fact about the number, not a failure.
        await _record(
            db, c, ready, dict(status=DeliveryStatus.skipped, error=exc.reason, provider_id=None, sent_at=None, next_attempt_at=None),
            dead_number=exc.reason if exc.permanent else None,
        )
        return
    except transport.MailDeferred as exc:
        error = (str(exc) or exc.__class__.__name__)[:300]
        if c.tries >= pipeline.max_tries():
            await _record(db, c, ready, dict(status=DeliveryStatus.dead, error=f"gave up after {c.tries} tries: {error}"[:400], next_attempt_at=None), dead=True)
        else:
            await _record(db, c, ready, dict(status=DeliveryStatus.pending, error=f"will try again: {error}"[:400], next_attempt_at=pipeline.now() + pipeline.backoff(c.tries)))
        logger.warning("delivery %s deferred (try %d): %s", c.id, c.tries, error)
        return
    except Exception as exc:  # the row is the record; the reason goes in it
        error = (str(exc) or exc.__class__.__name__)[:400]
        logger.warning("delivery %s failed: %s", c.id, error)
        await _record(db, c, ready, dict(status=DeliveryStatus.failed, error=error, provider_id=None, sent_at=None, next_attempt_at=None))
        return
    provider_id = response.get("MessageId")
    meter = None
    if ready.quota_kind is not None:
        d = ready.d
        meter = dict(
            club_id=ready.event.club_id, channel=d.channel.value, kind=ready.quota_kind,
            via=response.get("Via") or quota.PLATFORM, terms=ready.terms,
            ref=f"delivery:{d.id}:{int((d.queued_at or d.created_at).timestamp())}",
        )
    await _record(
        db, c, ready,
        dict(status=DeliveryStatus.sent, error=None, provider_id=provider_id, sent_at=pipeline.now(), design_version=version, next_attempt_at=None),
        # The address it went to: the mailbox, or the number in E.164.
        attempt={"provider": response.get("Provider"), "provider_id": provider_id, "address": response.get("Address") or suppression.normal(ready.reg.email)},
        meter=meter,
    )




async def deliver(session, delivery_id: uuid.UUID) -> Optional[Deliveries]:
    """One row, now, on the caller's session (committed as it goes, so no
    connection is held while it renders or sends). Nothing happens unless the
    row is pending -- a row another worker holds, or one already sent, is
    left alone. Returns the row as it stands after."""
    db = _db(session)
    claims = await _claim(db, [Deliveries.id == delivery_id], 1)
    if claims:
        await _handle(db, claims[0], _Run())
    return await session.get(Deliveries, delivery_id, populate_existing=True)


async def run(delivery_ids: list) -> None:
    """The background entry point: the queued rows, a few at a time, each
    claimed before it is sent."""
    if not delivery_ids:
        return
    ids = list(delivery_ids)
    db = _db()
    left = list(ids)

    async def claim(n: int) -> list[_Claim]:
        # Through the given ids a chunk at a time; a chunk whose rows another
        # worker holds (or already sent) yields nothing, and the next is tried.
        while left:
            chunk, left[:] = left[:n], left[n:]
            got = await _claim(db, [Deliveries.id.in_(chunk)], n)
            if got:
                return got
        return []

    run_state = _Run()
    await pipeline.run(
        ("delivery-ids", uuid.uuid4()),
        lambda: pipeline.pump(claim, lambda c: _handle(db, c, run_state), release=lambda claims: _release(db, claims)),
    )


async def _run_due(event_id: int, kind: DeliveryKind) -> int:
    """The resumer's job: whatever of this event's kind is pending and due."""
    db = _db()
    run_state = _Run()

    def due() -> list:
        return [
            Deliveries.event_id == event_id,
            Deliveries.kind == kind,
            sa.or_(Deliveries.next_attempt_at.is_(None), Deliveries.next_attempt_at <= pipeline.now()),
        ]

    return await pipeline.pump(
        lambda n: _claim(db, due(), n),
        lambda c: _handle(db, c, run_state),
        release=lambda claims: _release(db, claims),
    )


# --- the jobs ---------------------------------------------------------------


@jobs.handler("deliveries.send", max_attempts=6, timeout=3600.0, concurrency=2)
async def _send_job(payload: dict) -> None:
    """What is pending and due of one event's tickets or certificates: the
    rows are claimed one by one before they are sent, so this job running
    twice, or beside another send of the same event, sends nobody twice. A
    big send takes minutes at the provider's pace (an hour is its limit;
    what is left then is the resumer's)."""
    event_id, kind = int(payload["event_id"]), DeliveryKind(payload["kind"])
    await pipeline.run(("delivery-job", uuid.uuid4()), lambda: _run_due(event_id, kind))


async def start(session, event_id: int, kind: DeliveryKind, *, dedupe: bool = False, priority: Optional[int] = None) -> None:
    """Have the event's pending sends of this kind sent after the caller's
    commit -- by this process's job runner, or the worker's. `dedupe`: not
    if a job for them is already waiting or running (the resumer's minute).
    `priority`: the job's, NORMAL unless said (jobs.HIGH for one guest's)."""
    await jobs.enqueue(
        session, "deliveries.send", {"event_id": int(event_id), "kind": kind.value},
        dedupe_key=f"deliveries:{int(event_id)}:{kind.value}" if dedupe else None, priority=priority,
    )


async def resume(session, now: Optional[datetime] = None) -> dict:
    """The minute sweep's look at deliveries: put back the claims of workers
    that died, give up on what waited too long or failed too often, and
    enqueue a job for each event with something due (one per event and kind,
    not one a minute while a long send runs). Sends nothing here."""
    now = now or pipeline.now()
    db = _db(session)
    stale = (
        await db.execute(
            update(Deliveries)
            .where(Deliveries.status == DeliveryStatus.sending, Deliveries.claimed_at < now - pipeline.STALE_CLAIM)
            .values(status=DeliveryStatus.pending, claimed_at=None, next_attempt_at=now, updated_at=now)
            .returning(Deliveries.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    too_old = (
        await db.execute(
            update(Deliveries)
            .where(Deliveries.status == DeliveryStatus.pending, sa.func.coalesce(Deliveries.queued_at, Deliveries.created_at) < now - RESUME_WINDOW)
            .values(status=DeliveryStatus.failed, error="not sent: queued over a day ago and never finished — send it again if it still matters", updated_at=now)
            .returning(Deliveries.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    # A row back from a dead worker as often as it may be tried is given up on.
    gave_up = (
        await db.execute(
            update(Deliveries)
            .where(Deliveries.status == DeliveryStatus.pending, Deliveries.tries >= pipeline.max_tries())
            .values(status=DeliveryStatus.dead, error=f"gave up after {pipeline.max_tries()} tries", updated_at=now)
            .returning(Deliveries.event_id, Deliveries.kind)
            .execution_options(synchronize_session=False)
        )
    ).all()
    if gave_up:
        async with db.tx() as s:
            for event_id in sorted({e for e, _k in gave_up}):
                event = await s.get(Events, event_id)
                await pipeline.alert_dead(
                    s, event_id=event_id, club_id=getattr(event, "club_id", None), event_name=getattr(event, "name", ""),
                    what="ticket or certificate", error="its worker stopped every time it was tried",
                )
    due = (
        await db.execute(
            select(Deliveries.event_id, Deliveries.kind)
            .where(
                Deliveries.status == DeliveryStatus.pending,
                sa.or_(
                    Deliveries.next_attempt_at <= now,
                    and_(Deliveries.next_attempt_at.is_(None), Deliveries.updated_at < now - pipeline.ABANDONED),
                ),
            )
            .distinct()
        )
    ).all()
    started = 0
    if due:
        async with db.tx() as s:
            for event_id, kind in due:
                await start(s, event_id, kind, dedupe=True)
                started += 1
    return {"stale": len(stale), "too_old": len(too_old), "dead": len(gave_up), "started": started}


# --- the day-before reminder, by WhatsApp ---------------------------------------

#: The reminder comes due this long before the start...
REMINDER_LEAD = timedelta(hours=24)
#: ...and is not sent in the last hour before it: a sweep that was down
#: catches up until then, and after that the doors-open email has it.
REMINDER_CUTOFF = timedelta(hours=1)
#: Only to a guest who registered at least this long before it comes due:
#: whoever registered within the day has just had their ticket, which says
#: when and where.
REMINDER_AFTER_SIGNUP = timedelta(hours=1)


async def queue_reminders(session, now: Optional[datetime] = None) -> int:
    """The minute sweep's look for WhatsApp reminders come due: a pending
    row for every guest who asked for WhatsApp, holds a live ticket (paid
    for, on a paid event) and has none yet, on a published event starting
    within REMINDER_LEAD and not within REMINDER_CUTOFF -- and the job that
    sends them, in the same transaction.

    Once per registration, ever: the delivery row is the record, and the
    unique (registration, kind, channel) key turns a second sweep, or two at
    once, into nothing. Without the platform's WhatsApp number, only for the
    clubs that send from their own (app/api/club_whatsapp). The journey
    emails are the club's own words, which WhatsApp does not carry; this is
    the approved event_reminder template, at the same moment as the
    pre-event email's default. Returns how many were queued."""
    from app.api.closeout.models import EventStatus
    from app.api.club_whatsapp import models as club_whatsapp

    # Without the platform's number, only the events of clubs that send from
    # their own (whether each reminder can go is decided at its send).
    own_only = not whatsapp.configured()
    if own_only and not await session.scalar(
        select(func.count()).select_from(club_whatsapp.ClubWhatsAppAccounts)
        .where(club_whatsapp.ClubWhatsAppAccounts.status == club_whatsapp.ACTIVE, club_whatsapp.ClubWhatsAppAccounts.routing == True)  # noqa: E712
    ):
        return 0

    now = now or pipeline.now()
    table = Deliveries.__table__
    R, E = EventRegistrationsLink, Events
    has_one = (
        select(literal(1))
        .where(Deliveries.registration_id == R.id, Deliveries.kind == DeliveryKind.reminder, Deliveries.channel == DeliveryChannel.whatsapp)
    )
    source = (
        select(
            func.gen_random_uuid(), R.event_id, R.id,
            cast(literal(DeliveryKind.reminder.name), table.c.kind.type), cast(literal(DeliveryChannel.whatsapp.name), table.c.channel.type),
            cast(literal(DeliveryStatus.pending.name), table.c.status.type),
            literal(0), literal(0), literal(0), literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()),
        )
        .select_from(R)
        .join(E, E.id == R.event_id)
        .where(
            # ix_events_shown_start: the events starting in the window, and no others.
            E.is_deleted == False,  # noqa: E712
            E.lifecycle.in_([EventStatus.published, EventStatus.live]),
            E.event_datetime > now + REMINDER_CUTOFF,
            E.event_datetime <= now + REMINDER_LEAD,
            # ix_registrations_whatsapp_opt_in: the guests who asked, event by event.
            R.is_deleted == False,  # noqa: E712
            *_wants_whatsapp(),
            _eligible(DeliveryKind.reminder),
            or_(E.has_fee == False, R.is_paid == True),  # noqa: E712
            R.created_at <= E.event_datetime - literal(REMINDER_LEAD + REMINDER_AFTER_SIGNUP, sa.Interval()),
            ~exists(has_one),
        )
    )
    if own_only:
        A = club_whatsapp.ClubWhatsAppAccounts
        source = source.where(E.club_id.in_(
            select(A.club_id).where(A.status == club_whatsapp.ACTIVE, A.routing == True)  # noqa: E712
        ))
    stmt = (
        pg_insert(table)
        .from_select(
            ["id", "event_id", "registration_id", "kind", "channel", "status", "attempts", "tries", "design_version",
             "queued_at", "created_at", "updated_at"],
            source,
        )
        .on_conflict_do_nothing(constraint="uq_deliveries_registration_kind_channel")
        .returning(table.c.event_id)
    )
    event_ids = (await session.execute(stmt)).scalars().all()
    for event_id in sorted(set(event_ids)):
        await start(session, event_id, DeliveryKind.reminder, dedupe=True)
    await session.commit()
    return len(event_ids)


async def send_ticket(session, event: Events, registration: EventRegistrationsLink, background_tasks=None) -> None:
    """The ticket email at registration or payment: queued like any other
    send, so the attendee list shows it landing, and -- when there is a
    response to wait for (`background_tasks`) -- sent by a job after the
    commit, off the request. Nothing is re-sent if the guest already has one."""
    result = await queue(session, event, DeliveryKind.ticket, registration_ids=[registration.id], only="unsent", send=background_tasks is not None)
    if not result["ids"] or background_tasks is not None:
        return
    for did in result["ids"]:
        await deliver(session, did)


#: How soon one ticket can be sent again because someone asked from the public form.
RESEND_GAP = timedelta(minutes=10)


async def resend_ticket(session, event: Events, registration: EventRegistrationsLink, background_tasks=None) -> bool:
    """The ticket once more, to the address it was sent to, for someone who
    tried to register again with it.

    Anyone can type an address into the public form, so this goes only to
    the registration's own address, and at most once every ten minutes."""
    return await resend_document(session, event, registration, DeliveryKind.ticket, inline=background_tasks is None)


async def resend_document(session, event: Events, registration: EventRegistrationsLink, kind: DeliveryKind, *, inline: bool = False) -> bool:
    """A ticket or certificate once more, to the registration's own address,
    for someone the public asked on behalf of: the registration form, a
    stale link's "send me a fresh one" (app/api/emails/old_links.py).

    At most once every RESEND_GAP per registration and kind; nothing when
    there is nothing to send (a revoked ticket, no certificate issued).
    Sent by a job after the caller's commit (HIGH: one guest's); `inline`
    sends it now, on this session. True when it was queued."""
    prior = await session.scalar(
        select(Deliveries).where(
            Deliveries.registration_id == registration.id,
            Deliveries.kind == kind,
            Deliveries.channel == DeliveryChannel.email,
        )
    )
    if prior is not None:
        last = prior.sent_at if prior.status is DeliveryStatus.sent else prior.updated_at
        if last is not None and datetime.now(timezone.utc) - last < RESEND_GAP:
            return False
    result = await queue(session, event, kind, registration_ids=[registration.id], force=True, send=not inline, email_only=True)
    if not result["ids"]:
        return False
    if inline:
        for did in result["ids"]:
            await deliver(session, did)
    return True


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

def shown(status: DeliveryStatus) -> DeliveryStatus:
    return SHOWN.get(status, status)


def shown_sql(column):
    """`shown`, in SQL, as text."""
    return case(
        (column == DeliveryStatus.sending, literal(DeliveryStatus.pending.value)),
        (column == DeliveryStatus.dead, literal(DeliveryStatus.failed.value)),
        else_=cast(column, String),
    )


async def summary(session, event: Events, kind: DeliveryKind) -> dict:
    rows = (
        await session.execute(
            select(Deliveries.channel, Deliveries.status, func.count())
            .where(Deliveries.event_id == event.id, Deliveries.kind == kind)
            .group_by(Deliveries.channel, Deliveries.status)
        )
    ).all()
    counts: dict = {}
    for c, s, n in rows:
        counts[(c, shown(s))] = counts.get((c, shown(s)), 0) + n
    registrations = await session.scalar(
        select(func.count()).select_from(_active(event.id).subquery())
    ) or 0
    revoked = await session.scalar(
        select(func.count()).select_from(
            _active(event.id).where(EventRegistrationsLink.ticket_revoked_at.isnot(None)).subquery()
        )
    ) or 0
    # WhatsApp's audience is the guests who asked for it, not everyone: the
    # rest are not "unsent" there, they were never to be sent anything.
    wanting = await session.scalar(
        select(func.count()).select_from(_active(event.id).where(*_wants_whatsapp()).subquery())
    ) if DeliveryChannel.whatsapp in await channels_for(session, event) else 0
    channels = []
    for channel in DeliveryChannel:
        n = lambda s: counts.get((channel, s), 0)  # noqa: E731
        with_row = sum(n(s) for s in SHOWN_STATUSES)
        audience = wanting if channel is DeliveryChannel.whatsapp else registrations
        channels.append({
            "channel": channel.value,
            "sent": n(DeliveryStatus.sent),
            "failed": n(DeliveryStatus.failed),
            "skipped": n(DeliveryStatus.skipped),
            "pending": n(DeliveryStatus.pending),
            "unsent": max(0, (audience or 0) - with_row),
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
        # A ticket or certificate needs no words; the reminder says what it is.
        case((Deliveries.kind == DeliveryKind.reminder, literal("Day-before reminder")), else_=literal(None, type_=String)).label("what"),
        cast(Deliveries.channel, String).label("channel"),
        shown_sql(Deliveries.status).label("status"),
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
            shown_sql(MessageDeliveries.status).label("status"),
            cast(MessageDeliveries.outcome, String).label("outcome"),
            MessageDeliveries.reason.label("reason"),
            MessageDeliveries.error.label("error"),
            MessageDeliveries.attempts.label("attempts"),
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
        select(Deliveries)
        .where(
            Deliveries.event_id == event_id,
            Deliveries.kind == kind,
            Deliveries.registration_id.in_(list(registration_ids)),
        )
        # Email first, then WhatsApp, for every guest.
        .order_by(Deliveries.registration_id, Deliveries.channel)
    ):
        out.setdefault(d.registration_id, []).append({
            "channel": d.channel.value,
            "status": shown(d.status).value,
            "error": d.error,
            "outcome": d.outcome.value if d.outcome else None,
            "reason": d.reason,
            "attempts": d.attempts or 0,
            "sent_at": d.sent_at,
        })
    return out
