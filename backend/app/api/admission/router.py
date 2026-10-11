from fastapi import APIRouter

from app.api.admission import service
from app.api.admission.schemas import AdmissionPatch, AdmissionSettings, NumberedResult
from app.api.deliveries import service as deliveries
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/admission")


@router.get("", summary="How this event numbers its tickets")
async def get_settings(session: SessionDep, user: ClubAuth, event_id: int) -> AdmissionSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    return AdmissionSettings(**await service.settings(session, event))


@router.patch("", summary="Change the format for the next tickets")
async def patch_settings(session: SessionDep, user: ClubAuth, event_id: int, body: AdmissionPatch) -> AdmissionSettings:
    """Numbers already given out are never rewritten."""
    event = await deliveries.event_for_club(session, user, event_id)
    return AdmissionSettings(**await service.update_settings(session, event, body))


@router.post("/number-all", summary="Number everyone registered so far")
async def number_all(session: SessionDep, user: ClubAuth, event_id: int) -> NumberedResult:
    event = await deliveries.event_for_club(session, user, event_id)
    return NumberedResult(**await service.number_all(session, event))
