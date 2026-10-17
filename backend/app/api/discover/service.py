"""The public discover site: what is on, near the visitor or anywhere, and
the clubs behind it.

What the public sees: an event that is published or live, not deleted, not
over, by a club that is verified and not deleted; a club that is verified
and not deleted. Once over, an event the club held still shows on the club
page's past list, on its own page and in the sitemap, and nowhere else.

Every list is filtered, ordered and paged by the database, so a request
reads a page of rows plus what the indexes walk, whatever the number of
events and clubs:

- "Not over yet" and the order by date run on a partial index of what can be
  shown (ix_events_shown_start).
- Words are case-blind substrings (ILIKE) of the event's name or place, its
  club's or its category's name, tested on what is on. A search that finds
  nothing tries again by trigram word similarity, which forgives a typo,
  through pg_trgm indexes.
- Distance is the same haversine as geo.km, in SQL, behind a latitude/
  longitude box (geo.bbox) that an index can find.
- "Popular" sorts by events.going_count and clubs.follower_count, which
  triggers keep exact; what a card shows is counted from the rows for the page.
- The front page's sections each take a LIMIT, and the parts that are the
  same for every visitor are kept for a minute (cache.py). "Near you" is
  kept per quarter-degree cell as a short list that is provably enough for
  any point in the cell, and finished for the visitor's own point, so it is
  exactly what a fresh query would give.
"""
from __future__ import annotations

import heapq
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import orjson

from sqlalchemy import and_, bindparam, case, exists, func, or_, select, text, true, union
from sqlalchemy.orm import aliased, contains_eager, selectinload

from app.api.agenda import service as agenda
from app.api.closeout.models import EventStatus
from app.api.clubs import service as clubs_service
from app.api.clubs.events_list import registration_open
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubSocials, ClubTeamMembers
from app.api.discover import cache, geo
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventCategories, EventInterestsLink, Events, ends_at_sql
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
NEAR_COUNT = 12
HOME_LIST = 8
HOME_CLUBS = 8
HOME_THREADS = 6
CLUB_TEAM_ON_EVENT = 8
ALSO_ON = 4
SITEMAP_MAX = 5000
SITEMAP_PAST = timedelta(days=365)
WHENS = ("any", "today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month")
# Rounding slack when a cached pool is cut by distance: far below a metre.
_SLACK_KM = 1e-6


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _in(column, values):
    """`column IN (…)` with the values written into the SQL rather than bound.

    asyncpg prepares every statement, and after a few runs Postgres may plan
    it once for any parameters; a partial index whose predicate names these
    values (status IN ('published', 'live')) is only usable when the query
    carries them as constants."""
    return column.in_(bindparam(None, list(values), expanding=True, literal_execute=True))


def _not_draft():
    return Events.lifecycle != bindparam(None, EventStatus.draft, type_=Events.lifecycle.type, literal_execute=True)


def _end_expr():
    return ends_at_sql(Events)


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
        _in(Events.lifecycle, SHOWN),
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


def _event_words(q: str):
    """Words anywhere in the event's name or place, its club's name or its
    category's name: case-blind substrings, as always.

    Tested on what is on: the club and category sides are hashed lookups of
    the few ids whose names match. Measured against the alternatives on
    37,000 events (RESULTS.md): finding matches through trigram indexes over
    every event, past ones included, costs more than this filter for any
    word common enough to matter, because the events still to come are
    already a small, indexed set."""
    p = _like(q)
    return or_(
        Events.name.ilike(p, escape="\\"),
        Events.location_name.ilike(p, escape="\\"),
        Events.club_id.in_(select(Clubs.id).where(Clubs.name.ilike(p, escape="\\"), Clubs.is_deleted == False)),  # noqa: E712
        Events.category_id.in_(select(EventCategories.id).where(EventCategories.name.ilike(p, escape="\\"), EventCategories.is_deleted == False)),  # noqa: E712
    )


def _matching(event_match, club_match, category_match):
    """`events.id IN (…)`: the events (of any date; the caller filters)
    whose own name or place, whose club's name, or whose category's name
    matches. One arm per table, each through that table's trigram index, and
    a semi-join the planner can drive from the matches."""
    e1, e2, e3 = (aliased(Events, name=f"words_e{i}") for i in (1, 2, 3))
    club = aliased(Clubs, name="words_club")
    cat = aliased(EventCategories, name="words_category")
    return Events.id.in_(union(
        select(e1.id).where(event_match(e1), e1.is_deleted == False),  # noqa: E712
        select(e2.id).join(club, club.id == e2.club_id).where(club_match(club), club.is_deleted == False, e2.is_deleted == False),  # noqa: E712
        select(e3.id).join(cat, cat.id == e3.category_id).where(category_match(cat), cat.is_deleted == False, e3.is_deleted == False),  # noqa: E712
    ))


# How alike a word must be to count as a typo of it (pg_trgm word similarity;
# its default, 0.6, misses "robtics" for "robotics" at 0.55, while 0.5 still
# leaves out "Robots" at 0.38).
FUZZY_THRESHOLD = 0.5


def _event_words_fuzzy(q: str):
    """The same places, by trigram word similarity (pg_trgm's `%>`): for when
    a typo found nothing. Here the trigram indexes find the few candidates;
    as a filter, `%>` costs several microseconds a row."""
    q = q.strip()
    return _matching(
        lambda m: or_(m.name.op("%>")(q), m.location_name.op("%>")(q)),
        lambda m: m.name.op("%>")(q),
        lambda m: m.name.op("%>")(q),
    )


def _fuzzy_worthwhile(q: Optional[str]) -> bool:
    """A trigram needs three characters; fewer would match everything or nothing."""
    return bool(q and len(q.strip()) >= 3)


async def _fuzzy(session) -> None:
    """Use FUZZY_THRESHOLD for `%>` until this request's transaction ends."""
    await session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {FUZZY_THRESHOLD}"))


def _relevance(q: str):
    """How well an event answers the words: the best of its name, its club's,
    its place's and its category's, in that order of weight."""
    q = q.strip()
    return func.greatest(
        func.word_similarity(q, Events.name),
        0.8 * func.word_similarity(q, Clubs.name),
        0.6 * func.word_similarity(q, Events.location_name),
        0.5 * func.word_similarity(q, EventCategories.name),
    )


def _popularity():
    return Events.going_count + func.coalesce(Events.page_views, 0)


def _conditions(now: datetime, *, when: str = "any", category: Optional[int] = None, type_: str = "any", price: str = "any", club_id: Optional[int] = None) -> list:
    conds = _visible_where(now)
    start, end = window(when, now)
    if start is not None:
        conds.append(_end_expr() >= start)
    if end is not None:
        conds.append(Events.event_datetime < end)
    if category:
        conds.append(Events.category_id == category)
    if type_ == "online":
        conds.append(Events.is_online == True)  # noqa: E712
    elif type_ == "in_person":
        conds.append(func.coalesce(Events.is_online, False) == False)  # noqa: E712
    if price == "free":
        conds.append(func.coalesce(Events.has_fee, False) == False)  # noqa: E712
    elif price == "paid":
        conds.append(Events.has_fee == True)  # noqa: E712
    if club_id is not None:
        conds.append(Events.club_id == club_id)
    return conds


def filtered(now: datetime, *, when: str = "any", category: Optional[int] = None, type_: str = "any", price: str = "any", q: Optional[str] = None, club_id: Optional[int] = None):
    conds = _conditions(now, when=when, category=category, type_=type_, price=price, club_id=club_id)
    if q and q.strip():
        conds.append(_event_words(q))
    return _event_select().where(*conds).order_by(Events.event_datetime, Events.id)


async def _rows(session, stmt) -> list[Events]:
    return list((await session.scalars(stmt)).unique().all())


async def going_counts(session, ids: list[int]) -> dict[int, int]:
    """How many are going to each of these events: the trigger-kept counter
    (exactly the registrations not deleted), read as plain columns so a copy of
    the event already in this session cannot answer with an older number. A
    primary-key read per event, where counting the rows of a popular event
    read thousands."""
    if not ids:
        return {}
    rows = await session.execute(select(Events.id, Events.going_count).where(Events.id.in_(ids)))
    return {k: int(v) for k, v in rows.all() if v}


def _event_km(e: Events, here) -> Optional[float]:
    if here is None or e.is_online or e.lat is None or e.lng is None:
        return None
    return geo.km(here[0], here[1], e.lat, e.lng)


def _in_person_point():
    return and_(func.coalesce(Events.is_online, False) == False, Events.lat.isnot(None), Events.lng.isnot(None))  # noqa: E712


def _within(here, radius_km: float):
    """Online events are anywhere, and an in-person one without a point cannot
    be ruled out; the rest must be inside the radius. The box goes first so the
    exact distance is only worked out for rows inside it."""
    box = geo.bbox(here[0], here[1], radius_km)
    return or_(
        Events.is_online == True,  # noqa: E712
        Events.lat.is_(None),
        Events.lng.is_(None),
        and_(geo.in_box_sql(box, Events.lat, Events.lng), geo.km_sql(here[0], here[1], Events.lat, Events.lng) <= radius_km),
    )


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


def _card_km(c: dict, here) -> Optional[float]:
    """A card's distance from the visitor, from what the card carries (as _event_km)."""
    if here is None or c["is_online"] or c["lat"] is None or c["lng"] is None:
        return None
    return round(geo.km(here[0], here[1], c["lat"], c["lng"]), 1)


def _placed(cards_: list[dict], here) -> list[dict]:
    """Copies of shared cards with the visitor's distance on them."""
    return [dict(c, distance_km=_card_km(c, here)) for c in cards_]


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


async def _categories(session, now: datetime, only_upcoming: bool) -> list[dict]:
    counts = await category_counts(session, now)
    rows = (await session.scalars(select(EventCategories).where(EventCategories.is_deleted == False))).all()  # noqa: E712
    out = [
        dict(id=c.id, name=c.name, icon=c.icon, upcoming=counts.get(c.id, 0))
        for c in rows
        if counts.get(c.id) or (c.created_by_id is None and not only_upcoming)
    ]
    out.sort(key=lambda c: (-c["upcoming"], c["name"].lower(), c["id"]))
    return out


async def categories(session, now: datetime, *, only_upcoming: bool = False) -> list[dict]:
    """Every category the product ships plus any a club added that has something on; most upcoming first.
    The same for everyone, so kept for the minute."""
    got = await cache.shared.remember(("categories", only_upcoming, cache.minute(now)), lambda: _categories(session, now, only_upcoming))
    return [dict(c) for c in got]


async def _count(session, stmt) -> int:
    return int(await session.scalar(stmt) or 0)


def _event_count(conds: list):
    return select(func.count(Events.id)).select_from(Events).join(Clubs, Clubs.id == Events.club_id).where(*conds)


async def _page(session, conds: list, keys: list[tuple], offset: int, limit: int, *, with_category: bool = False) -> list[Events]:
    """One page of events in the order of `keys` ((expression, descending)).

    The sort runs on narrow rows (the id and the keys), and only the page's
    rows are then read in full with their club and category: sorting ten
    thousand whole rows to keep twenty-four was most of the work."""
    if limit <= 0:
        return []
    narrow = select(Events.id.label("id"), *[k.label(f"k{i}") for i, (k, _) in enumerate(keys)]).select_from(Events).join(Clubs, Clubs.id == Events.club_id)
    if with_category:
        narrow = narrow.outerjoin(EventCategories, EventCategories.id == Events.category_id)
    page = (
        narrow.where(*conds)
        .order_by(*[k.desc() if d else k for k, d in keys])
        .offset(offset).limit(limit)
        .subquery("page")
    )
    order = [page.c[f"k{i}"].desc() if d else page.c[f"k{i}"] for i, (_, d) in enumerate(keys)]
    return await _rows(session, _event_select().join(page, page.c.id == Events.id).order_by(*order))


async def search_events(
    session, where: dict, *, q: Optional[str], when: str, category: Optional[int], type_: str, price: str,
    radius_km: float, sort: str, offset: int, limit: int, now: Optional[datetime] = None,
) -> dict:
    now = now or now_utc()
    here = _here(where)
    conds = _conditions(now, when=when, category=category, type_=type_, price=price)
    radius = here is not None and type_ != "online" and radius_km > 0
    if radius:
        conds.append(_within(here, radius_km))
    words = bool(q and q.strip())
    total = await _count(session, _event_count(conds + ([_event_words(q)] if words else [])))
    if words:
        if total == 0 and _fuzzy_worthwhile(q):
            # Nothing has these letters in this order: forgive a typo.
            await _fuzzy(session)
            conds.append(_event_words_fuzzy(q))
            total = await _count(session, _event_count(conds))
        else:
            conds.append(_event_words(q))

    by_date = [(Events.event_datetime, False), (Events.id, False)]
    page: list[Events] = []
    if total <= offset:
        pass
    elif sort == "distance" and here is not None:
        # Nearest first, then everything without a distance (online, or no
        # point) by date. The two parts are paged as one list; the first can
        # use the point index, inside the radius's box.
        placed = conds + [_in_person_point()]
        if radius:
            placed.append(geo.in_box_sql(geo.bbox(here[0], here[1], radius_km), Events.lat, Events.lng))
        unplaced = conds + [or_(Events.is_online == True, Events.lat.is_(None), Events.lng.is_(None))]  # noqa: E712
        n_placed = await _count(session, _event_count(placed))
        if offset < n_placed:
            page = await _page(session, placed, [(geo.km_sql(here[0], here[1], Events.lat, Events.lng), False), *by_date], offset, limit)
        if offset + limit > n_placed:
            page += await _page(session, unplaced, by_date, max(0, offset - n_placed), limit - len(page))
    elif sort == "popular":
        page = await _page(session, conds, [(_popularity(), True), *by_date], offset, limit)
    elif sort == "relevance" and words:
        page = await _page(session, conds, [(_relevance(q), True), *by_date], offset, limit, with_category=True)
    else:
        page = await _page(session, conds, by_date, offset, limit)
    going = await going_counts(session, [e.id for e in page])
    cats = [dict(id=c["id"], name=c["name"], upcoming=c["upcoming"]) for c in await categories(session, now, only_upcoming=True)]
    return dict(
        items=[card(e, now, going.get(e.id, 0), _event_km(e, here)) for e in page],
        total=total, offset=offset, limit=limit, where=where, categories=cats,
    )


# ─── Clubs ───────────────────────────────────────────────────────────────


def _club_visible():
    return [Clubs.is_verified == True, Clubs.is_deleted == False]  # noqa: E712


def _club_point():
    """Where a club is, as SQL: its own point, else the middle of its
    in-person events' points. The events are only read for a club without
    its own point (the lateral's condition on the club is checked first)."""
    events_point = (
        select(func.avg(Events.lat).label("lat"), func.avg(Events.lng).label("lng"))
        .where(
            Events.club_id == Clubs.id, Events.lat.isnot(None), Events.lng.isnot(None),
            Events.is_deleted == False, _not_draft(),  # noqa: E712
            func.coalesce(Events.is_online, False) == False,  # noqa: E712
            or_(Clubs.lat.is_(None), Clubs.lng.is_(None)),
        )
        .lateral("club_events_point")
    )
    own = and_(Clubs.lat.isnot(None), Clubs.lng.isnot(None))
    lat = case((own, Clubs.lat), else_=events_point.c.lat)
    lng = case((own, Clubs.lng), else_=events_point.c.lng)
    return events_point, lat, lng


async def follower_counts(session, ids: list[int]) -> dict[int, int]:
    """How many follow each of these clubs: the trigger-kept counter (follows
    that are following and not deleted), read as plain columns (see going_counts)."""
    if not ids:
        return {}
    rows = await session.execute(select(Clubs.id, Clubs.follower_count).where(Clubs.id.in_(ids)))
    return {k: int(v) for k, v in rows.all() if v}


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


def _club_words(q: str):
    p = _like(q)
    return or_(Clubs.name.ilike(p, escape="\\"), Clubs.tagline.ilike(p, escape="\\"), Clubs.location_name.ilike(p, escape="\\"))


def _club_words_fuzzy(q: str):
    q = q.strip()
    return or_(Clubs.name.op("%>")(q), Clubs.tagline.op("%>")(q), Clubs.location_name.op("%>")(q))


def _club_interest(interest: int):
    return exists(
        select(ClubInterestsLink.id).where(
            ClubInterestsLink.club_id == Clubs.id, ClubInterestsLink.interest_id == interest, ClubInterestsLink.is_deleted == False,  # noqa: E712
        )
    )


def _popular_clubs_order():
    """Most followed first, then by name as Python orders it (code points), then id."""
    return [Clubs.follower_count.desc(), func.lower(Clubs.name).collate("C"), Clubs.id]


async def _top_interests(session, limit: int) -> list[dict]:
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


async def top_interests(session, limit: int = 16, now: Optional[datetime] = None) -> list[dict]:
    now = now or now_utc()
    got = await cache.shared.remember(("interests", limit, cache.minute(now)), lambda: _top_interests(session, limit))
    return [dict(i) for i in got]


async def search_clubs(
    session, where: dict, *, q: Optional[str], interest: Optional[int], radius_km: float, sort: str,
    offset: int, limit: int, now: Optional[datetime] = None,
) -> dict:
    now = now or now_utc()
    here = _here(where)
    conds = list(_club_visible())
    if interest:
        conds.append(_club_interest(interest))
    joined = None
    dist = lat = lng = None
    if here is not None:
        joined, lat, lng = _club_point()
        dist = case((and_(lat.isnot(None), lng.isnot(None)), geo.km_sql(here[0], here[1], lat, lng)), else_=None)
        if radius_km > 0:
            conds.append(or_(dist.is_(None), dist <= radius_km))

    def counted(extra: list):
        stmt = select(func.count(Clubs.id)).select_from(Clubs)
        if joined is not None:
            stmt = stmt.outerjoin(joined, true())
        return stmt.where(*conds, *extra)

    words = bool(q and q.strip())
    total = await _count(session, counted([_club_words(q)] if words else []))
    if words:
        if total == 0 and _fuzzy_worthwhile(q):
            await _fuzzy(session)
            conds.append(_club_words_fuzzy(q))
            total = await _count(session, counted([]))
        else:
            conds.append(_club_words(q))

    if sort == "nearby" and here is not None:
        order = [dist.is_(None), dist, Clubs.follower_count.desc(), Clubs.id]
    elif sort == "newest":
        order = [Clubs.created_at.desc(), Clubs.id.desc()]
    else:
        order = _popular_clubs_order()
    if here is not None:
        stmt = select(Clubs, lat, lng).outerjoin(joined, true())
    else:
        stmt = select(Clubs)
    rows = (await session.execute(stmt.where(*conds).order_by(*order).offset(offset).limit(limit))).all() if total > offset else []
    page = [r[0] for r in rows]
    distances = {}
    if here is not None:
        for c, la, ln in rows:
            distances[c.id] = geo.km(here[0], here[1], float(la), float(ln)) if la is not None and ln is not None else None
    return dict(
        items=await club_cards(session, page, now, distances=distances),
        total=total, offset=offset, limit=limit, where=where, interests=await top_interests(session, now=now),
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
        Events.club_id == club.id, Events.is_deleted == False, _in(Events.lifecycle, HELD), _end_expr() < now,  # noqa: E712
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
        or_(and_(_in(Events.lifecycle, SHOWN), end >= now), and_(_in(Events.lifecycle, HELD), end < now)),
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


async def _sitemap(session, now: datetime) -> dict:
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


async def sitemap(session, now: Optional[datetime] = None) -> dict:
    """Every page a search engine should know: events still to come or held in
    the last year, and every club, each with when it last changed. The same
    for every crawler, so kept for the minute."""
    now = now or now_utc()
    return await cache.shared.remember(("sitemap", cache.minute(now)), lambda: _sitemap(session, now))


async def sitemap_json(session, now: Optional[datetime] = None) -> bytes:
    """The sitemap as the response body, made once a minute: building and
    checking 6,500 entries for every request cost more than reading them."""
    from app.api.discover.schemas import Sitemap

    now = now or now_utc()

    async def build() -> bytes:
        return orjson.dumps(Sitemap(**await sitemap(session, now)).model_dump(mode="json"))

    return await cache.shared.remember(("sitemap-json", cache.minute(now)), build)


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


async def _for_viewer(session, shared_threads: list[dict], viewer_id: Optional[int]) -> list[dict]:
    """Shared, anonymous post cards, with what this reader liked and voted for."""
    if viewer_id is None or not shared_threads:
        return [dict(t) for t in shared_threads]
    import uuid

    liked, voted = await threads._mine(session, [uuid.UUID(t["id"]) for t in shared_threads], viewer_id)
    out = []
    for t in shared_threads:
        tid = uuid.UUID(t["id"])
        poll = t["poll"]
        if poll is not None:
            option = voted.get(tid)
            poll = dict(poll, voted_option_id=str(option) if option else None)
        out.append(dict(t, liked=tid in liked, poll=poll))
    return out


# ─── Home ────────────────────────────────────────────────────────────────


CITIES_EVERY = 10  # minutes


async def _cities(session) -> int:
    place = func.lower(func.trim(Events.location_name))
    n = await session.scalar(
        select(func.count(func.distinct(place)))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(
            Events.is_deleted == False, _in(Events.lifecycle, HELD), *_club_visible(),  # noqa: E712
            func.coalesce(Events.is_online, False) == False,  # noqa: E712
            place.isnot(None), place != "",
        )
    )
    return int(n or 0)


async def stats(session, upcoming: int, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    clubs = await session.scalar(select(func.count(Clubs.id)).where(*_club_visible()))
    # Every place an event was ever held: a distinct count over all of them,
    # and a number that moves slowly, so it is worked out every ten minutes.
    cities = await cache.shared.remember(("cities", cache.minute(now) // CITIES_EVERY), lambda: _cities(session))
    return dict(upcoming_events=upcoming, clubs=int(clubs or 0), cities=cities)


async def _home_shared(session, now: datetime) -> dict:
    """The front page as anyone sees it: nothing about where they are or who they are."""
    visible = _visible_where(now)
    by_date = [(Events.event_datetime, False), (Events.id, False)]
    this_week = await _page(session, visible + [Events.event_datetime < now + timedelta(days=7)], by_date, 0, HOME_LIST)
    online = await _page(session, visible + [Events.is_online == True], by_date, 0, HOME_LIST)  # noqa: E712
    popular = await _page(session, visible, [(_popularity(), True), *by_date], 0, HOME_LIST)
    going = await going_counts(session, list({e.id for e in this_week + online + popular}))
    upcoming = await _count(session, _event_count(visible))

    top_clubs = list((await session.scalars(select(Clubs).where(*_club_visible()).order_by(*_popular_clubs_order()).limit(HOME_CLUBS))).all())

    posts = list((await session.scalars(_threads_base().order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(HOME_THREADS))).all())
    by_id = await threads._clubs_by_id(session, {t.club_id for t in posts})

    def out(es):
        return [card(e, now, going.get(e.id, 0), None) for e in es]

    return dict(
        this_week=out(this_week), online=out(online), popular=out(popular),
        categories=await categories(session, now, only_upcoming=True),
        clubs=await club_cards(session, top_clubs, now),
        threads=await threads._shape(session, posts, by_id, None),
        stats=await stats(session, upcoming, now),
    )


async def _near_pool(session, now: datetime, centre: tuple[float, float], reach: float) -> dict:
    """In-person events, soonest first, enough to answer "near you" for any
    point within `reach` km of `centre`.

    An event more than NEAR_KM + reach from the centre is more than NEAR_KM
    from every such point, so it is left out. One within NEAR_KM - reach is
    near every such point, so once NEAR_COUNT of those have been seen, nothing
    later can make anyone's first NEAR_COUNT: the list stops there. What is
    kept is (id, lat, lng); the cards are made when a visitor needs them."""
    far = NEAR_KM + reach + _SLACK_KM
    sure = NEAR_KM - reach - _SLACK_KM
    rows = await session.execute(
        select(Events.id, Events.lat, Events.lng)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(*_visible_where(now), _in_person_point(), geo.in_box_sql(geo.bbox(centre[0], centre[1], far), Events.lat, Events.lng))
        .order_by(Events.event_datetime, Events.id)
    )
    pool: list[tuple[int, float, float]] = []
    certain = 0
    for event_id, lat, lng in rows.all():
        d = geo.km(centre[0], centre[1], lat, lng)
        if d > far:
            continue
        pool.append((event_id, lat, lng))
        if d <= sure:
            certain += 1
            if certain >= NEAR_COUNT:
                break
    return dict(events=pool, cards={})


async def _near(session, now: datetime, pool: dict, here: tuple[float, float]) -> list[dict]:
    picked = [i for i, lat, lng in pool["events"] if geo.km(here[0], here[1], lat, lng) <= NEAR_KM][:NEAR_COUNT]
    missing = [i for i in picked if i not in pool["cards"]]
    if missing:
        rows = await _rows(session, _event_select().where(Events.id.in_(missing)))
        going = await going_counts(session, missing)
        for e in rows:
            pool["cards"][e.id] = card(e, now, going.get(e.id, 0), None)
    return _placed([pool["cards"][i] for i in picked if i in pool["cards"]], here)


async def _club_places(session) -> list[tuple[int, Optional[float], Optional[float], int]]:
    """Every listed club's point (its own or its events') and follower count."""
    joined, lat, lng = _club_point()
    rows = await session.execute(select(Clubs.id, lat, lng, Clubs.follower_count).outerjoin(joined, true()).where(*_club_visible()))
    return [(i, float(la) if la is not None else None, float(ln) if ln is not None else None, n) for i, la, ln, n in rows.all()]


async def _club_pool(session, now: datetime, centre: tuple[float, float], reach: float) -> dict:
    """Clubs enough to give the HOME_CLUBS nearest for any point within
    `reach` km of `centre`: (id, lat, lng, followers), cards made as needed.

    If R is the distance from the centre to its HOME_CLUBS-th nearest club, a
    point P in reach has that many clubs within R + reach, so its own
    nearest are all within R + reach of P, and so within R + 2·reach of the
    centre. Clubs with no point at all come after every club with one."""
    places = await cache.shared.remember(("club-places", cache.minute(now)), lambda: _club_places(session))
    placed = [(geo.km(centre[0], centre[1], la, ln), i) for i, la, ln, _ in places if la is not None and ln is not None]
    if len(placed) >= HOME_CLUBS:
        bound = heapq.nsmallest(HOME_CLUBS, (d for d, _ in placed))[-1] + 2 * reach + _SLACK_KM
        keep = {i for d, i in placed if d <= bound}
    else:
        keep = {i for _, i in placed}
        unplaced = sorted(((-n, i) for i, la, ln, n in places if la is None or ln is None))[:HOME_CLUBS]
        keep |= {i for _, i in unplaced}
    return dict(clubs=[p for p in places if p[0] in keep], cards={})


async def _nearest_clubs(session, now: datetime, pool: dict, here: tuple[float, float]) -> list[dict]:
    ranked = []
    for i, la, ln, n in pool["clubs"]:
        d = geo.km(here[0], here[1], la, ln) if la is not None and ln is not None else None
        ranked.append(((d is None, d or 0.0, -n, i), i, d))
    picked = heapq.nsmallest(HOME_CLUBS, ranked)
    missing = [i for _, i, _ in picked if i not in pool["cards"]]
    if missing:
        rows = list((await session.scalars(select(Clubs).where(Clubs.id.in_(missing)))).all())
        for c in await club_cards(session, rows, now):
            pool["cards"][c["id"]] = c
    return [dict(pool["cards"][i], distance_km=round(d, 1) if d is not None else None) for _, i, d in picked if i in pool["cards"]]


async def home(session, where: dict, viewer_id: Optional[int], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    here = _here(where)
    minute = cache.minute(now)
    shared = await cache.shared.remember(("home", minute), lambda: _home_shared(session, now))

    near: list[dict] = []
    clubs = [dict(c) for c in shared["clubs"]]
    if here is not None:
        cell = geo.cell(*here)
        if geo.cell_is_cacheable(cell):
            centre, reach = geo.cell_centre(cell), geo.cell_reach(cell)
            pool = await cache.shared.remember(("near", cell, minute), lambda: _near_pool(session, now, centre, reach))
            club_pool = await cache.shared.remember(("home-clubs", cell, minute), lambda: _club_pool(session, now, centre, reach))
        else:  # by a pole: worked out for this point alone
            pool = await _near_pool(session, now, here, 0.0)
            club_pool = await _club_pool(session, now, here, 0.0)
        near = await _near(session, now, pool, here)
        clubs = await _nearest_clubs(session, now, club_pool, here)

    return dict(
        where=where,
        near=near,
        this_week=_placed(shared["this_week"], here),
        online=_placed(shared["online"], here),
        popular=_placed(shared["popular"], here),
        categories=[dict(c) for c in shared["categories"]],
        clubs=clubs,
        threads=await _for_viewer(session, shared["threads"], viewer_id),
        stats=dict(shared["stats"]),
    )

