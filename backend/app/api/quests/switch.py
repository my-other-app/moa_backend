"""Whether an event's quest is on, asked by every hook before it writes
anything (CONTRACT §5.1): one indexed read, kept for a few seconds in this
process. An event without a quest costs its hooks nothing more than that.

The answer may be a few seconds old in another process: a hook that runs
just after the switch is turned off only queues an evaluation, which reads
the switch afresh and does nothing; one that misses the switch being turned
on is made good by the recompute that turning it on queues.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

TTL_SECONDS = 10.0
#: event id -> (expiry, the quest's id when it is on, whether the event has a quest at all, on or off)
_cache: dict[int, tuple[float, Optional[object], bool]] = {}


async def _state(session, event_id: Optional[int]) -> tuple[Optional[object], bool]:
    """(the quest's id when it is on, whether the event has a quest at all): one read, kept TTL_SECONDS."""
    if not event_id:
        return None, False
    hit = _cache.get(int(event_id))
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1], hit[2]
    from app.api.quests.models import Quests

    try:
        # In a savepoint: a read that fails here leaves the caller's own
        # transaction (a check-in, a poll answer) usable.
        async with session.begin_nested():
            row = (await session.execute(select(Quests.id, Quests.enabled).where(Quests.event_id == int(event_id)))).first()
    except Exception:  # noqa: BLE001 - no quest is the safe answer
        logger.exception("could not tell whether event %s has a quest on", event_id)
        row = None
    quest_id = row[0] if row is not None and row[1] else None
    _cache[int(event_id)] = (now + TTL_SECONDS, quest_id, row is not None)
    return quest_id, row is not None


async def enabled_quest_id(session, event_id: Optional[int]):
    """The id of the event's quest when it is on, else None."""
    return (await _state(session, event_id))[0]


async def has_quest(session, event_id: Optional[int]) -> bool:
    """Whether the event has a quest at all, switched on or off (the same cached read)."""
    return (await _state(session, event_id))[1]


def forget(event_id: Optional[int] = None) -> None:
    """Drop what this process remembers (all of it with no event)."""
    if event_id is None:
        _cache.clear()
    else:
        _cache.pop(int(event_id), None)
