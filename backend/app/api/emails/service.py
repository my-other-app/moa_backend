"""Designed emails: the club's library, each event's copies, and every
message sent from one.

An event starts with the four journey emails copied from the club's
library (itself seeded from the starters the first time it is asked for),
so a club that designs once sends the same emails at every event. A send
freezes the design into a message with one delivery per guest; the worker
then renders and sends each one and records what happened.
"""
import asyncio
import base64
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import joinedload

from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.deliveries.models import DeliveryChannel, DeliveryStatus
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
from app.core.email import email as email_mod
from app.core.mail import links
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
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

STAGE_ORDER = {EmailStage(s): i for i, s in enumerate(JOURNEY_STAGES)}
STAGE_ORDER[EmailStage.CUSTOM] = len(JOURNEY_STAGES)
# Keeps a timer from sending an old event's reminder on the day it is deployed.
SWEEP_GRACE = timedelta(days=2)
SWEEP_LOCK = 773101


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


async def ensure_library(session, club_id: int) -> list[EmailTemplates]:
    """The club's library, with the four journey emails present."""
    rows = list(await session.scalars(select(EmailTemplates).where(EmailTemplates.club_id == club_id, EmailTemplates.event_id.is_(None))))
    have = {t.stage for t in rows}
    added = False
    for stage in JOURNEY_STAGES:
        if EmailStage(stage) not in have:
            tpl = _new(club_id, None, EmailStage(stage), email_starter(stage))
            session.add(tpl)
            rows.append(tpl)
            added = True
    if added:
        await session.flush()
    return _sorted(rows)


async def ensure_event_templates(session, event: Events) -> list[EmailTemplates]:
    """The event's emails, with every journey stage present: copied from the
    club's library the first time, so a club's design carries over."""
    if event.club_id is None:
        raise CustomHTTPException(422, message="This event has no club, so it has no emails to send from.")
    rows = list(await session.scalars(select(EmailTemplates).where(EmailTemplates.event_id == event.id)))
    have = {t.stage for t in rows if t.stage is not EmailStage.CUSTOM}
    missing = [EmailStage(s) for s in JOURNEY_STAGES if EmailStage(s) not in have]
    if missing:
        library = {t.stage: t for t in await ensure_library(session, event.club_id)}
        for stage in missing:
            src = library[stage]
            tpl = _new(event.club_id, event.id, stage, fields_of(src), source_id=src.id)
            session.add(tpl)
            rows.append(tpl)
        await session.commit()
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
    await session.commit()
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
    return layout_colors(style or {})["primary"]


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
) -> Outgoing:
    """The email as it will be sent: pictures inline by Content-ID so the
    message carries them, the guest's files attached when asked for."""
    values = values if values is not None else (guest_values(event, reg) if reg is not None else sample_values(event))
    parts: list[dict] = []
    images: dict = {}

    def inline(data: Optional[bytes], cid: str) -> Optional[str]:
        if not data:
            return None
        parts.append({"filename": f"{cid}.{image_ext(data)}", "content": data, "content_id": cid})
        return f"cid:{cid}"

    blocks = _image_blocks(design)
    for b in blocks:
        if b.source == "upload" and b.image_key and b.image_key not in images:
            src = inline(objects.get_bytes(b.image_key), f"img-{len(images) + 1}")
            if src:
                images[b.image_key] = src
    logo = inline(fetch_bytes(logo_url), "org-logo") if design.theme.logo == "org_logo" and logo_url else None
    icon = inline(fetch_bytes(poster_url(event) or ""), "event-icon") if any(b.source == "event_icon" for b in blocks) else None
    qr = None
    if uses_qr(design):
        qr = inline(base64.b64decode(qr_png_base64(reg.ticket_id if reg is not None else "SAMPLE")), "ticket-qr")
    out = render_email(RenderInput(subject=subject, design=design, values=values, brand_primary=brand, logo_url=logo, event_icon_url=icon, images=images, qr_url=qr))
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
    await asyncio.to_thread(
        email_mod.send_email, recipients=[to], subject=f"[Test] {out.subject}", text=out.text, html=out.html,
        attachments=out.attachments, sender_name=(club.name if club else None) or None,
    )
    return {"sent": True, "to": to, "subject": out.subject}


# --- audiences ------------------------------------------------------------------


def audience_query(event_id: int, audience: EmailAudience):
    # An address that bounced is left alone until it is corrected.
    q = deliveries._active(event_id).where(EventRegistrationsLink.email_bounced_at.is_(None))
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
    regs = list(await session.scalars(audience_query(event.id, audience)))
    without = sum(1 for r in regs if not r.email)
    return {"recipients": len(regs) - without, "without_email": without}


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
    `run_message` does that after the response (or inline, for the timer)."""
    if client_ref is not None:
        existing = await session.scalar(select(Messages).where(Messages.event_id == event.id, Messages.client_ref == client_ref))
        if existing is not None:
            return existing
    regs = list(await session.scalars(audience_query(event.id, audience).order_by(EventRegistrationsLink.created_at)))
    msg = Messages(
        id=uuid.uuid4(), event_id=event.id, client_ref=client_ref, kind=kind, template_id=template.id if template else None,
        design=design.model_dump(by_alias=True), attachments=attachments.model_dump(by_alias=True), scheduled=scheduled,
        subject=subject, body=body, audience=audience.who, roles=list(audience.roles), channels=["email"],
        status=MessageStatus.SENDING, recipients=len(regs), sent_count=0, failed_count=0, skipped_count=0, created_by=created_by,
    )
    session.add(msg)
    for reg in regs:
        session.add(MessageDeliveries(id=uuid.uuid4(), message_id=msg.id, registration_id=reg.id, channel=DeliveryChannel.email, status=DeliveryStatus.pending))
    await session.commit()
    return msg


async def send_template(session, event: Events, tpl: EmailTemplates, *, scheduled: bool = False, created_by=None) -> Messages:
    if tpl.stage is EmailStage.INVITE:
        raise CustomHTTPException(
            422, message="The invite goes out with the tickets. Use Send tickets on the attendees page."
        )
    fields = fields_of(tpl)
    return await start_message(
        session, event, kind=MessageKind.TEMPLATE, subject=fields.subject, design=fields.design, attachments=fields.attachments,
        audience=fields.audience, template=tpl, scheduled=scheduled, created_by=created_by,
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


async def deliver_message(session, msg: Messages, d: MessageDeliveries, design: EmailDesign, attachments: EmailAttachments, *, brand: str, logo_url: Optional[str]) -> None:
    """Worker: one guest. Records whatever happened."""
    reg = d.registration
    event = reg.event
    club = getattr(event, "club", None)
    if not reg.email:
        d.status = DeliveryStatus.skipped
        d.error = "no email address"
    elif reg.email_bounced_at is not None:
        d.status = DeliveryStatus.skipped
        d.error = "that address bounced — correct it to send again"
    else:
        try:
            out = await compose(session, event, reg, msg.subject or "", design, attachments, brand=brand, logo_url=logo_url)
            response = await asyncio.to_thread(
                email_mod.send_email, recipients=[reg.email], subject=out.subject, text=out.text, html=out.html,
                attachments=out.attachments, sender_name=(club.name if club else None) or None,
            )
            d.status = DeliveryStatus.sent
            d.provider_id = (response or {}).get("MessageId")
            d.sent_at = datetime.now(timezone.utc)
            d.error = None
        except Exception as exc:
            d.status = DeliveryStatus.failed
            d.error = (str(exc) or exc.__class__.__name__)[:400]
            logger.warning("message %s to %s failed: %s", msg.id, reg.id, d.error)
    await session.commit()


async def process_message(session, message_id) -> Optional[Messages]:
    msg = await session.scalar(select(Messages).options(joinedload(Messages.event).joinedload(Events.club)).where(Messages.id == message_id))
    if msg is None:
        return None
    event = msg.event
    design = EmailDesign.model_validate(msg.design or {})
    attachments = EmailAttachments.model_validate(msg.attachments or {})
    brand = await brand_primary(session, event)
    logo_url = logo_url_of(event)
    rows = list(
        await session.scalars(
            select(MessageDeliveries)
            .options(joinedload(MessageDeliveries.registration).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user)))
            .where(MessageDeliveries.message_id == msg.id, MessageDeliveries.status == DeliveryStatus.pending)
        )
    )
    for d in rows:
        try:
            await deliver_message(session, msg, d, design, attachments, brand=brand, logo_url=logo_url)
        except Exception:
            logger.exception("message delivery %s could not be processed", d.id)
            await session.rollback()
    counts = dict(
        (await session.execute(select(MessageDeliveries.status, func.count()).where(MessageDeliveries.message_id == msg.id).group_by(MessageDeliveries.status))).all()
    )
    msg.sent_count = counts.get(DeliveryStatus.sent, 0)
    msg.failed_count = counts.get(DeliveryStatus.failed, 0)
    msg.skipped_count = counts.get(DeliveryStatus.skipped, 0)
    msg.status = MessageStatus.SENT
    await session.commit()
    return msg


async def run_message(message_id) -> None:
    """The background entry point for one message."""
    async with deliveries.session_scope() as session:
        try:
            await process_message(session, message_id)
        except Exception:
            logger.exception("message %s could not be processed", message_id)
            await session.rollback()


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
        base = base.where(MessageDeliveries.status.in_((DeliveryStatus.failed, DeliveryStatus.skipped)))
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
            "channel": d.channel.value, "status": d.status.value, "error": d.error, "sent_at": d.sent_at,
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


async def invite_email(session, reg: EventRegistrationsLink, event: Events) -> Optional[Outgoing]:
    """The designed ticket email. Every event has one -- copied from the
    club's library the first time it is needed -- unless the club switched it
    off, in which case the plain ticket email goes. The ticket itself is
    always attached: that is what the email is for."""
    if event.club_id is None:
        return None
    tpl = await session.scalar(select(EmailTemplates).where(EmailTemplates.event_id == event.id, EmailTemplates.stage == EmailStage.INVITE))
    if tpl is None:
        templates = await ensure_event_templates(session, event)
        tpl = next((t for t in templates if t.stage is EmailStage.INVITE), None)
    if tpl is None or not tpl.enabled:
        return None
    fields = fields_of(tpl)
    return await compose(
        session, event, reg, fields.subject, fields.design, EmailAttachments(ticket_pdf=True, certificate_pdf=fields.attachments.certificate_pdf),
        brand=await brand_primary(session, event), logo_url=logo_url_of(event),
    )


# --- timers -----------------------------------------------------------------------


async def sweep(session, now: Optional[datetime] = None) -> int:
    """Send every timed journey email that has come due and not yet gone.
    Once per template, ever: the message row is the record."""
    now = now or datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(EmailTemplates, Events)
            .join(Events, Events.id == EmailTemplates.event_id)
            .options(joinedload(Events.club))
            .where(
                EmailTemplates.enabled == True,
                EmailTemplates.event_id.isnot(None),
                Events.is_deleted == False,
                EmailTemplates.trigger["mode"].as_string().in_(["BEFORE_START", "AFTER_END"]),
            )
        )
    ).unique().all()
    sent = 0
    for tpl, event in rows:
        due = due_at(event, fields_of(tpl).trigger)
        if due is None or due > now or due < now - SWEEP_GRACE:
            continue
        already = await session.scalar(select(func.count()).select_from(Messages).where(Messages.template_id == tpl.id, Messages.scheduled == True))
        if already:
            continue
        msg = await send_template(session, event, tpl, scheduled=True)
        await process_message(session, msg.id)
        sent += 1
    return sent


async def sweep_loop(interval: int = 60) -> None:
    """Runs for the life of the process. One worker at a time does the
    sending, by taking a Postgres advisory lock for the sweep."""
    await asyncio.sleep(5)
    while True:
        try:
            async with deliveries.session_scope() as session:
                if await session.scalar(text(f"SELECT pg_try_advisory_lock({SWEEP_LOCK})")):
                    try:
                        n = await sweep(session)
                        if n:
                            logger.info("email sweep sent %d timed email(s)", n)
                    finally:
                        await session.execute(text(f"SELECT pg_advisory_unlock({SWEEP_LOCK})"))
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
