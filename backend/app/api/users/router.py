from typing import List
from fastapi import APIRouter, Body, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from app.api.users.schemas import (
    UserAvatarSelect,
    UserCreate,
    UserInterestSelect,
    UserPrivate,
    # UserProfileCreate,
    UserProfilePublic,
    UserPublic,
)
from app.db.core import SessionDep
from app.api.users import service
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.clubs.schemas import ClubPublicMin
from app.core.auth.dependencies import DependsAuth, UserAuth
from app.core.response import pagination
from app.api.interests.schemas import InterestPublic

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
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: DependsAuth,
) -> PaginatedResponse[ClubPublicMin]:
    following_clubs = await service.following_clubs(
        session, user.id, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(following_clubs, request=request, schema=ClubPublicMin)


@router.post("/profile/create", summary="Create a user profile.")
async def create_user_profile(
    session: SessionDep,
    user: UserAuth,
    whatsapp: str | None = Form(None),
    org_id: int | None = Form(None),
    avatar_id: int | None = Form(None),
    profile_pic: UploadFile | None = File(None),
) -> UserProfilePublic:
    return await service.create_or_update_profile(
        session,
        user_id=user.id,
        whatsapp=whatsapp,
        org_id=org_id,
        avatar_id=avatar_id,
        profile_pic=profile_pic,
    )


@router.get("/profile/me", summary="view self user profile")
async def get_profile(session: SessionDep, user: UserAuth) -> UserPrivate:
    profile = await service.get_user_profile(session=session, user_id=user.id)
    return profile


@router.post("/interests/select", summary="select interests")
async def select_interests(
    session: SessionDep, user: UserAuth, body: UserInterestSelect
) -> List[InterestPublic]:
    await service.select_interests(
        session=session, user_id=user.id, interest_ids=body.interest_ids
    )
    return await service.list_interests(session=session, user_id=user.id)


@router.get("/interests/list", summary="list user interests")
async def list_interests(session: SessionDep, user: UserAuth) -> List[InterestPublic]:
    return await service.list_interests(session=session, user_id=user.id)


@router.post("/avatar/select", summary="select user avatar")
async def select_avatar(
    session: SessionDep, user: UserAuth, avatar: UserAvatarSelect
) -> UserProfilePublic:
    return await service.select_avatar(
        session, user_id=user.id, avatar_id=avatar.avatar_id
    )
