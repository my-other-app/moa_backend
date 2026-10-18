"""Redis, when there is one (APP_REDIS_URL): speed and cross-process fan-out,
never the truth.

Postgres holds everything that must be right. Redis lets several API
processes (and servers) share what each would otherwise keep alone: the
rate limits' counts (app/core/rate_limit.py), the discover cache's second
level (app/api/discover/cache.py), and messages published to subscribers in
other processes (`publish` / `subscribe`: the live room's updates, the job
queue's wake-ups).

Unset, nothing here touches a network: `client()` is None, `publish` hands
the message to this process's own subscribers, and every caller works as it
did before, per process.

Set, every call is bounded by REDIS_TIMEOUT and none raises into a request:
a call that fails or times out is logged, Redis is taken to be down for a few
seconds (no request waits on it meanwhile), and the caller falls back to its
own way -- the in-memory limit, the local cache, the in-process broker.

Subscriptions share one connection per process: it pattern-subscribes to
every channel under the prefix once (`PSUBSCRIBE moa:ch:*`) and hands each
message to the local subscribers of its channel, if any. One connection
however many live-room screens a process serves, and nothing to re-subscribe
channel by channel when it reconnects; the price is that every process sees
every message, which at a few processes and small JSON messages is nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar

from app.config import settings

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - in requirements.txt
    aioredis = None

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Every key and channel this app uses starts with this.
PREFIX = "moa:"
_CHANNELS = PREFIX + "ch:"
#: After a failure, Redis is left alone this long.
DOWN_FOR = 5.0
#: A subscriber's queue: past this many unread messages the oldest go.
QUEUE_SIZE = 256

_client = None
_client_loop = None
_down_until = 0.0


def url() -> str:
    return (settings.REDIS_URL or "").strip()


def configured() -> bool:
    return bool(url()) and aioredis is not None


def client():
    """This process's Redis client, or None when APP_REDIS_URL is unset.
    Connections are made on first use, each call bounded by REDIS_TIMEOUT."""
    global _client, _client_loop
    if not configured():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or (loop is not None and _client_loop is not None and _client_loop is not loop):
        timeout = float(settings.REDIS_TIMEOUT)
        _client = aioredis.from_url(
            url(), socket_timeout=timeout, socket_connect_timeout=timeout,
            health_check_interval=30, retry_on_timeout=False,
        )
        _client_loop = loop
    return _client


def available():
    """The client, unless Redis is unset or was found down a moment ago."""
    c = client()
    if c is None or time.monotonic() < _down_until:
        return None
    return c


def mark_down(exc: BaseException, what: str = "a call") -> None:
    """Leave Redis alone for DOWN_FOR seconds; log once per outage, not per call."""
    global _down_until
    first = time.monotonic() >= _down_until
    _down_until = time.monotonic() + DOWN_FOR
    if first:
        logger.warning("Redis unavailable (%s: %s); falling back for %.0f s", what, exc.__class__.__name__, DOWN_FOR)


async def run(op: Callable[[Any], Awaitable[T]], default: T = None, *, what: str = "a call") -> T:
    """`await op(client)`, bounded; `default` when Redis is unset, down, or the call fails."""
    c = available()
    if c is None:
        return default
    try:
        return await asyncio.wait_for(op(c), timeout=float(settings.REDIS_TIMEOUT) * 2)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - Redis is never a reason to fail a request
        mark_down(exc, what)
        return default


async def ping() -> Optional[bool]:
    """True when Redis answers, False when it does not, None when unset.
    Asks even while it is marked down: this is how readiness finds out."""
    c = client()
    if c is None:
        return None
    try:
        return bool(await asyncio.wait_for(c.ping(), timeout=float(settings.REDIS_TIMEOUT) * 2))
    except Exception:  # noqa: BLE001
        return False


# ─── Publish / subscribe ─────────────────────────────────────────────────


class _Broker:
    """This process's subscribers, by channel."""

    def __init__(self):
        self.subscribers: dict[str, set[asyncio.Queue]] = {}

    def add(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.subscribers.setdefault(channel, set()).add(q)
        return q

    def remove(self, channel: str, q: asyncio.Queue) -> None:
        subs = self.subscribers.get(channel)
        if subs is not None:
            subs.discard(q)
            if not subs:
                self.subscribers.pop(channel, None)

    def deliver(self, channel: str, message: dict) -> int:
        n = 0
        for q in list(self.subscribers.get(channel, ())):
            if q.full():
                # A subscriber that stopped reading loses its oldest, not the newest.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(message)
            n += 1
        return n


class _Hub:
    """The one Redis connection this process's subscribers share."""

    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.connected = False

    def ensure(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._pump(), name="redis:subscriptions")

    async def _pump(self) -> None:
        failures = 0
        while True:
            pubsub = None
            try:
                c = client()
                if c is None:
                    return
                pubsub = c.pubsub(ignore_subscribe_messages=True)
                await pubsub.psubscribe(_CHANNELS + "*")
                self.connected, failures = True, 0
                while True:
                    message = await pubsub.get_message(timeout=1.0)
                    if message is None or message.get("type") != "pmessage":
                        continue
                    channel = _text(message.get("channel"))[len(_CHANNELS):]
                    try:
                        body = json.loads(message.get("data"))
                    except (TypeError, ValueError):
                        continue
                    _broker.deliver(channel, body)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect, a little later each time
                failures += 1
                mark_down(exc, "subscribe")
            finally:
                self.connected = False
                if pubsub is not None:
                    try:
                        await asyncio.wait_for(pubsub.aclose(), timeout=1.0)
                    except Exception:  # noqa: BLE001
                        pass
            if not _broker.subscribers:
                return
            await asyncio.sleep(min(30.0, 0.5 * 2 ** failures))

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None


def _text(value) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value or "")


_broker = _Broker()
_hub = _Hub()


async def publish(channel: str, message: dict) -> None:
    """Send `message` to every subscriber of `channel`, in every process when
    there is Redis, in this one when there is not (or it is down). Never raises."""
    data = json.dumps(message, default=str)
    sent = await run(lambda c: c.publish(_CHANNELS + channel, data), None, what="publish")
    if sent is None or not _hub.connected:
        # No Redis, or this process is not hearing from it right now: its own
        # subscribers hear it directly. (Connected, they hear it through Redis.)
        _broker.deliver(channel, json.loads(data))


async def subscribe(channel: str) -> AsyncIterator[dict]:
    """Every message published to `channel` from now on, until the caller
    stops iterating. Across processes with Redis, within this one without."""
    q = _broker.add(channel)
    if client() is not None:
        _hub.ensure()
    try:
        while True:
            yield await q.get()
    finally:
        _broker.remove(channel, q)


def subscribers(channel: str) -> int:
    return len(_broker.subscribers.get(channel, ()))


async def close() -> None:
    """At shutdown: the subscription connection, then the client's pool."""
    global _client, _client_loop
    await _hub.stop()
    c, _client, _client_loop = _client, None, None
    if c is not None:
        try:
            await asyncio.wait_for(c.aclose(), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass


def reset_for_tests() -> None:
    global _client, _client_loop, _down_until
    _client, _client_loop, _down_until = None, None, 0.0
    _broker.subscribers.clear()
    _hub.task = None
    _hub.connected = False
