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
_cache: dict[int, tuple[float, Optional[object]]] = {}


async def enabled_quest_id(session, event_id: Optional[int]):
    """The id of the event's quest when it is on, else None."""
    if not event_id:
        return None
    hit = _cache.get(int(event_id))
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]
    from app.api.quests.models import Quests

    try:
        # In a savepoint: a read that fails here leaves the caller's own
        # transaction (a check-in, a poll answer) usable.
        async with session.begin_nested():
            quest_id = await session.scalar(select(Quests.id).where(Quests.event_id == int(event_id), Quests.enabled == True))  # noqa: E712
    except Exception:  # noqa: BLE001 - no quest is the safe answer
        logger.exception("could not tell whether event %s has a quest on", event_id)
        quest_id = None
    _cache[int(event_id)] = (now + TTL_SECONDS, quest_id)
    return quest_id


def forget(event_id: Optional[int] = None) -> None:
    """Drop what this process remembers (all of it with no event)."""
    if event_id is None:
        _cache.clear()
    else:
        _cache.pop(int(event_id), None)
