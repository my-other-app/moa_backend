"""The public discover site: what is on, near the visitor or anywhere, and
the clubs behind it.

What the public sees: an event that is published or live, not deleted, not
over, by a listed club; a listed club (clubs.listed: verified, not deleted,
and not under a blocked organisation, kept by triggers). Once over, an event the club held still shows on the club
page's past list, on its own page and in the sitemap, and nowhere else.

Every list is filtered, ordered and paged by the database, so a request
reads a page of rows plus what the indexes walk, whatever the number of
events and clubs:

- "Not over yet" and the order by date run on a partial index of what can be
  shown (ix_events_shown_start), walked from the earliest start an event
  still on can have (upcoming.py), not from the oldest event still marked
  published.
- Words: every word of the query must be found in what the event is found
  by -- its name, place and tag, its club's name and place, its category,
  its interests and their topics -- kept lowercased and unaccented in one
  column, events.search_text (app.api.search.text): substrings (LIKE) for
  words of three letters or more, a whole word for one of two letters ("ai"
  finds "AI Summit", not "Chair"), and a single letter or sign is ignored.
  One column and one trigram index per word, where it was four columns over
  three tables. A search that finds nothing tries again by trigram word
  similarity, which forgives a typo, through the same index. With words, the
  default order is relevance. When Meilisearch is on and well, a search with
  words asks it instead (app.api.search.engine) and Postgres is the fallback.
- Distance is the same haversine as geo.km, in SQL, behind a latitude/
  longitude box (geo.bbox) that an index can find.
- "Popular" sorts events by events.popularity (attention decayed by age,
  rewritten every few minutes: popularity.py) on its own index, and clubs by
  clubs.follower_count, which a trigger keeps exact.
- A list's total counts at most COUNT_CAP + 1 rows; past that it says
  COUNT_CAP and `total_capped` ("1,000+"), and the pages go on regardless.
  `offset` stops at MAX_OFFSET; past it, `after` (the `next_cursor` of the
  page before) starts the next page at its last row (cursor.py).
- A club's place (its own point, else the middle of its in-person events) is
  kept on the club by triggers (places.py), so "clubs near" is a box on an
  index and not an average over every club's events.
- The front page's sections each take a LIMIT, and the parts that are the
  same for every visitor are kept for a minute (cache.py), and for a minute
  more while one task rebuilds them in the background. "Near you" is
  kept per quarter-degree cell as a short list that is provably enough for
  any point in the cell, and finished for the visitor's own point, so it is
  exactly what a fresh query would give.
"""
from __future__ import annotations

import heapq
import html
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import orjson

from sqlalchemy import ARRAY, DateTime, Integer, and_, bindparam, case, cast, column, exists, func, literal_column, or_, select, text, true, union
from sqlalchemy.orm import aliased, contains_eager, selectinload

from app.api.agenda import service as agenda
from app.api.closeout.models import EventStatus
from app.api.club_domains.lookup import active_hosts
from app.api.clubs import service as clubs_service
from app.api.clubs.events_list import registration_open
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubSocials, ClubTeamMembers
from app.api.discover import cache, cells, cursor as cursors, geo, places, stats as site_stats, upcoming
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventCategories, EventInterestsLink, Events, ends_at_sql, span_sql
from app.api.interests import topics as topic_refs
from app.api.interests.models import InterestCategory, Interests
from app.api.organisers import service as organisers
from app.api.search import text as stext
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
#: "Near you" reads what is on in its box in chunks of this many, growing to the most.
NEAR_CHUNK = 64
NEAR_CHUNK_MAX = 4096
HOME_LIST = 8
HOME_CLUBS = 8
HOME_THREADS = 6
CLUB_TEAM_ON_EVENT = 8
ALSO_ON = 4
SITEMAP_MAX = 5000
SITEMAP_PAST = timedelta(days=365)
#: Entries in one page of the paged sitemap (search engines take up to 50,000).
SITEMAP_PAGE = 5000
#: How long the paged sitemap's page boundaries are kept, in seconds.
SITEMAP_BOUNDS_TTL = 3600.0
#: The most a list's total counts exactly; past it, "COUNT_CAP+".
COUNT_CAP = 1000
#: The deepest `offset` a list takes; past it, `after`.
MAX_OFFSET = 1000
#: The most words of a query that count.
MAX_WORDS = 6
WHENS = ("any", "today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month")
#: The longest stretch `from`…`to` may cover, in days, both ends counted.
RANGE_MAX_DAYS = 366
#: How far from this month the calendar answers, either way.
CALENDAR_MONTHS = 24
CLUB_PAST_ON_PAGE = 6
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


def _end_expr():
    return ends_at_sql(Events)


def _ends_at(e: Events) -> datetime:
    return _aware(e.event_datetime) + timedelta(hours=float(e.duration or 0))


def _like(q: str) -> str:
    q = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"


#: A word this short is matched as a whole word, not inside others.
WHOLE_WORD_BELOW = 3


def words_of(q: Optional[str]) -> list[str]:
    """The words of a query that count: split on spaces, lower case, each
    once, at most MAX_WORDS; a single character (a letter, "&", "-") is no
    word. A query of one character alone is kept as it is."""
    raw = [w for w in (q or "").lower().split() if w]
    out: list[str] = []
    for w in raw:
        if len(w) >= 2 and w not in out:
            out.append(w)
    if not out and len(raw) == 1:
        out = raw
    return out[:MAX_WORDS]


def _matches(column, word: str):
    """`column` (a search_text: lowercased, unaccented) holds `word` (a word
    of a query normalised the same way): as a substring (LIKE, which the
    trigram indexes serve) from three characters up; shorter, as a whole
    word, so "ai" does not find "chair" (a regular expression with word
    boundaries; no index can serve a word this short, so it only ever
    filters)."""
    if len(word) >= WHOLE_WORD_BELOW:
        return column.like(_like(word), escape="\\")
    return column.op("~")(r"\m" + re.escape(word) + r"\M")


def _img(value, *keys) -> Optional[str]:
    """One URL out of an image column (a dict of presigned URLs by size)."""
    if not isinstance(value, dict):
        return None
    for k in keys:
        v = value.get(k)
        if v:
            return v
    return None


#: How long a card's summary may be, in characters.
SUMMARY_MAX = 200
_TAGS = re.compile(r"<[^>]{0,500}>")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Paired emphasis at word edges (**bold**, _it_, `code`), never inside a word (snake_case, 3*4).
_MD_EMPHASIS = re.compile(r"(?<!\w)(\*\*|__|~~|\*|_|`)(?=\S)(.+?)(?<=\S)\1(?!\w)")
# Line starts: headings, quotes, list bullets.
_MD_LINE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-+*][ \t]+)", re.MULTILINE)


def summary_of(text: Optional[str], limit: int = SUMMARY_MAX) -> Optional[str]:
    """An event's description as a line of plain text, at most `limit`
    characters: tags and Markdown marks dropped (a link keeps its words),
    entities read, spaces collapsed; a longer one is cut at a word boundary
    (when one falls in its last 40%) and ends in an ellipsis. As the site's
    own clip() does, so a feed can show it as it is. None for nothing."""
    if not text or not isinstance(text, str):
        return None
    s = _TAGS.sub(" ", text)
    s = _MD_LINK.sub(r"\1", s)
    s = _MD_LINE.sub("", s)
    s = _MD_EMPHASIS.sub(r"\2", s)
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    if not s:
        return None
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:!?-") + "…"


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


def day_start(d: date) -> datetime:
    """Midnight in India at the start of a calendar day."""
    return datetime.combine(d, time.min, tzinfo=IST)


def date_range(start: Optional[date], end: Optional[date]) -> tuple[Optional[datetime], Optional[datetime]]:
    """[`start` 00:00, the day after `end` 00:00) in India, either end open.
    Raises 422 for a range that ends before it starts or covers more than
    RANGE_MAX_DAYS days."""
    if start is not None and end is not None:
        if start > end:
            raise CustomHTTPException(422, message="Invalid request", errors={"to": "Must be on or after `from`."})
        if (end - start).days + 1 > RANGE_MAX_DAYS:
            raise CustomHTTPException(422, message="Invalid request", errors={"to": f"A range can cover at most {RANGE_MAX_DAYS} days."})
    return (day_start(start) if start else None, day_start(end + timedelta(days=1)) if end else None)


def _overlaps(start: Optional[datetime], end: Optional[datetime]) -> list:
    """An event that touches [start, end): it starts inside, or it started
    before and is still on at `start` (a half-open overlap; an event of no
    length counts where it starts). Either end may be open.

    With both ends, the event's span is probed through ix_events_span (a
    GiST range index) and the exact rule decides; with one, the start or end
    index bounds it. The planner picks between them on statistics: a short
    stretch through the span index, a long one by walking the date index in
    order (RESULTS.md)."""
    conds: list = []
    start = _instant(start) if isinstance(start, datetime) else start
    end = _instant(end) if isinstance(end, datetime) else end
    if start is not None and end is not None:
        conds.append(span_sql(Events).op("&&")(func.tstzrange(start, end, literal_column("'[)'"))))
    if end is not None:
        conds.append(Events.event_datetime < end)
    if start is not None:
        conds.append(or_(_end_expr() > start, Events.event_datetime >= start))
    return conds


def _instant(dt: datetime):
    """A moment as SQL, typed as one (timestamptz) whatever it is compared with."""
    return cast(dt, DateTime(timezone=True))


# ─── Events ──────────────────────────────────────────────────────────────


def _visible_where(now: datetime, since: datetime) -> list:
    """What is on or to come. `since` is upcoming.since(now): no event still
    on started earlier, and with it the date-ordered walks start near now."""
    return [
        Events.is_deleted == False,  # noqa: E712
        _in(Events.lifecycle, SHOWN),
        *_club_visible(),
        _end_expr() >= now,
        Events.event_datetime >= since,
    ]


def _event_select():
    return (
        select(Events)
        .join(Clubs, Clubs.id == Events.club_id)
        .outerjoin(EventCategories, EventCategories.id == Events.category_id)
        .options(contains_eager(Events.club), contains_eager(Events.category))
    )


def _event_word(w: str):
    """One word anywhere the event is found by (events.search_text)."""
    return _matches(Events.search_text, w)


def _event_words(q: str):
    """Every word of the query somewhere the event is found by: "robotics
    kochi" is a robotics event in Kochi, not every robotics event and every
    event in Kochi, nor only those with the two words side by side. `q` is
    normalised already (search_text.normalise)."""
    words = words_of(q)
    return and_(*[_event_word(w) for w in words]) if words else true()


# How alike a word must be to count as a typo of it (pg_trgm word similarity;
# its default, 0.6, misses "robtics" for "robotics" at 0.55, while 0.5 still
# leaves out "Robots" at 0.38).
FUZZY_THRESHOLD = 0.5


def _event_words_fuzzy(q: str):
    """The same, by trigram word similarity (pg_trgm's `%>`): for when a typo
    found nothing. Every word must still be found, each forgiven its typo (a
    word under three letters, which has no trigram to forgive with, as a
    whole word), so "robtics kochi" is a robotics event in Kochi and not
    every robotics event. The trigram index on search_text finds the few
    candidates: over what can be shown (ix_events_shown_search_trgm) for the
    search, whose conditions name it; over every event for the calendar."""
    arms = []
    for w in words_of(q):
        arms.append(_event_word(w) if len(w) < WHOLE_WORD_BELOW else Events.search_text.op("%>")(w))
    return and_(*arms) if arms else true()


def _fuzzy_worthwhile(q: Optional[str]) -> bool:
    """A trigram needs three characters; fewer would match everything or nothing."""
    return bool(q and len(q.strip()) >= 3)


async def _nowhere(session, q: str) -> bool:
    """True when some word of the query (of three letters or more) is in no
    event's search_text, of any date: then nothing on has every word, and a
    search can go straight to forgiving a typo instead of reading every
    event on to find none (the substring test is a filter on what is on, and
    a filter that matches nothing reads everything).

    Answered through ix_events_search_trgm. Sequential scans are off for
    this one statement: for EXISTS the planner would otherwise read the
    table hoping for an early match, which for a typo never comes."""
    words = [w for w in words_of(q) if len(w) >= WHOLE_WORD_BELOW]
    if not words:
        return False

    def anywhere(w: str):
        return exists(select(Events.id).where(Events.search_text.like(_like(w), escape="\\"), Events.is_deleted == False))  # noqa: E712

    await session.execute(text("SET LOCAL enable_seqscan = off"))
    try:
        return not await session.scalar(select(and_(*[anywhere(w) for w in words])))
    finally:
        await session.execute(text("RESET enable_seqscan"))


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
    """events.popularity (popularity.py), on ix_events_shown_popular."""
    return Events.popularity


def _topic_id(topic: str):
    """The topic a `topic` parameter names, as SQL: its id when it is a
    number, else the live topic with that slug, in any case (none: nothing
    matches)."""
    ref = topic.strip().lower()
    if ref.isascii() and ref.isdigit() and int(ref) < 2**31:
        return int(ref)
    return select(InterestCategory.id).where(InterestCategory.slug == ref, InterestCategory.is_deleted == False).scalar_subquery()  # noqa: E712


def picks(link, link_owner, owner, *, topic: Optional[str] = None, interest: Optional[int] = None):
    """`owner` (Events.id, Clubs.id) picked `interest`, or any interest under
    `topic` (a slug or an id), through its `link` table (whose `link_owner`
    column holds the owner); None for neither.
    `interest` wins when both are given (it is the narrower, and when the two
    disagree, the one the visitor chose last).

    An EXISTS per row on the link's (owner) index, or, where the planner
    prefers, a semi-join from the topic's interests through the link's
    (interest_id, owner) index: either way the event or club's own walk
    (its date or popularity order) stays the plan's spine."""
    if interest is not None:
        return exists(select(link.id).where(link_owner == owner, link.interest_id == interest, link.is_deleted == False))  # noqa: E712
    if topic and topic.strip():
        return exists(
            select(link.id).join(Interests, Interests.id == link.interest_id)
            .where(link_owner == owner, link.is_deleted == False, Interests.is_deleted == False, Interests.category_id == _topic_id(topic))  # noqa: E712
        )
    return None


def _filters(
    *, category: Optional[int] = None, type_: str = "any", price: str = "any", club_id: Optional[int] = None,
    topic: Optional[str] = None, interest: Optional[int] = None,
) -> list:
    """What a visitor narrows a list by, beside when and where."""
    conds: list = []
    if category:
        conds.append(Events.category_id == category)
    picked = picks(EventInterestsLink, EventInterestsLink.event_id, Events.id, topic=topic, interest=interest)
    if picked is not None:
        conds.append(picked)
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


def _conditions(
    now: datetime, since: datetime, *, when: str = "any", category: Optional[int] = None, type_: str = "any", price: str = "any",
    club_id: Optional[int] = None, days: Optional[tuple[Optional[datetime], Optional[datetime]]] = None,
    topic: Optional[str] = None, interest: Optional[int] = None,
) -> list:
    """What is on, narrowed. `days`, a (start, end) from date_range, replaces `when`."""
    conds = _visible_where(now, since)
    if days is not None and (days[0] is not None or days[1] is not None):
        conds += _overlaps(*days)
    else:
        start, end = window(when, now)
        if start is not None:
            conds.append(_end_expr() >= start)
        if end is not None:
            conds.append(Events.event_datetime < end)
    return conds + _filters(category=category, type_=type_, price=price, club_id=club_id, topic=topic, interest=interest)


def filtered(now: datetime, since: datetime, *, when: str = "any", category: Optional[int] = None, type_: str = "any", price: str = "any", q: Optional[str] = None, club_id: Optional[int] = None):
    conds = _conditions(now, since, when=when, category=category, type_=type_, price=price, club_id=club_id)
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


async def card_extras(session, ids: list[int]) -> tuple[dict[int, int], dict[int, list[dict]]]:
    """What a page of cards needs beside their rows, in one statement: how
    many are going to each (the counter, read as going_counts reads it) and
    each event's topics (its interests' topics, in topic order). An event's
    row comes back once per interest it picked: a few at most."""
    if not ids:
        return {}, {}
    link = EventInterestsLink
    rows = await session.execute(
        select(
            Events.id, Events.going_count,
            InterestCategory.id, InterestCategory.slug, InterestCategory.name, InterestCategory.color, InterestCategory.sort_order,
        )
        .select_from(Events)
        .outerjoin(link, and_(link.event_id == Events.id, link.is_deleted == False))  # noqa: E712
        .outerjoin(Interests, and_(Interests.id == link.interest_id, Interests.is_deleted == False))  # noqa: E712
        .outerjoin(InterestCategory, and_(InterestCategory.id == Interests.category_id, InterestCategory.is_deleted == False))  # noqa: E712
        .where(Events.id.in_(ids))
    )
    going: dict[int, int] = {}
    topics: dict[int, dict[int, tuple]] = {}
    for event_id, count, *topic in rows.all():
        if count:
            going[event_id] = int(count)
        if topic[0] is not None:
            topics.setdefault(event_id, {}).setdefault(topic[0], tuple(topic))
    return going, {k: _topic_list(v.values()) for k, v in topics.items()}


def _topic_list(rows) -> list[dict]:
    """(id, slug, name, color, sort_order) rows as topic refs, in topic order."""
    return [dict(id=t[0], slug=t[1], name=t[2], color=t[3]) for t in sorted(rows, key=lambda t: (t[4] or 0, (t[2] or "").lower(), t[0]))]


def _event_km(e: Events, here) -> Optional[float]:
    if here is None or e.is_online or e.lat is None or e.lng is None:
        return None
    return geo.km(here[0], here[1], e.lat, e.lng)


def _in_person_point():
    return and_(func.coalesce(Events.is_online, False) == False, Events.lat.isnot(None), Events.lng.isnot(None))  # noqa: E712


def _within(here, radius_km: float):
    """Online events are anywhere, and an in-person one without a point cannot
    be ruled out; the rest must be inside the radius: in one of the grid
    cells that cover it (cells.py: an index probe per cell), then the exact
    distance."""
    return or_(
        Events.is_online == True,  # noqa: E712
        Events.lat.is_(None),
        Events.lng.is_(None),
        cells.near_sql(here, radius_km, Events.geocell, Events.lat, Events.lng),
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


def card(e: Events, now: datetime, going: int, distance: Optional[float] = None, topics: Optional[list] = None) -> dict:
    """`going` and `topics` as card_extras reads them for the whole page."""
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
        summary=summary_of(e.about),
        # No publish time is kept; when the event was made stands in for it (sort=newest).
        created_at=_iso(e.created_at),
        updated_at=_iso(e.updated_at),
        topics=[dict(t) for t in topics or ()],
    )


def _card_km(c: dict, here) -> Optional[float]:
    """A card's distance from the visitor, from what the card carries (as _event_km)."""
    if here is None or c["is_online"] or c["lat"] is None or c["lng"] is None:
        return None
    return round(geo.km(here[0], here[1], c["lat"], c["lng"]), 1)


def _placed(cards_: list[dict], here) -> list[dict]:
    """Copies of shared cards with the visitor's distance on them."""
    return [dict(c, distance_km=_card_km(c, here)) for c in cards_]


async def cards(session, rows: list[Events], now: datetime, here=None, extras: Optional[tuple] = None) -> list[dict]:
    """`extras`: card_extras for these rows (or more), when the caller has it."""
    going, topics = extras if extras is not None else await card_extras(session, [e.id for e in rows])
    return [card(e, now, going.get(e.id, 0), _event_km(e, here), topics.get(e.id)) for e in rows]


async def category_counts(session, now: datetime, since: Optional[datetime] = None) -> dict[int, int]:
    """How many events are on or to come in each category. Every event has a
    category, so these add up to all of them (the front page's number)."""
    since = since or await upcoming.since(session, now)
    rows = await session.execute(
        select(Events.category_id, func.count(Events.id))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(*_visible_where(now, since))
        .group_by(Events.category_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def topic_counts(session, now: datetime, since: Optional[datetime] = None) -> dict[str, dict[int, int]]:
    """What is on in each topic and interest, and the listed clubs that pick
    each topic, in three grouped statements: {"topic_events", "interest_events",
    "topic_clubs"}, each {id: count}. An event with two interests in one
    topic counts once for it. topic_refs.with_counts keeps the answer."""
    since = since or await upcoming.since(session, now)
    link = EventInterestsLink
    events = func.count(func.distinct(Events.id))

    def on(key):
        return (
            select(key, events).select_from(Events).join(Clubs, Clubs.id == Events.club_id)
            .join(link, and_(link.event_id == Events.id, link.is_deleted == False))  # noqa: E712
            .join(Interests, and_(Interests.id == link.interest_id, Interests.is_deleted == False))  # noqa: E712
            .where(*_visible_where(now, since))
            .group_by(key)
        )

    by_topic = await session.execute(on(Interests.category_id))
    by_interest = await session.execute(on(Interests.id))
    clubs = await session.execute(
        select(Interests.category_id, func.count(func.distinct(Clubs.id)))
        .select_from(Clubs)
        .join(ClubInterestsLink, and_(ClubInterestsLink.club_id == Clubs.id, ClubInterestsLink.is_deleted == False))  # noqa: E712
        .join(Interests, and_(Interests.id == ClubInterestsLink.interest_id, Interests.is_deleted == False))  # noqa: E712
        .where(*_club_visible())
        .group_by(Interests.category_id)
    )
    return dict(
        topic_events={k: int(v) for k, v in by_topic.all()},
        interest_events={k: int(v) for k, v in by_interest.all()},
        topic_clubs={k: int(v) for k, v in clubs.all()},
    )


async def _counts_kept(session, now: datetime, since: Optional[datetime] = None) -> dict[int, int]:
    return await cache.kept(cache.shared, ("category-counts",), lambda s, n: category_counts(s, n, since if n is now else None), session, now)


async def _categories(session, now: datetime, only_upcoming: bool, since: Optional[datetime], counts: Optional[dict]) -> list[dict]:
    if counts is None:
        counts = await _counts_kept(session, now, since)
    rows = (await session.scalars(select(EventCategories).where(EventCategories.is_deleted == False))).all()  # noqa: E712
    out = [
        dict(id=c.id, name=c.name, icon=c.icon, upcoming=counts.get(c.id, 0))
        for c in rows
        if counts.get(c.id) or (c.created_by_id is None and not only_upcoming)
    ]
    out.sort(key=lambda c: (-c["upcoming"], c["name"].lower(), c["id"]))
    return out


async def categories(
    session, now: datetime, *, only_upcoming: bool = False, since: Optional[datetime] = None, counts: Optional[dict] = None,
) -> list[dict]:
    """Every category the product ships plus any a club added that has something on; most upcoming first.
    The same for everyone, so kept for the minute. `since` and `counts`, when
    the caller has them, spare working them out again."""
    got = await cache.kept(
        cache.shared, ("categories", only_upcoming),
        lambda s, n: _categories(s, n, only_upcoming, since if n is now else None, counts if n is now else None), session, now,
    )
    return [dict(c) for c in got]


async def _count(session, stmt) -> int:
    return int(await session.scalar(stmt) or 0)


def _event_count(conds: list):
    return select(func.count(Events.id)).select_from(Events).join(Clubs, Clubs.id == Events.club_id).where(*conds)


def _event_ids(conds: list):
    return select(Events.id).select_from(Events).join(Clubs, Clubs.id == Events.club_id).where(*conds)


async def _count_upto(session, rows, cap: int) -> int:
    """How many rows the select `rows` gives, counting no further than `cap`:
    the database stops reading once it has that many."""
    return await _count(session, select(func.count()).select_from(rows.limit(cap).subquery("counted")))


async def capped_count(session, rows) -> tuple[int, bool]:
    """(total, capped): the exact count up to COUNT_CAP, else (COUNT_CAP, True).
    An exact count of every upcoming event read the whole events table at a
    lakh of clubs (2.7–5 s); this reads at most COUNT_CAP + 1 rows."""
    n = await _count_upto(session, rows, COUNT_CAP + 1)
    return (COUNT_CAP, True) if n > COUNT_CAP else (n, False)


async def _page(
    session, conds: list, keys: list[tuple], offset: int, limit: int, *, with_category: bool = False, keyed: bool = False,
):
    """One page of events in the order of `keys` ((expression, descending)).

    The sort runs on narrow rows (the id and the keys), and only the page's
    rows are then read in full with their club and category: sorting ten
    thousand whole rows to keep twenty-four was most of the work. `keyed`:
    (rows, each row's key values) instead, for the next page's cursor (the
    values as the database worked them out, so a cursor compares exactly)."""
    if limit <= 0:
        return ([], []) if keyed else []
    narrow = select(Events.id.label("id"), *[k.label(f"k{i}") for i, (k, _) in enumerate(keys)]).select_from(Events).join(Clubs, Clubs.id == Events.club_id)
    if with_category:
        narrow = narrow.outerjoin(EventCategories, EventCategories.id == Events.category_id)
    page = (
        narrow.where(*conds)
        .order_by(*[k.desc() if d else k for k, d in keys])
        .offset(offset).limit(limit)
        .subquery("page")
    )
    cols = [page.c[f"k{i}"] for i in range(len(keys))]
    order = [c.desc() if d else c for c, (_, d) in zip(cols, keys)]
    got = (await session.execute(_event_select().add_columns(*cols).join(page, page.c.id == Events.id).order_by(*order))).unique().all()
    rows = [r[0] for r in got]
    if not keyed:
        return rows
    return rows, [list(r[1:]) for r in got]


#: The order of each sort, as (expression, descending) keys, and how a cursor carries them.
def _sort_keys(sort: str, q: Optional[str]) -> tuple[list[tuple], list[str]]:
    by_date = [(Events.event_datetime, False), (Events.id, False)]
    if sort == "popular":
        return [(_popularity(), True), *by_date], ["float", "dt", "int"]
    if sort == "relevance":
        return [(_relevance(q), True), *by_date], ["float", "dt", "int"]
    if sort == "newest":
        return [(Events.created_at, True), (Events.id, True)], ["dt", "int"]
    return by_date, ["dt", "int"]


async def search_events(
    session, where: dict, *, q: Optional[str], when: str, category: Optional[int], type_: str, price: str,
    radius_km: float, sort: Optional[str], offset: int, limit: int, now: Optional[datetime] = None,
    from_: Optional[date] = None, to: Optional[date] = None, after: Optional[str] = None,
    topic: Optional[str] = None, interest: Optional[int] = None,
) -> dict:
    """`from_`/`to` (calendar days in India, both ends counted), when either
    is given, replace `when`: an event matches if it touches that stretch.
    `topic` (a slug or an id) and `interest`: events with that interest, or
    any interest under that topic (`interest` wins when both are given).

    `sort` left out is relevance with words, else date. `total` is exact up
    to COUNT_CAP; past it, COUNT_CAP with `total_capped` (the pages go on
    regardless: a page is asked for, not worked out from the total).
    `after`, the `next_cursor` of the page before, starts this page after its
    last event (`offset` is then ignored): a range of the order's index,
    however deep."""
    now = now or now_utc()
    here = _here(where)
    days = date_range(from_, to) if (from_ is not None or to is not None) else None
    raw_q, q = q, await stext.normalise(session, q)
    since = await upcoming.since(session, now)
    words = bool(words_of(q))
    if sort is None:
        sort = "relevance" if words else "date"
    if sort == "relevance" and not words:
        sort = "date"
    if sort == "distance" and here is None:
        sort = "date"
    if words:
        # Meilisearch, when it is on and well; Postgres below otherwise.
        from app.api.search import engine

        found = await engine.discover_events(
            session, where, q=q, here=here, when=when, category=category, type_=type_, price=price, radius_km=radius_km,
            sort=sort, offset=offset, limit=limit, now=now, since=since, days=days, topic=topic, interest=interest, after=after,
        )
        if found is not None:
            return found
        # A Meilisearch cursor carries an offset: carry on from it here.
        resumed = engine.offset_of(after, f"events:{sort}")
        if resumed is not None:
            offset, after = resumed, None
    conds = _conditions(now, since, when=when, category=category, type_=type_, price=price, days=days, topic=topic, interest=interest)
    radius = here is not None and type_ != "online" and radius_km > 0
    if radius:
        conds.append(_within(here, radius_km))
    if words and _fuzzy_worthwhile(q) and await _nowhere(session, q):
        total, capped = 0, False  # a typo, most likely: no need to read what is on to find nothing
    else:
        total, capped = await capped_count(session, _event_ids(conds + ([_event_words(q)] if words else [])))
    if words:
        if total == 0 and _fuzzy_worthwhile(q):
            # Nothing has these letters in this order: forgive a typo.
            await _fuzzy(session)
            conds.append(_event_words_fuzzy(q))
            total, capped = await capped_count(session, _event_ids(conds))
        else:
            conds.append(_event_words(q))

    by_date = [(Events.event_datetime, False), (Events.id, False)]
    page: list[Events] = []
    next_cursor = None
    if sort == "distance":
        page, next_cursor = await _distance_page(session, conds, here, radius_km if radius else None, offset, limit, total, capped, after)
    else:
        keys, types = _sort_keys(sort, raw_q)
        start = cursors.decode(after, f"events:{sort}", types)
        if start is not None:
            conds.append(cursors.after(keys, start))
            offset = 0
        if start is not None or capped or total > offset:
            page, keyed = await _page(session, conds, keys, offset, limit + 1, with_category=sort == "relevance", keyed=True)
            if len(page) > limit:
                page = page[:limit]
                next_cursor = cursors.encode(f"events:{sort}", keyed[limit - 1])
    going, topics = await card_extras(session, [e.id for e in page])
    cats = [dict(id=c["id"], name=c["name"], upcoming=c["upcoming"]) for c in await categories(session, now, only_upcoming=True, since=since)]
    return dict(
        items=[card(e, now, going.get(e.id, 0), _event_km(e, here), topics.get(e.id)) for e in page],
        total=total, total_capped=capped, offset=offset, limit=limit, where=where, categories=cats,
        topics=await topic_refs.facet(session, now), next_cursor=next_cursor,
    )


DISTANCE_TYPES = ["int", "float", "dt", "int"]


async def _distance_page(session, conds: list, here, radius_km: Optional[float], offset: int, limit: int, total: int, capped: bool, after: Optional[str]):
    """Nearest first, then everything without a distance (online, or no
    point) by date. The two parts are paged as one list; the first can use
    the point index, inside the radius's box. Where the page falls needs the
    placed part's size only up to the page's end. A cursor carries the part
    (0 placed, 1 not) and the last row's keys."""
    by_date = [(Events.event_datetime, False), (Events.id, False)]
    km = geo.km_sql(here[0], here[1], Events.lat, Events.lng)
    placed = conds + [_in_person_point()]
    if radius_km is not None:
        # The cells that cover the circle: the cell index finds the placed part.
        by_cell = cells.cover_sql(Events.geocell, cells.cover(here[0], here[1], radius_km))
        placed.append(by_cell if by_cell is not None else geo.in_box_sql(geo.bbox(here[0], here[1], radius_km), Events.lat, Events.lng))
    unplaced = conds + [or_(Events.is_online == True, Events.lat.is_(None), Events.lng.is_(None))]  # noqa: E712
    placed_keys = [(km, False), *by_date]
    start = cursors.decode(after, "events:distance", DISTANCE_TYPES)
    # Each row with its cursor: (0, km, start, id) placed, (1, 0, start, id) not.
    tagged: list[tuple[Events, list]] = []

    async def take(cond_list, keys, part, skip, n):
        rows, keyed = await _page(session, cond_list, keys, skip, n, keyed=True)
        for e, k in zip(rows, keyed):
            tagged.append((e, [0, *k] if part == 0 else [1, 0.0, *k]))

    if start is not None:
        part, values = start[0], start[1:]
        if part == 0:
            await take(placed + [cursors.after(placed_keys, values)], placed_keys, 0, 0, limit + 1)
            if len(tagged) <= limit:
                await take(unplaced, by_date, 1, 0, limit + 1 - len(tagged))
        else:
            await take(unplaced + [cursors.after(by_date, values[1:])], by_date, 1, 0, limit + 1)
    elif capped or total > offset:
        n_placed = await _count_upto(session, _event_ids(placed), offset + limit + 2)
        if offset < n_placed:
            await take(placed, placed_keys, 0, offset, limit + 1)
        if offset + limit + 1 > n_placed:
            await take(unplaced, by_date, 1, max(0, offset - n_placed), limit + 1 - len(tagged))
    next_cursor = cursors.encode("events:distance", tagged[limit - 1][1]) if len(tagged) > limit else None
    page = [e for e, _ in tagged[:limit]]
    return page, next_cursor


# ─── The calendar ────────────────────────────────────────────────────────


def month_bounds(month: str, now: datetime) -> tuple[date, date]:
    """The first day of "YYYY-MM" and of the month after. 422 for a month more
    than CALENDAR_MONTHS from this one (in India) either way."""
    year, mon = int(month[:4]), int(month[5:7])
    local = now.astimezone(IST)
    if abs((year * 12 + mon - 1) - (local.year * 12 + local.month - 1)) > CALENDAR_MONTHS:
        raise CustomHTTPException(422, message="Invalid request", errors={"month": f"Pick a month within {CALENDAR_MONTHS} months of this one."})
    return date(year, mon, 1), date(year + mon // 12, mon % 12 + 1, 1)


async def _day_counts(session, first: date, after: date, conds: list) -> dict[str, int]:
    """How many of the events `conds` admits touch each day of [first, after),
    days in India, in one statement. Only days with something on come back.

    The month's events are found once, through ix_events_span; each then
    gives the days it touches, from the day it starts to the day it is last on
    (its end less a microsecond: an event ending at midnight is not on the
    next day; one of no length is on the day it starts), cut to the month, by
    a generate_series of its own; and the days are counted. The work is the
    month's events and the days they touch, never every event against every
    day."""
    start, end = day_start(first), day_start(after)
    month_events = (
        select(Events.event_datetime.label("starts"), _end_expr().label("ends"))
        .select_from(Events).join(Clubs, Clubs.id == Events.club_id)
        .where(*conds, *_overlaps(start, end))
        .subquery("month_events")
    )

    def local_day(instant):
        """The calendar day in India an instant falls on, as a plain timestamp at its midnight."""
        return func.date_trunc("day", func.timezone(literal_column("'Asia/Kolkata'"), instant))

    starts, ends = month_events.c.starts, month_events.c.ends
    last_on = case((ends > starts, ends - literal_column("INTERVAL '1 microsecond'")), else_=starts)
    month_first = cast(datetime.combine(first, time.min), DateTime(timezone=False))
    month_last = cast(datetime.combine(after - timedelta(days=1), time.min), DateTime(timezone=False))
    days = (
        func.generate_series(func.greatest(local_day(starts), month_first), func.least(local_day(last_on), month_last), literal_column("INTERVAL '1 day'"))
        .table_valued(column("day", DateTime(timezone=False)))
        .render_derived(name="days")
    )
    rows = await session.execute(select(days.c.day, func.count()).select_from(month_events).join(days, true()).group_by(days.c.day))
    return {d.date().isoformat(): int(n) for d, n in rows.all() if n}


async def _calendar(
    session, now: datetime, first: date, after: date, *, words: Optional[str], category, type_: str, price: str, here, radius_km: float,
    topic: Optional[str] = None, interest: Optional[int] = None,
) -> dict[str, int]:
    conds = _public_where(now) + [_in(Events.lifecycle, HELD)] + _filters(category=category, type_=type_, price=price, topic=topic, interest=interest)
    if here is not None:
        conds.append(_within(here, radius_km))
    counts = await _day_counts(session, first, after, conds + ([_event_words(words)] if words else []))
    if words and not counts and _fuzzy_worthwhile(words):
        # As the search does: nothing has these letters in this order, so forgive a typo.
        await _fuzzy(session)
        counts = await _day_counts(session, first, after, conds + [_event_words_fuzzy(words)])
    return counts


async def calendar(
    session, where: dict, *, month: str, q: Optional[str], category: Optional[int], type_: str, price: str,
    radius_km: float, now: Optional[datetime] = None, topic: Optional[str] = None, interest: Optional[int] = None,
) -> dict:
    """How many public events touch each day of `month` (days in India), with
    the search's filters: what is on or to come as the search shows it, and
    on days already past, what was held (as a club page's past list shows
    it), so the calendar shows what happened too. An event that runs over
    several days counts on each. Kept for the minute per filter and place.

    A place counts by its quarter-degree cell (geo.cell, about 28 km): the
    days are counted around the cell's centre, so every visitor in the cell
    shares one answer and one cache entry, instead of one per point a
    browser reports. An event within a few km of the radius's edge may
    therefore be in or out for a visitor at the cell's edge; the calendar is
    a guide to busy days, and the search below it answers for the exact
    point. (Cells by the poles, where a cell stops being small, use the point.)"""
    now = now or now_utc()
    first, after = month_bounds(month, now)
    here = _here(where)
    radius = here is not None and type_ != "online" and radius_km > 0
    words = " ".join(words_of(await stext.normalise(session, q))) or None
    centre = None
    if radius:
        c = geo.cell(*here)
        centre = geo.cell_centre(c) if geo.cell_is_cacheable(c) else here
    place = (centre, radius_km) if radius else (None, None)
    # `interest` wins over `topic` (picks), so a topic beside an interest is no part of the answer or its key.
    topic = None if interest is not None else ((topic or "").strip() or None)
    key = ("calendar", month, words, category, type_, price, place, topic, interest)
    days = await cache.kept(cache.shared, key, lambda s, n: _calendar(
        s, n, first, after, words=words, category=category, type_=type_, price=price, here=centre, radius_km=radius_km,
        topic=topic, interest=interest,
    ), session, now)
    return dict(month=month, days=dict(days))


# ─── Clubs ───────────────────────────────────────────────────────────────


def _club_visible():
    """A club the public may see: listed (verified, not deleted, its
    organisation not blocked). The first two are spelled out as well: they
    are implied, and they keep the partial indexes that name them usable."""
    return [Clubs.listed == True, Clubs.is_verified == True, Clubs.is_deleted == False]  # noqa: E712


async def follower_counts(session, ids: list[int]) -> dict[int, int]:
    """How many follow each of these clubs: the trigger-kept counter (follows
    that are following and not deleted), read as plain columns (see going_counts)."""
    if not ids:
        return {}
    rows = await session.execute(select(Clubs.id, Clubs.follower_count).where(Clubs.id.in_(ids)))
    return {k: int(v) for k, v in rows.all() if v}


async def _upcoming_counts(session, ids: list[int], now: datetime, since: datetime) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Events.club_id, func.count(Events.id))
        .join(Clubs, Clubs.id == Events.club_id)
        .where(Events.club_id.in_(ids), *_visible_where(now, since))
        .group_by(Events.club_id)
    )
    return {k: int(v) for k, v in rows.all()}


async def _club_interests(session, ids: list[int]) -> tuple[dict[int, list[str]], dict[int, list[dict]]]:
    """Each club's first three interests' names, and the topics of all of
    them (in topic order), from one statement."""
    names: dict[int, list[str]] = {i: [] for i in ids}
    topics: dict[int, dict[int, tuple]] = {i: {} for i in ids}
    if ids:
        rows = await session.execute(
            select(
                ClubInterestsLink.club_id, Interests.name,
                InterestCategory.id, InterestCategory.slug, InterestCategory.name, InterestCategory.color, InterestCategory.sort_order,
            )
            .join(Interests, Interests.id == ClubInterestsLink.interest_id)
            .outerjoin(InterestCategory, and_(InterestCategory.id == Interests.category_id, InterestCategory.is_deleted == False))  # noqa: E712
            .where(ClubInterestsLink.club_id.in_(ids), ClubInterestsLink.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
            .order_by(ClubInterestsLink.id)
        )
        for club_id, name, *topic in rows.all():
            if len(names[club_id]) < 3 and name not in names[club_id]:
                names[club_id].append(name)
            if topic[0] is not None:
                topics[club_id].setdefault(topic[0], tuple(topic))
    return names, {k: _topic_list(v.values()) for k, v in topics.items()}


async def _published(session, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set((await session.scalars(select(ClubSites.club_id).where(ClubSites.club_id.in_(ids), ClubSites.published_at.isnot(None)))).all())


async def club_cards(session, clubs: list[Clubs], now: datetime, *, since: datetime, distances: Optional[dict] = None, followers: Optional[dict] = None) -> list[dict]:
    ids = [c.id for c in clubs]
    distances = distances or {}
    if followers is None:
        followers = await follower_counts(session, ids)
    upcoming = await _upcoming_counts(session, ids, now, since)
    interests, topics = await _club_interests(session, ids)
    published = await _published(session, ids)
    # A club's own domain, while it is active, is where its website is.
    own = await active_hosts(session, published)
    return [
        dict(
            id=c.id, name=c.name, slug=c.slug,
            logo=_img(c.logo, "thumbnail", "medium", "original"),
            cover=_img(c.cover, "medium", "large", "original"),
            tagline=c.tagline, location_name=c.location_name,
            followers=followers.get(c.id, 0), upcoming_events=upcoming.get(c.id, 0),
            interests=interests.get(c.id, []),
            topics=topics.get(c.id, []),
            distance_km=round(distances[c.id], 1) if distances.get(c.id) is not None else None,
            website_url=(f"https://{own[c.id]}" if c.id in own else sites.public_url(c)) if c.id in published else None,
        )
        for c in clubs
    ]


def _club_words(q: str):
    """Every word of the (normalised) query in what the club is found by:
    its name, tagline, place, interests and their topics (clubs.search_text)."""
    words = words_of(q)
    if not words:
        return true()
    return and_(*[_matches(Clubs.search_text, w) for w in words])


def _club_words_fuzzy(q: str):
    """Every word in the club's search_text, each forgiven a typo (as _event_words_fuzzy)."""
    arms = []
    for w in words_of(q):
        arms.append(_matches(Clubs.search_text, w) if len(w) < WHOLE_WORD_BELOW else Clubs.search_text.op("%>")(w))
    return and_(*arms) if arms else true()


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
    got = await cache.kept(cache.shared, ("interests", limit), lambda s, n: _top_interests(s, limit), session, now)
    return [dict(i) for i in got]


async def search_clubs(
    session, where: dict, *, q: Optional[str], interest: Optional[int], radius_km: float, sort: str,
    offset: int, limit: int, now: Optional[datetime] = None, after: Optional[str] = None, topic: Optional[str] = None,
) -> dict:
    """Clubs by words, interest and distance. A club's distance is from its
    place (places.py: its own point, else the middle of its in-person
    events); a club with no place at all is never ruled out by a radius and
    comes after every club with one when sorted by distance. `total` as in
    search_events: exact up to COUNT_CAP, else COUNT_CAP with `total_capped`;
    `after` as there too (the popular order walks ix_clubs_listed_popular).
    `topic` (a slug or an id): clubs that pick any interest under it;
    `interest` wins when both are given."""
    now = now or now_utc()
    here = _here(where)
    q = await stext.normalise(session, q)
    conds = list(_club_visible())
    picked = picks(ClubInterestsLink, ClubInterestsLink.club_id, Clubs.id, topic=topic, interest=interest or None)
    if picked is not None:
        conds.append(picked)
    lat, lng = Clubs.place_lat, Clubs.place_lng
    dist = None
    if here is not None:
        dist = case((places.placed(), geo.km_sql(here[0], here[1], lat, lng)), else_=None)
        if radius_km > 0:
            conds.append(places.within(here, radius_km))

    def ids(extra: list):
        return select(Clubs.id).where(*conds, *extra)

    words = bool(words_of(q))
    if sort not in ("nearby", "newest") or (sort == "nearby" and here is None):
        sort = "popular"
    if words:
        # Meilisearch, when it is on and well; Postgres below otherwise.
        from app.api.search import engine

        found = await engine.discover_clubs(
            session, where, q=q, here=here, interest=interest, topic=topic, radius_km=radius_km, sort=sort,
            offset=offset, limit=limit, now=now, after=after,
        )
        if found is not None:
            return found
        resumed = engine.offset_of(after, f"clubs:{sort}")
        if resumed is not None:
            offset, after = resumed, None
    total, capped = await capped_count(session, ids([_club_words(q)] if words else []))
    if words:
        if total == 0 and _fuzzy_worthwhile(q):
            await _fuzzy(session)
            conds.append(_club_words_fuzzy(q))
            total, capped = await capped_count(session, ids([]))
        else:
            conds.append(_club_words(q))

    if sort == "nearby" and here is not None:
        sort = "nearby"
        keys = [(case((dist.is_(None), 1), else_=0), False), (func.coalesce(dist, 0.0), False), (Clubs.follower_count, True), (Clubs.id, False)]
        types = ["int", "float", "int", "int"]
    elif sort == "newest":
        keys, types = [(Clubs.created_at, True), (Clubs.id, True)], ["dt", "int"]
    else:
        sort = "popular"
        keys, types = [(Clubs.follower_count, True), (func.lower(Clubs.name).collate("C"), False), (Clubs.id, False)], ["int", "str", "int"]
    start = cursors.decode(after, f"clubs:{sort}", types)
    if start is not None:
        conds.append(cursors.after(keys, start))
        offset = 0
    # The place as plain columns: a club already in this session would answer
    # with the place it was loaded with, and the triggers may have moved it since.
    stmt = select(Clubs, lat, lng, *[k for k, _ in keys])
    order = [k.desc() if d else k for k, d in keys]
    rows = (await session.execute(stmt.where(*conds).order_by(*order).offset(offset).limit(limit + 1))).all() if (start is not None or capped or total > offset) else []
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = cursors.encode(f"clubs:{sort}", list(rows[-1][3:]))
    rows = [r[:3] for r in rows]
    page = [r[0] for r in rows]
    distances = {}
    if here is not None:
        for c, la, ln in rows:
            distances[c.id] = geo.km(here[0], here[1], float(la), float(ln)) if la is not None and ln is not None else None
    return dict(
        items=await club_cards(session, page, now, since=await upcoming.since(session, now), distances=distances),
        total=total, total_capped=capped, offset=offset, limit=limit, where=where, interests=await top_interests(session, now=now),
        next_cursor=next_cursor,
    )


def _social_links(raw: dict) -> dict:
    return {k: (sites._social_href(k, v) if v and v.strip() else None) for k, v in raw.items()}


def _team_member(m: ClubTeamMembers, role: Optional[str]) -> dict:
    return dict(
        name=m.name, role=role, photo=_img(m.photo, "medium", "original", "thumbnail"),
        links=_social_links({k: getattr(m, k) for k in ("instagram", "linkedin", "x", "website")}),
    )


def _club_past_where(club_id: int, now: datetime) -> list:
    """What a club held: over, and published, live, closed or archived (never
    a draft or a deleted event). The club page's past list and its "all past
    events" pages both read it, newest first (_CLUB_PAST_ORDER)."""
    return [Events.club_id == club_id, Events.is_deleted == False, _in(Events.lifecycle, HELD), _end_expr() < now]  # noqa: E712


_CLUB_PAST_ORDER = (Events.event_datetime.desc(), Events.id.desc())


async def club_events(session, slug: str, when: str, offset: int, limit: int, now: Optional[datetime] = None) -> dict:
    """Every event of a club, a page at a time: `past` by the club page's
    past rule, newest first; `upcoming` as the search shows them, soonest
    first. 404 for a club the public cannot see.

    Four statements whatever the club holds: the club, the count, the page
    (chosen on narrow rows by ix_events_club_start, then read in full) and the
    page's going counters and topics (card_extras)."""
    now = now or now_utc()
    club_id = await session.scalar(select(Clubs.id).where(Clubs.slug == slug, *_club_visible()))
    if club_id is None:
        raise CustomHTTPException(404, message="Club not found")
    if when == "past":
        conds, keys = _club_past_where(club_id, now), [(Events.event_datetime, True), (Events.id, True)]
    else:
        conds, keys = _conditions(now, await upcoming.since(session, now), club_id=club_id), [(Events.event_datetime, False), (Events.id, False)]
    total = await _count(session, _event_count(conds))
    page = await _page(session, conds, keys, offset, limit) if total > offset else []
    return dict(
        items=await cards(session, page, now),
        total=total, offset=offset, limit=limit,
    )


async def club_page(session, slug: str, viewer_id: Optional[int], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    club = await session.scalar(select(Clubs).where(Clubs.slug == slug, *_club_visible()))
    if club is None:
        raise CustomHTTPException(404, message="Club not found")
    since = await upcoming.since(session, now)
    detail = (await club_cards(session, [club], now, since=since))[0]
    socials = await session.scalar(select(ClubSocials).where(ClubSocials.club_id == club.id, ClubSocials.is_deleted == False))  # noqa: E712
    detail.update(
        about=club.about, contact_email=club.contact_email,
        socials=_social_links({k: getattr(socials, k, None) for k in ("instagram", "linkedin", "youtube", "website")}),
    )

    coming = await _rows(session, filtered(now, since, club_id=club.id).limit(60))
    past_where = _club_past_where(club.id, now)
    past = await _rows(session, _event_select().where(*past_where).order_by(*_CLUB_PAST_ORDER).limit(CLUB_PAST_ON_PAGE))
    detail["events_held"] = int(await session.scalar(select(func.count(Events.id)).where(*past_where)) or 0)
    extras = await card_extras(session, [e.id for e in coming + past])

    posts = list((await session.scalars(
        select(ClubThreads).where(ClubThreads.club_id == club.id, ClubThreads.is_deleted == False)  # noqa: E712
        .order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(5)
    )).all())

    team = [_team_member(m, m.role) for m in await clubs_service.list_team(session, club.id)]
    partners = [dict(name=p.name, role=p.role, logo=_img(p.logo, "medium", "thumbnail", "original")) for p in await clubs_service.list_partners(session, club.id)]
    return dict(
        club=detail,
        upcoming=await cards(session, coming, now, extras=extras),
        past=await cards(session, past, now, extras=extras),
        threads=await threads._shape(session, posts, {club.id: club}, viewer_id),
        team=team, partners=partners,
    )


# ─── An event's page ─────────────────────────────────────────────────────


def _public_where(now: datetime, since: Optional[datetime] = None) -> list:
    """An event anyone may open: on now or still to come, as the search shows
    it, or held and over, as a club page's past list shows it. `since`
    (upcoming.since), when the caller has it, bounds the first arm's start as
    the lists do; it never changes the answer, and the callers that look
    events up by id or by a stretch of days (the span index) do without."""
    end = _end_expr()
    on = [_in(Events.lifecycle, SHOWN), end >= now] + ([Events.event_datetime >= since] if since is not None else [])
    return [
        Events.is_deleted == False,  # noqa: E712
        *_club_visible(),
        or_(and_(*on), and_(_in(Events.lifecycle, HELD), end < now)),
    ]


def _ref_where(ref: str):
    """A slug, or the id the old site's addresses carried; None for a number too big to be an id."""
    if ref.isascii() and ref.isdigit():
        n = int(ref)
        return Events.id == n if n < 2**31 else None
    return Events.slug == ref


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt is not None else None


async def _interests_of(session, event_id: int) -> tuple[list[str], list[dict]]:
    """The event's interests' names, in the order it picked them, and their topics, in topic order."""
    rows = await session.execute(
        select(Interests.name, InterestCategory.id, InterestCategory.slug, InterestCategory.name, InterestCategory.color, InterestCategory.sort_order)
        .join(EventInterestsLink, EventInterestsLink.interest_id == Interests.id)
        .outerjoin(InterestCategory, and_(InterestCategory.id == Interests.category_id, InterestCategory.is_deleted == False))  # noqa: E712
        .where(EventInterestsLink.event_id == event_id, EventInterestsLink.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
        .order_by(EventInterestsLink.id)
    )
    names: list[str] = []
    topics: dict[int, tuple] = {}
    for name, *topic in rows.all():
        if name not in names:
            names.append(name)
        if topic[0] is not None:
            topics.setdefault(topic[0], tuple(topic))
    return names, _topic_list(topics.values())


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
    since = await upcoming.since(session, now)
    e = None
    if key is not None:
        e = await session.scalar(_event_select().options(selectinload(Events.speakers)).where(key, *_public_where(now, since)))
    if e is None:
        raise CustomHTTPException(404, message="Event not found")
    club, cat = e.club, e.category
    going = (await going_counts(session, [e.id])).get(e.id, 0)
    poster = _img(e.poster, "original", "large", "medium", "thumbnail")
    # An online event has no venue to show; a link left in the map field may well be the meeting itself.
    venue = (None, None, None) if e.is_online else (e.location_link, e.lat, e.lng)
    interest_names, topics = await _interests_of(session, e.id)
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
        interests=interest_names,
        topics=topics,
        guidelines=e.event_guidelines,
        contact=dict(name=e.contact_name, email=e.contact_email, phone=e.contact_phone),
        url=public_urls.event_url(e.slug),
        summary=summary_of(e.about),
        created_at=_iso(e.created_at),
        updated_at=_iso(e.updated_at),
    )
    speakers = [
        dict(id=s.id, name=s.name, designation=s.designation, photo=_img(s.photo, "medium", "thumbnail", "original"), bio=s.bio, link=s.link)
        for s in e.speakers if not s.is_deleted
    ]
    team, team_source = await _event_team(session, e)
    more = await _rows(session, filtered(now, since, club_id=club.id).where(Events.id != e.id).limit(ALSO_ON))
    similar = await _rows(session, filtered(now, since, category=e.category_id).where(Events.club_id != club.id).limit(ALSO_ON))
    shown = await card_extras(session, [x.id for x in more + similar])
    return dict(
        event=event,
        club=dict(
            id=club.id, name=club.name, slug=club.slug, logo=_img(club.logo, "thumbnail", "medium", "original"),
            tagline=club.tagline, followers=(await follower_counts(session, [club.id])).get(club.id, 0), location_name=club.location_name,
        ),
        speakers=speakers,
        agenda=dict(timezone=EVENT_TIMEZONE, items=await agenda.list_items(session, e.id)),
        team=team, team_source=team_source,
        more_from_club=await cards(session, more, now, extras=shown),
        similar=await cards(session, similar, now, extras=shown),
    )


# ─── Sitemap ─────────────────────────────────────────────────────────────


async def _sitemap(session, now: datetime) -> dict:
    events = (await session.execute(
        select(Events.slug, Events.updated_at)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(*_sitemap_events_where(now))
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


def _sitemap_events_where(now: datetime) -> list:
    """The events a search engine should know: public, and on, to come or held in the last year."""
    return [*_public_where(now), Events.event_datetime >= now - SITEMAP_PAST]


async def sitemap(session, now: Optional[datetime] = None) -> dict:
    """At most SITEMAP_MAX events (the latest first) and clubs: what the site's
    sitemap.xml has read so far. sitemap_index and sitemap_page list them all.
    Events still to come or held in the last year, and every club, each with
    when it last changed. The same for every crawler, so kept for the minute."""
    now = now or now_utc()
    return await cache.kept(cache.shared, ("sitemap",), _sitemap, session, now)


async def sitemap_json(session, now: Optional[datetime] = None) -> bytes:
    """The sitemap as the response body, made once a minute: building and
    checking 6,500 entries for every request cost more than reading them."""
    from app.api.discover.schemas import Sitemap

    now = now or now_utc()

    async def build(s, n) -> bytes:
        return orjson.dumps(Sitemap(**await _sitemap(s, n)).model_dump(mode="json"))

    return await cache.kept(cache.shared, ("sitemap-json",), build, session, now)


# The paged sitemap: every public event (held ones of any age included) and
# every listed club, in pages of SITEMAP_PAGE in id order, so a page holds the
# same entries from one crawl to the next. One statement numbers the rows and gives each page its first id,
# its size and its newest change; a page is then a range of ids, read through
# the primary key. The page boundaries are kept for SITEMAP_BOUNDS_TTL: a page
# may meanwhile lose a few entries or (the last one) gain some, and is never
# longer than SITEMAP_PAGE.

SITEMAP_KINDS = ("events", "clubs")


def _sitemap_rows(kind: str, now: datetime, *, every: bool = False):
    """(model, conditions, join the clubs) of a sitemap kind: every public
    event (on, to come or held, of any age: an event's page stays up once it
    is over) or every listed club.

    `every`, for the boundaries, which read every event: the events' club
    rule becomes "not one of the unlisted clubs" (unverified or deleted: a
    few percent), which Postgres keeps as a hashed list while it reads the
    events once; joined (or as IN, which it turns into a join) it probed the
    clubs once an event, 600,000 pages for a million events. For one page of
    a few thousand events the join is cheaper than reading every club. The
    columns are NOT NULL, so the two say the same."""
    if kind == "events" and not every:
        return Events, _public_where(now), True
    if kind == "events":
        end = _end_expr()
        return Events, [
            Events.is_deleted == False,  # noqa: E712
            Events.club_id.isnot(None),
            Events.club_id.notin_(select(Clubs.id).where(Clubs.listed == False)),  # noqa: E712
            or_(and_(_in(Events.lifecycle, SHOWN), end >= now), and_(_in(Events.lifecycle, HELD), end < now)),
        ], False
    return Clubs, list(_club_visible()), False


async def _sitemap_bounds(session, kind: str, now: datetime) -> list[dict]:
    model, conds, _ = _sitemap_rows(kind, now, every=True)
    numbered = select(
        model.id.label("id"), model.updated_at.label("at"), func.row_number().over(order_by=model.id).label("n"),
    ).where(*conds).subquery("numbered")
    page = ((numbered.c.n - 1) // SITEMAP_PAGE).label("page")
    rows = await session.execute(
        select(page, func.min(numbered.c.id), func.count(), func.max(numbered.c.at)).group_by(page).order_by(page)
    )
    return [dict(first=first, entries=int(n), updated_at=_iso(at)) for _, first, n, at in rows.all()]


async def _sitemap_bounds_kept(session, kind: str, now: datetime) -> list[dict]:
    return await cache.kept(cache.shared, ("sitemap-bounds", kind), lambda s, n: _sitemap_bounds(s, kind, n), session, now, ttl=SITEMAP_BOUNDS_TTL)


async def sitemap_index(session, now: Optional[datetime] = None) -> dict:
    """Every page of the sitemap: its kind, number, size and newest change."""
    now = now or now_utc()
    pages = []
    for kind in SITEMAP_KINDS:
        for i, p in enumerate(await _sitemap_bounds_kept(session, kind, now), start=1):
            pages.append(dict(kind=kind, page=i, entries=p["entries"], updated_at=p["updated_at"]))
    return dict(page_size=SITEMAP_PAGE, pages=pages)


async def sitemap_page(session, kind: str, page: int, now: Optional[datetime] = None) -> dict:
    """One page of `kind`'s sitemap entries, in id order. 404 past the last page."""
    now = now or now_utc()
    bounds = await _sitemap_bounds_kept(session, kind, now)
    if not 1 <= page <= len(bounds):
        raise CustomHTTPException(404, message="No such sitemap page")
    model, conds, join_clubs = _sitemap_rows(kind, now)
    conds = conds + [model.id >= bounds[page - 1]["first"]]
    if page < len(bounds):
        conds.append(model.id < bounds[page]["first"])
    stmt = select(model.slug, model.updated_at).where(*conds).order_by(model.id).limit(SITEMAP_PAGE)
    if join_clubs:
        stmt = stmt.join(Clubs, Clubs.id == Events.club_id)
    rows = (await session.execute(stmt)).all()
    return dict(kind=kind, page=page, pages=len(bounds), items=[dict(slug=slug, updated_at=_iso(at)) for slug, at in rows])


# ─── Threads ─────────────────────────────────────────────────────────────


def _threads_base():
    return (
        select(ClubThreads)
        .join(Clubs, Clubs.id == ClubThreads.club_id)
        .where(ClubThreads.is_deleted == False, *_club_visible())  # noqa: E712
    )


async def threads_feed(session, viewer_id: Optional[int], offset: int, limit: int, after: Optional[str] = None) -> dict:
    """The newest posts from every listed club. Reading them here counts no
    views. `total` as in search_events: exact up to COUNT_CAP; `after` as
    there too (a range of ix_club_threads_newest)."""
    base = _threads_base()
    total, capped = await capped_count(session, base.with_only_columns(ClubThreads.id))
    start = cursors.decode(after, "threads", ["dt", "uuid"])
    page_q = base.order_by(ClubThreads.created_at.desc(), ClubThreads.id)
    if start is not None:
        page_q = page_q.where(cursors.after([(ClubThreads.created_at, True), (ClubThreads.id, False)], start))
        offset = 0
    rows = list((await session.scalars(page_q.offset(offset).limit(limit + 1))).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = cursors.encode("threads", [rows[-1].created_at, rows[-1].id])
    by_id = await threads._clubs_by_id(session, {t.club_id for t in rows})
    return dict(items=await threads._shape(session, rows, by_id, viewer_id), total=total, total_capped=capped, limit=limit, offset=offset, next_cursor=next_cursor)


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


async def stats(session, upcoming: int, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    clubs = await session.scalar(select(func.count(Clubs.id)).where(*_club_visible()))
    # Every place an event was ever held: a distinct count over all of them,
    # kept in a small table and counted again in the background (stats.py).
    cities = await site_stats.cities(session, now)
    return dict(upcoming_events=upcoming, clubs=int(clubs or 0), cities=cities)


async def _home_shared(session, now: datetime, since: datetime) -> dict:
    """The front page as anyone sees it: nothing about where they are or who they are."""
    visible = _visible_where(now, since)
    by_date = [(Events.event_datetime, False), (Events.id, False)]
    this_week = await _page(session, visible + [Events.event_datetime < now + timedelta(days=7)], by_date, 0, HOME_LIST)
    online = await _page(session, visible + [Events.is_online == True], by_date, 0, HOME_LIST)  # noqa: E712
    popular = await _page(session, visible, [(_popularity(), True), *by_date], 0, HOME_LIST)
    going, topics = await card_extras(session, list({e.id for e in this_week + online + popular}))

    top_clubs = list((await session.scalars(select(Clubs).where(*_club_visible()).order_by(*_popular_clubs_order()).limit(HOME_CLUBS))).all())

    posts = list((await session.scalars(_threads_base().order_by(ClubThreads.created_at.desc(), ClubThreads.id).limit(HOME_THREADS))).all())
    by_id = await threads._clubs_by_id(session, {t.club_id for t in posts})

    def out(es):
        return [card(e, now, going.get(e.id, 0), None, topics.get(e.id)) for e in es]

    # Every event has a category: the per-category counts add up to every
    # event on or to come, so the front page's number needs no count of its own.
    counts = await _counts_kept(session, now, since)
    return dict(
        this_week=out(this_week), online=out(online), popular=out(popular),
        categories=await categories(session, now, only_upcoming=True, since=since, counts=counts),
        clubs=await club_cards(session, top_clubs, now, since=since),
        threads=await threads._shape(session, posts, by_id, None),
        stats=await stats(session, sum(counts.values()), now),
    )


async def _near_pool(session, now: datetime, since: datetime, centre: tuple[float, float], reach: float) -> dict:
    """In-person events, soonest first, enough to answer "near you" for any
    point within `reach` km of `centre`.

    An event more than NEAR_KM + reach from the centre is more than NEAR_KM
    from every such point, so it is left out. One within NEAR_KM - reach is
    near every such point, so once NEAR_COUNT of those have been seen, nothing
    later can make anyone's first NEAR_COUNT: the list stops there. What is
    kept is (id, lat, lng); the cards are made when a visitor needs them."""
    far = NEAR_KM + reach + _SLACK_KM
    sure = NEAR_KM - reach - _SLACK_KM
    # Read in date order a chunk at a time (each a range of the date order
    # after the last row read), growing, and stop at the chunk that settles
    # it: a city holds thousands of events to come, and the list is settled
    # by the first few dozen near the centre.
    # The club rule as "not one of the unlisted clubs" (a hashed list of the
    # few), not a join: joined, every event looked its club up.
    conds = [
        Events.is_deleted == False, _in(Events.lifecycle, SHOWN), _end_expr() >= now, Events.event_datetime >= since,  # noqa: E712
        Events.club_id.isnot(None), Events.club_id.notin_(select(Clubs.id).where(Clubs.listed == False)),  # noqa: E712
        _in_person_point(), geo.km_sql(centre[0], centre[1], Events.lat, Events.lng) <= far,
    ]
    order = [(Events.event_datetime, False), (Events.id, False)]
    # The cells within `far`, each walked in date order on ix_events_shown_cell
    # (geocell, event_datetime, id) and merged: a chunk reads at most a chunk
    # per cell, however many events the country has and however few are
    # near. Where the cover is too wide to list (by a pole), the whole date
    # order, boxed, as before.
    near_cells = cells.listed(cells.cover(centre[0], centre[1], far))
    pool: list[tuple[int, float, float]] = []
    if near_cells is None:
        conds.append(geo.in_box_sql(geo.bbox(centre[0], centre[1], far), Events.lat, Events.lng))
        return await _near_walk(session, lambda last, n: (
            select(Events.id, Events.lat, Events.lng, Events.event_datetime, true().label("keep"))
            .where(*conds, *([cursors.after(order, last)] if last is not None else []))
            .order_by(Events.event_datetime, Events.id).limit(n)
        ), centre, far, sure, pool)

    # Each cell read in its index's order (geocell, event_datetime, id),
    # stopping at the chunk. The planner cannot see how few of a cell's rows
    # fail these conditions (it expected 7 a cell where there were 1,318, at
    # a lakh of clubs) and would fetch every row of every cell to sort them
    # (18,643 heap pages, for 21 a cell kept); with bitmap scans off for
    # these statements only, it walks the index and stops (one chunk, about
    # 3,000 pages for the events, measured). Reading the rows first and
    # testing them after needs no setting, but reads past every row a test
    # fails -- events over and not yet closed by the clock -- in chunks four
    # times bigger each (25,000-32,000 pages, measured).
    each = select(func.unnest(bindparam("near_cells", near_cells, type_=ARRAY(Integer))).label("cell")).subquery("near_cells")

    def chunk_of(last, n: int):
        per_cell = (
            select(Events.id, Events.lat, Events.lng, Events.event_datetime)
            .where(Events.geocell.isnot(None), Events.geocell == each.c.cell, *conds,
                   *([cursors.after(order, last)] if last is not None else []))
            .order_by(Events.event_datetime, Events.id).limit(n)
            .lateral("per_cell")
        )
        c = per_cell.c
        return (
            select(c.id, c.lat, c.lng, c.event_datetime, true().label("keep")).select_from(each).join(per_cell, true())
            .order_by(c.event_datetime, c.id).limit(n)
        )

    # Reset on success; a failure ends in a rollback, which drops the setting anyway.
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    got = await _near_walk(session, chunk_of, centre, far, sure, pool)
    await session.execute(text("RESET enable_bitmapscan"))
    return got


async def _near_walk(session, chunk_of, centre, far, sure, pool) -> dict:
    """_near_pool's chunks, in date order, until the list is settled: each a
    range of the date order after the last row read, growing. `chunk_of(last,
    n)` is the select of the next n rows (id, lat, lng, start, keep) after
    the cursor `last`."""
    certain = 0
    last = None
    chunk = NEAR_CHUNK
    while True:
        rows = (await session.execute(chunk_of(last, chunk))).all()
        for event_id, lat, lng, starts, keep in rows:
            last = [starts, event_id]
            if not keep:
                continue
            d = geo.km(centre[0], centre[1], lat, lng)
            if d > far:
                continue
            pool.append((event_id, lat, lng))
            if d <= sure:
                certain += 1
                if certain >= NEAR_COUNT:
                    return dict(events=pool, cards={})
        if len(rows) < chunk:
            break
        chunk = min(chunk * 4, NEAR_CHUNK_MAX)
    return dict(events=pool, cards={})


async def _near(session, now: datetime, pool: dict, here: tuple[float, float]) -> list[dict]:
    picked = [i for i, lat, lng in pool["events"] if geo.km(here[0], here[1], lat, lng) <= NEAR_KM][:NEAR_COUNT]
    missing = [i for i in picked if i not in pool["cards"]]
    if missing:
        rows = await _rows(session, _event_select().where(Events.id.in_(missing)))
        going, topics = await card_extras(session, missing)
        for e in rows:
            pool["cards"][e.id] = card(e, now, going.get(e.id, 0), None, topics.get(e.id))
    return _placed([pool["cards"][i] for i in picked if i in pool["cards"]], here)


async def _club_pool(session, now: datetime, centre: tuple[float, float], reach: float) -> dict:
    """Clubs enough to give the HOME_CLUBS nearest for any point within
    `reach` km of `centre`: (id, lat, lng, followers), cards made as needed.

    If R is the distance from the centre to its HOME_CLUBS-th nearest club, a
    point P in reach has that many clubs within R + reach, so its own
    nearest are all within R + reach of P, and so within R + 2·reach of the
    centre. Clubs with no place at all come after every club with one, so
    they are only needed when fewer than HOME_CLUBS clubs have a place.

    R is found by widening circles on the clubs' place index (places.nearest):
    a few small box queries where clubs are, never every club's place."""
    r, every = await places.nearest(session, centre, HOME_CLUBS)
    if r is not None:
        rows = await places.around(session, centre, r + 2 * reach + _SLACK_KM)
    else:  # fewer than HOME_CLUBS clubs have a place: all of them, then the most followed without
        rows = every + await places.unplaced(session, HOME_CLUBS)
    return dict(clubs=rows, cards={})


async def _nearest_clubs(session, now: datetime, since: datetime, pool: dict, here: tuple[float, float]) -> list[dict]:
    ranked = []
    for i, la, ln, n in pool["clubs"]:
        d = geo.km(here[0], here[1], la, ln) if la is not None and ln is not None else None
        ranked.append(((d is None, d or 0.0, -n, i), i, d))
    picked = heapq.nsmallest(HOME_CLUBS, ranked)
    missing = [i for _, i, _ in picked if i not in pool["cards"]]
    if missing:
        rows = list((await session.scalars(select(Clubs).where(Clubs.id.in_(missing)))).all())
        for c in await club_cards(session, rows, now, since=since):
            pool["cards"][c["id"]] = c
    return [dict(pool["cards"][i], distance_km=round(d, 1) if d is not None else None) for _, i, d in picked if i in pool["cards"]]


async def home(session, where: dict, viewer_id: Optional[int], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    here = _here(where)
    since = await upcoming.since(session, now)

    async def shared_part(s, n):
        return await _home_shared(s, n, since if n is now else await upcoming.since(s, n))

    shared = await cache.kept(cache.shared, ("home",), shared_part, session, now)

    near: list[dict] = []
    clubs = [dict(c) for c in shared["clubs"]]
    if here is not None:
        cell = geo.cell(*here)
        if geo.cell_is_cacheable(cell):
            centre, reach = geo.cell_centre(cell), geo.cell_reach(cell)

            async def near_part(s, n):
                return await _near_pool(s, n, since if n is now else await upcoming.since(s, n), centre, reach)

            pool = await cache.kept(cache.shared, ("near", cell), near_part, session, now)
            club_pool = await cache.kept(cache.shared, ("home-clubs", cell), lambda s, n: _club_pool(s, n, centre, reach), session, now)
        else:  # by a pole: worked out for this point alone
            pool = await _near_pool(session, now, since, here, 0.0)
            club_pool = await _club_pool(session, now, here, 0.0)
        near = await _near(session, now, pool, here)
        clubs = await _nearest_clubs(session, now, since, club_pool, here)

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

