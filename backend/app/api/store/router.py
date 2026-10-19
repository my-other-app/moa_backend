from typing import Optional

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import select

from app.api.checkins.gate_auth import GateAuth
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.access import is_admin_user
from app.api.events.models import Events
from app.api.merch import service as merch_photos
from app.api.store import service
from app.api.store.schemas import (
    Catalog, ClubStore, ClubStoreItem, OffersIn, OffersOut, OrderOut, OrdersPage, SettingsPatch, StoreItemIn, StoreItemOut,
    StoreItemPatch, StoreSummary,
)
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/store")
club_router = APIRouter(prefix="/clubs/store")
public_router = APIRouter()


@router.get("", summary="The club's items this event offers: what each sold and earned here, and what is left on the shared shelf")
async def list_items(session: SessionDep, user: ClubAuth, event_id: int) -> list[StoreItemOut]:
    event = await deliveries.event_for_club(session, user, event_id)
    return [StoreItemOut(**i) for i in await service.list_items(session, event)]


@router.get("/offers", summary="Which of the club's store items this event offers, in order")
async def get_offers(session: SessionDep, user: ClubAuth, event_id: int) -> OffersOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return OffersOut(**await service.offers_out(session, event))


@router.put("/offers", summary="Set which of the club's store items this event offers, in this order")
async def put_offers(session: SessionDep, user: ClubAuth, event_id: int, body: OffersIn) -> OffersOut:
    """Replaces the event's offers. Each id must be a live item of the
    event's club (422 ITEM_UNAVAILABLE names the others). Offering more needs
    the plan's store and room under merch_items_per_event, with the event's
    handouts (a grandfathered event is exempt); taking items off is never
    refused, and leaves the orders already placed for them as they are."""
    event = await deliveries.event_for_club(session, user, event_id)
    return OffersOut(**await service.set_offers(session, event, body.item_ids, actor=user))


@router.get("/summary", summary="The store's figures: on sale, sold, earned, to hand over, sizes, latest orders")
async def summary(session: SessionDep, user: ClubAuth, event_id: int) -> StoreSummary:
    event = await deliveries.event_for_club(session, user, event_id)
    return StoreSummary(**await service.summary(session, event))


@router.patch("/settings", summary="Switch merchandise or swag on or off for this event")
async def settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.check_event_update(session, event, merch_enabled=body.merch_enabled, swag_enabled=body.swag_enabled, actor=user)
    if body.merch_enabled is not None:
        event.merch_enabled = body.merch_enabled
    if body.swag_enabled is not None:
        event.swag_enabled = body.swag_enabled
    await session.commit()
    return {"merch_enabled": event.merch_enabled, "swag_enabled": event.swag_enabled}


@router.post("", summary="Make an item in the club's store, offered on this event", status_code=201)
async def create_item(session: SessionDep, user: ClubAuth, event_id: int, body: StoreItemIn) -> StoreItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    if event.club_id is None:
        raise CustomHTTPException(422, message="This event has no club, so it has no store")
    await gates.check_merch_item(session, event, "merch", actor=user)
    m = await service.create_item(session, event.club_id, body, event=event)
    return StoreItemOut(**await service.event_item_out(session, event, m))


@router.post("/photo", summary="A photo for an item; the key goes on the item")
async def photo(session: SessionDep, user: ClubAuth, event_id: int, file: UploadFile = File(...)) -> dict:
    event = await deliveries.event_for_club(session, user, event_id)
    return merch_photos.store_photo(event.id, await file.read())


@router.patch("/{item_id}", summary="Change an item this event offers (for every event offering it): price, stock, sizes, who, on sale")
async def update_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: str, body: StoreItemPatch) -> StoreItemOut:
    event = await deliveries.event_for_club(session, user, event_id)
    m = await service.update_item(session, await service.item_for(session, event, item_id), body)
    return StoreItemOut(**await service.event_item_out(session, event, m))


@router.delete(
    "/{item_id}",
    summary="Stop offering an item on this event (it stays in the club's store unless it is then offered nowhere and nothing was sold or held)",
)
async def delete_item(session: SessionDep, user: ClubAuth, event_id: int, item_id: str) -> dict:
    """Removes this event's offer of the item; other events offering it, and
    orders already placed for it here, are untouched. When no other live
    event offers it and none of it was sold or is held, the item leaves the
    club's store too (soft-deleted). DELETE /clubs/store/{item_id} removes it
    from the club's store and so from every event."""
    event = await deliveries.event_for_club(session, user, event_id)
    await service.remove_offer(session, event, await service.item_for(session, event, item_id))
    return {"deleted": True}


@router.get("/orders", summary="Who bought what, and whether they have it yet")
async def orders(session: SessionDep, user: ClubAuth, event_id: int, q: Optional[str] = Query(None, max_length=120), status: Optional[str] = Query(None, pattern="^(paid|refunded|pending|oversold)$"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> OrdersPage:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrdersPage(**await service.orders(session, event, q=q, status=status, page=page, page_size=page_size))


@router.post("/orders/{order_id}/handover", summary="Mark an order handed over")
async def handover(session: SessionDep, user: ClubAuth, event_id: int, order_id: str) -> OrderOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrderOut(**await service.hand_over(session, await service.order_for(session, event, order_id), user.id))


@router.post("/orders/{order_id}/fulfil", summary="Settle an oversold order by handing it over after all")
async def fulfil(session: SessionDep, user: ClubAuth, event_id: int, order_id: str) -> OrderOut:
    """An order paid for when its pieces had sold out: once the organiser
    has raised the stock, the pieces are taken and the order is paid (409
    SOLD_OUT while they are still not there). The other way to settle one is
    refunding the payment."""
    event = await deliveries.event_for_club(session, user, event_id)
    return OrderOut(**await service.fulfil(session, await service.order_for(session, event, order_id)))


@router.post("/orders/{order_id}/handover/undo", summary="Take a handover back")
async def undo_handover(session: SessionDep, user: ClubAuth, event_id: int, order_id: str) -> OrderOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return OrderOut(**await service.hand_over(session, await service.order_for(session, event, order_id), user.id, undo=True))


# ─── The club's store catalogue ──────────────────────────────────────────


async def _club(session, user, club_id: Optional[int]) -> Clubs:
    """The caller's own club; an admin names one with ?club_id=."""
    if is_admin_user(user):
        if club_id is None:
            raise CustomHTTPException(422, message="Name the club: ?club_id=", errors={"club_id": "required for an admin"})
        club = await session.scalar(select(Clubs).where(Clubs.id == club_id, Clubs.is_deleted == False))  # noqa: E712
    else:
        club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(404, message="Club not found" if is_admin_user(user) else "No club for this account")
    return club


@club_router.get("", summary="Every item in the club's store: sold, held, left and earned across its events, and the events offering it")
async def club_items(session: SessionDep, user: ClubAuth, club_id: Optional[int] = Query(None, description="Admin only: the club")) -> ClubStore:
    club = await _club(session, user, club_id)
    return ClubStore(items=[ClubStoreItem(**i) for i in await service.club_items(session, club.id)])


@club_router.post("", summary="Make an item in the club's store (offered on no event yet)", status_code=201)
async def club_create(session: SessionDep, user: ClubAuth, body: StoreItemIn, club_id: Optional[int] = Query(None, description="Admin only: the club")) -> ClubStoreItem:
    club = await _club(session, user, club_id)
    await gates.require_feature(session, club.id, "merch", actor=user)
    m = await service.create_item(session, club.id, body)
    return ClubStoreItem(**await service.club_item(session, club.id, m))


@club_router.post("/photo", summary="A photo for an item in the club's store; the key goes on the item")
async def club_photo(session: SessionDep, user: ClubAuth, file: UploadFile = File(...), club_id: Optional[int] = Query(None, description="Admin only: the club")) -> dict:
    club = await _club(session, user, club_id)
    return service.club_photo(club.id, await file.read())


@club_router.patch("/{item_id}", summary="Change an item, for every event offering it: price, stock, sizes, who, on sale")
async def club_update(session: SessionDep, user: ClubAuth, item_id: str, body: StoreItemPatch, club_id: Optional[int] = Query(None, description="Admin only: the club")) -> ClubStoreItem:
    club = await _club(session, user, club_id)
    m = await service.update_item(session, await service.club_item_for(session, club.id, item_id), body)
    return ClubStoreItem(**await service.club_item(session, club.id, m))


@club_router.delete("/{item_id}", summary="Take an item out of the club's store, and so off every event (off sale too when any was sold or is held)")
async def club_delete(session: SessionDep, user: ClubAuth, item_id: str, club_id: Optional[int] = Query(None, description="Admin only: the club")) -> dict:
    club = await _club(session, user, club_id)
    await service.delete_item(session, await service.club_item_for(session, club.id, item_id))
    return {"deleted": True}


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
