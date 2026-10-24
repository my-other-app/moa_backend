"""The inbox, pushed: what changed in a club's WhatsApp conversations, told
to the dashboards that have the inbox open.

Whoever changes a conversation -- a message arriving through the webhook, a
status coming back, a reply sent, a conversation read -- calls `changed`
after the commit. It publishes on the club's channel (app/core/redis.py:
through Redis to every process, or within this one without it). A dashboard
holds one server-sent-event stream (`GET /clubs/whatsapp/stream`, behind
APP_LIVE_SSE like the live room's) and, told that a conversation changed,
reads it again with the same requests it polls with. Nothing a message says
travels on the channel: only which conversation, and what kind of change.

Off (APP_LIVE_SSE false) the stream answers 404 and the dashboard polls.
Best effort, as the live room's bus: a message lost to a Redis blip shows at
the next poll the dashboard makes when its stream ends (every few minutes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import suppress
from typing import Optional

from starlette.responses import Response

from app.config import settings
from app.core import redis

logger = logging.getLogger(__name__)

PING_SECONDS = 15.0
RETRY_MS = 3000
#: A stream lives this long, then ends; the dashboard reconnects (and so a
#: session ended elsewhere, or a permission taken away, stops being served).
LIFETIME = 300.0
SEND_TIMEOUT = 60.0


def on() -> bool:
    return bool(settings.LIVE_SSE)


def channel(club_id: int) -> str:
    return f"wa:club:{int(club_id)}"


async def changed(club_id: int, conversation_id: Optional[int], kind: str) -> None:
    """`kind`: message (one arrived or was sent), status (one moved on),
    read, conversation (closed, opened, linked). Never raises."""
    try:
        await redis.publish(channel(club_id), {"c": conversation_id, "k": kind, "t": time.time()})
    except Exception:  # noqa: BLE001 - the dashboard's poll catches what this loses
        logger.warning("could not tell club %s's inbox what changed", club_id)


def frame(event: str, data: dict, event_id: Optional[str] = None) -> bytes:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class InboxStream(Response):
    """One dashboard's stream: `changed` frames as conversations change, a
    `ping` in each quiet PING_SECONDS, an end after its lifetime."""

    media_type = "text/event-stream"

    def __init__(self, club_id: int, lifetime: float = LIFETIME):
        self.status_code = 200
        self.media_type = "text/event-stream; charset=utf-8"
        self.background = None
        self.init_headers({"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        self.club_id = club_id
        self.lifetime = lifetime * random.uniform(0.9, 1.1)
        # Subscribed now, before the response starts, so nothing in between is missed.
        self.subscription = redis.subscribe(channel(club_id))

    async def __call__(self, scope, receive, send) -> None:
        closed = asyncio.Event()

        async def watch() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    closed.set()
                    return

        watcher = asyncio.get_running_loop().create_task(watch())
        try:
            await self._serve(send, closed)
        except TimeoutError:
            logger.info("an inbox stream stopped reading; let go")
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            with suppress(Exception):
                await self.subscription.aclose()

    async def _serve(self, send, closed: asyncio.Event) -> None:
        async def write(data: bytes, more: bool = True) -> None:
            async with asyncio.timeout(SEND_TIMEOUT):
                await send({"type": "http.response.body", "body": data, "more_body": more})

        async with asyncio.timeout(SEND_TIMEOUT):
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await write(f"retry: {RETRY_MS}\n\n".encode())
        await write(frame("ready", {"club": self.club_id}))
        ends = time.monotonic() + self.lifetime
        iterator = self.subscription.__aiter__()
        while not closed.is_set():
            left = ends - time.monotonic()
            if left <= 0:
                break
            closing = asyncio.ensure_future(closed.wait())
            nxt = asyncio.ensure_future(iterator.__anext__())
            done, _pending = await asyncio.wait({closing, nxt}, timeout=min(PING_SECONDS, left), return_when=asyncio.FIRST_COMPLETED)
            closing.cancel()
            if nxt in done:
                try:
                    message = nxt.result()
                except StopAsyncIteration:
                    break
                await write(frame("changed", {"conversation_id": message.get("c"), "kind": message.get("k")}))
                continue
            nxt.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await nxt
            if closed.is_set():
                return
            await write(b": ping\n\n")
        await write(frame("end", {}), more=False)
