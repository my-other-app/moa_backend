from fastapi import APIRouter

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
from app.core.auth.dependencies import DependsAuth
from app.core.response.pagination import PaginationParams, paginate
from app.response import CustomHTTPException

router = APIRouter(prefix="/events")


@router.post("/create", response_model=EventPublic, summary="Create a new event")
async def create_event(
    user: DependsAuth, event: EventCreate, session: SessionDep = SessionDep
):
    return await service.create_event(session, event, user.id)


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
    user: DependsAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
):
    if not user.is_admin:
        raise CustomHTTPException(401, "Unauthorized")
    return await service.create_event_category(session, category, user.id)
