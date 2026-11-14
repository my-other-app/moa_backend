"""Routes of feature-register/profiles-networking-contract.md, under /api/v1."""
from __future__ import annotations

import html
from typing import Literal, Optional

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.profiles import access, admin, export, limits, network, recap, service, stats
from app.api.profiles.schemas import (
    ByCodeOut, LinksIn, MyProfile, NetworkingStats, NetworkingSummary, NetworkPage, NetworkPatch, NetworkPerson,
    ProfilePatch, ProjectIn, ProjectOrder, ProjectOut, ProjectPatch, PublicProfile, QrOut, SavedMePage, SaveIn, SaveOut,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth, OptionalAuth, UserAuth
from app.core.auth.permissions import ModerateAuth
from app.core.storage import formats
from app.db.core import SessionDep
from app.response import CustomHTTPException

me_router = APIRouter(prefix="/me", tags=["profiles"])
profiles_router = APIRouter(prefix="/profiles", tags=["profiles"])
network_router = APIRouter(prefix="/network", tags=["network"])
recap_router = APIRouter(prefix="/network/recap-emails", tags=["network"])
events_router = APIRouter(prefix="/events", tags=["network"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])


# ─── My profile ─────────────────────────────────────────────────────────


@me_router.get("/profile", summary="My profile, as I see it, with my settings")
async def get_my_profile(session: SessionDep, user: UserAuth) -> MyProfile:
    return MyProfile(**await service.my_profile(session, user.id))


@me_router.patch("/profile", summary="Change my profile: handle, texts, organisation, levels and switches")
async def patch_my_profile(body: ProfilePatch, session: SessionDep, user: UserAuth) -> MyProfile:
    """Only what is sent changes. 409 HANDLE_TAKEN, 422 HANDLE_INVALID or
    HANDLE_RESERVED for a handle; 422 ORG_NOT_FOUND for an organisation."""
    return MyProfile(**await service.update_profile(session, user, body))


@me_router.put("/profile/links", summary="My links: the whole ordered list (at most 10)")
async def put_my_links(body: LinksIn, session: SessionDep, user: UserAuth) -> MyProfile:
    """Each URL https:// or http://, a real host, no user name or password,
    at most 500 characters (422 LINK_INVALID, errors naming the link)."""
    return MyProfile(**await service.put_links(session, user, body.links))


@me_router.post("/profile/projects", summary="Add a project (at most 12)")
async def add_my_project(body: ProjectIn, session: SessionDep, user: UserAuth) -> ProjectOut:
    """422 PROJECT_LIMIT past 12; LINK_INVALID for its url."""
    return ProjectOut(**await service.add_project(session, user, body))


@me_router.put("/profile/projects/order", summary="Put my projects in order: every id once")
async def order_my_projects(body: ProjectOrder, session: SessionDep, user: UserAuth) -> MyProfile:
    return MyProfile(**await service.order_projects(session, user, body.ids))


@me_router.patch("/profile/projects/{project_id}", summary="Change a project")
async def edit_my_project(project_id: int, body: ProjectPatch, session: SessionDep, user: UserAuth) -> ProjectOut:
    return ProjectOut(**await service.edit_project(session, user, project_id, body))


@me_router.delete("/profile/projects/{project_id}", summary="Delete a project", status_code=204)
async def delete_my_project(project_id: int, session: SessionDep, user: UserAuth) -> Response:
    if not await service.remove_project(session, user.id, project_id):
        raise CustomHTTPException(404, message="We couldn't find that project.", error_code="NOT_FOUND")
    return Response(status_code=204)


@me_router.post("/profile/projects/{project_id}/image", summary="A project's picture: any picture format, stored resized")
async def set_my_project_image(project_id: int, session: SessionDep, user: UserAuth, file: UploadFile = File(...)) -> ProjectOut:
    if not formats.may_be_picture(file.content_type):
        raise CustomHTTPException(422, message=f"That file is not a picture. Upload a {formats.WORKS} picture.", error_code="NOT_AN_IMAGE")
    return ProjectOut(**await service.set_project_image(session, user, project_id, file))


@me_router.delete("/profile/projects/{project_id}/image", summary="Take a project's picture off")
async def clear_my_project_image(project_id: int, session: SessionDep, user: UserAuth) -> ProjectOut:
    return ProjectOut(**await service.clear_project_image(session, user, project_id))


@me_router.get("/qr", summary="My QR code (made on the first ask)")
async def get_my_qr(session: SessionDep, user: UserAuth) -> QrOut:
    return QrOut(**await service.my_code(session, user.id))


@me_router.post("/qr/replace", summary="A new QR code; the old one stops at once (410)")
async def replace_my_qr(session: SessionDep, user: UserAuth) -> QrOut:
    return QrOut(**await service.replace_code(session, user.id))


# ─── Public profiles (signed in or not) ─────────────────────────────────


@profiles_router.get("/by-code/{code}", summary="The profile a QR code opens")
async def profile_by_code(code: str, request: Request, session: SessionDep, user: OptionalAuth) -> ByCodeOut:
    """404 NOT_FOUND; 410 PROFILE_CODE_INACTIVE (replaced); 403
    PROFILE_SCANNING_OFF. 30 a minute per address (429 RATE_LIMITED)."""
    limits.code_read(request)
    viewer = access.viewer_id_of(user)
    owner, _ = await service.resolve_code(session, code, viewer)
    rel = await access.relation(session, viewer, owner.id)
    return ByCodeOut(handle=owner.username, profile=PublicProfile(**await service.public_profile(session, owner, rel)))


async def _profile_for(request: Request, session, user, handle: str):
    limits.profile_read(request)
    viewer = access.viewer_id_of(user)
    owner = await access.visible_person(session, viewer, handle=handle)
    rel = await access.relation(session, viewer, owner.id)
    return owner, await service.public_profile(session, owner, rel)


@profiles_router.get("/{handle}", summary="A person's profile, as this viewer may see it")
async def profile_by_handle(handle: str, request: Request, session: SessionDep, user: OptionalAuth) -> PublicProfile:
    """Anyone, signed in or not; richer for the people the owner met.
    404 NOT_FOUND for unknown, deleted, suspended, hidden or blocked either
    way. 120 a minute per address (429 RATE_LIMITED)."""
    _, profile = await _profile_for(request, session, user, handle)
    return PublicProfile(**profile)


@profiles_router.get("/{handle}/vcard", summary="A person's contact card (vCard 3.0)", response_class=Response)
async def profile_vcard(handle: str, request: Request, session: SessionDep, user: OptionalAuth) -> Response:
    owner, profile = await _profile_for(request, session, user, handle)
    body = export.profile_vcard(profile, service.profile_url(owner.username))
    return Response(
        content=body, media_type="text/vcard; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{owner.username}.vcf"', "Cache-Control": "private, no-store"},
    )


# ─── The Network ────────────────────────────────────────────────────────


@network_router.post("", summary="Save a person, by their QR code or handle")
async def save_person(body: SaveIn, session: SessionDep, user: UserAuth) -> SaveOut:
    """`met_at` (an offline scan's time) is kept up to 72 hours back, else
    now. `event_id` is kept only when both are checked in to it and the
    time falls in its networking window. 422 CANNOT_SAVE_SELF, 410
    PROFILE_CODE_INACTIVE, 403 PROFILE_SCANNING_OFF, 404 NOT_FOUND, 429
    RATE_LIMITED (60 a minute, 300 an hour)."""
    return SaveOut(**await network.save(session, user, body))


@network_router.get("", summary="My network: search, filter, sort, a page at a time")
async def list_network(
    session: SessionDep,
    user: UserAuth,
    q: Optional[str] = Query(None, max_length=100),
    event_id: Optional[int] = Query(None),
    tag: Optional[str] = Query(None, max_length=24),
    mutual: Optional[bool] = Query(None),
    sort: Literal["recent", "name", "met"] = Query("recent"),
    order: Optional[Literal["asc", "desc"]] = Query(None),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=network.PAGE_SIZE_MAX),
) -> NetworkPage:
    return NetworkPage(**await network.list_people(
        session, user.id, q=q, event_id=event_id, tag=tag, mutual=mutual, sort=sort, order=order, page=page, page_size=page_size,
    ))


@network_router.get("/saved-me", summary="People who saved me that I haven't saved")
async def saved_me(
    session: SessionDep, user: UserAuth,
    event_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=network.PAGE_SIZE_MAX),
) -> SavedMePage:
    return SavedMePage(**await network.saved_me(session, user.id, event_id=event_id, page=page, page_size=page_size))


@network_router.get("/export", summary="Everyone in my network, as vCards or a CSV", response_class=Response)
async def export_network(session: SessionDep, user: UserAuth, format: Literal["vcf", "csv"] = Query("vcf")) -> Response:
    items = await network.everyone(session, user.id)
    if format == "csv":
        body, media, name = export.network_csv(items), "text/csv; charset=utf-8", "network.csv"
    else:
        body, media, name = export.network_vcards(items), "text/vcard; charset=utf-8", "network.vcf"
    return Response(content=body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store"})


@network_router.get("/{network_id}", summary="One person in my network, with every encounter")
async def get_network_person(network_id: int, session: SessionDep, user: UserAuth) -> NetworkPerson:
    return NetworkPerson(**await network.get_person(session, user.id, network_id))


@network_router.patch("/{network_id}", summary="My note and tags on a person")
async def edit_network_person(network_id: int, body: NetworkPatch, session: SessionDep, user: UserAuth) -> NetworkPerson:
    return NetworkPerson(**await network.edit_person(session, user.id, network_id, body))


@network_router.delete("/{network_id}", summary="Remove a person (they are not told)", status_code=204)
async def remove_network_person(network_id: int, session: SessionDep, user: UserAuth) -> Response:
    await network.remove(session, user, network_id)
    return Response(status_code=204)


# ─── The recap email's link (no sign-in: the link is signed) ────────────


def _page(title: str, body: str, form: Optional[str] = None) -> HTMLResponse:
    esc = html.escape
    button = (
        f'<form method="post" action="{esc(form, quote=True)}"><button type="submit" '
        'style="background:#2C333D;color:#fff;border:0;padding:10px 18px;border-radius:6px;font-weight:600">Stop recap emails</button></form>'
        if form else ""
    )
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{esc(title)}</title></head>'
        '<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#2C333D;max-width:480px;margin:40px auto;padding:0 16px">'
        f"<h1 style=\"font-size:20px\">{esc(title)}</h1><p>{esc(body)}</p>{button}</body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@recap_router.get("/unsubscribe", summary="The recap email's unsubscribe page", response_class=HTMLResponse, include_in_schema=False)
async def recap_unsubscribe_page(request: Request, u: int = Query(...), t: str = Query(..., max_length=64)) -> HTMLResponse:
    """A page with the button (a link scanner opening the link changes nothing)."""
    if not recap.unsubscribe_ok(u, t):
        return _page("This link doesn't work", "Turn recap emails off in the app: Profile, Settings.")
    return _page("Stop recap emails?", "You won't get the morning-after email listing the people you met at an event.", form=f"{request.url.path}?{request.url.query}")


@recap_router.post("/unsubscribe", summary="Turn recap emails off (the email's link, one-click too)", response_class=HTMLResponse, include_in_schema=False)
async def recap_unsubscribe(session: SessionDep, u: int = Query(...), t: str = Query(..., max_length=64)) -> HTMLResponse:
    if not await recap.unsubscribe(session, u, t):
        return _page("This link doesn't work", "Turn recap emails off in the app: Profile, Settings.")
    return _page("Recap emails are off", "You can turn them back on in the app: Profile, Settings.")


# ─── The club's numbers ─────────────────────────────────────────────────


@events_router.get("/{event_id}/networking/stats", summary="An event's networking, in numbers (club)")
async def networking_stats(event_id: int, session: SessionDep, user: ClubAuth) -> NetworkingStats:
    """The host's team (owner, admin, editor, door) and a co-host's; counts, never names."""
    from app.api.events.access import ensure_event_access, is_admin_user
    from app.api.events.models import Events

    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        raise CustomHTTPException(404, message="Event not found", error_code="EVENT_NOT_FOUND")
    ensure_event_access(event, user.id, as_admin=is_admin_user(user))
    return NetworkingStats(**await stats.event_stats(session, event_id))


# ─── Admin ──────────────────────────────────────────────────────────────


@admin_router.get("/networking/summary", summary="Profiles and the Network, in numbers (admin)")
async def networking_summary(session: SessionDep, user: AdminAuth) -> NetworkingSummary:
    return NetworkingSummary(**await stats.summary(session))


@admin_router.delete("/profiles/{user_id}/links/{link_id}", summary="Take a link off a profile (admin)", status_code=204)
async def admin_remove_link(user_id: int, link_id: int, request: Request, session: SessionDep, user: ModerateAuth) -> Response:
    await admin.remove_link(session, user, user_id, link_id, request=request)
    return Response(status_code=204)


@admin_router.delete("/profiles/{user_id}/projects/{project_id}", summary="Take a project off a profile (admin)", status_code=204)
async def admin_remove_project(user_id: int, project_id: int, request: Request, session: SessionDep, user: ModerateAuth) -> Response:
    await admin.remove_project(session, user, user_id, project_id, request=request)
    return Response(status_code=204)


@admin_router.post("/profiles/{user_id}/unhide", summary="Show a hidden profile again (admin)", status_code=204)
async def admin_unhide(user_id: int, request: Request, session: SessionDep, user: ModerateAuth) -> Response:
    await admin.unhide(session, user, user_id, request=request)
    return Response(status_code=204)
