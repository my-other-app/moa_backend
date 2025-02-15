from fastapi import APIRouter
from app.api.users.schemas import User, UserCreate
from app.db.core import SessionDep
from app.api.users import service
from app.core.response.pagination import PaginatedResponse, PaginationParams, paginate
from app.api.clubs.schemas import ClubPublicMin
from app.core.auth.dependencies import DependsAuth

router = APIRouter(prefix="/user")


@router.post("/register", response_model=User, summary="Register a new user")
async def register_user(
    user: UserCreate,
    session: SessionDep,
):
    user = await service.create_user(user, session)
    return user


@router.get("/following", summary="Get all clubs user is following")
async def get_following_clubs(
    session: SessionDep, pagination: PaginationParams, user: DependsAuth
) -> PaginatedResponse[ClubPublicMin]:
    following_clubs = await service.following_clubs(session, user.id)
    return await paginate(following_clubs, ClubPublicMin, pagination, session)
