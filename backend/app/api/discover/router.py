"""The public discover API behind the showcase site: what is on near the
visitor, the clubs, their posts. Open to anyone; a signed-in reader only
changes whether a post shows as liked."""
import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Path, Query, Request, Response

from app.api.discover import counters, geo, service
from app.api.discover.schemas import (
    BackfillResult, CalendarMonth, CategoryCount, ClubEvents, ClubPage, ClubSearch, CounterFixes, EventPage, EventSearch,
    GeocodeBackfillResult, Home, Sitemap, Where,
)
from app.api.threads.schemas import ThreadPage
from app.core.auth.dependencies import AdminAuth, OptionalAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discover")
admin_router = APIRouter(prefix="/admin/discover")

LIMIT = "120/minute"
LAZY_BACKFILL = 25


def _visitor(request: Request) -> str:
    return geo.client_ip(request) or "unknown"


Lat = Query(None, ge=-90, le=90, description="The browser's latitude; without it the visitor is placed by address")
Lng = Query(None, ge=-180, le=180, description="The browser's longitude")


def _viewer(user) -> Optional[int]:
    return getattr(user, "id", None)


@router.get("/locate", summary="Where the visitor is: the browser's point when given, else roughly by address")
@limiter.limit(LIMIT, key_func=_visitor)
async def locate(request: Request, user: OptionalAuth, lat: Optional[float] = Lat, lng: Optional[float] = Lng) -> Where:
    return Where(**await geo.where(request, lat, lng))


@router.get("/home", summary="The front page: near the visitor, this week, online, popular, categories, clubs, posts, numbers")
@limiter.limit(LIMIT, key_func=_visitor)
async def home(request: Request, session: SessionDep, user: OptionalAuth, background_tasks: BackgroundTasks, lat: Optional[float] = Lat, lng: Optional[float] = Lng) -> Home:
    where = await geo.where(request, lat, lng)
    data = await service.home(session, where, _viewer(user))
    # A few rows whose map link has not been read yet, looked for and read
    # after this answers, at most once a minute (geo.scan_due).
    if geo.scan_due():
        background_tasks.add_task(geo.backfill_missing, LAZY_BACKFILL)
    return Home(**data)


Q = Query(None, max_length=100)
Category = Query(None, description="An event category id")
Kind = Query("any", alias="type")
Price = Query("any")
Radius = Query(50, ge=0, le=20000, description="Only with a known place, for in-person events; 0 for anywhere")


@router.get("/events", summary="Events to find: by words, when, category, in person or online, price, and distance")
@limiter.limit(LIMIT, key_func=_visitor)
async def events(
    request: Request,
    session: SessionDep,
    user: OptionalAuth,
    q: Optional[str] = Q,
    when: Literal["any", "today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month"] = Query("any"),
    from_: Optional[date] = Query(None, alias="from", description="YYYY-MM-DD, a day in India: with `to` (or alone), replaces `when`"),
    to: Optional[date] = Query(None, description="YYYY-MM-DD, a day in India, counted in full; at most 366 days after `from`"),
    category: Optional[int] = Category,
    type_: Literal["any", "in_person", "online"] = Kind,
    price: Literal["any", "free", "paid"] = Price,
    lat: Optional[float] = Lat,
    lng: Optional[float] = Lng,
    radius_km: float = Radius,
    sort: Literal["date", "distance", "popular", "relevance"] = Query("date", description="relevance: best match for `q` first (date without `q`)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=60),
) -> EventSearch:
    """`from`/`to`: an event matches if it touches [from 00:00, to + 1 day
    00:00) in India. 422 when `from` is after `to` or the range covers more
    than 366 days."""
    service.date_range(from_, to)  # a 422 before anything is looked up
    where = await geo.where(request, lat, lng)
    return EventSearch(**await service.search_events(
        session, where, q=q, when=when, category=category, type_=type_, price=price,
        radius_km=radius_km, sort=sort, offset=offset, limit=limit, from_=from_, to=to,
    ))


@router.get("/calendar", summary="How many events each day of a month has, with the search's filters")
@limiter.limit(LIMIT, key_func=_visitor)
async def calendar(
    request: Request,
    session: SessionDep,
    user: OptionalAuth,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM, within 24 months of this one"),
    q: Optional[str] = Q,
    category: Optional[int] = Category,
    type_: Literal["any", "in_person", "online"] = Kind,
    price: Literal["any", "free", "paid"] = Price,
    lat: Optional[float] = Lat,
    lng: Optional[float] = Lng,
    radius_km: float = Radius,
) -> CalendarMonth:
    """Days in India; only days with something on. Days already past count
    what was held, so the calendar shows what happened as well as what is
    coming. An event over several days counts on each."""
    where = await geo.where(request, lat, lng)
    return CalendarMonth(**await service.calendar(
        session, where, month=month, q=q, category=category, type_=type_, price=price, radius_km=radius_km,
    ))


@router.get("/events/{ref}", summary="An event's page: the event, its club, speakers, agenda and team, and what else is on")
@limiter.limit(LIMIT, key_func=_visitor)
async def event_page(request: Request, session: SessionDep, user: OptionalAuth, ref: str = Path(..., max_length=160, description="The event's slug, or its id")) -> EventPage:
    """404 for anything the public cannot see: a draft, a deleted event, an unverified club's."""
    return EventPage(**await service.event_page(session, ref))


@router.get("/clubs", summary="Clubs to find: by words, interest and distance")
@limiter.limit(LIMIT, key_func=_visitor)
async def clubs(
    request: Request,
    session: SessionDep,
    user: OptionalAuth,
    q: Optional[str] = Query(None, max_length=100),
    interest: Optional[int] = Query(None, description="An interest id"),
    lat: Optional[float] = Lat,
    lng: Optional[float] = Lng,
    radius_km: float = Query(50, ge=0, le=20000, description="Only with a known place; 0 for anywhere"),
    sort: Literal["popular", "nearby", "newest"] = Query("popular"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=60),
) -> ClubSearch:
    where = await geo.where(request, lat, lng)
    return ClubSearch(**await service.search_clubs(
        session, where, q=q, interest=interest, radius_km=radius_km, sort=sort, offset=offset, limit=limit,
    ))


@router.get("/clubs/{slug}/events", summary="A club's events, a page at a time: past (newest first) or upcoming (soonest first)")
@limiter.limit(LIMIT, key_func=_visitor)
async def club_events(
    request: Request,
    session: SessionDep,
    user: OptionalAuth,
    slug: str = Path(..., max_length=120),
    when: Literal["past", "upcoming"] = Query("upcoming"),
    offset: int = Query(0, ge=0),
    limit: int = Query(12, ge=1, le=48),
) -> ClubEvents:
    """`past` is the club page's past list in full; `upcoming` what the
    search shows of the club. 404 for a club the public cannot see."""
    return ClubEvents(**await service.club_events(session, slug, when, offset, limit))


@router.get("/clubs/{slug}", summary="A club's page: who they are, what is on, what was, their posts, team and partners")
@limiter.limit(LIMIT, key_func=_visitor)
async def club(request: Request, session: SessionDep, user: OptionalAuth, slug: str) -> ClubPage:
    return ClubPage(**await service.club_page(session, slug, _viewer(user)))


@router.get("/threads", summary="The newest posts from every club")
@limiter.limit(LIMIT, key_func=_visitor)
async def thread_feed(request: Request, session: SessionDep, user: OptionalAuth, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)) -> ThreadPage:
    return ThreadPage(**await service.threads_feed(session, _viewer(user), offset, limit))


@router.get("/categories", summary="Event categories, with how many upcoming events each has")
@limiter.limit(LIMIT, key_func=_visitor)
async def categories(request: Request, session: SessionDep, user: OptionalAuth) -> list[CategoryCount]:
    return [CategoryCount(**c) for c in await service.categories(session, service.now_utc())]


@router.get("/sitemap", summary="Every public event and club page, with when each last changed", response_model=Sitemap)
@limiter.limit(LIMIT, key_func=_visitor)
async def sitemap(request: Request, session: SessionDep, user: OptionalAuth) -> Response:
    """Events still to come or held in the last year, and every club; at most 5000 of each.
    The body is made and checked against Sitemap once a minute (service.sitemap_json)."""
    return Response(content=await service.sitemap_json(session), media_type="application/json")


@admin_router.post("/geo-backfill", summary="Read the point out of every event and club map link that has none yet")
async def geo_backfill(session: SessionDep, user: AdminAuth) -> BackfillResult:
    return BackfillResult(**await geo.backfill_all(session))


@admin_router.post("/geocode-backfill", summary="Place every event and club without a point by geocoding its venue's words")
async def geocode_backfill(
    session: SessionDep,
    user: AdminAuth,
    max_requests: int = Query(30, ge=0, le=50, description="At most this many requests go to the geocoder (one a second); words it already answered cost none"),
) -> GeocodeBackfillResult:
    """Events not over yet first, then clubs, then past events. Only rows
    without any point are written, so a pin or a map link's point is never
    touched. Run it again while `remaining` is above zero.

    At one request a second a run takes up to about `max_requests` seconds,
    so the cap keeps it inside the proxy's minute; what it found is committed
    as it goes, so even a run cut short keeps its work."""
    return GeocodeBackfillResult(**await geo.geocode_backfill(session, max_requests=max_requests))


@admin_router.post("/reconcile-counters", summary="Recount any event's going count or club's follower count that disagrees with its rows")
async def reconcile_counters(session: SessionDep, user: AdminAuth) -> CounterFixes:
    """Triggers keep both counts exact; this finds and puts right any that a
    write with triggers switched off (a restore, say) left behind. Returns how
    many were wrong: normally none."""
    return CounterFixes(**await counters.reconcile(session))
