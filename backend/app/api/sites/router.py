"""The club's website: the console's side under ClubAuth, and the page and
its images for anyone."""
import json
from urllib.parse import unquote

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.links import service as links
from app.api.sites import service
from app.api.sites.analytics import site_analytics
from app.api.sites.render import site_csp
from app.api.sites.schemas import PreviewOut, SiteOut, SitePatch
from app.api.subscriptions import gates
from app.config import settings
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import limiter
from app.core.storage import media
from app.core.storage.fields import signed_url
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/site")
public_router = APIRouter(prefix="/sites")

# A club page shows its logo, banner, people and partners (clubs/) and its
# events' posters (events/posters/; clubs/logos/ before Oct 2026).
IMAGE_PREFIXES = ("clubs/", "events/posters/")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


@router.get("", summary="The site's choices, what is available to show, and whether the published page is behind")
async def get_site(session: SessionDep, user: ClubAuth) -> SiteOut:
    club = await _club_for(session, user)
    return SiteOut(**await service.out(session, club, await service.get_or_create(session, club)))


@router.patch("", summary="Template, look, font, headline, subline, sections")
async def patch_site(session: SessionDep, user: ClubAuth, body: SitePatch) -> SiteOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "website", actor=user)
    current = await service.get_or_create(session, club)
    if body.template and body.template != current.template:
        # Choosing a template: the plan allows the first N in catalogue order.
        await gates.check_template(session, club.id, body.template, actor=user)
    s = await service.patch(session, current, body.model_dump(exclude_unset=True))
    return SiteOut(**await service.out(session, club, s))


@router.post("/preview", summary="The page as these choices would make it, without saving or publishing")
async def preview(session: SessionDep, user: ClubAuth, body: SitePatch) -> PreviewOut:
    club = await _club_for(session, user)
    s = await service.get_or_create(session, club)
    choices = service._choices(s)
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "sections" and v is not None:
            choices["sections"] = {**choices["sections"], **{kk: bool(vv) for kk, vv in v.items()}}
        elif k in ("headline", "subline") or v is not None:
            choices[k] = v
    return PreviewOut(html=await service.preview_html(session, club, choices))


@router.post("/publish", summary="Make the page from the saved choices and put it at the public address")
async def publish(session: SessionDep, user: ClubAuth) -> SiteOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "website", actor=user)
    current = await service.get_or_create(session, club)
    # A template chosen on a bigger plan is not published on a smaller one.
    await gates.check_template(session, club.id, current.template, actor=user)
    s = await service.publish(session, club, current)
    return SiteOut(**await service.out(session, club, s))


@router.get("/analytics", summary="What the website brought: visits, who sent them, the sign-ups after")
async def analytics(session: SessionDep, user: ClubAuth, days: int = Query(30, ge=1, le=365)) -> dict:
    club = await _club_for(session, user)
    if await links.site_published(session, club.id) and await links.website_link(session, club.id) is None:
        await links.ensure_website_link(session, club)
        await session.commit()
    return await site_analytics(session, club, days)


@public_router.post("/{slug}/visit", status_code=204, summary="A visit to a club's site, counted for its analytics")
@limiter.limit("60/minute")
async def visit(request: Request, slug: str, session: SessionDep) -> Response:
    """Sent by the page around the site (or the site itself, at its own
    address) as plain text, so a browser sends it without asking first."""
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    await links.record_site_visit(session, slug, request, ref=body.get("ref"), via=body.get("via"), referrer=body.get("referrer"))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@public_router.get("/{slug}", summary="A club's published page", response_class=HTMLResponse)
async def site_page(session: SessionDep, slug: str) -> HTMLResponse:
    html = await service.published_html(session, slug)
    if html is None:
        raise CustomHTTPException(404, message="This club has not published a site")
    # The same policy the club-sites Worker sends, so the page is held to it
    # at this address too (not framed: the API answers X-Frame-Options DENY).
    return HTMLResponse(html, headers={
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": site_csp(settings.PUBLIC_API_URL, frame_ancestors=""),
    })


@public_router.get("/{slug}/i", summary="One of the page's images: its address on the media CDN, or a fresh signed link to the bucket")
async def site_image(slug: str, k: str = Query(..., max_length=400)) -> RedirectResponse:
    key = unquote(k)
    if not key.startswith(IMAGE_PREFIXES) or not key.lower().endswith(IMAGE_EXTS) or ".." in key:
        raise CustomHTTPException(404, message="No such image")
    # A page published before the media CDN was on still points here. With
    # it on, a public picture's address never changes, so the redirect may
    # be kept for a day.
    cdn = media.cdn_url(key)
    if cdn is not None:
        return RedirectResponse(cdn, status_code=302, headers={"Cache-Control": "public, max-age=86400"})
    # The process's one signer (and its cache of links), not a new boto3
    # client per image: making one loads the S3 service model, ~4 ms each.
    # Every image on a club's page is a public picture: signed for
    # PUBLIC_IMAGE_URL_TTL, the same link for days.
    url = signed_url(key, public=True)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "public, max-age=1800"})
