"""Each guest's share link, and the one row they add up to (CONTRACT.md §8).

A guest's link is a tracking link of its own (kind `poster_guest`), so the
existing machinery does everything else: /l/<code> counts the click and
lands on the event page with `ref`, and a registration -- or a registration
request, on an event that confirms each one -- made with it is credited to
it (links.service.attribute_registration, approvals._tracking_link).

The organiser's Tracking links page never lists them one by one. Each event
with posters has one link of kind `poster`, "Guest posters", and the page
counts every guest link of the event as that one (`fold`). Per guest, the
numbers are on the Posters page.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import func, select, text

from app.api.links.models import TrackingLinks
from app.response import CustomHTTPException

ANCHOR = "poster"
GUEST = "poster_guest"
SOURCE, MEDIUM, CAMPAIGN = "poster", "guest_share", "im_attending"


async def _mint(session, *, club_id: int, event_id: int, kind: str, name: str) -> TrackingLinks:
    from app.api.links import service as links

    for _ in range(5):
        code = links.new_code()
        if await session.scalar(select(func.count()).where(TrackingLinks.code == code)) == 0:
            row = TrackingLinks(
                club_id=club_id, event_id=event_id, name=name[:80], code=code, kind=kind,
                utm_source=SOURCE, utm_medium=MEDIUM, utm_campaign=CAMPAIGN,
            )
            session.add(row)
            await session.flush()
            return row
    raise CustomHTTPException(500, message="Could not make a link code; try again.")


async def anchor_for(session, event) -> TrackingLinks:
    """The event's "Guest posters" row, made the first time. Flushed."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"poster_anchor:{event.id}"})
    found = await session.scalar(
        select(TrackingLinks).where(
            TrackingLinks.event_id == event.id, TrackingLinks.club_id == event.club_id,
            TrackingLinks.kind == ANCHOR, TrackingLinks.is_deleted == False,  # noqa: E712
        ).order_by(TrackingLinks.created_at, TrackingLinks.id).limit(1)
    )
    return found or await _mint(session, club_id=event.club_id, event_id=event.id, kind=ANCHOR, name="Guest posters")


async def guest_link(session, event, poster, full_name: str) -> TrackingLinks:
    """This guest's own link, made the first time (with the event's row). Flushed."""
    if poster.tracking_link_id is not None:
        row = await session.get(TrackingLinks, poster.tracking_link_id)
        if row is not None and not row.is_deleted:
            return row
    await anchor_for(session, event)
    row = await _mint(session, club_id=event.club_id, event_id=event.id, kind=GUEST, name=f"Poster · {full_name or 'guest'}")
    poster.tracking_link_id = row.id
    return row


def short_url(link: TrackingLinks) -> str:
    """The guest's link as the poster prints it: on the website's own address
    (POSTER_LINK_BASE, else PUBLIC_SITE_URL), whose /l/<code> forwards to the
    API's, which counts the click and lands on the event page."""
    from app.config import settings

    base = (getattr(settings, "POSTER_LINK_BASE", "") or settings.PUBLIC_SITE_URL).rstrip("/")
    return f"{base}/l/{link.code}"


async def fold(session, links: Iterable[TrackingLinks]) -> dict:
    """{guest link id: its event's "Guest posters" id}, for the anchors in
    `links`: what the Tracking links page counts as that one row."""
    anchors = {(l.club_id, l.event_id): l.id for l in links if l.kind == ANCHOR and l.event_id is not None}
    if not anchors:
        return {}
    rows = (await session.execute(
        select(TrackingLinks.id, TrackingLinks.club_id, TrackingLinks.event_id).where(
            TrackingLinks.kind == GUEST, TrackingLinks.event_id.in_({e for _, e in anchors}),
        )
    )).all()
    return {lid: anchors[(club, event)] for lid, club, event in rows if (club, event) in anchors}


async def guest_ids(session, anchor: TrackingLinks) -> list:
    """Every guest link the anchor stands for."""
    if anchor.kind != ANCHOR or anchor.event_id is None:
        return []
    return list((await session.scalars(
        select(TrackingLinks.id).where(TrackingLinks.kind == GUEST, TrackingLinks.event_id == anchor.event_id, TrackingLinks.club_id == anchor.club_id)
    )).all())
