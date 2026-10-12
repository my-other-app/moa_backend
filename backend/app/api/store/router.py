from typing import Optional

from fastapi import APIRouter, File, Query, UploadFile

from app.api.checkins.gate_auth import GateAuth
from app.api.deliveries import service as deliveries
from app.api.events.models import Events
from app.api.merch import service as merch_photos
from app.api.store import service
from app.api.store.schemas import Catalog, OrderOut, OrdersPage, SettingsPatch, StoreItemIn, StoreItemOut, StoreItemPatch, StoreSummary
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/store")
public_router = APIRouter()


@router.get("", summary="The items on the shelf, with what each sold and earned")
async def list_items(session: SessionDep, user: ClubAuth, event_id: int) -> list[StoreItemOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [StoreItemOut(**i) for i in await service.list_items(session, event)]


@router.get("/summary", summary="The store's figures: on sale, sold, earned, to hand over, sizes, latest orders")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> StoreSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return StoreSummary(**await service.summary(session, event))


@router.patch("/settings", summary="Switch merchandise or swag on or off for this event")
async def settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    if body.merch_enabled is not None:
        event.merch_enabled = body.merch_enabled
    if body.swag_enabled is not None:
        event.swag_enabled = body.swag_enabled
    await session.commit()
    return {"merch_enabled": event.merch_enabled, "swag_enabled": event.swag_enabled}


@router.post("", summary="Put an item on the shelf", status_code=201)
async def create_item(session: SessionDep, user: ClubAuth, event_id: int, body: StoreItemIn) -> StoreItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return StoreItemOut(**await service.create_item(session, event, body))


@router.post("/photo", summary="A photo for an item; the key goes on the item")
async def photo(session: SessionDep, user: ClubAuth, event_id: int, file: UploadFile = File(...)) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    return merch_photos.store_photo(event.id, await file.read())


@router.patch("/{item_id}", summary="Change an item: price, stock, sizes, who, on sale")
async def update_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: str, body: StoreItemPatch) -> StoreItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return StoreItemOut(**await service.update_item(session, event, await service.item_for(session, event, item_id), body))


@router.delete("/{item_id}", summary="Take an item off the shelf")
async def delete_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: str) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.delete_item(session, await service.item_for(session, event, item_id))
    return {"deleted": True}


@router.get("/orders", summary="Who bought what, and whether they have it yet")
async def orders(session: SessionDep, user: ClubAuth, event_id: int, q: Optional[str] = Query(None, max_length=120), status: Optional[str] = Query(None, pattern="^(paid|refunded|pending)$"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> OrdersPage:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrdersPage(**await service.orders(session, event, q=q, status=status, page=page, page_size=page_size))


@router.post("/orders/{order_id}/handover", summary="Mark an order handed over")
async def handover(session: SessionDep, user: ClubAuth, event_id: int, order_id: str) -> OrderOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrderOut(**await service.hand_over(session, await service.order_for(session, event, order_id), user.id))


@router.post("/orders/{order_id}/handover/undo", summary="Take a handover back")
async def undo_handover(session: SessionDep, user: ClubAuth, event_id: int, order_id: str) -> OrderOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrderOut(**await service.hand_over(session, await service.order_for(session, event, order_id), user.id, undo=True))


# ─── Guests and the door ─────────────────────────────────────────────────


@public_router.get("/events/{event_id}/store/catalog", summary="What a guest can buy with their ticket")
async def catalog(session: SessionDep, event_id: int) -> Catalog:
    event = await session.get(Events, event_id)
    if event is None or event.is_deleted:
        raise CustomHTTPException(404, message="Event not found")
    return Catalog(**await service.catalog(session, event))


def _may_hand_out(actor) -> None:
    if not actor.permissions.get("merch") and not actor.permissions.get("full"):
        raise CustomHTTPException(403, message="This door does not hand out merchandise")


@public_router.get("/events/{event_id}/gate/store/{registration_id}", summary="A guest's paid orders, for the door")
async def gate_orders(session: SessionDep, actor: GateAuth, event_id: int, registration_id: str) -> list[OrderOut]:
    _may_hand_out(actor)
    event = await session.get(Events, event_id)
    return [OrderOut(**o) for o in await service.orders_for_registration(session, event, registration_id)]


@public_router.post("/events/{event_id}/gate/store/{order_id}/handover", summary="Hand an order over at the door")
async def gate_handover(session: SessionDep, actor: GateAuth, event_id: int, order_id: str) -> OrderOut:
    _may_hand_out(actor)
    event = await session.get(Events, event_id)
    return OrderOut(**await service.hand_over(session, await service.order_for(session, event, order_id), (actor.user.id if getattr(actor, "user", None) else None)))
