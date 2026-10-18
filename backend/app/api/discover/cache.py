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
kept elsewhere (Redis, once there is one) can take their place without
touching their callers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
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
    def __init__(self, ttl: float = TTL_SECONDS, maxsize: int = MAX_ENTRIES, *, families: Optional[dict[str, int]] = None, register_it: bool = True):
        """`maxsize` is each family's capacity unless `families` names its own."""
        self.ttl = ttl
        self.maxsize = maxsize
        self.families = dict(families or {})
        self.enabled = True
        self.hits = 0
        self.stale_hits = 0
        self.misses = 0
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

    def clear(self) -> None:
        self._items.clear()
        self._stale.clear()
        self.hits = self.stale_hits = self.misses = 0

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
        try:
            value = await build()
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
            self.put(key, value, ttl, stale)
        if not future.done():
            future.set_result(value)
        return value

    def _revalidate(self, key: Hashable, refresh: Callable[[], Awaitable[Any]], ttl: Optional[float], stale: float) -> None:
        """Rebuild `key` in the background unless that is under way already."""
        if key in self._building:
            return
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._building[key] = future

        async def run() -> None:
            try:
                value = await refresh()
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
                self.put(key, value, ttl, stale)
            future.set_result(value)

        task = loop.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for the background rebuilds started so far (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


#: The same for everyone. Each family keeps its own few: a page of calendar
#: or "near you" keys never pushes out the front page.
shared = TTLCache(families={
    "home": 4, "categories": 8, "category-counts": 4, "interests": 16, "on-floor": 4,
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
