"""The public discover API behind the showcase site: what is on near the
visitor, the clubs, their posts. Open to anyone; a signed-in reader only
changes whether a post shows as liked."""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Path, Query, Request

from app.api.discover import geo, service
from app.api.discover.schemas import BackfillResult, CategoryCount, ClubPage, ClubSearch, EventPage, EventSearch, Home, Sitemap, Where
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
    # A few rows whose map link has not been read yet, read after this answers.
    try:
        pending = await geo.missing(session, LAZY_BACKFILL)
        if pending:
            background_tasks.add_task(geo.backfill, pending)
    except Exception:  # noqa: BLE001 — the page never waits on the backfill
        logger.warning("could not schedule the geo backfill", exc_info=True)
    return Home(**data)


@router.get("/events", summary="Events to find: by words, when, category, in person or online, price, and distance")
@limiter.limit(LIMIT, key_func=_visitor)
async def events(
    request: Request,
    session: SessionDep,
    user: OptionalAuth,
    q: Optional[str] = Query(None, max_length=100),
    when: Literal["any", "today", "tomorrow", "this_week", "this_weekend", "next_week", "this_month"] = Query("any"),
    category: Optional[int] = Query(None, description="An event category id"),
    type_: Literal["any", "in_person", "online"] = Query("any", alias="type"),
    price: Literal["any", "free", "paid"] = Query("any"),
    lat: Optional[float] = Lat,
    lng: Optional[float] = Lng,
    radius_km: float = Query(50, ge=0, le=20000, description="Only with a known place, for in-person events; 0 for anywhere"),
    sort: Literal["date", "distance", "popular"] = Query("date"),
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=60),
) -> EventSearch:
    where = await geo.where(request, lat, lng)
    return EventSearch(**await service.search_events(
        session, where, q=q, when=when, category=category, type_=type_, price=price,
        radius_km=radius_km, sort=sort, offset=offset, limit=limit,
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


@router.get("/sitemap", summary="Every public event and club page, with when each last changed")
@limiter.limit(LIMIT, key_func=_visitor)
async def sitemap(request: Request, session: SessionDep, user: OptionalAuth) -> Sitemap:
    """Events still to come or held in the last year, and every club; at most 5000 of each."""
    return Sitemap(**await service.sitemap(session))


@admin_router.post("/geo-backfill", summary="Read the point out of every event and club map link that has none yet")
async def geo_backfill(session: SessionDep, user: AdminAuth) -> BackfillResult:
    return BackfillResult(**await geo.backfill_all(session))
