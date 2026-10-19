"""Telling the room: a push when the host starts the quiz or launches a poll.

The console asks for it with the action (`notify` in its body: on unless
said otherwise for the quiz, off unless asked for a poll). Everyone checked
in to the event with an account gets a push on each of their devices --
`event_live_join`, a personal kind (app.core.notifications.delivery), never
an FCM topic -- and a line in their inbox; not whoever muted the club or
turned clubs' pushes off (app.core.notifications.topics), as with the club's
other pushes. Guests without the app have no devices and no inbox, and are
not counted. Nobody to tell is `nobody_checked_in` (nobody checked in with
the app) or `all_muted` (they are, and every one of them said no).

At most one per event every LIVE_NOTIFY_GAP_SECONDS (three minutes), and
LIVE_NOTIFY_PER_DAY (ten) per event per day in India. What is counted is the
audit log: each notification that went is an entry there (`live.notify`, the
event, who asked), written in the one transaction with the inbox lines and
the push's job, under a transaction lock per event (lock_keys.LIVE_NOTIFY).
Two hosts pressing at once -- two processes, two servers -- are one push and
one `rate_limited`, never two pushes; Postgres alone settles it. Only a
notification that went counts: one refused, or with nobody to tell, or that
failed, writes nothing.

The quiz or the poll has started and is committed before any of this, and
nothing here undoes it: whatever goes wrong is logged, and the console is
told the room was not (`skipped_reason`).
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.checkins.models import CheckIns
from app.api.clubs.models import ClubUsersLink
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink
from app.api.users.models import Users, UserTypes
from app.config import settings
from app.core import jobs
from app.core.notifications import fanout
from app.db import lock_keys

logger = logging.getLogger(__name__)

#: The push's and the inbox line's type: the app opens the event's live room.
TYPE = "event_live_join"
#: The audit log's action for a notification that went: the rate limit's count.
ACTION = "live.notify"
PUSH_JOB = "live.join_push"
NOTIFY_LOCK = lock_keys.LIVE_NOTIFY

BODIES = {
    "quiz": "The quiz is starting — join now",
    "poll": "A poll is live — vote now",
}

RATE_LIMITED = "rate_limited"
#: Nobody is checked in -- or nobody with the app: guests from the door have
#: no device and no inbox to tell.
NOBODY = "nobody_checked_in"
#: People with the app are checked in, and every one of them muted the club
#: or turned clubs' pushes off.
ALL_MUTED = "all_muted"
DISABLED = "disabled"
FAILED = "failed"

_tz = ZoneInfo(EVENT_TIMEZONE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def day_start(now: datetime) -> datetime:
    """Midnight in India of the day `now` falls on, in UTC."""
    return now.astimezone(_tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def outcome(sent: int = 0, reason: Optional[str] = None, last_sent_at: Optional[datetime] = None, retry_after: Optional[int] = None) -> dict:
    """The response's `notified`. `last_sent_at` and `retry_after` (seconds)
    are said when it was rate limited, for the console's "sent 2 min ago,
    wait 1 min"."""
    return {"sent": sent, "skipped_reason": reason, "last_sent_at": last_sent_at, "retry_after": retry_after}


def recipients(event_id: int, club_id: Optional[int], *, honour: bool = True) -> sa.Select:
    """The people to tell: checked in to the event, with an account (not a
    guest's), who have not muted the club or turned clubs' pushes off.
    `honour=False`: all of them, mutes and switches or not."""
    from app.core.notifications.topics import settings_off

    reg = EventRegistrationsLink
    q = (
        select(reg.user_id)
        .join(CheckIns, CheckIns.registration_id == reg.id)
        .join(Users, Users.id == reg.user_id)
        .where(
            reg.event_id == event_id, reg.is_deleted == False,  # noqa: E712
            Users.is_deleted == False, Users.user_type != UserTypes.guest,  # noqa: E712
        )
    )
    if not honour:
        return q.distinct()
    q = q.where(~settings_off("clubs", reg.user_id))
    if club_id is not None:
        q = q.where(~select(ClubUsersLink.id).where(
            ClubUsersLink.club_id == club_id, ClubUsersLink.user_id == reg.user_id,
            ClubUsersLink.is_muted == True, ClubUsersLink.is_deleted == False,  # noqa: E712
        ).exists())
    return q.distinct()


async def _sent_lately(session, event_id: int, now: datetime, today: datetime, gap: timedelta) -> tuple[int, Optional[datetime]]:
    """(notifications of the event since `today` began, the latest one's time)
    from the audit log, looking back to whichever is earlier: the day's start
    or one gap ago (a push at 23:59 still holds one at 00:01)."""
    row = (await session.execute(
        select(func.count().filter(AdminAuditLogs.created_at >= today), func.max(AdminAuditLogs.created_at))
        .where(
            AdminAuditLogs.action == ACTION, AdminAuditLogs.entity_type == "event", AdminAuditLogs.entity_id == str(event_id),
            AdminAuditLogs.created_at >= min(today, now - gap),
        )
    )).one()
    return int(row[0] or 0), row[1]


def _seconds(delta: timedelta) -> int:
    return max(1, math.ceil(delta.total_seconds()))


async def _lock(session, event_id: int) -> None:
    """This event's notifications one at a time, until this transaction ends:
    a second host waits here, then reads the first one's entry."""
    await session.execute(select(func.pg_advisory_xact_lock(NOTIFY_LOCK, event_id)))


async def tell_room(session, event, what: str, *, actor=None, ref=None, request=None, now: Optional[datetime] = None) -> dict:
    """Tell everyone checked in that the quiz is starting (`what` "quiz") or
    a poll is live ("poll"), unless the limit says not now. Called once the
    action has committed; ends the session's transaction either way. Never
    raises: the answer is the response's `notified`."""
    if what not in BODIES:
        raise ValueError(f"nothing to tell the room about {what!r}")
    # Read now: a rollback below expires the rows the session holds.
    event_id, club_id, name = int(event.id), event.club_id, event.name
    who = {"actor_id": getattr(actor, "id", None), "actor_email": getattr(actor, "email", None)}
    if not settings.LIVE_NOTIFY:
        return outcome(reason=DISABLED)
    try:
        from app.core.net import client_address

        address = (client_address(request)[:45] or None) if request is not None else None
        return await _tell(session, event_id, club_id, name, what, who, address, ref, now or now_utc())
    except Exception:  # noqa: BLE001 - the quiz or the poll stands; the room is just not told
        logger.exception("Could not tell the room of event %s about the %s", event_id, what)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("and the session could not be rolled back", exc_info=True)
        return outcome(reason=FAILED)


async def _tell(session, event_id: int, club_id, name: str, what: str, who: dict, address, ref, now: datetime) -> dict:
    # Whatever the action's answer read is done with: the lock below is held
    # by this transaction only, and goes with its commit.
    await session.commit()
    await _lock(session, event_id)
    gap = timedelta(seconds=max(0, int(settings.LIVE_NOTIFY_GAP_SECONDS)))
    today = day_start(now)
    count, last = await _sent_lately(session, event_id, now, today, gap)
    if last is not None and now - last < gap:
        await session.commit()
        return outcome(reason=RATE_LIMITED, last_sent_at=last, retry_after=_seconds(last + gap - now))
    if count >= max(0, int(settings.LIVE_NOTIFY_PER_DAY)):
        await session.commit()
        return outcome(reason=RATE_LIMITED, last_sent_at=last, retry_after=_seconds(today + timedelta(days=1) - now))
    user_ids = sorted(int(u) for u in (await session.scalars(recipients(event_id, club_id))).all())
    if not user_ids:
        there = await session.scalar(select(func.count()).select_from(recipients(event_id, club_id, honour=False).subquery()))
        await session.commit()
        return outcome(reason=ALL_MUTED if there else NOBODY)

    push_id = str(uuid.uuid4())
    body = BODIES[what]
    data = {"type": TYPE, "event_id": str(event_id), "what": what, "push_id": push_id}
    # The inbox lines, the push's job and the count's entry: all, or none.
    await fanout.insert_for_audience(
        session, fanout.users_in(user_ids), title=name, description=body, type=TYPE, data=data,
        from_club_id=club_id, event_id=event_id,
    )
    await jobs.enqueue(session, PUSH_JOB, {"user_ids": user_ids, "title": name, "body": body, "data": data}, dedupe_key=f"live:join:{push_id}")
    people = "person" if len(user_ids) == 1 else "people"
    session.add(AdminAuditLogs(
        **who, action=ACTION, entity_type="event", entity_id=str(event_id),
        summary=f"Told {len(user_ids)} checked-in {people}: {body}"[:255],
        details={"what": what, "ref": None if ref is None else str(ref), "push_id": push_id, "recipients": len(user_ids)},
        ip_address=address, created_at=now, updated_at=now,
    ))
    await session.commit()
    logger.info("Told %d checked in at event %s about the %s (%s)", len(user_ids), event_id, what, push_id)
    return outcome(sent=len(user_ids))


@jobs.handler(PUSH_JOB, max_attempts=4, timeout=300.0, concurrency=2, priority=jobs.HIGH)
async def _push_job(p: dict) -> None:
    """The push, to every device of the people it was written for, in FCM
    batches. Run again, it pushes again with the same push_id: the app shows
    a push with a given id once, and the tray replaces rather than stacks."""
    from app.core.notifications.service import send_notification_to_users

    async with jobs.scope() as s:
        await send_notification_to_users(s, [int(u) for u in p.get("user_ids") or ()], p["title"], p["body"], data=dict(p.get("data") or {}))
