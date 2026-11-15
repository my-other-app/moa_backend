"""A few seconds' answers for the pages the dashboards poll.

The club's home is polled every 15 s while one of its events is live, an
event's console every 15 s while its door is open, the deliveries log every
3 s while anything is still sending, and the Announcements page and its
figures are opened together. Each tab worked the same aggregates out again,
and on the day several tabs are open on each. Now one answer per club or
event is kept a few seconds and every tab shares it.

discover.cache's TTLCache does the keeping: concurrent misses on a key wait
for the one build, and with Redis (APP_REDIS_URL) the answer is shared by
every worker too, so a cold key is built once rather than once a process.
Each key's first element names what it holds; `forget` drops one at once
when something it shows has changed (a new announcement). The suite starts
every test with the store switched off (discover.cache.reset), so a test
sees what the database holds unless it turns the store on itself.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Hashable

from app.api.discover.cache import TTLCache

#: How long each answer is kept.
ANNOUNCEMENTS_SECONDS = 30.0
DASHBOARD_SECONDS = 10.0
CONSOLE_SECONDS = 5.0
DELIVERIES_SECONDS = 3.0

store = TTLCache(ttl=CONSOLE_SECONDS, maxsize=4096, shared_as="polls")


async def kept(key: Hashable, ttl: float, build: Callable[[], Awaitable[Any]]) -> Any:
    """`build()`'s answer, kept `ttl` seconds under `key`."""
    return await store.remember(key, build, ttl=ttl)


def forget(key: Hashable) -> None:
    store.forget(key)
