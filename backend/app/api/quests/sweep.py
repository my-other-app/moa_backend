"""Every five minutes, on one process: the kinds no event reports (a poster
link's visitors and registrations, a finished profile, a follow) for every
guest of each event whose quest is on and whose day it is (from a day before
the start to a day after the end) -- CONTRACT §5.1.

One grouped query per task (evaluators.py), in batches of guests; nothing
for an event without a quest on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import select

from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.QUESTS_SWEEP
FIRST_LOOK_SECONDS = 120
EVERY_SECONDS = 300
AROUND = timedelta(hours=24)


async def due_events(session, now: datetime) -> list[int]:
    from app.api.events.models import Events, ends_at_sql
    from app.api.quests import kinds as K
    from app.api.quests.models import Quests, QuestTasks

    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)  # ends_at_sql is UTC wall time
    polled = select(QuestTasks.quest_id).where(QuestTasks.kind.in_(K.POLLED), QuestTasks.archived_at.is_(None))
    rows = await session.scalars(
        select(Quests.event_id)
        .join(Events, Events.id == Quests.event_id)
        .where(
            Quests.enabled == True,
            Events.is_deleted == False,
            Quests.id.in_(polled),  # noqa: E712
            sa.func.timezone("UTC", Events.event_datetime) - sa.text("INTERVAL '24 hours'") <= utc_now,
            ends_at_sql(Events) + sa.text("INTERVAL '24 hours'") >= utc_now,
        )
    )
    return list(rows.all())


async def sweep(session, now: Optional[datetime] = None) -> int:
    from app.api.quests import engine
    from app.api.quests import kinds as K

    now = now or datetime.now(timezone.utc)
    done = 0
    for event_id in await due_events(session, now):
        try:
            done += await engine.evaluate(session, event_id, kinds=K.POLLED, everyone=False, reg_ids=None)
        except Exception:  # noqa: BLE001 - one event's trouble is not the next one's
            await session.rollback()
            logger.exception("quest sweep failed for event %s", event_id)
    return done


async def run(session) -> Optional[int]:
    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        return await sweep(session)


async def sweep_loop() -> None:
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            async with deliveries.session_scope() as session:
                await run(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("quest sweep failed")
        await asyncio.sleep(EVERY_SECONDS)
