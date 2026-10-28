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

With PUSH_TOPICS or INBOX_SHARED on, a broadcast goes through `announce`
instead (below): one row, and one send to its FCM topic.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import JSON, func, insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.notifications.models import (
    NotificationBroadcasts,
    NotificationBroadcastTargets,
    NotificationStatus,
    Notifications,
)
from app.core import jobs
from app.core.notifications import delivery
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


@jobs.handler("notifications.push", max_attempts=4, timeout=900.0, concurrency=2)
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
            select(UserDeviceTokens.id, UserDeviceTokens.user_id, UserDeviceTokens.fcm_token)
            .where(UserDeviceTokens.is_deleted == False, UserDeviceTokens.id > int(p.get("after") or 0), push.reachable(UserDeviceTokens.user_id))  # noqa: E712
            .order_by(UserDeviceTokens.id)
            .limit(push.FCM_BATCH)
        )).all()
        if len(rows) == push.FCM_BATCH:
            await enqueue_broadcast(s, p["id"], p["title"], p["body"], p.get("image_url"), after=rows[-1].id)
        # Each device's badge, its person's unread count: one count for the
        # batch, read before the commit ends the transaction.
        devices = [(r.user_id, r.fcm_token) for r in rows]
        badges = push.badges_by_token(devices, await push.unread_badges(s, {u for u, _ in devices})) if devices else None
        await s.commit()
        if not rows:
            return
        dead: list[str] = []
        sent = await push._send_to_tokens([r.fcm_token for r in rows], p["title"], p["body"], None, p.get("image_url"), dead=dead, badges=badges)
        if dead:
            await push.prune_tokens(s, dead)
        push.report_outcome(len(rows), sent, what=p["title"])


# ─── Broadcasts: one row, a topic send (feat/w2-push) ───────────────────────
#
# With PUSH_TOPICS or INBOX_SHARED on, a broadcast (app.core.notifications.
# delivery) goes through `announce`: its row in notification_broadcasts,
# unique by push_id, so a job run again writes and sends nothing twice; the
# inbox -- one shared row read through follows and interests (INBOX_SHARED),
# or a row per recipient as before; and the push -- one send to its FCM
# topic (PUSH_TOPICS) plus, device by device, the devices that do not
# subscribe to topics yet (PUSH_TOPICS_LEGACY), or every device of the
# audience device by device (PUSH_TOPICS off). All of it is written in one
# transaction with the row, so the pushes go once the row is kept.
#
# The audience honours mutes and switches: a club's muted followers, and
# whoever turned that kind of push off, are left out -- as they are left out
# of the topic (app.core.notifications.topics).


@dataclass
class Broadcast:
    kind: str                      # a key of delivery.BROADCASTS
    ref: str                       # what it is about: push_id is "{kind}:{ref}"
    title: str
    body: str
    data: Optional[dict] = None    # the inbox line's data
    push_data: dict = field(default_factory=dict)
    image_url: Optional[str] = None
    from_club_id: Optional[int] = None
    from_user_id: Optional[int] = None
    event_id: Optional[int] = None
    # Whom: a club's followers, or the people by topic (but not except_club's
    # followers), or every device.
    club_id: Optional[int] = None
    topic_ids: list[int] = field(default_factory=list)
    except_club_id: Optional[int] = None
    everyone: bool = False

    @property
    def push_id(self) -> str:
        return delivery.push_id(self.kind, self.ref)

    def audience(self) -> dict:
        if self.everyone:
            return {"all": True}
        if self.club_id is not None:
            return {"club": int(self.club_id)}
        return {"topics": [int(t) for t in self.topic_ids], "except_club": self.except_club_id}


def audience_users(audience: dict) -> sa.Select:
    """The user ids a broadcast's audience holds (not `all`: that one is
    every device, walked by token). Muted follows and switches turned off
    are left out."""
    from app.api.clubs.models import ClubUsersLink
    from app.api.interests.models import Interests
    from app.api.users.models import UserInterests
    from app.core.notifications.topics import settings_off

    if "club" in audience:
        return select(ClubUsersLink.user_id).where(
            ClubUsersLink.club_id == int(audience["club"]),
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
            ClubUsersLink.is_muted == False,  # noqa: E712
            ~settings_off("clubs", ClubUsersLink.user_id),
        )
    topic_ids = [int(t) for t in audience.get("topics") or ()]
    in_topics = select(Interests.id).where(Interests.category_id == sa.any_(_ints(topic_ids)), Interests.is_deleted == False)  # noqa: E712
    query = select(UserInterests.user_id).where(
        UserInterests.interest_id.in_(in_topics),
        UserInterests.is_deleted == False,  # noqa: E712
        ~settings_off("topics", UserInterests.user_id),
    )
    if audience.get("except_club") is not None:
        query = query.where(~select(ClubUsersLink.id).where(
            ClubUsersLink.user_id == UserInterests.user_id,
            ClubUsersLink.club_id == int(audience["except_club"]),
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
        ).exists())
    return query.distinct()


def _ints(ids):
    from app.core.notifications.service import int_array

    return int_array(ids)


async def announce(session: AsyncSession, b: Broadcast, *, topic: Optional[str] = None, condition: Optional[str] = None) -> bool:
    """Write the broadcast, its inbox lines and its push jobs, and commit.
    `topic` or `condition` is where its FCM send goes. False when it was
    already announced (its push_id is taken): nothing is written again."""
    from app.config import settings

    shared = bool(settings.INBOX_SHARED) and not b.everyone
    audience = b.audience()
    now = datetime.now(timezone.utc)
    table = NotificationBroadcasts.__table__
    row_id = await session.scalar(
        pg_insert(table).values(
            id=uuid.uuid4(), push_id=b.push_id, type=b.kind, title=b.title, description=b.body,
            from_club_id=b.from_club_id, from_user_id=b.from_user_id, event_id=b.event_id, data=b.data,
            audience=audience, shared=shared, created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=[table.c.push_id]).returning(table.c.id)
    )
    if row_id is None:
        await session.commit()
        return False
    if shared:
        targets = [f"club:{audience['club']}"] if "club" in audience else [f"topic:{t}" for t in audience["topics"]]
        await session.execute(insert(NotificationBroadcastTargets.__table__), [
            dict(broadcast_id=row_id, target=t, created_at=now, except_club_id=audience.get("except_club")) for t in targets
        ])
    elif not b.everyone:
        await insert_for_audience(
            session, audience_users(audience), title=b.title, description=b.body, type=b.kind, data=b.data,
            from_club_id=b.from_club_id, from_user_id=b.from_user_id, event_id=b.event_id,
        )
    data = {**{k: str(v) for k, v in (b.push_data or {}).items()}, "push_id": b.push_id}
    push = dict(push_id=b.push_id, title=b.title, body=b.body, data=data, image_url=b.image_url)
    if settings.PUSH_TOPICS:
        await jobs.enqueue(session, "push.topic", {**push, "topic": topic, "condition": condition}, dedupe_key=f"topic:{b.push_id}")
        if settings.PUSH_TOPICS_LEGACY:
            await enqueue_devices(session, push, audience, legacy_only=True)
    else:
        await enqueue_devices(session, push, audience, legacy_only=False)
    await session.commit()
    return True


@jobs.handler("push.topic", max_attempts=6, timeout=60.0)
async def _topic_job(p: dict) -> None:
    """One send to an FCM topic or condition. A failure that may pass raises
    (service.TopicSendFailed) and the job is tried again later; the push_id
    is the same each time, so a device the first send did reach shows it once."""
    from app.core.notifications import service as push

    await push.send_to_topic(
        topic=p.get("topic"), condition=p.get("condition"), title=p["title"], body=p["body"],
        data=p.get("data") or {}, image_url=p.get("image_url"),
    )


#: Audience members one `push.devices` job reads: their devices go in
#: FCM_BATCH multicasts, and the next members are the next job.
USERS_PER_JOB = 1000


async def enqueue_devices(session: AsyncSession, push: dict, audience: dict, *, legacy_only: bool, after: int = 0) -> None:
    await jobs.enqueue(
        session, "push.devices", {**push, "audience": audience, "legacy_only": bool(legacy_only), "after": int(after)},
        dedupe_key=f"devices:{push['push_id']}:{int(after)}"[:200],
    )


@jobs.handler("push.devices", max_attempts=4, timeout=300.0)
async def _devices_job(p: dict) -> None:
    """A broadcast device by device, a slice of its audience a job: the next
    USERS_PER_JOB people by id (every device: FCM_BATCH tokens by id), the
    next slice's job written before this one sends. A job run again resends
    its slice, with the same push_id. With `legacy_only`, only devices that
    do not subscribe to topics: the topic send reached the rest."""
    from app.api.users.models import UserDeviceTokens
    from app.core.notifications import service as push
    from app.core.notifications.topics import settings_off

    audience, after, legacy_only = p["audience"], int(p.get("after") or 0), bool(p.get("legacy_only"))
    live = [UserDeviceTokens.is_deleted == False, push.reachable(UserDeviceTokens.user_id)]  # noqa: E712
    if legacy_only:
        live.append(UserDeviceTokens.supports_topics == False)  # noqa: E712
    async with jobs.scope() as s:
        if audience.get("all"):
            rows = (await s.execute(
                select(UserDeviceTokens.id, UserDeviceTokens.user_id, UserDeviceTokens.fcm_token)
                .where(*live, UserDeviceTokens.id > after, ~settings_off("platform", UserDeviceTokens.user_id))
                .order_by(UserDeviceTokens.id)
                .limit(push.FCM_BATCH)
            )).all()
            devices = [(r.user_id, r.fcm_token) for r in rows]
            last = rows[-1].id if len(rows) == push.FCM_BATCH else None
        else:
            people = audience_users(audience).subquery()
            user_id = list(people.c)[0]
            ids = list((await s.scalars(
                select(user_id).where(user_id > after).order_by(user_id).limit(USERS_PER_JOB)
            )).all())
            last = ids[-1] if len(ids) == USERS_PER_JOB else None
            devices = [tuple(r) for r in (await s.execute(
                select(UserDeviceTokens.user_id, UserDeviceTokens.fcm_token)
                .where(*live, UserDeviceTokens.user_id == sa.any_(_ints(ids)))
                .order_by(UserDeviceTokens.id)
            )).all()] if ids else []
        if last is not None:
            await enqueue_devices(s, {k: p[k] for k in ("push_id", "title", "body", "data", "image_url")}, audience, legacy_only=legacy_only, after=last)
        # Each device's badge, its person's unread count (none with
        # INBOX_SHARED on: push.unread_badges), read before the commit.
        badges = push.badges_by_token(devices, await push.unread_badges(s, {u for u, _ in devices})) if devices else None
        await s.commit()
        tokens = [token for _, token in devices]
        if not tokens:
            return
        dead: list[str] = []
        sent = await push._send_to_tokens(tokens, p["title"], p["body"], p.get("data") or {}, p.get("image_url"), dead=dead, badges=badges)
        if dead:
            await push.prune_tokens(s, dead)
        push.report_outcome(len(tokens), sent, what=p["title"])
