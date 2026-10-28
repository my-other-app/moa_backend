"""An event's sponsors and partners (docs/EVENT_PARTNERS.md).

Each row saves on its own, as the club profile's partners do: made, corrected,
given a logo, moved within its kind or removed, one request each. At most
MAX_PER_EVENT live rows an event; a removed row is soft-deleted and never
shown or counted again."""
import asyncio
import logging
import posixpath
from datetime import datetime, timezone
from typing import Optional

from fastapi import UploadFile
from sqlalchemy import func, select

from app.api.clubs.models import ClubPartners
from app.api.event_partners.models import KINDS, LABEL_MAX, MAX_PER_EVENT, EventPartners
from app.api.events.models import Events
from app.core.storage import objects
from app.core.storage.fields import stored_key, upload
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


def out(p: EventPartners) -> dict:
    """The row as the dashboard and /events/info read it."""
    return dict(
        id=p.id, kind=p.kind, name=p.name, label=p.label, link=p.link, position=p.position,
        logo=p.logo if isinstance(p.logo, dict) else None, club_partner_id=p.club_partner_id,
    )


def _logo_url(logo) -> Optional[str]:
    if not isinstance(logo, dict):
        return None
    for size in ("medium", "large", "original", "thumbnail"):
        if logo.get(size):
            return logo[size]
    return None


def page_out(p: EventPartners) -> dict:
    """The row as the public event page reads it: one logo URL."""
    return dict(id=p.id, kind=p.kind, name=p.name, label=p.label, link=p.link, logo=_logo_url(p.logo))


def _live(event_id: int):
    return select(EventPartners).where(EventPartners.event_id == event_id, EventPartners.is_deleted == False)  # noqa: E712


async def list_rows(session, event_id: int) -> list[EventPartners]:
    """Sponsors first, then partners, each in its order."""
    rows = (await session.scalars(_live(event_id).order_by(EventPartners.position, EventPartners.id))).all()
    return [r for kind in KINDS for r in rows if r.kind == kind]


async def split(session, event_id: int, shape=out) -> dict:
    """{"sponsors": [...], "partners": [...]}, each in its order, in `shape`."""
    rows = await list_rows(session, event_id)
    return dict(
        sponsors=[shape(r) for r in rows if r.kind == "sponsor"],
        partners=[shape(r) for r in rows if r.kind == "partner"],
    )


async def row_for(session, event: Events, partner_id: int) -> EventPartners:
    p = await session.scalar(_live(event.id).where(EventPartners.id == partner_id))
    if p is None:
        raise CustomHTTPException(404, message="Sponsor or partner not found", error_code="EVENT_PARTNER_NOT_FOUND")
    return p


async def _hold(session, event: Events) -> None:
    """Lock the event's row until commit, so two adds at once can neither
    pass the cap together nor take the same place in the order."""
    await session.execute(select(Events.id).where(Events.id == event.id).with_for_update())


async def _room(session, event: Events, adding: int) -> None:
    have = await session.scalar(select(func.count(EventPartners.id)).where(EventPartners.event_id == event.id, EventPartners.is_deleted == False))  # noqa: E712
    if (have or 0) + adding > MAX_PER_EVENT:
        left = max(0, MAX_PER_EVENT - (have or 0))
        raise CustomHTTPException(
            422,
            message=(
                f"An event can list up to {MAX_PER_EVENT} sponsors and partners."
                + (f" There is room for {left} more." if left else " Remove one to add another.")
            ),
            error_code="EVENT_PARTNERS_LIMIT",
        )


async def _next_position(session, event_id: int, kind: str) -> int:
    last = await session.scalar(
        select(func.max(EventPartners.position)).where(
            EventPartners.event_id == event_id, EventPartners.kind == kind, EventPartners.is_deleted == False,  # noqa: E712
        )
    )
    return (last if last is not None else -1) + 1


async def create(session, event: Events, data: dict) -> dict:
    await _hold(session, event)
    await _room(session, event, 1)
    p = EventPartners(
        event_id=event.id, kind=data["kind"], name=data["name"], label=data.get("label"), link=data.get("link"),
        position=await _next_position(session, event.id, data["kind"]),
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return out(p)


async def update(session, event: Events, p: EventPartners, data: dict) -> dict:
    """Only what was sent. A new kind puts the row at the end of that kind."""
    if data.get("name") is not None:
        p.name = data["name"]
    if "label" in data:
        p.label = data["label"]
    if "link" in data:
        p.link = data["link"]
    kind = data.get("kind")
    if kind is not None and kind != p.kind:
        await _hold(session, event)
        p.position = await _next_position(session, event.id, kind)
        p.kind = kind
    await session.commit()
    await session.refresh(p)
    return out(p)


async def set_logo(session, p: EventPartners, logo: UploadFile) -> dict:
    old = p.logo if isinstance(p.logo, dict) else None
    p.logo = await upload(EventPartners.logo, {"bytes": await logo.read(), "filename": logo.filename})
    await session.commit()
    await session.refresh(p)
    # The old picture goes once the new one is saved; its files are this row's alone.
    if old is not None and hasattr(old, "delete"):
        old.delete()
    return out(p)


async def remove(session, p: EventPartners) -> None:
    p.is_deleted = True
    p.deleted_at = datetime.now(timezone.utc)
    await session.commit()


async def reorder(session, event: Events, kind: str, ids: list[int]) -> list[dict]:
    """Every live row of `kind`, in the order given: positions 0, 1, 2..."""
    await _hold(session, event)
    rows = {r.id: r for r in (await session.scalars(_live(event.id).where(EventPartners.kind == kind))).all()}
    if len(set(ids)) != len(ids) or set(ids) != set(rows):
        noun = "sponsor" if kind == "sponsor" else "partner"
        raise CustomHTTPException(
            422, message=f"List every {noun} of this event once, in the new order. Reload and try again.", error_code="EVENT_PARTNERS_ORDER",
        )
    for i, pid in enumerate(ids):
        rows[pid].position = i
    await session.commit()
    return [out(r) for r in await list_rows(session, event.id)]


# ─── From the club's partners ────────────────────────────────────────────


def _filename(key: str) -> str:
    return posixpath.basename(key) or "logo.png"


async def _copied_logo(club_partner: ClubPartners) -> Optional[str]:
    """A copy of the club partner's logo, stored as this column's own file
    (its own sizes, its own key), so replacing or deleting either picture
    never breaks the other. None when there is none or it cannot be read."""
    key = stored_key(club_partner.logo)
    if not key:
        return None
    try:
        data = await asyncio.to_thread(objects.fetch, key)
        if not data:
            return None
        return await upload(EventPartners.logo, {"bytes": data, "filename": _filename(key)})
    except Exception:  # noqa: BLE001 -- the partner is still added, without a logo
        logger.warning("could not copy club partner %s's logo", club_partner.id, exc_info=True)
        return None


async def from_club(session, event: Events, kind: str, club_partner_ids: list[int], club_id: int | None = None) -> dict:
    """The club's partners, as rows of `kind` at the end of it, in the order
    given. One already on the event (of either kind) is skipped."""
    wanted = list(dict.fromkeys(club_partner_ids))
    found = {
        p.id: p
        for p in (await session.scalars(
            select(ClubPartners).where(
                ClubPartners.id.in_(wanted), ClubPartners.club_id == (club_id or event.club_id), ClubPartners.is_deleted == False,  # noqa: E712
            )
        )).all()
    }
    missing = [i for i in wanted if i not in found]
    if missing:
        raise CustomHTTPException(
            422,
            message=f"Not one of your club's partners: {', '.join(str(i) for i in missing)}.",
            error_code="CLUB_PARTNER_NOT_FOUND",
        )
    await _hold(session, event)
    already = set((await session.scalars(
        select(EventPartners.club_partner_id).where(
            EventPartners.event_id == event.id, EventPartners.club_partner_id.in_(wanted), EventPartners.is_deleted == False,  # noqa: E712
        )
    )).all())
    adding = [found[i] for i in wanted if i not in already]
    await _room(session, event, len(adding))
    # The pictures first (off the loop), so the rows go in at once.
    logos = await asyncio.gather(*(_copied_logo(cp) for cp in adding))
    position = await _next_position(session, event.id, kind)
    made: list[EventPartners] = []
    for i, (cp, logo) in enumerate(zip(adding, logos)):
        label = (cp.role or "").strip()[:LABEL_MAX].strip() or None
        p = EventPartners(
            event_id=event.id, kind=kind, name=cp.name, label=label, link=None,
            position=position + i, club_partner_id=cp.id, logo=logo,
        )
        session.add(p)
        made.append(p)
    await session.commit()
    for p in made:
        await session.refresh(p)
    return dict(added=[out(p) for p in made], skipped=[i for i in wanted if i in already])
