from typing import List
from fastapi import APIRouter

from app.api.events.volunteer.schemas import (
    VolunteerCreateRemove,
    ListVolunteersResponse,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.api.events.volunteer import service

router = APIRouter(prefix="/volunteer")


@router.post("/add", summary="Add volunteers to an event")
async def add_volunteers(
    session: SessionDep, user: ClubAuth, volunteer: VolunteerCreateRemove
) -> List[ListVolunteersResponse]:
    await service.add_volunteer(
        session,
        email_id=volunteer.email_id,
        event_id=volunteer.event_id,
        club_id=user.club.id,
    )
    return await service.list_volunteers(session, volunteer.event_id)


@router.delete("/remove", summary="Remove volunteers from an event")
async def remove_volunteers(
    session: SessionDep, user: ClubAuth, volunteer: VolunteerCreateRemove
) -> List[ListVolunteersResponse]:
    await service.remove_volunteer(session, volunteer.email_id, volunteer.event_id)
    return await service.list_volunteers(session, volunteer.event_id)


@router.get("/list/{event_id}", summary="List all volunteers for an event")
async def list_volunteers(
    session: SessionDep, user: ClubAuth, event_id: int
) -> List[ListVolunteersResponse]:
    return await service.list_volunteers(session, event_id)
