"""What a check-in sets off once the guest is through: a note and a push to
them, and the badges it may earn them and the club.

When the event has People Like You (the club's plan has networking) and its
window is open, the note says so and its tap opens the list
(`open: people` in the push's data); otherwise it is the plain welcome.
Either way it is one push, and nothing about it can undo the check-in.

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
#: The note when People Like You is open to them.
PEOPLE_TITLE = "✅ You're checked in"
PEOPLE_BODY = "Welcome to {event}. See who here shares your interests."

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

    try:
        told = await session.scalar(select(Notifications.id).where(
            Notifications.user_id == a.user_id, Notifications.event_id == a.event_id, Notifications.type == "event_checkin",
        ).limit(1))
        if told is not None:
            return
        # The app opens the event from `event_checkin`, and the list from
        # `open: people`; an app from before the list just opens the event.
        data = {"type": "event_checkin", "event_id": str(a.event_id)}
        if await _people_like_you(session, a.event_id):
            title, body = PEOPLE_TITLE, PEOPLE_BODY.format(event=a.event_name)
            data["open"] = "people"
        else:
            title, body = TITLE, f"Welcome to {a.event_name}. Enjoy the event!"
        line = await notes.create_notification(
            session=session, user_id=a.user_id, title=title, description=body, type="event_checkin",
            event_id=a.event_id, from_club_id=a.club_id,
            data={"event_name": a.event_name, **({"open": "people"} if "open" in data else {})},
        )
        # A tap on the push marks the welcome read.
        if getattr(line, "id", None) is not None:
            data["notification_id"] = str(line.id)
        token = await push.get_fcm_token_for_user(session, a.user_id)
        # The icon's badge: their unread count, the welcome just written included.
        badge = await push.unread_badge(session, a.user_id) if token else None
        # End the read before the network call, so no pooled connection is
        # held while FCM answers.
        await session.commit()
        if not token:
            return
        dead: list[str] = []
        # The FCM client blocks on its HTTP call: a thread, with a loop of its own.
        await asyncio.to_thread(_send_blocking, token, title, body, data, dead, badge)
        if dead:
            await push.prune_tokens(session, dead)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("check-in notification failed for user %s", a.user_id)


async def _people_like_you(session, event_id: int) -> bool:
    """Whether the guest just let in can look for people like them now: the
    club's plan has networking, and the window (to an hour after the end) is
    open. A doubt is the plain welcome: this never raises."""
    from app.api.events.models import Events
    from app.api.events.networking import is_within_networking_window
    from app.api.subscriptions import gates

    try:
        # In a savepoint: a read that fails here leaves the note's own
        # transaction usable, so the plain welcome still goes.
        async with session.begin_nested():
            event = await session.get(Events, event_id)
            if event is None or not is_within_networking_window(event)[0]:
                return False
            return bool(await gates.allows(session, event.club_id, "networking", event=event))
    except Exception:  # noqa: BLE001
        logger.exception("could not tell whether People Like You is open at event %s", event_id)
        return False


def _send_blocking(token: str, title: str, body: str, data: dict, dead: list, badge: Optional[int] = None) -> bool:
    from app.core.notifications import service as push

    return asyncio.run(push._send_to_token(token, title, body, data, dead=dead, badge=badge))


async def _badges(session, a: Admitted) -> None:
    from app.api.badges.service import check_and_award_club_badges, check_and_award_user_badges

    try:
        await check_and_award_user_badges(session, user_id=a.user_id)
        if a.club_id:
            await check_and_award_club_badges(session, club_id=a.club_id)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("badge check failed after check-in for user %s", a.user_id)
