"""Giving back the pieces of store orders whose hold ran out: every minute,
on one worker (an advisory lock on a connection of its own; nothing here
calls out, so it is held for the run)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from app.db.locks import singleton
from app.db import lock_keys

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.STORE_SWEEP
SWEEP_INTERVAL = 60


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[int]:
    """Holds given back, or None when another worker is running it."""
    from app.api.store import service

    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        return await service.expire_holds(session, now)


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    """Runs for the life of the process."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(23)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done:
                    logger.info("store holds sweep: gave back %s order(s)", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("store holds sweep failed")
        await asyncio.sleep(interval)
