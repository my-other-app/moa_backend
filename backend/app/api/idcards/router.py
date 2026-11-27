"""ID cards: the summary, the print rules, each guest's card and batches."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.checkins.gate_auth import GateFrameAuth
from app.api.deliveries import service as deliveries
from app.api.idcards import service
from app.api.idcards.models import PrintSource
from app.api.kiosk import print_requests
from app.api.kiosk.schemas import DashboardPrintBody, PrinterOut, PrintRequestList, PrintRequestOut
from app.api.idcards.schemas import CardSettings, CardSettingsUpdate, CardSummary, PrintJobOut
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.core.auth.permissions import EXPORT_DATA, ensure_admin_can
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/idcards")


@router.get("/summary", summary="How many cards exist, and the print rules")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> CardSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return CardSummary(**await service.summary(session, event))


@router.patch("/settings", summary="Who may print again, auto-print, pre-printed stock")
async def settings(session: SessionDep, user: ClubAuth, event_id: int, body: CardSettingsUpdate) -> CardSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "badges", actor=user, event=event)
    return CardSettings(**await service.update_settings(session, event, body))


@router.get("/jobs", summary="Cards put out, newest first")
async def jobs(session: SessionDep, user: ClubAuth, event_id: int) -> list[PrintJobOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [PrintJobOut(**j) for j in await service.recent_jobs(session, event)]


@router.get("/printers", summary="The print stations paired to this event, and whether they are online")
async def printers(session: SessionDep, user: ClubAuth, event_id: int) -> list[PrinterOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [PrinterOut(**p) for p in await print_requests.list_printers(session, event)]


@router.post("/print-requests", summary="Send a guest's card to a print station", status_code=201)
async def print_request(session: SessionDep, user: ClubAuth, event_id: int, body: DashboardPrintBody, response: Response) -> PrintRequestOut:
    """Staff are organisers for the reprint rule (`override` opens it for one
    card). 201 queued; 200 with the open request when one is already waiting."""
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "badges", actor=user, event=event)
    reg = await print_requests.registration_in(session, event, body.registration_id)
    door_id = None
    if body.door_id is not None:
        from app.api.checkins import doors as doors_mod

        door_id = (await doors_mod.door_of(session, event, body.door_id)).id
    out, created = await print_requests.ask(session, event, reg, source="dashboard", user_id=user.id, printer_id=body.printer_id, door_id=door_id, override=body.override, by_organiser=True)
    response.status_code = 201 if created else 200
    return PrintRequestOut(**out)


@router.get("/print-requests", summary="The print queue, newest first")
async def print_queue(session: SessionDep, user: ClubAuth, event_id: int, status: str = Query("open", pattern="^(open|done|failed|all)$"), limit: int = Query(50, ge=1, le=200)) -> PrintRequestList:
    event = await deliveries.event_for_club(session, user, event_id)
    return PrintRequestList(**await print_requests.listing(session, event, status, limit))


@router.post("/print-requests/{request_id}/retry", summary="Queue a failed or cancelled card again")
async def print_retry(session: SessionDep, user: ClubAuth, event_id: int, request_id: UUID) -> PrintRequestOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return PrintRequestOut(**await print_requests.retry(session, event, request_id))


@router.post("/print-requests/{request_id}/cancel", summary="Take a card out of the queue")
async def print_cancel(session: SessionDep, user: ClubAuth, event_id: int, request_id: UUID) -> PrintRequestOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return PrintRequestOut(**await print_requests.cancel(session, event, request_id))


@router.get("/batch.pdf", summary="Every card, a page each")
async def batch(
    session: SessionDep, user: ClubAuth, event_id: int,
    scope: str = Query("all", pattern="^(all|not_printed)$"), role: Optional[str] = Query(None, pattern="^(attendee|speaker|staff)$"), record: bool = Query(False),
) -> Response:
    """`record` marks every card in it as printed, which is what the reprint
    rule and the "not yet printed" scope read. Every guest's card, with the
    QR the door scans: for an admin, a data export (EXPORT_DATA)."""
    from app.api.checkins.gate_auth import GateActor

    ensure_admin_can(user, EXPORT_DATA)
    event = await deliveries.event_for_club(session, user, event_id)
    actor = GateActor(kind="user", name=user.full_name or user.email, user=user, full=True)
    pdf, n = await service.batch_pdf(session, event, scope=scope, role=role, record=record, actor=actor)
    name = f"{event.slug}-idcards{'-' + role if role else ''}{'-unprinted' if scope == 'not_printed' else ''}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"', "X-Cards": str(n)})


@router.get("/{registration_id}.pdf", summary="One guest's card")
async def card(
    request: Request, session: SessionDep, actor: GateFrameAuth, event_id: int, registration_id: UUID,
    source: str = Query("manual", pattern="^(checkin|manual|walkup|kiosk|test)$"), record: bool = Query(False), override: bool = Query(False), door: Optional[str] = Query(None, max_length=40),
) -> Response:
    """From the console or the attendees page. `record=true` records the
    print, which is what the reprint rule turns on; without it, a preview.
    It recorded by default, so any link to a card -- opened by staff signed
    in, from anywhere -- marked it printed and the door's reprint was then
    refused. The door key comes in the X-Gate-Key header only."""
    event = request.state.gate_event
    pdf = await service.card_pdf(session, event, registration_id, actor=actor, source=PrintSource(source), record=record, override=override and actor.full, door=door)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="idcard-{registration_id}.pdf"', "Cache-Control": "no-store"})
