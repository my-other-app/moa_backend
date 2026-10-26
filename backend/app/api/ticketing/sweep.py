"""The waiting lists' minute: offers that ran out closed, and every event with
people waiting offered the places it has free -- one ticket refunded or
cancelled from anywhere reaches the line within the minute. Registration
requests (app/api/approvals) ride along: a confirmation's hold that ran out
lapses with its offer, and an event over or called off has its pending
requests closed. On one worker (an advisory lock on a connection of its own)."""
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
        # (A hold a club's confirmation gave that ran out lapses with it: app/api/approvals.)
        expired = await waitlist.expire(session, now=now)
        offered = 0
        for event_id in sorted(set(expired) | set(await waitlist.events_waiting(session))):
            try:
                offered += len(await waitlist.fill(session, event_id, now=now))
            except Exception:  # noqa: BLE001 - one event's trouble is not the rest's
                await session.rollback()
                logger.exception("waitlist sweep: event %s could not be filled", event_id)
        try:
            # Registration requests still pending for an event now over, cancelled or gone.
            from app.api.approvals import service as approvals

            closed = await approvals.close_finished(session, now=now)
            if closed:
                logger.info("waitlist sweep: %s registration requests closed", closed)
        except Exception:  # noqa: BLE001 - the next minute tries again
            await session.rollback()
            logger.exception("waitlist sweep: registration requests could not be closed")
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
