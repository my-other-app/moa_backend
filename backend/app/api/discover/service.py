"""The public discover site: what is on, near the visitor or anywhere, and
the clubs behind it.

What the public sees: an event that is published or live, not deleted, not
over, by a club that is verified and not deleted; a club that is verified
and not deleted. Once over, an event the club held still shows on the club
page's past list, on its own page and in the sitemap, and nowhere else.

The data set is small (college clubs), so the candidate rows are read once
and distance, sorting and paging happen here, after the filters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.orm import contains_eager, selectinload

from app.api.agenda import service as agenda
from app.api.closeout.models import EventStatus
from app.api.clubs import service as clubs_service
from app.api.clubs.events_list import registration_open
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubSocials, ClubTeamMembers, ClubUsersLink
from app.api.discover import geo
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventCategories, EventInterestsLink, EventRegistrationsLink, Events
from app.api.interests.models import Interests
from app.api.organisers import service as organisers
from app.api.sites import service as sites
from app.api.sites.models import ClubSites
from app.api.threads import service as threads
from app.api.threads.models import ClubThreads
from app.core import public_urls
from app.response import CustomHTTPException

IST = ZoneInfo("Asia/Kolkata")
SHOWN = (EventStatus.published, EventStatus.live)
HELD = (EventStatus.published, EventStatus.live, EventStatus.closed, EventStatus.archived)
NEAR_KM = 50.0
CLUB_TEAM_ON_EVENT = 8
ALSO_ON = 4
SITEMAP_MAX = 5000
SITEMAP_PAST = timedelta(days=365)
WHENS = ("any", "today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _end_expr():
    return Events.event_datetime + Events.duration * text("INTERVAL '1 hour'")


def _ends_at(e: Events) -> datetime:
    return _aware(e.event_datetime) + timedelta(hours=float(e.duration or 0))


def _like(q: str) -> str:
    q = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


def _img(value, *keys) -> Optional[str]:
    """One URL out of an image column (a dict of presigned URLs by size)."""
    if not isinstance(value, dict):
        return None
    for k in keys:
        v = value.get(k)
        if v:
            return v
    return None


def _here(where: dict) -> Optional[tuple[float, float]]:
    if where and where.get("lat") is not None and where.get("lng") is not None:
        return where["lat"], where["lng"]
    return None


# ─── Time windows ────────────────────────────────────────────────────────


def window(when: str, now: datetime) -> tuple[Optional[datetime], Optional[datetime]]:
    """The stretch of time a `when` means, with days counted in India."""
    local = now.astimezone(IST)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day = timedelta(days=1)
    wd = today.weekday()  # Monday 0 … Sunday 6
    if when == "today":
        return today, today + day
    if when == "tomorrow":
        return today + day, today + 2 * day
    if when == "this_week":
        return now, now + 7 * day
    if when == "this_weekend":
        saturday = today - day if wd == 6 else today + (5 - wd) * day
        return saturday, saturday + 2 * day
    if when == "next_week":
        monday = today + (7 - wd) * day
        return monday, monday + 7 * day
    if when == "this_month":
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return now, first_next
    return None, None


# ─── Events ──────────────────────────────────────────────────────────────


def _visible_where(now: datetime) -> list:
    return [
        Events.is_deleted == False,  # noqa: E712
        Events.lifecycle.in_(SHOWN),
        Clubs.is_verified == True,  # noqa: E712
        Clubs.is_deleted == False,  # noqa: E712
        _end_expr() >= now,
    ]


def _event_select():
    return (
        select(Events)
        .join(Clubs, Clubs.id == Events.club_id)
        .outerjoin(EventCategories, EventCategories.id == Events.category_id)
        .options(contains_eager(Events.club), contains_eager(Events.category))
    )


def filtered(now: datetime, *, when: str = "any", category: Optional[int] = None, type_: str = "any", price: str = "any", q: Optional[str] = None, club_id: Optional[int] = None):
    stmt = _event_select().where(*_visible_where(now))
    start, end = window(when, now)
    if start is not None:
        stmt = stmt.where(_end_expr() >= start)
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
    if club_id is not None:
        stmt = stmt.where(Events.club_id == club_id)
    return stmt.order_by(Events.event_datetime, Events.id)


async def _rows(session, stmt) -> list[Events]:
    return list((await session.scalars(stmt)).unique().all())


async def going_counts(session, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(EventRegistrationsLink.event_id, func.count())
        .where(EventRegistrationsLink.event_id.in_(ids), EventRegistrationsLink.is_deleted == False)  # noqa: E712
        .group_by(EventRegistrationsLink.event_id)
    )
    return {k: int(v) for k, v in rows.all()}


def _event_km(e: Events, here) -> Optional[float]:
    if here is None or e.is_online or e.lat is None or e.lng is None:
        return None
    return geo.km(here[0], here[1], e.lat, e.lng)


def status_of(e: Events, now: datetime, going: int) -> str:
    start, end = _aware(e.event_datetime), _ends_at(e)
    if end < now:
        return "ended"
    if start <= now:
        return "live"
    if e.max_participants and going >= e.max_participants:
        return "full"
    reg_end = _aware(e.reg_enddate)
    if reg_end is not None and now <= reg_end <= now + timedelta(hours=48):
        return "closing_soon"
    return "upcoming"


def card(e: Events, now: datetime, going: int, distance: Optional[float] = None) -> dict:
    club = e.club
    cat = e.category
    return dict(
        id=e.id,
        name=e.name,
        slug=e.slug,
        poster=_img(e.poster, "medium", "original"),
        starts_at=_aware(e.event_datetime).isoformat(),
        ends_at=_ends_at(e).isoformat(),
        location_name=e.location_name,
        is_online=bool(e.is_online),
        lat=e.lat,
        lng=e.lng,
        distance_km=round(distance, 1) if distance is not None else None,
        has_fee=bool(e.has_fee),
        fee=float(e.reg_fee) if e.has_fee and e.reg_fee is not None else None,
        category=dict(id=cat.id, name=cat.name) if cat is not None else None,
        club=dict(id=club.id, name=club.name, slug=club.slug, logo=_img(club.logo, "thumbnail", "medium", "original")) if club is not None else None,
        going=going,
        capacity=e.max_participants,
        status=status_of(e, now, going),
        url=public_urls.event_url(e.slug),
    )


async def cards(session, rows: list[Events], now: datetime, here=None, going: Optional[dict] = None) -> list[dict]:
    if going is None:
        going = await going_counts(session, [e.id for e in rows])
    return [card(e, now, going.get(e.id, 0), _event_km(e, here)) for e in rows]


async def category_counts(session, now: datetime) -> dict[int, int]:
    rows = await session.execute(
        select(Events.category_id, func.count(Events.id))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(*_visible_where(now))
        .group_by(Events.category_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def categories(session, now: datetime, *, only_upcoming: bool = False) -> list[dict]:
    """Every category the product ships plus any a club added that has something on; most upcoming first."""
    counts = await category_counts(session, now)
    rows = (await session.scalars(select(EventCategories).where(EventCategories.is_deleted == False))).all()  # noqa: E712
    out = [
        dict(id=c.id, name=c.name, icon=c.icon, upcoming=counts.get(c.id, 0))
        for c in rows
        if counts.get(c.id) or (c.created_by_id is None and not only_upcoming)
    ]
    out.sort(key=lambda c: (-c["upcoming"], c["name"].lower(), c["id"]))
    return out


async def search_events(
    session, where: dict, *, q: Optional[str], when: str, category: Optional[int], type_: str, price: str,
    radius_km: float, sort: str, offset: int, limit: int, now: Optional[datetime] = None,
) -> dict:
    now = now or now_utc()
    rows = await _rows(session, filtered(now, when=when, category=category, type_=type_, price=price, q=q))
    here = _here(where)
    dist = {e.id: _event_km(e, here) for e in rows}
    # Online events are anywhere; an in-person one without a point cannot be ruled out, so it stays.
    if here is not None and type_ != "online" and radius_km > 0:
        rows = [e for e in rows if e.is_online or dist[e.id] is None or dist[e.id] <= radius_km]
    going = await going_counts(session, [e.id for e in rows])
    if sort == "distance" and here is not None:
        rows.sort(key=lambda e: (dist[e.id] is None, dist[e.id] or 0.0, _aware(e.event_datetime), e.id))
    elif sort == "popular":
        rows.sort(key=lambda e: (-(going.get(e.id, 0) + (e.page_views or 0)), _aware(e.event_datetime), e.id))
    page = rows[offset:offset + limit]
    cats = [dict(id=c["id"], name=c["name"], upcoming=c["upcoming"]) for c in await categories(session, now, only_upcoming=True)]
    return dict(
        items=[card(e, now, going.get(e.id, 0), dist[e.id]) for e in page],
        total=len(rows), offset=offset, limit=limit, where=where, categories=cats,
    )


# ─── Clubs ───────────────────────────────────────────────────────────────


def _club_visible():
    return [Clubs.is_verified == True, Clubs.is_deleted == False]  # noqa: E712


async def club_points(session, clubs: list[Clubs]) -> dict[int, tuple[float, float]]:
    """Where each club is: its own point, else the middle of its in-person events' points."""
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


async def follower_counts(session, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(ClubUsersLink.club_id, func.count())
        .where(ClubUsersLink.club_id.in_(ids), ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)  # noqa: E712
        .group_by(ClubUsersLink.club_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def _upcoming_counts(session, ids: list[int], now: datetime) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Events.club_id, func.count(Events.id))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(Events.club_id.in_(ids), *_visible_where(now))
        .group_by(Events.club_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def _interest_names(session, ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        rows = await session.execute(
            select(ClubInterestsLink.club_id, Interests.name)
            .join(Interests, Interests.id == ClubInterestsLink.interest_id)
            .where(ClubInterestsLink.club_id.in_(ids), ClubInterestsLink.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
            .order_by(ClubInterestsLink.id)
        )
        for club_id, name in rows.all():
            if len(out[club_id]) < 3 and name not in out[club_id]:
                out[club_id].append(name)
    return out


async def _published(session, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set((await session.scalars(select(ClubSites.club_id).where(ClubSites.club_id.in_(ids), ClubSites.published_at.isnot(None)))).all())


async def club_cards(session, clubs: list[Clubs], now: datetime, *, distances: Optional[dict] = None, followers: Optional[dict] = None) -> list[dict]:
    ids = [c.id for c in clubs]
    distances = distances or {}
    if followers is None:
        followers = await follower_counts(session, ids)
    upcoming = await _upcoming_counts(session, ids, now)
    interests = await _interest_names(session, ids)
    published = await _published(session, ids)
    return [
        dict(
            id=c.id, name=c.name, slug=c.slug,
            logo=_img(c.logo, "thumbnail", "medium", "original"),
            cover=_img(c.cover, "medium", "large", "original"),
            tagline=c.tagline, location_name=c.location_name,
            followers=followers.get(c.id, 0), upcoming_events=upcoming.get(c.id, 0),
            interests=interests.get(c.id, []),
            distance_km=round(distances[c.id], 1) if distances.get(c.id) is not None else None,
            website_url=sites.public_url(c) if c.id in published else None,
        )
        for c in clubs
    ]


async def _club_rows(session, *, q: Optional[str] = None, interest: Optional[int] = None) -> list[Clubs]:
    stmt = select(Clubs).where(*_club_visible())
    if q and q.strip():
        p = _like(q)
        stmt = stmt.where(or_(Clubs.name.ilike(p, escape="\\"), Clubs.tagline.ilike(p, escape="\\"), Clubs.location_name.ilike(p, escape="\\")))
    if interest:
        stmt = stmt.where(exists(
            select(ClubInterestsLink.id).where(
                ClubInterestsLink.club_id == Clubs.id, ClubInterestsLink.interest_id == interest, ClubInterestsLink.is_deleted == False,  # noqa: E712
            )
        ))
    return list((await session.scalars(stmt.order_by(Clubs.id))).all())


def _club_distances(clubs: list[Clubs], points: dict, here) -> dict[int, Optional[float]]:
    if here is None:
        return {c.id: None for c in clubs}
    return {c.id: (geo.km(here[0], here[1], *points[c.id]) if c.id in points else None) for c in clubs}


async def top_interests(session, limit: int = 16) -> list[dict]:
    n = func.count(func.distinct(Clubs.id))
    rows = await session.execute(
        select(Interests.id, Interests.name, n)
        .join(ClubInterestsLink, ClubInterestsLink.interest_id == Interests.id)
        .join(Clubs, Clubs.id == ClubInterestsLink.club_id)
        .where(ClubInterestsLink.is_deleted == False, Interests.is_deleted == False, *_club_visible())  # noqa: E712
        .group_by(Interests.id, Interests.name)
        .order_by(n.desc(), Interests.name)
        .limit(limit)
    )
    return [dict(id=i, name=name, clubs=int(c)) for i, name, c in rows.all()]


async def search_clubs(
    session, where: dict, *, q: Optional[str], interest: Optional[int], radius_km: float, sort: str,
    offset: int, limit: int, now: Optional[datetime] = None,
) -> dict:
    now = now or now_utc()
    rows = await _club_rows(session, q=q, interest=interest)
    here = _here(where)
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
        items=await club_cards(session, page, now, distances=dist, followers=followers),
        total=len(rows), offset=offset, limit=limit, where=where, interests=await top_interests(session),
    )


def _social_links(raw: dict) -> dict:
    return {k: (sites._social_href(k, v) if v and v.strip() else None) for k, v in raw.items()}


def _team_member(m: ClubTeamMembers, role: Optional[str]) -> dict:
    return dict(
        name=m.name, role=role, photo=_img(m.photo, "medium", "original", "thumbnail"),
        links=_social_links({k: getattr(m, k) for k in ("instagram", "linkedin", "x", "website")}),
    )


async def club_page(session, slug: str, viewer_id: Optional[int], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    club = await session.scalar(select(Clubs).where(Clubs.slug == slug, *_club_visible()))
    if club is None:
        raise CustomHTTPException(404, message="Club not found")
    detail = (await club_cards(session, [club], now))[0]
    socials = await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club.id, ClubSocials.is_deleted == False))  # noqa: E712
    detail.update(
        about=club.about, contact_email=club.contact_email,
        socials=_social_links({k: getattr(socials, k, None) for k in ("instagram", "linkedin", "youtube", "website")}),
    )

    upcoming = await _rows(session, filtered(now, club_id=club.id).limit(60))
    past_where = [
        Events.club_id == club.id, Events.is_deleted == False, Events.lifecycle.in_(HELD), _end_expr() < now,  # noqa: E712
    ]
    past = await _rows(session, _event_select().where(*past_where).order_by(Events.event_datetime.desc(), Events.id.desc()).limit(6))
    detail["events_held"] = int(await session.scalar(select(func.count(Events.id)).where(*past_where)) or 0)
    going = await going_counts(session, [e.id for e in upcoming + past])

    posts = list((await session.scalars(
        select(ClubThreads).where(ClubThreads.club_id == club.id, ClubThreads.is_deleted == False)  # noqa: E712
        .order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(5)
    )).all())

    team = [_team_member(m, m.role) for m in await clubs_service.list_team(session, club.id)]
    partners = [dict(name=p.name, role=p.role, logo=_img(p.logo, "medium", "thumbnail", "original")) for p in await clubs_service.list_partners(session, club.id)]
    return dict(
        club=detail,
        upcoming=await cards(session, upcoming, now, going=going),
        past=await cards(session, past, now, going=going),
        threads=await threads._shape(session, posts, {club.id: club}, viewer_id),
        team=team, partners=partners,
    )


# ─── An event's page ─────────────────────────────────────────────────────


def _public_where(now: datetime) -> list:
    """An event anyone may open: on now or still to come, as the search shows
    it, or held and over, as a club page's past list shows it."""
    end = _end_expr()
    return [
        Events.is_deleted == False,  # noqa: E712
        *_club_visible(),
        or_(and_(Events.lifecycle.in_(SHOWN), end >= now), and_(Events.lifecycle.in_(HELD), end < now)),
    ]


def _ref_where(ref: str):
    """A slug, or the id the old site's addresses carried; None for a number too big to be an id."""
    if ref.isascii() and ref.isdigit():
        n = int(ref)
        return Events.id == n if n < 2**31 else None
    return Events.slug == ref


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt is not None else None


async def _interest_names_of(session, event_id: int) -> list[str]:
    rows = await session.scalars(
        select(Interests.name)
        .join(EventInterestsLink, EventInterestsLink.interest_id == Interests.id)
        .where(EventInterestsLink.event_id == event_id, EventInterestsLink.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
        .order_by(EventInterestsLink.id)
    )
    out: list[str] = []
    for name in rows.all():
        if name not in out:
            out.append(name)
    return out


async def _event_team(session, e: Events) -> tuple[list[dict], str]:
    """The organisers the event names; without them, the first of the club's team."""
    named = await organisers.rows_for(session, e)
    if named:
        return [_team_member(m, o.role or m.role) for o, m in named], "event"
    members = (await clubs_service.list_team(session, e.club_id))[:CLUB_TEAM_ON_EVENT]
    return [_team_member(m, m.role) for m in members], ("club" if members else "none")


async def event_page(session, ref: str, now: Optional[datetime] = None) -> dict:
    """Everything the public event page shows. Nothing about who registered
    beyond the count, and never the online joining link."""
    now = now or now_utc()
    key = _ref_where(ref)
    e = None
    if key is not None:
        e = await session.scalar(_event_select().options(selectinload(Events.speakers)).where(key, *_public_where(now)))
    if e is None:
        raise CustomHTTPException(404, message="Event not found")
    club, cat = e.club, e.category
    going = (await going_counts(session, [e.id])).get(e.id, 0)
    poster = _img(e.poster, "original", "large", "medium", "thumbnail")
    # An online event has no venue to show; a link left in the map field may well be the meeting itself.
    venue = (None, None, None) if e.is_online else (e.location_link, e.lat, e.lng)
    event = dict(
        id=e.id, slug=e.slug, name=e.name, about=e.about,
        poster=poster, poster_thumb=_img(e.poster, "medium", "thumbnail") or poster,
        starts_at=_aware(e.event_datetime).isoformat(), ends_at=_ends_at(e).isoformat(),
        duration_hours=float(e.duration) if e.duration is not None else None, timezone=EVENT_TIMEZONE,
        is_online=bool(e.is_online), location_name=e.location_name,
        location_link=venue[0], lat=venue[1], lng=venue[2],
        has_fee=bool(e.has_fee), fee=float(e.reg_fee) if e.has_fee and e.reg_fee is not None else None,
        has_prize=bool(e.has_prize), prize_amount=float(e.prize_amount) if e.has_prize and e.prize_amount is not None else None,
        capacity=e.max_participants, going=going,
        seats_left=max(e.max_participants - going, 0) if e.max_participants else None,
        reg_opens_at=_iso(e.reg_startdate), reg_closes_at=_iso(e.reg_enddate),
        status=status_of(e, now, going), registration_open=registration_open(e, going, now),
        category=dict(id=cat.id, name=cat.name) if cat is not None else None,
        interests=await _interest_names_of(session, e.id),
        guidelines=e.event_guidelines,
        contact=dict(name=e.contact_name, email=e.contact_email, phone=e.contact_phone),
        url=public_urls.event_url(e.slug),
    )
    speakers = [
        dict(id=s.id, name=s.name, designation=s.designation, photo=_img(s.photo, "medium", "thumbnail", "original"), bio=s.bio, link=s.link)
        for s in e.speakers if not s.is_deleted
    ]
    team, team_source = await _event_team(session, e)
    more = await _rows(session, filtered(now, club_id=club.id).where(Events.id != e.id).limit(ALSO_ON))
    similar = await _rows(session, filtered(now, category=e.category_id).where(Events.club_id != club.id).limit(ALSO_ON))
    shown_going = await going_counts(session, [x.id for x in more + similar])
    return dict(
        event=event,
        club=dict(
            id=club.id, name=club.name, slug=club.slug, logo=_img(club.logo, "thumbnail", "medium", "original"),
            tagline=club.tagline, followers=(await follower_counts(session, [club.id])).get(club.id, 0), location_name=club.location_name,
        ),
        speakers=speakers,
        agenda=dict(timezone=EVENT_TIMEZONE, items=await agenda.list_items(session, e.id)),
        team=team, team_source=team_source,
        more_from_club=await cards(session, more, now, going=shown_going),
        similar=await cards(session, similar, now, going=shown_going),
    )


# ─── Sitemap ─────────────────────────────────────────────────────────────


async def sitemap(session, now: Optional[datetime] = None) -> dict:
    """Every page a search engine should know: events still to come or held in
    the last year, and every club, each with when it last changed."""
    now = now or now_utc()
    events = (await session.execute(
        select(Events.slug, Events.updated_at)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(*_public_where(now), Events.event_datetime >= now - SITEMAP_PAST)
        .order_by(Events.event_datetime.desc(), Events.id.desc())
        .limit(SITEMAP_MAX)
    )).all()
    clubs = (await session.execute(
        select(Clubs.slug, Clubs.updated_at).where(*_club_visible()).order_by(Clubs.id).limit(SITEMAP_MAX)
    )).all()
    return dict(
        events=[dict(slug=slug, updated_at=_iso(at)) for slug, at in events],
        clubs=[dict(slug=slug, updated_at=_iso(at)) for slug, at in clubs],
    )


# ─── Threads ─────────────────────────────────────────────────────────────


def _threads_base():
    return (
        select(ClubThreads)
        .join(Clubs, Clubs.id == ClubThreads.club_id)
        .where(ClubThreads.is_deleted == False, *_club_visible())  # noqa: E712
    )


async def threads_feed(session, viewer_id: Optional[int], offset: int, limit: int) -> dict:
    """The newest posts from every verified club. Reading them here counts no views."""
    base = _threads_base()
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = list((await session.scalars(base.order_by(ClubThreads.created_at.desc(), ClubThreads.id).offset(offset).limit(limit))).all())
    by_id = await threads._clubs_by_id(session, {t.club_id for t in rows})
    return dict(items=await threads._shape(session, rows, by_id, viewer_id), total=total or 0, limit=limit, offset=offset)


# ─── Home ────────────────────────────────────────────────────────────────


async def stats(session, upcoming: int) -> dict:
    clubs = await session.scalar(select(func.count(Clubs.id)).where(*_club_visible()))
    place = func.lower(func.trim(Events.location_name))
    cities = await session.scalar(
        select(func.count(func.distinct(place)))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Events.is_deleted == False, Events.lifecycle.in_(HELD), *_club_visible(),  # noqa: E712
            func.coalesce(Events.is_online, False) == False,  # noqa: E712
            place.isnot(None), place != "",
        )
    )
    return dict(upcoming_events=upcoming, clubs=int(clubs or 0), cities=int(cities or 0))


async def home(session, where: dict, viewer_id: Optional[int], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    rows = await _rows(session, filtered(now))
    going = await going_counts(session, [e.id for e in rows])
    here = _here(where)

    near: list[Events] = []
    if here is not None:
        near = [e for e in rows if not e.is_online and e.lat is not None and e.lng is not None and _event_km(e, here) <= NEAR_KM][:12]
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

    posts = list((await session.scalars(_threads_base().order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(6))).all())
    by_id = await threads._clubs_by_id(session, {t.club_id for t in posts})

    def out(es):
        return [card(e, now, going.get(e.id, 0), _event_km(e, here)) for e in es]

    return dict(
        where=where,
        near=out(near), this_week=out(this_week), online=out(online), popular=out(popular),
        categories=await categories(session, now, only_upcoming=True),
        clubs=await club_cards(session, club_rows[:8], now, distances=dist, followers=followers),
        threads=await threads._shape(session, posts, by_id, viewer_id),
        stats=await stats(session, len(rows)),
    )
