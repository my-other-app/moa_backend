"""What other pages say about an event's quest (CONTRACT §8.5, §8.6).

- `flag`: `quest` on the event's info and on its public page, so a client
  asks for the card only when there is one.
- `network_progress`: the "meet people" tasks' counts right after a QR save,
  for the scanner's "3 of 5 met" (the save's own evaluation; the job the
  save queues stays the safety net).
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)


async def flag(session, event_id: Optional[int], user=None, *, per_viewer: bool = True) -> dict:
    """{"enabled", "can_take_part"}: on, and (for a signed-in viewer) whether
    they hold a live ticket for it. `can_take_part` is null on a page that
    is the same for everyone (`per_viewer=False`)."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.quests import switch

    enabled = bool(event_id) and await switch.enabled_quest_id(session, event_id) is not None
    can = None
    if per_viewer:
        can = False
        if enabled and user is not None and getattr(user, "id", None) is not None:
            can = (
                await session.scalar(
                    select(R.id).where(R.event_id == event_id, R.user_id == user.id, R.is_deleted == False, R.ticket_revoked_at.is_(None))  # noqa: E712
                )
                is not None
            )
    return {"enabled": enabled, "can_take_part": can}


async def network_progress(session, event_id: Optional[int], user_id: int, *, evaluate: bool) -> Optional[dict]:
    """The saver's own network tasks at this event, counted now. None when
    the event has no quest on, the saver has no ticket, or there is no such
    task. Never raises: a save that happened stays happened."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.quests import engine, switch
    from app.api.quests import kinds as K
    from app.api.quests.models import QuestProgress, QuestTasks

    if not event_id:
        return None
    try:
        quest_id = await switch.enabled_quest_id(session, event_id)
        if quest_id is None:
            return None
        tasks = list(
            (
                await session.scalars(
                    select(QuestTasks)
                    .where(QuestTasks.quest_id == quest_id, QuestTasks.kind == K.NETWORK, QuestTasks.archived_at.is_(None))
                    .order_by(QuestTasks.sort_order, QuestTasks.created_at)
                )
            ).all()
        )
        reg_id = await session.scalar(select(R.id).where(R.event_id == event_id, R.user_id == user_id, R.is_deleted == False))  # noqa: E712
        if not tasks or reg_id is None:
            return None
        if evaluate:
            # The same rule as the job's (checked-in people, at the event, by QR, the minute cap).
            await engine.evaluate(session, event_id, kinds=[K.NETWORK], reg_ids=[reg_id])
        rows = {
            p.task_id: p
            for p in (
                await session.scalars(
                    select(QuestProgress)
                    .where(QuestProgress.registration_id == reg_id, QuestProgress.task_id.in_([t.id for t in tasks]))
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
        out = []
        for t in tasks:
            p = rows.get(t.id)
            target = K.target(t.kind, t.config)
            done = p is not None and p.done_at is not None and p.revoked_at is None
            out.append({"task_id": t.id, "title": t.title, "count": target if done else (p.count if p else 0), "target": target, "done": done})
        return {"event_id": event_id, "tasks": out}
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("network quest progress failed for user %s at event %s", user_id, event_id)
        return None
