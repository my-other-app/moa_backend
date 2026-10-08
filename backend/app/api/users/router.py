import csv
import io
from datetime import datetime
from typing import AsyncIterator, List

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.api.users.schemas import (
    AdminUserDetailResponse,
    AdminUserListResponse,
    AdminUserStats,
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
from app.core.response.base_model import CustomBaseModel
from app.response import CustomHTTPException
from app.api.users import service
from app.api.audit import service as audit
from app.core.response.sorting import apply_sort
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.clubs.schemas import ClubListResponse, ClubPublicMin
from app.core.auth.dependencies import AdminAuth, DependsAuth, UserAuth
from app.core.auth.permissions import (
    ContentAuth,
    DestroyAuth,
    ExportDataAuth,
    ManageAdminsAuth,
    ModerateAuth,
)
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


# ---------------------------------------------------------------------------
# Admin: the user list, and the CSV export of the same list.
#
# The filters live in one place because the export has to mean "what I am
# looking at". A second copy of the predicate would drift, and an export that
# quietly ignores a filter is worse than no export at all -- the admin cannot
# tell by looking at the file.
# ---------------------------------------------------------------------------

#: Sort keys the API accepts, mapped to the columns they order by.
_USER_SORTS = {
    "joined": "created_at",
    "name": "full_name",
    "email": "email",
    "username": "username",
}


def _user_list_filters(
    search: str | None,
    user_type: str | None,
    provider: str | None,
    is_active: bool | None,
):
    """The WHERE clauses for the admin user list, as a list of criteria."""
    from sqlalchemy import or_

    from app.api.users.models import Users

    criteria = []
    if search:
        term = f"%{search}%"
        criteria.append(
            or_(
                Users.full_name.ilike(term),
                Users.email.ilike(term),
                Users.username.ilike(term),
            )
        )
    if user_type:
        criteria.append(Users.user_type == user_type)
    if provider:
        criteria.append(Users.provider == provider)
    if is_active is not None:
        criteria.append(Users.is_active.is_(is_active))
    return criteria


def _sorted_user_query(query, sort: str | None, order: str | None):
    """
    Apply the requested sort, then break ties on the primary key.

    Without the final `id` key, rows sharing a sort value can appear on two
    pages or on neither -- the database is free to return ties in any order,
    and both the paged list and the chunked export read by offset.
    """
    from app.api.users.models import Users

    query = apply_sort(
        query,
        columns={name: getattr(Users, col) for name, col in _USER_SORTS.items()},
        default=Users.created_at,
        sort=sort,
        order=order,
    )
    return query.order_by(Users.id.desc())


@router.get("/admin/list", summary="List all users (admin)")
async def list_all_users_admin(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    search: str | None = Query(None),
    user_type: str | None = Query(None, description="app_user, club, admin or guest"),
    provider: str | None = Query(None, description="google, apple or email"),
    sort: str | None = Query(None, description="joined, name, email or username"),
    order: str | None = Query(None, description="asc or desc"),
    is_active: bool | None = Query(
        None, description="true for active only, false for suspended only"
    ),
) -> PaginatedResponse[AdminUserListResponse]:
    """List all users for an admin."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    from app.api.users.models import Users, UserProfiles

    criteria = _user_list_filters(search, user_type, provider, is_active)

    # The same criteria go on both queries, or the total disagrees with the
    # rows and the pager offers pages that do not exist.
    total = (
        await session.execute(select(func.count(Users.id)).where(*criteria))
    ).scalar() or 0

    query = (
        select(Users)
        .where(*criteria)
        .options(
            selectinload(Users.profile).selectinload(UserProfiles.org),
            selectinload(Users.profile).selectinload(UserProfiles.avatar),
            selectinload(Users.interests),
        )
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await session.execute(_sorted_user_query(query, sort, order))

    return paginated_response(
        result.scalars().all(),
        request=request,
        schema=AdminUserListResponse,
        total=total,
    )


#: Rows per database round trip while streaming the export. Big enough that a
#: 50k-user export is 50 queries rather than 5,000; small enough that one
#: chunk is a few hundred kilobytes rather than the whole table in memory.
_EXPORT_CHUNK = 1000

_EXPORT_COLUMNS = (
    ("ID", lambda u: u.id),
    ("Name", lambda u: u.full_name),
    ("Username", lambda u: u.username),
    ("Email", lambda u: u.email),
    ("Phone", lambda u: u.phone),
    ("Type", lambda u: getattr(u.user_type, "value", u.user_type)),
    ("Sign-in", lambda u: getattr(u.provider, "value", u.provider)),
    ("Joined", lambda u: u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else ""),
    ("Status", lambda u: "active" if u.is_active else "suspended"),
    ("Suspended reason", lambda u: u.suspended_reason or ""),
)


@router.get("/admin/export", summary="Export the filtered user list as CSV (admin)")
async def export_users_admin(
    session: SessionDep,
    admin: ExportDataAuth,
    search: str | None = Query(None),
    user_type: str | None = Query(None, description="app_user, club, admin or guest"),
    provider: str | None = Query(None, description="google, apple or email"),
    sort: str | None = Query(None, description="joined, name, email or username"),
    order: str | None = Query(None, description="asc or desc"),
    is_active: bool | None = Query(
        None, description="true for active only, false for suspended only"
    ),
) -> StreamingResponse:
    """
    Stream every row matching the filters, not just the page on screen.

    Behind `export_data` rather than plain AdminAuth: a viewer can already
    page the list, but handing out the whole user base as one file is a
    different act from reading a screen of it, and it is the one the audit
    log and the role split care about.

    Streamed in chunks rather than assembled and returned: the box this runs
    on has a few hundred megabytes of headroom, and a full export held in
    memory as Python objects is the kind of thing that takes the API down
    with it.

    The request's own session is used while streaming, which is safe here:
    FastAPI closes the dependency exit stack after the response body has
    been sent, so a `yield` dependency outlives the generator that produced
    the body. `test_export_pages_past_one_chunk` is what actually holds that
    down -- it reads several chunks, so a session closed early fails it.
    """
    from sqlalchemy import select

    from app.api.users.models import Users

    criteria = _user_list_filters(search, user_type, provider, is_active)

    async def rows() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def drain() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        # A BOM, so Excel on Windows reads the file as UTF-8 instead of
        # mangling every non-ASCII name in it.
        writer.writerow([name for name, _ in _EXPORT_COLUMNS])
        yield "\ufeff" + drain()

        offset = 0
        while True:
            query = select(Users).where(*criteria).offset(offset).limit(_EXPORT_CHUNK)
            result = await session.execute(_sorted_user_query(query, sort, order))
            batch = result.scalars().all()
            if not batch:
                break
            for db_user in batch:
                writer.writerow([value(db_user) for _, value in _EXPORT_COLUMNS])
            yield drain()
            if len(batch) < _EXPORT_CHUNK:
                break
            offset += _EXPORT_CHUNK

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="users-{stamp}.csv"',
            # Nothing downstream should keep a copy of the user base.
            "Cache-Control": "no-store",
        },
    )


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
    user: ContentAuth,
    avatar: UploadFile = File(...),
    name: str = Form(...),
) -> UserAvatarDetail:
    return await service.create_user_avatar(session, name=name, file=avatar)


@router.put("/avatar/update/{id}", summary="update user avatar")
async def update_avatar(
    id: int,
    session: SessionDep,
    user: ContentAuth,
    name: str | None = Form(None),
    avatar: UploadFile | None = File(None),
) -> UserAvatarDetail:
    return await service.update_user_avatar(session, id=id, name=name, file=avatar)


@router.delete("/avatar/delete/{id}", summary="delete user avatar")
async def delete_avatar(
    id: int,
    session: SessionDep,
    user: DestroyAuth,
) -> dict:
    return await service.delete_user_avatar(session, id=id)


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


# ---------------------------------------------------------------------------
# Admin: one user in full.
#
# Declared after /admin/list so the literal path keeps priority over
# /admin/{user_id}. Every route here is AdminAuth: the self-service
# equivalents (/following, /my-events) read the caller's own id and cannot
# answer "what has this other user done", which is the whole point of the
# admin screen.
# ---------------------------------------------------------------------------


@router.get("/admin/{user_id}", summary="Get one user in full (admin)")
async def get_user_admin_detail(
    user_id: int,
    session: SessionDep,
    user: AdminAuth,
) -> AdminUserDetailResponse:
    db_user, stats = await service.get_user_admin_detail(session, user_id)
    return AdminUserDetailResponse(
        **UserDetailResponse.model_validate(db_user).model_dump(),
        stats=AdminUserStats(**stats),
        # Not on UserDetailResponse, so it has to be carried explicitly --
        # otherwise the field silently defaults to True and a suspended
        # account reads as active.
        is_active=db_user.is_active,
        suspended_reason=db_user.suspended_reason,
        admin_role=getattr(db_user.admin_role, "value", db_user.admin_role),
    )


@router.get("/admin/{user_id}/clubs", summary="Clubs a user follows (admin)")
async def list_user_clubs_admin(
    user_id: int,
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
) -> PaginatedResponse[ClubListResponse]:
    clubs = await service.following_clubs(
        session, user_id, limit=pagination.limit, offset=pagination.offset
    )
    total = await service.count_following_clubs(session, user_id)
    return paginated_response(
        clubs, request=request, schema=ClubListResponse, total=total
    )


@router.get("/admin/{user_id}/events", summary="Events a user registered for (admin)")
async def list_user_events_admin(
    user_id: int,
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    is_attended: bool | None = Query(None),
    is_paid: bool | None = Query(None),
    is_won: bool | None = Query(None),
) -> PaginatedResponse[UserRegisteredEvents]:
    """
    Unfiltered by default. The filters are None rather than False so that
    omitting one means "do not filter" -- passing False would narrow the list
    to registrations where the flag is off, which is not what "all events"
    means.
    """
    events = await service.list_registered_events(
        session,
        user_id,
        is_attended=is_attended,
        is_paid=is_paid,
        is_won=is_won,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    total = await service.count_registered_events(
        session, user_id, is_attended=is_attended, is_paid=is_paid, is_won=is_won
    )
    return paginated_response(
        events, request=request, schema=UserRegisteredEvents, total=total
    )


class SetUserActiveRequest(CustomBaseModel):
    is_active: bool
    reason: str | None = None


@router.put("/admin/{user_id}/active", summary="Suspend or reinstate a user (admin)")
async def set_user_active_admin(
    user_id: int,
    body: SetUserActiveRequest,
    session: SessionDep,
    user: ModerateAuth,
    request: Request = None,
) -> AdminUserDetailResponse:
    """
    Suspension, as opposed to deletion.

    An admin dealing with an abusive account previously had one option --
    delete it -- which is permanent in practice and takes their data with it.
    """
    if user_id == user.id:
        # Nothing stops an admin locking themselves out otherwise, and
        # reinstating requires an admin.
        raise CustomHTTPException(400, message="You cannot suspend your own account")

    target = await service.set_user_active(
        session, user_id, is_active=body.is_active, reason=body.reason
    )
    await audit.record(
        session,
        actor=user,
        action="user.suspend" if not body.is_active else "user.reinstate",
        entity_type="user",
        entity_id=user_id,
        summary=(
            f"Suspended {target.email or target.username}"
            if not body.is_active
            else f"Reinstated {target.email or target.username}"
        ),
        details={"reason": body.reason},
        request=request,
    )
    await session.commit()

    db_user, stats = await service.get_user_admin_detail(session, user_id)
    return AdminUserDetailResponse(
        **UserDetailResponse.model_validate(db_user).model_dump(),
        stats=AdminUserStats(**stats),
        # Not on UserDetailResponse, so it has to be carried explicitly --
        # otherwise the field silently defaults to True and a suspended
        # account reads as active.
        is_active=db_user.is_active,
        suspended_reason=db_user.suspended_reason,
        admin_role=getattr(db_user.admin_role, "value", db_user.admin_role),
    )


class AdminUpdateUserRequest(CustomBaseModel):
    """Only the fields sent are applied; omitted ones are left alone."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None


class SetAdminRoleRequest(CustomBaseModel):
    role: str


@router.put("/admin/{user_id}", summary="Edit a user (admin)")
async def admin_update_user_endpoint(
    user_id: int,
    body: AdminUpdateUserRequest,
    session: SessionDep,
    user: ManageAdminsAuth,
    request: Request = None,
) -> AdminUserDetailResponse:
    """
    Admins could suspend an account but not correct it -- a typo'd email had
    no fix in the product.
    """
    await service.admin_update_user(
        session,
        user_id,
        full_name=body.full_name,
        email=body.email,
        phone=body.phone,
    )
    await audit.record(
        session,
        actor=user,
        action="user.update",
        entity_type="user",
        entity_id=user_id,
        summary="Edited account details",
        details=body.model_dump(exclude_none=True),
        request=request,
    )
    await session.commit()

    db_user, stats = await service.get_user_admin_detail(session, user_id)
    return AdminUserDetailResponse(
        **UserDetailResponse.model_validate(db_user).model_dump(),
        stats=AdminUserStats(**stats),
        is_active=db_user.is_active,
        suspended_reason=db_user.suspended_reason,
        admin_role=getattr(db_user.admin_role, "value", db_user.admin_role),
    )


@router.delete("/admin/{user_id}", summary="Delete a user (admin)")
async def admin_delete_user_endpoint(
    user_id: int,
    session: SessionDep,
    user: ManageAdminsAuth,
    request: Request = None,
):
    if user_id == user.id:
        raise CustomHTTPException(400, message="You cannot delete your own account")

    target = await service.admin_delete_user(session, user_id)
    await audit.record(
        session,
        actor=user,
        action="user.delete",
        entity_type="user",
        entity_id=user_id,
        summary=f"Deleted {target.email or target.username}",
        request=request,
    )
    await session.commit()
    return {"message": "User deleted"}


@router.put("/admin/{user_id}/role", summary="Set an admin's role")
async def set_admin_role_endpoint(
    user_id: int,
    body: SetAdminRoleRequest,
    session: SessionDep,
    user: ManageAdminsAuth,
    request: Request = None,
) -> AdminUserDetailResponse:
    """
    Roles were enforced from the first day they existed but could only be
    changed with a direct database update, which left the feature
    half-delivered.
    """
    from app.api.users.models import AdminRoles

    try:
        role = AdminRoles(body.role)
    except ValueError:
        raise CustomHTTPException(400, message=f"Unknown role: {body.role}")

    if user_id == user.id and role is not AdminRoles.owner:
        # Demoting yourself removes the capability needed to undo it.
        raise CustomHTTPException(
            400, message="You cannot remove your own owner role"
        )

    target = await service.set_admin_role(session, user_id, role)
    await audit.record(
        session,
        actor=user,
        action="user.set_role",
        entity_type="user",
        entity_id=user_id,
        summary=f"Set admin role to {role.value} for {target.email}",
        details={"role": role.value},
        request=request,
    )
    await session.commit()

    db_user, stats = await service.get_user_admin_detail(session, user_id)
    return AdminUserDetailResponse(
        **UserDetailResponse.model_validate(db_user).model_dump(),
        stats=AdminUserStats(**stats),
        is_active=db_user.is_active,
        suspended_reason=db_user.suspended_reason,
        admin_role=getattr(db_user.admin_role, "value", db_user.admin_role),
    )
