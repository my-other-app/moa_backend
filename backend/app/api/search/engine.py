"""Which engine answers a search's words, and asking Meilisearch.

A list with words asks Meilisearch when it is configured, the breaker is
closed (app.core.search) and the indexes are built for this version
(sync.is_built, read at most every READY_SECONDS); anything else, and any
failure on the way, is answered by Postgres, as before. Only the words go to
Meilisearch: a list without words is already an index walk in Postgres.

What Meilisearch decides is which rows match the words, and their order.
Every page it names is read back from Postgres under the list's own
conditions -- the same SQL the Postgres path uses (visibility, filters, the
exact radius) -- so a document a few seconds stale can never show what the
public may not see; at worst a page is a row short. The filters sent to
Meilisearch mirror that SQL rule for rule (`_event_filter`), and the totals
are Meilisearch's.

Paging: `offset` (at most 1,000, as on the Postgres path) and the cursor.
Meilisearch's cursors carry an offset ("<list>:<order>@"), which the
Postgres path also takes, so a search that falls back mid-way carries on.
A Postgres cursor (a keyset) is continued by Postgres. Meilisearch pages end
at MAX_HITS (1,200) rows deep; past that, a list's total says "1,000+".
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.discover import cursor as cursors
from app.api.events.models import Events
from app.core import search
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

MAX_HITS = search.MAX_TOTAL_HITS
#: How long a process trusts what it last read about the build.
READY_SECONDS = 30.0

_ready: Optional[tuple[float, bool]] = None


def reset() -> None:
    global _ready
    _ready = None


async def usable(session) -> bool:
    """Whether a search should ask Meilisearch now."""
    global _ready
    if not search.configured() or search.breaker.state() == "open":
        return False
    now = time.monotonic()
    if _ready is None or now - _ready[0] >= READY_SECONDS:
        from app.api.search import sync

        try:
            _ready = (now, await sync.is_built(session))
        except Exception:  # noqa: BLE001 -- a search never fails for this
            logger.warning("could not read whether the search indexes are built", exc_info=True)
            return False
    return _ready[1]


# ─── Cursors ──────────────────────────────────────────────────────────────


def offset_cursor(kind: str, offset: int) -> str:
    return cursors.encode(kind + "@", [offset])


def offset_of(after: Optional[str], kind: str) -> Optional[int]:
    """The offset a Meilisearch cursor of `kind` carries; None for none (or a keyset cursor)."""
    if not after:
        return None
    try:
        got = cursors.decode(after, kind + "@", ["int"])
    except CustomHTTPException:
        return None
    return got[0] if got and got[0] >= 0 else None


# ─── Filters ──────────────────────────────────────────────────────────────


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICRO = timedelta(microseconds=1)


def us(dt: datetime) -> int:
    """An instant as whole microseconds since 1970: what the documents hold."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // _MICRO


def _geo_radius(here: tuple[float, float], radius_km: float) -> str:
    """Meilisearch's circle for ours. Its haversine (milli's
    distance_between_two_points, by the geoutils crate) uses an Earth of
    6,371,000 m and rounds to the millimetre; ours (geo.km) 6,371,008.8 m.
    So its distances are 1.4 parts in a million shorter, and the same
    radius plus a centimetre already holds every point ours does. The few
    it lets in beyond the edge (within centimetres of it) are dropped when
    the page is read back, by the exact km_sql."""
    metres = radius_km * 1000.0 + 0.01
    return f"_geoRadius({here[0]:.7f}, {here[1]:.7f}, {metres:.3f})"


async def topic_ref(session, topic: Optional[str]) -> Optional[int]:
    """The id a `topic` parameter names (as discover.service._topic_id): a
    number is an id; else the live topic with that slug; -1 for none."""
    from app.api.interests.models import InterestCategory

    ref = (topic or "").strip().lower()
    if not ref:
        return None
    if ref.isascii() and ref.isdigit() and int(ref) < 2**31:
        return int(ref)
    got = await session.scalar(select(InterestCategory.id).where(InterestCategory.slug == ref, InterestCategory.is_deleted == False))  # noqa: E712
    return got if got is not None else -1


def _event_filter(
    now: datetime, *, when: str, days, category, type_: str, price: str, topic_id: Optional[int], interest: Optional[int],
    here, radius_km: float,
) -> list[str]:
    """discover.service._conditions (+ _within), as a Meilisearch filter."""
    from app.api.discover import service as discover

    f = ["listed = true", "lifecycle IN [published, live]", f"ends_at >= {us(now)}"]
    if days is not None and (days[0] is not None or days[1] is not None):
        start, end = days
        if end is not None:
            f.append(f"starts_at < {us(end)}")
        if start is not None:
            f.append(f"(ends_at > {us(start)} OR starts_at >= {us(start)})")
    else:
        start, end = discover.window(when, now)
        if start is not None:
            f.append(f"ends_at >= {us(start)}")
        if end is not None:
            f.append(f"starts_at < {us(end)}")
    if category:
        f.append(f"category_id = {int(category)}")
    if interest is not None:
        f.append(f"interest_ids = {int(interest)}")
    elif topic_id is not None:
        f.append(f"topic_ids = {int(topic_id)}")
    if type_ == "online":
        f.append("is_online = true")
    elif type_ == "in_person":
        f.append("is_online = false")
    if price == "free":
        f.append("has_fee = false")
    elif price == "paid":
        f.append("has_fee = true")
    if here is not None and type_ != "online" and radius_km > 0:
        # In person with a point inside the radius, nothing else (discover.service._within).
        f += ["is_online = false", "has_point = true", _geo_radius(here, radius_km)]
    return f


def _and(parts: list[str]) -> str:
    return " AND ".join(parts)


_EVENT_SORTS = {
    "date": ["starts_at:asc", "id:asc"],
    "popular": ["popularity:desc", "starts_at:asc", "id:asc"],
    "newest": ["created_at:desc", "id:desc"],
    "relevance": None,
}


async def _ask(index_uid: str, q: str, filter_: str, sort: Optional[list[str]], offset: int, limit: int) -> tuple[list[int], int]:
    """(the page's ids, the hits in all, up to MAX_HITS): one round trip for both."""
    page = {"indexUid": index_uid, "q": q, "filter": filter_, "offset": offset, "limit": limit,
            "attributesToRetrieve": ["id"], "matchingStrategy": "all"}
    if sort:
        page["sort"] = sort
    count = {"indexUid": index_uid, "q": q, "filter": filter_, "page": 1, "hitsPerPage": 1,
             "attributesToRetrieve": ["id"], "matchingStrategy": "all"}
    got_page, got_count = await search.multi_search([page, count])
    return [int(h["id"]) for h in got_page.get("hits", [])], int(got_count.get("totalHits") or 0)


def _words_for(q: str) -> Optional[str]:
    """The words Meilisearch is asked for: the query's words as Postgres
    takes them (discover.service.words_of); None when there is no word of
    two letters or more to search by."""
    from app.api.discover.service import words_of

    words = words_of(q)
    if not words or all(len(w) < 2 for w in words):
        return None
    return " ".join(words)


# ─── Discover ─────────────────────────────────────────────────────────────


async def discover_events(
    session, where: dict, *, q: str, here, when: str, category, type_: str, price: str, radius_km: float, sort: str,
    offset: int, limit: int, now: datetime, since: datetime, days, topic: Optional[str], interest: Optional[int], after: Optional[str],
) -> Optional[dict]:
    """discover.service.search_events's answer, the words found by
    Meilisearch; None when Postgres should answer (not usable, a cursor of
    its own, a failure)."""
    from app.api.discover import service as discover
    from app.api.interests import topics as topic_refs

    words = _words_for(q)
    if words is None or not await usable(session):
        return None
    kind = f"events:{sort}"
    if after:
        start = offset_of(after, kind)
        if start is None:
            return None  # a keyset cursor: Postgres carries on
        offset = start
    if offset + limit > MAX_HITS:
        return None
    sort_by = [f"_geoPoint({here[0]:.7f}, {here[1]:.7f}):asc", "starts_at:asc", "id:asc"] if sort == "distance" else _EVENT_SORTS.get(sort)
    radius = here is not None and type_ != "online" and radius_km > 0
    topic_id = None if interest is not None else await topic_ref(session, topic)
    filt = _event_filter(now, when=when, days=days, category=category, type_=type_, price=price, topic_id=topic_id,
                         interest=interest, here=here, radius_km=radius_km)
    try:
        ids, hits = await _ask(search.index(search.EVENTS), words, _and(filt), sort_by, offset, limit + 1)
    except search.SearchError as e:
        logger.info("search fell back to Postgres: %s", e)
        return None
    more = len(ids) > limit
    ids = ids[:limit]
    # Read back under the Postgres path's own conditions, words aside.
    conds = discover._conditions(now, since, when=when, category=category, type_=type_, price=price, days=days, topic=topic, interest=interest)
    if radius:
        conds.append(discover._within(here, radius_km))
    rows = {e.id: e for e in await discover._rows(session, discover._event_select().where(Events.id.in_(ids), *conds))} if ids else {}
    page = [rows[i] for i in ids if i in rows]
    going, topics = await discover.card_extras(session, [e.id for e in page])
    cats = [dict(id=c["id"], name=c["name"], upcoming=c["upcoming"]) for c in await discover.categories(session, now, only_upcoming=True, since=since)]
    capped = hits > discover.COUNT_CAP
    return dict(
        items=[discover.card(e, now, going.get(e.id, 0), discover._event_km(e, here), topics.get(e.id)) for e in page],
        total=discover.COUNT_CAP if capped else hits, total_capped=capped, offset=offset, limit=limit, where=where,
        categories=cats, topics=await topic_refs.facet(session, now),
        next_cursor=offset_cursor(kind, offset + limit) if more and offset + limit < MAX_HITS else None,
    )


async def discover_clubs(
    session, where: dict, *, q: str, here, interest: Optional[int], topic: Optional[str], radius_km: float, sort: str,
    offset: int, limit: int, now: datetime, after: Optional[str],
) -> Optional[dict]:
    """discover.service.search_clubs's answer, the words found by Meilisearch; None for Postgres."""
    from app.api.clubs.models import ClubInterestsLink, Clubs
    from app.api.discover import geo, places, service as discover, upcoming

    words = _words_for(q)
    if words is None or not await usable(session):
        return None
    kind = f"clubs:{sort}"
    if after:
        start = offset_of(after, kind)
        if start is None:
            return None
        offset = start
    if offset + limit > MAX_HITS:
        return None
    f = ["listed = true"]
    if interest:
        f.append(f"interest_ids = {int(interest)}")
    else:
        topic_id = await topic_ref(session, topic)
        if topic_id is not None:
            f.append(f"topic_ids = {topic_id}")
    if here is not None and radius_km > 0:
        f.append(f"(has_place = false OR {_geo_radius(here, radius_km)})")
    if sort == "nearby" and here is not None:
        sort_by = [f"_geoPoint({here[0]:.7f}, {here[1]:.7f}):asc", "follower_count:desc", "id:asc"]
    elif sort == "newest":
        sort_by = ["created_at:desc", "id:desc"]
    else:
        sort_by = ["follower_count:desc", "name_sort:asc", "id:asc"]
    try:
        ids, hits = await _ask(search.index(search.CLUBS), words, _and(f), sort_by, offset, limit + 1)
    except search.SearchError as e:
        logger.info("club search fell back to Postgres: %s", e)
        return None
    more = len(ids) > limit
    ids = ids[:limit]
    conds = list(discover._club_visible())
    picked = discover.picks(ClubInterestsLink, ClubInterestsLink.club_id, Clubs.id, topic=topic, interest=interest or None)
    if picked is not None:
        conds.append(picked)
    if here is not None and radius_km > 0:
        conds.append(places.within(here, radius_km))
    got = (await session.execute(select(Clubs, Clubs.place_lat, Clubs.place_lng).where(Clubs.id.in_(ids), *conds))).all() if ids else []
    by_id = {c.id: (c, la, ln) for c, la, ln in got}
    rows = [by_id[i] for i in ids if i in by_id]
    distances = {}
    if here is not None:
        for c, la, ln in rows:
            distances[c.id] = geo.km(here[0], here[1], float(la), float(ln)) if la is not None and ln is not None else None
    capped = hits > discover.COUNT_CAP
    return dict(
        items=await discover.club_cards(session, [r[0] for r in rows], now, since=await upcoming.since(session, now), distances=distances),
        total=discover.COUNT_CAP if capped else hits, total_capped=capped, offset=offset, limit=limit, where=where,
        interests=await discover.top_interests(session, now=now),
        next_cursor=offset_cursor(kind, offset + limit) if more and offset + limit < MAX_HITS else None,
    )


# ─── The app's lists ──────────────────────────────────────────────────────


async def list_events(session, search_q: str, *, limit: int, offset: int, is_ended: Optional[bool], interest_ids: list[int]):
    """events.service.list_events's page for a search with nothing of the
    person's own to filter by: the events (club and category loaded), in
    the list's order; None for Postgres."""
    from app.api.closeout.models import EventStatus
    from app.api.clubs.models import Clubs
    from app.api.events.models import EventInterestsLink, ends_at_sql
    from app.api.search import text as stext
    from sqlalchemy import exists, func

    words = _words_for(await stext.normalise(session, search_q))
    if words is None or not await usable(session):
        return None
    now = datetime.now(timezone.utc)
    f = ["listed = true", "lifecycle != archived"]
    if is_ended is True:
        f.append(f"ends_at < {us(now)}")
    elif is_ended is False:
        f.append(f"ends_at >= {us(now)}")
    if interest_ids:
        f.append(f"interest_ids IN [{', '.join(str(int(i)) for i in interest_ids)}]")
    sort_by = ["starts_at:desc", "id:desc"] if is_ended else ["starts_at:asc", "id:asc"]
    try:
        ids, _ = await _ask(search.index(search.EVENTS), words, _and(f), sort_by, offset, limit)
    except search.SearchError as e:
        logger.info("event list search fell back to Postgres: %s", e)
        return None
    if not ids:
        return []
    # Read back under the list's own rules (events.service.list_events).
    conds = [
        Events.id.in_(ids), Events.is_deleted == False,  # noqa: E712
        Events.lifecycle.notin_((EventStatus.archived, EventStatus.draft)), Events.club_id.isnot(None),
        Events.club_id.notin_(select(Clubs.id).where(Clubs.listed == False)),  # noqa: E712
    ]
    if is_ended is True:
        conds.append(ends_at_sql(Events) < func.now())
    elif is_ended is False:
        conds.append(ends_at_sql(Events) >= func.now())
    if interest_ids:
        conds.append(exists(select(EventInterestsLink.id).where(
            EventInterestsLink.event_id == Events.id, EventInterestsLink.interest_id.in_(interest_ids), EventInterestsLink.is_deleted == False,  # noqa: E712
        )))
    loaded = await session.scalars(select(Events).where(*conds).options(joinedload(Events.category), joinedload(Events.club)))
    by_id = {e.id: e for e in loaded.unique().all()}
    return [by_id[i] for i in ids if i in by_id]


async def list_clubs(session, search_q: str, *, limit: int, offset: int, interest_ids: list[int]) -> Optional[tuple[list[int], int]]:
    """(the page's club ids in the list's order, the hits in all) for the
    app's club list searched with nothing of the person's own to filter by;
    None for Postgres. The caller reads the rows under its own rules."""
    from app.api.search import text as stext

    words = _words_for(await stext.normalise(session, search_q))
    if words is None or not await usable(session):
        return None
    f = ["listed = true"]
    if interest_ids:
        f.append(f"interest_ids IN [{', '.join(str(int(i)) for i in interest_ids)}]")
    try:
        return await _ask(search.index(search.CLUBS), words, _and(f), ["created_at:desc", "id:desc"], offset, limit)
    except search.SearchError as e:
        logger.info("club list search fell back to Postgres: %s", e)
        return None
