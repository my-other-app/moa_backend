"""The admin Overview screen, as one query set.

The screen shows roughly fifteen different aggregates: platform totals, what is
happening today, nine averages, four monthly growth series, three breakdowns, a
funnel, a top-clubs table, the moderation queue and the money split. Fetching
those from the client would be fifteen round trips for one page load, on a
single-instance backend that is already CPU-throttled.

So it is one endpoint. Everything here is a GROUP BY or an aggregate executed in
the database -- nothing walks rows in Python to count them, because the row
counts grow with the platform and the aggregate counts do not.

Two deliberate omissions from the design:

- "Where users are" needs a city on Users, which does not exist. Inventing one
  would mean a migration and a backfill nobody can source, so the key is absent
  rather than present-and-empty.
- Subscription revenue (MRR) has no domain yet. The key is absent for the same
  reason: a zero would read as "no subscriptions" rather than "not built".
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Float, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import (
    EventCategories,
    EventRatingsLink,
    EventRegistrationsLink,
    Events,
)
from app.api.orgs.models import Organizations
from app.api.payments.models import PaymentLogs, PaymentStatus
from app.api.users.models import Users

# A year of monthly points is already more than the growth charts show, and the
# window is user-supplied.
MAX_WINDOW_DAYS = 365
MAX_MONTHS = 12
TOP_CLUBS = 8
BREAKDOWN_ROWS = 6
RUNNING_DAYS = 7

# Reused predicates. Every list endpoint filters soft deletes, so every count
# here must too -- a figure that does not will disagree with the table it links
# to, which is the bug this whole module exists downstream of.
LIVE_EVENT = Events.is_deleted == False  # noqa: E712
LIVE_CLUB = Clubs.is_deleted == False  # noqa: E712
LIVE_REG = EventRegistrationsLink.is_deleted == False  # noqa: E712
CAPTURED = and_(
    PaymentLogs.status == PaymentStatus.captured,
    PaymentLogs.is_deleted == False,  # noqa: E712
)


def _f(value) -> float:
    """Decimal and None both reach the JSON layer; neither serialises well."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _rate(part, whole) -> float:
    """A percentage, or 0 when there is nothing to divide by."""
    whole = _f(whole)
    return round(_f(part) / whole * 100, 1) if whole else 0.0


async def _scalar(session: AsyncSession, stmt) -> float:
    return _f(await session.scalar(stmt))


async def _totals(session: AsyncSession) -> dict:
    users = await _scalar(session, select(func.count(Users.id)))
    clubs = await _scalar(session, select(func.count(Clubs.id)).where(LIVE_CLUB))
    verified = await _scalar(
        session,
        select(func.count(Clubs.id)).where(LIVE_CLUB, Clubs.is_verified == True),  # noqa: E712
    )
    orgs = await _scalar(
        session,
        select(func.count(Organizations.id)).where(
            Organizations.is_deleted == False  # noqa: E712
        ),
    )
    events = await _scalar(session, select(func.count(Events.id)).where(LIVE_EVENT))
    upcoming = await _scalar(
        session,
        select(func.count(Events.id)).where(
            LIVE_EVENT, Events.event_datetime >= datetime.now(timezone.utc)
        ),
    )
    registrations = await _scalar(
        session, select(func.count(EventRegistrationsLink.id)).where(LIVE_REG)
    )
    attended = await _scalar(
        session,
        select(func.count(EventRegistrationsLink.id)).where(
            LIVE_REG, EventRegistrationsLink.is_attended == True  # noqa: E712
        ),
    )
    gmv = await _scalar(
        session, select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(CAPTURED)
    )

    return {
        "users": int(users),
        "clubs": int(clubs),
        "verified_clubs": int(verified),
        "pending_clubs": int(max(0, clubs - verified)),
        "organisations": int(orgs),
        "events": int(events),
        "upcoming_events": int(upcoming),
        "past_events": int(max(0, events - upcoming)),
        "registrations": int(registrations),
        "attended": int(attended),
        "attendance_rate": _rate(attended, registrations),
        "gmv": gmv,
    }


async def _today(session: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    live_now = await _scalar(
        session,
        select(func.count(Events.id)).where(
            LIVE_EVENT, Events.event_datetime >= start, Events.event_datetime < end
        ),
    )
    registrations = await _scalar(
        session,
        select(func.count(EventRegistrationsLink.id)).where(
            LIVE_REG, EventRegistrationsLink.created_at >= start
        ),
    )
    check_ins = await _scalar(
        session,
        select(func.count(EventRegistrationsLink.id)).where(
            LIVE_REG, EventRegistrationsLink.attended_on >= start
        ),
    )
    collected = await _scalar(
        session,
        select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(
            CAPTURED, PaymentLogs.created_at >= start
        ),
    )
    new_users = await _scalar(
        session, select(func.count(Users.id)).where(Users.created_at >= start)
    )

    return {
        "events_today": int(live_now),
        "registrations": int(registrations),
        "check_ins": int(check_ins),
        "collected": collected,
        "new_users": int(new_users),
    }


async def _running(session: AsyncSession) -> list[dict]:
    """Events today and over the next week, with their registration counts.

    One query with a correlated count rather than a list of events followed by a
    count per event -- that second shape is the N+1 this endpoint exists to
    avoid.
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=RUNNING_DAYS)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    regs = (
        select(func.count(EventRegistrationsLink.id))
        .where(
            EventRegistrationsLink.event_id == Events.id,
            EventRegistrationsLink.is_deleted == False,  # noqa: E712
        )
        .correlate(Events)
        .scalar_subquery()
    )

    rows = await session.execute(
        select(Events.id, Events.name, Events.event_datetime, Events.has_fee,
               Events.reg_fee, Clubs.name.label("club"), regs.label("registrations"))
        .join(Clubs, Clubs.id == Events.club_id, isouter=True)
        .where(LIVE_EVENT, Events.event_datetime >= start_of_day,
               Events.event_datetime <= horizon)
        .order_by(Events.event_datetime)
        .limit(12)
    )

    out = []
    for row in rows.all():
        out.append({
            "id": row.id,
            "name": row.name,
            "club": row.club,
            "event_datetime": row.event_datetime.isoformat() if row.event_datetime else None,
            "registrations": int(row.registrations or 0),
            "has_fee": bool(row.has_fee),
            "reg_fee": _f(row.reg_fee),
            # "Live" means today, which is what the design's accent colour marks.
            "is_today": bool(row.event_datetime and row.event_datetime < start_of_day + timedelta(days=1)),
        })
    return out


async def _averages(session: AsyncSession, totals: dict) -> dict:
    views = await _scalar(
        session, select(func.coalesce(func.sum(Events.page_views), 0)).where(LIVE_EVENT)
    )
    paid_regs = await _scalar(
        session,
        select(func.count(EventRegistrationsLink.id)).where(
            LIVE_REG, EventRegistrationsLink.is_paid == True  # noqa: E712
        ),
    )
    paid_amount = await _scalar(
        session,
        select(func.coalesce(func.sum(EventRegistrationsLink.paid_amount), 0)).where(
            LIVE_REG, EventRegistrationsLink.is_paid == True  # noqa: E712
        ),
    )
    paid_events = await _scalar(
        session,
        select(func.count(Events.id)).where(LIVE_EVENT, Events.has_fee == True),  # noqa: E712
    )
    followers = await _scalar(
        session,
        select(func.count(ClubUsersLink.id)).where(
            ClubUsersLink.is_following == True,  # noqa: E712
            ClubUsersLink.is_deleted == False,  # noqa: E712
        ),
    )
    rating = await _scalar(
        session,
        select(func.coalesce(func.avg(cast(EventRatingsLink.rating, Float)), 0)).where(
            EventRatingsLink.is_deleted == False  # noqa: E712
        ),
    )

    events = totals["events"]
    clubs = totals["clubs"]
    return {
        "registrations_per_event": round(totals["registrations"] / events, 1) if events else 0.0,
        "attendance_rate": totals["attendance_rate"],
        "view_to_signup": _rate(totals["registrations"], views),
        "average_ticket_price": round(paid_amount / paid_regs, 2) if paid_regs else 0.0,
        "revenue_per_paid_event": round(paid_amount / paid_events, 2) if paid_events else 0.0,
        "followers_per_club": round(followers / clubs, 1) if clubs else 0.0,
        "events_per_club": round(events / clubs, 1) if clubs else 0.0,
        "average_rating": round(rating, 1),
        "page_views": int(views),
    }


async def _monthly(session: AsyncSession, column, *filters, value=None) -> list[dict]:
    """One row per calendar month, oldest first, capped to MAX_MONTHS."""
    bucket = func.date_trunc("month", column).label("month")
    aggregate = func.coalesce(func.sum(value), 0) if value is not None else func.count()
    rows = await session.execute(
        select(bucket, aggregate.label("value"))
        .where(*filters)
        .group_by(bucket)
        .order_by(bucket.desc())
        .limit(MAX_MONTHS)
    )
    out = [
        {"month": r.month.date().isoformat() if r.month else None, "value": _f(r.value)}
        for r in rows.all()
    ]
    out.reverse()
    return out


async def _growth(session: AsyncSession) -> dict:
    return {
        "users": await _monthly(session, Users.created_at),
        "clubs": await _monthly(
            session, Clubs.created_at, LIVE_CLUB, Clubs.is_verified == True  # noqa: E712
        ),
        "events": await _monthly(session, Events.created_at, LIVE_EVENT),
        "gmv": await _monthly(
            session, PaymentLogs.created_at, CAPTURED, value=PaymentLogs.amount
        ),
    }


async def _breakdowns(session: AsyncSession) -> dict:
    async def grouped(label_col, id_col, *filters, join=None) -> list[dict]:
        stmt = select(label_col.label("label"), func.count(id_col).label("value"))
        if join is not None:
            stmt = stmt.join(*join)
        rows = await session.execute(
            stmt.where(*filters)
            .group_by(label_col)
            .order_by(func.count(id_col).desc())
            .limit(BREAKDOWN_ROWS)
        )
        return [
            {"label": str(r.label) if r.label is not None else "Unknown",
             "value": int(r.value)}
            for r in rows.all()
        ]

    events_by_type = await grouped(
        EventCategories.name, Events.id, LIVE_EVENT,
        join=(EventCategories, EventCategories.id == Events.category_id),
    )
    clubs_by_org = await grouped(
        Organizations.name, Clubs.id, LIVE_CLUB,
        join=(Organizations, Organizations.id == Clubs.org_id),
    )
    rows = await session.execute(
        select(Users.provider, func.count(Users.id).label("value"))
        .group_by(Users.provider)
        .order_by(func.count(Users.id).desc())
    )
    by_provider = [
        {
            # The column is an Enum; `.value` keeps "google" out of the response
            # as "SigninProviders.google".
            "label": (getattr(r.provider, "value", r.provider) or "email"),
            "value": int(r.value),
        }
        for r in rows.all()
    ]

    return {
        "events_by_type": events_by_type,
        "clubs_by_organisation": clubs_by_org,
        "users_by_provider": by_provider,
    }


async def _funnel(session: AsyncSession, totals: dict, averages: dict) -> list[dict]:
    rated = await _scalar(
        session,
        select(func.count(EventRatingsLink.id)).where(
            EventRatingsLink.is_deleted == False  # noqa: E712
        ),
    )
    views = averages["page_views"]
    registered = totals["registrations"]
    attended = totals["attended"]
    # The design's funnel "only ever narrows". It is built from independent
    # counts, so a stage can legitimately exceed the one before it (ratings are
    # not gated on the attendance flag). Clamping keeps the shape honest
    # without inventing numbers -- the raw value is still reported.
    stages = [
        ("Event page views", views, None),
        ("Registered", min(registered, views) if views else registered, views),
        ("Checked in", attended, registered),
        ("Left a rating", min(int(rated), attended) if attended else int(rated), attended),
    ]
    return [
        {"label": label, "value": int(value),
         "of_previous": _rate(value, previous) if previous else None}
        for label, value, previous in stages
    ]


async def _top_clubs(session: AsyncSession, order_by: str) -> list[dict]:
    """
    Followers, registrations and revenue per club in one pass.

    Each metric is a correlated scalar subquery rather than three joins, because
    joining all three at once multiplies the rows and every aggregate comes back
    inflated -- the classic fan-out trap.
    """
    followers = (
        select(func.count(ClubUsersLink.id))
        .where(ClubUsersLink.club_id == Clubs.id,
               ClubUsersLink.is_following == True,  # noqa: E712
               ClubUsersLink.is_deleted == False)  # noqa: E712
        .correlate(Clubs).scalar_subquery()
    )
    regs = (
        select(func.count(EventRegistrationsLink.id))
        .join(Events, Events.id == EventRegistrationsLink.event_id)
        .where(Events.club_id == Clubs.id,
               Events.is_deleted == False,  # noqa: E712
               EventRegistrationsLink.is_deleted == False)  # noqa: E712
        .correlate(Clubs).scalar_subquery()
    )
    revenue = (
        select(func.coalesce(func.sum(EventRegistrationsLink.paid_amount), 0))
        .join(Events, Events.id == EventRegistrationsLink.event_id)
        .where(Events.club_id == Clubs.id,
               Events.is_deleted == False,  # noqa: E712
               EventRegistrationsLink.is_deleted == False,  # noqa: E712
               EventRegistrationsLink.is_paid == True)  # noqa: E712
        .correlate(Clubs).scalar_subquery()
    )

    columns = {"followers": followers, "registrations": regs, "revenue": revenue}
    chosen = columns.get(order_by, followers)

    rows = await session.execute(
        select(Clubs.id, Clubs.name, Organizations.name.label("org"),
               followers.label("followers"), regs.label("registrations"),
               revenue.label("revenue"))
        .join(Organizations, Organizations.id == Clubs.org_id, isouter=True)
        .where(LIVE_CLUB)
        .order_by(chosen.desc())
        .limit(TOP_CLUBS)
    )
    return [
        {"id": r.id, "name": r.name, "organisation": r.org,
         "followers": int(r.followers or 0),
         "registrations": int(r.registrations or 0),
         "revenue": _f(r.revenue)}
        for r in rows.all()
    ]


async def _money(session: AsyncSession, days: int) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    previous = since - timedelta(days=days)

    async def captured_between(start=None, end=None) -> float:
        filters = [CAPTURED]
        if start is not None:
            filters.append(PaymentLogs.created_at >= start)
        if end is not None:
            filters.append(PaymentLogs.created_at < end)
        return await _scalar(
            session, select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(*filters)
        )

    refunded = await _scalar(
        session,
        select(func.coalesce(func.sum(PaymentLogs.amount), 0)).where(
            PaymentLogs.status == PaymentStatus.refunded,
            PaymentLogs.is_deleted == False,  # noqa: E712
            PaymentLogs.created_at >= since,
        ),
    )
    failed = await _scalar(
        session,
        select(func.count(PaymentLogs.id)).where(
            PaymentLogs.status == PaymentStatus.failed,
            PaymentLogs.is_deleted == False,  # noqa: E712
            PaymentLogs.created_at >= since,
        ),
    )
    captured = await captured_between(start=since)
    previous_captured = await captured_between(start=previous, end=since)

    return {
        "captured": captured,
        "refunded": refunded,
        "net": captured - refunded,
        "failed": int(failed),
        "retained_share": _rate(captured - refunded, captured),
        "previous_captured": previous_captured,
    }


async def get_overview(
    session: AsyncSession, days: int = 30, top_by: str = "followers"
) -> dict:
    days = max(1, min(days, MAX_WINDOW_DAYS))

    totals = await _totals(session)
    averages = await _averages(session, totals)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "totals": totals,
        "today": await _today(session),
        "running": await _running(session),
        "averages": averages,
        "growth": await _growth(session),
        "breakdowns": await _breakdowns(session),
        "funnel": await _funnel(session, totals, averages),
        "top_clubs": await _top_clubs(session, top_by),
        "money": await _money(session, days),
    }
