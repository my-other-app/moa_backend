"""Published sites, kept current: every five minutes, one worker at a time.

A club's site is a stored snapshot, made again when something calls
`republish_for_club` -- an event saved through the API, a domain going live.
Nothing called it when:

- an event's start went by (the event stayed under "Upcoming" with a
  "Register" button), or the status clock moved it on;
- the club changed its name, logo, cover, about or colour, or its team or
  partners.

Rather than a hook in every one of those places (the status clock, the club
profile, the team and partner endpoints, all owned elsewhere), the sweep asks
the database which published pages are behind anything they show: a row they
are made from changed after the page was made, or an event's start passed
since. Those are made again, oldest first, a batch per run. Every row they
are made from carries `updated_at`, so nothing new is stored to know it.

And nightly, between 01:00 and 05:00 India time, every published site older
than STALE_AFTER is made again, in batches -- the follower count, the
"events held" number, a cover re-signed through the image route.

Why on the server rather than sorting "upcoming" from "past" in the browser:
search engines, link previews and visitors without JavaScript read the HTML
as stored, so the stored page has to be right; and the numbers and the
profile go stale too, which no amount of client-side sorting fixes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.clubs.models import ClubPartners, Clubs, ClubTeamMembers
from app.api.events.models import Events
from app.api.sites.models import ClubSites
from app.db import lock_keys

logger = logging.getLogger(__name__)

SITES_LOCK = lock_keys.SITES_SWEEP
#: Pages made again per run, at most; the rest wait for the next run.
BATCH = 20
#: A page older than this is made again overnight even with nothing changed.
STALE_AFTER = timedelta(hours=20)
#: India-time hours [start, end) in which the overnight refresh runs.
NIGHT = (1, 5)
_IST = timezone(timedelta(hours=5, minutes=30))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def behind(now: datetime):
    """SQL: this published page is behind something it shows."""
    made = ClubSites.published_at
    club = ClubSites.club_id
    return or_(
        exists().where(Clubs.id == club, Clubs.updated_at > made),
        exists().where(ClubTeamMembers.club_id == club, ClubTeamMembers.updated_at > made),
        exists().where(ClubPartners.club_id == club, ClubPartners.updated_at > made),
        # An event saved (published, moved by the status clock) since the
        # page was made, or one whose start has gone by since: it belongs
        # under "Past events" now. Within the club's live events, which the
        # partial index ix_events_club_start holds.
        exists().where(
            Events.club_id == club,
            Events.is_deleted == False,  # noqa: E712
            or_(Events.updated_at > made, and_(Events.event_datetime > made, Events.event_datetime <= now)),
        ),
        # One deleted since: the few deleted rows, by their own index.
        exists().where(Events.is_deleted == True, Events.club_id == club, Events.updated_at > made),  # noqa: E712
    )


def overnight(now: datetime) -> bool:
    return NIGHT[0] <= now.astimezone(_IST).hour < NIGHT[1]


async def due(session, now: datetime, limit: int = BATCH) -> list[int]:
    """The clubs whose published page should be made again now, oldest first."""
    published = (ClubSites.published_at.isnot(None), ClubSites.html.isnot(None))
    ids = list((await session.execute(
        select(ClubSites.club_id).where(*published, behind(now)).order_by(ClubSites.published_at).limit(limit)
    )).scalars())
    if len(ids) < limit and overnight(now):
        more = (await session.execute(
            select(ClubSites.club_id)
            .where(*published, ClubSites.published_at < now - STALE_AFTER, ClubSites.club_id.notin_(ids or [-1]))
            .order_by(ClubSites.published_at)
            .limit(limit - len(ids))
        )).scalars()
        ids += list(more)
    return ids


async def refresh(session, now: datetime | None = None, limit: int = BATCH) -> int:
    """Make the due pages again; how many were made. One page failing (it is
    logged) does not stop the others."""
    from app.api.sites import service

    now = now or _now()
    made = 0
    for club_id in await due(session, now, limit):
        try:
            if await service.republish_for_club(session, club_id):
                made += 1
        except Exception:  # noqa: BLE001
            logger.exception("sites sweep: the site of club %s could not be made", club_id)
            await session.rollback()
    return made


async def run_refresh(session, now: datetime | None = None) -> int | None:
    """`refresh`, if no other worker is running it. None when one is."""
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as lock_conn:
            return await _locked(lock_conn, session, now)
    return await _locked(bind, session, now)


async def _locked(conn, session, now) -> int | None:
    if not await conn.scalar(text(f"SELECT pg_try_advisory_lock({SITES_LOCK})")):
        return None
    try:
        return await refresh(session, now)
    finally:
        await conn.scalar(text(f"SELECT pg_advisory_unlock({SITES_LOCK})"))
