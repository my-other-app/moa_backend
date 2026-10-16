"""The club's website: the console's side under ClubAuth, and the page and
its images for anyone."""
from urllib.parse import unquote

import boto3
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.sites import service
from app.api.sites.schemas import PreviewOut, SiteOut, SitePatch
from app.config import settings
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/site")
public_router = APIRouter(prefix="/sites")

IMAGE_PREFIXES = ("clubs/",)
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
    s = await service.patch(session, await service.get_or_create(session, club), body.model_dump(exclude_unset=True))
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
    s = await service.publish(session, club, await service.get_or_create(session, club))
    return SiteOut(**await service.out(session, club, s))


@public_router.get("/{slug}", summary="A club's published page", response_class=HTMLResponse)
async def site_page(session: SessionDep, slug: str) -> HTMLResponse:
    html = await service.published_html(session, slug)
    if html is None:
        raise CustomHTTPException(404, message="This club has not published a site")
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=300"})


@public_router.get("/{slug}/i", summary="One of the page's images: a fresh signed link to the bucket")
async def site_image(slug: str, k: str = Query(..., max_length=400)) -> RedirectResponse:
    key = unquote(k)
    if not key.startswith(IMAGE_PREFIXES) or not key.lower().endswith(IMAGE_EXTS) or ".." in key:
        raise CustomHTTPException(404, message="No such image")
    url = boto3.client("s3", aws_access_key_id=settings.S3_ACCESS_KEY, aws_secret_access_key=settings.S3_SECRET_KEY).generate_presigned_url("get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=3600)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "public, max-age=1800"})
