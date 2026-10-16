from fastapi import APIRouter

from app.api.deliveries import service as deliveries
from app.api.organisers import service
from app.api.organisers.schemas import OrganiserOut, OrganisersIn
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/organisers")


@router.get("", summary="Who from the club's team runs this event, in the page's order")
async def list_organisers(session: SessionDep, user: ClubAuth, event_id: int) -> list[OrganiserOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [OrganiserOut(**o) for o in await service.list_organisers(session, event)]


@router.put("", summary="Name the event's organisers; the list replaces the last one, in its order")
async def set_organisers(session: SessionDep, user: ClubAuth, event_id: int, body: OrganisersIn) -> list[OrganiserOut]:
    """Everyone must be on the club's team. The event page shows them in place of the club's whole team."""
    event = await deliveries.event_for_club(session, user, event_id)
    return [OrganiserOut(**o) for o in await service.replace(session, event, body.members)]
