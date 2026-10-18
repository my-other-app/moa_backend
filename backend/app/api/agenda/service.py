"""The run of show, and the programme it prints as."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select

from app.api.agenda.models import AgendaItems, AgendaTemplates
from app.api.events.documents import EVENT_TIMEZONE, in_layout_mode, layout_of, layout_sample_values, render_layout_document
from app.api.events.models import Events
from app.api.templates_resolve import resolve_template
from app.core.layout.engine import AgendaEntry, agenda_entries, sample_agenda
from app.core.layout.model import PAGE_PRESETS, TicketLayoutFields, dump, page_preset_of
from app.core.layout.starters import agenda_starter
from app.core.storage import objects
from app.response import CustomHTTPException


# ─── The run of show ──────────────────────────────────────────────────────


def _span(r: AgendaItems) -> tuple:
    end = r.ends_at or r.starts_at
    return r.starts_at, end


def parallel_ids(rows: list) -> set:
    """Two blocks are parallel when their times overlap — the same start, or
    one beginning before the other ends. A block with no end is a moment: it
    overlaps whatever is running at that moment."""
    out: set = set()
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a0, a1 = _span(rows[i])
            b0, b1 = _span(rows[j])
            if a0 == b0 or (a0 < b1 and b0 < a1):
                out.add(rows[i].id)
                out.add(rows[j].id)
    return out


def _speaker_photo_url(r: AgendaItems) -> Optional[str]:
    """The event page shows it, so a photo from the event's own uploads is a
    public picture: signed for a week (objects.public_url). The key is the
    organiser's to give and is not checked on save, so any other key keeps
    the hour it always had rather than a week of anyone's file."""
    key = r.speaker_photo_key
    if not key:
        return None
    from app.api.event_designs.router import _layout_prefix

    if r.event_id is not None and key.startswith(_layout_prefix(r.event_id)) and ".." not in key:
        return objects.public_url(key)
    return objects.presigned_url(key)


def item_out(r: AgendaItems, parallel: set) -> dict:
    return {
        "id": r.id, "event_id": r.event_id, "starts_at": r.starts_at, "ends_at": r.ends_at, "title": r.title, "description": r.description,
        "speaker": r.speaker, "speaker_photo_key": r.speaker_photo_key,
        "speaker_photo_url": _speaker_photo_url(r),
        "location": r.location, "sort_order": r.sort_order, "parallel": r.id in parallel,
    }


async def rows_for(session, event_id: int) -> list:
    return list((await session.scalars(
        select(AgendaItems).where(AgendaItems.event_id == event_id).order_by(AgendaItems.starts_at, AgendaItems.sort_order, AgendaItems.created_at)
    )).all())


async def list_items(session, event_id: int) -> list:
    rows = await rows_for(session, event_id)
    par = parallel_ids(rows)
    return [item_out(r, par) for r in rows]


async def _get(session, event: Events, item_id: UUID) -> AgendaItems:
    row = await session.get(AgendaItems, item_id)
    if row is None or row.event_id != event.id:
        raise CustomHTTPException(404, message="That agenda block could not be found.")
    return row


async def create_item(session, event: Events, body) -> dict:
    top = await session.scalar(select(func.max(AgendaItems.sort_order)).where(AgendaItems.event_id == event.id))
    row = AgendaItems(event_id=event.id, sort_order=(top if top is not None else -1) + 1, **body.model_dump())
    session.add(row)
    await session.commit()
    return next(i for i in await list_items(session, event.id) if i["id"] == row.id)


async def update_item(session, event: Events, item_id: UUID, body) -> dict:
    row = await _get(session, event, item_id)
    data = body.model_dump(exclude_unset=True)
    starts = data.get("starts_at", row.starts_at)
    ends = data["ends_at"] if "ends_at" in data else row.ends_at
    if ends is not None and ends <= starts:
        raise CustomHTTPException(422, message="A block must end after it starts.")
    for k, v in data.items():
        setattr(row, k, v)
    await session.commit()
    return next(i for i in await list_items(session, event.id) if i["id"] == row.id)


async def delete_item(session, event: Events, item_id: UUID) -> None:
    row = await session.get(AgendaItems, item_id)
    if row is None or row.event_id != event.id:
        return
    await session.delete(row)
    await session.commit()


async def entries_for(session, event: Events) -> list:
    """The run of show an agenda layer draws: the event's own sessions, or samples while it has none."""
    rows = await rows_for(session, event.id)
    if rows:
        return agenda_entries(rows)
    return sample_agenda(event.event_datetime, EVENT_TIMEZONE)


def entries_json(entries: list) -> list:
    """Entries as the editor's stage reads them."""
    return [
        {"startsAt": e.starts_at.isoformat(), "endsAt": e.ends_at.isoformat() if e.ends_at else None, "title": e.title, "speaker": e.speaker, "location": e.location}
        for e in entries
    ]


async def upcoming(session, event: Events, now: Optional[datetime] = None, limit: int = 6) -> list:
    """What a kiosk shows while a card prints: the sessions still to come today, the running one first."""
    now = now or datetime.now(timezone.utc)
    rows = await rows_for(session, event.id)
    par = parallel_ids(rows)
    live = [r for r in rows if (r.ends_at or r.starts_at + timedelta(minutes=30)) >= now - timedelta(minutes=10)]
    chosen = (live or rows)[:limit]
    return [
        {"starts_at": r.starts_at, "ends_at": r.ends_at, "title": r.title, "speaker": r.speaker, "location": r.location, "parallel": r.id in par}
        for r in chosen
    ]


# ─── The programme ────────────────────────────────────────────────────────


def agenda_default_config() -> dict:
    s = agenda_starter("screen")
    return {"layout": {**dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])), "version": 0}}


async def programme_style(session, event: Events) -> dict:
    template = await resolve_template(session, AgendaTemplates, event_id=event.id, club_id=event.club_id)
    style = dict(template.config or {}) if template is not None else {}
    if not in_layout_mode(style):
        style = {**agenda_default_config(), **{k: v for k, v in style.items() if k != "layout"}}
    return style


async def saved(session, event: Events) -> bool:
    """Whether the organiser has saved a programme — the kiosk shows it only then."""
    n = await session.scalar(select(func.count()).select_from(AgendaTemplates).where(AgendaTemplates.event_id == event.id, AgendaTemplates.is_deleted == False))  # noqa: E712
    return bool(n)


async def programme_pdf(session, event: Events, page_key: Optional[str] = None) -> bytes:
    """The programme as it stands, for the print run: on the page it was
    designed for, or fitted onto A4 — a screen-shaped design keeps its
    proportions in the middle of the sheet rather than being stretched to
    the paper. With no page asked for, a design already on paper prints as
    it is and a screen design goes to A4 in its own orientation."""
    style = await programme_style(session, event)
    layout = layout_of(style)
    on_paper = layout.page.height_mm is not None and not (page_preset_of(layout.page) or "").startswith("screen")
    key = page_key or (None if on_paper else ("a4_landscape" if layout.page.width_mm >= (layout.page.height_mm or 0) else "a4_portrait"))
    page = PAGE_PRESETS[key]["page"] if key else layout.page
    same = page.width_mm == layout.page.width_mm and page.height_mm == layout.page.height_mm
    values = layout_sample_values(event, "ticket")
    entries = await entries_for(session, event)
    # Drawn on the render pool, off the event loop (app.core.layout.pool).
    from app.core.layout import pool

    return await pool.run(
        render_layout_document, style, values, "", title=f"{event.name} · programme", agenda=entries,
        fit="stretch" if same else "contain", page=page, club=getattr(event, "club", None),
    )


__all__ = ["AgendaEntry"]
