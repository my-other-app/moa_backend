"""The quiz's board in Redis, when there is Redis: ranks and the top ten
without sorting the players.

Without Redis each process's quiz snapshot (room.QuizView) reads every
player once a second, keeps their scores sorted and finds a rank by binary
search: cheap per poll, but the snapshot itself is a read and a sort of the
whole quiz, every second, in every process, and a rank is up to a second
behind an answer made in another process. With Redis the scores live in one
sorted set per quiz run, shared by every process:

- a scored answer sets the player's score there (ZADD GT with the total the
  database returned for the answer, not ZINCRBY: an increment retried, or
  lost between the commit and Redis, would leave the set off for good, where
  the total is right however often it is written and whatever order two
  writes land in -- a score only grows within a run);
- a rank is 1 + the players with a strictly higher score (ZCOUNT, exclusive),
  so tied players share a rank exactly as `QuizView.rank_of` gives it
  (ZREVRANK would number them apart);
- the top ten is ZREVRANGE: ties are broken by the member, which starts with
  the player's join time counted down, so the earlier joiner comes first, as
  the database's `ORDER BY score DESC, created_at` has it. The member carries
  the player's id and name, so the board needs nothing else.

Postgres stays the record. A set is complete only once built from it: its
sentinel member (SENTINEL, at -inf, outside every count) is added last, by
the one process that took the build's lock, after every player was written
with ZADD GT -- so a build that read the database before an answer cannot
undo it. A set without its sentinel (never built, evicted under Redis's
memory policy, expired) is built again by the next process that needs it;
meanwhile that process uses its own snapshot. A write that fails (Redis
down) marks the set dirty in this process, which builds it again when Redis
answers.

One set per run of a quiz: its key ends with the run's join code while it is
live, and with the moment it ended once it has. Starting or resetting a quiz
deletes its players; the next run has a new key, so nothing of the last
run's can linger, and the old keys expire (KEEP).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select

from app.core import redis

logger = logging.getLogger(__name__)

PREFIX = redis.PREFIX + "live:board:"
SENTINEL = "~"
#: A run's set is kept this long after it was built.
KEEP = 6 * 3600
#: The build lock, in ms: one process builds a set at a time.
LOCK_MS = 15_000
#: Players written per ZADD while building.
CHUNK = 1000
_LATEST = 10**16 - 1

_STANDINGS = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return {0} end
local out = {1}
for i = 2, #ARGV do
  local s = redis.call('ZSCORE', KEYS[1], ARGV[i])
  if s then
    out[#out + 1] = s
    out[#out + 1] = redis.call('ZCOUNT', KEYS[1], '(' .. s, '+inf')
  else
    out[#out + 1] = ''
    out[#out + 1] = -1
  end
end
return out
"""

_TOP = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return {0} end
local r = redis.call('ZREVRANGE', KEYS[1], 0, tonumber(ARGV[2]), 'WITHSCORES')
table.insert(r, 1, 1)
return r
"""

#: Sets this process failed to write to: built again before they are read.
_dirty: set[str] = set()
_scripts: dict = {}


def on() -> bool:
    """Whether ranks come from Redis: APP_REDIS_URL set (and the client importable)."""
    return redis.client() is not None


def token(status, join_code: Optional[str], ended_at: Optional[datetime]) -> Optional[str]:
    """The run a quiz is in: its join code while live, its end once ended; None
    for a draft, which has no players."""
    value = getattr(status, "value", status)
    if value == "live" and join_code:
        return f"c{join_code}"
    if value == "ended" and ended_at is not None:
        return f"e{int(ended_at.timestamp() * 1_000_000)}"
    return None


def key(quiz_id, run: str) -> str:
    return f"{PREFIX}{quiz_id}:{run}"


def member(player_id, created_at: Optional[datetime], name: str) -> str:
    """The player in the set: the join time counted down (ties to the earlier
    joiner), the id (unique), the name (for the board)."""
    us = int(created_at.timestamp() * 1_000_000) if created_at is not None else _LATEST
    return f"{_LATEST - us:016d}|{player_id}|{name}"


def name_of(m: str) -> str:
    return m.split("|", 2)[2] if m.count("|") >= 2 else m


def _script(c, name: str, lua: str):
    got = _scripts.get(name)
    if got is None or got[0] is not c:
        got = (c, c.register_script(lua))
        _scripts[name] = got
    return got[1]


def _text(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def ensure(session, quiz_id, run: str) -> bool:
    """True once the run's set is complete in Redis (built from Postgres here
    if it was not, and this process could take the lock). False: use the
    snapshot of this process instead, for now."""
    if not on():
        return False
    k = key(quiz_id, run)
    if k not in _dirty:
        built = await redis.run(lambda c: c.zscore(k, SENTINEL), "down", what="quiz board")
        if built == "down":
            return False
        if built is not None:
            return True
    return await rebuild(session, quiz_id, run)


async def rebuild(session, quiz_id, run: str) -> bool:
    """Write every player of the quiz from Postgres into the run's set, under
    the build lock. ZADD GT: a newer score written meanwhile is kept."""
    from app.api.live.models import LiveQuizPlayers

    k = key(quiz_id, run)
    mine = secrets.token_hex(8)
    got = await redis.run(lambda c: c.set(k + ":lock", mine, nx=True, px=LOCK_MS), None, what="quiz board")
    if not got:
        return False
    rows = (await session.execute(
        select(LiveQuizPlayers.id, LiveQuizPlayers.created_at, LiveQuizPlayers.name, LiveQuizPlayers.score)
        .where(LiveQuizPlayers.quiz_id == quiz_id)
    )).all()

    async def write(c) -> bool:
        pipe = c.pipeline(transaction=False)
        for i in range(0, len(rows), CHUNK):
            pipe.zadd(k, {member(pid, at, name): int(score) for pid, at, name, score in rows[i:i + CHUNK]}, gt=True)
        pipe.zadd(k, {SENTINEL: float("-inf")})
        pipe.expire(k, KEEP)
        await pipe.execute()
        # The lock, only if it is still ours.
        await c.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0", 1, k + ":lock", mine)
        return True

    done = await redis.run(write, False, what="quiz board build")
    if done:
        _dirty.discard(k)
    return bool(done)


async def joined(k: str, m: str) -> None:
    """A new player, at 0 (NX: never lowers a score already there)."""
    if await redis.run(lambda c: c.zadd(k, {m: 0}, nx=True), "failed", what="quiz board") == "failed":
        _dirty.add(k)


async def scored(k: str, m: str, total: int) -> None:
    """The player's total after a scored answer (GT: only ever raises it)."""
    if await redis.run(lambda c: c.zadd(k, {m: int(total)}, gt=True), "failed", what="quiz board") == "failed":
        _dirty.add(k)


async def top(k: str, n: int = 10) -> Optional[list[dict]]:
    """The board: the first n, positions 1..n, as the snapshot shows them.
    None when the set is not complete or Redis does not answer."""
    if k in _dirty:
        return None
    got = await redis.run(lambda c: _script(c, "top", _TOP)(keys=[k], args=[SENTINEL, n]), None, what="quiz board")
    if not got or int(got[0]) != 1:
        return None
    pairs = got[1:]
    out = []
    for i in range(0, len(pairs) - 1, 2):
        m = _text(pairs[i])
        if m == SENTINEL:
            continue
        out.append({"rank": len(out) + 1, "name": name_of(m), "score": int(float(pairs[i + 1]))})
        if len(out) == n:
            break
    return out


async def standings(k: str, members: Iterable[str]) -> Optional[dict[str, tuple[Optional[int], Optional[int]]]]:
    """Each member's (score, rank) from the set, in one round trip; (None,
    None) for a member not in it (joined while Redis was away). None when the
    set is not complete or Redis does not answer."""
    if k in _dirty:
        return None
    members = list(members)
    got = await redis.run(lambda c: _script(c, "standings", _STANDINGS)(keys=[k], args=[SENTINEL, *members]), None, what="quiz board")
    if not got or int(got[0]) != 1:
        return None
    out: dict[str, tuple[Optional[int], Optional[int]]] = {}
    for i, m in enumerate(members):
        s, above = got[1 + 2 * i], int(got[2 + 2 * i])
        s = _text(s)
        out[m] = (None, None) if above < 0 or s == "" else (int(float(s)), 1 + above)
    return out


def reset_for_tests() -> None:
    _dirty.clear()
    _scripts.clear()
