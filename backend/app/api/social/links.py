"""The tracking link behind each platform's {link}, and what each brought.

One link per event per platform and medium, made the first time it is
needed: `social` for the club's own posts and the team's share kit
("Social · Instagram"), `share` for the attendees' share row ("Share ·
WhatsApp"). Always the host club's link, whoever posts (a co-host too):
a sign-up is credited only to a link of the event's own club
(links.service.attribute_registration). Every plan has them: they are the
share kit's, not the Tracking links feature's own. Found again by the tags
it carries, so a club that renames one keeps it; one the club retired is
made anew.
"""
from __future__ import annotations

from sqlalchemy import func, select, text

from app.api.events.models import EventRegistrationsLink
from app.api.links import service as links
from app.api.links.models import TrackingLinks
from app.response import CustomHTTPException

#: The UTM source each platform's link carries (a member's LinkedIn and a Page share one).
SOURCE = {"facebook": "facebook", "instagram": "instagram", "linkedin": "linkedin", "linkedin_page": "linkedin", "x": "x",
          "whatsapp": "whatsapp", "copy": "copy"}
LABEL = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "x": "X", "whatsapp": "WhatsApp", "copy": "Copied link"}
SOCIAL, SHARE = "social", "share"


async def link_for(session, event, platform: str, medium: str = SOCIAL) -> TrackingLinks:
    """The event's link for `platform`, made now if it has none. Flushed,
    not committed: the caller's commit keeps it."""
    source = SOURCE[platform]
    # Two requests making the event's first link at once make one.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"social_link:{event.id}:{source}:{medium}"})
    found = await session.scalar(
        select(TrackingLinks).where(
            TrackingLinks.club_id == event.club_id, TrackingLinks.event_id == event.id,
            TrackingLinks.utm_source == source, TrackingLinks.utm_medium == medium, TrackingLinks.is_deleted == False,  # noqa: E712
        ).order_by(TrackingLinks.created_at, TrackingLinks.id).limit(1)
    )
    if found is not None:
        return found
    name = f"{'Social' if medium == SOCIAL else 'Share'} · {LABEL[source]}"
    for _ in range(5):
        code = links.new_code()
        if await session.scalar(select(func.count()).where(TrackingLinks.code == code)) == 0:
            row = TrackingLinks(club_id=event.club_id, event_id=event.id, name=name, code=code, utm_source=source, utm_medium=medium, kind="custom")
            session.add(row)
            await session.flush()
            return row
    raise CustomHTTPException(500, message="Could not mint a link code; try again.")


def short_url(link: TrackingLinks) -> str:
    return links.short_url(link)


async def attribution(session, event_id: int) -> dict[str, int]:
    """Registrations each platform brought: those that came through the
    event's social and share links, by platform."""
    rows = (await session.execute(
        select(TrackingLinks.utm_source, func.count(EventRegistrationsLink.id))
        .join(EventRegistrationsLink, EventRegistrationsLink.tracking_link_id == TrackingLinks.id)
        .where(
            TrackingLinks.event_id == event_id, TrackingLinks.utm_medium.in_((SOCIAL, SHARE)),
            EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
        )
        .group_by(TrackingLinks.utm_source)
    )).all()
    return {source: int(n) for source, n in rows}
