from fastapi import APIRouter, BackgroundTasks, Depends, Request, UploadFile, Query, File
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.response import CustomHTTPException
from app.api.clubs.schemas import (
    ClubTeamMemberOut,
    ClubTeamMemberCreate,
    ClubTeamMemberPatch,
    ClubAdminDetailResponse,
    ClubAdminFullResponse,
    ClubCreateUpdateResponse,
    ClubFollowPublic,
    ClubFollowersListResponse,
    ClubListResponse,
    ClubPublicDetailResponse,
    ClubSocials,
    CreateClub,
    CreateClubAdmin,
    NoteCreate,
    NoteCreateUpdateResponse,
    NoteListResponse,
    ClubSocialsCreate,
    UpdateClub,
    UserClubLinkDetailResponse,
    ClubPartnerCreate,
    ClubPartnerOut,
    ClubPartnerPatch,
    FollowerProfileResponse,
)
from app.db.core import SessionDep
from app.api.clubs import events_list, followers_insights, service
from app.api.discover import geo
from app.api.audit import service as audit
from app.core.auth.dependencies import ClubAuth, DependsAuth, UserAuth, AdminAuth
from app.core.auth.permissions import ContentAuth, DestroyAuth, ModerateAuth
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from typing import Optional, List

from app.api.clubs.schemas import ClubEventsFigures, FollowersInsights, FollowersPage
from app.api.events.schemas import EventListResponseSelf
from app.api.clubs.notifications_router import router as notifications_router

router = APIRouter(prefix="/clubs")
router.include_router(notifications_router, tags=["Club Notifications"])


@router.post("/create", summary="create club")
async def create_club(
    session: SessionDep, background_tasks: BackgroundTasks, club: CreateClub = Depends()
) -> ClubCreateUpdateResponse:
    """Create a club. the club will be now verified, and will be unable to login until the admin verifies it."""
    club = await service.create_club(
        session,
        name=club.name,
        email=club.email,
        password=club.password,
        phone=club.phone,
        logo=club.logo,
        about=club.about,
        org_id=club.org_id,
        location_name=club.location_name,
        interest_ids=club.interest_ids,
    )
    _follow_map_link(background_tasks, club)
    return club


@router.post("/admin/create", summary="create club as admin")
async def admin_create_club(
    session: SessionDep, user: ContentAuth, club: CreateClubAdmin
) -> ClubAdminDetailResponse:
    """Create a club as an admin. A random password will be generated."""
    return await service.create_club(
        session,
        name=club.name,
        email=club.email,
        is_admin_created=True,
    )


@router.get("/overview", summary="Everything the club dashboard shows")
async def club_overview(
    session: SessionDep,
    user: ClubAuth,
    days: int = Query(30, ge=1, le=365, description="Window length in days"),
) -> dict:
    """
    One response for the whole dashboard, so every figure on the screen
    describes the same moment. Assembled from eight calls, a registration
    count from one second can sit beside a revenue total from another and the
    pair will not add up.
    """
    from app.api.clubs import overview as overview_service

    club = (
        await session.execute(select(Clubs).where(Clubs.user_id == user.id))
    ).scalar_one_or_none()
    if not club:
        raise CustomHTTPException(status_code=404, message="No club for this account")

    return await overview_service.get_club_overview(session, club, days=days)


@router.get("/admin/list", summary="list all clubs (admin)")
async def list_all_clubs_admin(
    request: Request,
    session: SessionDep,
    user: AdminAuth,
    pagination: PaginationParams,
    search: Optional[str] = Query(None, description="Search by club name"),
    org_id: Optional[int] = Query(None, description="Filter by organization"),
    is_verified: Optional[bool] = Query(None, description="Approved clubs only"),
    sort: Optional[str] = Query(None, description="created, name or followers"),
    order: Optional[str] = Query(None, description="asc or desc"),
) -> PaginatedResponse[ClubAdminDetailResponse]:
    """
    List all clubs for an admin.

    `org_id` is here so an organisation's page can show its clubs including
    the unverified ones -- the public /clubs/list filter hides those, which
    are exactly the ones an admin needs to see.
    """
    clubs = await service.get_all_clubs(
        session,
        org_id=org_id,
        limit=pagination.limit,
        offset=pagination.offset,
        search=search,
        include_unverified=True,
        is_verified=is_verified,
        sort=sort,
        order=order,
        with_admin_fields=True,
    )
    clubs, total = clubs
    return paginated_response(
        clubs, request, schema=ClubAdminDetailResponse, total=total
    )


@router.post("/{club_id}/approve", summary="Approve a pending club (admin)")
async def approve_club_endpoint(
    club_id: int, session: SessionDep, user: ModerateAuth, request: Request = None
) -> ClubAdminDetailResponse:
    club = await service.approve_club(session, club_id)
    await audit.record(
        session,
        actor=user,
        action="club.approve",
        entity_type="club",
        entity_id=club_id,
        summary=f"Approved club “{club.name}”",
        request=request,
    )
    await session.commit()
    return club


@router.post("/{club_id}/reject", summary="Reject a pending club (admin)")
async def reject_club_endpoint(
    club_id: int, session: SessionDep, user: DestroyAuth, request: Request = None
) -> ClubAdminDetailResponse:
    club = await service.reject_club(session, club_id)
    await audit.record(
        session,
        actor=user,
        action="club.reject",
        entity_type="club",
        entity_id=club_id,
        summary=f"Rejected club “{club.name}” (soft-deleted)",
        request=request,
    )
    await session.commit()
    return club


def _follow_map_link(background_tasks: BackgroundTasks, club) -> None:
    """A club still without a point is placed after the answer: its short map
    link (maps.app.goo.gl…) followed, or else its place's words geocoded."""
    if geo.needs_locating(club):
        background_tasks.add_task(geo.locate_and_store, "club", club.id)


@router.put("/update", summary="update club")
async def update_club(
    session: SessionDep, user: ClubAuth, background_tasks: BackgroundTasks, club: UpdateClub = Depends()
) -> ClubCreateUpdateResponse:
    club.name = user.club.name  # the name is not the club's to change here
    club = await service.update_club(session=session, club_id=user.club.id, club=club)
    _follow_map_link(background_tasks, club)
    return club


@router.get("/list", summary="get all clubs")
async def get_all_clubs(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
    org_id: Optional[int] = Query(None),
    is_following: Optional[bool] = Query(None),
    is_pinned: Optional[bool] = Query(None),
    is_hidden: Optional[bool] = Query(None),
    interest_ids: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search by club name"),
) -> PaginatedResponse[ClubListResponse]:
    """List clubs with optional filters and search."""
    interest_ids = [int(i) for i in (interest_ids.split(",") if interest_ids else [])]
    clubs = await service.get_all_clubs(
        session=session,
        user_id=user.id if user else None,
        org_id=org_id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
        interest_ids=interest_ids,
        is_pinned=is_pinned,
        is_hidden=is_hidden,
        search=search,
    )
    clubs, total = clubs
    return paginated_response(clubs, request, schema=ClubListResponse, total=total)


@router.get("/info/{club_id}", summary="get club info")
async def get_club_details_endpoint(
    club_id: int, session: SessionDep, user: DependsAuth
) -> ClubPublicDetailResponse:
    """Get detailed information about a club."""
    return await service.get_club_details(
        session=session,
        club_id=club_id,
        user_id=user.id if user else None,
    )


@router.get("/info", summary="get club info (self)")
async def get_club_details_self_endpoint(
    session: SessionDep, user: ClubAuth
) -> ClubPublicDetailResponse:
    """Get detailed information about a club."""
    return await service.get_club_details(
        session=session,
        club_id=user.club.id,
        user_id=user.id if user else None,
        enforce_visibility=False,
    )


@router.post("/socials")
async def create_club_socials(
    socials: ClubSocialsCreate,
    session: SessionDep,
    user: ClubAuth,
) -> ClubSocials:
    """Create club social links."""
    return await service.create_or_update_club_socials(
        session, club_id=user.club.id, socials=socials
    )


@router.post("/notes/create", summary="Create note")
async def create_note(
    session: SessionDep, user: ClubAuth, note: NoteCreate
) -> NoteCreateUpdateResponse:
    return await service.create_note(
        session, club_id=user.club.id, user_id=user.id, title=note.title, note=note.note
    )


@router.get("/notes/list/{club_id}", summary="list notes")
async def list_notes(
    request: Request,
    club_id: int,
    session: SessionDep,
    pagination: PaginationParams,
    user: DependsAuth,
) -> PaginatedResponse[NoteListResponse]:
    query = await service.list_notes(
        session, club_id=club_id, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(query, request=request, schema=NoteListResponse)


@router.get("/notes/list", summary="list notes (self)")
async def list_notes_self(
    request: Request, session: SessionDep, pagination: PaginationParams, user: ClubAuth
) -> PaginatedResponse[NoteListResponse]:
    query = await service.list_notes(session, club_id=user.club.id)
    return paginated_response(query, request=request, schema=NoteListResponse)


@router.post("/follow/{club_id}", summary="follow club")
async def follow_club(
    club_id: int, session: SessionDep, user: UserAuth,
    ref: str | None = Query(None, max_length=16, description="A tracking link's code, so the follow is credited to it"),
) -> UserClubLinkDetailResponse:
    follow = await service.follow_club(session, club_id, user.id, tracking_ref=ref)
    return follow


@router.post("/unfollow/{club_id}", summary="un follow club")
async def unfollow_club(
    club_id: int, session: SessionDep, user: UserAuth
) -> UserClubLinkDetailResponse:
    follow = await service.unfollow_club(session, club_id, user.id)
    return follow


@router.post("/pin/{club_id}")
async def toggle_pin_club_endpoint(
    club_id: int,
    session: SessionDep,
    user: UserAuth,
):
    """Toggle pin status for a club."""
    return await service.toggle_pin_club(
        session=session,
        club_id=club_id,
        user_id=user.id,
    )


@router.post("/hide/{club_id}")
async def toggle_hide_club_endpoint(
    club_id: int,
    session: SessionDep,
    user: UserAuth,
):
    """Toggle hide status for a club."""
    return await service.toggle_hide_club(
        session=session,
        club_id=club_id,
        user_id=user.id,
    )


@router.get("/followers/list", summary="The club's followers, with what each did with the club")
async def list_followers_page(
    session: SessionDep,
    user: ClubAuth,
    pagination: PaginationParams,
    segment: str = Query("all", description="all · new · regulars · once · never · quiet"),
    sort: str = Query("newest", description="newest · oldest · active · name"),
    search: Optional[str] = Query(None, max_length=80),
) -> FollowersPage:
    if segment not in followers_insights.SEGMENTS or sort not in followers_insights.SORTS:
        raise CustomHTTPException(422, message="Unknown segment or sort.")
    items, total, counts = await followers_insights.list_followers(session, user.club.id, segment=segment, search=search, sort=sort, limit=pagination.limit, offset=pagination.offset)
    return FollowersPage(items=items, total=total, limit=pagination.limit, offset=pagination.offset, counts=counts)


@router.get("/followers/insights", summary="The audience above the list: growth, who turns up, where from, what they care about")
async def followers_insights_endpoint(session: SessionDep, user: ClubAuth) -> FollowersInsights:
    return FollowersInsights(**await followers_insights.insights(session, user.club.id))


@router.get("/followers/export.csv", summary="The followers in a segment, as a spreadsheet")
async def followers_export(session: SessionDep, user: ClubAuth, segment: str = Query("all")):
    from fastapi.responses import Response

    if segment not in followers_insights.SEGMENTS:
        raise CustomHTTPException(422, message="Unknown segment.")
    body = await followers_insights.export_csv(session, user.club.id, segment=segment)
    return Response(content="\ufeff" + body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="followers-{segment}.csv"'})


@router.get("/followers/{club_id}")
async def list_club_followers_endpoint(
    request: Request,
    club_id: int,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
) -> PaginatedResponse[ClubFollowersListResponse]:
    """List followers of a club, with how many there are in all."""
    followers = await service.list_club_followers(
        session=session,
        club_id=club_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    total = await service.count_club_followers(session=session, club_id=club_id)
    return paginated_response(followers, request, schema=ClubFollowersListResponse, total=total)


@router.get("/followers/{club_id}/{user_id}", summary="One follower: who they are and what they did with the club")
async def follower_profile_endpoint(session: SessionDep, user: ClubAuth, club_id: int, user_id: int) -> FollowerProfileResponse:
    if user.club is None or user.club.id != club_id:
        raise CustomHTTPException(status_code=403, message="Not your club")
    return FollowerProfileResponse(**await service.follower_profile(session, club_id, user_id))


@router.put("/logo")
async def update_club_logo(
    session: SessionDep,
    user: ClubAuth,
    logo: UploadFile = File(...),
):
    """Update club logo."""
    if not logo.content_type.startswith("image/"):
        raise Exception("File must be an image")

    return await service.update_club_logo(session, club_id=user.club.id, logo=logo)


@router.put("/cover", summary="The banner across the top of the club page")
async def update_club_cover(session: SessionDep, user: ClubAuth, cover: UploadFile = File(...)):
    if not (cover.content_type or "").startswith("image/"):
        raise CustomHTTPException(422, message="The cover must be an image")
    return await service.update_club_cover(session, club_id=user.club.id, cover=cover)


# ─── Partners ─────────────────────────────────────────────────────────────


@router.get("/partners", summary="The club's year-round partners")
async def list_partners(session: SessionDep, user: ClubAuth) -> list[ClubPartnerOut]:
    return [ClubPartnerOut(**service._partner_out(p)) for p in await service.list_partners(session, user.club.id)]


@router.post("/partners", summary="Add a partner", status_code=201)
async def create_partner(session: SessionDep, user: ClubAuth, body: ClubPartnerCreate) -> ClubPartnerOut:
    return ClubPartnerOut(**await service.create_partner(session, user.club.id, body.name, body.role, body.since))


@router.patch("/partners/{partner_id}", summary="Correct a partner")
async def update_partner(session: SessionDep, user: ClubAuth, partner_id: int, body: ClubPartnerPatch) -> ClubPartnerOut:
    p = await service.partner_for(session, user.club.id, partner_id)
    return ClubPartnerOut(**await service.update_partner(session, p, body.model_dump(exclude_unset=True)))


@router.put("/partners/{partner_id}/logo", summary="A partner's logo")
async def update_partner_logo(session: SessionDep, user: ClubAuth, partner_id: int, logo: UploadFile = File(...)) -> ClubPartnerOut:
    if not (logo.content_type or "").startswith("image/"):
        raise CustomHTTPException(422, message="The logo must be an image")
    p = await service.partner_for(session, user.club.id, partner_id)
    return ClubPartnerOut(**await service.update_partner_logo(session, p, logo))


# ─── The team ────────────────────────────────────────────────────────────


@router.get("/team", summary="The people who run the club")
async def list_team(session: SessionDep, user: ClubAuth) -> list[ClubTeamMemberOut]:
    return [ClubTeamMemberOut(**service._member_out(m)) for m in await service.list_team(session, user.club.id)]


@router.post("/team", summary="Add a team member", status_code=201)
async def create_member(session: SessionDep, user: ClubAuth, body: ClubTeamMemberCreate) -> ClubTeamMemberOut:
    return ClubTeamMemberOut(**await service.create_member(session, user.club.id, body.model_dump()))


@router.patch("/team/{member_id}", summary="Correct a team member")
async def update_member(session: SessionDep, user: ClubAuth, member_id: int, body: ClubTeamMemberPatch) -> ClubTeamMemberOut:
    m = await service.member_for(session, user.club.id, member_id)
    return ClubTeamMemberOut(**await service.update_member(session, m, body.model_dump(exclude_unset=True)))


@router.put("/team/{member_id}/photo", summary="A team member's photo")
async def update_member_photo(session: SessionDep, user: ClubAuth, member_id: int, photo: UploadFile = File(...)) -> ClubTeamMemberOut:
    if not (photo.content_type or "").startswith("image/"):
        raise CustomHTTPException(422, message="The photo must be an image")
    m = await service.member_for(session, user.club.id, member_id)
    return ClubTeamMemberOut(**await service.update_member_photo(session, m, photo))


@router.delete("/team/{member_id}", summary="Remove a team member")
async def delete_member(session: SessionDep, user: ClubAuth, member_id: int) -> dict:
    await service.delete_member(session, await service.member_for(session, user.club.id, member_id))
    return {"deleted": True}


@router.delete("/partners/{partner_id}", summary="Remove a partner")
async def delete_partner(session: SessionDep, user: ClubAuth, partner_id: int) -> dict:
    await service.delete_partner(session, await service.partner_for(session, user.club.id, partner_id))
    return {"deleted": True}


@router.get("/events/figures", summary="The season above the events list: upcoming, registered, collected, what waits")
async def get_club_events_figures(session: SessionDep, user: ClubAuth) -> ClubEventsFigures:
    return ClubEventsFigures(**await events_list.figures(session, user.club.id))


@router.get("/events/list", summary="Get all events of a club")
async def get_club_events(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: ClubAuth,
    is_ended: Optional[bool] = Query(None, description="Older switch: past (true) or upcoming (false)"),
    scope: str = Query("all", description="upcoming · now · draft · past · all"),
    sort: Optional[str] = Query(None, description="soonest · latest · registered · viewed"),
    search: Optional[str] = Query(None, description="Search by event name"),
) -> PaginatedResponse[EventListResponseSelf]:
    """List events with optional filters."""
    if scope not in events_list.SCOPES or (sort is not None and sort not in events_list.SORTS):
        raise CustomHTTPException(422, message="Unknown scope or sort.")
    events, total = await events_list.list_events(
        session, user.club.id, scope=scope, sort=sort, search=search, limit=pagination.limit, offset=pagination.offset, is_ended=is_ended,
    )
    return paginated_response(events, request, schema=EventListResponseSelf, total=total)


@router.get("/{club_id}/events", summary="Get public events of a club")
async def get_club_events_public(
    club_id: int,
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
    is_ended: Optional[bool] = Query(None, description="Filter by past (true) or upcoming (false) events"),
    search: Optional[str] = Query(None, description="Search by event name"),
) -> PaginatedResponse[EventListResponseSelf]:
    """List public events of a club with optional filters."""
    events, total = await service.get_club_events(
        session=session,
        club_id=club_id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_ended=is_ended,
        search=search,
    )
    return paginated_response(events, request, schema=EventListResponseSelf, total=total)


# ---------------------------------------------------------------------------
# Admin: one club in full, and editing it.
#
# Declared after /admin/list and /admin/create so those literal paths keep
# priority over /admin/{club_id}.
# ---------------------------------------------------------------------------


@router.get("/admin/{club_id}", summary="Get one club in full (admin)")
async def get_club_admin_detail_endpoint(
    club_id: int, session: SessionDep, user: AdminAuth
) -> ClubAdminFullResponse:
    """
    Unlike /clubs/info/{id}, this does not require the club to be verified.
    A pending club is exactly the one an admin needs to look at before
    deciding whether to approve it, and the public endpoint 404s on it.
    """
    return await service.get_club_admin_detail(session, club_id)


@router.put("/admin/{club_id}", summary="Update any club (admin)")
async def update_club_admin(
    club_id: int,
    session: SessionDep,
    user: ContentAuth,
    background_tasks: BackgroundTasks,
    club: UpdateClub = Depends(),
    request: Request = None,
) -> ClubCreateUpdateResponse:
    """
    PUT /clubs/update only ever edits the caller's own club, so an admin had
    no way to correct a club's details. Same handler underneath, with the id
    taken from the path.

    Note it assigns every column from the request and clears the interest
    links before re-adding what it was sent, so callers must send the full
    record rather than a patch.
    """
    updated = await service.update_club(session=session, club_id=club_id, club=club)
    _follow_map_link(background_tasks, updated)
    await audit.record(
        session,
        actor=user,
        action="club.update",
        entity_type="club",
        entity_id=club_id,
        summary=f"Edited club “{updated.name}”",
        request=request,
    )
    await session.commit()
    return updated


@router.delete("/admin/{club_id}", summary="Delete a club (admin)")
async def delete_club_admin(
    club_id: int, session: SessionDep, user: DestroyAuth, request: Request = None
) -> ClubAdminDetailResponse:
    club = await service.delete_club(session, club_id)
    await audit.record(
        session,
        actor=user,
        action="club.delete",
        entity_type="club",
        entity_id=club_id,
        summary=f"Deleted club “{club.name}”",
        request=request,
    )
    await session.commit()
    return club


@router.get("/admin/deleted/list", summary="Deleted clubs (admin)")
async def list_deleted_clubs_admin(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
) -> PaginatedResponse[ClubAdminDetailResponse]:
    """Declared under /admin/deleted so it cannot collide with /admin/{club_id}."""
    clubs, total = await service.list_deleted_clubs(
        session, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(
        clubs, request, schema=ClubAdminDetailResponse, total=total
    )


@router.post("/admin/{club_id}/restore", summary="Restore a deleted club (admin)")
async def restore_club_admin(
    club_id: int, session: SessionDep, user: ModerateAuth, request: Request = None
) -> ClubAdminDetailResponse:
    club = await service.restore_club(session, club_id)
    await audit.record(
        session,
        actor=user,
        action="club.restore",
        entity_type="club",
        entity_id=club_id,
        summary=f"Restored club “{club.name}”",
        request=request,
    )
    await session.commit()
    return club
