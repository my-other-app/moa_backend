from typing import List
from fastapi import APIRouter

from app.api.events.volunteer.schemas import (
    CheckinRequest,
    VolunteerCreateRemove,
    ListVolunteersResponse,
)
from app.core.auth.dependencies import ClubAuth, DependsAuth
from app.db.core import SessionDep
from app.api.events.volunteer import service
from app.response import CustomHTTPException

router = APIRouter(prefix="/volunteer")


@router.post("/add", summary="Add volunteers to an event or club")
async def add_volunteers(
    session: SessionDep, user: ClubAuth, volunteer: VolunteerCreateRemove
) -> List[ListVolunteersResponse]:
    if not volunteer.event_id and not volunteer.club_id:
        raise CustomHTTPException(400, "Either event_id or club_id is required")

    if volunteer.event_id and volunteer.club_id:
        raise CustomHTTPException(400, "Only one of event_id or club_id is allowed")

    await service.add_volunteer(
        session,
        email_id=volunteer.email_id,
        event_id=volunteer.event_id,
        club_id=volunteer.club_id,
    )
    return await service.list_volunteers(session, volunteer.event_id)


@router.delete("/remove", summary="Remove volunteers from an event")
async def remove_volunteers(
    session: SessionDep, user: ClubAuth, volunteer: VolunteerCreateRemove
) -> List[ListVolunteersResponse]:
    if volunteer.event_id:
        await service.remove_event_volunteer(
            session, volunteer.email_id, volunteer.event_id
        )
    elif volunteer.club_id:
        await service.remove_club_volunteer(
            session, volunteer.email_id, volunteer.club_id
        )
    return await service.list_volunteers(session, volunteer.event_id)


@router.get("/list/{event_id}", summary="List all volunteers for an event")
async def list_volunteers(
    session: SessionDep, user: ClubAuth, event_id: int
) -> List[ListVolunteersResponse]:
    return await service.list_volunteers(session, event_id)


@router.post("/checkin/{event_id}", summary="Check-in a participant for an event")
async def checkin_participant(
    session: SessionDep, event_id: int, request: CheckinRequest, user: DependsAuth
) -> List[ListVolunteersResponse]:
    if volunteer := service.is_volunteer(session, user.id, event_id):
        await service.checkin_user(
            session, event_id, ticker_id=request.ticket_id, volunteer_id=volunteer.id
        )
        return {"message": "User checked-in"}
    raise CustomHTTPException(401, "User is not a volunteer for this event")
