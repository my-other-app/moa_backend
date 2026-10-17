"""ID cards: the design, each guest's card, batches, and the rule for a
second card."""
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pypdfium2 as pdfium
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.api.checkins.gate_auth import GateActor
from app.api.events.documents import layout_values, render_layout_document, in_layout_mode, layout_colors
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.tickets import design_version_of, poster_url, role_of
from app.api.idcards.models import BadgeTemplates, PrintJobs, PrintSource, ReprintPolicy
from app.api.templates_audience import RegistrationRole
from app.api.templates_resolve import resolve_template
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import badge_starter
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PREPRINTED_LAYERS = ("background", "role_band", "org_logo", "event_name", "ticket_type")
PREPRINTED_META = {
    "background": {"label": "Card background", "note": "The base colour or full-bleed brand panel."},
    "role_band": {"label": "Role band", "note": "The coloured band behind the role."},
    "org_logo": {"label": "Club logo", "note": "The logo block at the top of the card."},
    "event_name": {"label": "Event name", "note": "The event line above the photo."},
    "ticket_type": {"label": "Role", "note": "Only if you buy separate blanks per role — VIP cards with “VIP” already on them."},
}
REPRINT_META = {
    "anyone": {"label": "Anyone", "note": "A second card prints from any door, whenever a guest asks."},
    "organiser": {"label": "The organiser only", "note": "A door prints each guest's first card; another needs someone running the event."},
    "never": {"label": "Never", "note": "One card per guest. The organiser can still override for the one that was lost."},
}


def badge_default_config() -> dict:
    """What an event starts from: the conference card, as a layered design."""
    s = badge_starter("classic")
    return {"accent_color": "#1db9a0", "secondary_text_color": "#aaaaaa", "layout": {**dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])), "version": 0}}


async def badge_style(session, event: Events, role: Optional[RegistrationRole]) -> dict:
    template = await resolve_template(session, BadgeTemplates, event_id=event.id, club_id=event.club_id, role=role)
    style = dict(template.config or {}) if template is not None else {}
    if not in_layout_mode(style):
        style = {**badge_default_config(), **{k: v for k, v in style.items() if k != "layout"}}
    return style


def may_print_again(policy: ReprintPolicy, printed_at, by_organiser: bool, override: bool = False) -> tuple[bool, str]:
    """The first card is never refused; the rule turns on `printed_at`."""
    if not printed_at or policy is ReprintPolicy.anyone:
        return True, ""
    if by_organiser and override:
        return True, ""
    if policy is ReprintPolicy.organiser and by_organiser:
        return True, ""
    return False, (
        "Their card has already printed, and this event does not reprint ID cards."
        if policy is ReprintPolicy.never
        else "Their card has already printed. On this event only someone running it can print another."
    )


def skip_preprinted_for(event: Events, style: dict) -> bool:
    layout = style.get("layout") or {}
    return bool(event.preprinted_carries) or any(l.get("preprinted") for l in layout.get("layers") or []) or bool((layout.get("canvas") or {}).get("templatePreprinted"))


def render_card(event: Events, reg: EventRegistrationsLink, style: dict, *, for_print: bool) -> bytes:
    values = layout_values(reg, event, "badge")
    return render_layout_document(
        style, values, reg.ticket_id, title=f"ID card {reg.ticket_id}", event_icon_url=poster_url(event),
        skip_preprinted=for_print and skip_preprinted_for(event, style), club=getattr(event, "club", None),
    )


async def registration_of(session, event: Events, registration_id) -> EventRegistrationsLink:
    reg = await session.scalar(
        select(EventRegistrationsLink)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))
        .where(EventRegistrationsLink.id == registration_id, EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)
    )
    if reg is None:
        raise CustomHTTPException(404, message="Registration not found")
    return reg


async def card_pdf(session, event: Events, registration_id, *, actor: GateActor, source: PrintSource, record: bool, override: bool = False, door: Optional[str] = None) -> bytes:
    """One guest's card. Recording it is what turns the reprint rule on
    for next time, so a preview (`record=False`) never counts."""
    # Every way a card is printed comes through here (the console, the
    # attendees page, a kiosk): badge printing is the club's plan's.
    from app.api.subscriptions import gates

    await gates.require_feature(session, event.club_id, "badges", actor=actor.user)
    reg = await registration_of(session, event, registration_id)
    if record:
        ok, why = may_print_again(event.reprint_policy or ReprintPolicy.anyone, reg.badge_printed_at, actor.full, override)
        if not ok:
            raise CustomHTTPException(403, message=why, error_code="REPRINT_REFUSED")
    style = await badge_style(session, event, role_of(reg))
    pdf = render_card(event, reg, style, for_print=record)
    if record:
        overrode = bool(reg.badge_printed_at) and override and actor.full and (event.reprint_policy or ReprintPolicy.anyone) is not ReprintPolicy.anyone
        session.add(PrintJobs(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, source=source, door_label=door or (actor.door.name if actor.door else None), requested_by=actor.user_id, cards=1, design_version=design_version_of(style), overrode=overrode))
        reg.badge_printed_at = datetime.now(timezone.utc)
        await session.commit()
    return pdf


def _active(event_id: int):
    return select(EventRegistrationsLink).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user)).where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.ticket_revoked_at.is_(None))


async def batch_targets(session, event: Events, *, scope: str, role: Optional[str]) -> list[EventRegistrationsLink]:
    q = _active(event.id)
    if scope == "not_printed" or (event.reprint_policy is ReprintPolicy.never and scope != "all"):
        q = q.where(EventRegistrationsLink.badge_printed_at.is_(None))
    if role:
        q = q.where(EventRegistrationsLink.role == RegistrationRole(role))
    return list(await session.scalars(q.order_by(EventRegistrationsLink.full_name)))


async def batch_pdf(session, event: Events, *, scope: str, role: Optional[str], record: bool, actor: GateActor) -> tuple[bytes, int]:
    """Every card in one PDF, a page each, so it prints on a card printer
    through the desk machine or at a print shop."""
    from app.api.subscriptions import gates

    await gates.require_feature(session, event.club_id, "badges", actor=actor.user)
    regs = await batch_targets(session, event, scope=scope, role=role)
    merged = pdfium.PdfDocument.new()
    styles: dict = {}
    for reg in regs:
        key = role_of(reg)
        if key not in styles:
            styles[key] = await badge_style(session, event, key)
        card = pdfium.PdfDocument(render_card(event, reg, styles[key], for_print=record))
        merged.import_pages(card, pages=None)
        card.close()
    if not regs:
        # An empty batch is still a PDF, so the download says so rather than failing.
        from app.core.layout.renderer import blank_page_pdf

        return blank_page_pdf("No cards to print."), 0
    buf = io.BytesIO()
    merged.save(buf)
    merged.close()
    if record:
        now = datetime.now(timezone.utc)
        version = max((design_version_of(s) for s in styles.values()), default=0)
        session.add(PrintJobs(id=uuid.uuid4(), event_id=event.id, registration_id=None, source=PrintSource.batch, requested_by=actor.user_id, cards=len(regs), design_version=version))
        for reg in regs:
            reg.badge_printed_at = now
        await session.commit()
    return buf.getvalue(), len(regs)


def settings_of(event: Events) -> dict:
    return {
        "reprint_policy": (event.reprint_policy or ReprintPolicy.anyone).value,
        "auto_print_badges": bool(event.auto_print_badges),
        "preprinted_carries": list(event.preprinted_carries or []),
    }


async def update_settings(session, event: Events, body) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if changes.get("reprint_policy") is not None:
        event.reprint_policy = ReprintPolicy(changes["reprint_policy"])
    if changes.get("auto_print_badges") is not None:
        event.auto_print_badges = bool(changes["auto_print_badges"])
    if changes.get("preprinted_carries") is not None:
        unknown = [x for x in changes["preprinted_carries"] if x not in PREPRINTED_LAYERS]
        if unknown:
            raise CustomHTTPException(422, message=f"Not a part of a card: {', '.join(unknown)}", errors={"fields": {"preprinted_carries": "unknown layer"}})
        event.preprinted_carries = list(dict.fromkeys(changes["preprinted_carries"]))
    await session.commit()
    return settings_of(event)


async def summary(session, event: Events) -> dict:
    regs = list(await session.scalars(select(EventRegistrationsLink.role, EventRegistrationsLink.badge_printed_at).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.ticket_revoked_at.is_(None))))
    rows = (await session.execute(select(EventRegistrationsLink.role, EventRegistrationsLink.badge_printed_at).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False, EventRegistrationsLink.ticket_revoked_at.is_(None)))).all()
    by_role: dict = {}
    for role, printed in rows:
        r = getattr(role, "value", role) or "attendee"
        d = by_role.setdefault(r, {"role": r, "registered": 0, "printed": 0})
        d["registered"] += 1
        d["printed"] += 1 if printed else 0
    style = await badge_style(session, event, None)
    jobs = await session.scalar(select(func.count()).select_from(PrintJobs).where(PrintJobs.event_id == event.id)) or 0
    del regs
    return {
        "registered": sum(d["registered"] for d in by_role.values()),
        "printed": sum(d["printed"] for d in by_role.values()),
        "by_role": sorted(by_role.values(), key=lambda d: d["role"]),
        "design_version": design_version_of(style),
        "jobs": jobs,
        "settings": settings_of(event),
        "preprinted_layers": [{"key": k, **v} for k, v in PREPRINTED_META.items()],
        "reprint_policies": [{"key": k, **v} for k, v in REPRINT_META.items()],
    }


async def recent_jobs(session, event: Events, limit: int = 50) -> list[dict]:
    rows = (
        await session.execute(
            select(PrintJobs, EventRegistrationsLink.full_name)
            .outerjoin(EventRegistrationsLink, EventRegistrationsLink.id == PrintJobs.registration_id)
            .where(PrintJobs.event_id == event.id).order_by(PrintJobs.created_at.desc()).limit(limit)
        )
    ).all()
    return [
        {"id": j.id, "source": j.source.value, "door": j.door_label, "cards": j.cards, "guest": name, "design_version": j.design_version, "overrode": j.overrode, "error": j.error, "created_at": j.created_at}
        for j, name in rows
    ]


def badge_for_scan(event: Events, reg: EventRegistrationsLink, actor: GateActor, *, admitted: bool) -> dict:
    """What the console should do about a card after a scan."""
    policy = event.reprint_policy or ReprintPolicy.anyone
    may, _ = may_print_again(policy, reg.badge_printed_at, actor.full)
    return {
        "auto": bool(event.auto_print_badges) and admitted and may and not reg.badge_printed_at,
        "may_print": may,
        "printed_at": reg.badge_printed_at,
        "policy": policy.value,
    }


def brand_of(style: dict, club=None) -> dict:
    return layout_colors(style, club)
