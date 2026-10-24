"""Sending tickets, and what became of them."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.deliveries import service
from app.api.deliveries.models import DeliveryChannel, DeliveryKind
from app.api.deliveries.schemas import (
    DeliveryLog,
    DeliverySummary,
    RevokeRequest,
    SendResponse,
    SendTicketsRequest,
    TicketState,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}")


@router.post("/tickets/send", summary="Email tickets to the guests who have not had theirs")
async def send_tickets(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    body: SendTicketsRequest,
) -> SendResponse:
    """Never re-sends a channel already sent unless `only` is `all`. A guest
    with no address on a channel is skipped, not failed. The sending happens
    after this answers; the summary says how it is going."""
    event = await service.event_for_club(session, user, event_id)
    result = await service.queue(
        session,
        event,
        DeliveryKind.ticket,
        registration_ids=body.registration_ids,
        only=body.only,
        force=body.only == "all",
        send=True,
    )
    return SendResponse(**{k: v for k, v in result.items() if k != "ids"})


@router.post("/tickets/{registration_id}/send", summary="One guest's ticket again")
async def send_one(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    registration_id: UUID,
) -> SendResponse:
    """A guest who lost the email, or a corrected address. Earlier deliveries
    do not stop it."""
    event = await service.event_for_club(session, user, event_id)
    result = await service.queue(
        session, event, DeliveryKind.ticket, registration_ids=[registration_id], only="all", force=True, send=True
    )
    return SendResponse(**{k: v for k, v in result.items() if k != "ids"})


@router.post("/tickets/{registration_id}/revoke", summary="Refuse this ticket at the door")
async def revoke(
    session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID, body: RevokeRequest
) -> TicketState:
    event = await service.event_for_club(session, user, event_id)
    reg = await service.set_revoked(session, event, registration_id, True, body.reason)
    return TicketState(registration_id=reg.id, ticket_id=reg.ticket_id, revoked_at=reg.ticket_revoked_at)


@router.post("/tickets/{registration_id}/reinstate", summary="Accept this ticket again")
async def reinstate(session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID) -> TicketState:
    event = await service.event_for_club(session, user, event_id)
    reg = await service.set_revoked(session, event, registration_id, False)
    return TicketState(registration_id=reg.id, ticket_id=reg.ticket_id, revoked_at=reg.ticket_revoked_at)


@router.get("/tickets/bundle.zip", summary="Every ticket PDF, one file per guest")
async def bundle(session: SessionDep, user: ClubAuth, event_id: int) -> Response:
    event = await service.event_for_club(session, user, event_id)
    content = await service.bundle_zip(session, event)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{event.slug}-tickets.zip"'},
    )


@router.get("/deliveries/summary", summary="How many were reached, per channel")
async def deliveries_summary(
    session: SessionDep, user: ClubAuth, event_id: int, kind: DeliveryKind = Query(DeliveryKind.ticket)
) -> DeliverySummary:
    event = await service.event_for_club(session, user, event_id)
    return DeliverySummary(**await service.summary(session, event, kind))


@router.get("/deliveries", summary="Every send to every guest, newest first")
async def deliveries_log(
    session: SessionDep,
    user: ClubAuth,
    event_id: int,
    kind: Optional[str] = Query(None, pattern="^(ticket|certificate|reminder|message)$"),
    channel: Optional[DeliveryChannel] = Query(None),
    problem: bool = Query(False, description="Only sends that failed or came back"),
    search: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> DeliveryLog:
    event = await service.event_for_club(session, user, event_id)
    return DeliveryLog(
        **await service.log(
            session, event, kind=kind, channel=channel, problem=problem, search=search, page=page, page_size=page_size
        )
    )
