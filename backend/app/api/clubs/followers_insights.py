"""The club's followers, as the followers page needs them.

A follower is more than a name and a date: how many of the club's events
they signed up for and came to, when they were last seen, where they are
from, what they care about, and which link brought them. Above the list,
the audience as a whole: how it grows, how much of it turns up, where it
comes from, and what it is interested in — the things a club plans the next
event from.

Worked out in SQL: one row per follower (`_followers`: the follow, the
person, their organisation, the link that brought them, and what they did
with the club, grouped once over the club's registrations), filtered,
sorted and paged there; only the page's people have their interests and
pictures read. The counts and the audience above the list are GROUP BYs
over the same rows. Nothing ever holds every follower in Python, or passes
their ids back as parameters.

The order is the follow's (newest first, then id: the club's follower
index), and each sort keeps that order among people it ranks the same.
"""
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased, joinedload

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.interests.models import Interests
from app.api.links.models import TrackingLinks
from app.api.orgs.models import Organizations
from app.api.users.models import UserInterests, UserProfiles, Users
from app.db.binds import any_of
from app.core.utils import csv_safe

SEGMENTS = ("all", "new", "regulars", "once", "never", "quiet")
SORTS = ("newest", "oldest", "active", "name")
NEW_DAYS = 30
QUIET_DAYS = 90
#: Interests shown with each follower (and counted for the audience).
INTERESTS_SHOWN = 4
#: Lowercasing for search and the name sort: ICU's, which folds every
#: script's capitals as Python's str.lower did, whatever the database's own
#: locale (a "C" database folds only A-Z).
UNICODE = "und-x-icu"


def _lower(column):
    return func.lower(column.collate(UNICODE))


def _following(club_id: int):
    """The people following the club, as a subquery of their ids."""
    C = aliased(ClubUsersLink)
    return select(C.user_id).where(C.club_id == club_id, C.is_following == True, C.is_deleted == False)  # noqa: E712


def _followers(club_id: int, now: datetime, *, place: bool = False):
    """Every live follower of the club, a row each, as a subquery. `place`:
    with each one's place in the follow's order (the audience's counts break
    ties by it; the list does not need it, and leaves the planner free to
    filter early)."""
    C, U, Reg, E = ClubUsersLink, Users, EventRegistrationsLink, Events
    P, O, T = aliased(UserProfiles), aliased(Organizations), aliased(TrackingLinks)

    # What each follower did with the club: one pass over its events' registrations.
    act = (
        select(
            Reg.user_id.label("user_id"),
            func.count().label("registrations"),
            func.count().filter(Reg.is_attended == True).label("attended"),  # noqa: E712
            func.max(Reg.created_at).label("last_registered_at"),
            func.count().filter(E.event_datetime > now).label("upcoming"),
        )
        .join(E, E.id == Reg.event_id)
        .where(
            Reg.is_deleted == False, E.club_id == club_id, E.is_deleted == False,  # noqa: E712
            Reg.user_id.in_(_following(club_id)),
        )
        .group_by(Reg.user_id)
        .subquery("act")
    )
    registrations = func.coalesce(act.c.registrations, 0)
    attended = func.coalesce(act.c.attended, 0)
    upcoming = func.coalesce(act.c.upcoming, 0)
    # The name shown: the profile's, else the account's, else the username.
    display = func.coalesce(func.nullif(P.full_name, ""), func.nullif(U.full_name, ""), func.nullif(U.username, ""), "Someone")
    seen = func.coalesce(act.c.last_registered_at, C.created_at)
    extra = [func.row_number().over(order_by=(C.created_at.desc(), C.id.desc())).label("place")] if place else []
    return (
        select(
            C.id.label("link_id"),
            C.user_id.label("user_id"),
            C.created_at.label("followed_at"),
            U.username.label("username"),
            display.label("full_name"),
            P.id.label("profile_id"),
            P.avatar_id.label("avatar_id"),
            O.name.label("org"),
            T.name.label("source"),
            registrations.label("registrations"),
            attended.label("attended"),
            act.c.last_registered_at.label("last_registered_at"),
            upcoming.label("upcoming"),
            (C.created_at >= now - timedelta(days=NEW_DAYS)).label("is_new"),
            (attended >= 2).label("is_regulars"),
            (attended == 1).label("is_once"),
            (attended == 0).label("is_never"),
            and_(seen < now - timedelta(days=QUIET_DAYS), upcoming == 0).label("is_quiet"),
            *extra,
        )
        .select_from(C)
        .join(U, and_(U.id == C.user_id, U.is_deleted == False))  # noqa: E712
        .outerjoin(P, and_(P.user_id == U.id, P.is_deleted == False))  # noqa: E712
        .outerjoin(O, and_(O.id == P.org_id, O.is_deleted == False))  # noqa: E712
        .outerjoin(T, and_(T.id == C.tracking_link_id, T.is_deleted == False))  # noqa: E712
        .outerjoin(act, act.c.user_id == C.user_id)
        .where(C.club_id == club_id, C.is_following == True, C.is_deleted == False)  # noqa: E712
        .subquery("followers")
    )


def _in_segment(f, segment: str):
    return {"new": f.c.is_new, "regulars": f.c.is_regulars, "once": f.c.is_once, "never": f.c.is_never, "quiet": f.c.is_quiet}[segment]


def _matching(f, term: str):
    """Name, username or organisation contains `term`, case ignored."""
    t = term.lower()
    return or_(
        func.strpos(_lower(f.c.full_name), t) > 0,
        func.strpos(_lower(func.coalesce(f.c.username, "")), t) > 0,
        func.strpos(_lower(func.coalesce(f.c.org, "")), t) > 0,
    )


def _order(f, sort: str) -> tuple:
    follow = (f.c.followed_at.desc(), f.c.link_id.desc())
    return {
        "newest": follow,
        "oldest": (f.c.followed_at.asc(), f.c.link_id.desc()),
        "active": (f.c.attended.desc(), f.c.registrations.desc(), f.c.last_registered_at.desc().nulls_last(), *follow),
        # Code point order, as the list was sorted before ("C": byte order, which is that for UTF-8).
        "name": (_lower(f.c.full_name).collate("C").asc(), *follow),
    }[sort]


def _segment_tags(row) -> list[str]:
    tags = ["new"] if row.is_new else []
    tags.append("regulars" if row.is_regulars else "once" if row.is_once else "never")
    if row.is_quiet:
        tags.append("quiet")
    return tags


def _picks():
    """Each person's interests, numbered in the order they picked them, as
    Users.interests reads them: through the link table as it stands (a pick
    deleted with an account still counts, as it did), live interests only."""
    UI, I = UserInterests.__table__, Interests
    return (
        select(UI.c.user_id, I.name, func.row_number().over(partition_by=UI.c.user_id, order_by=UI.c.id).label("n"))
        .join(I, and_(I.id == UI.c.interest_id, I.is_deleted == False))  # noqa: E712
    )


async def _interests(session, user_ids: list[int]) -> dict[int, list[str]]:
    """The first INTERESTS_SHOWN interests of each person."""
    if not user_ids:
        return {}
    ranked = _picks().where(any_of(UserInterests.__table__.c.user_id, user_ids)).subquery()
    out: dict[int, list[str]] = {}
    rows = await session.execute(
        select(ranked.c.user_id, ranked.c.name).where(ranked.c.n <= INTERESTS_SHOWN).order_by(ranked.c.user_id, ranked.c.n)
    )
    for user_id, name in rows.all():
        out.setdefault(user_id, []).append(name)
    return out


async def _pictures(session, rows) -> dict[int, object]:
    """Each row's picture: the profile's own photo, else its stock avatar
    (the page's profiles only: each picture's links are signed as it is read)."""
    profile_ids = [r.profile_id for r in rows if r.profile_id is not None]
    if not profile_ids:
        return {}
    profiles = await session.scalars(
        select(UserProfiles).where(any_of(UserProfiles.id, profile_ids)).options(joinedload(UserProfiles.avatar))
    )
    return {p.id: p.profile_pic or (p.avatar.image if p.avatar else None) for p in profiles.unique().all()}


async def _hydrate(session, rows) -> list[dict]:
    interests = await _interests(session, [r.user_id for r in rows])
    pictures = await _pictures(session, rows)
    return [
        dict(
            user=dict(id=r.user_id, full_name=r.full_name, username=r.username, avatar=pictures.get(r.profile_id)),
            org=r.org,
            interests=interests.get(r.user_id, []),
            followed_at=r.followed_at,
            source=r.source,
            registrations=int(r.registrations),
            attended=int(r.attended),
            last_registered_at=r.last_registered_at,
            upcoming=int(r.upcoming),
            segments=_segment_tags(r),
        )
        for r in rows
    ]


async def _counts(session, f) -> dict:
    row = (await session.execute(
        select(func.count(), *(func.count().filter(_in_segment(f, s)) for s in SEGMENTS[1:])).select_from(f)
    )).one()
    return {"all": int(row[0]), **{s: int(n) for s, n in zip(SEGMENTS[1:], row[1:])}}


async def list_followers(session, club_id: int, *, segment: str = "all", search: Optional[str] = None, sort: str = "newest", limit: int = 24, offset: int = 0) -> tuple[list, int, dict]:
    now = datetime.now(timezone.utc)
    f = _followers(club_id, now)
    counts = await _counts(session, f)
    where = []
    if segment != "all":
        where.append(_in_segment(f, segment))
    if search and search.strip():
        where.append(_matching(f, search.strip()))
    total = counts["all"] if not where else int(await session.scalar(select(func.count()).select_from(f).where(*where)) or 0)
    rows = (await session.execute(select(f).where(*where).order_by(*_order(f, sort)).limit(limit).offset(offset))).all()
    return await _hydrate(session, rows), total, counts


def _month(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


async def _most_common(session, f, key, n: int) -> list[dict]:
    """Counter(key over the followers).most_common(n): the largest first,
    ties in the order each first appears among the followers."""
    k = key.label("k")
    rows = await session.execute(
        select(k, func.count().label("n")).select_from(f).group_by(k).order_by(func.count().desc(), func.min(f.c.place)).limit(n)
    )
    return [dict(name=name, count=int(count)) for name, count in rows.all()]


async def _top_interests(session, f, club_id: int, n: int) -> list[dict]:
    """The interests the followers show (each one's first INTERESTS_SHOWN),
    most common first, ties in the order they first appear."""
    ranked = _picks().where(UserInterests.__table__.c.user_id.in_(_following(club_id))).subquery()
    first = func.min(f.c.place * (INTERESTS_SHOWN + 1) + ranked.c.n)
    rows = await session.execute(
        select(ranked.c.name, func.count())
        .select_from(f)
        .join(ranked, and_(ranked.c.user_id == f.c.user_id, ranked.c.n <= INTERESTS_SHOWN))
        .group_by(ranked.c.name)
        .order_by(func.count().desc(), first)
        .limit(n)
    )
    return [dict(name=name, count=int(count)) for name, count in rows.all()]


async def insights(session, club_id: int) -> dict:
    now = datetime.now(timezone.utc)
    f = _followers(club_id, now, place=True)
    new_since, prev_since = now - timedelta(days=NEW_DAYS), now - timedelta(days=2 * NEW_DAYS)
    row = (await session.execute(
        select(
            func.count(),
            func.count().filter(f.c.is_new),
            func.count().filter(f.c.followed_at >= prev_since, f.c.followed_at < new_since),
            func.count().filter(f.c.attended >= 1),
            func.count().filter(f.c.upcoming > 0),
            *(func.count().filter(_in_segment(f, s)) for s in SEGMENTS[1:]),
        ).select_from(f)
    )).one()
    total, new, prev_new, came, registered_ahead = (int(v) for v in row[:5])
    segments = {s: int(v) for s, v in zip(SEGMENTS[1:], row[5:])}

    # Growth: joins per month for the last twelve, and the running total.
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    month = func.to_char(func.timezone("UTC", f.c.followed_at), "YYYY-MM")
    joined = {k: int(v) for k, v in (await session.execute(select(month, func.count()).select_from(f).group_by(month))).all()}
    before = sum(n for k, n in joined.items() if k < months[0])
    growth, running = [], before
    for k in months:
        running += joined.get(k, 0)
        growth.append(dict(month=k, joined=joined.get(k, 0), total=running))

    by_org = await _most_common(session, f, func.coalesce(func.nullif(f.c.org, ""), "Not given"), 6)
    interests = await _top_interests(session, f, club_id, 8)
    sources = await _most_common(session, f, func.coalesce(func.nullif(f.c.source, ""), "On the app"), 6)
    upcoming_event = await session.scalar(
        select(Events).where(Events.club_id == club_id, Events.is_deleted == False, Events.event_datetime > now).order_by(Events.event_datetime.asc()).limit(1)  # noqa: E712
    )
    return dict(
        total=total, new=new, new_delta=(new - prev_new) if (new or prev_new) else None,
        came=came, regulars=segments["regulars"], registered_ahead=registered_ahead, quiet=segments["quiet"],
        next_event=dict(id=upcoming_event.id, name=upcoming_event.name) if upcoming_event else None,
        growth=growth,
        by_org=by_org,
        interests=interests,
        sources=sources,
        segments=segments | {"all": total},
        generated_at=now,
    )


EXPORT_HEADERS = ["name", "username", "organisation", "followed_on", "registrations", "came", "last_registered", "interests", "source"]


async def export_rows(session, club_id: int, *, segment: str = "all"):
    """The followers in a segment, a row each (typed: the days as dates, the
    counts as numbers). The CSV and the workbook are both written from it."""
    rows, _, _ = await list_followers(session, club_id, segment=segment, limit=100_000, offset=0)
    return (
        [
            r["user"]["full_name"], r["user"]["username"] or "", r["org"] or "", r["followed_at"].date() if r["followed_at"] else "",
            r["registrations"], r["attended"], r["last_registered_at"].date() if r["last_registered_at"] else "", "; ".join(r["interests"]), r["source"] or "",
        ]
        for r in rows
    )


async def export_csv(session, club_id: int, *, segment: str = "all") -> str:
    buf = io.StringIO()
    w = csv_safe.writer(buf)
    w.writerow(EXPORT_HEADERS)
    for row in await export_rows(session, club_id, segment=segment):
        w.writerow([v.isoformat() if isinstance(v, date) else v for v in row])
    return buf.getvalue()


async def export_xlsx(session, club_id: int, *, segment: str = "all") -> str:
    """The same rows as an .xlsx file on disk; the caller sends it and
    removes it (app/core/utils/spreadsheet.py)."""
    from app.core.utils import spreadsheet

    rows = await export_rows(session, club_id, segment=segment)
    return await spreadsheet.write_xlsx(rows, title="Followers", headers=EXPORT_HEADERS)


__all__ = ["list_followers", "insights", "export_csv", "export_xlsx", "SEGMENTS", "SORTS"]
