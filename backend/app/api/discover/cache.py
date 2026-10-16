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
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable

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

    def put(self, key: Hashable, value: Any) -> None:
        if not self.enabled:
            return
        self._items[key] = (time.monotonic() + self.ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self.hits = self.misses = 0

    async def remember(self, key: Hashable, build: Callable[[], Awaitable[Any]]) -> Any:
        """The value kept under `key`, or `build()`'s, kept for next time."""
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
        self.put(key, value)
        if not future.done():
            future.set_result(value)
        return value


shared = TTLCache()


def minute(now) -> int:
    """The minute a moment falls in: the part of a key that makes it expire."""
    return int(now.timestamp() // 60)


def reset(*, enabled: bool = True) -> None:
    """Forget everything (tests start each case from nothing)."""
    shared.clear()
    shared.enabled = enabled
