"""Front-page numbers that move slowly and cost a read of every event to count.

"Cities" is how many different places events have been held in: a distinct
count of every in-person event's venue ever held. At a lakh of clubs that is
a million rows sorted on disk (1.7 s, docs/SCALING.md fix 12), and it was
done by every worker each time its front page was rebuilt. Nothing about it
can be bounded without changing what it counts, so it is counted rarely and
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

logger = logging.getLogger(__name__)

CITIES = "cities"
#: How old the stored number may get before a refresh counts again.
EVERY = timedelta(minutes=30)
#: How often one process looks (seconds).
CHECK_EVERY = 60.0
_LOCK = 7_420_401
_HELD = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)


class DiscoverStats(AbstractSQLModel):
    """A number the front page shows, as last counted."""

    __tablename__ = "discover_stats"

    name = Column(String(40), primary_key=True)
    value = Column(BigInteger, nullable=False)
    computed_at = Column(TZAwareDateTime(timezone=True), nullable=False)


async def count_cities(session) -> int:
    """Every place an in-person event was held, by a verified club: distinct venue words."""
    place = func.lower(func.trim(Events.location_name))
    n = await session.scalar(
        select(func.count(func.distinct(place)))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Events.is_deleted == False,  # noqa: E712
            Events.lifecycle.in_(bindparam(None, list(_HELD), expanding=True, literal_execute=True)),
            Clubs.is_verified == True, Clubs.is_deleted == False,  # noqa: E712
            func.coalesce(Events.is_online, False) == False,  # noqa: E712
            place.isnot(None), place != "",
        )
    )
    return int(n or 0)


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
