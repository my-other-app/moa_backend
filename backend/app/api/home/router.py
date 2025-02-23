from fastapi import APIRouter, Request, Query
from typing import List, Optional

from fastapi.encoders import jsonable_encoder
from app.core.auth.dependencies import UserAuth
from app.core.response.pagination import PaginationParams, paginated_response
from app.db.core import SessionDep
from app.api.events.schemas import EventPublic, EventPublicMin
from app.api.clubs.schemas import ClubPublic, NotesPrivate, NotesPublic
from app.api.clubs.models import Notes
from app.api.home.schemas import SearchResults
from . import service

router = APIRouter(prefix="/home", tags=["feed"])


@router.get("/feed/events", summary="Get suggested events")
async def get_suggested_events(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
    is_following: Optional[bool] = Query(None),
    is_registered: Optional[bool] = Query(None),
    is_ended: Optional[bool] = Query(None),
):
    """Get suggested events based on user interests and filters."""
    events = await service.suggest_events(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
        is_registered=is_registered,
        is_ended=is_ended,
    )
    return paginated_response(jsonable_encoder(events), request, schema=EventPublicMin)


@router.get("/feed/clubs", summary="Get suggested clubs")
async def get_suggested_clubs(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
    is_following: Optional[bool] = Query(None),
):
    """Get suggested clubs based on user interests and filters."""
    clubs = await service.suggest_clubs(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
    )
    return paginated_response(clubs, request, schema=ClubPublic)


@router.get("/feed/notes")
async def get_suggested_notes(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
    is_following: Optional[bool] = Query(None),
):
    """Get suggested notes based on user interests and filters."""
    notes = await service.suggest_notes(
        session=session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
    )
    return paginated_response(notes, request, schema=NotesPublic)


@router.get("/feed/search")
async def global_search_endpoint(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: UserAuth,
    query: str = Query(..., min_length=1),
    is_following: Optional[bool] = Query(None),
    interest_ids: Optional[str] = Query(None),
) -> SearchResults:
    """Search across events, clubs, and notes."""
    interest_ids = [int(i) for i in interest_ids.split(",")] if interest_ids else []
    results = await service.global_search(
        session=session,
        search_query=query,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
        interest_ids=interest_ids,
    )

    return {
        "events": results["events"],
        "clubs": results["clubs"],
        "notes": results["notes"],
    }
