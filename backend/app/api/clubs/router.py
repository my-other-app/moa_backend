from typing import Annotated
from fastapi import APIRouter, Depends
from app.api.clubs.schemas import CreateClub, ClubPublic, EditClub
from app.db.core import SessionDep
from app.api.clubs import service
from app.core.auth.dependencies import DependsAuth
from app.core.response.pagination import (
    PaginationParams,
    get_pagination_params,
    paginate,
)

router = APIRouter(prefix="/clubs")


@router.post("/create", summary="create club")
async def create_club(
    club: CreateClub, session: SessionDep, user: DependsAuth
) -> ClubPublic:
    club = await service.create_club(club, user.id, session)
    return club


@router.put("/update", summary="update club")
async def update_club(
    club: EditClub, session: SessionDep, user: DependsAuth
) -> ClubPublic:
    club = await service.update_club(club, session, user.id)
    return club


@router.get("/info/{club_id}", summary="get club info")
async def get_club(club_id: int, session: SessionDep, user: DependsAuth) -> ClubPublic:
    club = await service.get_club(club_id, session)
    return club


@router.get("/list", summary="get all clubs")
async def get_all_clubs(
    session: SessionDep,
    user: DependsAuth,
    params: PaginationParams,
    org_id: int | None = None,
):
    clubs = await service.get_all_clubs(session, org_id)
    return await paginate(clubs, ClubPublic, params, session)
