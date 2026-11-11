"""Social posting's routes (feature-register/social-posting-contract.md).

- /clubs/social/...: the club dashboard. Roles (club_access.policy): door
  staff none; editors draft, schedule (approval off), see results; owners
  and admins also connect, disconnect, change the approval setting and
  approve. A co-host acts on a shared event with its own accounts and posts.
- /social/oauth/{platform}/callback: where each platform sends the browser
  back after sign-in (public; the state is the proof).
- /social/meta/deauthorize, /social/meta/data-deletion: Meta's callbacks
  (public; Meta's signed_request is the proof); /social/data-deletion/{code}:
  the status page's read.
- /social/share-kit/{event_id}, /social/share-kits: the team's share kit,
  for a club user or an app user on the team.
- /events/{event_id}/share: the attendees' share row (public).
"""
from __future__ import annotations

from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Path, Query, Request
from fastapi.responses import RedirectResponse

from app.api.social import accounts as accounts_mod
from app.api.social import alerts, meta_hooks, oauth, platforms, posters, service, sharekit
from app.api.social import sweep as _sweep  # noqa: F401 - registers the daily job's handler
from app.api.social.access import for_club, for_event
from app.api.social.models import SIZES
from app.api.social.schemas import (
    AccountOut, CampaignIn, CampaignOut, ChoiceIn, ChoiceOut, ConnectIn, ConnectOut, DeletionOut, DeletionStatusOut, DraftsOut,
    EventPostsOut, Platform, PlatformOut, PostIn, PostOut, PostPatch, PosterIn, PosterOut, PreviewIn, PreviewOut, RewriteIn,
    RewriteOut, SettingsIn, SettingsOut, ShareKitListItem, ShareKitOut, ShareRowItem, Size,
)
from app.api.users.models import Users
from app.core.auth.dependencies import ClubAuth, check_user_type
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/social", tags=["social"])
public_router = APIRouter(prefix="/social", tags=["social"])
events_public_router = APIRouter(tags=["social"])

#: A club's account, or an app user (a team member on the app), or an admin.
KitAuth = Annotated[Users, Depends(check_user_type(["app_user", "club", "admin"]))]


# ─── Platforms and accounts ──────────────────────────────────────────────


@club_router.get("/platforms", summary="Each platform: whether the club can connect and post to it now, and if not why")
async def list_platforms(request: Request, session: SessionDep, user: ClubAuth) -> list[PlatformOut]:
    who = await for_club(session, request, user)
    return [PlatformOut(**p) for p in await platforms.listing(session, who.club.id, actor=user)]


@club_router.get("/accounts", summary="The club's connected social accounts")
async def list_accounts(request: Request, session: SessionDep, user: ClubAuth) -> list[AccountOut]:
    who = await for_club(session, request, user)
    rows = await accounts_mod.of_club(session, who.club.id)
    waiting = await accounts_mod.waiting_counts(session, [a.id for a in rows])
    return [AccountOut(**accounts_mod.out(a, waiting.get(a.id, 0))) for a in rows]


@club_router.post("/accounts/{platform}/connect", summary="Start connecting an account: the platform's sign-in address")
@limiter.limit("20/minute", key_func=account_or_address)
async def connect(request: Request, session: SessionDep, user: ClubAuth, platform: Platform, body: ConnectIn) -> ConnectOut:
    who = await for_club(session, request, user)
    return ConnectOut(authorize_url=await oauth.start(session, who, platform, body.return_to, request))


@club_router.get("/accounts/pending/{state_id}", summary="The Pages or accounts a sign-in found, to pick one")
async def pending(request: Request, session: SessionDep, user: ClubAuth, state_id: UUID) -> list[ChoiceOut]:
    who = await for_club(session, request, user)
    return [ChoiceOut(**c) for c in await oauth.choices(session, who, state_id)]


@club_router.post("/accounts/pending/{state_id}", summary="Connect the Page or account picked")
async def choose(request: Request, session: SessionDep, user: ClubAuth, state_id: UUID, body: ChoiceIn) -> AccountOut:
    who = await for_club(session, request, user)
    account = await oauth.choose(session, who, state_id, body.external_id)
    return AccountOut(**accounts_mod.out(account))


@club_router.delete("/accounts/{account_id}", summary="Disconnect an account: access revoked where the platform allows, tokens deleted")
async def disconnect(request: Request, session: SessionDep, user: ClubAuth, account_id: UUID) -> dict:
    who = await for_club(session, request, user)
    account = await accounts_mod.get_for_club(session, who.club.id, account_id)
    await accounts_mod.disconnect(session, account)
    await alerts.record(session, actor=who.actor, action="disconnect", club_id=who.club.id, request=request,
                        summary=f"Disconnected {alerts.LABELS.get(account.platform, account.platform)}: {account.name or account.handle or ''}",
                        details={"account_id": str(account.id), "platform": account.platform})
    await session.commit()
    return {"disconnected": True}


@club_router.get("/settings", summary="The club's social posting settings")
async def get_settings(request: Request, session: SessionDep, user: ClubAuth) -> SettingsOut:
    who = await for_club(session, request, user)
    return SettingsOut(**await service.settings_of(session, who.club.id))


@club_router.put("/settings", summary="Change the approval setting (owners and admins)")
async def put_settings(request: Request, session: SessionDep, user: ClubAuth, body: SettingsIn) -> SettingsOut:
    who = await for_club(session, request, user)
    return SettingsOut(**await service.save_settings(session, who, body.approval_required, request))


# ─── An event's posts ────────────────────────────────────────────────────


@club_router.get("/events/{event_id}/posts", summary="The event's posts by this club, with each platform's result and what each brought")
async def event_posts(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> EventPostsOut:
    who, event = await for_event(session, request, user, event_id)
    return EventPostsOut(**await service.event_posts(session, who, event))


@club_router.get("/events/{event_id}/drafts", summary="A drafted post per platform: text, hashtags, poster size and tracking link")
async def drafts(request: Request, session: SessionDep, user: ClubAuth, event_id: int,
                 platforms_: Optional[str] = Query(None, alias="platforms", max_length=100, description="Comma-separated; empty: all")) -> DraftsOut:
    who, event = await for_event(session, request, user, event_id)
    wanted = [p.strip() for p in platforms_.split(",") if p.strip()] if platforms_ else None
    return DraftsOut(**await service.drafts(session, who, event, wanted))


@club_router.post("/events/{event_id}/drafts/rewrite", summary="Rewrite a post for a platform with the club's AI assist")
@limiter.limit("20/minute", key_func=account_or_address)
async def rewrite(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: RewriteIn) -> RewriteOut:
    who, event = await for_event(session, request, user, event_id)
    return RewriteOut(**await service.rewrite(session, request, who, event, body.platform, body.text, body.tone))


@club_router.post("/events/{event_id}/preview", summary="The post as it will read, with the platform's warnings")
async def preview(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: PreviewIn) -> PreviewOut:
    who, event = await for_event(session, request, user, event_id)
    return PreviewOut(**await service.preview(session, who, event, body.platform, body.text, body.variant_id))


@club_router.post("/events/{event_id}/posts", summary="A new post (scheduled, or `draft: true` to save only)", status_code=201)
async def create_post(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: PostIn) -> PostOut:
    who, event = await for_event(session, request, user, event_id)
    data = body.model_dump()
    data["trigger"] = body.trigger.stored()
    return PostOut(**await service.create_post(session, who, event, data, request))


@club_router.post("/events/{event_id}/posts/campaign", summary="Announce it: the standard Announcement and Reminder from the drafts", status_code=201)
async def campaign(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: CampaignIn) -> CampaignOut:
    who, event = await for_event(session, request, user, event_id)
    return CampaignOut(**await service.campaign(session, who, event, list(body.platforms), list(body.account_ids), request))


@club_router.get("/events/{event_id}/posters", summary="The event's poster in each platform's size")
async def get_posters(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> list[PosterOut]:
    who, event = await for_event(session, request, user, event_id)
    made = await posters.ensure(session, event)
    await session.commit()
    return [PosterOut(**posters.out(made[s])) for s in SIZES if s in made]


@club_router.put(
    "/events/{event_id}/posters/{size}",
    summary="One size: crop or fit, the focus point, the background -- or the club's own picture (multipart `file`)",
    openapi_extra={"requestBody": {"content": {
        "application/json": {"schema": PosterIn.model_json_schema()},
        "multipart/form-data": {"schema": {"type": "object", "properties": {"file": {"type": "string", "format": "binary"},
                                                                              "mode": {"type": "string", "enum": ["crop", "fit"]}}}},
    }}},
)
async def put_poster(request: Request, session: SessionDep, user: ClubAuth, event_id: int, size: Size) -> PosterOut:
    who, event = await for_event(session, request, user, event_id)
    kind = (request.headers.get("content-type") or "").lower()
    if kind.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise CustomHTTPException(422, message="Add the picture as `file`.", error_code="IMAGE_MISSING")
        raw = await upload.read(20 * 1024 * 1024 + 1)
        mode = form.get("mode") if form.get("mode") in ("crop", "fit") else "crop"
        row = await posters.set_own(session, event, size, raw, mode=mode)
    else:
        try:
            body = PosterIn.model_validate(await request.json())
        except Exception:  # noqa: BLE001 - not JSON, or not this shape
            raise CustomHTTPException(422, message="Send {mode, focus?, background?}, or a picture as multipart `file`.",
                                      error_code="POSTER_INVALID")
        focus = (body.focus.get("x", 0.5), body.focus.get("y", 0.5)) if body.focus else None
        row = await posters.set_mode(session, event, size, mode=body.mode, focus=focus, background=body.background)
    await session.commit()
    return PosterOut(**posters.out(row))


# ─── One post ────────────────────────────────────────────────────────────


@club_router.patch("/posts/{post_id}", summary="Change a post's name, trigger or texts (not what already went out)")
async def update_post(request: Request, session: SessionDep, user: ClubAuth, post_id: UUID, body: PostPatch) -> PostOut:
    who = await for_club(session, request, user)
    post = await service.post_for(session, who, post_id)
    data = body.model_dump(exclude_unset=True)
    if body.trigger is not None:
        data["trigger"] = body.trigger.stored()
    return PostOut(**await service.update_post(session, who, post, data, request))


@club_router.post("/posts/{post_id}/schedule", summary="Put a draft or paused post on the clock (or ask approval)")
async def schedule(request: Request, session: SessionDep, user: ClubAuth, post_id: UUID) -> PostOut:
    who = await for_club(session, request, user)
    return PostOut(**await service.schedule(session, who, await service.post_for(session, who, post_id), request))


@club_router.post("/posts/{post_id}/approve", summary="Approve an editor's post (owners and admins)")
async def approve(request: Request, session: SessionDep, user: ClubAuth, post_id: UUID) -> PostOut:
    who = await for_club(session, request, user)
    return PostOut(**await service.approve(session, who, await service.post_for(session, who, post_id), request))


@club_router.post("/posts/{post_id}/cancel", summary="Cancel what of a post has not gone out")
async def cancel(request: Request, session: SessionDep, user: ClubAuth, post_id: UUID) -> PostOut:
    who = await for_club(session, request, user)
    return PostOut(**await service.cancel(session, who, await service.post_for(session, who, post_id), request))


@club_router.delete("/posts/{post_id}", summary="Delete a draft")
async def delete_post(request: Request, session: SessionDep, user: ClubAuth, post_id: UUID) -> dict:
    who = await for_club(session, request, user)
    await service.delete_post(session, who, await service.post_for(session, who, post_id))
    return {"deleted": True}


@club_router.post("/targets/{target_id}/retry", summary="Send a failed, held or skipped post to its platform again")
async def retry(request: Request, session: SessionDep, user: ClubAuth, target_id: UUID) -> PostOut:
    who = await for_club(session, request, user)
    return PostOut(**await service.retry(session, who, await service.target_for(session, who, target_id), request))


# ─── The platforms' callbacks ────────────────────────────────────────────


@public_router.get("/oauth/{platform}/callback", summary="Where a platform sends the browser back after sign-in", include_in_schema=True)
@limiter.limit("60/minute")
async def oauth_callback(request: Request, session: SessionDep, platform: Platform) -> RedirectResponse:
    params = {k: request.query_params.get(k) for k in ("code", "state", "error", "error_description")}
    to = await oauth.callback(session, platform, params)
    return RedirectResponse(to, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.post("/meta/deauthorize", summary="Meta: a person removed MyOtherApp; their Pages and Instagram accounts are disconnected")
@limiter.limit("60/minute")
async def meta_deauthorize(request: Request, session: SessionDep, signed_request: Optional[str] = Form(None)) -> dict:
    return {"disconnected": await meta_hooks.deauthorize(session, signed_request)}


@public_router.post("/meta/data-deletion", summary="Meta: delete what we keep of a person's Pages and Instagram accounts")
@limiter.limit("60/minute")
async def meta_data_deletion(request: Request, session: SessionDep, signed_request: Optional[str] = Form(None)) -> DeletionOut:
    return DeletionOut(**await meta_hooks.data_deletion(session, signed_request))


@public_router.get("/data-deletion/{code}", summary="A data deletion request's status, for the public status page")
@limiter.limit("60/minute")
async def deletion_status(request: Request, session: SessionDep, code: str = Path(..., max_length=40)) -> DeletionStatusOut:
    return DeletionStatusOut(**await meta_hooks.deletion_status(session, code))


# ─── The share kit ───────────────────────────────────────────────────────


@public_router.get("/share-kit/{event_id}", summary="The event's share kit: each platform's text, poster and link")
async def share_kit(session: SessionDep, user: KitAuth, event_id: int) -> ShareKitOut:
    return ShareKitOut(**await sharekit.kit_for(session, user, event_id))


@public_router.get("/share-kits", summary="Recent published events of the clubs whose team you are on")
async def share_kits(session: SessionDep, user: KitAuth) -> list[ShareKitListItem]:
    return [ShareKitListItem(**e) for e in await sharekit.recent_for(session, user)]


@events_public_router.get("/events/{event_id}/share", summary="The attendees' share row: each platform's words and its own tracking link")
@limiter.limit("120/minute")
async def share_row(request: Request, session: SessionDep, event_id: int) -> list[ShareRowItem]:
    return [ShareRowItem(**r) for r in await sharekit.share_row(session, event_id)]
