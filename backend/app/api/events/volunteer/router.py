from typing import List
from fastapi import APIRouter

from app.api.events.volunteer.schemas import VolunteerCreate, ListVolunteersResponse
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.api.events.volunteer import service

router = APIRouter(prefix="/volunteer")


@router.post("/set", summary="Add volunteers to an event")
async def add_volunteers(
    session: SessionDep, user: ClubAuth, volunteer: VolunteerCreate
) -> List[ListVolunteersResponse]:
    await service.update_volunteers(
        session, volunteer.email_ids, volunteer.event_id, user.club.id
    )
    return await service.list_volunteers(session, volunteer.event_id)


@router.get("/list/{event_id}", summary="List all volunteers for an event")
async def list_volunteers(
    session: SessionDep, user: ClubAuth, event_id: int
) -> List[ListVolunteersResponse]:
    return await service.list_volunteers(session, event_id)
