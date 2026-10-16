from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.links import overview as overview_mod
from app.api.links import service
from app.api.links.schemas import LinkCreate, LinkDetail, LinkOut, LinkPatch, LinksOverview, LinksSummary
from app.config import settings
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter()
# The short link lives at the root — "api.myotherapp.in/l/x7k2m9pq" fits on a poster.
public_router = APIRouter()


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


def _out(link, club) -> LinkOut:
    return LinkOut(
        id=link.id, name=link.name, code=link.code, url=service.short_url(link), destination=service.destination(link, getattr(club, "slug", None)),
        utm_source=link.utm_source, utm_medium=link.utm_medium, utm_campaign=link.utm_campaign, utm_content=link.utm_content,
        clicks=0, uniques=0, conversions=0, rate=0.0, second=0, created_at=link.created_at.isoformat(), kind=link.kind or "custom",
    )


# ─── An event's links ─────────────────────────────────────────────────────


@router.get("/events/{event_id}/links", summary="Every tracking link for this event, with what it brought")
async def event_links(session: SessionDep, user: ClubAuth, event_id: int, days: int = Query(30, ge=1, le=3650)) -> LinksSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return LinksSummary(**await service.summary(session, event.club, event, days))


@router.get("/events/{event_id}/links/overview", summary="This event's links as the Tracking links page reads them")
async def event_links_overview(session: SessionDep, user: ClubAuth, event_id: int, days: int = Query(30, ge=1, le=3650)) -> LinksOverview:
    event = await deliveries.event_for_club(session, user, event_id)
    return LinksOverview(**await overview_mod.overview(session, event.club, event, days))


@router.get("/events/{event_id}/links/{link_id}/detail", summary="One of this event's links, opened further")
async def event_link_detail(session: SessionDep, user: ClubAuth, event_id: int, link_id: UUID, days: int = Query(30, ge=1, le=3650)) -> LinkDetail:
    event = await deliveries.event_for_club(session, user, event_id)
    return LinkDetail(**await overview_mod.detail(session, event.club, event, link_id, days))


@router.post("/events/{event_id}/links", summary="A new tracking link for this event", status_code=201)
async def create_event_link(session: SessionDep, user: ClubAuth, event_id: int, body: LinkCreate) -> LinkOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return _out(await service.create(session, event.club, event, body, user), event.club)


@router.patch("/events/{event_id}/links/{link_id}", summary="Rename or retag a link; its code never changes")
async def update_event_link(session: SessionDep, user: ClubAuth, event_id: int, link_id: UUID, body: LinkPatch) -> LinkOut:
    event = await deliveries.event_for_club(session, user, event_id)
    link = await service.link_for(session, event.club, event, link_id)
    return _out(await service.update(session, link, body), event.club)


@router.delete("/events/{event_id}/links/{link_id}", summary="Retire a link; what it already brought stays counted")
async def delete_event_link(session: SessionDep, user: ClubAuth, event_id: int, link_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete(session, await service.link_for(session, event.club, event, link_id))
    return {"deleted": True}


# ─── The club's links ─────────────────────────────────────────────────────


@router.get("/clubs/links", summary="Every tracking link to the club page, with the follows it brought")
async def club_links(session: SessionDep, user: ClubAuth, days: int = Query(30, ge=1, le=3650)) -> LinksSummary:
    club = await _club_for(session, user)
    return LinksSummary(**await service.summary(session, club, None, days))


@router.get("/clubs/links/overview", summary="Every link of the club — its page and its events — as the Tracking links page reads them")
async def club_links_overview(session: SessionDep, user: ClubAuth, days: int = Query(30, ge=1, le=3650)) -> LinksOverview:
    club = await _club_for(session, user)
    # A club whose website is up has the site's own link here, from the first look.
    if await service.site_published(session, club.id) and await service.website_link(session, club.id) is None:
        await service.ensure_website_link(session, club)
        await session.commit()
    return LinksOverview(**await overview_mod.overview(session, club, None, days))


@router.get("/clubs/links/{link_id}/detail", summary="One of the club's links, opened further")
async def club_link_detail(session: SessionDep, user: ClubAuth, link_id: UUID, days: int = Query(30, ge=1, le=3650)) -> LinkDetail:
    club = await _club_for(session, user)
    return LinkDetail(**await overview_mod.detail(session, club, None, link_id, days))


@router.post("/clubs/links", summary="A new tracking link to the club page", status_code=201)
async def create_club_link(session: SessionDep, user: ClubAuth, body: LinkCreate) -> LinkOut:
    club = await _club_for(session, user)
    return _out(await service.create(session, club, None, body, user), club)


@router.patch("/clubs/links/{link_id}", summary="Rename or retag a club link")
async def update_club_link(session: SessionDep, user: ClubAuth, link_id: UUID, body: LinkPatch) -> LinkOut:
    club = await _club_for(session, user)
    link = await service.link_for(session, club, None, link_id)
    return _out(await service.update(session, link, body), club)


@router.delete("/clubs/links/{link_id}", summary="Retire a club link")
async def delete_club_link(session: SessionDep, user: ClubAuth, link_id: UUID) -> dict:
    club = await _club_for(session, user)
    await service.delete(session, await service.link_for(session, club, None, link_id))
    return {"deleted": True}


# ─── The short link itself ────────────────────────────────────────────────


@public_router.get("/l/{code}", summary="Open a tracking link: the click is counted, then on to the page")
async def open_link(request: Request, session: SessionDep, code: str) -> RedirectResponse:
    link = await service.by_code(session, code)
    if link is None:
        # A retired or mistyped code still lands somewhere sensible.
        return RedirectResponse(settings.PUBLIC_SITE_URL, status_code=302)
    await service.record_click(session, link, request)
    # `via=l`: this opening is counted already, so the club's site does not count it again.
    to = service.destination(link, link.club.slug if link.club else None) + "&via=l"
    return RedirectResponse(to, status_code=302, headers={"Cache-Control": "no-store"})
