"""The guest's “Give my ticket back” page's two calls, and the club's list
of tickets given back (app/api/ticket_cancel/service.py)."""
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.events.access import is_admin_user
from app.api.ticket_cancel import service
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/tickets/cancel", tags=["tickets"])
club_router = APIRouter(prefix="/events/registration", tags=["tickets"])


class CancelIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=service.REASON_MAX)


@router.get("/{token}", summary="What a cancel link is for, and whether the ticket can be given back (public)")
@limiter.limit("30/minute")
async def cancel_preview(request: Request, session: SessionDep, token: str) -> dict:
    return await service.preview(session, token)


@router.post("/{token}", summary="Give the ticket back: free tickets only, before the event starts (public)")
@limiter.limit("10/minute")
async def cancel_ticket(request: Request, session: SessionDep, token: str, body: Optional[CancelIn] = None) -> dict:
    return await service.cancel(session, token, body.reason if body is not None else None)


@club_router.get("/{event_id}/cancellations", summary="Tickets the guests gave back themselves, newest first")
async def cancellations(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    """Beside the list (which shows only the people still on it): who gave
    their ticket back by the email's link, when, and why if they said."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.api.events.access import ensure_event_access
    from app.api.events.models import Events
    from app.response import CustomHTTPException

    event = await session.scalar(select(Events).where(Events.id == event_id, Events.is_deleted == False).options(joinedload(Events.club)))  # noqa: E712
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    ensure_event_access(event, user.id, is_admin_user(user), message="Not authorized to manage this event")
    return {"items": await service.given_back(session, event_id)}
