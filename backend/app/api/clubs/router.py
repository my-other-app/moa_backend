from fastapi import APIRouter, Depends, Request, UploadFile
from app.api.clubs.schemas import (
    ClubFollowPublic,
    CreateClub,
    ClubPublic,
    EditClub,
    NoteCreate,
    NotePublic,
)
from app.db.core import SessionDep
from app.api.clubs import service
from app.core.auth.dependencies import ClubAuth, DependsAuth, UserAuth
from app.core.response.pagination import (
    PaginationParams,
    paginated_response,
)

router = APIRouter(prefix="/clubs")


@router.post("/create", summary="create club")
async def create_club(session: SessionDep, club: CreateClub = Depends()) -> ClubPublic:
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
    )
    return club


@router.put("/update", summary="update club")
async def update_club(
    session: SessionDep, user: ClubAuth, club: EditClub = Depends()
) -> ClubPublic:
    club = await service.update_club(club, session, user.id)
    return club


@router.get("/info/{club_id}", summary="get club info")
async def get_club(club_id: int, session: SessionDep, user: DependsAuth) -> ClubPublic:
    club = await service.get_club(club_id, session)
    return club


@router.get("/list", summary="get all clubs")
async def get_all_clubs(
    request: Request,
    session: SessionDep,
    user: DependsAuth,
    params: PaginationParams,
    org_id: int | None = None,
):
    clubs = await service.get_all_clubs(
        session, org_id, limit=params.limit, offset=params.offset
    )
    return paginated_response(clubs, request=request, schema=ClubPublic)


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


@router.post("/notes/{club_id}/create", summary="Create note")
async def create_note(
    club_id: int, session: SessionDep, user: ClubAuth, note: NoteCreate
) -> NotePublic:
    return await service.create_note(
        session, club_id=club_id, user_id=user.id, title=note.title, note=note.note
    )


@router.get("/notes/{club_id}/list", summary="list notes")
async def list_notes(
    request: Request, club_id: int, session: SessionDep, pagination: PaginationParams
):
    query = await service.list_notes(session, club_id=club_id)
    return paginated_response(query, request=request, schema=NotePublic)
