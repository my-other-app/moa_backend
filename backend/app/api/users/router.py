import csv
import io
import logging
from datetime import datetime
from typing import AsyncIterator, List

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.api.users.schemas import (
    AdminUserDetailResponse,
    AdminUserListResponse,
    AdminUserStats,
    FCMTokenDelete,
    FCMTokenRequest,
    FCMTokenResponse,
    UserAvatarDetail,
    UserAvatarSelect,
    UserCreate,
    UserCreateResponse,
    UserDetailResponse,
    UserInterestSelect,
    UserPreferences,
    UserPreferencesPatch,
    UserRegisterResponse,
    UserRegisteredEvents,
)
from app.db.core import SessionDep
from app.core.response.base_model import CustomBaseModel
from app.response import CustomHTTPException
from app.api.users import service
from app.api.audit import service as audit
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
from app.core.rate_limit import limiter
from app.core.storage import formats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user")
admin_router = APIRouter(prefix="/admin/users", tags=["admin"])


@admin_router.post("/merge-guests", summary="Fold waiting guests into every app account")
async def merge_guests(request: Request, session: SessionDep, admin: ModerateAuth) -> dict:
    """The merge the app runs per account, for every app account with guests
    under its address -- including guests whose address differs only in
    case, which the merge used to miss. Safe to run again: a second run finds
    nothing to do."""
    totals = await service.merge_all_guests(session)
    await audit.record(
        session,
        actor=admin,
        action="users.merge_guests",
        entity_type="user",
        summary=f"Merged guests into {totals['app_users_merged']} app accounts",
        details=totals,
        request=request,
    )
    await session.commit()
    return totals


@router.post("/register", summary="Register a new user")
# Per caller address (app.core.net): loose enough for a campus behind one
# Wi-Fi address, tight enough that a script cannot mint accounts by the
# thousand.
@limiter.limit("20/minute")
async def register_user(
    request: Request,
    user: UserCreate,
    session: SessionDep,
    background: BackgroundTasks,
) -> UserRegisterResponse:
    # The app's own rule, held here too: the server took any length before.
    if user.password and len(user.password) < 6:
        raise CustomHTTPException(400, message="Invalid Request", errors={"password": "Use at least 6 characters"})
    user = await service.create_user(
        session,
        full_name=user.full_name,
        # Stored in one case, so signing in and finding the account never
        # depend on how the address was typed.
        email=user.email.strip().lower(),
        phone=user.phone,
        password=user.password,
    )
    # The address was only typed. A code goes to it now, so the app can ask
    # for it on the next screen; until it comes back the account gets
    # nothing booked under the address (merges_into).
    try:
        from app.api.auth import verification

        code = await verification.issue(session, user)
        await session.commit()
        background.add_task(verification.send_code_email, user.email, code=code, name=user.full_name)
    except Exception:
        # The account stands either way; the app can ask for another code.
        logger.warning("Could not issue a verification code for new user %s", user.id, exc_info=True)
        await session.rollback()
        # A rollback expires the account; reload it rather than let the token
        # code lazy-load it (MissingGreenlet on the async engine).
        await session.refresh(user)
    # With a session row, like every other sign-in: it shows on the Security
    # page and can be signed out from there.
    token = await auth_service.create_access_refresh_tokens(user, session=session, request=request)
    await session.commit()
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

#: A search shorter than this matches exactly (an id, an address, a
#: username) and never by "contains": a trigram index cannot narrow one or
#: two letters, so "contains" would read every account.
_FUZZY_MIN = 3
#: The list's total is counted up to this and no further (X-Total-Capped
#: says when it stopped): counting a crore of accounts for a pager is a read
#: of the whole table on every page.
_TOTAL_CAP = 10_000


async def _search_criteria(session, search: str):
    """What `search` means, cheapest first.

    An exact id, address (through ix_users_email_normalised) or username is
    found by index. A whole address that matches is the answer on its own.
    Anything else is those exact matches together with "contains" on the
    name, address and username, served by one trigram index -- for a
    term of three characters or more; a shorter one matches exactly or not
    at all, since a trigram index cannot narrow one or two letters and
    "contains" would read every account."""
    from sqlalchemy import false, func, or_, select

    from app.api.users.models import Users

    term = search.strip()
    if not term:
        return []
    exact = [Users.username == term]
    if term.isdigit() and len(term) < 10:
        exact.append(Users.id == int(term))
    if "@" in term:
        exact.append(func.lower(func.btrim(Users.email)) == term.lower())
    ids = list((await session.scalars(select(Users.id).where(or_(*exact)).limit(50))).all())
    if ids and "@" in term:
        return [Users.id.in_(ids)]
    found = [Users.id.in_(ids)] if ids else []
    if len(term) >= _FUZZY_MIN:
        # The name, username and address are one column, lowercased and
        # unaccented (app.api.search.text), under one trigram index; the
        # term is normalised the same way, so "jose" finds "José".
        from app.api.search import text as stext

        folded = await stext.normalise(session, term)
        escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        found.append(Users.search_text.like(f"%{escaped}%", escape="\\"))
    return [or_(*found)] if found else [false()]


async def _user_list_filters(
    session,
    search: str | None,
    user_type: str | None,
    provider: str | None,
    is_active: bool | None,
):
    """The WHERE clauses for the admin user list, as a list of criteria."""
    from app.api.users.models import Users

    criteria = []
    if search:
        criteria.extend(await _search_criteria(session, search))
    if user_type:
        criteria.append(Users.user_type == user_type)
    if provider:
        criteria.append(Users.provider == provider)
    if is_active is not None:
        criteria.append(Users.is_active.is_(is_active))
    return criteria


def _sort_column(sort: str | None):
    from app.api.users.models import Users

    return getattr(Users, _USER_SORTS.get(sort or "", "created_at"))


def _descending(order: str | None) -> bool:
    return (order or "desc").lower() != "asc"


def _sorted_user_query(query, sort: str | None, order: str | None):
    """
    Order by the requested column, then the primary key the same way.

    Without the `id` key, rows sharing a sort value can appear on two pages
    or on neither -- the database is free to return ties in any order. The
    default, newest first, is (created_at DESC, id DESC): ix_users_created_id.
    """
    from app.api.users.models import Users

    column = _sort_column(sort)
    if _descending(order):
        return query.order_by(column.desc(), Users.id.desc())
    return query.order_by(column.asc(), Users.id.asc())


def _after(sort: str | None, order: str | None, cursor: str | None):
    """The keyset condition for the page after `cursor`, or None."""
    from sqlalchemy import tuple_

    from app.api.users.models import Users

    if not cursor:
        return None
    try:
        raw_value, raw_id = cursor.rsplit("~", 1)
        last_id = int(raw_id)
        last = datetime.fromisoformat(raw_value) if _sort_column(sort) is Users.created_at else raw_value
    except (ValueError, TypeError):
        raise CustomHTTPException(422, message="That cursor is not one this list gave out", error_code="BAD_CURSOR")
    key = tuple_(_sort_column(sort), Users.id)
    return key < tuple_(last, last_id) if _descending(order) else key > tuple_(last, last_id)


def _cursor_of(db_user, sort: str | None) -> str:
    value = getattr(db_user, _USER_SORTS.get(sort or "", "created_at"))
    return f"{value.isoformat() if isinstance(value, datetime) else value}~{db_user.id}"


@router.get("/admin/list", summary="List all users (admin)")
async def list_all_users_admin(
    request: Request,
    response: Response,
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
    cursor: str | None = Query(None, description="From `next`: the page after the last row of the one before"),
) -> PaginatedResponse[AdminUserListResponse]:
    """List all users for an admin.

    Paged by keyset: `next` carries a `cursor` (the last row's sort value and
    id), and a page is an index range rather than a skip past every row
    before it. `offset` still works without a cursor. `total` is counted up
    to 10,000; past that it says 10,000 and the X-Total-Capped header is set.
    """
    from urllib.parse import urlencode

    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    from app.api.users.models import Users, UserProfiles

    criteria = await _user_list_filters(session, search, user_type, provider, is_active)

    # The same criteria go on both queries, or the total disagrees with the
    # rows and the pager offers pages that do not exist.
    total = (
        await session.scalar(
            select(func.count()).select_from(select(Users.id).where(*criteria).limit(_TOTAL_CAP + 1).subquery())
        )
    ) or 0
    if total > _TOTAL_CAP:
        total = _TOTAL_CAP
        response.headers["X-Total-Capped"] = "true"

    after = _after(sort, order, cursor)
    query = (
        select(Users)
        .where(*criteria, *([after] if after is not None else []))
        .options(
            selectinload(Users.profile).selectinload(UserProfiles.org),
            selectinload(Users.profile).selectinload(UserProfiles.avatar),
            selectinload(Users.interests),
        )
        .limit(pagination.limit)
    )
    if after is None:
        query = query.offset(pagination.offset)
    rows = list((await session.execute(_sorted_user_query(query, sort, order))).scalars().all())

    limit = pagination.limit - 1
    page = paginated_response(rows, request=request, schema=AdminUserListResponse, total=total)
    # The next page starts after this one's last row, not at an offset.
    if len(rows) > limit:
        params = {k: v for k, v in request.query_params.items() if k != "offset"}
        params["cursor"] = _cursor_of(rows[limit - 1], sort)
        page.next = f"{request.url.path}?{urlencode(params)}"
    else:
        page.next = None
    return page


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
    with it. Each chunk starts where the last one ended (a keyset on the
    sort column and id), so chunk n costs what chunk 1 did; it skipped past
    every earlier row by OFFSET, which made a large export quadratic.

    The request's own session is used while streaming, which is safe here:
    FastAPI closes the dependency exit stack after the response body has
    been sent, so a `yield` dependency outlives the generator that produced
    the body. `test_export_pages_past_one_chunk` is what actually holds that
    down -- it reads several chunks, so a session closed early fails it.
    """
    from sqlalchemy import select

    from app.api.users.models import Users

    criteria = await _user_list_filters(session, search, user_type, provider, is_active)

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

        cursor = None
        while True:
            after = _after(sort, order, cursor)
            query = select(Users).where(*criteria, *([after] if after is not None else [])).limit(_EXPORT_CHUNK)
            batch = (await session.execute(_sorted_user_query(query, sort, order))).scalars().all()
            if not batch:
                break
            for db_user in batch:
                writer.writerow([value(db_user) for _, value in _EXPORT_COLUMNS])
            yield drain()
            if len(batch) < _EXPORT_CHUNK:
                break
            cursor = _cursor_of(batch[-1], sort)
            # The rows are written: let them go rather than keep every chunk.
            session.expunge_all()

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
    """Update user's profile picture. Answers with its links by size:
    {"thumbnail", "medium", "large", "original"}."""
    # Refused here only when the part says it is something else. Some phones
    # send a gallery photo with no type, or as application/octet-stream:
    # those go on, and the column reads the bytes and refuses anything that
    # is not a picture with a 422 of its own (storage.fields.prepare_image).
    # This was a bare Exception -- a 500 -- and crashed on a part with no type.
    if not formats.may_be_picture(profile_picture.content_type):
        raise CustomHTTPException(
            422, message=f"That file is not a picture. Upload a {formats.WORKS} picture.", error_code="NOT_AN_IMAGE"
        )

    return await service.update_profile_picture(
        session, user_id=user.id, profile_picture=profile_picture
    )


@router.get("/profile/me", summary="view self user profile")
async def get_profile(session: SessionDep, user: UserAuth) -> UserDetailResponse:
    # The app reads this whenever it needs the profile, sign-in or not, and
    # app sessions last weeks: a ticket bought on the website as a guest is
    # folded in here rather than at the next sign-in.
    await service.merge_guests_now(session, user)
    profile = await service.get_user_profile(session=session, user_id=user.id)
    return profile


@router.patch("/preferences", summary="The person's own switches: People Like You")
async def update_preferences(session: SessionDep, user: DependsAuth, body: UserPreferencesPatch) -> UserPreferences:
    """`networking_visible` off: nobody sees them in an event's People Like
    You. They can still look themselves (events.networking.HIDDEN_CAN_BROWSE).
    The profile (GET /user/profile/me) reads the same switch back."""
    if body.networking_visible is not None:
        user.networking_visible = body.networking_visible
        await session.commit()
    return UserPreferences(networking_visible=user.networking_visible)


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
) -> UserAvatarDetail | None:
    """Pick a preset avatar; it replaces an uploaded photo. `avatar_id`
    null clears the avatar (null back) and leaves a photo be."""
    return await service.select_avatar(
        session, user_id=user.id, avatar_id=avatar.avatar_id
    )


@router.post("/avatar/create", summary="create user avatar")
async def create_avatar(
    session: SessionDep,
    user: ContentAuth,
    avatar: UploadFile = File(...),
    # user_avatars.name holds twenty: a longer one was a 500 from the database.
    name: str = Form(..., min_length=1, max_length=20),
) -> UserAvatarDetail:
    return await service.create_user_avatar(session, name=name, file=avatar)


@router.put("/avatar/update/{id}", summary="update user avatar")
async def update_avatar(
    id: int,
    session: SessionDep,
    user: ContentAuth,
    name: str | None = Form(None, min_length=1, max_length=20),
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
    await service.merge_guests_now(session, user)
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
    """Register or update the user's FCM token for push notifications.

    The app sends this on every launch with a stored session, so it is also
    where guests waiting under the address are folded in."""
    await service.merge_guests_now(session, user)
    await service.update_fcm_token(
        session=session,
        user_id=user.id,
        fcm_token=body.fcm_token,
        platform=body.platform,
        supports_topics=body.topics == 1,
    )
    return FCMTokenResponse(success=True, message="FCM token registered successfully")


@router.delete("/fcm-token", summary="Stop push notifications to this device (sign-out)", status_code=204)
async def remove_fcm_token(
    session: SessionDep,
    user: DependsAuth,
    body: FCMTokenDelete,
) -> Response:
    """Removes the token from this account, so the device signing out stops
    getting its pushes. 204 whether or not the account had the token."""
    await service.remove_fcm_token(session, user_id=user.id, fcm_token=body.token)
    return Response(status_code=204)


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
