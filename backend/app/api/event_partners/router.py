from fastapi import APIRouter, File, UploadFile

from app.api.deliveries import service as deliveries
from app.api.event_partners import service
from app.api.event_partners.schemas import (
    EventPartnerIn,
    EventPartnerOrder,
    EventPartnerOut,
    EventPartnerPatch,
    FromClubIn,
    FromClubOut,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/partners")


@router.get("", summary="The event's sponsors, then its partners, each in its order")
async def list_partners(session: SessionDep, user: ClubAuth, event_id: int) -> list[EventPartnerOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [EventPartnerOut(**service.out(p)) for p in await service.list_rows(session, event.id)]


@router.post("", summary="Add a sponsor or a partner, at the end of its kind", status_code=201)
async def create_partner(session: SessionDep, user: ClubAuth, event_id: int, body: EventPartnerIn) -> EventPartnerOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return EventPartnerOut(**await service.create(session, event, body.model_dump()))


@router.put("/order", summary="Put one kind in a new order")
async def order_partners(session: SessionDep, user: ClubAuth, event_id: int, body: EventPartnerOrder) -> list[EventPartnerOut]:
    """`ids` names every live row of `kind`, each once; the rest of the list stays as it is."""
    event = await deliveries.event_for_club(session, user, event_id)
    return [EventPartnerOut(**p) for p in await service.reorder(session, event, body.kind, body.ids)]


@router.post("/from-club", summary="Add some of the club's own partners to this event", status_code=201)
async def add_from_club(session: SessionDep, user: ClubAuth, event_id: int, body: FromClubIn) -> FromClubOut:
    """Copies each one's name, logo and role (as the label); one already on the event is skipped."""
    event = await deliveries.event_for_club(session, user, event_id)
    return FromClubOut(**await service.from_club(session, event, body.kind, body.club_partner_ids))


@router.patch("/{partner_id}", summary="Correct a sponsor or partner")
async def update_partner(session: SessionDep, user: ClubAuth, event_id: int, partner_id: int, body: EventPartnerPatch) -> EventPartnerOut:
    event = await deliveries.event_for_club(session, user, event_id)
    p = await service.row_for(session, event, partner_id)
    return EventPartnerOut(**await service.update(session, event, p, body.model_dump(exclude_unset=True)))


@router.put("/{partner_id}/logo", summary="A sponsor's or partner's logo: square, 1080 x 1080 is best")
async def update_partner_logo(session: SessionDep, user: ClubAuth, event_id: int, partner_id: int, logo: UploadFile = File(...)) -> EventPartnerOut:
    if not (logo.content_type or "").startswith("image/"):
        raise CustomHTTPException(422, message="The logo must be an image")
    event = await deliveries.event_for_club(session, user, event_id)
    p = await service.row_for(session, event, partner_id)
    return EventPartnerOut(**await service.set_logo(session, p, logo))


@router.delete("/{partner_id}", summary="Remove a sponsor or partner")
async def delete_partner(session: SessionDep, user: ClubAuth, event_id: int, partner_id: int) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.remove(session, await service.row_for(session, event, partner_id))
    return {"deleted": True}
