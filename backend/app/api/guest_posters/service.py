"""The guest's side: who may see a poster, which moment and design apply,
their photo and framing, and what they asked to be drawn (CONTRACT.md §6).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from limits import parse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.events.models import EventRegistrationsLink, Events
from app.api.guest_posters import designs, links as poster_links, moments as mom, photos, render
from app.api.guest_posters import values as poster_values
from app.api.guest_posters.models import GuestPosters, PosterSettings
from app.config import settings
from app.core import public_urls
from app.core.layout_v2 import poster
from app.core.mail import links as signing
from app.core.rate_limit import limiter
from app.db.listeners import INCLUDE_DELETED
from app.response import CustomHTTPException

# Per guest (registration). CONTRACT.md §11.
RENDERS = ("6/minute", "30/hour")
PHOTOS = ("10/hour",)
FRAMINGS = ("60/hour",)
EVENTS = ("60/hour",)
ACTIONS = {"download": "downloads", "share": "shares", "copy_caption": "copies", "copy_link": "copies"}


# ─── Who may ────────────────────────────────────────────────────────────────


def key_for(ticket_id: str) -> str:
    """The poster link's signature for a ticket: what `?s=` carries."""
    return signing.sign(f"poster:{ticket_id}")


def key_ok(ticket_id: str, s: Optional[str]) -> bool:
    return bool(s) and signing.verify(f"poster:{ticket_id}", s)


def poster_page_url(ticket_id: str) -> str:
    """The guest's poster page, signed: the email token {{poster_url}}."""
    return public_urls.site_url(f"/posters/{ticket_id}?s={key_for(ticket_id)}")


async def registration_by_ticket(session, ticket_id: str) -> Optional[EventRegistrationsLink]:
    """The registration a ticket id names, removed or not (so the page can say why)."""
    from app.api.users.models import UserProfiles, Users

    if not ticket_id or len(ticket_id) > 60:
        return None
    return await session.scalar(
        select(EventRegistrationsLink)
        .options(
            joinedload(EventRegistrationsLink.event).joinedload(Events.club),
            joinedload(EventRegistrationsLink.event).selectinload(Events.cohost_clubs),
            joinedload(EventRegistrationsLink.user).joinedload(Users.profile).joinedload(UserProfiles.org),
        )
        .where(EventRegistrationsLink.ticket_id == ticket_id)
        # Removed ones too, so the page can say the ticket was given back.
        .execution_options(**{INCLUDE_DELETED: True})
    )


async def holder(session, ticket_id: str, user, s: Optional[str]) -> EventRegistrationsLink:
    """The registration, once the caller is its holder: their account's
    token, or the poster link's signature. 404, then 403."""
    registration = await registration_by_ticket(session, ticket_id)
    if registration is None or registration.event is None:
        raise CustomHTTPException(404, message="No such ticket.", error_code="TICKET_NOT_FOUND")
    if user is not None and getattr(user, "id", None) == registration.user_id:
        return registration
    if key_ok(ticket_id, s):
        return registration
    raise CustomHTTPException(403, message="This poster is its guest's own.", error_code="POSTER_FORBIDDEN")


# ─── Limits ─────────────────────────────────────────────────────────────────


@lru_cache(maxsize=None)
def _item(rate: str):
    return parse(rate)


def spend(rates: tuple, scope: str, who: str, *, quiet: bool = False) -> bool:
    """Count one against each rate. Over: 429 with retry_after (or False when `quiet`)."""
    if not limiter.enabled:
        return True
    for rate in rates:
        item = _item(rate)
        if limiter.limiter.hit(item, scope, who):
            continue
        if quiet:
            return False
        try:
            reset_at, _ = limiter.limiter.get_window_stats(item, scope, who)
            wait = max(1, int(reset_at - time.time()) + 1)
        except Exception:  # noqa: BLE001
            wait = 60
        raise CustomHTTPException(429, message="Too many tries in a short time. Wait a moment and try again.",
                                  error_code="RATE_LIMITED", errors={"retry_after": wait})
    return True


# ─── The guest's standing ───────────────────────────────────────────────────


def platform_on() -> bool:
    return bool(getattr(settings, "POSTERS_ENABLED", True))


async def settings_row(session, event_id: int) -> Optional[PosterSettings]:
    return await session.get(PosterSettings, event_id)


def is_on(row: Optional[PosterSettings]) -> bool:
    return platform_on() and row is not None and bool(row.enabled)


def formats_of(row: Optional[PosterSettings]) -> list[str]:
    saved = row.formats if row is not None and isinstance(row.formats, list) else list(poster.ORDER)
    out = [f for f in poster.ORDER if f in saved]
    return out or list(poster.ORDER)


@dataclass
class Guest:
    registration: EventRegistrationsLink
    event: Events
    available: bool
    reason: Optional[str] = None
    moment: Optional[str] = None
    moments: dict = field(default_factory=dict)
    formats: list = field(default_factory=list)
    role: str = "attendee"
    values: dict = field(default_factory=dict)
    caption: str = ""
    share_url: str = ""
    rows: dict = field(default_factory=dict)


async def poster_row(session, registration, *, create: bool) -> Optional[GuestPosters]:
    """The guest's row; made the first time when `create` (a read otherwise: a poll writes nothing)."""
    def find():
        return session.scalar(
            select(GuestPosters).where(GuestPosters.registration_id == registration.id).execution_options(populate_existing=True)
        )

    found = await find()
    if found is not None or not create:
        return found
    await session.execute(
        pg_insert(GuestPosters.__table__).values(event_id=registration.event_id, registration_id=registration.id)
        .on_conflict_do_nothing(index_elements=["registration_id"])
    )
    return await find()


async def guest(session, registration, *, mint: bool = True) -> tuple[Guest, Optional[GuestPosters]]:
    """Where this guest stands, and (when posters are theirs now) their row
    and share link, made the first time."""
    from app.api.doc_presets.values import subject_of
    from app.core.layout_v2 import vocabulary as tk

    event = registration.event
    row = await settings_row(session, event.id)
    if not is_on(row):
        return Guest(registration, event, False, mom.OFF), None
    moments = mom.moments_of(row)
    st = await mom.standing(session, event, registration, moments)
    formats = formats_of(row)
    if st.moment is None:
        return Guest(registration, event, False, st.reason, moments=moments, formats=formats), None
    mine = await poster_row(session, registration, create=mint)
    if mine is None:
        return Guest(registration, event, False, mom.NOT_SENT, moments=moments, formats=formats), None
    link = await poster_links.guest_link(session, event, mine, registration.full_name) if mint else None
    share_url = poster_links.short_url(link) if link is not None else ""
    subject = subject_of(registration, event)
    role = await poster_values.poster_role(session, event, registration)
    talk = await poster_values.talk_of(session, event, registration)
    ticket_type = await poster_values.ticket_type_of(session, registration)
    phrase = moments[st.moment]["phrase"]
    values = poster_values.poster_values(
        event, registration, subject, role=role, organisation=poster_values.organisation_of(event, registration, subject),
        ticket_type=ticket_type, talk=talk, moment_phrase=phrase, share_url=share_url,
    )
    caption = tk.fill(moments[st.moment]["caption"], values, poster.KIND).strip()
    rows = await designs.rows_of(session, event.id)
    return Guest(registration, event, True, None, st.moment, moments, formats, role, values, caption, share_url, rows), mine


async def preview_values(session, registration) -> tuple[dict, Optional[GuestPosters]]:
    """A registration's poster values for the organiser's preview, whatever
    its standing: the moment that applies (else the first switched on), its
    link when it has one (else a sample). Nothing is made."""
    from app.api.doc_presets.values import subject_of
    from app.api.links.models import TrackingLinks

    event = registration.event
    row = await settings_row(session, event.id)
    moments = mom.moments_of(row)
    st = await mom.standing(session, event, registration, moments)
    key = st.moment or next((k for k in mom.MOMENTS if moments[k]["enabled"]), "before")
    mine = await poster_row(session, registration, create=False)
    link = await session.get(TrackingLinks, mine.tracking_link_id) if mine is not None and mine.tracking_link_id else None
    subject = subject_of(registration, event)
    role = await poster_values.poster_role(session, event, registration)
    values = poster_values.poster_values(
        event, registration, subject, role=role, organisation=poster_values.organisation_of(event, registration, subject),
        ticket_type=await poster_values.ticket_type_of(session, registration), talk=await poster_values.talk_of(session, event, registration),
        moment_phrase=moments[key]["phrase"], share_url=poster_links.short_url(link) if link is not None else poster_values.SAMPLE_SHARE_URL,
    )
    return values, mine


async def prepared(session, g: Guest, mine: GuestPosters, fmt: str) -> render.Prepared:
    resolved = designs.resolve_from(g.rows, designs.chain(g.role, getattr(g.registration, "ticket_type_id", None)), fmt)
    return render.prepare(g.event, mine, resolved, fmt, g.values)


async def for_job(session, poster_id) -> Optional[tuple[Guest, GuestPosters]]:
    if not poster_id:
        return None
    mine = await session.get(GuestPosters, poster_id)
    if mine is None:
        return None
    registration = await session.scalar(select(EventRegistrationsLink.ticket_id).where(EventRegistrationsLink.id == mine.registration_id))
    registration = await registration_by_ticket(session, registration) if registration else None
    if registration is None:
        return None
    g, again = await guest(session, registration, mint=True)
    return g, (again or mine)


def needs(g: Guest) -> None:
    """409 when the guest has no poster now (posters off, ticket not sent...)."""
    if not g.available:
        raise CustomHTTPException(409, message=_REASONS.get(g.reason, "Posters are not available for this ticket."),
                                  error_code="POSTER_NOT_AVAILABLE", errors={"reason": g.reason})


_REASONS = {
    mom.OFF: "Posters are not on for this event.",
    mom.EVENT_CANCELLED: "This event was cancelled.",
    mom.CANCELLED: "This ticket was given back.",
    mom.REVOKED: "This ticket was revoked.",
    mom.NOT_SENT: "Your poster is ready once your ticket is sent.",
    mom.MOMENT_OFF: "Posters are not on right now.",
}


# ─── What the page reads ────────────────────────────────────────────────────


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def api_url(path: str) -> str:
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1{path}"


def _render_out(g: Guest, mine: GuestPosters, p: render.Prepared, key: str) -> dict:
    state = render.status(mine, p)
    out = {"status": state, "fingerprint": p.fingerprint, "image_url": None}
    if state == "ready":
        out["image_url"] = api_url(f"/posters/{g.registration.ticket_id}/image/{p.fmt}?s={key}&v={p.fingerprint}")
    return out


async def availability(session, g: Guest, mine: Optional[GuestPosters]) -> dict:
    if not g.available:
        if g.reason == mom.OFF:
            return {"available": False, "reason": mom.OFF}
        return {"available": False, "reason": g.reason, "message": _REASONS.get(g.reason),
                "event": _event_out(g.event), "guest": _guest_out(g.registration, None)}
    ticket_id = g.registration.ticket_id
    key = key_for(ticket_id)
    formats = []
    for fmt in g.formats:
        p = await prepared(session, g, mine, fmt)
        w, h = poster.FORMATS[fmt]
        formats.append({
            "key": fmt, "label": poster.LABELS[fmt], "width": w, "height": h, "variant": p.variant,
            "frame": p.frame, "framing": p.framing, "render": _render_out(g, mine, p, key),
        })
    photo = None
    if mine.photo_key:
        photo = {"width": mine.photo_width, "height": mine.photo_height, "uploaded_at": _iso(mine.photo_at),
                 "url": api_url(f"/posters/{ticket_id}/photo?s={key}&v={(mine.photo_sha or '')[:12]}")}
    return {
        "available": True, "reason": None, "key": key,
        "event": _event_out(g.event),
        "guest": {**_guest_out(g.registration, g.values), "role": g.role, "role_label": poster_values.role_label(g.role)},
        "moment": {"key": g.moment, "phrase": g.moments[g.moment]["phrase"], "caption": g.caption},
        "share_url": g.share_url,
        "photo": photo,
        "formats": formats,
        "limits": {"renders_per_hour": 30, "photos_per_hour": 10},
    }


def _event_out(event) -> dict:
    return {"id": event.id, "name": event.name, "slug": event.slug, "starts_at": _iso(event.event_datetime),
            "ends_at": _iso(mom.ends_at(event)), "url": public_urls.event_url(event.slug or event.id)}


def _guest_out(registration, values: Optional[dict]) -> dict:
    from app.api.doc_presets.values import _split, initials_of

    first, _ = _split(registration.full_name or "")
    return {"name": registration.full_name, "first_name": first, "initials": initials_of(registration.full_name or "")}


# ─── Photo and framing ──────────────────────────────────────────────────────


async def _forget_renders(session, mine: GuestPosters, event_id: int) -> list:
    keys = [s.get("key") for s in (mine.renders or {}).values() if isinstance(s, dict) and s.get("key")]
    mine.renders = {}
    return [k for k in keys if photos.is_ours(k, event_id, mine.registration_id)]


async def set_photo(session, g: Guest, mine: GuestPosters, raw: bytes) -> None:
    spend(PHOTOS, "posters:photo", str(mine.registration_id))
    art = await asyncio.to_thread(photos.clean, raw)
    key, sha = await asyncio.to_thread(photos.store, g.event.id, mine.registration_id, art)
    old = [mine.photo_key] if mine.photo_key and photos.is_ours(mine.photo_key, g.event.id, mine.registration_id) else []
    old += await _forget_renders(session, mine, g.event.id)
    mine.photo_key, mine.photo_sha, mine.photo_width, mine.photo_height = key, sha, art.width, art.height
    mine.photo_at = datetime.now(timezone.utc)
    mine.framing = {}
    mine.purged_at = None
    mine.updated_at = mine.photo_at
    await session.commit()
    if old:
        await asyncio.to_thread(photos.delete, old)


async def drop_photo(session, g_event_id: int, mine: GuestPosters) -> None:
    old = [mine.photo_key] if mine.photo_key and photos.is_ours(mine.photo_key, g_event_id, mine.registration_id) else []
    old += await _forget_renders(session, mine, g_event_id)
    mine.photo_key = mine.photo_sha = None
    mine.photo_width = mine.photo_height = None
    mine.photo_at = None
    mine.framing = {}
    mine.updated_at = datetime.now(timezone.utc)
    await session.commit()
    if old:
        await asyncio.to_thread(photos.delete, old)


async def set_framing(session, mine: GuestPosters, fmt: Optional[str], framing: dict) -> None:
    spend(FRAMINGS, "posters:framing", str(mine.registration_id))
    saved = dict(mine.framing or {})
    if fmt is None:
        saved = {"*": framing}
    else:
        saved[fmt] = framing
    mine.framing = saved
    mine.updated_at = datetime.now(timezone.utc)
    await session.commit()


async def ask_render(session, g: Guest, mine: GuestPosters, fmt: str) -> tuple[int, dict]:
    """(HTTP status, body) for POST .../render."""
    p = await prepared(session, g, mine, fmt)
    state = render.status(mine, p)
    body = {"format": fmt, "fingerprint": p.fingerprint, "caption": g.caption}
    if state == "ready":
        return 200, {**body, "status": "ready", "image_url": _render_out(g, mine, p, key_for(g.registration.ticket_id))["image_url"]}
    if state == "rendering":
        return 202, {**body, "status": "rendering", "retry_after_ms": 1500}
    if state == "failed":
        raise CustomHTTPException(503, message="Try again in a moment.", error_code="POSTER_RENDER_BUSY", errors={"retry_after": int(render.FAILED_FOR)})
    if p.layout is None:
        raise CustomHTTPException(503, message="Try again in a moment.", error_code="POSTER_RENDER_BUSY", errors={"retry_after": 60})
    spend(RENDERS, "posters:render", str(mine.registration_id))
    await render.queue(session, mine, p)
    await session.commit()
    return 202, {**body, "status": "rendering", "retry_after_ms": 1500}


async def render_status(session, g: Guest, mine: GuestPosters, fmt: str) -> dict:
    p = await prepared(session, g, mine, fmt)
    out = _render_out(g, mine, p, key_for(g.registration.ticket_id))
    body = {"format": fmt, "fingerprint": p.fingerprint, "caption": g.caption, "status": out["status"], "image_url": out["image_url"]}
    if out["status"] == "rendering":
        body["retry_after_ms"] = 1500
    return body


async def record(session, mine: GuestPosters, action: str) -> None:
    column = ACTIONS.get(action)
    if column is None or not spend(EVENTS, "posters:events", str(mine.registration_id), quiet=True):
        return
    await session.execute(
        update(GuestPosters).where(GuestPosters.id == mine.id).values(**{column: getattr(GuestPosters, column) + 1})
        .execution_options(synchronize_session=False)
    )
    await session.commit()
