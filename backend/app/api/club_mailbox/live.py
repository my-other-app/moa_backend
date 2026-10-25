"""The mailbox, pushed: which thread changed, told to the dashboards that
have Mail open -- the WhatsApp inbox's stream (app/api/club_whatsapp/live.py),
on a channel of its own.

Whoever changes a thread (mail arriving, a reply sent, a thread read or
moved) calls `changed` after the commit. A dashboard holds one stream
(`GET /clubs/mailbox/stream`, behind APP_LIVE_SSE) and, told a thread
changed, reads it again with the requests it polls with. Nothing a mail says
travels on the channel: the thread's id and the kind of change. Off, the
stream answers 404 and the dashboard polls.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from app.api.club_whatsapp import live as whatsapp_live
from app.core import redis

logger = logging.getLogger(__name__)

on = whatsapp_live.on
admit = whatsapp_live.admit
release = whatsapp_live.release


def channel(club_id: int) -> str:
    return f"mail:club:{int(club_id)}"


async def changed(club_id: int, thread_id: Optional[int], kind: str) -> None:
    """`kind`: message (one arrived or was sent), read, thread (moved,
    deleted). Never raises."""
    try:
        await redis.publish(channel(club_id), {"c": thread_id, "k": kind, "t": time.time()})
    except Exception:  # noqa: BLE001 - the dashboard's poll catches what this loses
        logger.warning("could not tell club %s's mailbox what changed", club_id)


class MailStream(whatsapp_live.InboxStream):
    """The inbox stream's frames (`ready`, `changed`, a ping, `end`), from
    the mailbox's channel. `changed` carries `conversation_id`: the thread."""

    def __init__(self, club_id: int, lifetime: float = whatsapp_live.LIFETIME, address: Optional[str] = None):
        self.status_code = 200
        self.media_type = "text/event-stream; charset=utf-8"
        self.background = None
        self.init_headers({"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        self.club_id = club_id
        self.address = address
        self.lifetime = lifetime * random.uniform(0.9, 1.1)
        self.subscription = redis.subscribe(channel(club_id))
