from fastapi import APIRouter
from app.api.users.schemas import UserCreate, UserProfileCreate, UserPublic
from app.db.core import SessionDep
from app.api.users import service
from app.core.response.pagination import PaginatedResponse, PaginationParams, paginate
from app.api.clubs.schemas import ClubPublicMin
from app.core.auth.dependencies import DependsAuth
from app.core.response import pagination

router = APIRouter(prefix="/user")


@router.post("/register", response_model=UserPublic, summary="Register a new user")
async def register_user(
    user: UserCreate,
    session: SessionDep,
):
    user = await service.create_user(
        session,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        password=user.password,
    )
    return user


@router.get("/following", summary="Get all clubs user is following")
async def get_following_clubs(
    session: SessionDep, pagination: PaginationParams, user: DependsAuth
) -> PaginatedResponse[ClubPublicMin]:
    following_clubs = await service.following_clubs(session, user.id)
    return await paginate(following_clubs, ClubPublicMin, pagination, session)


@router.post("/profile/create", summary="Create a user profile.")
async def create_user_profile(
    session: SessionDep, profile: UserProfileCreate, user: DependsAuth
):
    return await service.create_or_update_profile(
        session,
        user_id=user.id,
        whatsapp=profile.whatsapp,
        org_id=profile.org_id,
        avatar_id=profile.avatar_id,
    )
