"""Short-lived answers the discover site shares between visitors.

What is on and who runs it changes over minutes, not milliseconds, and the
front page is read by everyone, so the parts of it that are the same for
every visitor are worked out once a minute and kept here: in this process,
in a small dict with a size cap (there is no shared cache in this stack; see
app/config.py). Keys carry the minute they were made in, so a new minute is a
new key and nothing is ever more than a minute old: a newly published event
is on the front page within the minute. Nothing about who is looking (a
liked post, a vote) is ever kept here.

Concurrent misses on one key wait for the first to finish rather than all
going to the database at the turn of the minute.

Two stores, the same kind: `shared` for what is the same for everyone, and
`personal` for what is worked out per person (For You's ranking), kept a few
minutes and dropped early when the person changes what it rests on
(`forget`). Both are reached only through `remember`, `forget` and `reset`,
so a store kept elsewhere (Redis, once there is one) can take their place
without touching their callers.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable, Optional

TTL_SECONDS = 60.0
MAX_ENTRIES = 2048

_MISSING = object()


class TTLCache:
    def __init__(self, ttl: float = TTL_SECONDS, maxsize: int = MAX_ENTRIES):
        self.ttl = ttl
        self.maxsize = maxsize
        self.enabled = True
        self.hits = 0
        self.misses = 0
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._building: dict[Hashable, asyncio.Future] = {}
        self._stale: set[Hashable] = set()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Hashable) -> Any:
        if not self.enabled:
            return _MISSING
        item = self._items.get(key)
        if item is None:
            return _MISSING
        expires, value = item
        if expires <= time.monotonic():
            del self._items[key]
            return _MISSING
        self._items.move_to_end(key)
        return value

    def put(self, key: Hashable, value: Any, ttl: Optional[float] = None) -> None:
        if not self.enabled:
            return
        self._items[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), value)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def forget(self, key: Hashable) -> None:
        """Drop what is kept under `key`; a build of it in flight is not kept either."""
        self._items.pop(key, None)
        if key in self._building:
            self._stale.add(key)

    def clear(self) -> None:
        self._items.clear()
        self._stale.clear()
        self.hits = self.misses = 0

    async def remember(self, key: Hashable, build: Callable[[], Awaitable[Any]], ttl: Optional[float] = None) -> Any:
        """The value kept under `key`, or `build()`'s, kept for next time
        (for `ttl` seconds when given, else the store's own)."""
        value = self.get(key)
        if value is not _MISSING:
            self.hits += 1
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
            self.put(key, value, ttl)
        if not future.done():
            future.set_result(value)
        return value


shared = TTLCache()
#: Per person: For You's ranked ids (events.recommendations), five minutes.
personal = TTLCache(ttl=300.0, maxsize=10000)


def minute(now) -> int:
    """The minute a moment falls in: the part of a key that makes it expire."""
    return int(now.timestamp() // 60)


def reset(*, enabled: bool = True) -> None:
    """Forget everything (tests start each case from nothing)."""
    for store in (shared, personal):
        store.clear()
        store.enabled = enabled
