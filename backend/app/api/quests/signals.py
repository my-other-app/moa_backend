"""The hooks other modules call, and the jobs behind them (CONTRACT §5.1, §10).

`touch(session, event_id, kinds, ...)` is what a check-in, a Network save, a
poll answer, a question, a feedback form, a poster or a follow calls, in its
own transaction. For an event whose quest is off (every event, until an
organiser turns one on) it is one cached read and nothing else. When it is
on, it queues `quests.evaluate` for that guest and kind, at most once per
guest and kind in each BUCKET_SECONDS: the job runs just after its bucket
closes, so it sees every change of the bucket, and a burst of poll answers
is one evaluation. It never raises into the caller: a quest must not break
a check-in.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.core import jobs

logger = logging.getLogger(__name__)

EVALUATE = "quests.evaluate"
RECOMPUTE = "quests.recompute"
NOTIFY = "quests.notify"
NOTIFY_HELD = "quests.notify_held"

#: Evaluations of one guest and kind are gathered this long. 0: each at once
#: (the test suite, where nothing runs later).
BUCKET_SECONDS = 10


async def touch(session, event_id: Optional[int], kinds: Iterable[str], *, registration_id=None, user_id: Optional[int] = None, everyone: bool = False) -> None:
    if not event_id:
        return
    from app.api.quests import switch

    # A cached answer costs nothing; a fresh one is read in its own savepoint (switch.py).
    if await switch.enabled_quest_id(session, event_id) is None:
        return
    try:
        async with session.begin_nested():
            kinds = sorted(set(kinds))
            who = f"r{registration_id}" if registration_id is not None else (f"u{user_id}" if user_id is not None else "all")
            payload = {"event_id": int(event_id), "kinds": kinds, "everyone": bool(everyone)}
            if registration_id is not None:
                payload["reg_ids"] = [str(registration_id)]
            if user_id is not None:
                payload["user_ids"] = [int(user_id)]
            run_at, dedupe = None, None
            if BUCKET_SECONDS:
                bucket = int(time.time() // BUCKET_SECONDS)
                run_at = datetime.fromtimestamp((bucket + 1) * BUCKET_SECONDS + 1, tz=timezone.utc)
                dedupe = f"quests:eval:{event_id}:{who}:{','.join(kinds)}:{bucket}"[:200]
            await jobs.enqueue(session, EVALUATE, payload, run_at=run_at, dedupe_key=dedupe)
    except Exception:  # noqa: BLE001 - a quest never stands in the way of what set it off
        logger.exception("quest hook failed for event %s (%s)", event_id, ",".join(kinds) if not isinstance(kinds, str) else kinds)


async def recompute(session, event_id: int) -> None:
    """The full recompute, queued in the caller's transaction (one at a time per event)."""
    await jobs.enqueue(session, RECOMPUTE, {"event_id": int(event_id)}, dedupe_key=f"quests:recompute:{event_id}")


async def unlocked(session, unlock_id) -> None:
    await jobs.enqueue(session, NOTIFY, {"unlock_id": str(unlock_id)}, dedupe_key=f"quests:notify:{unlock_id}")


async def held(session, claim_id) -> None:
    await jobs.enqueue(session, NOTIFY_HELD, {"claim_id": str(claim_id)}, dedupe_key=f"quests:held:{claim_id}")


@jobs.handler(EVALUATE, max_attempts=4, timeout=120.0, concurrency=2, priority=jobs.NORMAL)
async def _evaluate_job(p: dict) -> None:
    from app.api.quests import engine

    async with jobs.scope() as session:
        await engine.evaluate(
            session,
            int(p["event_id"]),
            kinds=p.get("kinds") or None,
            reg_ids=p.get("reg_ids"),
            user_ids=p.get("user_ids"),
            everyone=bool(p.get("everyone")),
        )


@jobs.handler(RECOMPUTE, max_attempts=3, timeout=900.0, concurrency=1, priority=jobs.NORMAL)
async def _recompute_job(p: dict) -> None:
    from app.api.quests import engine

    async with jobs.scope() as session:
        await engine.evaluate(session, int(p["event_id"]), everyone=True)


@jobs.handler(NOTIFY, max_attempts=4, timeout=60.0, priority=jobs.HIGH, urgent=True)
async def _notify_job(p: dict) -> None:
    from app.api.quests import notify

    await notify.unlock(p["unlock_id"])


@jobs.handler(NOTIFY_HELD, max_attempts=4, timeout=60.0, priority=jobs.HIGH, urgent=True)
async def _notify_held_job(p: dict) -> None:
    from app.api.quests import notify

    await notify.held(p["claim_id"])
