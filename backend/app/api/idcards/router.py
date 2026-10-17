"""ID cards: the summary, the print rules, each guest's card and batches."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.checkins.gate_auth import GateAuth
from app.api.deliveries import service as deliveries
from app.api.idcards import service
from app.api.idcards.models import PrintSource
from app.api.idcards.schemas import CardSettings, CardSettingsUpdate, CardSummary, PrintJobOut
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/events/{event_id}/idcards")


@router.get("/summary", summary="How many cards exist, and the print rules")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> CardSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return CardSummary(**await service.summary(session, event))


@router.patch("/settings", summary="Who may print again, auto-print, pre-printed stock")
async def settings(session: SessionDep, user: ClubAuth, event_id: int, body: CardSettingsUpdate) -> CardSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "badges", actor=user)
    return CardSettings(**await service.update_settings(session, event, body))


@router.get("/jobs", summary="Cards put out, newest first")
async def jobs(session: SessionDep, user: ClubAuth, event_id: int) -> list[PrintJobOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [PrintJobOut(**j) for j in await service.recent_jobs(session, event)]


@router.get("/batch.pdf", summary="Every card, a page each")
async def batch(
    session: SessionDep, user: ClubAuth, event_id: int,
    scope: str = Query("all", pattern="^(all|not_printed)$"), role: Optional[str] = Query(None, pattern="^(attendee|speaker|staff)$"), record: bool = Query(False),
) -> Response:
    """`record` marks every card in it as printed, which is what the reprint
    rule and the "not yet printed" scope read."""
    from app.api.checkins.gate_auth import GateActor

    event = await deliveries.event_for_club(session, user, event_id)
    actor = GateActor(kind="user", name=user.full_name or user.email, user=user, full=True)
    pdf, n = await service.batch_pdf(session, event, scope=scope, role=role, record=record, actor=actor)
    name = f"{event.slug}-idcards{'-' + role if role else ''}{'-unprinted' if scope == 'not_printed' else ''}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"', "X-Cards": str(n)})


@router.get("/{registration_id}.pdf", summary="One guest's card")
async def card(
    request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID,
    source: str = Query("manual", pattern="^(checkin|manual|walkup|kiosk|test)$"), record: bool = Query(True), override: bool = Query(False), door: Optional[str] = Query(None, max_length=40),
) -> Response:
    """From the console or the attendees page. Recording it (the default)
    is what the reprint rule turns on; `record=false` is a preview."""
    event = request.state.gate_event
    pdf = await service.card_pdf(session, event, registration_id, actor=actor, source=PrintSource(source), record=record, override=override and actor.full, door=door)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="idcard-{registration_id}.pdf"', "Cache-Control": "no-store"})
