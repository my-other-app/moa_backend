"""What a check-in sets off once the guest is through: a note and a push to
them, and the badges it may earn them and the club.

None of it is the door's business, so none of it holds the door up. The
scan answers first; this runs afterwards as a job (`checkins.after`, written
in the check-in's own transaction: app.core.jobs), on a session of its own,
with the push sent from a worker thread so a slow FCM never stalls the event
loop. Run twice (the queue is at least once), the guest still has one note:
it is not written again for a check-in that already has one.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from app.core import jobs
from app.db.core import AsyncSessionLocal
from app.db.listeners import add_loader_criteria

logger = logging.getLogger(__name__)

TITLE = "✅ Checked in successfully!"

@dataclass(frozen=True)
class Admitted:
    user_id: int
    event_id: int
    event_name: str
    club_id: Optional[int]


@asynccontextmanager
async def session_scope():
    """A session for work that outlives the request. Tests swap this for one
    on their own transaction."""
    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


async def enqueue(session, admitted: Admitted) -> None:
    """The hooks as a job, in the caller's transaction: they run once it commits."""
    await jobs.enqueue(session, "checkins.after", {
        "user_id": admitted.user_id, "event_id": admitted.event_id, "event_name": admitted.event_name, "club_id": admitted.club_id,
    })


@jobs.handler("checkins.after", max_attempts=4, timeout=120.0, priority=jobs.HIGH)
async def _after_job(p: dict) -> None:
    await run(Admitted(user_id=int(p["user_id"]), event_id=int(p["event_id"]), event_name=p["event_name"], club_id=p.get("club_id")))


async def run(admitted: Admitted) -> None:
    """Never raises: a check-in that happened stays happened."""
    try:
        async with session_scope() as session:
            await _notify(session, admitted)
            await _badges(session, admitted)
    except Exception:  # noqa: BLE001
        logger.exception("after-check-in hooks failed for user %s at event %s", admitted.user_id, admitted.event_id)


async def _notify(session, a: Admitted) -> None:
    from app.api.notifications import service as notes
    from app.core.notifications import service as push

    from sqlalchemy import select

    from app.api.notifications.models import Notifications

    body = f"Welcome to {a.event_name}. Enjoy the event!"
    try:
        told = await session.scalar(select(Notifications.id).where(
            Notifications.user_id == a.user_id, Notifications.event_id == a.event_id, Notifications.type == "event_checkin",
        ).limit(1))
        if told is not None:
            return
        await notes.create_notification(
            session=session, user_id=a.user_id, title=TITLE, description=body, type="event_checkin",
            event_id=a.event_id, from_club_id=a.club_id, data={"event_name": a.event_name},
        )
        token = await push.get_fcm_token_for_user(session, a.user_id)
        # End the read before the network call, so no pooled connection is
        # held while FCM answers.
        await session.commit()
        if not token:
            return
        dead: list[str] = []
        # The FCM client blocks on its HTTP call: a thread, with a loop of its own.
        await asyncio.to_thread(_send_blocking, token, TITLE, body, {"type": "check_in", "event_id": str(a.event_id)}, dead)
        if dead:
            await push.prune_tokens(session, dead)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("check-in notification failed for user %s", a.user_id)


def _send_blocking(token: str, title: str, body: str, data: dict, dead: list) -> bool:
    from app.core.notifications import service as push

    return asyncio.run(push._send_to_token(token, title, body, data, dead=dead))


async def _badges(session, a: Admitted) -> None:
    from app.api.badges.service import check_and_award_club_badges, check_and_award_user_badges

    try:
        await check_and_award_user_badges(session, user_id=a.user_id)
        if a.club_id:
            await check_and_award_club_badges(session, club_id=a.club_id)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("badge check failed after check-in for user %s", a.user_id)
