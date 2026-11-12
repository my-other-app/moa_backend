"""The club's website: the console's side under ClubAuth, and the page,
its images and the templates' gallery pictures for anyone."""
import json
import re
from urllib.parse import unquote

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.links import service as links
from app.api.sites import images as library
from app.api.sites import registry, service
from app.api.sites.analytics import site_analytics
from app.api.sites.render import site_csp
from app.api.sites.schemas import PreviewOut, SiteImageOrder, SiteImageOut, SiteImagePatch, SiteOut, SitePatch
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

# A club page shows its logo, banner, people, partners and photo library
# (clubs/), its events' posters (events/posters/; clubs/logos/ before Oct
# 2026) and its speakers' photos (events/speakers/).
IMAGE_PREFIXES = ("clubs/", "events/posters/", "events/speakers/")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


async def _club_for(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="No club for this account")
    return club


@router.get("", summary="The site's choices, the templates, the photo library, and whether the published page is behind")
async def get_site(session: SessionDep, user: ClubAuth) -> SiteOut:
    club = await _club_for(session, user)
    return SiteOut(**await service.out(session, club, await service.get_or_create(session, club)))


@router.patch("", summary="Template, layout (section order, hidden, titles, photo slots, accent), headline, subline")
async def patch_site(session: SessionDep, user: ClubAuth, body: SitePatch) -> SiteOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "website", actor=user)
    current = await service.get_or_create(session, club)
    if body.template and body.template != current.template:
        # Choosing a template: the plan allows the first N in catalogue order.
        await gates.check_template(session, club.id, body.template, actor=user)
    s, adjusted = await service.patch(session, club, current, body.changes())
    return SiteOut(**await service.out(session, club, s, adjusted))


@router.post("/preview", summary="The page as these choices would make it, without saving or publishing")
@limiter.limit("60/minute")
async def preview(request: Request, session: SessionDep, user: ClubAuth, body: SitePatch) -> PreviewOut:
    club = await _club_for(session, user)
    s = await service.get_or_create(session, club)
    chosen, _ = await service.apply_choices(session, club.id, service.choices(s), body.changes())
    return PreviewOut(html=await service.preview_html(session, club, chosen))


@router.post("/publish", summary="Make the page from the saved choices and put it at the public address")
async def publish(session: SessionDep, user: ClubAuth) -> SiteOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "website", actor=user)
    current = await service.get_or_create(session, club)
    # A template chosen on a bigger plan is not published on a smaller one.
    await gates.check_template(session, club.id, current.template, actor=user)
    s = await service.publish(session, club, current)
    return SiteOut(**await service.out(session, club, s))


# ─── The photo library ───────────────────────────────────────────────────


async def _image_out(session, club, row) -> SiteImageOut:
    view = await service.library_view(session, club, await service.get_or_create(session, club))
    return SiteImageOut(**next(i for i in view if i["id"] == row.id))


@router.post("/images", status_code=201, summary="Add a photo to the site's library (JPEG, PNG or WebP, up to 10 MB; 20 at most)")
async def add_image(session: SessionDep, user: ClubAuth, file: UploadFile = File(...), alt: str | None = Form(None)) -> SiteImageOut:
    club = await _club_for(session, user)
    await gates.require_feature(session, club.id, "website", actor=user)
    s = await service.get_or_create(session, club)
    row = await library.upload(session, s, file, alt)
    return await _image_out(session, club, row)


@router.patch("/images/{image_id}", summary="A library photo's alt text")
async def patch_image(session: SessionDep, user: ClubAuth, image_id: int, body: SiteImagePatch) -> SiteImageOut:
    club = await _club_for(session, user)
    row = await library.one(session, club.id, image_id)
    if "alt" in body.model_fields_set:
        row = await library.set_alt(session, row, body.alt)
    return await _image_out(session, club, row)


@router.put("/images/order", summary="The library's order: every photo's id, first to last")
async def order_images(session: SessionDep, user: ClubAuth, body: SiteImageOrder) -> list[SiteImageOut]:
    club = await _club_for(session, user)
    await library.reorder(session, club.id, body.ids)
    return [SiteImageOut(**i) for i in await service.library_view(session, club, await service.get_or_create(session, club))]


@router.delete("/images/{image_id}", summary="Delete a library photo: out of every slot, off the live page at once")
async def delete_image(session: SessionDep, user: ClubAuth, image_id: int) -> dict:
    club = await _club_for(session, user)
    row = await library.one(session, club.id, image_id)
    s = await service.get_or_create(session, club)
    shown = await library.delete(session, s, row)
    republished = await service.republish_for_club(session, club.id) if shown else False
    return {"deleted": True, "republished": republished}


@router.get("/analytics", summary="What the website brought: visits, who sent them, the sign-ups after")
async def analytics(session: SessionDep, user: ClubAuth, days: int = Query(30, ge=1, le=365)) -> dict:
    club = await _club_for(session, user)
    if await links.site_published(session, club.id) and await links.website_link(session, club.id) is None:
        await links.ensure_website_link(session, club)
        await session.commit()
    return await site_analytics(session, club, days)


# ─── For anyone ──────────────────────────────────────────────────────────

#: The website templates' gallery pictures, at the root beside the document
#: designs' (app/core/doc_designs/static.py): /static/site-templates/<key>.png
#: and <key>-mobile.png, from the package's thumb.png and thumb-mobile.png.
#: Linked with ?v=<the file's hash> (service.template_meta), so immutable.
static_router = APIRouter()
STATIC_PREFIX = "/static/site-templates"
_THUMB = re.compile(r"^([a-z][a-z0-9_-]{0,19}?)(-mobile)?$")
_STATIC_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable", "Cross-Origin-Resource-Policy": "cross-origin"}


@static_router.api_route(STATIC_PREFIX + "/{name}.png", methods=["GET", "HEAD"], include_in_schema=False)
async def template_thumb(name: str):
    m = _THUMB.match(name)
    pkg = registry.get(m.group(1)) if m else None
    path = pkg.thumb("mobile" if m.group(2) else "desktop") if pkg is not None else None
    if path is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(path, media_type="image/png", headers=_STATIC_HEADERS)


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
    """X-Site-Slug names the club's current slug: asked by one it had once
    (app/api/slugs), the club-sites Worker 301s that subdomain to the
    current one's instead of serving the page there."""
    found = await service.published_page(session, slug)
    if found is None:
        raise CustomHTTPException(404, message="This club has not published a site")
    html, current = found
    # The same policy the club-sites Worker sends, so the page is held to it
    # at this address too (not framed: the API answers X-Frame-Options DENY).
    return HTMLResponse(html, headers={
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": site_csp(settings.PUBLIC_API_URL, frame_ancestors=""),
        "X-Site-Slug": current,
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
