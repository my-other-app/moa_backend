"""Where things are.

Events and clubs carry a point for "near you", from one of three sources,
kept in `geo_source`, strongest first:

- 'manual': pinned on the dashboard's map. Kept until new pin values come,
  or `clear_point` drops it back to the other two.
- 'link': read out of the map link (usually Google Maps). A link that holds
  the point is read on the spot; a short link (maps.app.goo.gl and friends)
  holds nothing until it is followed, which happens after the save.
- 'geocoded': the venue's words looked up (geocode.py), after the save, when
  there is no point from the other two.

A weaker source never replaces a stronger one, and a derived point goes when
what it was derived from is cleared or changed (apply_place).

A visitor is placed by the browser when it says (lat/lng on the request), or
roughly by their address otherwise. Nothing here raises: a place that cannot
be found is simply not known.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

Point = tuple[float, float]

_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
# "12.97,77.59", "loc:12.97,77.59", "12.97, 77.59", and "12.97 77.59" when both carry decimals.
_PAIR = re.compile(rf"^\s*(?:loc:\s*)?({_NUM})\s*,\s*({_NUM})\s*$")
_PAIR_SPACED = re.compile(r"^\s*(?:loc:\s*)?([-+]?\d{1,3}\.\d+)\s+([-+]?\d{1,3}\.\d+)\s*$")
# The place's own pin in a /maps/place/ link's data blob — more exact than the view's centre.
_PIN = re.compile(rf"!3d({_NUM})!4d({_NUM})")
# The map's centre: /@12.97,77.59,15z
_AT = re.compile(rf"@({_NUM}),({_NUM})")
# Query parameters that carry a point, most specific first.
_PARAMS = ("q", "query", "ll", "center", "destination", "daddr", "sll", "coordinates")
# Parameters that wrap the real address (a consent page's continue=, and the like).
_WRAPPERS = ("continue", "link", "url", "u")


def _valid(lat: float, lng: float) -> Optional[Point]:
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if lat == 0 and lng == 0:
        return None  # "0,0" is a default, not a place
    return (round(lat, 6), round(lng, 6))


def _pair(text: str) -> Optional[Point]:
    m = _PAIR.match(text) or _PAIR_SPACED.match(text)
    if not m:
        return None
    try:
        return _valid(float(m.group(1)), float(m.group(2)))
    except ValueError:
        return None


def coords_from_link(url: Optional[str], _depth: int = 0) -> Optional[Point]:
    """(lat, lng) out of a map link, or None when it holds no point.

    Reads /@lat,lng,zoom, ?q= / query= / ll= / center= (and the directions
    forms), the !3d…!4d… pin of a /place/ link, a geo: URI, a path segment
    that is a bare pair, and a bare "lat,lng" pasted in place of a link."""
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text or len(text) > 4000:
        return None
    try:
        bare = _pair(unquote(text))
        if bare:
            return bare
        if text.lower().startswith("geo:"):
            return _pair(unquote(text[4:]).split(";")[0].split("?")[0])

        if "://" not in text:
            text = "https://" + text
        u = urlparse(text)
        decoded = unquote(text)

        m = _PIN.search(decoded)
        if m:
            found = _valid(float(m.group(1)), float(m.group(2)))
            if found:
                return found

        qs = parse_qs(u.query)
        for key in _PARAMS:
            for value in qs.get(key, []):
                found = _pair(value)
                if found:
                    return found

        m = _AT.search(unquote(u.path)) or _AT.search(decoded)
        if m:
            found = _valid(float(m.group(1)), float(m.group(2)))
            if found:
                return found

        # /maps/search/12.97,+77.59 and /maps/dir//12.97,77.59
        for segment in unquote(u.path).split("/"):
            found = _pair(segment.replace("+", " "))
            if found:
                return found

        # OpenStreetMap: ?mlat=..&mlon=.. or #map=zoom/lat/lng
        if qs.get("mlat") and qs.get("mlon"):
            found = _valid(float(qs["mlat"][0]), float(qs["mlon"][0]))
            if found:
                return found
        m = re.search(rf"map=\d+(?:\.\d+)?/({_NUM})/({_NUM})", u.fragment or "")
        if m:
            found = _valid(float(m.group(1)), float(m.group(2)))
            if found:
                return found

        if _depth < 2:
            for key in _WRAPPERS:
                for value in qs.get(key, []):
                    found = coords_from_link(value, _depth + 1)
                    if found:
                        return found
    except (ValueError, TypeError, AttributeError):
        return None
    return None


# Google's own hosts: the only ones a link is ever followed through. A club
# writes its own map link, so a look-alike (maps.google.example.com) or a
# redirect into our own network must never be requested.
_GOOGLE_HOST = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:google\.(?:com|co\.in|in|co\.uk|com\.au|ca|ae|com\.sg|de|fr)|goo\.gl|g\.page|g\.co|share\.google)$"
)
MAX_HOPS = 6


def _is_google(url: str) -> bool:
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme not in ("https", "http") or port not in (None, 80, 443):
        return False
    return bool(_GOOGLE_HOST.match((u.hostname or "").lower()))


def is_short_link(url: Optional[str]) -> bool:
    """A link whose point only shows once it is followed."""
    if not url or not isinstance(url, str):
        return False
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    if not _is_google(text):
        return False
    try:
        u = urlparse(text)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    path = u.path or ""
    if host in ("maps.app.goo.gl", "g.page", "share.google"):
        return True
    if host == "goo.gl" and path.startswith("/maps"):
        return True
    if host == "g.co" and path.startswith("/kgs"):
        return True
    # maps.google.com/?cid=…, maps.google.com/maps?ftid=…, google.com/maps?cid=…
    if host.startswith("maps.google.") or (re.match(r"^(www\.)?google\.[a-z.]+$", host) and path.startswith("/maps")):
        return True
    return False


def _http(timeout: float = 5.0) -> httpx.AsyncClient:
    """Swapped for a fake in tests: nothing here may reach the network there."""
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MyOtherApp/1.0; +https://myotherapp.in)"},
    )


async def resolve_link(url: Optional[str]) -> Optional[Point]:
    """The point a link stands for, following a short link to where it lands. Never raises.

    Redirects are followed one hop at a time, and only through Google's own
    hosts; the first hop that holds a point ends it."""
    try:
        found = coords_from_link(url)
        if found or not is_short_link(url):
            return found
        target = url.strip()
        if "://" not in target:
            target = "https://" + target
        async with _http() as http:
            for _ in range(MAX_HOPS):
                if not _is_google(target):
                    return None
                # Streamed, and closed unread: only where it points matters, not the page.
                async with http.stream("GET", target, follow_redirects=False) as r:
                    location = r.headers.get("location") if r.is_redirect else None
                if not location:
                    return None
                target = urljoin(target, location)
                found = coords_from_link(target)
                if found:
                    return found
    except Exception:  # noqa: BLE001 — a link that cannot be followed is a place not known
        logger.info("map link could not be followed: %s", url, exc_info=True)
    return None


# ─── Setting a row's point ────────────────────────────────────────────────

MANUAL, LINK, GEOCODED = "manual", "link", "geocoded"
#: Strongest first.
SOURCES = (MANUAL, LINK, GEOCODED)
KEEP = object()


def _source(obj) -> Optional[str]:
    return getattr(obj, "geo_source", None)


def _clear_point(obj) -> None:
    obj.lat = obj.lng = None
    obj.geo_source = None


def _words(text: Optional[str]) -> str:
    return " ".join((text or "").split()).lower()


def valid_point(lat, lng) -> Optional[Point]:
    """A pin as given: a real point on the map, or None (out of range, not a
    number, or "0,0", which is a default and not a place)."""
    try:
        return _valid(float(lat), float(lng))
    except (TypeError, ValueError):
        return None


def pin_from_form(lat, lng) -> Optional[Point]:
    """A pin from the dashboard's map, as a form sends it: both or neither,
    and a real point. Anything else is a 422, field by field, as a bad form
    value is."""
    from fastapi.exceptions import RequestValidationError

    if lat is None and lng is None:
        return None
    if lat is None or lng is None:
        missing = "lat" if lat is None else "lng"
        raise RequestValidationError([{"loc": ("body", missing), "msg": "Send lat and lng together, or neither.", "type": "value_error"}])
    point = valid_point(lat, lng)
    if point is None:
        raise RequestValidationError([{"loc": ("body", "lat"), "msg": "Not a point on the map.", "type": "value_error"}])
    return point


def apply_place(obj, *, link=KEEP, venue=KEEP, manual: Optional[Point] = None, clear_point: bool = False) -> None:
    """Save an event's or club's place: its map link, its venue's words and a
    pin, and the point they give, by the precedence 'manual' > 'link' >
    'geocoded'.

    `link` and `venue`, when given, replace what the row has (KEEP leaves it).
    `manual` is a pin from the dashboard's map: it becomes the point. Without
    one, a pin already there stays, whatever the link and words become,
    unless `clear_point` drops it. Below a pin: a link that holds a point gives
    it now; a changed or cleared link takes its old point away, and so do
    changed or cleared words a geocoded point came from. What is left without
    a point is found after the save (needs_locating, locate_and_store)."""
    old_link, old_venue = getattr(obj, "location_link", None), getattr(obj, "location_name", None)
    if link is not KEEP:
        obj.location_link = (link or "").strip() or None
    if venue is not KEEP:
        obj.location_name = venue
    link_changed = getattr(obj, "location_link", None) != old_link
    venue_changed = _words(getattr(obj, "location_name", None)) != _words(old_venue)

    if manual is not None:
        obj.lat, obj.lng = manual
        obj.geo_source = MANUAL
        return
    if clear_point and _source(obj) == MANUAL:
        _clear_point(obj)
    if _source(obj) == MANUAL:
        return

    found = coords_from_link(obj.location_link) if obj.location_link else None
    if found:
        obj.lat, obj.lng = found
        obj.geo_source = LINK
        return
    if getattr(obj, "lat", None) is None:
        _clear_point(obj)
    elif link_changed or clear_point:
        # The old link's point (or one from before sources were kept), or a
        # geocoded one that a new short link may well better.
        _clear_point(obj)
    elif _source(obj) == GEOCODED and venue_changed:
        _clear_point(obj)


def set_link(obj, link: Optional[str]) -> bool:
    """Put a link on an event or club and read its point now when it holds one
    (apply_place with only a link). A pin dropped by hand stays. Returns True
    when the point has to be found by following the link, after the save."""
    apply_place(obj, link=link)
    return needs_resolving(obj)


def needs_resolving(obj) -> bool:
    link = getattr(obj, "location_link", None)
    return bool(link and getattr(obj, "lat", None) is None and is_short_link(link))


def _geocodable(obj) -> bool:
    """A club, or an event in person: somewhere a venue's words can place."""
    from app.api.discover import geocode

    if getattr(obj, "is_online", False):
        return False
    return geocode.worth_asking(getattr(obj, "location_name", None))


def needs_locating(obj) -> bool:
    """Whether a saved row still has to find its point: by following its
    short link, or failing that by geocoding its venue's words."""
    if _source(obj) == MANUAL or getattr(obj, "lat", None) is not None:
        return False
    if needs_resolving(obj):
        return True
    return bool(settings.GEOCODER_ENABLED) and _geocodable(obj)


@asynccontextmanager
async def session_scope():
    """A session for work after the response. Tests swap this for their own."""
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


def _model(kind: str):
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    return Events if kind == "event" else Clubs


def _may_place(obj, source: str) -> bool:
    """Whether a point from `source` may go on the row: it has none, or only
    a weaker one (a link's point replaces a geocoded one; nothing replaces a pin)."""
    if getattr(obj, "lat", None) is None:
        return True
    return SOURCES.index(source) < SOURCES.index(_source(obj)) if _source(obj) in SOURCES else False


def _upgradable(obj) -> bool:
    """A geocoded point whose row has a link that may give a better one."""
    link = getattr(obj, "location_link", None)
    return _source(obj) == GEOCODED and bool(link) and (coords_from_link(link) is not None or is_short_link(link))


async def _store(kind: str, obj_id: int, point: Point, source: str, *, link: Optional[str], venue: Optional[str]) -> bool:
    """Keep a point found after the save, unless the row moved on meanwhile:
    a pin or another point arrived, or the link or words it came from changed
    (that save has its own task)."""
    async with session_scope() as session:
        obj = await session.get(_model(kind), obj_id)
        if obj is None or not _may_place(obj, source):
            return False
        if obj.location_link != link:
            return False
        if source == GEOCODED and (_words(obj.location_name) != _words(venue) or not _geocodable(obj)):
            return False
        obj.lat, obj.lng = point
        obj.geo_source = source
        await session.commit()
    return True


async def resolve_and_store(kind: str, obj_id: int) -> bool:
    """Follow one row's link and keep the point it lands on. Never raises."""
    try:
        async with session_scope() as session:
            obj = await session.get(_model(kind), obj_id)
            link = getattr(obj, "location_link", None) if obj is not None else None
            if not link or not _may_place(obj, LINK):
                return False
            venue = obj.location_name
        found = await resolve_link(link)
        _tried[(kind, obj_id)] = time.monotonic()
        if not found:
            return False
        return await _store(kind, obj_id, found, LINK, link=link, venue=venue)
    except Exception:  # noqa: BLE001
        logger.warning("could not store the point for %s %s", kind, obj_id, exc_info=True)
        return False


async def locate_and_store(kind: str, obj_id: int) -> Optional[str]:
    """After a save: find a row's point and keep it. Its short link is
    followed first; when that gives nothing (or there is none), its venue's
    words are geocoded. Returns the source of the point kept, or None. Never
    raises."""
    from app.api.discover import geocode

    try:
        async with session_scope() as session:
            obj = await session.get(_model(kind), obj_id)
            if obj is None or _source(obj) == MANUAL or obj.lat is not None:
                return None
            link, venue, geocodable = obj.location_link, obj.location_name, _geocodable(obj)
        if link and is_short_link(link):
            found = await resolve_link(link)
            _tried[(kind, obj_id)] = time.monotonic()
            if found:
                return LINK if await _store(kind, obj_id, found, LINK, link=link, venue=venue) else None
        if not geocodable or not settings.GEOCODER_ENABLED:
            return None
        async with session_scope() as session:
            found = await geocode.lookup(session, venue)
        if found and await _store(kind, obj_id, found, GEOCODED, link=link, venue=venue):
            return GEOCODED
    except Exception:  # noqa: BLE001
        logger.warning("could not locate %s %s", kind, obj_id, exc_info=True)
    return None


# ─── Backfill ──────────────────────────────────────────────────────────────

# When each row's link was last followed, so a link that leads nowhere is not followed on every page view.
_tried: dict[tuple[str, int], float] = {}
RETRY_AFTER = 6 * 3600
_backfilling = False
# How often a page view may look for rows still without a point. The look is
# two queries; once a minute is plenty for a backfill that runs after the
# page has answered.
SCAN_EVERY = 60.0
_last_scan = -SCAN_EVERY


def scan_due() -> bool:
    """Whether this page view should look for rows to backfill (and, if so, the
    next one within SCAN_EVERY seconds should not)."""
    global _last_scan
    now = time.monotonic()
    if now - _last_scan < SCAN_EVERY:
        return False
    _last_scan = now
    return True


def reset_scan() -> None:
    global _last_scan
    _last_scan = -SCAN_EVERY


def _wants_link_where(model):
    """Rows with a link and no point, or only a geocoded one the link may better."""
    from sqlalchemy import or_

    return [
        model.location_link.isnot(None), model.location_link != "", model.is_deleted == False,  # noqa: E712
        or_(model.lat.is_(None), model.geo_source == GEOCODED),
    ]


async def missing(session, limit: Optional[int] = None, *, skip_recent: bool = True) -> list[tuple[str, int]]:
    """Rows whose link may still give their point, events first: no point
    yet, or a geocoded one under a link that holds or leads to a better one."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    now = time.monotonic()
    out: list[tuple[str, int]] = []
    for kind, model in (("event", Events), ("club", Clubs)):
        rows = (await session.execute(
            select(model.id, model.location_link, model.lat, model.geo_source).where(*_wants_link_where(model)).order_by(model.id.desc())
        )).all()
        for i, link, lat, source in rows:
            if lat is not None and not _upgradable(SimpleNamespace(location_link=link, geo_source=source)):
                continue
            if skip_recent and now - _tried.get((kind, i), -RETRY_AFTER * 2) < RETRY_AFTER:
                continue
            out.append((kind, i))
    return out[:limit] if limit else out


CONCURRENCY = 5


async def _resolve_many(items: list[tuple[str, int]]) -> list[bool]:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(kind, i):
        async with gate:
            return await resolve_and_store(kind, i)

    return list(await asyncio.gather(*(one(k, i) for k, i in items)))


async def backfill(items: list[tuple[str, int]]) -> None:
    """The lazy backfill a page view starts: a few rows at a time, one run at a time. Never raises."""
    global _backfilling
    if _backfilling or not items:
        return
    _backfilling = True
    try:
        await _resolve_many(items)
    except Exception:  # noqa: BLE001
        logger.warning("geo backfill failed", exc_info=True)
    finally:
        _backfilling = False


async def backfill_missing(limit: int) -> None:
    """The lazy backfill a page view starts: find a few rows still without a
    point, then follow their links. After the response; never raises."""
    try:
        async with session_scope() as session:
            pending = await missing(session, limit)
        await backfill(pending)
    except Exception:  # noqa: BLE001
        logger.warning("geo backfill failed", exc_info=True)


async def backfill_all(session) -> dict:
    """Every row whose link may still give its point (missing): read what
    can be read here, follow the rest."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    counts = {"events": {"checked": 0, "resolved": 0}, "clubs": {"checked": 0, "resolved": 0}}
    follow: list[tuple[str, int]] = []
    for kind, model in (("event", Events), ("club", Clubs)):
        key = kind + "s"
        rows = (await session.scalars(select(model).where(*_wants_link_where(model)))).all()
        for obj in rows:
            if obj.lat is not None and not _upgradable(obj):
                continue
            counts[key]["checked"] += 1
            found = coords_from_link(obj.location_link)
            if found:
                obj.lat, obj.lng = found
                obj.geo_source = LINK
                counts[key]["resolved"] += 1
            elif is_short_link(obj.location_link):
                follow.append((kind, obj.id))
    await session.commit()
    for (kind, _), ok in zip(follow, await _resolve_many(follow)):
        if ok:
            counts[kind + "s"]["resolved"] += 1
    return counts


async def geocode_backfill(session, *, max_requests: int = 30, now=None) -> dict:
    """Geocode the venue's words of every row still without a point: events
    not over yet first (soonest first), then clubs, then past events (newest
    first). Words already asked about cost nothing and are applied however
    many rows share them; at most `max_requests` go to the geocoder (one a
    second), and `remaining` counts the rows left for another run. A pin or a
    link's point is never touched: only rows without a point are written."""
    from datetime import datetime, timezone

    from sqlalchemy import func

    from app.api.clubs.models import Clubs
    from app.api.discover import geocode
    from app.api.events.models import Events, ends_at_sql

    now = now or datetime.now(timezone.utc)
    counts = {
        "enabled": bool(settings.GEOCODER_ENABLED),
        "events": {"checked": 0, "geocoded": 0, "not_found": 0},
        "clubs": {"checked": 0, "geocoded": 0, "not_found": 0},
        "requests": 0,
        "remaining": 0,
    }
    if not settings.GEOCODER_ENABLED:
        return counts

    def lacking(model):
        conds = [model.lat.is_(None), model.is_deleted == False, model.location_name.isnot(None), func.btrim(model.location_name) != ""]  # noqa: E712
        if model is Events:
            conds.append(func.coalesce(Events.is_online, False) == False)  # noqa: E712
        return conds

    batches = (
        ("event", Events, select(Events.id, Events.location_name).where(*lacking(Events), ends_at_sql(Events) >= now).order_by(Events.event_datetime, Events.id)),
        ("club", Clubs, select(Clubs.id, Clubs.location_name).where(*lacking(Clubs)).order_by(Clubs.id)),
        ("event", Events, select(Events.id, Events.location_name).where(*lacking(Events), ends_at_sql(Events) < now).order_by(Events.event_datetime.desc(), Events.id.desc())),
    )
    for kind, model, stmt in batches:
        key = kind + "s"
        rows = [(i, v, geocode.normalise(v)) for i, v in (await session.execute(stmt)).all()]
        rows = [r for r in rows if r[2] is not None]
        answers = await geocode.cached_many(session, [q for _, _, q in rows])
        written = 0
        for obj_id, venue, query in rows:
            known, point = answers[query]
            if not known:
                if counts["requests"] >= max_requests or geocode.paused():
                    counts["remaining"] += 1
                    continue
                counts["requests"] += 1
                known, point = await geocode.ask(session, query)
                if not known:
                    counts["remaining"] += 1
                    continue
                answers[query] = (True, point)
            counts[key]["checked"] += 1
            if point is None:
                counts[key]["not_found"] += 1
                continue
            # Only where nothing arrived meanwhile, and the words are still these.
            done = await session.execute(
                model.__table__.update()
                .where(model.id == obj_id, model.lat.is_(None), model.location_name == venue)
                .values(lat=point[0], lng=point[1], geo_source=GEOCODED)
            )
            counts[key]["geocoded"] += done.rowcount or 0
            written += 1
            if written % 200 == 0:
                await session.commit()
        await session.commit()
    return counts


# ─── Distances ────────────────────────────────────────────────────────────

EARTH_KM = 6371.0088
# A box a hair wider than the circle: rounding in the trigonometry must never
# leave out a point the exact distance keeps (1e-7 degrees is about a centimetre).
_BOX_PAD = 1e-7


def km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def km_sql(lat: float, lng: float, lat_col, lng_col):
    """`km` from a fixed point to a row's point, as SQL: the same formula, step
    for step, so the database orders and filters by the distance this module
    reports. The fixed point's half is worked out here."""
    from sqlalchemy import func

    p1 = math.radians(lat)
    p2 = func.radians(lat_col)
    dp, dl = p2 - p1, func.radians(lng_col - lng)
    # Halved by multiplying: x * 0.5 is exactly x / 2 in floating point, and
    # SQLAlchemy would turn "/ 2" into a NUMERIC division.
    h = func.power(func.sin(dp * 0.5), 2.0) + math.cos(p1) * func.cos(p2) * func.power(func.sin(dl * 0.5), 2.0)
    return (2 * EARTH_KM) * func.asin(func.least(1.0, func.sqrt(h)))


Box = tuple[float, float, list[tuple[float, float]]]


def bbox(lat: float, lng: float, radius_km: float) -> Optional[Box]:
    """The latitude/longitude box that holds every point within `radius_km` of
    (lat, lng) on the sphere `km` measures: (south, north, [(west, east), …]).

    A B-tree on lat/lng can find the rows inside it; the exact distance then
    decides. Longitude spans the circle's true extent, asin(sin d / cos φ),
    not d / cos φ, which falls short away from the equator. Two longitude
    ranges when the box crosses the antimeridian; every longitude when the
    circle takes in a pole. None when the circle covers the whole sphere and a
    box would rule nothing out."""
    if radius_km < 0:
        return None
    d = radius_km / EARTH_KM  # the angular radius
    if d >= math.pi:
        return None
    phi = math.radians(lat)
    south, north = phi - d, phi + d
    if south <= -math.pi / 2 or north >= math.pi / 2:
        # A pole is inside: every longitude, from the far edge to the pole.
        return (max(math.degrees(south), -90.0) - _BOX_PAD, min(math.degrees(north), 90.0) + _BOX_PAD, [(-180.0, 180.0)])
    dl = math.degrees(math.asin(min(1.0, math.sin(d) / math.cos(phi)))) + _BOX_PAD
    west, east = lng - dl, lng + dl
    if east - west >= 360:
        ranges = [(-180.0, 180.0)]
    elif west < -180:
        ranges = [(west + 360, 180.0), (-180.0, east)]
    elif east > 180:
        ranges = [(west, 180.0), (-180.0, east - 360)]
    else:
        ranges = [(west, east)]
    return (math.degrees(south) - _BOX_PAD, math.degrees(north) + _BOX_PAD, ranges)


def in_box_sql(box: Optional[Box], lat_col, lng_col):
    """SQL for "inside the box" (True when there is no box)."""
    from sqlalchemy import and_, or_, true

    if box is None:
        return true()
    south, north, ranges = box
    lngs = [lng_col.between(w, e) for w, e in ranges]
    return and_(lat_col.between(south, north), lngs[0] if len(lngs) == 1 else or_(*lngs))


# ─── Cells: the coarse place a cached answer is kept for ──────────────────

CELL_DEG = 0.25


def cell(lat: float, lng: float) -> tuple[int, int]:
    return (math.floor(lat / CELL_DEG), math.floor(lng / CELL_DEG))


def cell_centre(c: tuple[int, int]) -> tuple[float, float]:
    return ((c[0] + 0.5) * CELL_DEG, (c[1] + 0.5) * CELL_DEG)


def cell_reach(c: tuple[int, int]) -> float:
    """How far (km) any point of the cell can be from its centre: the farthest
    corner, since distance from a point grows towards a lat/lng box's corners."""
    clat, clng = cell_centre(c)
    lat0, lng0 = c[0] * CELL_DEG, c[1] * CELL_DEG
    return max(km(clat, clng, la, ln) for la in (lat0, lat0 + CELL_DEG) for ln in (lng0, lng0 + CELL_DEG))


def cell_is_cacheable(c: tuple[int, int]) -> bool:
    """Cells are kept away from the poles, where a lat/lng box stops being small."""
    return -85.0 <= c[0] * CELL_DEG and (c[0] + 1) * CELL_DEG <= 85.0


# ─── The visitor ──────────────────────────────────────────────────────────


def client_ip(request) -> str:
    """The visitor's address: the connection's (app.core.net), or, for a page
    the site's own server renders, the visitor it names in X-Visitor-IP --
    believed only behind a valid X-Site-Token (app.core.site)."""
    from app.core import site
    from app.core.net import client_address

    return site.visitor_ip(request) or client_address(request)


def _is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified)


IP_TTL = 24 * 3600
IP_MISS_TTL = 15 * 60  # a refusal (the provider's rate limit, say) is tried again soon, not tomorrow
_IP_CACHE_MAX = 20000
_ip_cache: dict[str, tuple[float, Optional[dict]]] = {}


async def _fetch_ip(ip: str) -> Optional[dict]:
    async with _http(timeout=2.5) as http:
        r = await http.get(settings.GEOIP_URL.format(ip=ip))
    if r.status_code != 200:
        return None
    d = r.json()
    if not isinstance(d, dict) or d.get("error"):
        return None
    lat, lng = d.get("latitude", d.get("lat")), d.get("longitude", d.get("lon"))
    if lat is None or lng is None:
        return None
    point = _valid(float(lat), float(lng))
    if point is None:
        return None
    return dict(
        city=d.get("city") or None,
        region=d.get("region") or d.get("regionName") or None,
        country=d.get("country_name") or d.get("country") or None,
        lat=point[0],
        lng=point[1],
    )


async def locate_ip(ip: Optional[str]) -> Optional[dict]:
    """Roughly where an address is — {city, region, country, lat, lng} — cached for a day. Never raises."""
    if not ip or not settings.GEOIP_URL or not _is_public(ip):
        return None
    now = time.monotonic()
    hit = _ip_cache.get(ip)
    if hit and hit[0] > now:
        return hit[1]
    try:
        place = await _fetch_ip(ip)
    except Exception:  # noqa: BLE001
        place = None
    if len(_ip_cache) >= _IP_CACHE_MAX:
        _ip_cache.clear()
    _ip_cache[ip] = (now + (IP_TTL if place else IP_MISS_TTL), place)
    return place


async def where(request, lat: Optional[float], lng: Optional[float]) -> dict:
    """The visitor's place for a request: the browser's point when given, else the address's."""
    ip_place = await locate_ip(client_ip(request))
    if lat is not None and lng is not None and _valid(lat, lng):
        city = region = None
        # Name the place only when the address agrees with the browser.
        if ip_place and km(lat, lng, ip_place["lat"], ip_place["lng"]) <= 75:
            city, region = ip_place["city"], ip_place["region"]
        return dict(city=city, region=region, lat=lat, lng=lng, source="browser")
    if ip_place:
        return dict(city=ip_place["city"], region=ip_place["region"], lat=ip_place["lat"], lng=ip_place["lng"], source="ip")
    return dict(city=None, region=None, lat=None, lng=None, source="none")
