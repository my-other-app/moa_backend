"""Everything the club Dashboard shows, in one response.

One endpoint rather than a dozen, for the same reason the admin Overview is
one: every figure on the screen then describes the same moment. A dashboard
assembled from eight calls can show a registration count from one second and
a revenue total from another, and the pair will not add up.

Money is real captured money -- `paid_amount` on registrations that were
actually paid -- never `reg_fee x registrations`. The latter is what a club
*would* have made if everyone paid, which is a different and much larger
number, and putting it under a heading that says "revenue" is the kind of
thing people make decisions on.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import Numeric, case, cast, func, select

from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import EventRegistrationsLink, Events

#: How many past events the reach table lists, and how many upcoming the
#: "Coming up" list shows. Both are a glance, not a browse.
REACH_LIMIT = 6
UPCOMING_LIMIT = 5
ATTENTION_LIMIT = 5


def _window(days: int) -> tuple[datetime, datetime, datetime]:
    """Now, the start of this period, and the start of the one before it."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    return now, start, previous_start


def _delta(current: float, previous: float) -> float | None:
    """
    Percentage change, or None when there is nothing to compare against.

    None rather than 0 or 100: a club's first month has no previous period,
    and "+100%" against nothing is a number someone will repeat out loud.
    """
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


async def get_club_overview(session, club: Clubs, days: int = 30) -> dict:
    now, start, previous_start = _window(days)

    event_ids_q = select(Events.id).where(
        Events.club_id == club.id, Events.is_deleted == False
    )

    # ---- Registrations and revenue, this period and the one before -------
    #
    # Both windows in one grouped query rather than two round trips: the
    # filter differs only by date, and a second query is a second chance for
    # the two to describe different moments.
    reg_rows = (
        await session.execute(
            select(
                case(
                    (EventRegistrationsLink.created_at >= start, "current"),
                    else_="previous",
                ).label("bucket"),
                func.count(EventRegistrationsLink.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                EventRegistrationsLink.is_paid == True,
                                cast(EventRegistrationsLink.paid_amount, Numeric(12, 2)),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case((EventRegistrationsLink.is_attended == True, 1), else_=0)
                    ),
                    0,
                ),
            )
            .where(
                EventRegistrationsLink.event_id.in_(event_ids_q),
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.created_at >= previous_start,
            )
            .group_by("bucket")
        )
    ).all()

    buckets = {
        row[0]: {"registrations": row[1], "revenue": float(row[2] or 0), "attended": row[3]}
        for row in reg_rows
    }
    current = buckets.get("current", {"registrations": 0, "revenue": 0.0, "attended": 0})
    previous = buckets.get("previous", {"registrations": 0, "revenue": 0.0, "attended": 0})

    # ---- Page views, by the event's own date ----------------------------
    #
    # page_views is a running total on the event, not a time series, so this
    # attributes an event's whole view count to the period it happened in.
    # Stated here because it is an approximation: an event viewed heavily
    # before the window still counts in the window it falls in.
    view_rows = (
        await session.execute(
            select(
                case(
                    (Events.event_datetime >= start, "current"), else_="previous"
                ).label("bucket"),
                func.coalesce(func.sum(Events.page_views), 0),
            )
            .where(
                Events.club_id == club.id,
                Events.is_deleted == False,
                Events.event_datetime >= previous_start,
            )
            # Grouped by the label, not a second copy of the CASE: Postgres
            # matches GROUP BY expressions textually, and two renderings of
            # the same expression are not the same expression to it.
            .group_by("bucket")
        )
    ).all()
    views = {row[0]: int(row[1] or 0) for row in view_rows}

    # ---- Daily revenue series, both periods -----------------------------
    series_rows = (
        await session.execute(
            select(
                func.date_trunc("day", EventRegistrationsLink.created_at).label("day"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                EventRegistrationsLink.is_paid == True,
                                cast(EventRegistrationsLink.paid_amount, Numeric(12, 2)),
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.count(EventRegistrationsLink.id),
            )
            .where(
                EventRegistrationsLink.event_id.in_(event_ids_q),
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.created_at >= previous_start,
            )
            .group_by("day")
            .order_by("day")
        )
    ).all()

    by_day = {
        row[0].date(): {"revenue": float(row[1] or 0), "registrations": row[2]}
        for row in series_rows
    }

    def series_for(window_start: datetime) -> list[dict]:
        # Every day present, including the empty ones -- a chart that skips
        # quiet days compresses time and makes a flat week look busy.
        out = []
        for offset in range(days):
            day = (window_start + timedelta(days=offset)).date()
            point = by_day.get(day, {"revenue": 0.0, "registrations": 0})
            out.append(
                {
                    "date": day.isoformat(),
                    "revenue": point["revenue"],
                    "registrations": point["registrations"],
                }
            )
        return out

    # ---- Registrations by weekday ---------------------------------------
    weekday_rows = (
        await session.execute(
            select(
                func.extract("dow", EventRegistrationsLink.created_at),
                func.count(EventRegistrationsLink.id),
            )
            .where(
                EventRegistrationsLink.event_id.in_(event_ids_q),
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.created_at >= start,
            )
            .group_by(func.extract("dow", EventRegistrationsLink.created_at))
        )
    ).all()
    # Postgres dow: 0 = Sunday. Presented Monday-first, which is how a week
    # reads to anyone planning one.
    counts = {int(row[0]): row[1] for row in weekday_rows}
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    busiest = [
        {"label": labels[i], "value": counts.get((i + 1) % 7, 0)} for i in range(7)
    ]

    # ---- Repeat attendees ------------------------------------------------
    #
    # Of the people who have attended anything, how many attended more than
    # one. Counted over the club's whole history, not the window: a repeat
    # rate measured over 30 days mostly measures how many events ran.
    attendee_rows = (
        await session.execute(
            select(
                EventRegistrationsLink.user_id,
                func.count(func.distinct(EventRegistrationsLink.event_id)),
            )
            .where(
                EventRegistrationsLink.event_id.in_(event_ids_q),
                EventRegistrationsLink.is_deleted == False,
                EventRegistrationsLink.is_attended == True,
            )
            .group_by(EventRegistrationsLink.user_id)
        )
    ).all()
    total_attendees = len(attendee_rows)
    repeat_attendees = sum(1 for _, n in attendee_rows if n > 1)
    repeat_rate = (
        round((repeat_attendees / total_attendees) * 100, 1) if total_attendees else None
    )

    # ---- Events: next, upcoming, needing attention, reach ---------------
    events = (
        (
            await session.execute(
                select(Events)
                .where(Events.club_id == club.id, Events.is_deleted == False)
                .order_by(Events.event_datetime.desc())
            )
        )
        .scalars()
        .all()
    )

    # Registrations and attendance per event, in one grouped query over the
    # club's own events rather than a correlated subquery per row.
    per_event_counts: dict[int, tuple[int, int]] = {}
    if events:
        for event_id, registered, attended in (
            await session.execute(
                select(
                    EventRegistrationsLink.event_id,
                    func.count(EventRegistrationsLink.id),
                    func.coalesce(
                        func.sum(
                            case(
                                (EventRegistrationsLink.is_attended == True, 1), else_=0
                            )
                        ),
                        0,
                    ),
                )
                .where(
                    EventRegistrationsLink.event_id.in_([e.id for e in events]),
                    EventRegistrationsLink.is_deleted == False,
                )
                .group_by(EventRegistrationsLink.event_id)
            )
        ).all():
            per_event_counts[int(event_id)] = (int(registered), int(attended))

    def shape(event) -> dict:
        registered, attended = per_event_counts.get(event.id, (0, 0))
        views_count = int(event.page_views or 0)
        return {
            "id": event.id,
            "name": event.name,
            "event_datetime": event.event_datetime,
            "location_name": event.location_name,
            "has_fee": bool(event.has_fee),
            "reg_fee": float(event.reg_fee) if event.reg_fee is not None else None,
            "page_views": views_count,
            "registrations": registered,
            "attended": attended,
            "view_to_signup": (
                round((registered / views_count) * 100, 1) if views_count else None
            ),
            "turnout": (
                round((attended / registered) * 100, 1) if registered else None
            ),
        }

    upcoming_events = sorted(
        (e for e in events if e.event_datetime and e.event_datetime >= now),
        key=lambda e: e.event_datetime,
    )
    past_events = [e for e in events if e.event_datetime and e.event_datetime < now]

    # Finished, people registered, nobody marked present. Certificates and
    # winners are both blocked until the club marks attendance, so this is
    # the list the dashboard exists to surface.
    attention = [
        shape(e)
        for e in past_events
        if per_event_counts.get(e.id, (0, 0))[0] > 0
        and per_event_counts.get(e.id, (0, 0))[1] == 0
    ]

    # ---- Followers over the window --------------------------------------
    follower_rows = (
        await session.execute(
            select(
                func.date_trunc("week", ClubUsersLink.created_at).label("week"),
                func.count(ClubUsersLink.id),
            )
            .where(
                ClubUsersLink.club_id == club.id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
                ClubUsersLink.created_at >= now - timedelta(weeks=12),
            )
            .group_by("week")
            .order_by("week")
        )
    ).all()
    followers_series = [
        {"week": row[0].date().isoformat(), "value": int(row[1])} for row in follower_rows
    ]

    followers_total = (
        await session.scalar(
            select(func.count(ClubUsersLink.id)).where(
                ClubUsersLink.club_id == club.id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )
    ) or 0

    current_views = views.get("current", 0)
    attendance_rate = (
        round((current["attended"] / current["registrations"]) * 100, 1)
        if current["registrations"]
        else None
    )
    previous_attendance_rate = (
        round((previous["attended"] / previous["registrations"]) * 100, 1)
        if previous["registrations"]
        else None
    )

    return {
        "generated_at": now,
        "period_days": days,
        "kpis": {
            "registrations": {
                "value": current["registrations"],
                "previous": previous["registrations"],
                "delta": _delta(current["registrations"], previous["registrations"]),
            },
            "page_views": {
                "value": current_views,
                "previous": views.get("previous", 0),
                "delta": _delta(current_views, views.get("previous", 0)),
            },
            "revenue": {
                "value": current["revenue"],
                "previous": previous["revenue"],
                "delta": _delta(current["revenue"], previous["revenue"]),
            },
            "attendance_rate": {
                "value": attendance_rate,
                "previous": previous_attendance_rate,
                "delta": (
                    _delta(attendance_rate, previous_attendance_rate)
                    if attendance_rate is not None and previous_attendance_rate
                    else None
                ),
            },
        },
        "revenue_series": {
            "current": series_for(start),
            "previous": series_for(previous_start),
        },
        "busiest_day": busiest,
        "repeat_rate": repeat_rate,
        "followers": {"total": followers_total, "series": followers_series},
        "next_event": shape(upcoming_events[0]) if upcoming_events else None,
        "upcoming": [shape(e) for e in upcoming_events[:UPCOMING_LIMIT]],
        "needs_attention": attention[:ATTENTION_LIMIT],
        "needs_attention_total": len(attention),
        "reach": [shape(e) for e in past_events[:REACH_LIMIT]],
    }
