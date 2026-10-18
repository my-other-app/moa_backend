"""What changed in a live room, told to every process that streams it.

A change is named by its topic: `room` (the feed: questions, polls, updates,
the event's own state, whether a quiz runs) or `quiz` (the quiz: its state,
its players and their answers). Whoever makes the change calls `changed`
after the commit; the process's streams (app/api/live/stream.py), and with
Redis every other process's, rebuild what that topic shows, once per process,
and push it.

- What the host does is told at once (`now=True`): the next question, a
  question approved, a poll launched, an update pushed.
- What the audience does -- a vote, a poll answer, a question asked, a quiz
  answer, a player joining -- is folded: the first such change in an event
  starts a window of COALESCE_SECONDS, and everything in it goes as one
  message at its end. A hall voting at once is one message a third of a
  second per process, not a message a tap.

Each message carries a version: an integer that only grows, per event, the
same in every process. With Redis it is minted there (one INCR, floored at
the clock in microseconds, so it also grows across an outage when this
process minted its own); without, by this process, the same way. A stream
labels what it sends with the version of the last message for its topics,
so a phone reconnecting to another process with `Last-Event-ID` is sent
nothing when nothing changed.

A message says whether it was told at once (`h`: the host's change is in
it): a stream that paces what it sends (the room feed, stream.PACE) sends
such a change without waiting.

A message may carry deltas: what one browser did (a vote, a poll answer), so
a process holding that browser's stream updates its own flags without
reading them again. At most MAX_DELTAS a message; past that `x` is set and
the streams read their browsers' flags afresh.

Messages are best-effort (Redis pub/sub, or this process's own broker): a
stream also rebuilds every few seconds and tells everyone when what it built
differs from what it had (stream.RESYNC_SECONDS), so a message lost to a
Redis blip, or a change made outside the live room (the event closed, its
club unlisted), shows within seconds rather than never.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core import redis

logger = logging.getLogger(__name__)

TOPICS = ("room", "quiz")
#: How long the audience's changes in an event are gathered into one message.
COALESCE_SECONDS = 0.3
#: Deltas one message carries at most; beyond, the streams re-read.
MAX_DELTAS = 2000
#: How long an event's version is kept in Redis after its last change.
VERSION_KEEP = 2 * 24 * 3600

_VERSION_LUA = """
local v = redis.call('INCR', KEYS[1])
local floor = tonumber(ARGV[1])
if v < floor then
  redis.call('SET', KEYS[1], ARGV[1])
  v = floor
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return v
"""


def channel(event_id: int) -> str:
    return f"live:{int(event_id)}"


def _version_key(event_id: int) -> str:
    return f"{redis.PREFIX}live:v:{int(event_id)}"


def _clock() -> int:
    return time.time_ns() // 1000


#: The last version this process minted or heard, per event: its own next
#: one is above it.
_last: dict[int, int] = {}


def seen(event_id: int, version: int) -> None:
    """A version heard from a message: this process's own next one goes above it."""
    if version > _last.get(event_id, 0):
        _last[event_id] = version


def _local_version(event_id: int) -> int:
    v = max(_last.get(event_id, 0) + 1, _clock())
    _last[event_id] = v
    return v


async def next_version(event_id: int) -> int:
    """A new version for the event: Redis's when it answers, else this process's."""
    got = await redis.run(
        lambda c: c.eval(_VERSION_LUA, 1, _version_key(event_id), max(_clock(), _last.get(event_id, 0) + 1), VERSION_KEEP),
        None, what="live version",
    )
    if got is None:
        return _local_version(event_id)
    v = int(got)
    seen(event_id, v)
    return v


async def current_version(event_id: int) -> int:
    """The version of the event's last change, for a stream starting cold:
    Redis's, so every process labels the same state alike; this process's
    own when there is no Redis. One is minted when there is none yet."""
    got = await redis.run(lambda c: c.get(_version_key(event_id)), None, what="live version")
    if got is not None:
        v = int(got)
        seen(event_id, v)
        return v
    if redis.available() is None and event_id in _last:
        return _last[event_id]
    return await next_version(event_id)


# ─── Telling ─────────────────────────────────────────────────────────────


@dataclass
class _Pending:
    topics: set = field(default_factory=set)
    deltas: list = field(default_factory=list)
    overflow: bool = False
    #: Something in it was told at once (the host's): streams send it unpaced.
    now: bool = False
    timer: Optional[asyncio.TimerHandle] = None
    flushing: bool = False


_pending: dict[int, _Pending] = {}
_tasks: set[asyncio.Task] = set()


def changed(event_id: int, *topics: str, delta: Optional[list] = None, now: bool = False) -> None:
    """`topics` of the event changed (after the commit that changed them).
    `now`: told at once, with anything gathered so far; otherwise within
    COALESCE_SECONDS, folded with the rest of the window. `delta`: what one
    browser did, for its own stream's flags. Never raises; without a running
    loop (a script) it does nothing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    event_id = int(event_id)
    p = _pending.get(event_id)
    if p is None:
        p = _pending[event_id] = _Pending()
    p.topics.update(t for t in topics if t in TOPICS)
    if delta is not None:
        if len(p.deltas) < MAX_DELTAS:
            p.deltas.append(delta)
        else:
            p.overflow = True
    if now:
        p.now = True
        if p.timer is not None:
            p.timer.cancel()
            p.timer = None
        if not p.flushing:
            p.flushing = True
            _spawn(loop, flush(event_id))
    elif p.timer is None and not p.flushing:
        p.timer = loop.call_later(COALESCE_SECONDS, _due, event_id)


def _due(event_id: int) -> None:
    p = _pending.get(event_id)
    if p is None:
        return
    p.timer = None
    if not p.flushing:
        p.flushing = True
        _spawn(asyncio.get_running_loop(), flush(event_id))


def _spawn(loop, coro) -> None:
    task = loop.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def flush(event_id: int) -> Optional[dict]:
    """Send what is gathered for the event now. Returns the message sent."""
    p = _pending.pop(event_id, None)
    if p is None:
        return None
    if p.timer is not None:
        p.timer.cancel()
    if not p.topics:
        return None
    try:
        v = await next_version(event_id)
        message = {"v": v, "t": sorted(p.topics), "d": [] if p.overflow else p.deltas, "x": p.overflow, "h": p.now}
        await redis.publish(channel(event_id), message)
        return message
    except Exception:  # noqa: BLE001 - the streams' resync catches what this loses
        logger.exception("could not tell the live room %s what changed", event_id)
        return None


async def changed_once(event_id: int, tag: str, *topics: str) -> None:
    """`changed(now=True)` by one process only, when several would notice the
    same moment (a quiz question's clock running out): the first to claim
    `tag` in Redis tells; without Redis this process does."""
    # SET NX answers None when another process has the tag; "alone" is what
    # comes back when there is no Redis to ask (or it does not answer).
    claimed = await redis.run(
        lambda c: c.set(f"{redis.PREFIX}live:once:{int(event_id)}:{tag}", "1", nx=True, ex=600), "alone", what="live once",
    )
    if claimed is None:
        return
    changed(event_id, *topics, now=True)


async def drain() -> None:
    """Send everything gathered and wait for the sends (tests; shutdown)."""
    for event_id in list(_pending):
        await flush(event_id)
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset_for_tests() -> None:
    for p in _pending.values():
        if p.timer is not None:
            p.timer.cancel()
    _pending.clear()
    _last.clear()
    _tasks.clear()
