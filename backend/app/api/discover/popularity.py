"""events.popularity: the attention an event draws, decayed with its age.

"Popular" sorted every event that is on by going_count + page_views, worked
out per row: a sort of every upcoming event (U of them) to show a page of
24. Now the number is stored on the event and indexed over what can be shown
(ix_events_shown_popular: popularity DESC, event_datetime, id), so the
popular lists walk the index from the top and stop at the page: k rows, not
U.

    popularity = ln(1 + going_count + page_views) + day_made / DECAY_DAYS

where day_made is the day (UTC) the event was made, counted from 1970. In
ranking terms that is the attention decayed by e every DECAY_DAYS of age:
for any two events it orders them as (1 + attention) * e^(-age / DECAY_DAYS)
would, whatever "now" is, so the stored number never has to change with the
clock, only with the counts. At DECAY_DAYS = 30, ten times the attention
outweighs about 69 days of age: a big event announced months ago still
ranks, and does not sit above everything new for ever. Events made the same
day compare by attention alone, ties by date then id, as before.

It is written in batches, not by the registration that changes going_count:
an indexed column on the row a registration storm writes would turn every
one of those writes into a write of every index on events. `refresh`, every
few minutes on one process (counters.check_loop), rewrites only the rows
whose number changed, and sets what is over (still marked published by an
organiser who turned the status clock off) to 0, below everything on, so the
index walk does not read through them. A new or changed event is placed at
the next refresh (`EVERY_SECONDS`); its order among the rest is otherwise
right.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from app.db.locks import singleton
from app.db import lock_keys

logger = logging.getLogger(__name__)

DECAY_DAYS = 30.0
EVERY_SECONDS = 180
LOCK = lock_keys.DISCOVER_POPULARITY

#: The stored number for an event row `e` (SQL), as `refresh` writes it.
FORMULA = (
    "ln(1.0 + e.going_count + coalesce(e.page_views, 0)) "
    f"+ floor(extract(epoch FROM e.created_at) / 86400.0) / {DECAY_DAYS}"
)
ENDS_AT = "timezone('UTC', timezone('UTC', e.event_datetime) + e.duration * INTERVAL '1 hour')"

REFRESH_SQL = f"""
UPDATE events AS t SET popularity = p.v
FROM (
  SELECT e.id, CASE WHEN {ENDS_AT} < now() THEN 0 ELSE {FORMULA} END AS v
  FROM events e
  WHERE NOT e.is_deleted AND e.status IN ('published', 'live')
) AS p
WHERE t.id = p.id AND t.popularity IS DISTINCT FROM p.v
"""


def value(going: int, views: int, created_at: datetime) -> float:
    """The formula in Python (tests, the bench's reference)."""
    import math

    day = (created_at.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() // 86400
    return math.log(1.0 + going + (views or 0)) + day / DECAY_DAYS


async def refresh(session) -> int:
    """Write every shown event's popularity that changed; returns how many."""
    result = await session.execute(text(REFRESH_SQL))
    await session.commit()
    return int(result.rowcount or 0)


async def refresh_if_free(session, now: Optional[datetime] = None) -> Optional[int]:
    """`refresh`, unless another process is at it."""
    async with singleton(session, LOCK) as mine:
        if not mine:
            return None
        return await refresh(session)
