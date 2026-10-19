"""Front-page numbers that move slowly and cost a read of every event to count.

"Cities" is how many cities events have been held in: the points of every
in-person event a listed club held, rounded to about 5 km, grouped so that
places within CITY_KM of a busier place count as its city (the busiest
places lead; each other place joins the first leader within CITY_KM, else
leads a city of its own). It used to count distinct venue strings, so
"Main Auditorium, CET" and "CET main auditorium" were two "cities" and a
city with a hundred venues was a hundred; the site shows it as cities. An
event with no point on the map is not counted. At a lakh of clubs it reads
every such event once (docs/SCALING.md fix 12), so it is counted rarely and
kept in a one-row-per-number table, discover_stats:

- the front page reads the row (a primary-key lookup);
- at most once a minute per process, the front page's request schedules
  `refresh` after its response; that takes a transaction-scoped advisory
  lock, so one process at a time, and counts again only when the stored
  number is older than EVERY;
- with no row yet (a new database), the number is counted where it is asked
  for, and the next refresh stores it.

With the shared cache switched off (the tests), nothing is kept: the number
is counted each time, as the minute cache does.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import BigInteger, Column, String, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.discover import cache, geo
from app.api.events.models import Events
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db import lock_keys

logger = logging.getLogger(__name__)

CITIES = "cities"
#: Places closer than this to a busier one are in its city.
CITY_KM = 25.0
#: Points are rounded to this many degrees first (about 5.5 km).
GRID_DEG = 0.05
#: How old the stored number may get before a refresh counts again.
EVERY = timedelta(minutes=30)
#: How often one process looks (seconds).
CHECK_EVERY = 60.0
_LOCK = lock_keys.DISCOVER_STATS
_HELD = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)


class DiscoverStats(AbstractSQLModel):
    """A number the front page shows, as last counted."""

    __tablename__ = "discover_stats"

    name = Column(String(40), primary_key=True)
    value = Column(BigInteger, nullable=False)
    computed_at = Column(TZAwareDateTime(timezone=True), nullable=False)


async def count_cities(session) -> int:
    """How many cities in-person events of listed clubs were held in (see the module's docstring)."""
    lat = func.round(Events.lat / GRID_DEG) * GRID_DEG
    lng = func.round(Events.lng / GRID_DEG) * GRID_DEG
    n = func.count()
    rows = (await session.execute(
        select(lat, lng, n)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Events.is_deleted == False,  # noqa: E712
            Events.lifecycle.in_(bindparam(None, list(_HELD), expanding=True, literal_execute=True)),
            Clubs.listed == True,  # noqa: E712
            func.coalesce(Events.is_online, False) == False,  # noqa: E712
            Events.lat.isnot(None), Events.lng.isnot(None),
        )
        .group_by(lat, lng)
    )).all()
    return len(cities_of([(float(la), float(ln), int(k)) for la, ln, k in rows]))


def cities_of(places: list[tuple[float, float, int]], km: float = CITY_KM) -> list[tuple[float, float]]:
    """The leaders of `places` ((lat, lng, events)): the busiest first, each
    other place joining the first leader within `km`, else leading its own.
    Leaders are looked up in a grid of cells about `km` wide, so each place
    is compared with the leaders around it only."""
    cell_deg = km / 111.0
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    leaders: list[tuple[float, float]] = []
    for la, ln, _ in sorted(places, key=lambda p: (-p[2], p[0], p[1])):
        cy, cx = int(la // cell_deg), int(ln // (cell_deg / max(0.1, math.cos(math.radians(la)))))
        near = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for l_la, l_ln in grid.get((cy + dy, cx + dx), ()):
                    if geo.km(la, ln, l_la, l_ln) <= km:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        if not near:
            leaders.append((la, ln))
            grid.setdefault((cy, cx), []).append((la, ln))
    return leaders


async def cities(session, now: datetime) -> int:
    """The number of cities, as last stored (counted now when there is none)."""
    if not cache.shared.enabled:
        return await count_cities(session)
    value = await session.scalar(select(DiscoverStats.value).where(DiscoverStats.name == CITIES))
    if value is None:
        return await count_cities(session)
    return int(value)


_last_check = float("-inf")


def refresh_due() -> bool:
    """Whether this request should schedule `refresh` (and the next ones within CHECK_EVERY should not)."""
    global _last_check
    if not cache.shared.enabled:
        return False
    now = time.monotonic()
    if now - _last_check < CHECK_EVERY:
        return False
    _last_check = now
    return True


def reset() -> None:
    global _last_check
    _last_check = float("-inf")


async def refresh(now: Optional[datetime] = None) -> Optional[int]:
    """Count the cities again and store the number, if it is older than EVERY
    and no other process is at it. Returns the new number, or None."""
    now = now or datetime.now(timezone.utc)
    try:
        async with geo.session_scope() as session:
            if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK}):
                return None
            at = await session.scalar(select(DiscoverStats.computed_at).where(DiscoverStats.name == CITIES))
            if at is not None and at > now - EVERY:
                return None
            n = await count_cities(session)
            await session.execute(
                insert(DiscoverStats).values(name=CITIES, value=n, computed_at=now)
                .on_conflict_do_update(index_elements=[DiscoverStats.name], set_=dict(value=n, computed_at=now))
            )
            await session.commit()
            return n
    except Exception:  # noqa: BLE001 — a number on the front page is never worth a failed request
        logger.exception("counting the front page's cities failed")
        return None
