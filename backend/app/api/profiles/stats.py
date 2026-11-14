"""Counts, never names: an event's networking for its club, and the
platform's for the admins."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Integer, cast, exists, func, literal_column, select

from app.api.events.models import EventRegistrationsLink as R
from app.api.profiles.models import NetworkEncounters, NetworkPeople, ProfileCodes, ProfileLinks, ProfileProjects
from app.api.users.models import UserProfiles, Users, UserTypes

IST_ZONE = literal_column("'Asia/Kolkata'")
#: Below this many people checked in, an event's stats are all zeros (too_few).
MIN_CHECKED_IN = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def event_stats(session, event_id: int) -> dict:
    """GET /events/{id}/networking/stats: who is checked in, how many took
    part, the saves made there, the pairs both of whom saved the other
    there, and the saves by hour (India time). Under MIN_CHECKED_IN people
    checked in, zeros and `too_few` (checked_in included: the door list
    already says who came; the numbers would say who saved whom)."""
    NP, E = NetworkPeople, NetworkEncounters
    checked_in = int(await session.scalar(
        select(func.count(func.distinct(R.user_id))).where(R.event_id == event_id, R.is_attended == True, R.is_deleted == False)  # noqa: E712
    ) or 0)
    if checked_in < MIN_CHECKED_IN:
        # Among a handful of people the numbers say who saved whom, and when.
        return {"checked_in": 0, "people_networked": 0, "saves": 0, "mutual_pairs": 0, "by_hour": [], "too_few": True}
    at = select(NP.owner_id, NP.person_id).join(E, E.network_person_id == NP.id).where(E.event_id == event_id).subquery()
    saves = int(await session.scalar(select(func.count()).select_from(at)) or 0)
    people = int(await session.scalar(
        select(func.count(func.distinct(literal_column("uid")))).select_from(
            select(at.c.owner_id.label("uid")).union_all(select(at.c.person_id.label("uid"))).subquery()
        )
    ) or 0)
    a, b = at.alias("a"), at.alias("b")
    mutual = int(await session.scalar(
        select(func.count()).select_from(a).join(b, (a.c.owner_id == b.c.person_id) & (a.c.person_id == b.c.owner_id))
        .where(a.c.owner_id < a.c.person_id)
    ) or 0)
    hour = func.date_trunc("hour", func.timezone(IST_ZONE, E.met_at))
    rows = (await session.execute(
        select(hour.label("h"), func.count()).where(E.event_id == event_id).group_by(hour).order_by(hour)
    )).all()
    by_hour = [{"hour": h.strftime("%Y-%m-%dT%H:00:00+05:30"), "saves": int(n)} for h, n in rows]
    return {"checked_in": checked_in, "people_networked": people, "saves": saves, "mutual_pairs": mutual, "by_hour": by_hour, "too_few": False}


async def summary(session) -> dict:
    """GET /admin/networking/summary."""
    from app.api.reports.models import ContentReports

    now = _now()
    live = (Users.user_type == UserTypes.app_user, Users.is_deleted == False)  # noqa: E712
    # The photo column's type cannot be part of a cached statement (its
    # sizes are a dict): whether there is one is asked in SQL text.
    has_photo = literal_column("user_profiles.profile_pic IS NOT NULL", Boolean).label("has_photo")
    profiles = select(UserProfiles.user_id, UserProfiles.headline, has_photo).join(Users, Users.id == UserProfiles.user_id).where(
        UserProfiles.is_deleted == False, *live,  # noqa: E712
    ).subquery()
    total = int(await session.scalar(select(func.count()).select_from(profiles)) or 0)
    completed = int(await session.scalar(
        select(func.count()).select_from(profiles).where(
            profiles.c.headline.isnot(None), profiles.c.headline != "", profiles.c.has_photo.is_(True),
            exists().where(ProfileLinks.user_id == profiles.c.user_id),
            exists().where(ProfileProjects.user_id == profiles.c.user_id),
        )
    ) or 0)
    codes = int(await session.scalar(select(func.count()).select_from(ProfileCodes).where(ProfileCodes.replaced_at.is_(None))) or 0)
    NP = NetworkPeople
    s7 = int(await session.scalar(select(func.count()).select_from(NP).where(NP.created_at >= now - timedelta(days=7))) or 0)
    s30 = int(await session.scalar(select(func.count()).select_from(NP).where(NP.created_at >= now - timedelta(days=30))) or 0)
    all_saves, mutual = (await session.execute(select(func.count(), func.coalesce(func.sum(cast(NP.mutual, Integer)), 0)).select_from(NP))).one()
    reports = int(await session.scalar(
        select(func.count()).select_from(ContentReports).where(ContentReports.target_type == "profile", ContentReports.status == "open")
    ) or 0)
    return {
        "profiles_completed": completed, "profiles_total": total, "codes_active": codes, "saves_7d": s7, "saves_30d": s30,
        "mutual_rate": round(100 * int(mutual) / int(all_saves), 1) if all_saves else 0.0, "reports_open": reports,
    }
