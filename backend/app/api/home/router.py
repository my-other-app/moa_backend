from fastapi import APIRouter, Request

from app.core.auth.dependencies import UserAuth
from app.db.core import SessionDep
from app.api.home import service
from app.core.response.pagination import PaginationParams, paginated_response
from app.api.events.schemas import EventPublic

router = APIRouter(prefix="/home")


@router.get("/feed/events", summary="Get events to show in feed")
async def user_feed(
    request: Request, session: SessionDep, user: UserAuth, pagination: PaginationParams
):
    related_events = await service.user_related_events(
        session, user.id, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(related_events, request=request, schema=EventPublic)
