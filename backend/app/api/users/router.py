from typing import List
from fastapi import APIRouter, File, Form, Query, Request, UploadFile

from app.api.users.schemas import (
    FCMTokenRequest,
    FCMTokenResponse,
    UserAvatarDetail,
    UserAvatarSelect,
    UserCreate,
    UserCreateResponse,
    UserDetailResponse,
    UserInterestSelect,
    UserRegisterResponse,
    UserRegisteredEvents,
)
from app.db.core import SessionDep
from app.api.users import service
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.clubs.schemas import ClubListResponse, ClubPublicMin
from app.core.auth.dependencies import AdminAuth, DependsAuth, UserAuth
from app.api.interests.schemas import InterestPublic
from app.api.auth import service as auth_service

router = APIRouter(prefix="/user")


@router.post("/register", summary="Register a new user")
async def register_user(
    user: UserCreate,
    session: SessionDep,
) -> UserRegisterResponse:
    user = await service.create_user(
        session,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        password=user.password,
    )
    token = await auth_service.create_access_refresh_tokens(user)
    return {
        "username": user.username,
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "token_type": token.token_type,
    }


@router.get("/following", summary="Get all clubs user is following")
async def get_following_clubs(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: DependsAuth,
) -> PaginatedResponse[ClubListResponse]:
    following_clubs = await service.following_clubs(
        session, user.id, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(following_clubs, request=request, schema=ClubListResponse)


@router.post("/profile/create", summary="Create a user profile.")
async def create_user_profile(
    session: SessionDep,
    user: UserAuth,
    full_name: str = Form(...),
    whatsapp: str | None = Form(None),
    org_id: int | None = Form(None),
) -> UserCreateResponse:
    return await service.create_user_profile(
        session,
        full_name=full_name,
        user_id=user.id,
        whatsapp=whatsapp,
        org_id=org_id,
    )


@router.post("/profile/update", summary="Update a user profile.")
async def create_user_profile(
    session: SessionDep,
    user: UserAuth,
    full_name: str = Form(...),
    whatsapp: str | None = Form(None),
    org_id: int | None = Form(None),
) -> UserCreateResponse:
    return await service.update_user_profile(
        session,
        full_name=full_name,
        user_id=user.id,
        whatsapp=whatsapp,
        org_id=org_id,
    )


@router.put("/profile/update-picture")
async def update_profile_picture(
    session: SessionDep,
    user: UserAuth,
    profile_picture: UploadFile = File(...),
) -> dict:
    """Update user's profile picture."""
    if not profile_picture.content_type.startswith("image/"):
        raise Exception("File must be an image")

    return await service.update_profile_picture(
        session, user_id=user.id, profile_picture=profile_picture
    )


@router.get("/profile/me", summary="view self user profile")
async def get_profile(session: SessionDep, user: UserAuth) -> UserDetailResponse:
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


@router.get("/avatar/list", summary="list all avatars")
async def list_avatars(session: SessionDep) -> List[UserAvatarDetail]:
    return await service.list_avatars(session)


@router.post("/avatar/select", summary="select user avatar")
async def select_avatar(
    session: SessionDep, user: UserAuth, avatar: UserAvatarSelect
) -> UserAvatarDetail:
    return await service.select_avatar(
        session, user_id=user.id, avatar_id=avatar.avatar_id
    )


@router.post("/avatar/create", summary="create user avatar")
async def create_avatar(
    session: SessionDep,
    user: AdminAuth,
    avatar: UploadFile = File(...),
    name: str = Form(...),
) -> UserAvatarDetail:
    return await service.create_user_avatar(session, name=name, file=avatar)


@router.delete("/delete", summary="delete user")
async def delete_user(
    session: SessionDep,
    user: UserAuth,
) -> dict:
    await service.delete_user(session, user.id)
    return {"message": "User deleted successfully."}


@router.get("/my-events", summary="Get all events user is attending")
async def get_my_events(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: UserAuth,
    is_attended: bool = Query(False),
    is_paid: bool = Query(False),
    is_won: bool = Query(False),
) -> PaginatedResponse[UserRegisteredEvents]:
    events = await service.list_registered_events(
        session,
        user.id,
        is_attended=is_attended,
        is_paid=is_paid,
        is_won=is_won,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(events, request=request, schema=UserRegisteredEvents)


@router.post("/fcm-token", summary="Register FCM token for push notifications")
async def register_fcm_token(
    session: SessionDep,
    user: DependsAuth,
    body: FCMTokenRequest,
) -> FCMTokenResponse:
    """Register or update the user's FCM token for push notifications."""
    await service.update_fcm_token(
        session=session,
        user_id=user.id,
        fcm_token=body.fcm_token,
        platform=body.platform,
    )
    return FCMTokenResponse(success=True, message="FCM token registered successfully")

