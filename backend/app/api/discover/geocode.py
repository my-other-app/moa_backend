"""A point for a venue typed as words.

Many events and clubs name their venue ("Seminar Hall, NIT Calicut") without
a map link, so "near you" could not place them. Here the words are looked up
in OpenStreetMap's Nominatim, under its usage policy
(https://operations.osmfoundation.org/policies/nominatim/):

- at most one request a second from this process: one lock, and a second's
  gap after each answer before the next request goes;
- a User-Agent that names the app and an address to write to;
- every answer kept, and every miss too, in `geocode_cache`, so a venue is
  asked about once: a found point is kept for good, a miss is asked again
  after MISS_TTL. Venues repeat (every event at "Main Auditorium, CET"), so
  most lookups never leave the database;
- India only (countrycodes=in), the first match only;
- a refusal (429, 403) pauses every lookup for PAUSE seconds.

GEOCODER_ENABLED=false stops it: nothing is asked, read or kept.

A geocoded point is the weakest kind: a map link's point or a pin dropped on
the dashboard's map always wins (geo.apply_place). Nothing here raises; a
place that cannot be found is simply not known.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import Boolean, Column, Float, String, select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

logger = logging.getLogger(__name__)

Point = tuple[float, float]

#: Seconds between one answer and the next request (Nominatim: at most 1/s).
SPACING = 1.0
TIMEOUT = 6.0
#: How long a "nothing found" stands before the words are asked about again.
MISS_TTL = timedelta(days=30)
#: How long every lookup waits after Nominatim refuses one.
PAUSE = 15 * 60
QUERY_MAX = 200

# Words that name no place: an online event's "venue", a placeholder.
_NOT_PLACES = frozenset({
    "online", "virtual", "remote", "zoom", "google meet", "meet", "microsoft teams", "teams", "webex",
    "youtube", "youtube live", "instagram live", "discord", "tba", "tbd", "to be announced",
    "to be decided", "na", "n/a", "none", "nil", "-", "--", "venue", "campus", "home",
})


class GeocodeCache(AbstractSQLModel):
    """What the geocoder said about some words: a point, or that it found none."""

    __tablename__ = "geocode_cache"

    # The words as normalise() leaves them.
    query = Column(String(QUERY_MAX), primary_key=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)
    found = Column(Boolean, nullable=False)
    fetched_at = Column(TZAwareDateTime(timezone=True), nullable=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise(text: Optional[str]) -> Optional[str]:
    """The words as asked and as kept: lower case, single spaces, no stray
    punctuation at the ends. None for nothing worth asking about: empty, too
    long to be a venue, or a word that names no place ("Online", "TBA")."""
    if not text or not isinstance(text, str):
        return None
    q = " ".join(text.split()).strip(" ,.;:-–—|/\\").lower()
    if not q or len(q) > QUERY_MAX or q in _NOT_PLACES or not re.search(r"[^\W\d_]", q):
        return None
    return q


def worth_asking(text: Optional[str]) -> bool:
    return normalise(text) is not None


def _http() -> httpx.AsyncClient:
    """Swapped for a fake in tests: nothing here may reach the network there."""
    email = settings.GEOCODER_EMAIL
    agent = f"MyOtherApp/1.0 (+https://myotherapp.in; {email})" if email else "MyOtherApp/1.0 (+https://myotherapp.in)"
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": agent, "Accept-Language": "en"})


# ─── The pace ───────────────────────────────────────────────────────────

_lock: Optional[asyncio.Lock] = None
_lock_loop = None
_last_answer = -SPACING
_paused_until = 0.0
#: Requests sent since the process started (for the backfill's report and tests).
requests_sent = 0


def _gate() -> asyncio.Lock:
    """The one lock every request goes through, made for the running loop."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def paused() -> bool:
    return time.monotonic() < _paused_until


def reset() -> None:
    """Forget the pace and any pause (tests)."""
    global _last_answer, _paused_until, requests_sent
    _last_answer, _paused_until, requests_sent = -SPACING, 0.0, 0


class _Transient(Exception):
    """No answer this time (a timeout, an error, a refusal): nothing is kept."""


async def _ask(query: str) -> Optional[Point]:
    """One request to the geocoder, in turn: the point, None when it found
    nothing, _Transient when it did not answer."""
    global _last_answer, _paused_until, requests_sent
    from app.api.discover.geo import _valid

    async with _gate():
        if paused():
            raise _Transient("paused")
        wait = _last_answer + SPACING - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        requests_sent += 1
        try:
            async with _http() as http:
                r = await http.get(
                    settings.GEOCODER_URL,
                    params={"q": query, "format": "jsonv2", "limit": "1", "countrycodes": "in"},
                )
        except Exception as exc:  # noqa: BLE001 — a timeout or a dropped connection is no answer
            raise _Transient(str(exc)) from exc
        finally:
            _last_answer = time.monotonic()
    if r.status_code in (403, 429):
        _paused_until = time.monotonic() + PAUSE
        logger.warning("geocoder refused a request (%s); pausing lookups for %s s", r.status_code, PAUSE)
        raise _Transient(f"refused {r.status_code}")
    if r.status_code != 200:
        raise _Transient(f"status {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise _Transient("not json") from exc
    if not isinstance(body, list):
        raise _Transient("not a list")
    if not body:
        return None
    first = body[0] if isinstance(body[0], dict) else {}
    try:
        return _valid(float(first.get("lat")), float(first.get("lon")))
    except (TypeError, ValueError):
        return None


#: Words being asked about right now: a second caller waits for the first's answer.
_inflight: dict[str, asyncio.Future] = {}


async def _ask_once(query: str) -> Optional[Point]:
    pending = _inflight.get(query)
    if pending is not None:
        return await asyncio.shield(pending)
    future = asyncio.get_running_loop().create_future()
    _inflight[query] = future
    try:
        point = await _ask(query)
    except BaseException as exc:
        future.set_exception(exc if isinstance(exc, Exception) else _Transient("cancelled"))
        future.exception()  # read, so a failure nobody waited for is not reported as lost
        raise
    finally:
        _inflight.pop(query, None)
    future.set_result(point)
    return point


# ─── Lookups ────────────────────────────────────────────────────────────


def _known(found: bool, lat, lng, fetched_at) -> tuple[bool, Optional[Point]]:
    if found and lat is not None and lng is not None:
        return True, (lat, lng)
    fetched = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
    return (_now() - fetched < MISS_TTL), None


async def cached(session, query: str) -> tuple[bool, Optional[Point]]:
    """(known, point) for normalised words: known is False when they must be
    asked about (never asked, or a miss older than MISS_TTL). Read as plain
    columns, so a row this session saw earlier cannot answer with old values."""
    row = (await session.execute(
        select(GeocodeCache.found, GeocodeCache.lat, GeocodeCache.lng, GeocodeCache.fetched_at).where(GeocodeCache.query == query)
    )).first()
    return _known(*row) if row is not None else (False, None)


async def cached_many(session, queries) -> dict[str, tuple[bool, Optional[Point]]]:
    """cached() for many words at once (a statement per thousand)."""
    queries = sorted(set(queries))
    out: dict[str, tuple[bool, Optional[Point]]] = {q: (False, None) for q in queries}
    for i in range(0, len(queries), 1000):
        rows = await session.execute(
            select(GeocodeCache.query, GeocodeCache.found, GeocodeCache.lat, GeocodeCache.lng, GeocodeCache.fetched_at)
            .where(GeocodeCache.query.in_(queries[i:i + 1000]))
        )
        for q, *rest in rows.all():
            out[q] = _known(*rest)
    return out


async def _keep(session, query: str, point: Optional[Point]) -> None:
    values = dict(query=query, lat=point[0] if point else None, lng=point[1] if point else None, found=point is not None, fetched_at=_now())
    stmt = insert(GeocodeCache).values(**values)
    await session.execute(stmt.on_conflict_do_update(index_elements=[GeocodeCache.query], set_={k: stmt.excluded[k] for k in ("lat", "lng", "found", "fetched_at")}))


async def ask(session, query: str) -> tuple[bool, Optional[Point]]:
    """Ask the geocoder about normalised words now and keep its answer
    (committed): (answered, point). Not answered: off, paused, or no answer
    (a timeout, an error, a refusal), and nothing is kept. Never raises."""
    if not settings.GEOCODER_ENABLED or paused():
        return False, None
    try:
        point = await _ask_once(query)
    except _Transient:
        logger.info("geocoder gave no answer for %r", query, exc_info=True)
        return False, None
    except Exception:  # noqa: BLE001
        logger.warning("geocoding %r failed", query, exc_info=True)
        return False, None
    try:
        await _keep(session, query, point)
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("could not keep the geocoder's answer for %r", query, exc_info=True)
        await session.rollback()
    return True, point


async def lookup(session, text: Optional[str]) -> Optional[Point]:
    """The point for a venue's words, from the cache or else from the
    geocoder (whose answer, found or not, is kept). None when it is not
    known: nothing found, words not worth asking about, the geocoder off,
    paused or not answering. Never raises."""
    if not settings.GEOCODER_ENABLED:
        return None
    query = normalise(text)
    if query is None:
        return None
    try:
        known, point = await cached(session, query)
    except Exception:  # noqa: BLE001
        logger.warning("could not read the geocode cache for %r", query, exc_info=True)
        return None
    if known:
        return point
    return (await ask(session, query))[1]
