from fastapi import APIRouter, Depends, Request, UploadFile, Query, File
from app.api.clubs.schemas import (
    ClubCreateUpdateResponse,
    ClubFollowPublic,
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
)
from app.db.core import SessionDep
from app.api.clubs import service
from app.core.auth.dependencies import ClubAuth, DependsAuth, UserAuth, AdminAuth
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from typing import Optional, List

router = APIRouter(prefix="/clubs")


@router.post("/create", summary="create club")
async def create_club(
    session: SessionDep, club: CreateClub = Depends()
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
    return club


@router.post("/admin/create", summary="create club as admin")
async def admin_create_club(
    session: SessionDep, user: AdminAuth, club: CreateClubAdmin
) -> ClubCreateUpdateResponse:
    """Create a club as an admin. A random password will be generated."""
    return await service.create_club(
        session,
        name=club.name,
        email=club.email,
        is_admin_created=True,
    )


@router.put("/update", summary="update club")
async def update_club(
    session: SessionDep, user: ClubAuth, club: UpdateClub = Depends()
) -> ClubCreateUpdateResponse:
    club = await service.update_club(session=session, club_id=user.club.id, club=club)
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
) -> PaginatedResponse[ClubListResponse]:
    """List clubs with optional filters."""
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
    )
    return paginated_response(clubs, request, schema=ClubListResponse)


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
    query = await service.list_notes(session, club_id=club_id)
    return paginated_response(query, request=request, schema=NoteListResponse)


@router.get("/notes/list", summary="list notes (self)")
async def list_notes_self(
    request: Request, session: SessionDep, pagination: PaginationParams, user: ClubAuth
) -> PaginatedResponse[NoteListResponse]:
    query = await service.list_notes(session, club_id=user.club.id)
    return paginated_response(query, request=request, schema=NoteListResponse)


@router.post("/follow/{club_id}", summary="follow club")
async def follow_club(
    club_id: int, session: SessionDep, user: UserAuth
) -> ClubFollowPublic:
    follow = await service.follow_club(session, club_id, user.id)
    return follow


@router.post("/unfollow/{club_id}", summary="un follow club")
async def unfollow_club(
    club_id: int, session: SessionDep, user: UserAuth
) -> ClubFollowPublic:
    follow = await service.unfollow_club(session, club_id, user.id)
    print(follow.is_following)
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


@router.get("/followers/{club_id}")
async def list_club_followers_endpoint(
    request: Request,
    club_id: int,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
):
    """List followers of a club."""
    followers = await service.list_club_followers(
        session=session,
        club_id=club_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(followers, request, schema=ClubFollowPublic)


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
