"""An event's setup, copied onto a new date.

Everything that is a setting comes across: the fields guests answer, the
door and card rules, self check-in, retention, the designs (ticket, card,
certificate, programme), the email journey, the merchandise, the organisers
its page names. What is a judgement call is asked for: the doors, the team,
the agenda. What is never the same is left out: the venue, the
registrations, every handover, every report. The copy starts as a draft on
the clock.

The merchandise: the store and handout switches, the handout items (copied:
they are the event's own), and the club's store items the event offers
(the offers, not new items: the copy sells from the same shelf).

The copy is a new event, so it carries only what the club's plan allows
(`allowed`, from subscriptions.gates.duplicate_allowances): without `merch`
neither the store switched on nor its offers, without `swag` neither the
handouts switched on nor their items, never more than
`merch_items_per_event` items of the two together (handouts first), without
`badges` no ID card designs, without `report` no report recipients. What was
left behind is named in `skipped`, by flag or limit key, rather than refused.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.agenda.models import AgendaItems, AgendaTemplates
from app.api.certificate_templates.models import CertificateTemplates
from app.api.checkins.models import Doors
from app.api.closeout.models import EventStatus
from app.api.emails.models import EmailTemplates
from app.api.events.models import EventInterestsLink, Events
from app.api.events.volunteer.models import Volunteer
from app.api.idcards.models import BadgeTemplates
from app.api.merch.models import MerchItems, MerchVariants
from app.api.organisers.models import EventTeamMembers
from app.api.store.models import EventStoreItems, EventStoreOffers
from app.api.ticket_templates.models import TicketTemplates
from app.core.utils.keys import generate_slug

logger = logging.getLogger(__name__)

SETTINGS = (
    "category_id", "has_fee", "reg_fee", "has_prize", "prize_amount", "is_online", "about", "contact_name", "contact_phone", "contact_email", "url",
    "additional_details", "event_guidelines", "event_tag", "ticket_template_id", "certificate_template_id", "max_participants",
    "door_mode", "close_after_minutes", "spot_registration_checks_in", "capture_contact_at_gate", "require_contact_at_gate",
    "reprint_policy", "auto_print_badges", "preprinted_carries", "self_check_in_enabled", "self_check_in_verify", "self_check_in_print",
    "retention_mode", "retention_hours", "report_recipients", "keep_contacts", "requires_approval", "high_demand_label",
)
DESIGN_MODELS = (TicketTemplates, BadgeTemplates, CertificateTemplates, AgendaTemplates)


async def _event_rows(session, model, event_id: int) -> list:
    q = select(model).where(model.event_id == event_id)
    if hasattr(model, "is_deleted"):
        q = q.where(model.is_deleted == False)  # noqa: E712
    return list((await session.scalars(q)).all())


async def duplicate_event(
    session, source: Events, user, *, name: str, event_datetime: datetime, duration: Optional[float] = None,
    copy_doors: bool = False, copy_team: bool = False, copy_agenda: bool = False, copy_merch: bool = True,
    allowed: Optional[dict] = None,
) -> dict:
    allowed = {"merch": True, "swag": True, "badges": True, "report": True, "merch_items_per_event": None, **(allowed or {})}
    skipped: list[str] = []

    def skip(what: str) -> None:
        if what not in skipped:
            skipped.append(what)

    new = Events(name=name.strip(), slug=generate_slug(name), event_datetime=event_datetime, duration=duration or source.duration, club_id=source.club_id, images=[], reg_startdate=datetime.now(timezone.utc))
    for key in SETTINGS:
        setattr(new, key, getattr(source, key))
    if new.report_recipients and not allowed["report"]:
        new.report_recipients = []
        skipped.append("report")
    new.lifecycle = EventStatus.draft
    new.status_clock = True
    # The store's and the handouts' switches, as the plan allows them.
    if source.merch_enabled:
        if allowed["merch"]:
            new.merch_enabled = True
        else:
            skip("merch")
    if source.swag_enabled:
        if allowed["swag"]:
            new.swag_enabled = True
        else:
            skip("swag")
    session.add(new)
    await session.flush()
    copied = {"interests": 0, "designs": 0, "emails": 0, "merch": 0, "store": 0, "doors": 0, "team": 0, "agenda": 0, "organisers": 0}

    for link in await _event_rows(session, EventInterestsLink, source.id):
        session.add(EventInterestsLink(event_id=new.id, interest_id=link.interest_id))
        copied["interests"] += 1

    for o in await _event_rows(session, EventTeamMembers, source.id):
        session.add(EventTeamMembers(event_id=new.id, member_id=o.member_id, role=o.role, sort_order=o.sort_order))
        copied["organisers"] += 1

    # The event's own designs; a club default needs no copying, the new event resolves to it anyway.
    for model in DESIGN_MODELS:
        rows = await _event_rows(session, model, source.id)
        if rows and model is BadgeTemplates and not allowed["badges"]:
            skipped.append("badges")
            continue
        for row in rows:
            session.add(model(club_id=row.club_id, event_id=new.id, audience=row.audience, name=row.name, is_default=row.is_default, config=dict(row.config or {})))
            copied["designs"] += 1

    for t in await _event_rows(session, EmailTemplates, source.id):
        session.add(EmailTemplates(
            club_id=t.club_id, event_id=new.id, stage=t.stage, name=t.name, subject=t.subject, design=t.design, attachments=t.attachments,
            audience=t.audience, trigger=t.trigger, channels=t.channels, enabled=t.enabled, version=t.version, source_id=t.id, created_by=getattr(user, "id", None),
        ))
        copied["emails"] += 1

    if copy_merch:
        items = list((await session.scalars(select(MerchItems).options(selectinload(MerchItems.variants)).where(MerchItems.event_id == source.id).order_by(MerchItems.created_at))).all())
        cap = allowed["merch_items_per_event"]
        if items and not allowed["swag"]:
            skip("swag")
            items = []
        elif cap is not None and len(items) > cap:
            skip("merch_items_per_event")
            items = items[:cap]
        for m in items:
            item = MerchItems(event_id=new.id, name=m.name, photo_key=m.photo_key, stock=m.stock, eligibility_field=m.eligibility_field, eligibility_value=m.eligibility_value, variant_field=m.variant_field)
            item.variants = [MerchVariants(label=v.label, stock=v.stock) for v in m.variants]
            session.add(item)
            copied["merch"] += 1

        # The club's store items the source offers: the same items, offered on
        # the copy in the same order, in whatever room the handouts left.
        offers = list((await session.execute(
            select(EventStoreOffers.item_id, EventStoreOffers.sort_order)
            .join(EventStoreItems, EventStoreItems.id == EventStoreOffers.item_id)
            .where(EventStoreOffers.event_id == source.id, EventStoreItems.club_id == source.club_id, EventStoreItems.is_deleted == False)  # noqa: E712
            .order_by(EventStoreOffers.sort_order, EventStoreItems.sort_order)
        )).all())
        room = None if cap is None else max(0, cap - copied["merch"])
        if offers and not allowed["merch"]:
            skip("merch")
            offers = []
        elif room is not None and len(offers) > room:
            skip("merch_items_per_event")
            offers = offers[:room]
        for item_id, sort_order in offers:
            session.add(EventStoreOffers(event_id=new.id, item_id=item_id, sort_order=sort_order))
            copied["store"] += 1

    door_ids: dict = {}
    if copy_doors:
        for d in await _event_rows(session, Doors, source.id):
            nd = Doors(event_id=new.id, name=d.name, kind=d.kind, sort_order=d.sort_order, active=d.active)
            session.add(nd)
            await session.flush()
            door_ids[d.id] = nd.id
            copied["doors"] += 1

    if copy_team:
        for v in await _event_rows(session, Volunteer, source.id):
            session.add(Volunteer(
                email=v.email, event_id=new.id, user_id=v.user_id, club_id=v.club_id, is_approved=v.is_approved,
                can_check_in=v.can_check_in, can_walkup=v.can_walkup, can_merch=v.can_merch, door_id=door_ids.get(v.door_id),
            ))
            copied["team"] += 1

    if copy_agenda:
        delta = event_datetime - source.event_datetime
        for a in await _event_rows(session, AgendaItems, source.id):
            session.add(AgendaItems(
                event_id=new.id, starts_at=a.starts_at + delta, ends_at=(a.ends_at + delta) if a.ends_at else None, title=a.title, description=a.description,
                speaker=a.speaker, speaker_photo_key=a.speaker_photo_key, location=a.location, sort_order=a.sort_order,
            ))
            copied["agenda"] += 1

    await session.commit()
    if skipped:
        logger.info("Event %s copied to %s without %s (the club's plan)", source.id, new.id, ", ".join(skipped))
    return {"id": new.id, "slug": new.slug, "name": new.name, "copied": copied, "skipped": skipped}
