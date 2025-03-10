from datetime import timedelta
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Form,
    Request,
    Query,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from typing import Optional, List

from sqlalchemy import select

from app.db.core import SessionDep
from app.api.events import service
from app.api.events.schemas import (
    EventCategoryCreate,
    EventCategoryResponse,
    EventCreate,
    EventCreateUpdateResponse,
    EventDetailResponse,
    EventEdit,
    EventListResponse,
    EventRegistrationDetailResponse,
    EventRegistrationPublicMin,
    EventRating,
    EventRatingCreate,
    EventRegistrationRequest,
    EventRegistrationResponse,
    TicketDetailsResponse,
)
from app.core.auth.dependencies import (
    AdminAuth,
    ClubAuth,
    DependsAuth,
    OptionalUserAuth,
    UserAuth,
)
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.events.volunteer.router import router as volunteer_router
from app.api.users import service as user_service
from app.api.users.models import UserTypes
from app.response import CustomHTTPException
from app.core.utils.excel import read_excel
from app.api.events.models import Events
from app.api.schemas import BackgroundTaskLogResponseSchema
from app.api.service import create_background_task_log
from app.api.auth import service as auth_service

router = APIRouter(prefix="/events")

router.include_router(volunteer_router, tags=["Volunteer"])


@router.post("/categories/create", summary="Create a new event category")
async def create_event_category(
    user: AdminAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
) -> EventCategoryResponse:
    return await service.create_event_category(session, category, user.id)


@router.get("/categories/list", summary="List all event categories")
async def list_event_categories(
    session: SessionDep, user: DependsAuth
) -> List[EventCategoryResponse]:
    return await service.list_event_categories(session)


@router.post("/create", summary="Create a new event")
async def create_event(
    user: ClubAuth, session: SessionDep = SessionDep, event: EventCreate = Depends()
) -> EventCreateUpdateResponse:
    return await service.create_event(
        session,
        user_id=user.id,
        name=event.name,
        event_datetime=event.event_datetime,
        duration=event.duration,
        category_id=event.category_id,
        poster=event.poster,
        has_fee=event.has_fee,
        reg_fee=event.reg_fee,
        location_name=event.location_name,
        has_prize=event.has_prize,
        prize_amount=event.prize_amount,
        is_online=event.is_online,
        reg_startdate=event.reg_startdate,
        reg_enddate=event.reg_enddate,
        about=event.about,
        contact_phone=event.contact_phone,
        contact_email=event.contact_email,
        url=event.url,
        additional_details=event.additional_details,
        interest_ids=event.interest_ids,
        max_participants=event.max_participants,
        event_guidelines=event.event_guidelines,
    )


@router.put("/update/{event_id}", summary="Update an event")
async def update_event(
    event_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
    event: EventEdit = Depends(),
) -> EventCreateUpdateResponse:
    return await service.update_event(session, event, user.id, event_id=event_id)


@router.get("/info/{event_id}", summary="Get event info")
async def get_event(
    event_id: str, session: SessionDep = SessionDep
) -> EventDetailResponse:
    result = await service.get_event(session, event_id)
    data = jsonable_encoder(result)
    return data


@router.get("/list", summary="List all events")
async def list_events(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
    is_following: Optional[bool] = Query(None),
    is_registered: Optional[bool] = Query(None),
    is_ended: Optional[bool] = Query(None),
    interest_ids: Optional[str] = Query(None),
) -> PaginatedResponse[EventListResponse]:
    """List events with optional filters."""
    interest_ids = [int(i) for i in interest_ids.split(",")] if interest_ids else []
    events = await service.list_events(
        session=session,
        user_id=user.id if user else None,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
        is_registered=is_registered,
        is_ended=is_ended,
        interest_ids=interest_ids,
    )
    return paginated_response(events, request, schema=EventListResponse)


@router.delete("/delete/{event_id}", summary="Delete an event")
async def delete_event(
    event_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
):
    await service.delete_event(session, event_id, user.id)
    return {"message": "Event deleted successfully"}


@router.post("/rate/{event_id}", response_model=EventRating)
async def rate_event_endpoint(
    event_id: int,
    rating_data: EventRatingCreate,
    session: SessionDep,
    user: UserAuth,
):
    """Rate an event. Rating must be between 0 and 5."""
    return await service.rate_event(
        session=session,
        event_id=event_id,
        user_id=user.id,
        rating=rating_data.rating,
        review=rating_data.review,
    )


@router.post("/registration/{event_id}/register", summary="Register for an event")
async def register_event(
    background_tasks: BackgroundTasks,
    registration: EventRegistrationRequest,
    event_id: int | str,
    user: OptionalUserAuth,
    session: SessionDep = SessionDep,
) -> EventRegistrationResponse:
    response = {}

    if not user:
        if not (
            user := await user_service.get_non_club_user_by_email(
                session, registration.email
            )
        ):
            user = await user_service.create_user(
                session=session,
                full_name=registration.full_name,
                email=registration.email,
                phone=registration.phone,
                provider="email",
                user_type=UserTypes.guest,
            )

        if not user:
            raise CustomHTTPException(400, "User not found")
        if user.user_type != UserTypes.guest:
            raise CustomHTTPException(
                400, "This email is already registered, please login to continue"
            )

        token = await auth_service.create_access_refresh_tokens(user)
        response["auth_token"] = token
    register_data = await service.register_event(
        session=session,
        background_tasks=background_tasks,
        full_name=registration.full_name,
        email=registration.email,
        phone=registration.phone,
        user_id=user.id,
        event_id=event_id,
        additional_details=registration.additional_details,
    )
    response = {**response, **register_data}
    return response


@router.get("/registration/{event_id}/list", summary="Get event registration details")
async def list_event_registration(
    request: Request,
    user: ClubAuth,
    pagination: PaginationParams,
    event_id: int,
    session: SessionDep = SessionDep,
) -> PaginatedResponse[EventRegistrationPublicMin]:
    result = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(result, request, EventRegistrationPublicMin)


@router.get(
    "/registration/{event_id}/info/{registration_id}",
    summary="Get event registration details",
)
async def get_event_registration(
    request: Request,
    user: ClubAuth,
    event_id: int,
    registration_id: str,
    session: SessionDep = SessionDep,
) -> EventRegistrationDetailResponse:
    result = await service.get_registration(
        session, user_id=user.id, event_id=event_id, registration_id=registration_id
    )
    return jsonable_encoder(result)


@router.post(
    "/registration/{event_id}/bulk-import", summary="Bulk import user registrations"
)
async def bulk_import_event_registrations(
    user: AdminAuth,
    event_id: int,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    file: UploadFile = Form(...),
) -> BackgroundTaskLogResponseSchema:
    df = await read_excel(file)
    event = await session.scalar(select(Events).filter(Events.id == event_id))
    if not event:
        raise CustomHTTPException(404, "Event not found")
    background_log = await create_background_task_log(
        session,
        f"Bulk Import Event Registrations for '{event.name}'",
        "event_registrations_bulk_import",
    )
    background_tasks.add_task(
        service.bulk_import_event_registrations, session, event_id, df, background_log
    )
    return jsonable_encoder(background_log)


@router.get("/tickets/{ticket_id}", summary="Get ticket details")
async def get_ticket(
    request: Request,
    ticket_id: int | str,
    session: SessionDep = SessionDep,
) -> TicketDetailsResponse:
    result = await service.get_ticket_details(session, ticket_id=ticket_id)
    return jsonable_encoder(result)
