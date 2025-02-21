from fastapi import APIRouter, Body, Request
from fastapi.encoders import jsonable_encoder

from app.db.core import SessionDep
from app.api.events import service
from app.api.events.schemas import (
    EventCategoryCreate,
    EventCategoryPublic,
    EventCreate,
    EventEdit,
    EventPublic,
    EventPublicMin,
    EventRegistration,
    EventRegistrationPublicMin,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth, DependsAuth, UserAuth
from app.core.response.pagination import PaginationParams, paginated_response

router = APIRouter(prefix="/events")


@router.post("/create", response_model=EventPublic, summary="Create a new event")
async def create_event(
    user: DependsAuth, event: EventCreate, session: SessionDep = SessionDep
):
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
        images=event.images,
        about=event.about,
        contact_phone=event.contact_phone,
        contact_email=event.contact_email,
        url=event.url,
        additional_details=event.additional_details,
    )


@router.put("/update", response_model=EventPublic, summary="Update an event")
async def update_event(
    user: DependsAuth, event: EventEdit, session: SessionDep = SessionDep
):
    return await service.update_event(session, event, user.id)


@router.get("/info/{event_id}", response_model=EventPublic, summary="Get event info")
async def get_event(user: DependsAuth, event_id: int, session: SessionDep = SessionDep):
    return await service.get_event(session, event_id)


@router.get("/list", summary="List all events")
async def list_events(
    request: Request,
    user: DependsAuth,
    params: PaginationParams,
    session: SessionDep = SessionDep,
):
    result = await service.list_events(
        session, limit=params.limit, offset=params.offset
    )
    return paginated_response(result, request, EventPublicMin)


@router.post(
    "/categories/create",
    response_model=EventCategoryPublic,
    summary="Create a new event category",
)
async def create_event_category(
    user: AdminAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
):
    return await service.create_event_category(session, category, user.id)


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
