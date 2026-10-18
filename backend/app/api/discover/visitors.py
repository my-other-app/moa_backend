"""Who a visitor is, for counting them, and the views counted in batches.

**Who.** A signed-in reader is their account. Anyone else is a hash of their
address and browser, salted with a secret that changes every day (in India):
the same person counts once a day, and nobody can turn a stored hash back
into an address by hashing every address with the browser strings there
are, which a plain sha256(address|browser) allowed. The address is the one
the proxy saw (app.core.net.client_address), never the first
X-Forwarded-For entry, which the client writes itself.

**Views.** Reading a post or an event page used to write on every read: a
row, and the post's or event's counter read, increased and written back
(two readers at once lost one). Now a view is recorded in this process
(`ViewBuffer.record`): a repeat of one already recorded is dropped in
memory, and the rest wait in a list that one statement writes, with the
counters moved by the number of rows that statement actually inserted
(`views_count = views_count + n`), at most every FLUSH_SECONDS or every
FLUSH_SIZE views. The write rides on whichever request finds the batch due,
inside a savepoint of its own: a batch that fails is logged and dropped
(they are views), and never fails the request. What a crash loses is the
last few seconds of views.

The trade-off, chosen deliberately: a post counts a person once (as it
always has: one row per post and visitor, ON CONFLICT DO NOTHING), an
anonymous one once a day; an event page counts each visitor once a day
(page_views was every load, refreshes included). Counts show a few seconds
late. With the process's caches switched off (the suite: discover.cache's
`reset`), each view is written as it happens, in the request's own
transaction.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Hashable, Optional

from app.api.discover import cache
from app.config import settings
from app.core.net import client_address

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
#: The longest a recorded view waits to be written.
FLUSH_SECONDS = 5.0
#: Or this many waiting.
FLUSH_SIZE = 500
#: How many views a process remembers having seen, to drop repeats in memory.
SEEN_MAX = 200_000


def today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(IST).date().isoformat()


def _salt(day: str) -> bytes:
    return hmac.new(settings.SECRET_KEY.encode(), f"visitor-salt:{day}".encode(), hashlib.sha256).digest()


def anonymous(request, *, day: Optional[str] = None, length: int = 40) -> Optional[str]:
    """Today's hash of the caller's address and browser (hex), or None with neither."""
    ip = client_address(request) or ""
    ua = (request.headers.get("user-agent") or "") if request is not None else ""
    if not ip and not ua:
        return None
    return hmac.new(_salt(day or today()), f"{ip}|{ua}".encode(), hashlib.sha256).hexdigest()[:length]


class ViewBuffer:
    """Views waiting to be written, a repeat dropped in memory. See the module's docstring."""

    def __init__(self, name: str, flush: Callable, *, seconds: float = FLUSH_SECONDS, size: int = FLUSH_SIZE):
        self.name = name
        self._flush = flush  # async (session, [items]) -> None
        self.seconds = seconds
        self.size = size
        self.enabled = True
        self._pending: list = []
        self._since: Optional[float] = None
        self._seen: OrderedDict[Hashable, None] = OrderedDict()
        cache.register(self)

    def clear(self) -> None:
        self._pending.clear()
        self._seen.clear()
        self._since = None

    def __len__(self) -> int:
        return len(self._pending)

    def _fresh(self, key: Hashable) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        return True

    async def record(self, session, items: list[tuple[Hashable, object]]) -> None:
        """Record views ((dedupe key, item) pairs); write the batch if it is due."""
        if not self.enabled:
            await self._write(session, [item for _, item in items])
            return
        for key, item in items:
            if self._fresh(key):
                if not self._pending:
                    self._since = time.monotonic()
                self._pending.append(item)
        if self.due():
            await self.write(session)

    def due(self) -> bool:
        if not self._pending:
            return False
        return len(self._pending) >= self.size or time.monotonic() - (self._since or 0.0) >= self.seconds

    async def write(self, session) -> int:
        """Write what waits, in a savepoint of `session`; the caller commits."""
        batch, self._pending, self._since = self._pending, [], None
        return await self._write(session, batch)

    async def _write(self, session, batch: list) -> int:
        if not batch:
            return 0
        try:
            async with session.begin_nested():
                await self._flush(session, batch)
        except Exception:  # noqa: BLE001 — views are never worth a failed request
            logger.warning("writing %d %s view(s) failed; they are dropped", len(batch), self.name, exc_info=True)
            return 0
        return len(batch)


Flush = Callable[[object, list], Awaitable[None]]
