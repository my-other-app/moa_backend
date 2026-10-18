"""Designed emails: the club's library, each event's copies, and every
message sent from one.

An event starts with the four journey emails copied from the club's
library (itself seeded from the starters the first time it is asked for),
so a club that designs once sends the same emails at every event. A send
freezes the design into a message with one delivery per guest; workers then
claim, render and send them, a few at a time, and record what happened
(app/api/deliveries/pipeline.py). The minute sweep only queues: a timed
email becomes a message and a job, and a 10,000-guest reminder takes its
turn at the provider beside everyone else's instead of in front of them.
"""
import asyncio
import base64
import logging
import time
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.clubs.models import Clubs
from app.api.deliveries import pipeline
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import UNFINISHED, DeliveryAttempts, DeliveryChannel, DeliveryStatus
from app.api.emails.models import EmailStage, EmailTemplates, MessageDeliveries, MessageKind, MessageStatus, Messages
from app.api.events.documents import (
    EVENT_TIMEZONE,
    fetch_bytes,
    layout_colors,
    layout_sample_values,
    layout_values,
    qr_data_url,
    qr_png_base64,
)
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import poster_url, ticket_document
from app.api.templates_audience import RegistrationRole
from app.config import settings
from app.core import after_commit
from app.core.email import email as email_mod
from app.core.email import transport
from app.core.mail import links, suppression
from app.core.mail.render import RenderInput, parse_email_template, render_email, sample_email_values, uses_qr
from app.core.mail.schema import (
    EMAIL_BLOCK_META,
    EMAIL_ONLY_TOKENS,
    EMAIL_STAGE_META,
    JOURNEY_STAGES,
    EmailAttachments,
    EmailAudience,
    EmailDesign,
    EmailTemplateFields,
    EmailTrigger,
)
from app.core.mail.starters import email_starter
from app.core.storage import objects
from app.core.storage.images import image_ext
from app.core.utils.db_fields import TZAwareDateTime
from app.db.locks import singleton
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

STAGE_ORDER = {EmailStage(s): i for i, s in enumerate(JOURNEY_STAGES)}
STAGE_ORDER[EmailStage.CUSTOM] = len(JOURNEY_STAGES)
# Keeps a timer from sending an old event's reminder on the day it is deployed.
SWEEP_GRACE = timedelta(days=2)
SWEEP_LOCK = 773101
#: First key of the lock a template's send takes, so two clicks queue one message.
SEND_LOCK = 773102
#: A second click on Send while the first message is still going returns it.
DOUBLE_CLICK = timedelta(seconds=60)
#: A message still unfinished this long after it was made is not resumed:
#: "doors open in an hour" is worse than nothing five hours late.
RESUME_WINDOW = timedelta(hours=6)


# --- scope ----------------------------------------------------------------------


async def club_of(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


def image_prefix(*, event_id: Optional[int] = None, club_id: Optional[int] = None) -> str:
    base = settings.S3_BASE_PATH.rstrip("/")
    if event_id is not None:
        return f"{base}/events/{event_id}/layout/"
    return f"{base}/clubs/{club_id}/email/"


def _own_key(key: str, *, event_id: Optional[int], club_id: int) -> bool:
    if key.startswith(image_prefix(club_id=club_id)):
        return True
    return event_id is not None and key.startswith(image_prefix(event_id=event_id))


# --- templates ------------------------------------------------------------------


def fields_of(tpl: EmailTemplates) -> EmailTemplateFields:
    return EmailTemplateFields.model_validate(
        {
            "name": tpl.name,
            "subject": tpl.subject,
            "design": tpl.design or {},
            "attachments": tpl.attachments or {},
            "audience": tpl.audience or {},
            "trigger": tpl.trigger or {},
            "channels": tpl.channels or ["email"],
            "enabled": tpl.enabled,
        }
    )


def _apply(tpl: EmailTemplates, fields: EmailTemplateFields) -> None:
    tpl.name = fields.name
    tpl.subject = fields.subject
    tpl.design = fields.design.model_dump(by_alias=True)
    tpl.attachments = fields.attachments.model_dump(by_alias=True)
    tpl.audience = fields.audience.model_dump(by_alias=True)
    tpl.trigger = fields.trigger.model_dump(by_alias=True)
    tpl.channels = list(fields.channels)
    tpl.enabled = fields.enabled


def _new(club_id: int, event_id: Optional[int], stage: EmailStage, fields: EmailTemplateFields, *, source_id=None, created_by=None) -> EmailTemplates:
    tpl = EmailTemplates(id=uuid.uuid4(), club_id=club_id, event_id=event_id, stage=stage, version=1, source_id=source_id, created_by=created_by)
    _apply(tpl, fields)
    return tpl


def _texts(subject: str, design: EmailDesign):
    """Every piece of template text in a design, with where it lives."""
    yield "subject", subject
    yield "theme.footer", design.theme.footer
    for b in design.blocks:
        k = b.kind
        if k in ("heading", "paragraph"):
            yield f"{k} {b.id}", b.text
        elif k == "list":
            for i, item in enumerate(b.items):
                yield f"list {b.id} item {i + 1}", item
        elif k == "button":
            yield f"button {b.id}", b.label
            yield f"button {b.id} link", b.href
        elif k == "image" and b.href:
            yield f"image {b.id} link", b.href
        elif k == "details":
            for r in b.rows:
                yield f"details {b.id} {r.label}", r.value
        elif k == "ticket_card":
            yield f"card {b.id} eyebrow", b.eyebrow
            yield f"card {b.id} title", b.title
            yield f"card {b.id} note", b.note
            for r in b.rows:
                yield f"card {b.id} {r.label}", r.value


def design_problems(subject: str, design: EmailDesign) -> list[str]:
    out = []
    for where, src in _texts(subject, design):
        for err in parse_email_template(src).errors:
            out.append(f"{where}: {err}")
    return out


def validate_fields(fields: EmailTemplateFields, *, event_id: Optional[int], club_id: int, stage: EmailStage = EmailStage.CUSTOM) -> None:
    problems = design_problems(fields.subject, fields.design)
    if problems:
        raise CustomHTTPException(422, message=problems[0], errors={"fields": {"design": problems}})
    for b in fields.design.blocks:
        if b.kind == "image" and b.source == "upload" and b.image_key and not _own_key(b.image_key, event_id=event_id, club_id=club_id):
            raise CustomHTTPException(422, message="That image belongs to another event.", errors={"fields": {"design": ["image: not this event's upload"]}})
    if fields.trigger.mode in ("BEFORE_START", "AFTER_END") and not fields.trigger.minutes:
        raise CustomHTTPException(422, message="Say how long before or after.", errors={"fields": {"trigger": "minutes"}})
    if stage is EmailStage.INVITE and fields.trigger.mode != "ON_TICKETS":
        raise CustomHTTPException(
            422,
            message="The invite goes out with the tickets. Use the pre-event email for a timed send.",
            errors={"fields": {"trigger": "The invite goes out with the tickets."}},
        )


def _sorted(templates: list[EmailTemplates]) -> list[EmailTemplates]:
    return sorted(templates, key=lambda t: (STAGE_ORDER.get(t.stage, 99), t.created_at or datetime.min.replace(tzinfo=timezone.utc)))


# A burst of first registrations used to copy an event's journey emails three
# to six times, so every attendee got each reminder that many times. The
# unique indexes on (event_id, stage) and, for the library, (club_id, stage)
# (journey stages only) now make a second copy impossible: the copies go in
# with ON CONFLICT DO NOTHING, and whoever comes second waits for the first
# to commit, inserts nothing, and reads back the first one's rows. No lock of
# our own is needed (the advisory locks that did this job are gone).

_ROW_COLUMNS = (
    "id", "club_id", "event_id", "stage", "name", "subject", "design", "attachments", "audience", "trigger",
    "channels", "enabled", "version", "source_id", "created_by",
)


def _row(club_id: int, event_id: Optional[int], stage: EmailStage, fields: EmailTemplateFields, *, source_id=None) -> dict:
    tpl = _new(club_id, event_id, stage, fields, source_id=source_id)
    now = datetime.now(timezone.utc)
    return {**{c: getattr(tpl, c) for c in _ROW_COLUMNS}, "created_at": now, "updated_at": now}


async def _insert_missing(session, rows: list[dict]) -> None:
    """Insert the journey emails nobody has yet; any another request inserted
    meanwhile is left as it is. No conflict target: the unique index that
    raises the conflict is uq_email_templates_event_stage or, for the library,
    uq_email_templates_library_stage, and the statement also runs (without
    the guarantee) on a database the migration has not reached yet."""
    await session.execute(pg_insert(EmailTemplates.__table__).values(rows).on_conflict_do_nothing())


def _library_query(club_id: int):
    return select(EmailTemplates).where(EmailTemplates.club_id == club_id, EmailTemplates.event_id.is_(None))


async def ensure_library(session, club_id: int) -> list[EmailTemplates]:
    """The club's library, with the four journey emails present."""
    rows = list(await session.scalars(_library_query(club_id)))
    have = {t.stage for t in rows}
    missing = [EmailStage(s) for s in JOURNEY_STAGES if EmailStage(s) not in have]
    if missing:
        await _insert_missing(session, [_row(club_id, None, stage, email_starter(stage.value)) for stage in missing])
        rows = list(await session.scalars(_library_query(club_id)))
    return _sorted(rows)


async def ensure_event_templates(session, event: Events) -> list[EmailTemplates]:
    """The event's emails, with every journey stage present: copied from the
    club's library the first time, so a club's design carries over."""
    if event.club_id is None:
        raise CustomHTTPException(422, message="This event has no club, so it has no emails to send from.")
    query = select(EmailTemplates).where(EmailTemplates.event_id == event.id)
    rows = list(await session.scalars(query))
    have = {t.stage for t in rows if t.stage is not EmailStage.CUSTOM}
    missing = [EmailStage(s) for s in JOURNEY_STAGES if EmailStage(s) not in have]
    if missing:
        library = {t.stage: t for t in await ensure_library(session, event.club_id)}
        await _insert_missing(
            session,
            [_row(event.club_id, event.id, stage, fields_of(library[stage]), source_id=library[stage].id) for stage in missing],
        )
        await session.commit()
        rows = list(await session.scalars(query))
    return _sorted(rows)


async def template_of(session, template_id, *, event: Optional[Events] = None, club_id: Optional[int] = None) -> EmailTemplates:
    tpl = await session.get(EmailTemplates, template_id)
    if tpl is None:
        raise CustomHTTPException(404, message="Email not found")
    if event is not None and tpl.event_id != event.id:
        raise CustomHTTPException(404, message="Email not found")
    if event is None and (tpl.club_id != club_id or tpl.event_id is not None):
        raise CustomHTTPException(404, message="Email not found")
    return tpl


async def create_template(session, *, club_id: int, event_id: Optional[int], fields: EmailTemplateFields, stage: EmailStage = EmailStage.CUSTOM, created_by=None) -> EmailTemplates:
    validate_fields(fields, event_id=event_id, club_id=club_id, stage=stage)
    if stage is not EmailStage.CUSTOM:
        dup = await session.scalar(select(EmailTemplates).where(EmailTemplates.club_id == club_id, EmailTemplates.event_id == event_id if event_id is not None else EmailTemplates.event_id.is_(None), EmailTemplates.stage == stage))
        if dup is not None:
            raise CustomHTTPException(409, message=f"There is already a {EMAIL_STAGE_META[stage.value]['label']} email.")
    tpl = _new(club_id, event_id, stage, fields, created_by=created_by)
    session.add(tpl)
    try:
        await session.commit()
    except IntegrityError:
        # The same journey stage made a moment ago by another request.
        await session.rollback()
        raise CustomHTTPException(409, message=f"There is already a {EMAIL_STAGE_META[stage.value]['label']} email.")
    return tpl


async def update_template(session, tpl: EmailTemplates, fields: EmailTemplateFields) -> EmailTemplates:
    validate_fields(fields, event_id=tpl.event_id, club_id=tpl.club_id, stage=tpl.stage)
    before = (tpl.subject, tpl.design)
    _apply(tpl, fields)
    if (tpl.subject, tpl.design) != before:
        tpl.version = (tpl.version or 1) + 1
    await session.commit()
    return tpl


async def remove_template(session, tpl: EmailTemplates) -> None:
    if tpl.stage is not EmailStage.CUSTOM:
        raise CustomHTTPException(422, message="A journey email cannot be deleted. Turn it off instead.")
    await session.delete(tpl)
    await session.commit()


async def duplicate_template(session, tpl: EmailTemplates, created_by=None) -> EmailTemplates:
    fields = fields_of(tpl)
    fields.name = f"Copy of {fields.name}"[:80]
    fields.trigger = EmailTrigger()
    copy = _new(tpl.club_id, tpl.event_id, EmailStage.CUSTOM, fields, source_id=tpl.source_id, created_by=created_by)
    session.add(copy)
    await session.commit()
    return copy


async def save_to_library(session, tpl: EmailTemplates) -> EmailTemplates:
    """This event's version becomes the club's default for the stage, so the
    next event starts from it. A custom email becomes a new library one."""
    if tpl.event_id is None:
        return tpl
    fields = fields_of(tpl)
    target = None
    if tpl.stage is not EmailStage.CUSTOM:
        library = {t.stage: t for t in await ensure_library(session, tpl.club_id)}
        target = library.get(tpl.stage)
    if target is None:
        fields.trigger = EmailTrigger()
        target = _new(tpl.club_id, None, tpl.stage, fields, created_by=tpl.created_by)
        session.add(target)
    else:
        _apply(target, fields)
        target.version = (target.version or 1) + 1
    tpl.source_id = target.id
    await session.commit()
    return target


async def reset_template(session, tpl: EmailTemplates) -> EmailTemplates:
    """Back to what the event started from: the library's version of the
    stage, or the starter."""
    source = None
    if tpl.stage is not EmailStage.CUSTOM:
        library = {t.stage: t for t in await ensure_library(session, tpl.club_id)}
        source = library.get(tpl.stage)
    fields = fields_of(source) if source is not None else email_starter(tpl.stage.value)
    _apply(tpl, fields)
    tpl.version = (tpl.version or 1) + 1
    tpl.source_id = source.id if source is not None else None
    await session.commit()
    return tpl


# --- the journey ----------------------------------------------------------------


def due_at(event: Events, trigger: EmailTrigger) -> Optional[datetime]:
    if trigger.mode == "BEFORE_START" and trigger.minutes:
        return event.event_datetime - timedelta(minutes=trigger.minutes)
    if trigger.mode == "AFTER_END" and trigger.minutes:
        end = event.event_datetime + timedelta(hours=event.duration or 0)
        return end + timedelta(minutes=trigger.minutes)
    return None


async def last_message_for(session, tpl: EmailTemplates) -> Optional[Messages]:
    return await session.scalar(select(Messages).where(Messages.template_id == tpl.id).order_by(Messages.created_at.desc()).limit(1))


async def journey(session, event: Events) -> dict:
    templates = await ensure_event_templates(session, event)
    stages = []
    custom = []
    for tpl in templates:
        if tpl.stage is EmailStage.CUSTOM:
            custom.append(tpl)
            continue
        last = await last_message_for(session, tpl)
        scheduled_sent = last is not None and last.scheduled
        meta = EMAIL_STAGE_META[tpl.stage.value]
        stages.append(
            {
                "stage": tpl.stage.value,
                "label": meta["label"],
                "description": meta["note"],
                "template": tpl,
                "due_at": None if scheduled_sent or not tpl.enabled else due_at(event, fields_of(tpl).trigger),
                "last_message": last,
            }
        )
    return {"journey": stages, "custom": custom}


# --- values and rendering --------------------------------------------------------


def organiser_email(event: Events) -> str:
    club = getattr(event, "club", None)
    return event.contact_email or (club.contact_email if club else None) or ""


def logo_url_of(event: Events) -> Optional[str]:
    club = getattr(event, "club", None)
    logo = getattr(club, "logo", None) if club is not None else None
    if isinstance(logo, dict):
        return logo.get("medium") or logo.get("original") or logo.get("large")
    return logo if isinstance(logo, str) else None


async def brand_primary(session, event: Events) -> str:
    """The ticket design's accent, so emails match the ticket."""
    from app.api.ticket_templates.service import get_template_for_event

    try:
        style = await get_template_for_event(session, event.id, role=None)
    except Exception:
        style = None
    return layout_colors(style or {}, getattr(event, "club", None))["primary"]


def doors_in(start: datetime, now: Optional[datetime] = None) -> str:
    """Minutes until the doors open, for a reminder sent just before."""
    now = now or datetime.now(timezone.utc)
    minutes = int((start - now).total_seconds() // 60)
    return str(max(minutes, 0))


def has_certificate(reg: EventRegistrationsLink) -> bool:
    return reg.certificate_issued_at is not None and reg.certificate_revoked_at is None


def guest_values(event: Events, reg: EventRegistrationsLink) -> dict:
    """Every token for this guest: the ticket's, the certificate's when they
    hold one, and the email-only ones."""
    v = layout_values(reg, event, "certificate" if has_certificate(reg) else "ticket")
    v["ticket_url"] = "" if reg.ticket_revoked_at else links.file_url("ticket", reg.ticket_id)
    v["certificate_url"] = links.file_url("certificate", reg.ticket_id) if has_certificate(reg) else ""
    v["organiser_email"] = organiser_email(event)
    v["event_start"] = f"{v['event_day'][:3]} {v['event_date']} · {v['event_time']}"
    v["doors_in"] = doors_in(event.event_datetime)
    # The code the door types when the QR will not scan (app.core.mail.schema).
    v.setdefault("ticket_code", reg.ticket_id or "")
    return v


def sample_values(event) -> dict:
    return sample_email_values(layout_sample_values(event, "ticket"), organiser_email=organiser_email(event), api_origin=settings.PUBLIC_API_URL)


def sample_event(club: Clubs):
    """A stand-in event for the library's preview, which has none."""
    return SimpleNamespace(
        id=0, name="Sample event", event_datetime=datetime.now(timezone.utc) + timedelta(days=14), duration=3.0,
        location_name="Innovation Hall", contact_email=club.contact_email, additional_details=[], club=club, poster=None,
    )


@dataclass
class Outgoing:
    subject: str
    text: str
    html: str
    attachments: list = field(default_factory=list)
    design_version: int = 0


def _image_blocks(design: EmailDesign):
    return [b for b in design.blocks if b.kind == "image"]


async def compose(
    session,
    event: Events,
    reg: Optional[EventRegistrationsLink],
    subject: str,
    design: EmailDesign,
    attachments: EmailAttachments,
    *,
    brand: str,
    logo_url: Optional[str],
    values: Optional[dict] = None,
    cache: Optional[dict] = None,
) -> Outgoing:
    """The email as it will be sent: pictures inline by Content-ID so the
    message carries them, the guest's files attached when asked for.

    `cache` is a send's own: the pictures every guest's copy carries are
    fetched for the first guest and reused for the rest, rather than read
    from storage once a guest."""
    values = values if values is not None else (guest_values(event, reg) if reg is not None else sample_values(event))
    parts: list[dict] = []
    images: dict = {}

    def inline(data: Optional[bytes], cid: str) -> Optional[str]:
        if not data:
            return None
        parts.append({"filename": f"{cid}.{image_ext(data)}", "content": data, "content_id": cid})
        return f"cid:{cid}"

    async def fetched(fn, arg):
        key = (getattr(fn, "__name__", "fetch"), arg)
        if cache is not None and key in cache:
            return cache[key]
        data = await asyncio.to_thread(fn, arg)
        if cache is not None:
            cache[key] = data
        return data

    # Storage reads, image downloads, the QR and the render all block: they
    # run in a worker thread, so a send (the minute sweep's included) never
    # holds the event loop that every other request on this worker needs.
    blocks = _image_blocks(design)
    for b in blocks:
        if b.source == "upload" and b.image_key and b.image_key not in images:
            src = inline(await fetched(objects.get_bytes, b.image_key), f"img-{len(images) + 1}")
            if src:
                images[b.image_key] = src
    logo = inline(await fetched(fetch_bytes, logo_url), "org-logo") if design.theme.logo == "org_logo" and logo_url else None
    icon = inline(await fetched(fetch_bytes, poster_url(event) or ""), "event-icon") if any(b.source == "event_icon" for b in blocks) else None
    qr = None
    if uses_qr(design):
        png = await asyncio.to_thread(qr_png_base64, reg.ticket_id if reg is not None else "SAMPLE")
        qr = inline(base64.b64decode(png), "ticket-qr")
    out = await asyncio.to_thread(
        render_email,
        RenderInput(subject=subject, design=design, values=values, brand_primary=brand, logo_url=logo, event_icon_url=icon, images=images, qr_url=qr),
    )
    version = 0
    if reg is not None:
        if attachments.ticket_pdf and reg.ticket_revoked_at is None:
            doc = await ticket_document(session, reg)
            version = doc.design_version
            parts.append({"filename": doc.filename, "content": doc.content})
        if attachments.certificate_pdf and has_certificate(reg):
            from app.api.events.certificates import certificate_document

            doc = await certificate_document(session, reg)
            parts.append({"filename": doc.filename, "content": doc.content})
    return Outgoing(subject=out.subject, text=out.text, html=out.html, attachments=parts, design_version=version)


async def loaded_registration(session, event_id: int, registration_id) -> EventRegistrationsLink:
    """A guest with what rendering needs in memory: the event, its club, the user."""
    reg = await session.scalar(
        deliveries._active(event_id)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))
        .where(EventRegistrationsLink.id == registration_id)
    )
    if reg is None:
        raise CustomHTTPException(404, message="Registration not found")
    return reg


async def preview(session, event, subject: str, design: EmailDesign, *, registration_id=None, club_id: Optional[int] = None) -> dict:
    """What the editor shows: sample values, or a real guest's, with pictures
    by URL and the QR as data so nothing needs the network."""
    reg = await loaded_registration(session, event.id, registration_id) if registration_id is not None and getattr(event, "id", 0) else None
    values = guest_values(event, reg) if reg is not None else sample_values(event)
    images = {b.image_key: objects.presigned_url(b.image_key) for b in _image_blocks(design) if b.source == "upload" and b.image_key}
    brand = await brand_primary(session, event) if getattr(event, "id", 0) else layout_colors({})["primary"]
    out = render_email(
        RenderInput(
            subject=subject, design=design, values=values, brand_primary=brand, logo_url=logo_url_of(event),
            event_icon_url=poster_url(event), images=images, qr_url=qr_data_url(reg.ticket_id if reg is not None else "SAMPLE") if uses_qr(design) else None,
        )
    )
    return {"subject": out.subject, "html": out.html, "text": out.text, "problems": design_problems(subject, design)}


async def send_test(session, event: Events, *, to: str, subject: str, design: EmailDesign, attachments: EmailAttachments, registration_id=None) -> dict:
    reg = await loaded_registration(session, event.id, registration_id) if registration_id is not None else None
    out = await compose(session, event, reg, subject, design, attachments, brand=await brand_primary(session, event), logo_url=logo_url_of(event))
    club = getattr(event, "club", None)
    # Nothing to write: the request's connection goes back before SES answers.
    await session.commit()
    await pipeline.send_with_backoff(
        email_mod.send_email, recipients=[to], subject=f"[Test] {out.subject}", text=out.text, html=out.html,
        attachments=out.attachments, sender_name=(club.name if club else None) or None,
    )
    return {"sent": True, "to": to, "subject": out.subject}


# --- audiences ------------------------------------------------------------------


def audience_query(event_id: int, audience: EmailAudience):
    # An address that bounced is left alone until it is corrected, and one
    # that bounced or complained anywhere on the platform is left alone too.
    q = deliveries._active(event_id).where(
        EventRegistrationsLink.email_bounced_at.is_(None),
        suppression.not_suppressed(EventRegistrationsLink.email),
    )
    if audience.who == "CHECKED_IN":
        q = q.where(EventRegistrationsLink.is_attended == True)
    elif audience.who == "NOT_CHECKED_IN":
        q = q.where(EventRegistrationsLink.is_attended == False)
    if audience.roles:
        try:
            wanted = [RegistrationRole(r) for r in audience.roles]
        except ValueError:
            raise CustomHTTPException(422, message="Pick roles from attendee, speaker and staff.", errors={"fields": {"roles": "unknown role"}})
        q = q.where(EventRegistrationsLink.role.in_(wanted))
    return q


async def audience_count(session, event: Events, audience: EmailAudience) -> dict:
    """Counted by the database: it used to load every guest of the audience to count them."""
    no_email = or_(EventRegistrationsLink.email.is_(None), EventRegistrationsLink.email == "")
    total, without = (
        await session.execute(audience_query(event.id, audience).with_only_columns(func.count(), func.count().filter(no_email)))
    ).one()
    return {"recipients": (total or 0) - (without or 0), "without_email": without or 0}


# --- messages -------------------------------------------------------------------


async def existing_message(session, event: Events, client_ref) -> Optional[Messages]:
    return await session.scalar(select(Messages).where(Messages.event_id == event.id, Messages.client_ref == client_ref))


async def start_message(
    session,
    event: Events,
    *,
    kind: MessageKind,
    subject: str,
    design: EmailDesign,
    attachments: EmailAttachments,
    audience: EmailAudience,
    template: Optional[EmailTemplates] = None,
    scheduled: bool = False,
    body: str = "",
    client_ref=None,
    created_by=None,
) -> Messages:
    """The message and one pending delivery per guest. Nothing is sent here;
    `run_message` does that after the response, or a job the timer starts.

    Sent once however it is asked: a `client_ref` the event has seen returns
    that message (two at once: the unique index decides, and the second
    returns the first's), and a template sent again while its last manual
    send is still going returns that one -- the double click of a page that
    sends no client_ref."""
    if client_ref is not None:
        existing = await existing_message(session, event, client_ref)
        if existing is not None:
            return existing
    if template is not None and not scheduled and client_ref is None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :key)"),
            {"ns": SEND_LOCK, "key": zlib.crc32(str(template.id).encode()) - 2**31},
        )
        recent = await session.scalar(
            select(Messages)
            .where(
                Messages.template_id == template.id, Messages.scheduled == False,  # noqa: E712
                Messages.status == MessageStatus.SENDING, Messages.created_at >= datetime.now(timezone.utc) - DOUBLE_CLICK,
            )
            .order_by(Messages.created_at.desc())
            .limit(1)
        )
        if recent is not None:
            await session.commit()
            return recent
    now = datetime.now(timezone.utc)
    msg = Messages(
        id=uuid.uuid4(), event_id=event.id, client_ref=client_ref, kind=kind, template_id=template.id if template else None,
        design=design.model_dump(by_alias=True), attachments=attachments.model_dump(by_alias=True), scheduled=scheduled,
        subject=subject, body=body, audience=audience.who, roles=list(audience.roles), channels=["email"],
        status=MessageStatus.SENDING, recipients=0, sent_count=0, failed_count=0, skipped_count=0, created_by=created_by,
        created_at=now, updated_at=now,
    )
    session.add(msg)
    try:
        await session.flush()
        # One statement for the whole audience, rather than an object a guest.
        table = MessageDeliveries.__table__
        rows = audience_query(event.id, audience).with_only_columns(
            func.gen_random_uuid(), literal(msg.id), EventRegistrationsLink.id,
            cast(literal(DeliveryChannel.email.name), table.c.channel.type), cast(literal(DeliveryStatus.pending.name), table.c.status.type),
            literal(0), literal(now, TZAwareDateTime()), literal(now, TZAwareDateTime()),
        )
        inserted = await session.execute(
            pg_insert(table).from_select(
                ["id", "message_id", "registration_id", "channel", "status", "attempts", "created_at", "updated_at"], rows,
            )
        )
        msg.recipients = inserted.rowcount or 0
        await session.commit()
    except IntegrityError:
        # The same client_ref, committed a moment ago by the other click.
        await session.rollback()
        existing = await existing_message(session, event, client_ref) if client_ref is not None else None
        if existing is None:
            raise
        return existing
    return msg


async def send_template(session, event: Events, tpl: EmailTemplates, *, scheduled: bool = False, created_by=None, client_ref=None) -> Messages:
    if tpl.stage is EmailStage.INVITE:
        raise CustomHTTPException(
            422, message="The invite goes out with the tickets. Use Send tickets on the attendees page."
        )
    fields = fields_of(tpl)
    return await start_message(
        session, event, kind=MessageKind.TEMPLATE, subject=fields.subject, design=fields.design, attachments=fields.attachments,
        audience=fields.audience, template=tpl, scheduled=scheduled, created_by=created_by, client_ref=client_ref,
    )


def _paragraphs(body: str) -> list[dict]:
    chunks = [c.strip() for c in body.replace("\r\n", "\n").split("\n\n") if c.strip()]
    return [{"kind": "paragraph", "id": f"p{i + 1}", "text": c[:2000]} for i, c in enumerate(chunks)] or [{"kind": "paragraph", "id": "p1", "text": body.strip()[:2000]}]


async def design_for_broadcast(session, event: Events, body: str, design: Optional[EmailDesign]) -> EmailDesign:
    """A typed message wears the event's invite theme, so it looks like the
    rest of the club's mail."""
    if design is not None and design.blocks:
        return design
    templates = await ensure_event_templates(session, event)
    invite = next((t for t in templates if t.stage is EmailStage.INVITE), None)
    theme = fields_of(invite).design.theme.model_dump(by_alias=True) if invite is not None else {}
    return EmailDesign.model_validate({"theme": theme, "blocks": _paragraphs(body)})


async def send_broadcast(session, event: Events, *, subject: str, body: str, design: Optional[EmailDesign], attachments: EmailAttachments, audience: EmailAudience, client_ref=None, created_by=None) -> Messages:
    if not body.strip() and not (design is not None and design.blocks):
        raise CustomHTTPException(422, message="Write something to send.", errors={"fields": {"body": "Write something to send."}})
    full = await design_for_broadcast(session, event, body, design)
    problems = design_problems(subject, full)
    if problems:
        raise CustomHTTPException(422, message=problems[0], errors={"fields": {"body": problems}})
    return await start_message(
        session, event, kind=MessageKind.BROADCAST, subject=subject, design=full, attachments=attachments, audience=audience,
        body=body, client_ref=client_ref, created_by=created_by,
    )


@dataclass
class _MessageRun:
    """What every guest's copy of one message shares, read once."""

    message_id: uuid.UUID
    event_id: int
    event_name: str
    club_id: Optional[int]
    org_name: str
    subject: str
    design: EmailDesign
    attachments: EmailAttachments
    brand: str
    logo_url: Optional[str]
    assets: dict = field(default_factory=dict)


@dataclass
class _MessageClaim:
    id: uuid.UUID
    claimed_at: datetime
    attempts: int


async def _message_run(db: pipeline.Db, message_id) -> Optional[_MessageRun]:
    async with db.tx() as s:
        msg = await s.scalar(select(Messages).options(joinedload(Messages.event).joinedload(Events.club)).where(Messages.id == message_id))
        if msg is None:
            return None
        event = msg.event
        club = getattr(event, "club", None)
        return _MessageRun(
            message_id=msg.id, event_id=event.id, event_name=event.name, club_id=event.club_id, org_name=(club.name if club else "") or "",
            subject=msg.subject or "", design=EmailDesign.model_validate(msg.design or {}),
            attachments=EmailAttachments.model_validate(msg.attachments or {}), brand=await brand_primary(s, event), logo_url=logo_url_of(event),
        )


async def _claim_message_rows(db: pipeline.Db, message_id, n: int) -> list[_MessageClaim]:
    """Up to n of the message's pending, due rows, each taken by this worker alone."""
    at = pipeline.now()
    picked = (
        select(MessageDeliveries.id)
        .where(
            MessageDeliveries.message_id == message_id, MessageDeliveries.status == DeliveryStatus.pending,
            or_(MessageDeliveries.next_attempt_at.is_(None), MessageDeliveries.next_attempt_at <= at),
        )
        .limit(n)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            update(MessageDeliveries)
            .where(MessageDeliveries.id.in_(picked), MessageDeliveries.status == DeliveryStatus.pending)
            .values(status=DeliveryStatus.sending, claimed_at=at, attempts=MessageDeliveries.attempts + 1, updated_at=at)
            .returning(MessageDeliveries.id, MessageDeliveries.attempts)
            .execution_options(synchronize_session=False)
        )
    ).all()
    return [_MessageClaim(id=r[0], claimed_at=at, attempts=r[1]) for r in rows]


async def _release_message_rows(db: pipeline.Db, claims: list) -> None:
    if not claims:
        return
    await db.execute(
        update(MessageDeliveries)
        .where(MessageDeliveries.id.in_([c.id for c in claims]), MessageDeliveries.status == DeliveryStatus.sending)
        .values(status=DeliveryStatus.pending, claimed_at=None, attempts=MessageDeliveries.attempts - 1, updated_at=pipeline.now())
        .execution_options(synchronize_session=False)
    )


async def _record_message_row(db: pipeline.Db, run: _MessageRun, c: _MessageClaim, values: dict, *, attempt: Optional[dict] = None, dead: bool = False) -> None:
    async with db.tx() as s:
        res = await s.execute(
            update(MessageDeliveries)
            .where(MessageDeliveries.id == c.id, MessageDeliveries.status == DeliveryStatus.sending, MessageDeliveries.claimed_at == c.claimed_at)
            .values(claimed_at=None, updated_at=pipeline.now(), **values)
            .execution_options(synchronize_session=False)
        )
        if not res.rowcount:
            logger.warning("message delivery %s: the result came after its claim was given up; recorded the attempt only", c.id)
        if attempt and attempt.get("provider_id"):
            await s.execute(
                pg_insert(DeliveryAttempts.__table__)
                .values(id=uuid.uuid4(), message_delivery_id=c.id, created_at=pipeline.now(), **attempt)
                .on_conflict_do_nothing(index_elements=["provider_id"])
            )
        if dead and res.rowcount:
            await pipeline.alert_dead(s, event_id=run.event_id, club_id=run.club_id, event_name=run.event_name, what="message", error=values.get("error") or "")


async def _handle_message_row(db: pipeline.Db, run: _MessageRun, c: _MessageClaim) -> None:
    """Worker: one guest. Records whatever happened."""
    async with db.tx() as s:
        d = await s.scalar(
            select(MessageDeliveries)
            .options(joinedload(MessageDeliveries.registration).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user)))
            .where(MessageDeliveries.id == c.id)
        )
        if d is None:
            return
        reg = d.registration
        skip = None
        if not reg.email:
            skip = "no email address"
        elif reg.email_bounced_at is not None:
            skip = "that address bounced — correct it to send again"
        else:
            # It may have bounced elsewhere since the message was queued.
            found = (await suppression.reasons(s, [reg.email])).get(suppression.normal(reg.email))
            skip = suppression.why(found) if found is not None else None
    if skip is not None:
        await _record_message_row(db, run, c, dict(status=DeliveryStatus.skipped, error=skip, next_attempt_at=None))
        return
    try:
        out = await compose(db, reg.event, reg, run.subject, run.design, run.attachments, brand=run.brand, logo_url=run.logo_url, cache=run.assets)
        response = await pipeline.send_with_backoff(
            email_mod.send_email, recipients=[reg.email], subject=out.subject, text=out.text, html=out.html,
            attachments=out.attachments, sender_name=run.org_name or None, idempotency_key=f"message-{c.id}", check_suppression=False,
        )
    except transport.MailDeferred as exc:
        error = (str(exc) or exc.__class__.__name__)[:300]
        if c.attempts >= pipeline.max_tries():
            await _record_message_row(db, run, c, dict(status=DeliveryStatus.dead, error=f"gave up after {c.attempts} tries: {error}"[:400], next_attempt_at=None), dead=True)
        else:
            await _record_message_row(db, run, c, dict(status=DeliveryStatus.pending, error=f"will try again: {error}"[:400], next_attempt_at=pipeline.now() + pipeline.backoff(c.attempts)))
        logger.warning("message %s to %s deferred (try %d): %s", run.message_id, reg.id, c.attempts, error)
        return
    except Exception as exc:
        error = (str(exc) or exc.__class__.__name__)[:400]
        logger.warning("message %s to %s failed: %s", run.message_id, reg.id, error)
        await _record_message_row(db, run, c, dict(status=DeliveryStatus.failed, error=error, next_attempt_at=None))
        return
    response = response or {}
    await _record_message_row(
        db, run, c,
        dict(status=DeliveryStatus.sent, provider_id=response.get("MessageId"), sent_at=pipeline.now(), error=None, next_attempt_at=None),
        attempt={"provider": response.get("Provider"), "provider_id": response.get("MessageId"), "address": suppression.normal(reg.email)},
    )


async def finish_message(db: pipeline.Db, message_id) -> Optional[Messages]:
    """The message's counts as they stand, and SENT once nothing is left to send."""
    async with db.tx() as s:
        counts = dict(
            (await s.execute(select(MessageDeliveries.status, func.count()).where(MessageDeliveries.message_id == message_id).group_by(MessageDeliveries.status))).all()
        )
        values = dict(
            sent_count=counts.get(DeliveryStatus.sent, 0),
            failed_count=counts.get(DeliveryStatus.failed, 0) + counts.get(DeliveryStatus.dead, 0),
            skipped_count=counts.get(DeliveryStatus.skipped, 0),
        )
        if not any(counts.get(st, 0) for st in UNFINISHED):
            values["status"] = MessageStatus.SENT
        await s.execute(update(Messages).where(Messages.id == message_id).values(**values).execution_options(synchronize_session=False))
        return await s.get(Messages, message_id, populate_existing=True)


#: How often a long send writes its counts back, for the messages page.
PROGRESS_EVERY = 3.0


async def _run_message(message_id, session=None) -> Optional[Messages]:
    db = deliveries._db(session)
    run = await _message_run(db, message_id)
    if run is None:
        return None
    last = time.monotonic()

    async def claim(n: int):
        nonlocal last
        if time.monotonic() - last >= PROGRESS_EVERY:
            last = time.monotonic()
            await finish_message(db, message_id)
        return await _claim_message_rows(db, message_id, n)

    await pipeline.pump(claim, lambda c: _handle_message_row(db, run, c), release=lambda claims: _release_message_rows(db, claims))
    return await finish_message(db, message_id)


async def process_message(session, message_id) -> Optional[Messages]:
    """Send what is left of a message now, on the caller's session."""
    return await _run_message(message_id, session=session)


async def run_message(message_id) -> None:
    """The background entry point for one message. A message already being
    sent in this process is waited for, not sent again."""
    await pipeline.run(("message", message_id), lambda: _run_message(message_id))


def start_job(message_id) -> None:
    """Send the message in the background, without waiting for it."""
    pipeline.spawn(("message", message_id), lambda: _run_message(message_id))


async def resume_messages(session, now: Optional[datetime] = None) -> dict:
    """The minute sweep's look at messages: put back the claims of workers
    that died, give up on rows tried too often, finish what was cut off --
    a timed reminder stopped by a deploy among them -- and leave alone what
    is now too late to be worth sending. Starts jobs; sends nothing here."""
    now = now or pipeline.now()
    db = deliveries._db(session)
    stale = (
        await db.execute(
            update(MessageDeliveries)
            .where(MessageDeliveries.status == DeliveryStatus.sending, MessageDeliveries.claimed_at < now - pipeline.STALE_CLAIM)
            .values(status=DeliveryStatus.pending, claimed_at=None, next_attempt_at=now, updated_at=now)
            .returning(MessageDeliveries.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    gave_up = (
        await db.execute(
            update(MessageDeliveries)
            .where(MessageDeliveries.status == DeliveryStatus.pending, MessageDeliveries.attempts >= pipeline.max_tries())
            .values(status=DeliveryStatus.dead, error=f"gave up after {pipeline.max_tries()} tries", updated_at=now)
            .returning(MessageDeliveries.message_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    if gave_up:
        async with db.tx() as s:
            events = (await s.execute(select(Events).join(Messages, Messages.event_id == Events.id).where(Messages.id.in_({m for (m,) in gave_up})))).scalars().unique().all()
            for event in events:
                await pipeline.alert_dead(s, event_id=event.id, club_id=event.club_id, event_name=event.name, what="message", error="its worker stopped every time it was tried")
    unfinished = (await db.execute(select(Messages.id, Messages.created_at).where(Messages.status == MessageStatus.SENDING))).all()
    started = late = 0
    for message_id, created_at in unfinished:
        if created_at < now - RESUME_WINDOW:
            await db.execute(
                update(MessageDeliveries)
                .where(MessageDeliveries.message_id == message_id, MessageDeliveries.status.in_(UNFINISHED))
                .values(status=DeliveryStatus.failed, error="not sent: too late to send by the time it could be — send it again if it still matters", claimed_at=None, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await finish_message(db, message_id)
            late += 1
        elif not pipeline.running(("message", message_id)):
            start_job(message_id)
            started += 1
    return {"stale": len(stale), "dead": len(gave_up), "started": started, "late": late}


async def list_messages(session, event: Events, *, page: int = 1, page_size: int = 20) -> dict:
    base = select(Messages).where(Messages.event_id == event.id, Messages.kind != MessageKind.TEST)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    rows = list(await session.scalars(base.order_by(Messages.created_at.desc()).limit(page_size).offset((page - 1) * page_size)))
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


async def message_of(session, event: Events, message_id) -> Messages:
    msg = await session.scalar(select(Messages).where(Messages.id == message_id, Messages.event_id == event.id))
    if msg is None:
        raise CustomHTTPException(404, message="Message not found")
    return msg


async def message_deliveries(session, msg: Messages, *, problem: bool = False, search: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict:
    base = (
        select(MessageDeliveries, EventRegistrationsLink)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == MessageDeliveries.registration_id)
        .where(MessageDeliveries.message_id == msg.id)
    )
    if problem:
        base = base.where(MessageDeliveries.status.in_((DeliveryStatus.failed, DeliveryStatus.dead, DeliveryStatus.skipped)))
    if search:
        pat = f"%{search}%"
        base = base.where(EventRegistrationsLink.full_name.ilike(pat) | EventRegistrationsLink.email.ilike(pat) | EventRegistrationsLink.ticket_id.ilike(pat))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = (await session.execute(base.order_by(EventRegistrationsLink.full_name).limit(page_size).offset((page - 1) * page_size))).all()
    items = [
        {
            "registration_id": reg.id, "name": reg.full_name or "", "email": reg.email or None, "ticket_id": reg.ticket_id,
            "channel": d.channel.value, "status": deliveries.shown(d.status).value, "error": d.error, "sent_at": d.sent_at,
        }
        for d, reg in rows
    ]
    return {"deliveries": items, "total": total, "page": page, "page_size": page_size}


def message_out(msg: Messages, created_by_name: Optional[str] = None) -> dict:
    return {
        "id": msg.id, "kind": msg.kind.value, "template_id": msg.template_id, "scheduled": msg.scheduled, "subject": msg.subject,
        "body": msg.body or "", "audience": msg.audience, "roles": msg.roles or [], "channels": msg.channels or [], "status": msg.status.value,
        "recipients": msg.recipients, "sent_count": msg.sent_count, "failed_count": msg.failed_count, "skipped_count": msg.skipped_count,
        "created_at": msg.created_at, "created_by_name": created_by_name,
    }


def template_out(tpl: EmailTemplates) -> dict:
    return {
        **fields_of(tpl).model_dump(by_alias=True), "id": tpl.id, "clubId": tpl.club_id, "eventId": tpl.event_id, "stage": tpl.stage.value,
        "version": tpl.version or 1, "sourceId": tpl.source_id, "createdAt": tpl.created_at, "updatedAt": tpl.updated_at,
    }


# --- the ticket email -------------------------------------------------------------


async def invite_for(session, event: Events) -> Optional[dict]:
    """The event's designed ticket email as a send needs it -- subject,
    design, files, brand -- or None when the club switched it off (the plain
    ticket email goes then). Every event has one, copied from the club's
    library the first time it is needed. The ticket itself is always
    attached: that is what the email is for."""
    if event.club_id is None:
        return None
    tpl = await session.scalar(select(EmailTemplates).where(EmailTemplates.event_id == event.id, EmailTemplates.stage == EmailStage.INVITE))
    if tpl is None:
        templates = await ensure_event_templates(session, event)
        tpl = next((t for t in templates if t.stage is EmailStage.INVITE), None)
    if tpl is None or not tpl.enabled:
        return None
    fields = fields_of(tpl)
    return {
        "subject": fields.subject,
        "design": fields.design,
        "attachments": EmailAttachments(ticket_pdf=True, certificate_pdf=fields.attachments.certificate_pdf),
        "brand": await brand_primary(session, event),
        "logo_url": logo_url_of(event),
    }


async def compose_invite(session, event: Events, reg: EventRegistrationsLink, invite: dict, *, cache: Optional[dict] = None) -> Outgoing:
    return await compose(
        session, event, reg, invite["subject"], invite["design"], invite["attachments"], brand=invite["brand"], logo_url=invite["logo_url"], cache=cache,
    )


async def invite_email(session, reg: EventRegistrationsLink, event: Events) -> Optional[Outgoing]:
    """The designed ticket email for one guest, or None when it is switched off."""
    invite = await invite_for(session, event)
    return None if invite is None else await compose_invite(session, event, reg, invite)


# --- timers -----------------------------------------------------------------------


def due_query(now: datetime):
    """The enabled timed emails whose instant fell in the last SWEEP_GRACE,
    on events not deleted. An index range read of email_templates.due_at
    (ix_email_templates_due): the rows that can be due now and no others,
    however many events have journey emails. It used to read every timed
    email ever enabled, past events' included, and decide in Python: 72 s a
    minute at 40,000 events, on a web worker's event loop."""
    return (
        select(EmailTemplates, Events)
        .join(Events, Events.id == EmailTemplates.event_id)
        .options(joinedload(Events.club))
        .where(
            EmailTemplates.enabled == True,  # noqa: E712
            EmailTemplates.due_at.isnot(None),
            EmailTemplates.due_at >= now - SWEEP_GRACE,
            EmailTemplates.due_at <= now,
            Events.is_deleted == False,  # noqa: E712
        )
        .order_by(EmailTemplates.due_at)
    )


async def sweep(session, now: Optional[datetime] = None) -> int:
    """Queue every timed journey email that has come due and not yet gone,
    and start its job. Once per template, ever: the message row is the
    record -- and a message cut off half-sent is finished by the resumer,
    not sent again. Sends nothing itself, so one big reminder never holds up
    the sweep (or anyone else's reminder) while it goes. Returns how many it queued."""
    now = now or datetime.now(timezone.utc)
    queued: list = []
    # The session is the sweep's until it is done with it; the jobs start after.
    async with pipeline.session_lock(session):
        rows = (await session.execute(due_query(now))).unique().all()
        for tpl, event in rows:
            try:
                # The stored instant chose the row; the rule itself still decides.
                due = due_at(event, fields_of(tpl).trigger)
                if due is None or due > now or due < now - SWEEP_GRACE:
                    continue
                already = await session.scalar(select(func.count()).select_from(Messages).where(Messages.template_id == tpl.id, Messages.scheduled == True))  # noqa: E712
                if already:
                    continue
                msg = await send_template(session, event, tpl, scheduled=True)
                queued.append(msg.id)
            except Exception:  # noqa: BLE001
                # One email that cannot go must not hold up the others.
                logger.exception("timed email %s could not be queued", tpl.id)
                await session.rollback()
    for message_id in queued:
        start_job(message_id)
    return len(queued)


async def resume(session, now: Optional[datetime] = None) -> dict:
    """Everything left half-done by a process that stopped: deliveries,
    messages, and the after-commit mails that must go. Each part on its own,
    so one that fails does not keep the others from running."""
    out: dict = {}
    for name, part in (
        ("deliveries", lambda: deliveries.resume(session, now)),
        ("messages", lambda: resume_messages(session, now)),
        ("after_commit", lambda: after_commit.resume(session, now)),
    ):
        try:
            out[name] = await part()
        except Exception:  # noqa: BLE001
            logger.exception("resuming %s failed", name)
            await session.rollback()
    return out


async def sweep_loop(interval: int = 60) -> None:
    """Runs for the life of the process. One worker at a time queues the
    timed emails and resumes what was left half-done: the one holding the
    sweep's advisory lock, taken on a connection of its own (app.db.locks).
    The sending itself happens in jobs, beside the loop."""
    await asyncio.sleep(5)
    while True:
        try:
            async with deliveries.session_scope() as session:
                async with singleton(session, SWEEP_LOCK) as mine:
                    if mine:
                        n = await sweep(session)
                        if n:
                            logger.info("email sweep queued %d timed email(s)", n)
                        resumed = await resume(session)
                        if any(v for part in resumed.values() for v in (part.values() if isinstance(part, dict) else [part])):
                            logger.info("email sweep resumed %s", resumed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("email sweep failed")
        await asyncio.sleep(interval)


def context(event, *, brand: str) -> dict:
    from app.core.layout.tokens import TICKET_TOKENS

    return {
        "tokens": [*TICKET_TOKENS, *EMAIL_ONLY_TOKENS],
        "sample_values": sample_values(event),
        "brand_primary": brand,
        "logo_url": logo_url_of(event),
        "event_icon_url": poster_url(event),
        "stages": EMAIL_STAGE_META,
        "blocks": EMAIL_BLOCK_META,
    }
