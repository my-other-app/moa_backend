"""What changed in an event's games, told to every process that streams it.

Whoever changes a board, the ticker, the hunt's state or the look calls
`changed` after the commit. Changes are folded: at most one message a second
per event per process (the first goes at once, the rest of that second go
together as one), carrying the topics that changed:

- `board`: either leaderboard
- `feed`: the ticker
- `hunt`: the hunt's state or timer
- `settings`: the look, the names, what is on

It rides the live room's transport (app.core.redis): every process with Redis,
this one without. Best-effort; the streams also rebuild every few seconds, and
every client polls the GETs as a fallback.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.core import redis

logger = logging.getLogger(__name__)

TOPICS = ("board", "feed", "hunt", "settings")
#: The least time between two messages of one event.
MIN_GAP = 1.0

_last: dict[int, float] = {}
_topics: dict[int, set] = {}
_timers: dict[int, asyncio.TimerHandle] = {}
_tasks: set = set()
_version = 0


def channel(event_id: int) -> str:
    return f"games:{int(event_id)}"


def next_version() -> int:
    global _version
    _version = max(_version + 1, time.time_ns() // 1000)
    return _version


def changed(event_id: int, *topics: str) -> None:
    """`topics` of the event changed (after the commit). Never raises; without a running loop (a script) it does nothing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    event_id = int(event_id)
    _topics.setdefault(event_id, set()).update(t for t in topics if t in TOPICS)
    if event_id in _timers:
        return
    wait = max(0.0, MIN_GAP - (time.monotonic() - _last.get(event_id, -MIN_GAP)))
    _timers[event_id] = loop.call_later(wait, _fire, event_id)


def _fire(event_id: int) -> None:
    _timers.pop(event_id, None)
    topics = _topics.pop(event_id, set())
    if not topics:
        return
    _last[event_id] = time.monotonic()
    task = asyncio.get_running_loop().create_task(_send(event_id, topics))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _send(event_id: int, topics: set) -> None:
    try:
        await redis.publish(channel(event_id), {"v": next_version(), "t": sorted(topics)})
    except Exception:  # noqa: BLE001 - the streams' resync makes it good
        logger.exception("could not tell the games of event %s what changed", event_id)


async def drain() -> None:
    """Send what is waiting now (tests; shutdown)."""
    for event_id in list(_timers):
        handle = _timers.pop(event_id)
        handle.cancel()
        topics = _topics.pop(event_id, set())
        if topics:
            _last[event_id] = time.monotonic()
            await _send(event_id, topics)
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset_for_tests() -> None:
    for handle in _timers.values():
        handle.cancel()
    _timers.clear()
    _topics.clear()
    _last.clear()
    _tasks.clear()
