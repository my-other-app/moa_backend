"""The discover lists and the For You feed as they were computed before they
moved into the database (origin/master, 2026-10-17): every candidate read,
distances, counts and order worked out in Python. Kept here as the yardstick
the new queries are held to: same rows, same order, same numbers.

Only what the comparisons need: ids, totals, distances and counts, not the
cards' presentation, which the new code still builds with the same functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Float, and_, case, cast, exists, func, literal, or_, select, text
from sqlalchemy.orm import contains_eager, joinedload

from app.api.closeout.models import EventStatus, RetentionMode, RetentionState
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubUsersLink
from app.api.discover import geo
from app.api.discover.service import window
from app.api.events.models import EventCategories, EventInterestsLink, EventRegistrationsLink, Events
from app.api.events import recommendations as rec
from app.api.users.models import UserInterests, UserProfiles

SHOWN = (EventStatus.published, EventStatus.live)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _end():
    return Events.event_datetime + Events.duration * text("INTERVAL '1 hour'")


def _like(q: str) -> str:
    q = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def _filtered(now, *, when="any", category=None, type_="any", price="any", q=None):
    stmt = (
        select(Events)
        .join(Clubs, Clubs.id == Events.club_id)
        .outerjoin(EventCategories, EventCategories.id == Events.category_id)
        .options(contains_eager(Events.club), contains_eager(Events.category))
        .where(
            Events.is_deleted == False, Events.lifecycle.in_(SHOWN), Clubs.is_verified == True,  # noqa: E712
            Clubs.is_deleted == False, _end() >= now,  # noqa: E712
        )
    )
    start, end = window(when, now)
    if start is not None:
        stmt = stmt.where(_end() >= start)
    if end is not None:
        stmt = stmt.where(Events.event_datetime < end)
    if category:
        stmt = stmt.where(Events.category_id == category)
    if type_ == "online":
        stmt = stmt.where(Events.is_online == True)  # noqa: E712
    elif type_ == "in_person":
        stmt = stmt.where(func.coalesce(Events.is_online, False) == False)  # noqa: E712
    if price == "free":
        stmt = stmt.where(func.coalesce(Events.has_fee, False) == False)  # noqa: E712
    elif price == "paid":
        stmt = stmt.where(Events.has_fee == True)  # noqa: E712
    if q and q.strip():
        p = _like(q)
        stmt = stmt.where(or_(
            Events.name.ilike(p, escape="\\"), Clubs.name.ilike(p, escape="\\"),
            Events.location_name.ilike(p, escape="\\"), EventCategories.name.ilike(p, escape="\\"),
        ))
    return stmt.order_by(Events.event_datetime, Events.id)


async def going_counts(session, ids):
    if not ids:
        return {}
    rows = await session.execute(
        select(EventRegistrationsLink.event_id, func.count())
        .where(EventRegistrationsLink.event_id.in_(ids), EventRegistrationsLink.is_deleted == False)  # noqa: E712
        .group_by(EventRegistrationsLink.event_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def follower_counts(session, ids):
    if not ids:
        return {}
    rows = await session.execute(
        select(ClubUsersLink.club_id, func.count())
        .where(ClubUsersLink.club_id.in_(ids), ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)  # noqa: E712
        .group_by(ClubUsersLink.club_id)
    )
    return {k: int(v) for k, v in rows.all()}


def _km(e, here):
    if here is None or e.is_online or e.lat is None or e.lng is None:
        return None
    return geo.km(here[0], here[1], e.lat, e.lng)


async def search_events(session, here, *, q=None, when="any", category=None, type_="any", price="any", radius_km=50.0, sort="date", offset=0, limit=24, now) -> dict:
    rows = list((await session.scalars(_filtered(now, when=when, category=category, type_=type_, price=price, q=q))).unique().all())
    dist = {e.id: _km(e, here) for e in rows}
    if here is not None and type_ != "online" and radius_km > 0:
        rows = [e for e in rows if e.is_online or dist[e.id] is None or dist[e.id] <= radius_km]
    going = await going_counts(session, [e.id for e in rows])
    if sort == "distance" and here is not None:
        rows.sort(key=lambda e: (dist[e.id] is None, dist[e.id] or 0.0, _aware(e.event_datetime), e.id))
    elif sort == "popular":
        rows.sort(key=lambda e: (-(going.get(e.id, 0) + (e.page_views or 0)), _aware(e.event_datetime), e.id))
    page = rows[offset:offset + limit]
    return dict(
        ids=[e.id for e in page], total=len(rows),
        distance=[round(dist[e.id], 1) if dist[e.id] is not None else None for e in page],
        going=[going.get(e.id, 0) for e in page],
    )


async def club_points(session, clubs):
    out = {c.id: (c.lat, c.lng) for c in clubs if c.lat is not None and c.lng is not None}
    need = [c.id for c in clubs if c.id not in out]
    if need:
        rows = await session.execute(
            select(Events.club_id, func.avg(Events.lat), func.avg(Events.lng))
            .where(
                Events.club_id.in_(need), Events.lat.isnot(None), Events.lng.isnot(None),
                Events.is_deleted == False, Events.lifecycle != EventStatus.draft,  # noqa: E712
                func.coalesce(Events.is_online, False) == False,  # noqa: E712
            )
            .group_by(Events.club_id)
        )
        for club_id, lat, lng in rows.all():
            out[club_id] = (float(lat), float(lng))
    return out


async def _club_rows(session, *, q=None, interest=None):
    stmt = select(Clubs).where(Clubs.is_verified == True, Clubs.is_deleted == False)  # noqa: E712
    if q and q.strip():
        p = _like(q)
        stmt = stmt.where(or_(Clubs.name.ilike(p, escape="\\"), Clubs.tagline.ilike(p, escape="\\"), Clubs.location_name.ilike(p, escape="\\")))
    if interest:
        stmt = stmt.where(exists(select(ClubInterestsLink.id).where(
            ClubInterestsLink.club_id == Clubs.id, ClubInterestsLink.interest_id == interest, ClubInterestsLink.is_deleted == False,  # noqa: E712
        )))
    return list((await session.scalars(stmt.order_by(Clubs.id))).all())


def _club_distances(clubs, points, here):
    if here is None:
        return {c.id: None for c in clubs}
    return {c.id: (geo.km(here[0], here[1], *points[c.id]) if c.id in points else None) for c in clubs}


async def search_clubs(session, here, *, q=None, interest=None, radius_km=50.0, sort="popular", offset=0, limit=24) -> dict:
    rows = await _club_rows(session, q=q, interest=interest)
    dist = _club_distances(rows, await club_points(session, rows), here)
    if here is not None and radius_km > 0:
        rows = [c for c in rows if dist[c.id] is None or dist[c.id] <= radius_km]
    followers = await follower_counts(session, [c.id for c in rows])
    if sort == "nearby" and here is not None:
        rows.sort(key=lambda c: (dist[c.id] is None, dist[c.id] or 0.0, -followers.get(c.id, 0), c.id))
    elif sort == "newest":
        rows.sort(key=lambda c: (_aware(c.created_at) or datetime.min.replace(tzinfo=timezone.utc), c.id), reverse=True)
    else:
        rows.sort(key=lambda c: (-followers.get(c.id, 0), (c.name or "").lower(), c.id))
    page = rows[offset:offset + limit]
    return dict(
        ids=[c.id for c in page], total=len(rows),
        distance=[round(dist[c.id], 1) if dist.get(c.id) is not None else None for c in page],
        followers=[followers.get(c.id, 0) for c in page],
    )


async def home(session, here, *, now) -> dict:
    rows = list((await session.scalars(_filtered(now))).unique().all())
    going = await going_counts(session, [e.id for e in rows])
    near = []
    if here is not None:
        near = [e for e in rows if not e.is_online and e.lat is not None and e.lng is not None and _km(e, here) <= 50.0][:12]
    week_end = now + timedelta(days=7)
    this_week = [e for e in rows if _aware(e.event_datetime) < week_end][:8]
    online = [e for e in rows if e.is_online][:8]
    popular = sorted(rows, key=lambda e: (-(going.get(e.id, 0) + (e.page_views or 0)), _aware(e.event_datetime), e.id))[:8]

    club_rows = await _club_rows(session)
    followers = await follower_counts(session, [c.id for c in club_rows])
    dist = _club_distances(club_rows, await club_points(session, club_rows) if here is not None else {}, here)
    if here is not None:
        club_rows.sort(key=lambda c: (dist[c.id] is None, dist[c.id] or 0.0, -followers.get(c.id, 0), c.id))
    else:
        club_rows.sort(key=lambda c: (-followers.get(c.id, 0), (c.name or "").lower(), c.id))

    def out(es):
        return [(e.id, round(_km(e, here), 1) if _km(e, here) is not None else None, going.get(e.id, 0)) for e in es]

    top = club_rows[:8]
    return dict(
        near=out(near), this_week=out(this_week), online=out(online), popular=out(popular),
        clubs=[(c.id, round(dist[c.id], 1) if dist.get(c.id) is not None else None, followers.get(c.id, 0)) for c in top],
        upcoming=len(rows),
    )


async def for_you(session, user_id: int, limit: int = 10, offset: int = 0):
    """The For You query as it was: correlated subqueries per event."""
    now = func.now()
    event_end = Events.event_datetime + Events.duration * text("INTERVAL '1 hour'")
    user_interest_ids = select(UserInterests.interest_id).where(UserInterests.user_id == user_id, UserInterests.is_deleted == False).scalar_subquery()  # noqa: E712
    user_org_id = select(UserProfiles.org_id).where(UserProfiles.user_id == user_id).limit(1).scalar_subquery()
    shared_interests = (
        select(func.count()).select_from(EventInterestsLink)
        .where(EventInterestsLink.event_id == Events.id, EventInterestsLink.is_deleted == False, EventInterestsLink.interest_id.in_(user_interest_ids))  # noqa: E712
        .scalar_subquery()
    )

    def _club_flag(column):
        return (
            select(func.count()).select_from(ClubUsersLink)
            .where(ClubUsersLink.club_id == Events.club_id, ClubUsersLink.user_id == user_id, ClubUsersLink.is_deleted == False, column == True)  # noqa: E712
            .scalar_subquery()
        )

    is_pinned = _club_flag(ClubUsersLink.is_pinned)
    is_following = _club_flag(ClubUsersLink.is_following)
    club_history = (
        select(func.count()).select_from(EventRegistrationsLink)
        .join(Events.__table__.alias("prev_event"), text("prev_event.id = event_registrations_link.event_id"))
        .where(EventRegistrationsLink.user_id == user_id, EventRegistrationsLink.is_deleted == False, text("prev_event.club_id = events.club_id"))  # noqa: E712
        .scalar_subquery()
    )
    category_history = (
        select(func.count()).select_from(EventRegistrationsLink)
        .join(Events.__table__.alias("prev_cat_event"), text("prev_cat_event.id = event_registrations_link.event_id"))
        .where(EventRegistrationsLink.user_id == user_id, EventRegistrationsLink.is_deleted == False, text("prev_cat_event.category_id = events.category_id"))  # noqa: E712
        .scalar_subquery()
    )
    registration_count = (
        select(func.count()).select_from(EventRegistrationsLink)
        .where(EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
        .scalar_subquery()
    )
    interest_score = rec.W_INTEREST * func.least(shared_interests, rec.INTEREST_CAP)
    club_score = rec.W_PINNED * func.least(is_pinned, 1) + rec.W_FOLLOWING * func.least(is_following, 1)
    org_score = rec.W_SAME_ORG * case((and_(Clubs.org_id.isnot(None), Clubs.org_id == user_org_id), 1), else_=0)
    history_score = rec.W_CLUB_HISTORY * func.least(club_history, 1) + (rec.W_CATEGORY_HISTORY * func.least(category_history, 1))
    attention = cast(Events.page_views, Float) + (rec.POPULARITY_REGISTRATION_FACTOR * cast(registration_count, Float))
    popularity_score = rec.W_POPULARITY * func.least(func.ln(1.0 + attention) / 6.0, 1.0)
    days_until = func.extract("epoch", Events.event_datetime - now) / 86400.0
    urgency_score = case(*[(days_until <= days, literal(points)) for days, points in rec.URGENCY_BANDS], else_=literal(0.0))
    closing_soon = rec.W_CLOSING_SOON * case(
        (and_(Events.reg_enddate.isnot(None), Events.reg_enddate >= now, Events.reg_enddate <= now + text("INTERVAL '3 days'")), 1), else_=0,
    )
    score = (interest_score + club_score + org_score + history_score + popularity_score + urgency_score + closing_soon).label("score")
    already_registered = select(EventRegistrationsLink.id).where(
        EventRegistrationsLink.event_id == Events.id, EventRegistrationsLink.user_id == user_id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
    ).exists()
    club_hidden = select(ClubUsersLink.id).where(
        ClubUsersLink.club_id == Events.club_id, ClubUsersLink.user_id == user_id, ClubUsersLink.is_hidden == True, ClubUsersLink.is_deleted == False,  # noqa: E712
    ).exists()
    query = (
        select(Events, score)
        .outerjoin(Clubs, Clubs.id == Events.club_id)
        .where(Events.is_deleted == False, event_end >= now, (Events.reg_enddate.is_(None)) | (Events.reg_enddate >= now), ~already_registered, ~club_hidden)  # noqa: E712
        # The one deliberate change: only what the public can see (the old
        # feed also offered drafts and unverified clubs' events).
        .where(Events.lifecycle.in_(SHOWN), Clubs.is_verified == True, Clubs.is_deleted == False)  # noqa: E712
        .options(joinedload(Events.category), joinedload(Events.club))
        .order_by(score.desc(), Events.event_datetime.asc(), Events.id.asc())
        .limit(limit).offset(offset)
    )
    rows = (await session.execute(query)).unique().all()
    return [row[0].id for row in rows], [float(row[1]) for row in rows]


def clock_outcome(e: Events, job_state: Optional[RetentionState], now: datetime) -> EventStatus:
    """Where advance_clock's rules take one event this minute."""
    status = e.lifecycle or EventStatus.published
    end = e.event_datetime + timedelta(hours=e.duration or 0)
    if status is EventStatus.published and now >= e.event_datetime:
        status = EventStatus.live
    if status is EventStatus.live and now >= end + timedelta(minutes=e.close_after_minutes or 240):
        status = EventStatus.closed
    if status is EventStatus.closed:
        done = job_state is RetentionState.deleted
        if done or ((e.retention_mode or RetentionMode.keep) is RetentionMode.keep and now >= end + timedelta(days=30)):
            status = EventStatus.archived
    return status
