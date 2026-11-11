"""What /partner/v1 does with live credentials: through the same domain
services the dashboard and the app use, never the models directly.

- events: events.service.create_event / update_event (as the dashboard's
  forms), the plan's gates as the dashboard's routes check them, ticket types
  following the event (ticketing.types.follow_event_edit); publishing by the
  status clock's own change (closeout.service.update_settings); cancelling
  by club_ops.cancel, refused when the event has online payments;
- registrations: registration.service.register_event, as the organiser
  adding a guest (the plan's caps, the event's and the ticket type's places,
  the answers, the waiting list and the ticket email all as there); events
  that review registrations take a request through approvals.submit, as
  the public form does; corrections and cancelling by update_registration
  and take_off_list;
- check-ins: checkins.service.scan with the registration picked out (one
  row per guest, so a second scan is a duplicate, never a second check-in),
  undo by checkins.service.undo;
- certificates: certificates.service.issue and the deliveries queue.

Money never moves (contract decision 5): a paid event refuses a
registration without a payment (PAYMENT_REQUIRED, with the event's page to
pay on), unless the club allows payments taken outside MyOtherApp for that
event; such a registration is marked paid at nothing -- so it holds its paid
place, and finance, the ledger and payouts never count it -- and the amount
the partner reported is kept in partner_external_payments. Cancelling a
registration paid online, or an event with online payments, is the
dashboard's (CANCEL_IN_DASHBOARD): a refund must be decided by a person.

Every write leaves an audit line (as the club's account, via the API, which
client); every read of attendees' details is counted there too (how many,
never what).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.orm import joinedload

from app.api.audit import service as audit
from app.api.partner import external_ids as ext_ids
from app.api.partner import shapes
from app.api.partner.common import (
    check_if_match, check_limit, decode_cursor, encode_cursor, iso, list_response, now, object_response, parse_time,
)
from app.api.partner.errors import PartnerError, not_found
from app.api.partner.fetch import fetch_image
from app.api.partner.models import PartnerCertificateJobs, PartnerExternalPayments
from app.core import public_urls
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

MAX_HOURS = 72
#: What an event object shows but only the club changes (decision 5: money is
#: the club's call): "Accept registrations paid outside MyOtherApp".
READ_ONLY_EVENT_FIELDS = frozenset({"external_payments_allowed"})


def _uuid(value) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _record(caller, action: str, *, entity_type: str, entity_id, summary: str, **details) -> None:
    await audit.record(
        caller.session, actor=caller.owner, action=action, entity_type=entity_type, entity_id=entity_id,
        summary=f"{summary} (via {caller.name or 'the API'})"[:255], details=caller.audit_details(**details), request=caller.request,
    )


async def _count_attendee_reads(caller, n: int, *, event_id=None, what: str = "registrations") -> None:
    """Attendees' details handed out: counted in the audit log and the usage (counts, never values)."""
    if not caller.contact or n <= 0:
        return
    caller.request.state.partner_attendee_reads = int(getattr(caller.request.state, "partner_attendee_reads", 0) or 0) + n
    await _record(caller, "partner.attendees.read", entity_type="event" if event_id else "club", entity_id=event_id or caller.club_id,
                  summary=f"Details of {n} {what} read", count=n, fields=["name", "email", "phone", "answers"])
    await caller.session.commit()


# ─── Me and the club ─────────────────────────────────────────────────────


def me(caller) -> dict:
    from app.api.partner import scopes as catalogue

    club = caller.club
    return {
        "type": caller.client_type,
        "club": {"id": str(club.id), "name": club.name, "slug": club.slug} if club is not None else None,
        "partner": {"id": str(caller.app.id), "name": caller.app.name, "client_id": caller.app.client_id} if caller.app is not None else None,
        "key": {"id": str(caller.key.id), "name": caller.key.name, "prefix": caller.key.prefix} if caller.key is not None else None,
        "client_id": caller.client_id,
        "scopes": [s for s in catalogue.ALL if s in caller.scopes],
        "livemode": caller.livemode,
        "rate_limit": {"read_per_min": int(caller.rate_limit.get("read_per_min", 600)), "write_per_min": int(caller.rate_limit.get("write_per_min", 120))},
    }


async def _socials(session, club_id: int):
    from app.api.clubs.models import ClubSocials

    return await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club_id, ClubSocials.is_deleted == False))  # noqa: E712


async def club_out(caller, club=None) -> dict:
    club = club or caller.require_club()
    ext = await ext_ids.of_object(caller.session, caller.client_id, "club", club.id)
    return shapes.club_object(club, await _socials(caller.session, club.id), external_id=ext)


async def get_club(caller):
    return object_response(caller.request, await club_out(caller))


async def patch_club(caller, body):
    from app.api.clubs import service as clubs_service
    from app.api.clubs.models import ClubInterestsLink, Clubs

    session = caller.session
    club = caller.require_club()
    check_if_match(caller.request, await club_out(caller, club))
    sent = body.model_dump(exclude_unset=True)
    logo = await fetch_image(body.logo_url, "logo_url") if body.logo_url else None
    socials = await _socials(session, club.id)
    links = dict(sent.get("links") or {})
    interest_ids = list(await session.scalars(select(ClubInterestsLink.interest_id).where(ClubInterestsLink.club_id == club.id)))
    form = SimpleNamespace(
        name=sent.get("name") or club.name, about=sent["about"] if "about" in sent else club.about,
        tagline=sent["tagline"] if "tagline" in sent else club.tagline, brand_primary=club.brand_primary, brand_secondary=club.brand_secondary,
        location_link=club.location_link, location_name=club.location_name, pin=None, clear_point=False, org_id=club.org_id,
        contact_phone=club.contact_phone, contact_email=club.contact_email, interest_ids=interest_ids, logo=logo,
        **{k: links[k] if k in links else getattr(socials, k, None) for k in ("instagram", "linkedin", "youtube", "website")},
    )
    if "external_id" in sent:
        await ext_ids.set_for(session, caller, "club", club.id, ext_ids.check(sent["external_id"]))
    await clubs_service.update_club(session, club.id, form)
    await _record(caller, "partner.club.update", entity_type="club", entity_id=club.id, summary="Club profile edited", fields=sorted(sent))
    await session.commit()
    club = await session.scalar(select(Clubs).where(Clubs.id == club.id).execution_options(populate_existing=True))
    caller.club = club
    return object_response(caller.request, await club_out(caller, club))


# ─── Clubs (partner apps) ────────────────────────────────────────────────


async def list_clubs(caller, *, limit: Optional[int], cursor: Optional[str]):
    from app.api.clubs.models import Clubs
    from app.api.partner.models import PartnerConnections

    if caller.app is None:
        raise PartnerError("FORBIDDEN_SCOPE", "Only a partner app has connected clubs; a club's key reads its own club at /club.")
    limit = check_limit(limit)
    after = decode_cursor(cursor, ("dt", "str"))
    q = (
        select(PartnerConnections, Clubs).join(Clubs, Clubs.id == PartnerConnections.club_id)
        .where(PartnerConnections.app_id == caller.app.id, PartnerConnections.revoked_at.is_(None),
               PartnerConnections.livemode == caller.livemode, Clubs.is_deleted == False)  # noqa: E712
        .order_by(PartnerConnections.created_at, PartnerConnections.id).limit(limit + 1)
    )
    if after:
        q = q.where(tuple_(PartnerConnections.created_at, PartnerConnections.id) > (after[0], _uuid(after[1])))
    rows = list((await caller.session.execute(q)).all())
    page, more = rows[:limit], len(rows) > limit
    exts = await ext_ids.of_objects(caller.session, caller.client_id, "club", [c.id for _, c in page])
    data = []
    for conn, club in page:
        item = shapes.club_object(club, await _socials(caller.session, club.id), external_id=exts.get(str(club.id)))
        item["connection"] = {"id": str(conn.id), "scopes": conn.scopes.split(), "via": conn.via, "connected_at": iso(conn.created_at)}
        data.append(item)
    nxt = encode_cursor(page[-1][0].created_at, str(page[-1][0].id)) if more and page else None
    return list_response(data, nxt)


def _send_owner_invite(to: str, *, club_name: str, partner: str, owner_name: Optional[str]) -> None:
    """The person named as a new club's owner hears what was set up for them."""
    import html

    from app.config import settings
    from app.core.email import email as email_mod

    first = (owner_name or "").strip().split(" ")[0]
    greeting = f"Hi {first}," if first else "Hi,"
    dashboard = settings.MCP_DASHBOARD_URL.rstrip("/")
    text = (
        f"{greeting}\n\n{partner} set up {club_name} on MyOtherApp, with you as its owner.\n\n"
        "The MyOtherApp team looks at every new club before it goes live; you'll get an email once it's approved. "
        f"Then set your password at {dashboard}/forgot-password with this email address and sign in to the club dashboard.\n\n"
        f"If you don't know {partner} or didn't expect this, reply to this email and we'll remove the club.\n"
    )
    esc = html.escape
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;color:#2C333D;max-width:520px;margin:0 auto;padding:24px 16px">'
        f"<p>{esc(greeting)}</p><p><strong>{esc(partner)}</strong> set up <strong>{esc(club_name)}</strong> on MyOtherApp, with you as its owner.</p>"
        "<p>The MyOtherApp team looks at every new club before it goes live; you'll get an email once it's approved. "
        f'Then set your password at <a href="{esc(dashboard)}/forgot-password">{esc(dashboard)}/forgot-password</a> with this email address and sign in.</p>'
        f'<p style="color:#6B7280;font-size:13px">If you don\'t know {esc(partner)} or didn\'t expect this, reply and we\'ll remove the club.</p></div>'
    )
    email_mod.send_email(recipients=[to], subject=f"{club_name} was set up for you on MyOtherApp", text=text, html=body)


async def create_club(caller, body):
    """clubs:create: a club in the same pending state as a self-signup, its
    owner told by email, the partner connected to it. The admin still
    approves it; its tokens work from then."""
    from app.api.clubs import service as clubs_service
    from app.api.partner import allocation
    from app.api.partner import scopes as catalogue
    from app.api.partner.models import PartnerConnections
    from app.api.users import service as user_service
    from app.core.after_commit import run_after_commit

    session = caller.session
    app = caller.app
    if app is None:
        raise PartnerError("FORBIDDEN_SCOPE", "Only a partner app can create clubs.")
    email = str(body.owner.email).strip()
    if await user_service.email_has_club_account(session, email):
        raise PartnerError("ALREADY_EXISTS", "That email already has a club account on MyOtherApp.", details={"fields": {"owner.email": "Already a club's account."}})
    external = ext_ids.check(body.external_id)
    logo = await fetch_image(body.logo_url, "logo_url") if body.logo_url else None
    reservation = await ext_ids.reserve(session, caller, "club", external)
    links = (body.links.model_dump() if body.links else {})
    try:
        club = await clubs_service.create_club(
            session, name=body.name.strip(), email=email, password=secrets.token_urlsafe(32), phone=None, about=body.about,
            location_name=body.location, logo=logo, contact_phone=body.contact_phone,
            contact_email=str(body.contact_email) if body.contact_email else None, socials=links,
        )
    except CustomHTTPException as e:
        await ext_ids.release(session, reservation)
        if e.status_code == 400 and "already exists" in (e.message or ""):
            raise PartnerError("ALREADY_EXISTS", e.message, details={"fields": {"name": "Taken."}})
        raise
    except Exception:
        await ext_ids.release(session, reservation)
        raise
    if body.tagline:
        club.tagline = body.tagline.strip()[:120] or None
    # The admin, in registering the app for clubs:create, allowed it to bring
    # clubs with API access; the row says so, and the admin can still turn it off.
    await allocation.put(session, club.id, enabled=True, source="partner_created", notes=f"Created by the partner app {app.name}.")
    scopes = catalogue.join(s for s in catalogue.parse(app.allowed_scopes) if s not in ("clubs:create", "attendees:read"))
    conn = PartnerConnections(
        app_id=app.id, club_id=club.id, scopes=scopes, livemode=True, via="clubs_create", connected_by_name=app.name, last_used_at=now(),
    )
    session.add(conn)
    await session.flush()
    from app.api.admin_alerts import notify

    await notify(
        session, "club_applied", title=f"New club from {app.name}: {club.name}",
        body=f"{app.name} created {club.name} through the Integrations API, with {email} as its owner. Approve or reject it under Clubs.",
        club=club, data={"partner_app": app.client_id, "owner_email": email},
    )
    try:
        from app.api.leads import service as leads

        async with session.begin_nested():
            await leads.on_club_signup(session, club)
    except Exception:  # noqa: BLE001 - the lead is the sales team's, never in the way
        logger.warning("club %s: its sign-up lead couldn't be made", club.id, exc_info=True)
    run_after_commit(session, _send_owner_invite, email, club_name=club.name, partner=app.name, owner_name=body.owner.name)
    await _record(caller, "partner.club.create", entity_type="club", entity_id=club.id, summary=f"Club {club.name} created, waiting for approval", owner_email=email)
    await session.commit()
    await ext_ids.bind(session, reservation, club.id)
    out = shapes.club_object(club, await _socials(session, club.id), external_id=external)
    # No tokens here (an answer is kept for its Idempotency-Key): once the club
    # is approved, the app's own token acts for it with MOA-Club-Id.
    out["connection"] = {"id": str(conn.id), "scopes": scopes.split(), "via": "clubs_create", "act_with_header": {"MOA-Club-Id": str(club.id)}}
    return JSONResponse(out, status_code=201)


# ─── Events ──────────────────────────────────────────────────────────────


async def _event_of(caller, event_id, *, lock: bool = False):
    from app.api.events.models import Events

    caller.require_club()
    if not str(event_id).isdigit():
        raise not_found("Event")
    q = select(Events).options(joinedload(Events.club)).where(
        Events.id == int(event_id), Events.club_id == caller.club_id, Events.is_deleted == False,  # noqa: E712
    ).execution_options(populate_existing=True)
    event = await caller.session.scalar(q)
    if event is None:
        raise not_found("Event")
    return event


async def event_out(caller, event) -> dict:
    from app.api.ticketing import types as types_mod

    types = await types_mod.live(caller.session, event.id)
    ext = await ext_ids.of_object(caller.session, caller.client_id, "event", event.id)
    return shapes.event_object(event, ticket_types=types, external_id=ext)


def _ended_sql(Events):
    from app.api.events.models import ends_at_sql

    return ends_at_sql(Events) < now()


async def list_events(caller, *, status, starts_after, starts_before, updated_since, external_id, limit, cursor):
    from app.api.closeout.models import EventStatus
    from app.api.events.models import Events
    from app.api.ticketing.models import TicketTypes

    caller.require_club()
    session = caller.session
    limit = check_limit(limit)
    q = select(Events).where(Events.club_id == caller.club_id, Events.is_deleted == False)  # noqa: E712
    if status == "draft":
        q = q.where(Events.lifecycle == EventStatus.draft)
    elif status == "cancelled":
        q = q.where(Events.lifecycle == EventStatus.cancelled)
    elif status == "ended":
        q = q.where(or_(Events.lifecycle.in_((EventStatus.closed, EventStatus.archived)),
                        and_(Events.lifecycle.in_((EventStatus.published, EventStatus.live)), _ended_sql(Events))))
    elif status == "published":
        q = q.where(Events.lifecycle.in_((EventStatus.published, EventStatus.live)), ~_ended_sql(Events))
    if starts_after:
        q = q.where(Events.event_datetime >= parse_time(starts_after, "starts_after"))
    if starts_before:
        q = q.where(Events.event_datetime < parse_time(starts_before, "starts_before"))
    if external_id is not None:
        found = await ext_ids.find(session, caller.client_id, "event", ext_ids.check(external_id))
        q = q.where(Events.id == (int(found) if found and found.isdigit() else -1))
    since = parse_time(updated_since, "updated_since") if updated_since else None
    if since is not None:
        q = q.where(Events.updated_at >= since)
        after = decode_cursor(cursor, ("dt", "int"))
        if after:
            q = q.where(tuple_(Events.updated_at, Events.id) > (after[0], after[1]))
        q = q.order_by(Events.updated_at, Events.id)
    else:
        after = decode_cursor(cursor, ("int",))
        if after:
            q = q.where(Events.id > after[0])
        q = q.order_by(Events.id)
    rows = list((await session.scalars(q.limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    types: dict = {}
    if page:
        for t in await session.scalars(
            select(TicketTypes).where(TicketTypes.event_id.in_([e.id for e in page]), TicketTypes.is_deleted == False)  # noqa: E712
            .order_by(TicketTypes.sort_order, TicketTypes.id)
        ):
            types.setdefault(t.event_id, []).append(t)
    exts = await ext_ids.of_objects(session, caller.client_id, "event", [e.id for e in page])
    data = [shapes.event_object(e, ticket_types=types.get(e.id, []), external_id=exts.get(str(e.id))) for e in page]
    nxt = None
    if more and page:
        last = page[-1]
        nxt = encode_cursor(last.updated_at, last.id) if since is not None else encode_cursor(last.id)
    return list_response(data, nxt)


async def get_event(caller, event_id):
    event = await _event_of(caller, event_id)
    return object_response(caller.request, await event_out(caller, event))


def event_fields(body, *, current=None) -> dict:
    """The domain's columns from the API's fields, checked as the dashboard's form is."""
    sent = body.model_dump(exclude_unset=True)
    for field in READ_ONLY_EVENT_FIELDS & set(sent):
        raise PartnerError(
            "FIELD_READ_ONLY", f"{field} can't be set through the API: only the club turns it on, in the dashboard.",
            details={"fields": {field: "Read-only."}},
        )
    out: dict = {}
    starts = parse_time(sent["starts_at"], "starts_at") if "starts_at" in sent else (current.event_datetime if current is not None else None)
    if "ends_at" in sent:
        ends = parse_time(sent["ends_at"], "ends_at")
    elif current is not None:
        ends = current.event_datetime + timedelta(hours=float(current.duration or 0))
        if "starts_at" in sent:
            # Moving the start keeps the length.
            ends = starts + timedelta(hours=float(current.duration or 0))
    else:
        ends = None
    if starts is None or ends is None:
        raise PartnerError("VALIDATION", "starts_at and ends_at are needed.", details={"fields": {"starts_at": "Required.", "ends_at": "Required."}})
    hours = (ends - starts).total_seconds() / 3600
    if hours <= 0:
        raise PartnerError("VALIDATION", "ends_at must be after starts_at.", details={"fields": {"ends_at": "After starts_at."}})
    if hours > MAX_HOURS and (current is None or "starts_at" in sent or "ends_at" in sent):
        raise PartnerError("VALIDATION", f"An event can run for at most {MAX_HOURS} hours. For something longer, make one event a day.",
                           details={"fields": {"ends_at": f"At most {MAX_HOURS} hours after starts_at."}})
    out["event_datetime"], out["duration"] = starts, round(hours, 4)
    if "name" in sent:
        out["name"] = sent["name"].strip()
    if "description" in sent:
        out["about"] = sent["description"]
    if "venue" in sent:
        v = sent["venue"]
        if v is None:
            out["location_name"], out["pin"] = None, None
        else:
            words = ", ".join(p.strip() for p in (v.get("name"), v.get("address"), v.get("city")) if p and p.strip())
            out["location_name"] = words[:500]
            out["pin"] = (v["lat"], v["lng"]) if v.get("lat") is not None and v.get("lng") is not None else None
    if "online" in sent:
        o = sent["online"]
        url = (o or {}).get("url")
        if url and not url.lower().startswith(("https://", "http://")):
            raise PartnerError("VALIDATION", "online.url must be a web address.", details={"fields": {"online.url": "http(s) only."}})
        out["is_online"], out["url"] = bool(url), url
    if "capacity" in sent:
        out["max_participants"] = sent["capacity"]
    reg = sent.get("registration") or {}
    if "opens_at" in reg:
        out["reg_startdate"] = parse_time(reg["opens_at"], "registration.opens_at") or now()
    if "closes_at" in reg:
        out["reg_enddate"] = parse_time(reg["closes_at"], "registration.closes_at")
    if reg.get("requires_approval") is not None:
        out["requires_approval"] = bool(reg["requires_approval"])
    closes = out.get("reg_enddate", current.reg_enddate if current is not None else None)
    if closes is not None and closes > ends:
        raise PartnerError("VALIDATION", "Registration can't close after the event ends.", details={"fields": {"registration.closes_at": "Before ends_at."}})
    if "price" in sent:
        price = sent["price"]
        if price and int(price.get("amount") or 0) > 0:
            from app.core import money as money_mod

            out["has_fee"], out["reg_fee"] = True, float(money_mod.from_paise(int(price["amount"])))
        else:
            out["has_fee"], out["reg_fee"] = False, None
    return out


async def _category(session, name: Optional[str]):
    from app.api.events.models import EventCategories

    rows = list((await session.scalars(
        select(EventCategories).where(EventCategories.is_deleted == False).order_by(EventCategories.id)  # noqa: E712
    )).all())
    if not rows:
        raise PartnerError("VALIDATION", "There are no event types to file this event under. Ask the MyOtherApp team.")
    if not name:
        return rows[0]
    for row in rows:
        if row.name.strip().lower() == name.strip().lower():
            return row
    raise PartnerError("VALIDATION", f"There is no event type called {name}.", details={"fields": {"category": "One of: " + ", ".join(r.name for r in rows)}})


async def create_event(caller, body):
    """Always a DRAFT: nobody sees it until it is published (events:publish)."""
    from app.api.events import service as events_service
    from app.api.subscriptions import gates
    from app.api.ticketing import types as ticket_types

    session = caller.session
    club = caller.require_club()
    external = ext_ids.check(body.external_id)
    fields = event_fields(body)
    if fields["event_datetime"] <= now():
        raise PartnerError("VALIDATION", "starts_at is in the past; an event is for something still to come.", details={"fields": {"starts_at": "In the future."}})
    category = await _category(session, body.category)
    # The plan's checks the dashboard's create route makes, as the club.
    await gates.check_event_create(session, club.id, has_fee=bool(fields.get("has_fee")))
    poster = await fetch_image(body.poster_url, "poster_url") if body.poster_url else None
    reservation = await ext_ids.reserve(session, caller, "event", external)
    try:
        created = await events_service.create_event(
            session, user_id=caller.owner.id, name=fields["name"], event_datetime=fields["event_datetime"], duration=fields["duration"],
            category_id=category.id, poster=poster, has_fee=bool(fields.get("has_fee")), reg_fee=fields.get("reg_fee"),
            location_name=fields.get("location_name"), is_online=bool(fields.get("is_online")), url=fields.get("url"),
            reg_startdate=fields.get("reg_startdate") or now(), reg_enddate=fields.get("reg_enddate"), about=fields.get("about"),
            max_participants=fields.get("max_participants"), status="draft", pin=fields.get("pin"),
            requires_approval=fields.get("requires_approval"),
        )
        await ticket_types.follow_event_edit(session, created)
    except Exception:
        await ext_ids.release(session, reservation)
        raise
    await ext_ids.bind(session, reservation, created.id)
    await _record(caller, "partner.event.create", entity_type="event", entity_id=created.id, summary=f"Draft event {created.name} created")
    await session.commit()
    event = await _event_of(caller, created.id)
    return object_response(caller.request, await event_out(caller, event), status=201)


class _Stored(dict):
    """A stored question, handed back to update_event exactly as kept."""

    def to_store(self) -> dict:
        return dict(self)


async def _update(caller, event, changes: dict) -> None:
    """events.service.update_event with the event as it is, `changes` on top:
    the dashboard's edit form, every field it sends filled from the event."""
    from app.api.events import service as events_service
    from app.api.events.models import EventInterestsLink
    from app.api.subscriptions import gates  # noqa: F401 - update_event checks the plan itself
    from app.api.ticketing import types as ticket_types

    session = caller.session
    interest_ids = list(await session.scalars(select(EventInterestsLink.interest_id).where(EventInterestsLink.event_id == event.id)))
    pin = changes.get("pin")
    form = SimpleNamespace(
        name=changes.get("name", event.name), poster=changes.get("poster"), event_datetime=changes.get("event_datetime", event.event_datetime),
        has_fee=changes.get("has_fee", bool(event.has_fee)), reg_fee=changes.get("reg_fee", float(event.reg_fee) if event.reg_fee is not None else None),
        duration=changes.get("duration", event.duration), location_name=changes.get("location_name", event.location_name),
        location_link=None, clear_location_link=False, pin=pin, clear_point="pin" in changes and pin is None and "location_name" in changes,
        has_prize=bool(event.has_prize), prize_amount=float(event.prize_amount) if event.prize_amount is not None else None,
        is_online=changes.get("is_online", bool(event.is_online)), merch_enabled=None, swag_enabled=None, store_item_ids=None,
        reg_startdate=changes.get("reg_startdate", event.reg_startdate), reg_enddate=changes.get("reg_enddate", event.reg_enddate),
        about=changes.get("about", event.about), contact_name=event.contact_name, contact_phone=event.contact_phone,
        contact_email=event.contact_email, url=changes.get("url", event.url), category_id=event.category_id, club_id=event.club_id,
        interest_ids=interest_ids, additional_details=[_Stored(q) for q in (event.additional_details or [])],
        event_guidelines=event.event_guidelines, max_participants=changes.get("max_participants", event.max_participants),
        event_tag=event.event_tag, speakers=None, ticket_template_id=event.ticket_template_id,
        certificate_template_id=changes.get("certificate_template_id", event.certificate_template_id),
        requires_approval=changes.get("requires_approval"), high_demand_label=None, allow_self_cancel=None,
        # Never from the API: None leaves the club's own setting as it is.
        external_payments_allowed=None,
    )
    updated = await events_service.update_event(session, form, caller.owner.id, event_id=event.id, actor=caller.owner)
    await ticket_types.follow_event_edit(session, updated)


async def patch_event(caller, event_id, body):
    session = caller.session
    event = await _event_of(caller, event_id)
    check_if_match(caller.request, await event_out(caller, event))
    sent = body.model_dump(exclude_unset=True)
    changes = event_fields(body, current=event)
    for key in ("event_datetime", "duration"):
        if key in changes and not ({"starts_at", "ends_at"} & set(sent)):
            changes.pop(key)
    if "category" in sent:
        raise PartnerError("VALIDATION", "An event's type is changed in the dashboard.", details={"fields": {"category": "Read only here."}})
    if body.poster_url:
        changes["poster"] = await fetch_image(body.poster_url, "poster_url")
    if "external_id" in sent:
        await ext_ids.set_for(session, caller, "event", event.id, ext_ids.check(sent["external_id"]))
        await session.flush()
    if set(sent) - {"external_id"}:
        await _update(caller, event, changes)
    await _record(caller, "partner.event.update", entity_type="event", entity_id=event.id, summary=f"Event {event.name} edited", fields=sorted(sent))
    await session.commit()
    event = await _event_of(caller, event.id)
    return object_response(caller.request, await event_out(caller, event))


async def publish_event(caller, event_id):
    from app.api.closeout import service as closeout
    from app.api.closeout.models import EventStatus
    from app.api.closeout.schemas import SettingsPatch

    session = caller.session
    event = await _event_of(caller, event_id)
    life = event.lifecycle or EventStatus.published
    if life in (EventStatus.published, EventStatus.live):
        return object_response(caller.request, await event_out(caller, event))
    if life is not EventStatus.draft:
        raise PartnerError("INVALID_STATE", f"Only a draft can be published; this event is {shapes._status_of_event(event)}.")
    if event.event_datetime + timedelta(hours=float(event.duration or 0)) <= now():
        raise PartnerError("INVALID_STATE", "This event is already over, so it can't be published.")
    await closeout.update_settings(session, event, SettingsPatch(status="published"))
    await _record(caller, "partner.event.publish", entity_type="event", entity_id=event.id, summary=f"Event {event.name} published")
    await session.commit()
    event = await _event_of(caller, event.id)
    return object_response(caller.request, await event_out(caller, event))


class _ApiActor:
    """club_ops' Actor, named as the API client that acted."""

    def __init__(self, caller):
        self.user = caller.owner
        self.member = None
        self.impersonated_by = None
        self._name = f"{caller.name or 'API'} (via the API)"[:120]

    @property
    def user_id(self):
        return getattr(self.user, "id", None)

    member_id = None

    @property
    def name(self) -> str:
        return self._name

    def details(self) -> dict:
        return {"member_id": None, "impersonated_by": None, "via": "partner_api"}


async def cancel_event(caller, event_id, reason: str):
    from app.api.club_ops import cancel as cancel_service
    from app.api.mcp.oauth import dashboard_url

    session = caller.session
    event = await _event_of(caller, event_id)
    state = await cancel_service.state(session, event)
    if state["cancelled"]:
        return object_response(caller.request, await event_out(caller, event))
    if not state["can_cancel"]:
        raise PartnerError("INVALID_STATE", state["blocked_reason"] or "This event can't be cancelled.")
    if state["paid_online"] > 0:
        raise PartnerError(
            "CANCEL_IN_DASHBOARD",
            f"{state['paid_online']} people paid online for this event, so cancelling it refunds them: do it in the dashboard.",
            details={"paid_online": state["paid_online"], "dashboard_url": dashboard_url(f"/events/{event.id}/cancel")},
        )
    await cancel_service.cancel(
        session, event.id, reason=reason, actor=_ApiActor(caller), expected_registrations=state["registrations"],
        expected_refund_total=state["refund_total"], request=caller.request,
    )
    await _record(caller, "partner.event.cancel", entity_type="event", entity_id=event.id, summary=f"Event {event.name} cancelled", reason=reason[:200])
    await session.commit()
    event = await _event_of(caller, event.id)
    return object_response(caller.request, await event_out(caller, event))


# ─── Registrations ───────────────────────────────────────────────────────


async def _registration_of(caller, registration_id, *, include_deleted: bool = True):
    """(registration, request): a registration by its id, or a request under
    review by its own (and, once it became a registration, that registration)."""
    from app.api.approvals.models import RegistrationRequests
    from app.api.events.models import EventRegistrationsLink, Events

    caller.require_club()
    pk = _uuid(registration_id)
    if pk is None:
        raise not_found("Registration")
    q = (
        select(EventRegistrationsLink).join(Events, Events.id == EventRegistrationsLink.event_id)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(EventRegistrationsLink.id == pk, Events.club_id == caller.club_id, Events.is_deleted == False)  # noqa: E712
        .execution_options(include_deleted=include_deleted, populate_existing=True)
    )
    reg = await caller.session.scalar(q)
    if reg is not None:
        return reg, None
    req = await caller.session.scalar(
        select(RegistrationRequests).join(Events, Events.id == RegistrationRequests.event_id)
        .where(RegistrationRequests.id == pk, Events.club_id == caller.club_id, Events.is_deleted == False)  # noqa: E712
        .execution_options(populate_existing=True)
    )
    if req is None:
        raise not_found("Registration")
    if req.registration_id is not None:
        reg = await caller.session.scalar(q.where(EventRegistrationsLink.id == req.registration_id).execution_options(include_deleted=True))
        reg = reg or await caller.session.scalar(
            select(EventRegistrationsLink).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
            .where(EventRegistrationsLink.id == req.registration_id).execution_options(include_deleted=True)
        )
        if reg is not None:
            return reg, req
    return None, req


async def registration_out(caller, reg=None, req=None, *, count: bool = True) -> dict:
    from app.api.events.models import Events

    session = caller.session
    if reg is not None:
        ctx = await shapes.RegistrationContext.load(session, [reg])
        ext = await ext_ids.of_object(session, caller.client_id, "registration", reg.id)
        if ext is None and req is not None:
            ext = await ext_ids.of_object(session, caller.client_id, "registration", req.id)
        out = shapes.registration_object(reg, reg.event, ctx, contact=caller.contact, external_id=ext)
        event_id = reg.event_id
    else:
        event = await session.get(Events, req.event_id)
        ext = await ext_ids.of_object(session, caller.client_id, "registration", req.id)
        out = shapes.request_object(req, event, contact=caller.contact, external_id=ext)
        event_id = req.event_id
    if count:
        await _count_attendee_reads(caller, 1, event_id=event_id, what="registration")
    return out


async def get_registration(caller, registration_id):
    reg, req = await _registration_of(caller, registration_id)
    return object_response(caller.request, await registration_out(caller, reg, req))


_REQUEST_STATUSES = {"pending_approval": ("pending", "approved"), "declined": ("declined",), "withdrawn": ("withdrawn",)}


async def list_registrations(caller, event_id, *, status, updated_since, external_id, limit, cursor):
    from app.api.approvals.models import RegistrationRequests
    from app.api.events.models import EventRegistrationsLink as Reg

    session = caller.session
    event = await _event_of(caller, event_id)
    limit = check_limit(limit)
    since = parse_time(updated_since, "updated_since") if updated_since else None
    if status in _REQUEST_STATUSES:
        R = RegistrationRequests
        q = select(R).where(R.event_id == event.id, R.status.in_(_REQUEST_STATUSES[status]), R.registration_id.is_(None))
        if since is not None:
            q = q.where(R.updated_at >= since)
        if external_id is not None:
            found = await ext_ids.find(session, caller.client_id, "registration", ext_ids.check(external_id))
            q = q.where(R.id == (_uuid(found) or uuid.UUID(int=0)))
        after = decode_cursor(cursor, ("dt", "str"))
        if after:
            q = q.where(tuple_(R.updated_at, R.id) > (after[0], _uuid(after[1])))
        rows = list((await session.scalars(q.order_by(R.updated_at, R.id).limit(limit + 1))).all())
        page, more = rows[:limit], len(rows) > limit
        exts = await ext_ids.of_objects(session, caller.client_id, "registration", [r.id for r in page])
        data = [shapes.request_object(r, event, contact=caller.contact, external_id=exts.get(str(r.id))) for r in page]
        await _count_attendee_reads(caller, len(data), event_id=event.id)
        return list_response(data, encode_cursor(page[-1].updated_at, str(page[-1].id)) if more and page else None)
    q = select(Reg).where(Reg.event_id == event.id).execution_options(include_deleted=True)
    if status == "cancelled":
        q = q.where(Reg.is_deleted == True)  # noqa: E712
    elif status in ("confirmed", "awaiting_payment"):
        q = q.where(Reg.is_deleted == False)  # noqa: E712
        if event.has_fee:
            paid_ext = select(PartnerExternalPayments.registration_id)
            owes = and_(Reg.is_paid == False, Reg.id.not_in(paid_ext))  # noqa: E712
            q = q.where(owes if status == "awaiting_payment" else ~owes)
        elif status == "awaiting_payment":
            q = q.where(Reg.id.is_(None))
    if external_id is not None:
        found = await ext_ids.find(session, caller.client_id, "registration", ext_ids.check(external_id))
        q = q.where(Reg.id == (_uuid(found) or uuid.UUID(int=0)))
    if since is not None:
        q = q.where(Reg.updated_at >= since)
    after = decode_cursor(cursor, ("dt", "str"))
    if after:
        q = q.where(tuple_(Reg.updated_at, Reg.id) > (after[0], _uuid(after[1])))
    rows = list((await session.scalars(q.order_by(Reg.updated_at, Reg.id).limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    ctx = await shapes.RegistrationContext.load(session, page)
    exts = await ext_ids.of_objects(session, caller.client_id, "registration", [r.id for r in page])
    data = [shapes.registration_object(r, event, ctx, contact=caller.contact, external_id=exts.get(str(r.id))) for r in page]
    await _count_attendee_reads(caller, len(data), event_id=event.id)
    return list_response(data, encode_cursor(page[-1].updated_at, str(page[-1].id)) if more and page else None)


async def _guest_for(session, *, name: str, email: str, phone: Optional[str]):
    """The account a club-entered address belongs to, or a guest made for it:
    as the dashboard's "add a guest by hand" finds or makes one."""
    from app.api.users import service as user_service
    from app.api.users.models import UserTypes

    user = await user_service.get_non_club_user_by_email(session, email)
    if user is None:
        user = await user_service.create_user(session=session, full_name=name, email=email, phone=phone, provider="email", user_type=UserTypes.guest)
    if user is None:
        raise PartnerError("VALIDATION", "The guest account could not be made.")
    return user


async def _mark_paid_externally(session, event, reg, payment, client_id: str) -> None:
    """Paid outside MyOtherApp: the paid place taken as a capture takes it
    (the event's row, then its ticket type's room), the registration marked
    paid at nothing, the partner's amount kept beside it. Commits."""
    from app.api.payments import capacity
    from app.api.ticketing import places
    from app.api.ticketing import waitlist

    if not await capacity.take_paid_place(session, event):
        raise places.full(waitlist=await waitlist.joinable(session, event), max_participants=event.max_participants)
    if not await places.capture_room(session, event, reg):
        raise places.full(waitlist=await waitlist.joinable(session, event), max_participants=event.max_participants)
    reg.is_paid = True
    from decimal import Decimal

    reg.paid_amount = Decimal("0.00")
    session.add(PartnerExternalPayments(
        registration_id=reg.id, club_id=event.club_id, event_id=event.id, amount_paise=int(payment.amount),
        currency=payment.currency, reference=(payment.reference or "").strip()[:128] or None, client_id=client_id,
    ))
    await session.commit()


async def create_registration(caller, event_id, body):
    from app.api.approvals import service as approvals
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.events.registration import service as registration_service
    from app.api.users import service as user_service

    session = caller.session
    event = await _event_of(caller, event_id)
    external = ext_ids.check(body.external_id)
    person = body.person
    email, name = str(person.email).strip(), person.name.strip()
    phone = (person.phone or "").strip() or None
    if await user_service.email_has_club_account(session, email):
        raise PartnerError("VALIDATION", "This email belongs to a club's account; register the person's own email.", details={"fields": {"person.email": "A club's account."}})
    payment = body.payment
    if event.requires_approval:
        if payment is not None:
            raise PartnerError("VALIDATION", "This event reviews every registration, so a payment can't come with one.", details={"fields": {"payment": "Not with a reviewed event."}})
        reservation = await ext_ids.reserve(session, caller, "registration", external)
        try:
            done = await approvals.submit(
                session, event, full_name=name, email=email, phone=phone, user=None, answers=body.answers or None,
                ticket_type_id=int(body.ticket_type_id) if body.ticket_type_id not in (None, "") else None,
            )
        except Exception:
            await ext_ids.release(session, reservation)
            raise
        if not done.created:
            await ext_ids.release(session, reservation)
            raise PartnerError("ALREADY_REGISTERED", "This email already has a registration waiting for review on this event.", details={"id": str(done.request.id)})
        await ext_ids.bind(session, reservation, done.request.id)
        await _record(caller, "partner.registration.request", entity_type="event", entity_id=event.id, summary=f"Registration sent for review on {event.name}", request_id=str(done.request.id))
        await session.commit()
        return object_response(caller.request, await registration_out(caller, None, done.request, count=False), status=202)
    ticket_type_id = None
    if body.ticket_type_id not in (None, ""):
        if not str(body.ticket_type_id).isdigit():
            raise PartnerError("VALIDATION", "ticket_type_id is one of the event's ticket types' ids.", details={"fields": {"ticket_type_id": "Not a ticket type."}})
        ticket_type_id = int(body.ticket_type_id)
    if event.has_fee:
        from app.api.ticketing import quote as ticketing_quote

        priced = await ticketing_quote.quote(
            session, event, ticket_type_id=ticket_type_id, email=email, phone=phone, organiser=True, lock=False, early_capacity=False,
        )
        owes = priced.amount > 0
        if owes and payment is None:
            raise PartnerError(
                "PAYMENT_REQUIRED", "This is a paid event: the person pays on its page, or, if the club allows it, send the payment taken outside MyOtherApp.",
                details={"checkout_url": public_urls.event_url(event.slug), "amount": shapes.paise_of(priced.amount), "currency": "INR",
                         "external_payments_allowed": bool(event.external_payments_allowed)},
            )
        if payment is not None and not owes:
            raise PartnerError("VALIDATION", "This ticket costs nothing, so no payment comes with it.", details={"fields": {"payment": "Nothing to pay."}})
        if payment is not None and not event.external_payments_allowed:
            raise PartnerError(
                "EXTERNAL_PAYMENTS_OFF", "This event doesn't accept registrations paid outside MyOtherApp. Only the club can turn that on, in the dashboard.",
                details={"checkout_url": public_urls.event_url(event.slug)},
            )
        if payment is not None:
            from app.api.payments import capacity

            if await capacity.full(session, event):
                from app.api.ticketing import places, waitlist

                raise places.full(waitlist=await waitlist.joinable(session, event), max_participants=event.max_participants)
    elif payment is not None:
        raise PartnerError("VALIDATION", "This event is free, so no payment comes with a registration.", details={"fields": {"payment": "The event is free."}})
    reservation = await ext_ids.reserve(session, caller, "registration", external)
    try:
        user = await _guest_for(session, name=name, email=email, phone=phone)
        data = await registration_service.register_event(
            session=session, full_name=name, email=email, phone=phone, user_id=user.id, event_id=event.id,
            additional_details=body.answers or None, notify=bool(body.send_confirmation), by_organiser=True, ticket_type_id=ticket_type_id,
        )
    except CustomHTTPException as e:
        await ext_ids.release(session, reservation)
        if e.error_code == "ALREADY_REGISTERED":
            existing = await registration_service.live_registration_for_email(session, event.id, email)
            raise PartnerError("ALREADY_REGISTERED", "This email is already registered for this event.", details={"id": str(existing.id) if existing else None})
        raise
    except Exception:
        await ext_ids.release(session, reservation)
        raise
    reg_id = data["event_registration_id"]
    reg = await session.scalar(
        select(EventRegistrationsLink).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club))
        .where(EventRegistrationsLink.id == reg_id).execution_options(populate_existing=True)
    )
    if payment is not None and not reg.is_paid:
        try:
            await _mark_paid_externally(session, event, reg, payment, caller.client_id)
        except CustomHTTPException:
            await session.rollback()
            reg = await session.get(EventRegistrationsLink, reg_id)
            await registration_service.take_off_list(session, reg)
            await ext_ids.release(session, reservation)
            raise
        if body.send_confirmation:
            from app.api.deliveries import service as deliveries

            try:
                await deliveries.send_ticket(session, event, reg)
            except Exception:  # noqa: BLE001 - the ticket can be sent again from the dashboard
                logger.exception("the ticket for registration %s could not be queued", reg.id)
                await session.rollback()
    await ext_ids.bind(session, reservation, reg.id)
    await _record(caller, "partner.registration.create", entity_type="event", entity_id=event.id,
                  summary=f"Registration added to {event.name}", registration_id=str(reg.id), paid_externally=payment is not None)
    await session.commit()
    reg, _ = await _registration_of(caller, reg.id)
    if payment is not None:
        # Paid outside MyOtherApp after it was made: not a domain change webhooks_out sees.
        from app.api.partner.emitting import emit

        ctx = await shapes.RegistrationContext.load(session, [reg])
        await emit(caller, "registration.updated", shapes.registration_object(reg, reg.event, ctx, contact=True))
    return object_response(caller.request, await registration_out(caller, reg, None, count=False), status=201)


async def patch_registration(caller, registration_id, body):
    from app.api.events.registration import service as registration_service

    session = caller.session
    reg, req = await _registration_of(caller, registration_id, include_deleted=False)
    if reg is None:
        raise PartnerError("INVALID_STATE", "A registration under review is changed once it is confirmed.")
    check_if_match(caller.request, await registration_out(caller, reg, req, count=False))
    sent = body.model_dump(exclude_unset=True)
    if "external_id" in sent:
        await ext_ids.set_for(session, caller, "registration", reg.id, ext_ids.check(sent["external_id"]))
        await session.flush()
    if {"name", "answers"} & set(sent):
        await registration_service.update_registration(
            session, caller.owner.id, reg.event_id, str(reg.id), full_name=sent.get("name"),
            additional_details=sent.get("answers") if "answers" in sent else None,
        )
    await _record(caller, "partner.registration.update", entity_type="event", entity_id=reg.event_id, summary="Registration corrected",
                  registration_id=str(reg.id), fields=sorted(sent))
    await session.commit()
    reg, req = await _registration_of(caller, reg.id)
    return object_response(caller.request, await registration_out(caller, reg, req, count=False))


async def cancel_registration(caller, registration_id):
    from app.api.approvals import service as approvals
    from app.api.checkins.models import CheckIns
    from app.api.events.registration import service as registration_service

    session = caller.session
    reg, req = await _registration_of(caller, registration_id)
    if reg is None:
        if req.status in ("pending", "approved"):
            await approvals.withdraw(session, req.token)
            await _record(caller, "partner.registration.cancel", entity_type="event", entity_id=req.event_id, summary="Registration request withdrawn", request_id=str(req.id))
            await session.commit()
            _reg, req = await _registration_of(caller, req.id)
        return object_response(caller.request, await registration_out(caller, None, req, count=False))
    if reg.is_deleted:
        return object_response(caller.request, await registration_out(caller, reg, req, count=False))
    external = await session.get(PartnerExternalPayments, reg.id)
    if reg.is_paid and reg.payment_receipt:
        from app.api.mcp.oauth import dashboard_url

        raise PartnerError(
            "CANCEL_IN_DASHBOARD", "This registration was paid online, so cancelling it means a refund: do it in the dashboard.",
            details={"dashboard_url": dashboard_url(f"/events/{reg.event_id}/attendees")},
        )
    if reg.event.has_fee and reg.is_paid and external is None and shapes.paise_of(reg.paid_amount) > 0:
        raise PartnerError("CANCEL_IN_DASHBOARD", "This registration was paid at the door; cancel it in the dashboard.")
    if await session.scalar(select(CheckIns.id).where(CheckIns.registration_id == reg.id)) is not None:
        raise PartnerError("ALREADY_CHECKED_IN", "This person is already checked in. Undo the check-in first.")
    if external is not None:
        external.cancelled_at = now()
    await registration_service.take_off_list(session, reg)
    await _record(caller, "partner.registration.cancel", entity_type="event", entity_id=reg.event_id, summary="Registration cancelled", registration_id=str(reg.id))
    await session.commit()
    reg, req = await _registration_of(caller, reg.id)
    return object_response(caller.request, await registration_out(caller, reg, req, count=False))


# ─── Check-ins ───────────────────────────────────────────────────────────


def _gate_actor(caller):
    from app.api.checkins.gate_auth import GateActor

    return GateActor(kind="user", name=f"{caller.name or 'API'} (API)"[:60], user=caller.owner, full=True, can_check_in=True)


async def _registration_for_checkin(caller, body):
    from app.api.events.models import EventRegistrationsLink, Events

    given = [k for k in ("ticket_code", "registration_id", "external_id") if getattr(body, k)]
    if len(given) != 1:
        raise PartnerError("VALIDATION", "Send exactly one of ticket_code, registration_id or external_id.",
                           details={"fields": {k: "Exactly one." for k in ("ticket_code", "registration_id", "external_id")}})
    q = (
        select(EventRegistrationsLink).join(Events, Events.id == EventRegistrationsLink.event_id)
        .options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))
        .where(Events.club_id == caller.club_id, Events.is_deleted == False, EventRegistrationsLink.is_deleted == False)  # noqa: E712
    )
    if body.ticket_code:
        code = body.ticket_code.strip()
        reg = await caller.session.scalar(q.where(EventRegistrationsLink.ticket_id == code.upper())) or (
            await caller.session.scalar(q.where(EventRegistrationsLink.ticket_id == code)) if code != code.upper() else None
        )
    else:
        target = body.registration_id
        if body.external_id:
            target = await ext_ids.find(caller.session, caller.client_id, "registration", ext_ids.check(body.external_id))
        pk = _uuid(target)
        reg = await caller.session.scalar(q.where(EventRegistrationsLink.id == pk)) if pk else None
        if reg is None and pk is not None:
            from app.api.approvals.models import RegistrationRequests

            req = await caller.session.get(RegistrationRequests, pk)
            if req is not None and req.registration_id is not None:
                reg = await caller.session.scalar(q.where(EventRegistrationsLink.id == req.registration_id))
            elif req is not None:
                raise PartnerError("INVALID_STATE", "This registration is still waiting for review.")
    if reg is None:
        raise not_found("Registration")
    return reg


_REFUSED = {
    "REVOKED": ("TICKET_REVOKED", "This ticket was revoked by the club."),
    "EVENT_NOT_OPEN": ("EVENT_NOT_OPEN", "The door isn't open for this event (a draft, or the door is closed)."),
    "UNPAID": ("PAYMENT_REQUIRED", "This ticket isn't paid for yet."),
    "INVALID": ("NOT_FOUND", "No such ticket on this club's events."),
    "WRONG_EVENT": ("NOT_FOUND", "No such ticket on this event."),
}


async def create_checkin(caller, body):
    from app.api.checkins import service as checkins_service
    from app.api.checkins.models import CheckInVia

    caller.require_club()
    session = caller.session
    reg = await _registration_for_checkin(caller, body)
    event = reg.event
    scanned = parse_time(body.scanned_at, "scanned_at") if body.scanned_at else None
    result = await checkins_service.scan(
        session, event, registration=reg, door_label=(body.gate or "").strip() or None, client_ref=f"api-{uuid.uuid4().hex}",
        scanned_at=scanned, actor=_gate_actor(caller), via=CheckInVia.api,
    )
    outcome = result.get("outcome")
    if outcome not in ("CHECKED_IN", "ALREADY_CHECKED_IN"):
        code, message = _REFUSED.get(outcome, ("INVALID_STATE", "This person can't be checked in."))
        raise PartnerError(code, result.get("error") or message, details={"outcome": outcome})
    row = await checkins_service.check_in_of(session, reg.id)
    duplicate = outcome == "ALREADY_CHECKED_IN"
    if not duplicate:
        await _record(caller, "partner.checkin.create", entity_type="event", entity_id=event.id, summary="Checked in",
                      registration_id=str(reg.id), gate=body.gate, device=body.device)
        await session.commit()
    return JSONResponse(shapes.checkin_object(row, duplicate=duplicate), status_code=200 if duplicate else 201)


async def delete_checkin(caller, checkin_id):
    from app.api.checkins import service as checkins_service
    from app.api.checkins.models import CheckIns
    from app.api.events.models import Events

    caller.require_club()
    session = caller.session
    pk = _uuid(checkin_id)
    row = await session.scalar(
        select(CheckIns).join(Events, Events.id == CheckIns.event_id).where(CheckIns.id == pk, Events.club_id == caller.club_id)
    ) if pk else None
    if row is None:
        raise not_found("Check-in")
    event = await _event_of(caller, row.event_id)
    out = shapes.checkin_object(row)
    await checkins_service.undo(session, event, row.registration_id, _gate_actor(caller))
    await _record(caller, "partner.checkin.delete", entity_type="event", entity_id=event.id, summary="Check-in undone", registration_id=str(row.registration_id))
    await session.commit()
    out["deleted"] = True
    return JSONResponse(out)


async def list_checkins(caller, event_id, *, updated_since, limit, cursor):
    from app.api.checkins.models import CheckIns

    session = caller.session
    event = await _event_of(caller, event_id)
    limit = check_limit(limit)
    q = select(CheckIns).where(CheckIns.event_id == event.id)
    if updated_since:
        q = q.where(CheckIns.updated_at >= parse_time(updated_since, "updated_since"))
    after = decode_cursor(cursor, ("dt", "str"))
    if after:
        q = q.where(tuple_(CheckIns.updated_at, CheckIns.id) > (after[0], _uuid(after[1])))
    rows = list((await session.scalars(q.order_by(CheckIns.updated_at, CheckIns.id).limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    return list_response([shapes.checkin_object(r) for r in page], encode_cursor(page[-1].updated_at, str(page[-1].id)) if more and page else None)


# ─── Certificates ────────────────────────────────────────────────────────


async def list_certificates(caller, event_id, *, limit, cursor):
    from app.api.events.models import EventRegistrationsLink as Reg

    session = caller.session
    event = await _event_of(caller, event_id)
    limit = check_limit(limit)
    q = select(Reg).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.certificate_issued_at.isnot(None))  # noqa: E712
    after = decode_cursor(cursor, ("dt", "str"))
    if after:
        q = q.where(tuple_(Reg.certificate_issued_at, Reg.id) > (after[0], _uuid(after[1])))
    rows = list((await session.scalars(q.order_by(Reg.certificate_issued_at, Reg.id).limit(limit + 1))).all())
    page, more = rows[:limit], len(rows) > limit
    data = [shapes.certificate_object(r, contact=caller.contact) for r in page]
    await _count_attendee_reads(caller, len(data), event_id=event.id, what="certificates")
    return list_response(data, encode_cursor(page[-1].certificate_issued_at, str(page[-1].id)) if more and page else None)


def job_out(job: PartnerCertificateJobs) -> dict:
    return {
        "id": str(job.id), "object": "certificate_job", "event_id": job.event_ref, "status": job.status,
        "requested": job.request or {}, "result": job.result, "error": job.error, "livemode": bool(job.livemode),
        "created_at": iso(job.created_at), "finished_at": iso(job.finished_at),
    }


async def issue_certificates(caller, event_id, body):
    from app.api.certificate_templates.models import CertificateTemplates
    from app.api.certificates import service as cert_service
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind
    from app.api.events.models import EventRegistrationsLink as Reg

    session = caller.session
    event = await _event_of(caller, event_id)
    if bool(body.registration_ids) == bool(body.all_checked_in):
        raise PartnerError("VALIDATION", "Send registration_ids, or all_checked_in: true (one of them).",
                           details={"fields": {"registration_ids": "One of the two.", "all_checked_in": "One of the two."}})
    ids = None
    if body.registration_ids:
        ids = [_uuid(i) for i in body.registration_ids]
        if any(i is None for i in ids):
            raise PartnerError("VALIDATION", "Every registration id is a registration's id.", details={"fields": {"registration_ids": "Not ids."}})
        found = set(await session.scalars(select(Reg.id).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.id.in_(ids))))  # noqa: E712
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise PartnerError("NOT_FOUND", "Some of those registrations are not on this event.", details={"missing": missing[:50]})
    if body.template_id is not None and body.template_id != event.certificate_template_id:
        template = await session.get(CertificateTemplates, body.template_id)
        if template is None or getattr(template, "is_deleted", False) or getattr(template, "club_id", None) not in (None, event.club_id):
            raise PartnerError("NOT_FOUND", "No such certificate template for this club.", details={"fields": {"template_id": "Not the club's."}})
        await _update(caller, event, {"certificate_template_id": template.id})
        event = await _event_of(caller, event.id)
    job = PartnerCertificateJobs(
        club_id=event.club_id, event_ref=str(event.id), client_id=caller.client_id, livemode=True, status="running",
        request={"registration_ids": [str(i) for i in ids] if ids else None, "all_checked_in": bool(body.all_checked_in),
                 "template_id": body.template_id, "send": bool(body.send)},
    )
    session.add(job)
    await session.commit()
    try:
        result = await cert_service.issue(session, event, "checked_in" if body.all_checked_in else "all", [], registration_ids=ids)
        queued = None
        if body.send:
            sent = await deliveries.queue(session, event, DeliveryKind.certificate, registration_ids=ids, only="unsent", send=True)
            await session.commit()
            queued = int(sent.get("queued") or 0)
        job = await session.get(PartnerCertificateJobs, job.id)
        job.status, job.finished_at = "completed", now()
        job.result = {**{k: int(v) for k, v in result.items()}, "emails_queued": queued}
    except CustomHTTPException as e:
        await session.rollback()
        job = await session.get(PartnerCertificateJobs, job.id)
        job.status, job.finished_at, job.error = "failed", now(), (e.message or "")[:500]
    except Exception:
        # Not left "running" for ever: failed, and the 500 says why to us.
        await session.rollback()
        failed = await session.get(PartnerCertificateJobs, job.id)
        failed.status, failed.finished_at, failed.error = "failed", now(), "Something went wrong on our side."
        await session.commit()
        raise
    await _record(caller, "partner.certificates.issue", entity_type="event", entity_id=event.id, summary="Certificates issued", job_id=str(job.id))
    await session.commit()
    return JSONResponse(job_out(job), status_code=202)


async def get_certificate_job(caller, job_id):
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind

    caller.require_club()
    pk = _uuid(job_id)
    job = await caller.session.get(PartnerCertificateJobs, pk) if pk else None
    if job is None or job.club_id != caller.club_id or not job.livemode:
        raise not_found("Certificate job")
    out = job_out(job)
    if job.status == "completed" and (job.request or {}).get("send"):
        event = await _event_of(caller, job.event_ref)
        out["deliveries"] = (await deliveries.summary(caller.session, event, DeliveryKind.certificate)).get("channels")
    return JSONResponse(out)


async def get_certificate(caller, number: str):
    from app.api.certificates import service as cert_service

    caller.require_club()
    reg = await cert_service.certificate_by_number(caller.session, number)
    if reg is None or reg.event is None or reg.event.club_id != caller.club_id:
        raise not_found("Certificate")
    verdict = await cert_service.verify(caller.session, number)
    verify, pdf = shapes.certificate_urls(reg)
    out = {
        "number": verdict.get("number") or number, "object": "certificate_verification", "valid": bool(verdict.get("valid")),
        "reason": verdict.get("reason"), "registration_id": str(reg.id), "event_id": str(reg.event_id), "event_name": reg.event.name,
        "issued_at": iso(reg.certificate_issued_at), "revoked_at": iso(reg.certificate_revoked_at),
        "verify_url": verify, "pdf_url": pdf if verdict.get("valid") else None, "livemode": True,
    }
    if caller.contact:
        out["name"] = reg.full_name
        await _count_attendee_reads(caller, 1, event_id=reg.event_id, what="certificate")
    return JSONResponse(out)

