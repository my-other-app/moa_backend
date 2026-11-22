"""The games pushed: server-sent events, on the live room's transport.

    GET /api/v1/games/{event_id}/stream            a player's phone
    GET /api/v1/games/screen/{screen_token}/stream a venue screen

Both carry the same public bodies, so neither needs a login on the stream
itself (a phone re-reads its own rank with a GET when a `board` arrives):

    event: board     {"cards": {...rows}, "hunt": {...rows}}
    event: feed      [latest 3, not hidden]
    event: hunt      the hunt's state and timer
    event: settings  the look, names and what is on

`id:` is a version in microseconds, the same clock in every process, so a
client reconnecting with Last-Event-ID is sent only what changed since. One
`Feed` per event per process holds the bodies and rebuilds them, once however
many streams read it, when bus.py says a topic changed (and every RESYNC
seconds, telling streams only about bodies that differ). A stream lives
LIFETIME seconds and ends with a `retry` so the client reconnects.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Optional

import orjson
from starlette.responses import StreamingResponse

from app.api.games import bus, common, views
from app.core import redis

logger = logging.getLogger(__name__)

PING_SECONDS = 15.0
RETRY_MS = 3000
RESYNC_SECONDS = 10.0
LINGER_SECONDS = 20.0
LIFETIME = 600.0
MAX_STREAMS = 5000
FIRST_BUILD_WAIT = 10.0


@asynccontextmanager
async def _default_scope() -> AsyncIterator[Any]:
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


#: Where a feed gets the session it builds in; the suite points it at the test's.
session_scope = _default_scope


def frame(event: str, data: bytes, version: Optional[int] = None) -> bytes:
    head = b"id: %d\nevent: %s\n" % (version, event.encode()) if version is not None else b"event: %s\n" % event.encode()
    return head + b"data: " + data + b"\n\n"


class Full(Exception):
    """No room for another stream here."""


class Feed:
    """One event's streams in this process, and what they show."""

    def __init__(self, hub: "Hub", event_id: int):
        self.hub = hub
        self.event_id = event_id
        self.listeners: set[asyncio.Event] = set()
        self.versions: dict[str, int] = {}
        self.bodies: dict[str, bytes] = {}
        self.sub = None
        self.task: Optional[asyncio.Task] = None
        self.gone: Optional[tuple[int, str]] = None
        self.idle_since: Optional[float] = time.monotonic()

    def start(self) -> None:
        if self.sub is None:
            # Subscribed before the first build: nothing told in between is missed.
            self.sub = redis.subscribe(bus.channel(self.event_id))
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._run(), name=f"games-feed:{self.event_id}")

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        sub, self.sub = self.sub, None
        if sub is not None:
            await sub.aclose()

    def add(self) -> asyncio.Event:
        wake = asyncio.Event()
        self.listeners.add(wake)
        self.idle_since = None
        return wake

    def remove(self, wake: asyncio.Event) -> None:
        self.listeners.discard(wake)
        if not self.listeners:
            self.idle_since = time.monotonic()

    async def build(self, topics) -> None:
        """Rebuild `topics`; the ones whose body changed get a new version and wake the streams."""
        from app.api.events.models import Events
        from sqlalchemy import select

        changed = False
        async with session_scope() as s:
            event = await s.scalar(select(Events).where(Events.id == self.event_id, Events.is_deleted == False))  # noqa: E712
            if event is None:
                self.gone = (404, "Event not found")
                changed = True
            else:
                settings = await common.settings_of(s, self.event_id)
                for name in topics:
                    body = orjson.dumps(await views.topic(s, event, settings, name), default=str)
                    if self.bodies.get(name) != body:
                        self.bodies[name] = body
                        self.versions[name] = bus.next_version()
                        changed = True
                await s.commit()
        if changed:
            for wake in list(self.listeners):
                wake.set()

    async def _run(self) -> None:
        try:
            await self.build(bus.TOPICS)
            while True:
                topics: set = set()
                try:
                    async with asyncio.timeout(RESYNC_SECONDS):
                        message = await self.sub.__anext__()
                    topics.update(message.get("t") or ())
                    # What else was told in the same moment goes in one build.
                    await asyncio.sleep(0.05)
                except TimeoutError:
                    topics.update(bus.TOPICS)
                if not self.listeners and self.idle_since is not None and time.monotonic() - self.idle_since >= LINGER_SECONDS:
                    if self.hub.feeds.get(self.event_id) is self:
                        del self.hub.feeds[self.event_id]
                    return
                try:
                    await self.build(topics & set(bus.TOPICS) or set(bus.TOPICS))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next message or resync tries again
                    logger.exception("games feed %s could not rebuild", self.event_id)
                    await asyncio.sleep(1.0)
        finally:
            sub, self.sub = self.sub, None
            if sub is not None:
                with suppress(Exception):
                    await sub.aclose()


class Hub:
    def __init__(self):
        self.feeds: dict[int, Feed] = {}
        self.open = 0

    def feed(self, event_id: int) -> Feed:
        feed = self.feeds.get(event_id)
        if feed is None:
            feed = self.feeds[event_id] = Feed(self, event_id)
        feed.start()
        return feed

    def admit(self) -> None:
        if self.open >= MAX_STREAMS:
            raise Full("this server holds all the streams it can")
        self.open += 1

    def release(self) -> None:
        self.open = max(0, self.open - 1)

    async def close(self) -> None:
        feeds = list(self.feeds.values())
        self.feeds.clear()
        for feed in feeds:
            await feed.stop()


hub = Hub()


def parse_version(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


async def _events(feed: Feed, last_id: Optional[int], lifetime: float) -> AsyncIterator[bytes]:
    wake = feed.add()
    ends = time.monotonic() + lifetime
    try:
        yield b"retry: %d\n\n" % RETRY_MS
        with suppress(TimeoutError):
            async with asyncio.timeout(FIRST_BUILD_WAIT):
                while not feed.bodies and feed.gone is None:
                    wake.clear()
                    await wake.wait()
        # Everything newer than what the client has (everything, for a new one).
        sent: dict[str, int] = {t: last_id for t in bus.TOPICS} if last_id is not None else {}
        quiet = time.monotonic()
        while True:
            if feed.gone is not None:
                yield frame("gone", orjson.dumps({"status": feed.gone[0], "message": feed.gone[1]}))
                return
            wake.clear()
            for topic in bus.TOPICS:
                version = feed.versions.get(topic)
                if version is not None and version != sent.get(topic) and (last_id is None or version > last_id or topic not in sent):
                    sent[topic] = version
                    yield frame(topic, feed.bodies[topic], version)
                    quiet = time.monotonic()
            now = time.monotonic()
            if now >= ends:
                yield b"retry: %d\n\n" % (RETRY_MS + random.randint(0, RETRY_MS))
                return
            if now - quiet >= PING_SECONDS:
                yield frame("ping", b'{"t":%d}' % (time.time_ns() // 1_000_000))
                quiet = now
            with suppress(TimeoutError):
                async with asyncio.timeout(min(ends, quiet + PING_SECONDS) - time.monotonic()):
                    await wake.wait()
    finally:
        feed.remove(wake)
        feed.hub.release()


def response(event_id: int, last_event_id: Optional[str]) -> StreamingResponse:
    """The stream of one phone or screen for the event (the caller has checked it may have one)."""
    try:
        hub.admit()
    except Full as exc:
        raise common.err(503, "STREAM_FULL", str(exc))
    try:
        feed = hub.feed(event_id)
        body = _events(feed, parse_version(last_event_id), LIFETIME * random.uniform(0.9, 1.1))
    except BaseException:
        hub.release()
        raise
    return StreamingResponse(body, media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
