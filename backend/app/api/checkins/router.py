"""Doors, the team, the gate and the check-in log."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request

from app.api.checkins import doors as doors_mod
from app.api.checkins import service
from app.api.checkins.gate_auth import GateAuth, load_event
from app.api.checkins.models import CheckInVia, DoorKind
from app.api.checkins.schemas import (
    ByHandBody,
    CaptureBody,
    CaptureResult,
    CheckInLog,
    DoorCreate,
    DoorOut,
    DoorUpdate,
    GateContext,
    GateSearchRow,
    GateSettings,
    GateSettingsUpdate,
    LinkOut,
    LinkRequest,
    ScanBatchBody,
    ScanBatchResult,
    ScanBody,
    ScanResult,
    TeamOut,
    VolunteerUpdate,
    WalkupBody,
    WalkupResult,
)
from app.api.deliveries import service as deliveries
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}")


# --- doors ----------------------------------------------------------------------


@router.get("/doors", summary="Every door on the event")
async def list_doors(session: SessionDep, user: ClubAuth, event_id: int) -> list[DoorOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [DoorOut(**d) for d in await doors_mod.list_doors(session, event)]


@router.post("/doors", summary="A new door")
async def create_door(session: SessionDep, user: ClubAuth, event_id: int, body: DoorCreate) -> DoorOut:
    event = await deliveries.event_for_club(session, user, event_id)
    door = await doors_mod.create_door(session, event, name=body.name, kind=DoorKind(body.kind))
    return DoorOut(**next(d for d in await doors_mod.list_doors(session, event) if d["id"] == door.id))


@router.patch("/doors/{door_id}", summary="Rename, retire or reorder a door")
async def update_door(session: SessionDep, user: ClubAuth, event_id: int, door_id: UUID, body: DoorUpdate) -> DoorOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await doors_mod.update_door(session, event, door_id, **body.model_dump(exclude_unset=True))
    return DoorOut(**next(d for d in await doors_mod.list_doors(session, event) if d["id"] == door_id))


@router.delete("/doors/{door_id}", summary="Delete a door nobody was admitted at")
async def delete_door(session: SessionDep, user: ClubAuth, event_id: int, door_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await doors_mod.delete_door(session, event, door_id)
    return {"deleted": True}


@router.post("/doors/{door_id}/link", summary="A gate link for this door")
async def issue_link(session: SessionDep, user: ClubAuth, event_id: int, door_id: UUID, body: LinkRequest) -> LinkOut:
    """Shown once. A phone that opens it runs the console on this door; the
    previous link stops working."""
    event = await deliveries.event_for_club(session, user, event_id)
    return LinkOut(**await doors_mod.issue_link(session, event, door_id, can_walkup=body.can_walkup, days=body.days))


@router.delete("/doors/{door_id}/link", summary="Stop the door's gate link working")
async def revoke_link(session: SessionDep, user: ClubAuth, event_id: int, door_id: UUID) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await doors_mod.revoke_link(session, event, door_id)
    return {"revoked": True}


# --- the team ----------------------------------------------------------------------


@router.get("/team", summary="Who runs the door, and what each may do")
async def team(session: SessionDep, user: ClubAuth, event_id: int) -> TeamOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return TeamOut(**await service.team(session, event))


@router.patch("/team/{volunteer_id}", summary="A volunteer's switches and door")
async def update_volunteer(session: SessionDep, user: ClubAuth, event_id: int, volunteer_id: int, body: VolunteerUpdate) -> TeamOut:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.update_volunteer(session, event, volunteer_id, body)
    return TeamOut(**await service.team(session, event))


@router.patch("/gate-settings", summary="How the door behaves")
async def gate_settings(session: SessionDep, user: ClubAuth, event_id: int, body: GateSettingsUpdate) -> GateSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    return GateSettings(**await service.update_gate_settings(session, event, body))


# --- the gate ----------------------------------------------------------------------


def _event(request: Request):
    return request.state.gate_event


@router.get("/gate/context", summary="Everything the console needs on open")
async def gate_context(request: Request, session: SessionDep, actor: GateAuth, event_id: int) -> GateContext:
    return GateContext(**await service.context(session, _event(request), actor))


@router.post("/gate/scan", summary="A ticket at the door. Six outcomes, all 200.")
async def gate_scan(request: Request, session: SessionDep, actor: GateAuth, event_id: int, body: ScanBody) -> ScanResult:
    if not actor.permissions["check_in"]:
        raise CustomHTTPException(403, message="You may not check guests in at this door.")
    return ScanResult(**await service.scan(session, _event(request), code=body.code, door_label=body.door, client_ref=body.client_ref, scanned_at=body.scanned_at, actor=actor, via=CheckInVia.gate))


@router.post("/gate/scan-batch", summary="The offline queue, drained")
async def gate_scan_batch(request: Request, session: SessionDep, actor: GateAuth, event_id: int, body: ScanBatchBody) -> ScanBatchResult:
    if not actor.permissions["check_in"]:
        raise CustomHTTPException(403, message="You may not check guests in at this door.")
    return ScanBatchResult(results=await service.scan_batch(session, _event(request), [s.model_dump() for s in body.scans], actor))


@router.get("/gate/search", summary="Find a guest by name, email or ticket")
async def gate_search(request: Request, session: SessionDep, actor: GateAuth, event_id: int, q: str = Query("", max_length=100)) -> list[GateSearchRow]:
    if not actor.permissions["check_in"]:
        raise CustomHTTPException(403, message="You may not check guests in at this door.")
    return [GateSearchRow(**r) for r in await service.search(session, _event(request), q)]


@router.post("/gate/check-in/{registration_id}", summary="Check a guest in by hand")
async def gate_check_in(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID, body: ByHandBody) -> ScanResult:
    """A dead phone, a ticket left at home, a name read off a list."""
    if not actor.permissions["check_in"]:
        raise CustomHTTPException(403, message="You may not check guests in at this door.")
    return ScanResult(**await service.check_in_by_hand(session, _event(request), registration_id, body.door, actor))


@router.delete("/gate/check-in/{registration_id}", summary="Take a check-in back")
async def gate_undo(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID) -> dict:
    """For the one it landed on by mistake. The club only: a volunteer's door
    has a scan behind it and offers nothing like this."""
    if not actor.full:
        raise CustomHTTPException(403, message="Only the organiser can undo a check-in.")
    return await service.undo(session, _event(request), registration_id, actor)


@router.post("/gate/walkup", summary="Register someone at the door and check them in")
async def gate_walkup(request: Request, session: SessionDep, actor: GateAuth, event_id: int, body: WalkupBody, background_tasks: BackgroundTasks) -> WalkupResult:
    if not actor.permissions["walkup"]:
        raise CustomHTTPException(403, message="Walk-ups are not switched on for this door.")
    return WalkupResult(**await service.walkup(session, _event(request), body, actor, background_tasks))


@router.post("/gate/capture/{registration_id}", summary="A phone number asked for at the door")
async def gate_capture(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID, body: CaptureBody) -> CaptureResult:
    return CaptureResult(**await service.capture_contact(session, _event(request), registration_id, body.phone))


# --- the log ----------------------------------------------------------------------


@router.get("/check-ins", summary="Every admission: where, how, by whom")
async def check_in_log(
    session: SessionDep, user: ClubAuth, event_id: int,
    q: Optional[str] = Query(None, max_length=100), door_id: Optional[UUID] = Query(None), via: Optional[str] = Query(None, pattern="^(gate|kiosk|walkup|offline_sync|desk|phone|app)$"),
    by: Optional[int] = Query(None), order: str = Query("newest", pattern="^(newest|oldest)$"), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> CheckInLog:
    event = await deliveries.event_for_club(session, user, event_id)
    return CheckInLog(**await service.log(session, event, q=q, door_id=door_id, via=via, by=by, order=order, page=page, page_size=page_size))
