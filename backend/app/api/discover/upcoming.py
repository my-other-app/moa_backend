"""How far back "what is on" has to look.

An event is on until it ends, start plus `duration` hours, so every list of
what is on or to come asks for `end >= now`. On its own that condition lets a
walk in date order start at the oldest event still marked published (an
organiser who turned the status clock off leaves an event "published" for
ever), and at a lakh of clubs those are tens of thousands of rows read and
thrown away before the first one that is on (docs/SCALING.md, fix 1).

So each such list also asks for `start >= floor`, the earliest start an event
still on can have. Durations are capped at MAX_EVENT_HOURS (checked on
create, edit and copy), so that is the floor, unless an event saved before
the cap and longer than it is still running: then the floor reaches back to
its start. Those are found through ix_events_long_ends_at, which holds only
them, and the answer is kept for the minute. The floor never leaves out an
event that is on; it only spares the walk the ones that cannot be.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, literal_column, select

from app.api.discover import cache
from app.api.events.models import MAX_EVENT_HOURS, Events, ends_at_sql


async def _longest_running(session, now: datetime) -> float:
    longest = await session.scalar(
        select(func.max(Events.duration)).where(
            Events.is_deleted == False,  # noqa: E712
            # Written out, so it matches the index's predicate under any plan.
            Events.duration > literal_column(str(MAX_EVENT_HOURS)),
            ends_at_sql(Events) >= now,
        )
    )
    return max(float(MAX_EVENT_HOURS), float(longest or 0))


async def hours(session, now: datetime) -> float:
    """How many hours before `now` an event still on at `now` can have started."""
    return await cache.shared.remember(("on-floor", cache.minute(now)), lambda: _longest_running(session, now))


async def since(session, now: datetime) -> datetime:
    """The earliest start of an event still on at `now`."""
    return now - timedelta(hours=await hours(session, now))
