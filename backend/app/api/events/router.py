from fastapi import APIRouter, Body, Depends, Request, Query
from fastapi.encoders import jsonable_encoder
from typing import Optional, List

from app.db.core import SessionDep
from app.api.events import service
from app.api.events.schemas import (
    EventCategoryCreate,
    EventCategoryPublic,
    EventCategoryResponse,
    EventCreate,
    EventCreateUpdateResponse,
    EventDetailResponse,
    EventEdit,
    EventListResponse,
    EventPublic,
    EventPublicMin,
    EventRegistration,
    EventRegistrationPublicMin,
    EventRating,
    EventRatingCreate,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth, DependsAuth, UserAuth
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)

router = APIRouter(prefix="/events")


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
    user: DependsAuth, event_id: int, session: SessionDep = SessionDep
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
    user: UserAuth,
    event_id: int,
    session: SessionDep = SessionDep,
    additional_details: dict[str, str] = Body(None),
):
    return await service.register_event(session, event_id, user.id, additional_details)


@router.get("/registration/{event_id}/list", summary="Get event registration details")
async def list_event_registration(
    request: Request,
    user: ClubAuth,
    pagination: PaginationParams,
    event_id: int,
    session: SessionDep = SessionDep,
):
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
    registration_id: int,
    session: SessionDep = SessionDep,
) -> EventRegistration:
    result = await service.get_registration(
        session, user_id=user.id, event_id=event_id, registration_id=registration_id
    )
    return jsonable_encoder(result)
