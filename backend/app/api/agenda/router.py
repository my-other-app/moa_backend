from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.agenda import service
from app.api.agenda.schemas import AgendaItemIn, AgendaItemOut, AgendaItemPatch, AgendaOut
from app.api.checkins.gate_auth import load_event
from app.api.deliveries import service as deliveries
from app.api.events.documents import EVENT_TIMEZONE
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/agenda")


@router.get("", summary="The run of show, for anyone")
async def list_agenda(session: SessionDep, event_id: int) -> AgendaOut:
    """Public, like the event page itself: the app and the event website show it."""
    event = await load_event(session, event_id)
    return AgendaOut(items=[AgendaItemOut(**i) for i in await service.list_items(session, event.id)], timezone=EVENT_TIMEZONE, has_programme=await service.saved(session, event))


@router.post("", summary="A new block", status_code=201)
async def create_block(session: SessionDep, user: ClubAuth, event_id: int, body: AgendaItemIn) -> AgendaItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return AgendaItemOut(**await service.create_item(session, event, body))


@router.patch("/{item_id}", summary="Change a block")
async def update_block(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, body: AgendaItemPatch) -> AgendaItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return AgendaItemOut(**await service.update_item(session, event, item_id, body))


@router.delete("/{item_id}", summary="Remove a block")
async def delete_block(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_item(session, event, item_id)
    return {"ok": True}


@router.get("/programme.pdf", summary="The programme as designed, for printing")
async def programme(session: SessionDep, user: ClubAuth, event_id: int, page: Optional[str] = Query(None, pattern="^(a4_portrait|a4_landscape)$")) -> Response:
    """On the page it was designed for, or fitted onto A4. The sample sessions
    stand in until real ones exist, so the page is never blank."""
    event = await deliveries.event_for_club(session, user, event_id)
    pdf = await service.programme_pdf(session, event, page)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{event.slug}-programme.pdf"', "Cache-Control": "no-store"})
