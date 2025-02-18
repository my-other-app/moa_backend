from fastapi import APIRouter, Body, Form

from app.db.core import SessionDep
from app.api.events import service
from app.api.events.schemas import (
    EventCategoryCreate,
    EventCategoryPublic,
    EventCreate,
    EventEdit,
    EventPublic,
    EventPublicMin,
)
from app.core.auth.dependencies import AdminAuth, DependsAuth
from app.core.response.pagination import PaginationParams, paginate
from app.response import CustomHTTPException

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
    user: DependsAuth, params: PaginationParams, session: SessionDep = SessionDep
):
    query = await service.list_events(session)
    return await paginate(query, EventPublicMin, params, session)


@router.post(
    "/categories/create",
    response_model=EventCategoryPublic,
    summary="Create a new event category",
)
async def create_event_category(
    user: AdminAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
):
    return await service.create_event_category(session, category, user.id)


@router.post("/register/{event_id}", summary="Register for an event")
async def register_event(
    user: DependsAuth,
    event_id: int,
    session: SessionDep = SessionDep,
    additional_details: dict[str, str] = Body(None),
):
    return await service.register_event(session, event_id, user.id, additional_details)
