"""How fast mail leaves this process, and on which threads.

SES takes a set number of mails a second (14 to begin with) and answers
"Throttling" past it. Every send in the process -- a ticket batch, a
message, a timed reminder -- takes a token from one bucket first, so all of
them together stay under the quota, and they share it in turn: the bucket
hands tokens out first come, first served, so a 10,000-guest reminder and a
50-guest one interleave rather than one waiting behind the other.

When the provider throttles anyway (another process, the quota lowered),
`throttled()` halves the rate and empties the bucket; it climbs back by one
a second for every ten seconds without another.

Sends block (boto3, httpx), so they run on a pool of their own here. The
default thread pool is shared with everything else that runs off the event
loop -- renders, Razorpay, S3 -- and a long send must not starve those.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.config import settings

#: The slowest a throttled bucket goes, and how long it waits before climbing.
MIN_RATE = 1.0
RECOVER_AFTER = 10.0


class Bucket:
    def __init__(self, rate: float):
        self.ceiling = max(float(rate), MIN_RATE)
        self.rate = self.ceiling
        self.capacity = max(1.0, self.ceiling)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.calm_since = self.updated
        self._lock: Optional[asyncio.Lock] = None

    def _refill(self, now: float) -> None:
        if self.rate < self.ceiling and now - self.calm_since >= RECOVER_AFTER:
            steps = int((now - self.calm_since) // RECOVER_AFTER)
            self.rate = min(self.ceiling, self.rate + steps)
            self.calm_since += steps * RECOVER_AFTER
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def take(self) -> None:
        """Wait for a token. Waiters are served in the order they came."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = time.monotonic()
                self._refill(now)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)

    def throttled(self) -> None:
        """The provider said slow down: half the rate, and nothing in hand."""
        now = time.monotonic()
        self._refill(now)
        self.rate = max(MIN_RATE, self.rate / 2)
        self.tokens = 0.0
        self.calm_since = now


_bucket: Optional[Bucket] = None


def bucket() -> Bucket:
    """The process's bucket, rebuilt if MAIL_SEND_RATE changed."""
    global _bucket
    if _bucket is None or _bucket.ceiling != max(float(settings.MAIL_SEND_RATE), MIN_RATE):
        _bucket = Bucket(settings.MAIL_SEND_RATE)
    return _bucket


_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=max(4, 2 * int(settings.MAIL_CONCURRENCY)), thread_name_prefix="mail")
    return _pool


async def blocking(fn, /, *args, **kwargs):
    """`fn(*args, **kwargs)` on the mail threads, with the caller's context."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_executor(), functools.partial(ctx.run, fn, *args, **kwargs))


def reset_for_tests() -> None:
    global _bucket
    _bucket = None
