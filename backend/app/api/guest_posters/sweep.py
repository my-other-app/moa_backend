"""Guests' poster photos and renders removed once they are not needed
(CONTRACT.md §7), every hour, on one process:

- 30 days after the event's end (start + duration), every guest's photo and
  rendered posters of the event;
- at any time, those of a registration removed, given back or revoked, or of
  a deleted event.

The rows stay (the counts the organiser reads), with `purged_at` set and the
photo, framing and renders emptied. A batch at a time; what is left waits
for the next hour.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import or_, select

from app.api.events.models import EventRegistrationsLink, Events, ends_at_sql
from app.api.guest_posters import photos
from app.api.guest_posters.models import GuestPosters
from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.POSTERS_SWEEP
KEEP_AFTER_END = timedelta(days=30)
BATCH = 500
FIRST_LOOK_SECONDS = 900
EVERY_SECONDS = 3600


def due_where(now: datetime):
    """A guest's poster with a photo or a render that must go now."""
    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)  # ends_at_sql is UTC wall time
    has_something = or_(GuestPosters.photo_key.isnot(None), GuestPosters.renders != sa.text("'{}'::jsonb"))
    due = or_(
        ends_at_sql(Events) + sa.text(f"INTERVAL '{KEEP_AFTER_END.days} days'") < utc_now,
        Events.is_deleted == True,  # noqa: E712
        EventRegistrationsLink.is_deleted == True,  # noqa: E712
        EventRegistrationsLink.ticket_revoked_at.isnot(None),
    )
    return [has_something, due]


async def sweep(session, now: Optional[datetime] = None, *, batch: int = BATCH) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = list((await session.scalars(
        select(GuestPosters)
        .join(Events, Events.id == GuestPosters.event_id)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == GuestPosters.registration_id)
        .where(*due_where(now))
        .order_by(GuestPosters.event_id, GuestPosters.id)
        .limit(batch)
        .with_for_update(of=GuestPosters, skip_locked=True)
    )).all())
    keys = []
    for row in rows:
        if row.photo_key and photos.is_ours(row.photo_key, row.event_id, row.registration_id):
            keys.append(row.photo_key)
        for state in (row.renders or {}).values():
            key = state.get("key") if isinstance(state, dict) else None
            if key and photos.is_ours(key, row.event_id, row.registration_id):
                keys.append(key)
        row.photo_key = row.photo_sha = None
        row.photo_width = row.photo_height = None
        row.photo_at = None
        row.framing = {}
        row.renders = {}
        row.purged_at = now
        row.updated_at = now
    await session.commit()
    if keys:
        await asyncio.to_thread(photos.delete, keys)
    return {"posters": len(rows), "objects": len(keys)}


async def run(session) -> Optional[dict]:
    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        done = await sweep(session)
    if done["posters"]:
        logger.info("poster sweep: %d guests' photos and posters removed (%d objects)", done["posters"], done["objects"])
    return done


async def sweep_loop() -> None:
    """For the life of the process; the work on one process an hour."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            async with deliveries.session_scope() as session:
                await run(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next hour tries again
            logger.exception("poster sweep failed")
        await asyncio.sleep(EVERY_SECONDS)
