"""The waiting lists' minute: offers that ran out closed, and every event with
people waiting offered the places it has free -- one ticket refunded or
cancelled from anywhere reaches the line within the minute. On one worker
(an advisory lock on a connection of its own)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.WAITLIST_SWEEP
SWEEP_INTERVAL = 60


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[dict]:
    """What it did, or None when another worker is running it."""
    from app.api.ticketing import waitlist

    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        expired = await waitlist.expire(session, now=now)
        offered = 0
        for event_id in sorted(set(expired) | set(await waitlist.events_waiting(session))):
            try:
                offered += len(await waitlist.fill(session, event_id, now=now))
            except Exception:  # noqa: BLE001 - one event's trouble is not the rest's
                await session.rollback()
                logger.exception("waitlist sweep: event %s could not be filled", event_id)
        return {"expired_events": len(expired), "offered": offered}


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    """Runs for the life of the process."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(29)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and (done["offered"] or done["expired_events"]):
                    logger.info("waitlist sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("waitlist sweep failed")
        await asyncio.sleep(interval)
