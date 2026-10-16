"""The club's followers, as the followers page needs them.

A follower is more than a name and a date: how many of the club's events
they signed up for and came to, when they were last seen, where they are
from, what they care about, and which link brought them. Above the list,
the audience as a whole: how it grows, how much of it turns up, where it
comes from, and what it is interested in — the things a club plans the next
event from.
"""
import csv
import io
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.api.clubs.models import ClubUsersLink
from app.api.events.models import EventRegistrationsLink, Events
from app.api.links.models import TrackingLinks
from app.api.users.models import UserProfiles, Users

SEGMENTS = ("all", "new", "regulars", "once", "never", "quiet")
SORTS = ("newest", "oldest", "active", "name")
NEW_DAYS = 30
QUIET_DAYS = 90


def _display_name(user: Users) -> str:
    p = user.profile
    return (p.full_name if p and p.full_name else None) or user.full_name or user.username or "Someone"


def _avatar(user: Users):
    p = user.profile
    if p is None:
        return None
    return p.profile_pic or (p.avatar.image if p.avatar else None)


async def _activity(session, club_id: int, user_ids: list[int]) -> dict[int, dict]:
    """Per follower: registrations with this club, how many they came to, when last, whether they are in for something ahead."""
    out = {u: dict(registrations=0, attended=0, last_registered_at=None, upcoming=0) for u in user_ids}
    if not user_ids:
        return out
    now = datetime.now(timezone.utc)
    Reg = EventRegistrationsLink
    q = (
        select(
            Reg.user_id,
            func.count(),
            func.count().filter(Reg.is_attended == True),  # noqa: E712
            func.max(Reg.created_at),
            func.count().filter(Events.event_datetime > now),
        )
        .join(Events, Events.id == Reg.event_id)
        .where(Reg.user_id.in_(user_ids), Reg.is_deleted == False, Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
        .group_by(Reg.user_id)
    )
    for uid, regs, came, last, ahead in (await session.execute(q)).all():
        out[uid].update(registrations=regs, attended=came, last_registered_at=last, upcoming=ahead)
    return out


def _segment_of(followed_at, act: dict, now: datetime) -> list[str]:
    tags = []
    if followed_at and now - followed_at <= timedelta(days=NEW_DAYS):
        tags.append("new")
    if act["attended"] >= 2:
        tags.append("regulars")
    elif act["attended"] == 1:
        tags.append("once")
    else:
        tags.append("never")
    last = act["last_registered_at"]
    seen = last or followed_at
    if seen and now - seen > timedelta(days=QUIET_DAYS) and act["upcoming"] == 0:
        tags.append("quiet")
    return tags


async def _followers(session, club_id: int):
    q = (
        select(ClubUsersLink)
        .where(ClubUsersLink.club_id == club_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)  # noqa: E712
        .options(joinedload(ClubUsersLink.user).options(joinedload(Users.profile).options(joinedload(UserProfiles.avatar), joinedload(UserProfiles.org)), selectinload(Users.interests)))
        .order_by(ClubUsersLink.created_at.desc())
    )
    return list((await session.scalars(q)).unique().all())


async def _rows(session, club_id: int) -> list[dict]:
    links = await _followers(session, club_id)
    acts = await _activity(session, club_id, [l.user_id for l in links])
    link_ids = {l.tracking_link_id for l in links if l.tracking_link_id}
    sources = {}
    if link_ids:
        sources = {t.id: t.name for t in (await session.scalars(select(TrackingLinks).where(TrackingLinks.id.in_(link_ids)))).all()}
    now = datetime.now(timezone.utc)
    rows = []
    for l in links:
        u = l.user
        if u is None:
            continue
        act = acts[l.user_id]
        p = u.profile
        rows.append(
            dict(
                user=dict(id=u.id, full_name=_display_name(u), username=u.username, avatar=_avatar(u)),
                org=(p.org.name if p and p.org else None),
                interests=[i.name for i in (u.interests or [])][:4],
                followed_at=l.created_at,
                source=sources.get(l.tracking_link_id) if l.tracking_link_id else None,
                registrations=act["registrations"],
                attended=act["attended"],
                last_registered_at=act["last_registered_at"],
                upcoming=act["upcoming"],
                segments=_segment_of(l.created_at, act, now),
            )
        )
    return rows


def _matches(row: dict, term: str) -> bool:
    t = term.lower()
    return t in (row["user"]["full_name"] or "").lower() or t in (row["user"]["username"] or "").lower() or t in (row["org"] or "").lower()


async def list_followers(session, club_id: int, *, segment: str = "all", search: Optional[str] = None, sort: str = "newest", limit: int = 24, offset: int = 0) -> tuple[list, int, dict]:
    rows = await _rows(session, club_id)
    counts = {s: 0 for s in SEGMENTS}
    counts["all"] = len(rows)
    for r in rows:
        for s in r["segments"]:
            counts[s] += 1
    if segment != "all":
        rows = [r for r in rows if segment in r["segments"]]
    if search and search.strip():
        rows = [r for r in rows if _matches(r, search.strip())]
    floor = datetime(1970, 1, 1, tzinfo=timezone.utc)
    key = {
        "newest": lambda r: -(r["followed_at"] or floor).timestamp(),
        "oldest": lambda r: (r["followed_at"] or floor).timestamp(),
        "active": lambda r: (-r["attended"], -r["registrations"], -(r["last_registered_at"] or floor).timestamp()),
        "name": lambda r: (r["user"]["full_name"] or "").lower(),
    }[sort]
    rows.sort(key=key)
    return rows[offset : offset + limit], len(rows), counts


def _month(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


async def insights(session, club_id: int) -> dict:
    rows = await _rows(session, club_id)
    now = datetime.now(timezone.utc)
    total = len(rows)
    new = sum(1 for r in rows if "new" in r["segments"])
    prev_new = sum(1 for r in rows if r["followed_at"] and timedelta(days=NEW_DAYS) < now - r["followed_at"] <= timedelta(days=2 * NEW_DAYS))
    came = sum(1 for r in rows if r["attended"] >= 1)
    registered_ahead = sum(1 for r in rows if r["upcoming"] > 0)
    quiet = sum(1 for r in rows if "quiet" in r["segments"])
    regulars = sum(1 for r in rows if "regulars" in r["segments"])

    # Growth: joins per month for the last twelve, and the running total.
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    joined = Counter(_month(r["followed_at"]) for r in rows if r["followed_at"])
    before = sum(n for k, n in joined.items() if k < months[0])
    growth, running = [], before
    for k in months:
        running += joined.get(k, 0)
        growth.append(dict(month=k, joined=joined.get(k, 0), total=running))

    orgs = Counter(r["org"] or "Not given" for r in rows)
    interests = Counter(i for r in rows for i in r["interests"])
    sources = Counter(r["source"] or "On the app" for r in rows)
    upcoming_event = await session.scalar(
        select(Events).where(Events.club_id == club_id, Events.is_deleted == False, Events.event_datetime > now).order_by(Events.event_datetime.asc()).limit(1)  # noqa: E712
    )
    return dict(
        total=total, new=new, new_delta=(new - prev_new) if (new or prev_new) else None,
        came=came, regulars=regulars, registered_ahead=registered_ahead, quiet=quiet,
        next_event=dict(id=upcoming_event.id, name=upcoming_event.name) if upcoming_event else None,
        growth=growth,
        by_org=[dict(name=k, count=n) for k, n in orgs.most_common(6)],
        interests=[dict(name=k, count=n) for k, n in interests.most_common(8)],
        sources=[dict(name=k, count=n) for k, n in sources.most_common(6)],
        segments={s: sum(1 for r in rows if s in r["segments"]) for s in SEGMENTS if s != "all"} | {"all": total},
        generated_at=now,
    )


async def export_csv(session, club_id: int, *, segment: str = "all") -> str:
    rows, _, _ = await list_followers(session, club_id, segment=segment, limit=100_000, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "username", "organisation", "followed_on", "registrations", "came", "last_registered", "interests", "source"])
    for r in rows:
        w.writerow([
            r["user"]["full_name"], r["user"]["username"] or "", r["org"] or "", r["followed_at"].date().isoformat() if r["followed_at"] else "",
            r["registrations"], r["attended"], r["last_registered_at"].date().isoformat() if r["last_registered_at"] else "", "; ".join(r["interests"]), r["source"] or "",
        ])
    return buf.getvalue()


__all__ = ["list_followers", "insights", "export_csv", "SEGMENTS", "SORTS"]
