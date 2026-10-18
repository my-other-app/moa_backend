"""Short-lived answers the discover site shares between visitors.

What is on and who runs it changes over minutes, not milliseconds, and the
front page is read by everyone, so the parts of it that are the same for
every visitor are worked out once a minute and kept here: in this process,
in a small dict with a size cap (there is no shared cache in this stack; see
app/config.py). Nothing about who is looking (a liked post, a vote) is ever
kept in `shared`.

Concurrent misses on one key wait for the first to finish rather than all
going to the database at once.

Stale while revalidate: a caller that can rebuild on its own (`refresh`, a
build with a session of its own; `kept` makes one) may be answered with the
value just past its time, for up to `stale` seconds more, while one task
rebuilds it in the background. Before, every key carried the minute it was
made in, so at the turn of each minute the first request for each key
waited for the rebuild (the front page: 1.4 s at a lakh of clubs) and
everyone arriving meanwhile waited with it. Now only a cold key, or one left
unread for longer than its stale window, is built while someone waits.

Capacity is per family (a key's first element): a burst of calendar or
"near you" keys can no longer push the front page or the categories out.

Two stores of the shared kind: `shared` for what is the same for everyone,
and `personal` for what is worked out per person (For You's ranking), kept a
few minutes and dropped early when the person changes what it rests on
(`forget`). Other modules keep stores of their own (the live room's
second-long snapshots); every store, and anything else that defers work in
this process (`register`: the view buffers), is switched off and emptied
together by `reset` -- the suite starts each test from nothing that way.
All are reached only through `remember`, `forget` and `reset`, so a store
kept elsewhere can take their place without touching their callers.

A shared second level (APP_REDIS_URL). Each process kept its own copy, so
with four workers every key was built four times a minute, and a cold start
waited on the database while a sibling had the answer. `shared` now also
keeps each value in Redis (`Shared`), with the same fresh and stale times on
the wall clock: a miss here asks Redis before building, a build takes a lock
there first (SET NX) so one process builds a cold key while the others wait
a moment for its answer, and a background revalidation picks up a sibling's
fresher value rather than rebuilding it. `clear` moves the namespace to a new
generation, so nothing kept before it is read again. Redis down or slow: the
local level alone, as before (app/core/redis.py). Values are pickled and the
keys carry a hash of the app's code, so a process from another release never
reads a value of a shape it does not know.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import pickle
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Hashable, Optional

logger = logging.getLogger(__name__)

TTL_SECONDS = 60.0
MAX_ENTRIES = 2048

_MISSING = object()

#: Everything `reset` switches off and empties: the stores, and other
#: process-local deferrals (the view buffers) that register themselves.
_registered: list = []
#: What the last `reset` set: a store made after it starts the same way.
_enabled = True


def register(thing) -> None:
    """Have `reset` clear `thing` (anything with `clear()` and `enabled`); it
    starts switched on or off as the last `reset` left the others."""
    if not any(t is thing for t in _registered):
        _registered.append(thing)
        thing.enabled = _enabled


def family_of(key: Hashable) -> Optional[str]:
    """A key's family: its first element when that is a string."""
    if isinstance(key, tuple) and key and isinstance(key[0], str):
        return key[0]
    return None


class TTLCache:
    def __init__(
        self, ttl: float = TTL_SECONDS, maxsize: int = MAX_ENTRIES, *, families: Optional[dict[str, int]] = None,
        register_it: bool = True, shared_as: Optional[str] = None,
    ):
        """`maxsize` is each family's capacity unless `families` names its own.
        `shared_as`: the namespace of its second level in Redis, when there is one."""
        self.ttl = ttl
        self.maxsize = maxsize
        self.families = dict(families or {})
        self.enabled = True
        self.hits = 0
        self.stale_hits = 0
        self.misses = 0
        self.shared_hits = 0
        self.l2 = Shared(shared_as) if shared_as else None
        # family -> key -> (fresh until, stale until, value), oldest use first.
        self._items: dict[Optional[str], OrderedDict[Hashable, tuple[float, float, Any]]] = {}
        self._building: dict[Hashable, asyncio.Future] = {}
        self._stale: set[Hashable] = set()
        self._tasks: set[asyncio.Task] = set()
        if register_it:
            register(self)

    def __len__(self) -> int:
        return sum(len(f) for f in self._items.values())

    def capacity(self, family: Optional[str]) -> int:
        return self.families.get(family, self.maxsize) if family is not None else self.maxsize

    def _entry(self, key: Hashable):
        items = self._items.get(family_of(key))
        return items.get(key) if items is not None else None

    def get(self, key: Hashable) -> Any:
        """The value kept under `key` while it is fresh, else _MISSING."""
        if not self.enabled:
            return _MISSING
        item = self._entry(key)
        if item is None:
            return _MISSING
        fresh, stale, value = item
        now = time.monotonic()
        if fresh <= now:
            if stale <= now:
                self._drop(key)
            return _MISSING
        self._items[family_of(key)].move_to_end(key)
        return value

    def put(self, key: Hashable, value: Any, ttl: Optional[float] = None, stale: float = 0.0) -> None:
        if not self.enabled:
            return
        fresh = time.monotonic() + (self.ttl if ttl is None else ttl)
        fam = family_of(key)
        items = self._items.setdefault(fam, OrderedDict())
        items[key] = (fresh, fresh + max(0.0, stale), value)
        items.move_to_end(key)
        while len(items) > self.capacity(fam):
            items.popitem(last=False)

    def _drop(self, key: Hashable) -> None:
        items = self._items.get(family_of(key))
        if items is not None:
            items.pop(key, None)

    def forget(self, key: Hashable) -> None:
        """Drop what is kept under `key`; a build of it in flight is not kept either."""
        self._drop(key)
        if key in self._building:
            self._stale.add(key)
        if self.enabled and self._l2() is not None:
            self._later(self.l2.forget(key))

    def clear(self) -> None:
        self._items.clear()
        self._stale.clear()
        self.hits = self.stale_hits = self.misses = self.shared_hits = 0
        if self.enabled and self._l2() is not None:
            self._later(self.l2.clear())

    def _later(self, coro) -> None:
        """Run `coro` in the background when there is a loop; drop it when not."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _l2(self) -> Optional["Shared"]:
        return self.l2 if self.l2 is not None and self.l2.on() else None

    async def remember(
        self, key: Hashable, build: Callable[[], Awaitable[Any]], ttl: Optional[float] = None, *,
        stale: float = 0.0, refresh: Optional[Callable[[], Awaitable[Any]]] = None,
    ) -> Any:
        """The value kept under `key`, or `build()`'s, kept for next time
        (for `ttl` seconds when given, else the store's own).

        With `refresh` (a build that needs nothing of the caller's, such as
        its session) and `stale` seconds: a value up to `stale` seconds past
        its time is answered at once while `refresh()` replaces it in the
        background, one rebuild at a time per key."""
        if self.enabled:
            item = self._entry(key)
            if item is not None:
                fresh, stale_until, value = item
                now = time.monotonic()
                if now < fresh:
                    self.hits += 1
                    self._items[family_of(key)].move_to_end(key)
                    return value
                if refresh is not None and now < stale_until:
                    self.stale_hits += 1
                    self._revalidate(key, refresh, ttl, stale)
                    return value
        self.misses += 1
        if not self.enabled:
            return await build()
        pending = self._building.get(key)
        if pending is not None:
            try:
                return await asyncio.shield(pending)
            except Exception:  # noqa: BLE001 — the first builder failed; this one tries for itself
                return await build()
        future = asyncio.get_running_loop().create_future()
        self._building[key] = future
        keep_ttl, keep_stale = ttl, stale
        try:
            l2 = self._l2()
            if l2 is None:
                value = await build()
            else:
                value, keep_ttl, keep_stale, revalidate = await self._miss_shared(l2, key, build, ttl, stale, refresh is not None)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc if isinstance(exc, Exception) else RuntimeError("cancelled"))
                future.exception()  # read, so an unawaited failure is not reported as lost
            raise
        finally:
            self._building.pop(key, None)
            forgotten = key in self._stale
            self._stale.discard(key)
        if not forgotten:  # forgotten while it was being worked out: answer with it, keep nothing
            self.put(key, value, keep_ttl, keep_stale)
            if l2 is not None and revalidate:
                # A sibling's value just past its time: answered, and rebuilt behind.
                self._revalidate(key, refresh, ttl, stale)
        if not future.done():
            future.set_result(value)
        return value

    async def _miss_shared(self, l2: "Shared", key: Hashable, build, ttl: Optional[float], stale: float, can_refresh: bool):
        """A cold key with Redis: a sibling's value if it has one; else built
        here under the shared lock, or a sibling's build waited for. Returns
        (value, ttl, stale to keep it for, whether to revalidate it now)."""
        ttl = self.ttl if ttl is None else ttl
        gen, got = await l2.get(key)
        if got is not None and (got.fresh > 0 or can_refresh):
            self.shared_hits += 1
            return (got.value, *got.left(), got.fresh <= 0)
        token = await l2.lock(key)
        if token is False:  # a sibling is building it: wait a moment for its answer
            gen, got = await l2.wait(key)
            if got is not None:
                self.shared_hits += 1
                return (got.value, *got.left(), False)
        try:
            value = await build()
        finally:
            if token:
                await l2.unlock(key, token)
        await l2.put(key, gen, value, ttl, stale)
        return value, ttl, stale, False

    def _revalidate(self, key: Hashable, refresh: Callable[[], Awaitable[Any]], ttl: Optional[float], stale: float) -> None:
        """Rebuild `key` in the background unless that is under way already."""
        if key in self._building:
            return
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._building[key] = future

        async def run() -> None:
            keep_ttl, keep_stale = ttl, stale
            try:
                l2 = self._l2()
                if l2 is None:
                    value = await refresh()
                else:
                    got = await self._refresh_shared(l2, key, refresh, ttl, stale)
                    if got is None:  # a sibling is rebuilding it; the stale value stays meanwhile
                        self._building.pop(key, None)
                        self._stale.discard(key)
                        future.set_result(None)
                        return
                    value, keep_ttl, keep_stale = got
            except Exception as exc:  # noqa: BLE001 — the stale value stays until its window closes
                logger.warning("refreshing %r in the background failed", key, exc_info=True)
                self._building.pop(key, None)
                self._stale.discard(key)
                future.set_exception(exc)
                future.exception()
                return
            self._building.pop(key, None)
            forgotten = key in self._stale
            self._stale.discard(key)
            if not forgotten:
                self.put(key, value, keep_ttl, keep_stale)
            future.set_result(value)

        task = loop.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_shared(self, l2: "Shared", key: Hashable, refresh, ttl: Optional[float], stale: float):
        """A background rebuild with Redis: a sibling's fresher value if there
        is one; else rebuilt here, unless a sibling holds the lock (None)."""
        ttl = self.ttl if ttl is None else ttl
        gen, got = await l2.get(key)
        if got is not None and got.fresh > 0:
            self.shared_hits += 1
            return (got.value, *got.left())
        token = await l2.lock(key)
        if token is False:
            return None
        try:
            value = await refresh()
        finally:
            if token:
                await l2.unlock(key, token)
        await l2.put(key, gen, value, ttl, stale)
        return value, ttl, stale

    async def settle(self) -> None:
        """Wait for the background rebuilds started so far (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# ─── The shared second level ─────────────────────────────────────────────


def _code_version() -> str:
    """A hash of the app's source. In the shared keys, so values kept by
    other code -- a process not yet restarted in a deploy -- are never read
    as this code's: their shape may differ."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha1()
    try:
        for path in sorted(root.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    except OSError:  # pragma: no cover - unreadable source: a fixed namespace
        return "0"
    return digest.hexdigest()[:12]


_code: Optional[str] = None


def code_version() -> str:
    global _code
    if _code is None:
        _code = _code_version()
    return _code


@dataclass
class Got:
    value: Any
    #: Seconds it is still fresh for (<= 0: past its time), and its stale window.
    fresh: float
    stale: float

    def left(self) -> tuple[float, float]:
        """(fresh, stale) seconds from now, as `put` takes them."""
        fresh = max(0.0, self.fresh)
        return fresh, max(0.0, self.fresh + self.stale - fresh)


_UNLOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class Shared:
    """One store's level in Redis. Every method falls back quietly: None,
    no lock, nothing kept, when Redis is unset, down or slow."""

    #: How long a process waits for a sibling's build of a cold key, and how
    #: often it looks; how long a build may hold the lock.
    WAIT = 2.0
    STEP = 0.05
    LOCK_MS = 15_000
    #: A value larger than this, pickled, is kept here only.
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, namespace: str):
        self.namespace = namespace

    def on(self) -> bool:
        from app.core import redis

        return redis.available() is not None

    def _base(self) -> str:
        from app.core import redis

        return f"{redis.PREFIX}cache:{code_version()}:{self.namespace}"

    def _keys(self, key: Hashable) -> tuple[str, str, str]:
        h = hashlib.sha1(repr(key).encode()).hexdigest()[:32]
        base = self._base()
        return f"{base}:v:{h}", f"{base}:gen", f"{base}:lock:{h}"

    async def get(self, key: Hashable) -> tuple[Optional[int], Optional[Got]]:
        """(the namespace's generation, the value when it is of that generation
        and not past its stale window). The generation goes with a later put."""
        from app.core import redis

        value_key, gen_key, _ = self._keys(key)
        raw = await redis.run(lambda c: c.mget(value_key, gen_key), None, what="cache get")
        if raw is None:
            return None, None
        gen = int(raw[1] or 0)
        if raw[0] is None:
            return gen, None
        try:
            seen, fresh_until, stale_until, value = pickle.loads(raw[0])
        except Exception:  # noqa: BLE001 - unreadable: a miss
            return gen, None
        now = time.time()
        if seen != gen or stale_until <= now:
            return gen, None
        return gen, Got(value, fresh_until - now, stale_until - fresh_until)

    async def put(self, key: Hashable, gen: Optional[int], value: Any, ttl: float, stale: float) -> None:
        from app.core import redis

        if gen is None:
            return
        now = time.time()
        fresh_until = now + ttl
        stale_until = fresh_until + max(0.0, stale)
        try:
            data = pickle.dumps((gen, fresh_until, stale_until, value), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # noqa: BLE001 - not picklable: kept here only
            logger.warning("discover cache value for %r cannot be shared", key, exc_info=True)
            return
        if len(data) > self.MAX_BYTES:
            return
        value_key, _, _ = self._keys(key)
        await redis.run(lambda c: c.set(value_key, data, px=max(1, int((stale_until - now) * 1000))), None, what="cache put")

    async def lock(self, key: Hashable):
        """A token when this process may build `key`; False when a sibling
        holds the lock; None when there is no Redis to ask (build anyway)."""
        from app.core import redis

        _, _, lock_key = self._keys(key)
        token = secrets.token_hex(8)
        got = await redis.run(lambda c: c.set(lock_key, token, nx=True, px=self.LOCK_MS), "down", what="cache lock")
        if got == "down":
            return None
        return token if got else False

    async def unlock(self, key: Hashable, token: str) -> None:
        from app.core import redis

        _, _, lock_key = self._keys(key)
        await redis.run(lambda c: c.eval(_UNLOCK, 1, lock_key, token), None, what="cache unlock")

    async def wait(self, key: Hashable) -> tuple[Optional[int], Optional[Got]]:
        """A sibling's fresh value, once it has put it, for up to WAIT seconds."""
        deadline = time.monotonic() + self.WAIT
        gen: Optional[int] = None
        while time.monotonic() < deadline:
            await asyncio.sleep(self.STEP)
            gen, got = await self.get(key)
            if gen is None:
                return None, None
            if got is not None and got.fresh > 0:
                return gen, got
        return gen, None

    async def forget(self, key: Hashable) -> None:
        from app.core import redis

        value_key, _, _ = self._keys(key)
        await redis.run(lambda c: c.delete(value_key), None, what="cache forget")

    async def clear(self) -> None:
        """Every value kept so far is of an old generation now: never read again."""
        from app.core import redis

        _, gen_key, _ = self._keys(())
        await redis.run(lambda c: c.incr(gen_key), None, what="cache clear")


#: The same for everyone. Each family keeps its own few: a page of calendar
#: or "near you" keys never pushes out the front page. Shared between
#: processes through Redis when there is one.
shared = TTLCache(shared_as="discover", families={
    "home": 4, "categories": 8, "category-counts": 4, "interests": 16, "topics": 4, "on-floor": 4,
    "calendar": 1024, "near": 4096, "home-clubs": 4096,
    "for-you-popular": 2048, "for-you-online": 4,
    "sitemap": 4, "sitemap-json": 4, "sitemap-bounds": 8,
})
#: Per person: For You's ranked ids (events.recommendations), five minutes.
personal = TTLCache(ttl=300.0, maxsize=10000)


def minute(now) -> int:
    """The minute a moment falls in: the part of a key that makes it expire."""
    return int(now.timestamp() // 60)


async def kept(
    store: TTLCache, key: Hashable, build: Callable[[Any, Any], Awaitable[Any]], session, now, *,
    ttl: Optional[float] = None, stale: Optional[float] = None,
) -> Any:
    """`store.remember` for a build that takes a session and a moment: on a
    miss it runs with the caller's (`session`, `now`); once the value is past
    its time it is answered for up to `stale` seconds more (by default as
    long again as its ttl) while the build runs again in the background, with
    a session of its own and the clock's time."""
    async def refresh():
        from datetime import datetime, timezone

        from app.api.discover.geo import session_scope

        async with session_scope() as own:
            return await build(own, datetime.now(timezone.utc))

    window = (store.ttl if ttl is None else ttl) if stale is None else stale
    return await store.remember(key, lambda: build(session, now), ttl=ttl, stale=window, refresh=refresh)


def reset(*, enabled: bool = True) -> None:
    """Forget everything (tests start each case from nothing)."""
    global _enabled
    _enabled = enabled
    for thing in _registered:
        thing.clear()
        thing.enabled = enabled
