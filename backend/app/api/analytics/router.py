from fastapi import APIRouter
from sqlalchemy import select

from app.api.analytics import service
from app.api.analytics.schemas import ClubDashboard, ClubLifetime, EventFigures, EventSummary, PlatformDoors
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter()


@router.get("/events/{event_id}/figures", summary="The door as a shape: arrivals, doors, types, sends")
async def figures(session: SessionDep, user: ClubAuth, event_id: int) -> EventFigures:
    event = await deliveries.event_for_club(session, user, event_id)
    return EventFigures(**await service.figures(session, event))


@router.get("/events/{event_id}/summary", summary="Everything the event's overview shows")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> EventSummary:
    """One response, so no figure on the page describes a different moment
    from the one beside it. Once retention has deleted the guest list, the
    analytics come back from the closeout report's frozen copy."""
    event = await deliveries.event_for_club(session, user, event_id)
    return EventSummary(**await service.summary(session, event))


@router.get("/clubs/dashboard", summary="Where the club's events stand, what needs a decision, which doors are open")
async def dashboard(session: SessionDep, user: ClubAuth) -> ClubDashboard:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id))
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return ClubDashboard(**await service.dashboard(session, club))


@router.get("/clubs/admin/{club_id}/lifetime", summary="What a club has done over every event it has run (admin)")
async def lifetime(session: SessionDep, user: AdminAuth, club_id: int) -> ClubLifetime:
    club = await session.scalar(select(Clubs).where(Clubs.id == club_id))
    if club is None:
        raise CustomHTTPException(status_code=404, message="Club not found")
    return ClubLifetime(**await service.lifetime(session, club))


@router.get("/admin/doors", summary="Every door open on the platform, and what needs a decision (admin)")
async def platform_doors(session: SessionDep, user: AdminAuth) -> PlatformDoors:
    return PlatformDoors(**await service.platform_doors(session))
