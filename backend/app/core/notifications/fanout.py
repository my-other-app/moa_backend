"""Telling an audience: an inbox row for each person in one statement, then
a push to their devices in batches -- after the response, not inside it.

A new event used to write one notification row per follower with one INSERT
per round trip (the primary key's default was SQL, so the ORM could not
batch), then send, all inside the POST that created the event: a club with a
lakh of followers held the request for a lakh of round trips. Now the rows are
one `INSERT ... SELECT` over the audience, and the whole fan-out runs as a
background job on its own session once the request has answered
(app.core.background: not durable -- a fan-out in flight when the process
stops is lost; there is no job queue in this stack yet).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import JSON, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.notifications.models import NotificationStatus, Notifications
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
) -> list[int]:
    """One notification for each user id `audience` (a SELECT of one column)
    yields, in one statement. Returns the ids of the users it reached. The
    caller commits."""
    now = datetime.now(timezone.utc)
    people = audience.subquery("audience")
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
