"""The event store: items on a shelf, picked at registration, paid with the ticket, handed over at the door — and swag counted by hand."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink
from app.api.payments import service as payments
from app.api.payments.handlers import handle_event_registration_payment
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store.models import EventStoreOrders
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, mint_token, monkeypatch):
    owner, club = await make_club(name="Circuit Society", email="store@example.com")
    rival_owner, rival = await make_club(name="Rivals", email="rivals-store@example.com")
    club.is_verified = True
    paid = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW + timedelta(days=7), has_fee=True, reg_fee=100)
    free = await make_event(club=club, created_by=owner, name="Open Lab", event_datetime=NOW + timedelta(days=9), has_fee=False, reg_fee=0)
    await session.commit()
    counter = {"n": 0}

    def fake_order_create(data):
        counter["n"] += 1
        return {"id": f"order_test_{counter['n']}", "amount": data["amount"], "currency": "INR", "status": "created"}

    monkeypatch.setattr(payments.razorpay_client.order, "create", fake_order_create)
    return dict(owner=owner, club=club, paid=paid, free=free, h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)), orders=counter)


def _store(event_id, path=""):
    return f"/api/v1/events/{event_id}/store{path}"


async def _register(client, event_id, email, picks=None):
    payload = {"full_name": "Guest Buyer", "email": email, "phone": f"9{uuid.uuid4().int % 10**9:09d}"}
    if picks is not None:
        payload["store"] = picks
    r = await client.post(f"/api/v1/events/registration/{event_id}/register", json=payload)
    return r


async def _capture(session, razorpay_order_id: str, amount):
    """What Razorpay's capture does to our tables, without Razorpay."""
    order = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == razorpay_order_id))
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{uuid.uuid4().hex[:10]}", status=PaymentStatus.captured, amount=Decimal(amount), payment_method="upi"))
    order.status = OrderStatus.paid
    await session.commit()
    await handle_event_registration_payment(session, order)
    return order


async def test_the_shelf_is_set_up_and_shown_to_guests_only_when_on(client, world):
    h, event = world["h"], world["paid"]
    # Off by default: nothing to buy.
    assert (await client.get(_store(event.id, "/catalog"))).json() == {"enabled": False, "items": []}
    assert (await client.patch(_store(event.id, "/settings"), json={"merch_enabled": True, "swag_enabled": True}, headers=h)).json() == {"merch_enabled": True, "swag_enabled": True}
    info = (await client.get(f"/api/v1/events/info/{event.id}", headers=h)).json()
    assert info["merch_enabled"] is True and info["swag_enabled"] is True

    tee = await client.post(_store(event.id), json={"name": "Hack Night tee", "price": 450, "variants": [{"label": "S", "stock": 2}, {"label": "M", "stock": 3}], "free_roles": ["speaker"]}, headers=h)
    assert tee.status_code == 201, tee.text
    t = tee.json()
    assert t["total"] == 5 and t["left"] == 5 and t["price"] == 450.0 and t["sold_out"] is False and [v["label"] for v in t["variants"]] == ["S", "M"]
    kit = (await client.post(_store(event.id), json={"name": "ESP32-S3 starter kit", "price": 890, "stock": 1}, headers=h)).json()
    lanyard = (await client.post(_store(event.id), json={"name": "Club lanyard", "price": 80, "stock": 100, "on_sale": False}, headers=h)).json()
    staff_only = (await client.post(_store(event.id), json={"name": "Crew hoodie", "price": 1200, "stock": 8, "available_roles": ["staff"]}, headers=h)).json()
    for bad in ({"name": "x", "price": 10}, {"name": "x", "price": 10, "stock": 5, "variants": [{"label": "S", "stock": 1}]}, {"name": "x", "price": 10, "stock": 1, "available_roles": ["vip"]}, {"name": "x", "price": -1, "stock": 1}):
        assert (await client.post(_store(event.id), json=bad, headers=h)).status_code == 422, bad

    cat = (await client.get(_store(event.id, "/catalog"))).json()
    assert cat["enabled"] is True and [i["name"] for i in cat["items"]] == ["Hack Night tee", "ESP32-S3 starter kit"]
    assert cat["items"][0]["variants"] == [{"id": t["variants"][0]["id"], "label": "S", "left": 2}, {"id": t["variants"][1]["id"], "label": "M", "left": 3}] and cat["items"][1]["left"] == 1

    # Edits: price, a size's stock, on sale; a size cannot go below what sold (nothing yet); another club sees nothing.
    edited = await client.patch(_store(event.id, f"/{t['id']}"), json={"price": 500, "variants": [{"label": "S", "stock": 4}, {"label": "M", "stock": 3}, {"label": "L", "stock": 1}]}, headers=h)
    assert edited.status_code == 200 and edited.json()["price"] == 500.0 and edited.json()["total"] == 8
    assert (await client.patch(_store(event.id, f"/{lanyard['id']}"), json={"on_sale": True}, headers=h)).json()["on_sale"] is True
    assert (await client.get(_store(event.id), headers=world["rival_h"])).status_code == 403
    assert (await client.get(_store(event.id, "/summary"), headers=h)).json()["items_on_sale"] == 4
    assert (await client.delete(_store(event.id, f"/{staff_only['id']}"), headers=h)).json() == {"deleted": True}
    assert len((await client.get(_store(event.id), headers=h)).json()) == 3
    assert kit["left"] == 1


async def test_picks_are_paid_with_the_ticket_and_handed_over_once(client, session, world):
    h, event = world["h"], world["paid"]
    await client.patch(_store(event.id, "/settings"), json={"merch_enabled": True}, headers=h)
    tee = (await client.post(_store(event.id), json={"name": "Hack Night tee", "price": 450, "variants": [{"label": "S", "stock": 2}, {"label": "M", "stock": 3}]}, headers=h)).json()
    kit = (await client.post(_store(event.id), json={"name": "ESP32-S3 starter kit", "price": 890, "stock": 1}, headers=h)).json()
    m_id = tee["variants"][1]["id"]

    # A pick without a size, a sold-out pick, and a hidden pick are refused before any money moves.
    assert (await _register(client, event.id, "nosize@example.com", [{"item_id": tee["id"], "qty": 1}])).status_code == 422
    assert (await _register(client, event.id, "toomany@example.com", [{"item_id": kit["id"], "qty": 2}])).status_code == 422

    r = await _register(client, event.id, "aarav@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 2}, {"item_id": kit["id"], "qty": 1}])
    assert r.status_code == 200, r.text
    reg = r.json()
    assert reg["pay_amount"] == 100 + 900 + 890 and reg["store_total"] == 1790.0 and reg["store_order_id"]
    # Nothing is sold until the money lands.
    assert (await client.get(_store(event.id, "/summary"), headers=h)).json()["units_sold"] == 0

    order = await client.post("/api/v1/payments/orders", json={"source": "event_registration", "payload": {"event_id": event.id, "event_registration_id": reg["event_registration_id"]}}, headers=auth_header(reg["auth_token"]["access_token"]))
    assert order.status_code == 200, order.text
    assert float(order.json()["amount"]) == 1890.0
    # Changing the picks before paying makes a fresh order for the new amount, not the stale one.
    again = await _register(client, event.id, "aarav@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 1}])
    assert again.status_code == 200 and again.json()["pay_amount"] == 550
    order2 = await client.post("/api/v1/payments/orders", json={"source": "event_registration", "payload": {"event_id": event.id, "event_registration_id": reg["event_registration_id"]}}, headers=auth_header(reg["auth_token"]["access_token"]))
    assert float(order2.json()["amount"]) == 550.0 and order2.json()["razorpay_order_id"] != order.json()["razorpay_order_id"]

    await _capture(session, order2.json()["razorpay_order_id"], 550)
    row = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == uuid.UUID(reg["event_registration_id"])))
    await session.refresh(row)
    assert row.is_paid is True and float(row.paid_amount) == 100.0
    s = (await client.get(_store(event.id, "/summary"), headers=h)).json()
    assert (s["units_sold"], s["orders_paid"], s["revenue"], s["ticket_revenue"], s["to_hand_over"]) == (1, 1, 450.0, 100.0, 1)
    assert s["sizes_item"] == "Hack Night tee" and s["sizes"] == [{"label": "S", "sold": 0}, {"label": "M", "sold": 1}]
    assert s["latest_orders"][0]["guest"] == "Guest Buyer" and s["latest_orders"][0]["summary"] == "Hack Night tee (M)" and s["latest_orders"][0]["handed_over_at"] is None
    items = (await client.get(_store(event.id), headers=h)).json()
    tee_now = next(i for i in items if i["id"] == tee["id"])
    assert tee_now["sold"] == 1 and tee_now["left"] == 4 and tee_now["earned"] == 450.0
    # The kit was dropped from the final picks, so it is still on the shelf.
    assert next(i for i in items if i["id"] == kit["id"])["left"] == 1

    # Handed over at the door, once; undone, and over again.
    oid = s["latest_orders"][0]["id"]
    handed = await client.post(_store(event.id, f"/orders/{oid}/handover"), headers=h)
    assert handed.status_code == 200 and handed.json()["handed_over_at"]
    assert (await client.get(_store(event.id, "/summary"), headers=h)).json()["to_hand_over"] == 0
    assert (await client.post(_store(event.id, f"/orders/{oid}/handover/undo"), headers=h)).json()["handed_over_at"] is None
    page = (await client.get(_store(event.id, "/orders"), params={"q": "aarav"}, headers=h)).json()
    assert page["total"] == 1 and page["items"][0]["lines"][0]["variant_label"] == "M"

    # Finance sees the shelf money beside the ticket money.
    fin = (await client.get(f"/api/v1/events/{event.id}/finance", headers=h)).json()
    assert fin["figures"]["tickets"] == 100.0 and fin["figures"]["merch"] == 450.0 and fin["figures"]["gross"] == 550.0

    # A refund gives the size back to the shelf.
    log = await session.scalar(select(PaymentLogs).order_by(PaymentLogs.created_at.desc()))
    await session.refresh(log, ["order"])
    await payments._apply_refund(session, log)
    assert (await session.scalar(select(EventStoreOrders.status).where(EventStoreOrders.id == uuid.UUID(oid)))) == "refunded"
    assert next(i for i in (await client.get(_store(event.id), headers=h)).json() if i["id"] == tee["id"])["left"] == 5


async def test_a_free_event_still_sells_and_a_free_pick_needs_no_payment(client, session, world):
    h, event = world["h"], world["free"]
    await client.patch(_store(event.id, "/settings"), json={"merch_enabled": True}, headers=h)
    sticker = (await client.post(_store(event.id), json={"name": "Sticker pack", "price": 60, "stock": 200, "free_roles": ["attendee"]}, headers=h)).json()
    tee = (await client.post(_store(event.id), json={"name": "Tee", "price": 450, "stock": 10}, headers=h)).json()
    # Free for attendees: the catalogue says so, and the order is paid the moment it is placed.
    assert (await client.get(_store(event.id, "/catalog"))).json()["items"][0]["price"] == 0.0
    r = await _register(client, event.id, "free-pick@example.com", [{"item_id": sticker["id"], "qty": 1}])
    assert r.status_code == 200 and r.json()["pay_amount"] == 0 and r.json()["store_total"] == 0.0
    assert (await client.get(_store(event.id, "/summary"), headers=h)).json()["units_sold"] == 1
    # A paid pick at a free event: the ticket costs nothing, the tee does, one payment.
    r = await _register(client, event.id, "tee-buyer@example.com", [{"item_id": tee["id"], "qty": 1}])
    assert r.status_code == 200 and r.json()["pay_amount"] == 450
    order = await client.post("/api/v1/payments/orders", json={"source": "event_registration", "payload": {"event_id": event.id, "event_registration_id": r.json()["event_registration_id"]}}, headers=auth_header(r.json()["auth_token"]["access_token"]))
    assert order.status_code == 200 and float(order.json()["amount"]) == 450.0
    await _capture(session, order.json()["razorpay_order_id"], 450)
    row = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == uuid.UUID(r.json()["event_registration_id"])))
    await session.refresh(row)
    assert row.is_paid is True and float(row.paid_amount) == 0.0
    s = (await client.get(_store(event.id, "/summary"), headers=h)).json()
    assert s["revenue"] == 450.0 and s["units_sold"] == 2 and s["ticket_revenue"] == 0.0
    # With the shelf switched off, picks are refused.
    await client.patch(_store(event.id, "/settings"), json={"merch_enabled": False}, headers=h)
    assert (await _register(client, event.id, "late@example.com", [{"item_id": tee["id"], "qty": 1}])).status_code == 422


async def test_swag_is_counted_by_hand_and_names_its_sponsor(client, world):
    h, event = world["h"], world["paid"]
    made = await client.post(f"/api/v1/events/{event.id}/merch", json={"name": "Dev board voucher", "stock": 3, "sponsor": "Kinetic Labs", "handout_rule": "winners"}, headers=h)
    assert made.status_code == 201, made.text
    item = made.json()
    assert item["sponsor"] == "Kinetic Labs" and item["handout_rule"] == "winners" and item["handed_out"] == 0
    url = f"/api/v1/events/{event.id}/merch/{item['id']}/handout"
    assert (await client.post(url, headers=h)).json()["handed_out"] == 1
    assert (await client.post(url, headers=h)).json()["handed_out"] == 2
    assert (await client.post(f"{url}/undo", headers=h)).json()["handed_out"] == 1
    await client.post(url, headers=h)
    await client.post(url, headers=h)
    assert (await client.post(url, headers=h)).status_code == 422  # nothing left
    assert (await client.patch(f"/api/v1/events/{event.id}/merch/{item['id']}", json={"sponsor": "", "handout_rule": "door"}, headers=h)).json()["handout_rule"] == "door"
