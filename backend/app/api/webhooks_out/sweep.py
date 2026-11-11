"""Outgoing webhooks' hour, on one process at a time (an advisory lock):

- changes older than RETENTION (30 days) are deleted, a batch at a time,
  their deliveries with them (ON DELETE CASCADE): the delivery log is kept
  30 days;
- a rotated secret's overlap that has ended is emptied, so the old secret
  is gone from the database too.

Retries need no sweep: each is a job written with its run time
(delivery.py), which survives a restart.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text, update

from app.api.webhooks_out.models import WebhookEndpoints
from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.WEBHOOKS_SWEEP
SWEEP_INTERVAL = 3600
RETENTION = timedelta(days=30)
BATCH = 5000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def purge(session, now: Optional[datetime] = None) -> dict:
    """The hour's work (under the sweep's lock, run_sweep)."""
    now = now or _now()
    deleted = 0
    for _ in range(40):
        n = (await session.execute(
            text("DELETE FROM webhook_events WHERE id IN (SELECT id FROM webhook_events WHERE created_at < :cutoff LIMIT :batch)"),
            {"cutoff": now - RETENTION, "batch": BATCH},
        )).rowcount or 0
        await session.commit()
        deleted += n
        if n < BATCH:
            break
    emptied = (await session.execute(
        update(WebhookEndpoints)
        .where(WebhookEndpoints.previous_secret_expires_at.is_not(None), WebhookEndpoints.previous_secret_expires_at <= now)
        .values(previous_secret_enc=None, previous_secret_expires_at=None)
    )).rowcount or 0
    await session.commit()
    if deleted or emptied:
        logger.info("webhooks sweep: %d change(s) past 30 days deleted, %d old secret(s) emptied", deleted, emptied)
    return {"deleted": deleted, "secrets_emptied": emptied}


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[dict]:
    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        return await purge(session, now)


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(47)
    while True:
        try:
            async with deliveries.session_scope() as session:
                await run_sweep(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("webhooks sweep failed")
        await asyncio.sleep(interval)
