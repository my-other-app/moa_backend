from typing import Optional
from uuid import UUID

from fastapi import APIRouter, File, Query, Request, UploadFile

from app.api.checkins.gate_auth import GateAuth
from app.api.deliveries import service as deliveries
from app.api.merch import service
from app.api.merch.schemas import (
    AttendeeMerch,
    CollectBody,
    CollectResult,
    FieldOut,
    MerchAnalytics,
    MerchItemIn,
    MerchItemOut,
    MerchItemPatch,
    MerchLog,
    VariantIn,
    VariantPatch,
)
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/merch")
gate_router = APIRouter(prefix="/events/{event_id}/gate/merch")


@router.get("", summary="Every item, with stock and what was handed out")
async def list_items(session: SessionDep, user: ClubAuth, event_id: int) -> list[MerchItemOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [MerchItemOut(**i) for i in await service.list_items(session, event.id)]


@router.get("/fields", summary="What an item may key eligibility or sizes on")
async def fields(session: SessionDep, user: ClubAuth, event_id: int) -> list[FieldOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [FieldOut(**f) for f in service.fields_for(event)]


@router.post("", summary="A new item", status_code=201)
async def create_item(session: SessionDep, user: ClubAuth, event_id: int, body: MerchItemIn) -> MerchItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchItemOut(**await service.create_item(session, event, body))


@router.post("/photo", summary="A product photo")
async def photo(session: SessionDep, user: ClubAuth, event_id: int, file: UploadFile = File(...)) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    return service.store_photo(event.id, await file.read())


@router.get("/collections", summary="Every handover, row by row")
async def collections(
    session: SessionDep, user: ClubAuth, event_id: int,
    q: Optional[str] = Query(None, max_length=100), item_id: Optional[UUID] = Query(None), variant_id: Optional[UUID] = Query(None), door_id: Optional[UUID] = Query(None),
    by: Optional[int] = Query(None), order: str = Query("newest", pattern="^(newest|oldest)$"), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> MerchLog:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchLog(**await service.log(session, event, q=q, item_id=item_id, variant_id=variant_id, door_id=door_id, by=by, order=order, page=page, page_size=page_size))


@router.get("/analytics", summary="The same handovers, counted")
async def analytics(session: SessionDep, user: ClubAuth, event_id: int) -> MerchAnalytics:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchAnalytics(**await service.analytics(session, event))


@router.patch("/{item_id}", summary="Change an item")
async def update_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, body: MerchItemPatch) -> MerchItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchItemOut(**await service.update_item(session, event, item_id, body))


@router.delete("/{item_id}", summary="Remove an item")
async def delete_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, with_collections: bool = Query(False)) -> dict:
    """An item some guests already collected takes their collections with it, only when asked."""
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_item(session, event, item_id, with_collections)
    return {"ok": True}


@router.post("/{item_id}/variants", summary="A new size or colour")
async def add_variant(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, body: VariantIn) -> MerchItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchItemOut(**await service.add_variant(session, event, item_id, body))


@router.patch("/{item_id}/variants/{variant_id}", summary="Change a variant")
async def update_variant(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, variant_id: UUID, body: VariantPatch) -> MerchItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchItemOut(**await service.update_variant(session, event, item_id, variant_id, body))


@router.delete("/{item_id}/variants/{variant_id}", summary="Remove a variant")
async def delete_variant(session: SessionDep, user: ClubAuth, event_id: int, item_id: UUID, variant_id: UUID) -> MerchItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return MerchItemOut(**await service.delete_variant(session, event, item_id, variant_id))


# ─── At the gate ──────────────────────────────────────────────────────────


def _may_hand_out(actor) -> None:
    if not actor.permissions["merch"]:
        raise CustomHTTPException(403, message="You may not hand out merchandise at this door.")


@gate_router.get("/{registration_id}", summary="What this guest is owed")
async def attendee_merch(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID) -> AttendeeMerch:
    """Attendee-first: find the person, and be shown exactly what they are owed."""
    _may_hand_out(actor)
    return AttendeeMerch(**await service.status_for(session, request.state.gate_event, registration_id))


@gate_router.post("/{item_id}/collect", summary="Hand it over")
async def collect(request: Request, session: SessionDep, actor: GateAuth, event_id: int, item_id: UUID, body: CollectBody) -> CollectResult:
    _may_hand_out(actor)
    return CollectResult(**await service.collect(session, request.state.gate_event, item_id, body.registration_id, actor, variant_label=body.variant_label, door_label=body.door))
