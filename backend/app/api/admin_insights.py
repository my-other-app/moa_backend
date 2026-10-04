"""Time series and the moderation queue for the admin dashboard.

The dashboard reported counters and a seven-day sign-up chart. Every other
figure in it answers "how many" and none answer "is this getting better",
which is the question anyone running the platform actually has.

These return one row per day over a window, plus the same totals for the
preceding window so the UI can show a real period-over-period change instead
of the hard-coded strings the existing /admin/analytics endpoint formats.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import Clubs
from app.api.events.models import EventRegistrationsLink, Events
from app.api.users.models import Users

# A year of daily points is already more than a chart can show usefully, and
# the queries are unindexed scans over created_at.
MAX_WINDOW_DAYS = 365


async def _daily_counts(
    session: AsyncSession, column, since: datetime, *extra_filters, value=None
) -> dict[date, float]:
    """
    One row per day. `value` sums a column instead of counting rows, which is
    how revenue differs from the rest.
    """
    aggregate = func.sum(value) if value is not None else func.count()
    rows = await session.execute(
        select(cast(column, Date).label("day"), aggregate)
        .where(column >= since, *extra_filters)
        .group_by(cast(column, Date))
    )
    return {row[0]: float(row[1] or 0) for row in rows.all()}


async def get_trends(session: AsyncSession, days: int = 30) -> dict:
    """
    Daily series for the last `days`, plus totals for the window before it.

    Every day in the range is present even when nothing happened: a chart
    built from only the days with data draws a straight line between two
    distant points and implies activity that did not occur.
    """
    days = max(1, min(days, MAX_WINDOW_DAYS))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days - 1)
    start_of_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    previous_start = start_of_day - timedelta(days=days)

    paid = EventRegistrationsLink.is_paid == True
    live = EventRegistrationsLink.is_deleted == False

    users = await _daily_counts(session, Users.created_at, start_of_day)
    events = await _daily_counts(session, Events.created_at, start_of_day)
    registrations = await _daily_counts(
        session, EventRegistrationsLink.created_at, start_of_day, live
    )
    revenue = await _daily_counts(
        session,
        EventRegistrationsLink.created_at,
        start_of_day,
        live,
        paid,
        value=EventRegistrationsLink.paid_amount,
    )

    series = []
    for offset in range(days):
        day = (start_of_day + timedelta(days=offset)).date()
        series.append(
            {
                "date": day.isoformat(),
                "users": int(users.get(day, 0)),
                "events": int(events.get(day, 0)),
                "registrations": int(registrations.get(day, 0)),
                "revenue": revenue.get(day, 0.0),
            }
        )

    async def window_total(column, *filters, value=None):
        aggregate = func.sum(value) if value is not None else func.count()
        return float(
            await session.scalar(
                select(func.coalesce(aggregate, 0)).where(
                    column >= previous_start, column < start_of_day, *filters
                )
            )
            or 0
        )

    previous = {
        "users": int(await window_total(Users.created_at)),
        "events": int(await window_total(Events.created_at)),
        "registrations": int(
            await window_total(EventRegistrationsLink.created_at, live)
        ),
        "revenue": await window_total(
            EventRegistrationsLink.created_at,
            live,
            paid,
            value=EventRegistrationsLink.paid_amount,
        ),
    }

    totals = {
        key: sum(point[key] for point in series)
        for key in ("users", "events", "registrations", "revenue")
    }

    return {
        "days": days,
        "series": series,
        "totals": totals,
        "previous_totals": previous,
    }


async def get_moderation_queue(session: AsyncSession, limit: int = 20) -> dict:
    """
    The things waiting on a decision, in one place.

    An admin otherwise has to visit five pages to discover there is nothing to
    do -- or worse, not discover that there is.
    """
    now = datetime.now(timezone.utc)

    pending_clubs = (
        await session.scalars(
            select(Clubs)
            .where(Clubs.is_verified == False, Clubs.is_deleted == False)
            .order_by(Clubs.created_at.asc())
            .limit(limit)
        )
    ).all()

    # Events that have finished with registrations but nobody checked in.
    # Either attendance was never taken or the event did not happen; both are
    # worth a look, and both are invisible from any existing page.
    registration_count = (
        select(func.count(EventRegistrationsLink.id))
        .where(
            EventRegistrationsLink.event_id == Events.id,
            EventRegistrationsLink.is_deleted == False,
        )
        .scalar_subquery()
    )
    attended_count = (
        select(func.count(EventRegistrationsLink.id))
        .where(
            EventRegistrationsLink.event_id == Events.id,
            EventRegistrationsLink.is_deleted == False,
            EventRegistrationsLink.is_attended == True,
        )
        .scalar_subquery()
    )

    unreconciled = (
        await session.execute(
            select(Events, registration_count)
            .where(
                Events.is_deleted == False,
                Events.event_datetime < now,
                registration_count > 0,
                attended_count == 0,
            )
            .order_by(Events.event_datetime.desc())
            .limit(limit)
        )
    ).all()

    return {
        "pending_clubs": [
            {
                "id": club.id,
                "name": club.name,
                "created_at": club.created_at,
            }
            for club in pending_clubs
        ],
        "events_without_attendance": [
            {
                "id": event.id,
                "name": event.name,
                "event_datetime": event.event_datetime,
                "registration_count": int(count or 0),
            }
            for event, count in unreconciled
        ],
    }
