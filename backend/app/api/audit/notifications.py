"""Every announcement the platform has pushed, across all clubs.

/clubs/notifications/history answers this for the calling club only, so an
admin could not see what had been sent to people's phones -- the one action in
the product that reaches a user's device and cannot be recalled.

Notifications are stored one row per recipient, so they are grouped back into
the send they came from, the same way the club-facing endpoint does: by title,
event and hour.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import Clubs
from app.api.events.models import Events
from app.api.notifications.models import NotificationStatus, Notifications


async def list_sent_announcements(
    session: AsyncSession,
    club_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    hour = func.date_trunc("hour", Notifications.created_at).label("hour")

    filters = [
        Notifications.type == "club_announcement",
        Notifications.is_deleted == False,
    ]
    if club_id is not None:
        filters.append(Notifications.from_club_id == club_id)

    # One row per send, counted in SQL rather than by loading every recipient
    # row into memory -- a single announcement to a large club is thousands.
    grouped = (
        select(
            Notifications.title,
            Notifications.description,
            Notifications.event_id,
            Notifications.from_club_id,
            hour,
            func.count().label("sent_count"),
            func.count()
            .filter(Notifications.status == NotificationStatus.read)
            .label("read_count"),
            func.min(Notifications.created_at).label("created_at"),
        )
        .where(*filters)
        .group_by(
            Notifications.title,
            Notifications.description,
            Notifications.event_id,
            Notifications.from_club_id,
            hour,
        )
        .order_by(func.min(Notifications.created_at).desc())
    )

    total = await session.scalar(
        select(func.count()).select_from(grouped.subquery())
    )

    rows = (await session.execute(grouped.limit(limit).offset(offset))).all()

    event_ids = {r.event_id for r in rows if r.event_id}
    club_ids = {r.from_club_id for r in rows if r.from_club_id}

    events = (
        {
            e.id: e.name
            for e in await session.scalars(
                select(Events).where(Events.id.in_(event_ids))
            )
        }
        if event_ids
        else {}
    )
    clubs = (
        {
            c.id: c.name
            for c in await session.scalars(
                select(Clubs).where(Clubs.id.in_(club_ids))
            )
        }
        if club_ids
        else {}
    )

    return [
        {
            "title": row.title,
            "description": row.description,
            "event_id": row.event_id,
            "event_name": events.get(row.event_id),
            "club_name": clubs.get(row.from_club_id),
            "sent_count": row.sent_count,
            "read_count": row.read_count,
            "created_at": row.created_at,
        }
        for row in rows
    ], (total or 0)
