"""Where things are.

Events and clubs carry a map link (usually Google Maps) and, since "near
you", the coordinates read out of it. A link that holds them is read on the
spot; a short link (maps.app.goo.gl and friends) holds nothing until it is
followed, which happens after the save, never in its way.

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
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

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


def is_short_link(url: Optional[str]) -> bool:
    """A link whose point only shows once it is followed."""
    if not url or not isinstance(url, str):
        return False
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
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
    """The point a link stands for, following a short link to where it lands. Never raises."""
    try:
        found = coords_from_link(url)
        if found or not is_short_link(url):
            return found
        target = url.strip()
        if "://" not in target:
            target = "https://" + target
        async with _http() as http:
            # Streamed, and closed unread: only where it lands matters, not the page.
            async with http.stream("GET", target) as r:
                hops = [str(h.headers.get("location") or "") for h in r.history] + [str(r.url)]
        for hop in reversed(hops):
            found = coords_from_link(hop)
            if found:
                return found
    except Exception:  # noqa: BLE001 — a link that cannot be followed is a place not known
        logger.info("map link could not be followed: %s", url, exc_info=True)
    return None


# ─── Setting a row's point ────────────────────────────────────────────────


def set_link(obj, link: Optional[str]) -> bool:
    """Put a link on an event or club and read its point now when it holds one.

    An empty link clears the point. Returns True when the point has to be
    found by following the link, after the save."""
    link = (link or "").strip() or None
    obj.location_link = link
    found = coords_from_link(link) if link else None
    obj.lat, obj.lng = found if found else (None, None)
    return bool(link and not found and is_short_link(link))


def needs_resolving(obj) -> bool:
    link = getattr(obj, "location_link", None)
    return bool(link and getattr(obj, "lat", None) is None and is_short_link(link))


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


async def resolve_and_store(kind: str, obj_id: int) -> bool:
    """Follow one row's link and keep the point it lands on. Never raises."""
    try:
        model = _model(kind)
        async with session_scope() as session:
            obj = await session.get(model, obj_id)
            link = getattr(obj, "location_link", None) if obj is not None else None
            if not link or obj.lat is not None:
                return False
        found = await resolve_link(link)
        _tried[(kind, obj_id)] = time.monotonic()
        if not found:
            return False
        async with session_scope() as session:
            obj = await session.get(model, obj_id)
            # The link may have changed while it was being followed; that save has its own task.
            if obj is None or obj.location_link != link:
                return False
            obj.lat, obj.lng = found
            await session.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("could not store the point for %s %s", kind, obj_id, exc_info=True)
        return False


# ─── Backfill ──────────────────────────────────────────────────────────────

# When each row's link was last followed, so a link that leads nowhere is not followed on every page view.
_tried: dict[tuple[str, int], float] = {}
RETRY_AFTER = 6 * 3600
_backfilling = False


async def missing(session, limit: Optional[int] = None, *, skip_recent: bool = True) -> list[tuple[str, int]]:
    """Rows with a link and no point, events first."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    now = time.monotonic()
    out: list[tuple[str, int]] = []
    for kind, model in (("event", Events), ("club", Clubs)):
        ids = (await session.scalars(
            select(model.id).where(model.location_link.isnot(None), model.location_link != "", model.lat.is_(None), model.is_deleted == False).order_by(model.id.desc())  # noqa: E712
        )).all()
        for i in ids:
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


async def backfill_all(session) -> dict:
    """Every row with a link and no point: read what can be read here, follow the rest."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events

    counts = {"events": {"checked": 0, "resolved": 0}, "clubs": {"checked": 0, "resolved": 0}}
    follow: list[tuple[str, int]] = []
    for kind, model in (("event", Events), ("club", Clubs)):
        key = kind + "s"
        rows = (await session.scalars(
            select(model).where(model.location_link.isnot(None), model.location_link != "", model.lat.is_(None), model.is_deleted == False)  # noqa: E712
        )).all()
        for obj in rows:
            counts[key]["checked"] += 1
            found = coords_from_link(obj.location_link)
            if found:
                obj.lat, obj.lng = found
                counts[key]["resolved"] += 1
            elif is_short_link(obj.location_link):
                follow.append((kind, obj.id))
    await session.commit()
    for (kind, _), ok in zip(follow, await _resolve_many(follow)):
        if ok:
            counts[kind + "s"]["resolved"] += 1
    return counts


# ─── Distances ────────────────────────────────────────────────────────────


def km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


# ─── The visitor ──────────────────────────────────────────────────────────


def client_ip(request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else ""


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
