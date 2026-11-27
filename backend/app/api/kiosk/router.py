"""Kiosks: paired from the dashboard, run by a tablet at a door, and the
phone pages its QR opens."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from app.api.deliveries import service as deliveries
from app.api.kiosk import print_requests, self_service, service
from app.api.kiosk.auth import KioskAuth, KioskFrameAuth, PrinterAuth
from app.api.kiosk.schemas import (
    ClaimResult,
    KioskContext,
    KioskCreate,
    KioskDeviceOut,
    KioskDoorUpdate,
    KioskRotate,
    KioskSettings,
    KioskSettingsUpdate,
    LookupBody,
    PairedKiosk,
    PrintDoneBody,
    PrintedBody,
    PrintRequestBody,
    PrintRequestOut,
    RedeemBody,
    RedeemResult,
    SelfQr,
    SessionBody,
    StaffBody,
    TokenBody,
    VerifyBody,
)
from app.core.auth.dependencies import ClubAuth
from app.core.net import client_address
from app.db.core import SessionDep

admin_router = APIRouter(prefix="/events/{event_id}/kiosks")
kiosk_router = APIRouter(prefix="/kiosk")
self_router = APIRouter(prefix="/self")


# --- pairing, from the dashboard -------------------------------------------------------


@admin_router.get("", summary="The kiosks paired to this event")
async def list_kiosks(session: SessionDep, user: ClubAuth, event_id: int) -> list[KioskDeviceOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [KioskDeviceOut(**d) for d in await service.list_devices(session, event)]


@admin_router.post("", summary="Pair a kiosk: a token shown once")
async def create_kiosk(session: SessionDep, user: ClubAuth, event_id: int, body: KioskCreate) -> PairedKiosk:
    event = await deliveries.event_for_club(session, user, event_id)
    return PairedKiosk(**await service.create_device(session, event, name=body.name, door_label=body.door_label, door_id=body.door_id, shift_hours=body.shift_hours, created_by=user.id, role=body.role))


@admin_router.get("/settings", summary="Self check-in and the staff PIN")
async def kiosk_settings(session: SessionDep, user: ClubAuth, event_id: int) -> KioskSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    return KioskSettings(**service.kiosk_settings(event))


@admin_router.patch("/settings", summary="Switch self check-in on, pick the proof, set the PIN")
async def update_kiosk_settings(session: SessionDep, user: ClubAuth, event_id: int, body: KioskSettingsUpdate) -> KioskSettings:
    event = await deliveries.event_for_club(session, user, event_id)
    return KioskSettings(**await service.update_kiosk_settings(session, event, body))


@admin_router.patch("/{device_id}", summary="Pick the door a print station prints for")
async def update_kiosk(session: SessionDep, user: ClubAuth, event_id: int, device_id: UUID, body: KioskDoorUpdate) -> KioskDeviceOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return KioskDeviceOut(**await service.set_printer_door(session, event, device_id, body.door_id))


@admin_router.post("/{device_id}/rotate", summary="A new token for a new shift")
async def rotate_kiosk(session: SessionDep, user: ClubAuth, event_id: int, device_id: UUID, body: KioskRotate) -> PairedKiosk:
    event = await deliveries.event_for_club(session, user, event_id)
    return PairedKiosk(**await service.rotate_device(session, event, device_id, shift_hours=body.shift_hours))


@admin_router.delete("/{device_id}", summary="Unpair a kiosk")
async def revoke_kiosk(session: SessionDep, user: ClubAuth, event_id: int, device_id: UUID) -> KioskDeviceOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return KioskDeviceOut(**await service.revoke_device(session, event, device_id))


# --- the kiosk itself --------------------------------------------------------------------


@kiosk_router.get("/context", summary="Everything the kiosk screen needs")
async def kiosk_context(session: SessionDep, device: KioskAuth) -> KioskContext:
    return KioskContext(**await service.context(session, device))


@kiosk_router.post("/redeem", summary="A ticket at the kiosk")
async def kiosk_redeem(session: SessionDep, device: KioskAuth, body: RedeemBody, background_tasks: BackgroundTasks) -> RedeemResult:
    return RedeemResult(**await service.redeem(session, device, code=body.code, client_ref=body.client_ref, background=background_tasks))


@kiosk_router.post("/not-me", summary="“This isn't me”: undo a check-in this kiosk just made")
async def kiosk_not_me(session: SessionDep, device: KioskAuth, body: TokenBody) -> dict:
    return await service.not_me(session, device, body.token)


@kiosk_router.get("/card.pdf", summary="The card for a guest this kiosk just admitted")
async def kiosk_card(session: SessionDep, device: KioskFrameAuth, redemption: str = Query(..., min_length=10)) -> Response:
    pdf = await service.card_for_redemption(session, device, redemption)
    return Response(content=pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@kiosk_router.post("/staff", summary="Staff mode, by the event's PIN")
async def kiosk_staff(session: SessionDep, device: KioskAuth, body: StaffBody) -> dict:
    """Five tries per kiosk in fifteen minutes, then 429 until the window passes."""
    return await service.staff(session, device, body.pin)


@kiosk_router.get("/self-qr", summary="The rotating QR for check-in from a phone, and who just used it")
async def kiosk_self_qr(session: SessionDep, device: KioskAuth) -> SelfQr:
    return SelfQr(**await self_service.kiosk_qr(session, device))


@kiosk_router.get("/self-card.pdf", summary="The card a phone asked to print here")
async def kiosk_self_card(session: SessionDep, device: KioskFrameAuth, session_id: str = Query(..., alias="session", min_length=10)) -> Response:
    from app.api.idcards.models import PrintSource
    from app.api.idcards.service import card_pdf
    from app.api.kiosk.auth import kiosk_actor

    s = await self_service._load(session, session_id)
    if s.kiosk_id != device.id or not s.checked_in or not s.registration_id:
        from app.response import CustomHTTPException

        raise CustomHTTPException(404, message="Nothing to print for that session here.")
    pdf = await card_pdf(session, device.event, s.registration_id, actor=kiosk_actor(device), source=PrintSource.kiosk, record=True, door=device.door_label)
    await self_service.mark_printed(session, device, session_id)
    return Response(content=pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@kiosk_router.post("/self-printed", summary="The kiosk has printed a phone's card")
async def kiosk_self_printed(session: SessionDep, device: KioskAuth, body: PrintedBody) -> dict:
    await self_service.mark_printed(session, device, body.session)
    return {"ok": True}


# --- print requests from the tablet ---------------------------------------------------------


@kiosk_router.post("/print-requests", summary="Send a guest's ID card to a print station", status_code=201)
async def kiosk_print_request(session: SessionDep, device: KioskAuth, body: PrintRequestBody, response: Response) -> PrintRequestOut:
    """201 when queued; 200 with the open request when this guest's card is already waiting (a double tap)."""
    out, created = await print_requests.kiosk_ask(session, device, redemption=body.redemption, session_id=body.session)
    response.status_code = 201 if created else 200
    return PrintRequestOut(**out)


@kiosk_router.get("/print-requests/{request_id}", summary="Where a card this kiosk asked for has got to")
async def kiosk_print_request_status(session: SessionDep, device: KioskAuth, request_id: UUID) -> PrintRequestOut:
    return PrintRequestOut(**await print_requests.of_device(session, device, request_id))


# --- the print station (a device paired as a printer) -----------------------------------------


@kiosk_router.post("/printer/claim", summary="A print station's heartbeat, and the next card to print")
async def printer_claim(session: SessionDep, device: PrinterAuth) -> ClaimResult:
    return ClaimResult(**await print_requests.claim(session, device))


@kiosk_router.get("/printer/requests/{request_id}/card.pdf", summary="The card this station has claimed")
async def printer_card(session: SessionDep, device: PrinterAuth, request_id: UUID) -> Response:
    pdf = await print_requests.card_for(session, device, request_id)
    return Response(content=pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@kiosk_router.post("/printer/requests/{request_id}/done", summary="Printed, or failed")
async def printer_done(session: SessionDep, device: PrinterAuth, request_id: UUID, body: PrintDoneBody) -> PrintRequestOut:
    return PrintRequestOut(**await print_requests.done(session, device, request_id, ok=body.ok, error=body.error))


@kiosk_router.post("/printer/test", summary="A sample card to print as a test; nothing is recorded")
async def printer_test(session: SessionDep, device: PrinterAuth) -> Response:
    pdf = await print_requests.test_card(session, device)
    return Response(content=pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


# --- the guest's phone --------------------------------------------------------------------


@self_router.post("/start", summary="A scanned kiosk QR becomes a session on this phone")
async def self_start(request: Request, session: SessionDep, body: TokenBody) -> dict:
    return await self_service.start(session, body.token, address=client_address(request))


@self_router.post("/lookup", summary="Their name, matched whole against the guest list")
async def self_lookup(session: SessionDep, body: LookupBody) -> dict:
    return await self_service.lookup(session, body.session, body.name, body.contact)


@self_router.post("/verify", summary="The six-digit code")
async def self_verify(session: SessionDep, body: VerifyBody) -> dict:
    return await self_service.verify(session, body.session, body.code)


@self_router.post("/resend", summary="A fresh code, once")
async def self_resend(session: SessionDep, body: SessionBody) -> dict:
    return await self_service.resend(session, body.session)


@self_router.post("/check-in", summary="Through the door at the kiosk's gate")
async def self_check_in(session: SessionDep, body: SessionBody, background_tasks: BackgroundTasks) -> dict:
    return await self_service.check_in(session, body.session, background=background_tasks)


@self_router.post("/print", summary="One tap: the card prints at the kiosk")
async def self_print(session: SessionDep, body: SessionBody) -> dict:
    return await self_service.print_now(session, body.session)


@self_router.get("/status", summary="Checked in? Printed?")
async def self_status(session: SessionDep, session_id: str = Query(..., alias="session", min_length=10)) -> dict:
    return await self_service.status(session, session_id)
