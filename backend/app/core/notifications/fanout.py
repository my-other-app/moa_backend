"""Telling an audience: an inbox row for each person in one statement, then
a push to their devices in batches -- after the response, not inside it.

A new event used to write one notification row per follower with one INSERT
per round trip (the primary key's default was SQL, so the ORM could not
batch), then send, all inside the POST that created the event: a club with a
lakh of followers held the request for a lakh of round trips. Now the rows are
one `INSERT ... SELECT` over the audience, and the fan-out is a job
(app.core.jobs): durable, retried, and run by the worker when there is one.
The inbox rows and the push are two jobs, so a push that fails is tried again
without writing the rows twice.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import JSON, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.notifications.models import NotificationStatus, Notifications
from app.core import jobs
from app.core.background import dispatch, drain, runs_inline  # noqa: F401  (re-exported)


# ─── The inbox rows ─────────────────────────────────────────────────────────


async def insert_for_audience(
    session: AsyncSession,
    audience,
    *,
    title: str,
    description: str,
    type: str,
    data: Optional[dict] = None,
    from_club_id: Optional[int] = None,
    from_user_id: Optional[int] = None,
    event_id: Optional[int] = None,
    once: bool = False,
) -> list[int]:
    """One notification for each user id `audience` (a SELECT of one column)
    yields, in one statement. Returns the ids of the users it reached. The
    caller commits. `once` (with an event): not to anyone who already has a
    notification of this type about it -- the job run again adds none."""
    now = datetime.now(timezone.utc)
    people = audience.subquery("audience")
    user_id = list(people.c)[0]
    if once and event_id is not None:
        told = select(Notifications.id).where(
            Notifications.user_id == user_id, Notifications.event_id == event_id, Notifications.type == type,
            Notifications.is_deleted == False,  # noqa: E712
        ).exists()
        people = select(user_id).where(~told).subquery("audience_new")
        user_id = list(people.c)[0]
    source = select(
        func.gen_random_uuid(),
        user_id,
        literal(title, sa.String),
        literal(description, sa.String),
        literal(type, sa.String),
        literal(from_club_id, sa.Integer),
        literal(from_user_id, sa.Integer),
        literal(event_id, sa.Integer),
        literal(NotificationStatus.unread, Notifications.__table__.c.status.type),
        literal(data, JSON),
        sa.false(),
        literal(now, Notifications.__table__.c.created_at.type),
        literal(now, Notifications.__table__.c.updated_at.type),
    ).select_from(people)
    c = Notifications.__table__.c
    stmt = (
        insert(Notifications.__table__)
        .from_select(
            [c.id, c.user_id, c.title, c.description, c.type, c.from_club_id, c.from_user_id, c.event_id, c.status, c.data, c.is_deleted, c.created_at, c.updated_at],
            source,
        )
        .returning(c.user_id)
    )
    return list((await session.execute(stmt)).scalars().all())


def users_in(user_ids) -> sa.Select:
    """An audience from a list of ids: one array bind, however long the list."""
    from app.core.notifications.service import int_array

    return select(func.unnest(int_array(user_ids)).label("user_id"))


# ─── The push, a job of its own ─────────────────────────────────────────────


async def enqueue_push(session: AsyncSession, *, type: str, event_id: int, title: str, body: str, data: dict) -> None:
    """The push to everyone holding a `type` notification about `event_id`,
    after the caller's commit. One waiting per (type, event)."""
    await jobs.enqueue(
        session, "notifications.push",
        {"type": type, "event_id": int(event_id), "title": title, "body": body, "data": data},
        dedupe_key=f"push:{type}:{int(event_id)}",
    )


@jobs.handler("notifications.push", max_attempts=4, timeout=900.0)
async def _push_job(p: dict) -> None:
    """Read who holds the notification (by the event's index), then push in
    FCM batches (app.core.notifications.service). Run again, it pushes again."""
    from app.core.notifications.service import send_notification_to_users

    async with jobs.scope() as s:
        user_ids = list((await s.scalars(
            select(Notifications.user_id).where(
                Notifications.event_id == int(p["event_id"]), Notifications.type == p["type"], Notifications.is_deleted == False,  # noqa: E712
            ).distinct()
        )).all())
        await s.commit()
        if user_ids:
            await send_notification_to_users(session=s, user_ids=user_ids, title=p["title"], body=p["body"], data=p.get("data") or {})


# ─── A push to every device ─────────────────────────────────────────────────


async def enqueue_broadcast(session: AsyncSession, broadcast_id: str, title: str, body: str, image_url: Optional[str], *, after: int = 0) -> None:
    """The batch of devices after `after` (by key), as a job."""
    await jobs.enqueue(
        session, "notifications.broadcast",
        {"id": broadcast_id, "title": title, "body": body, "image_url": image_url, "after": int(after)},
        dedupe_key=f"broadcast:{broadcast_id}:{int(after)}",
    )


@jobs.handler("notifications.broadcast", max_attempts=4, timeout=300.0)
async def _broadcast_job(p: dict) -> None:
    """One FCM batch of a push to every device, then the next batch as a job
    of its own, written before this one's push: twenty thousand small jobs
    for a crore of devices, each retried alone, rather than one long run a
    deploy cuts off and a retry starts again from the first device."""
    from app.api.users.models import UserDeviceTokens
    from app.core.notifications import service as push

    async with jobs.scope() as s:
        rows = (await s.execute(
            select(UserDeviceTokens.id, UserDeviceTokens.fcm_token)
            .where(UserDeviceTokens.is_deleted == False, UserDeviceTokens.id > int(p.get("after") or 0))  # noqa: E712
            .order_by(UserDeviceTokens.id)
            .limit(push.FCM_BATCH)
        )).all()
        if len(rows) == push.FCM_BATCH:
            await enqueue_broadcast(s, p["id"], p["title"], p["body"], p.get("image_url"), after=rows[-1].id)
        await s.commit()
        if not rows:
            return
        dead: list[str] = []
        sent = await push._send_to_tokens([r.fcm_token for r in rows], p["title"], p["body"], None, p.get("image_url"), dead=dead)
        if dead:
            await push.prune_tokens(s, dead)
        push.report_outcome(len(rows), sent, what=p["title"])
