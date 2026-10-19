"""The club's store: items are the club's, each event offers the ones it
picks, and every event offering an item sells from its one shelf.

The plan's gates on offers are in test_plan_enforcement.py; the migration's
backfill is in test_club_merch_migration.py."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select

from app.api.events.models import EventCategories, Events
from app.api.payments import service as payments
from app.api.payments.handlers import handle_event_registration_payment
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store.models import EventStoreItems, EventStoreOffers, EventStoreOrders
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
API = "/api/v1"


@pytest.fixture
async def club_shop(client, session, make_club, make_event, mint_token, monkeypatch):
    owner, club = await make_club(name="Robotics Society", email="robotics@example.com")
    rival_owner, rival = await make_club(name="Rival Society", email="rival-shop@example.com")
    friday = await make_event(club=club, created_by=owner, name="Friday Build", event_datetime=NOW + timedelta(days=3), has_fee=True, reg_fee=100, merch_enabled=True)
    saturday = await make_event(club=club, created_by=owner, name="Saturday Demo", event_datetime=NOW + timedelta(days=4), has_fee=True, reg_fee=100, merch_enabled=True)
    meetup = await make_event(club=club, created_by=owner, name="Open Meetup", event_datetime=NOW + timedelta(days=5), has_fee=False, reg_fee=0, merch_enabled=True)
    theirs = await make_event(club=rival, created_by=rival_owner, name="Rival Night", event_datetime=NOW + timedelta(days=3), has_fee=True, reg_fee=100, merch_enabled=True)
    await session.commit()
    counter = {"n": 0}

    def fake_order_create(data):
        counter["n"] += 1
        return {"id": f"order_club_{counter['n']}", "amount": data["amount"], "currency": "INR", "status": "created"}

    monkeypatch.setattr(payments.razorpay_client.order, "create", fake_order_create)
    return dict(
        owner=owner, club=club, friday=friday, saturday=saturday, meetup=meetup, rival=rival, theirs=theirs,
        h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
    )


async def _make(client, headers, **body):
    r = await client.post(f"{API}/clubs/store", json={"name": "Tee", "price": 450, **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _offer(client, headers, event_id, ids):
    return await client.put(f"{API}/events/{event_id}/store/offers", json={"item_ids": ids}, headers=headers)


async def _register(client, event_id, email, picks=None, headers=None):
    payload = {"full_name": "Guest Buyer", "email": email, "phone": f"9{uuid.uuid4().int % 10**9:09d}"}
    if picks is not None:
        payload["store"] = picks
    return await client.post(f"{API}/events/registration/{event_id}/register", json=payload, headers=headers or {})


async def _pay(client, session, event_id, reg, amount):
    order = await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": event_id, "event_registration_id": reg["event_registration_id"]}},
        headers=auth_header(reg["auth_token"]["access_token"]),
    )
    assert order.status_code == 200, order.text
    row = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == order.json()["razorpay_order_id"]))
    session.add(PaymentLogs(order_id=row.id, razorpay_payment_id=f"pay_{uuid.uuid4().hex[:10]}", status=PaymentStatus.captured, amount=Decimal(amount), payment_method="upi"))
    row.status = OrderStatus.paid
    await session.commit()
    await handle_event_registration_payment(session, row)
    return order.json()


def _catalog(client, event_id):
    return client.get(f"{API}/events/{event_id}/store/catalog")


async def _listed(client, headers, event_id, item_id):
    return next((i for i in (await client.get(f"{API}/events/{event_id}/store", headers=headers)).json() if i["id"] == item_id), None)


async def _item_row(session, item_id) -> EventStoreItems:
    """The item's row as it is now, soft-deleted or not."""
    return await session.scalar(
        select(EventStoreItems).where(EventStoreItems.id == uuid.UUID(item_id)).execution_options(include_deleted=True, populate_existing=True)
    )


async def _club_item(client, headers, item_id):
    return next((i for i in (await client.get(f"{API}/clubs/store", headers=headers)).json()["items"] if i["id"] == item_id), None)


# ─── One shelf, several events ───────────────────────────────────────────


async def test_two_events_sell_from_one_items_stock(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Club tee", stock=2)
    assert tee["events"] == [] and (tee["sold"], tee["held"], tee["left"], tee["earned"]) == (0, 0, 2, 0.0)
    assert (await _offer(client, w["h"], w["friday"].id, [tee["id"]])).status_code == 200
    assert (await _offer(client, w["h"], w["saturday"].id, [tee["id"]])).status_code == 200
    assert (await _catalog(client, w["saturday"].id)).json()["items"][0]["left"] == 2

    # Sold on Friday: Saturday's shelf has one fewer.
    buyer = (await _register(client, w["friday"].id, "friday-buyer@example.com", [{"item_id": tee["id"], "qty": 1}])).json()
    await _pay(client, session, w["friday"].id, buyer, 550)
    assert (await _catalog(client, w["saturday"].id)).json()["items"][0]["left"] == 1

    # Each event's store says what it sold itself, and what the shelf has left.
    fri, sat = await _listed(client, w["h"], w["friday"].id, tee["id"]), await _listed(client, w["h"], w["saturday"].id, tee["id"])
    assert (fri["event_id"], fri["sold"], fri["earned"], fri["left"], fri["total"]) == (w["friday"].id, 1, 450.0, 1, 2)
    assert (sat["event_id"], sat["sold"], sat["earned"], sat["left"], sat["total"]) == (w["saturday"].id, 0, 0.0, 1, 2)

    # A hold on Friday takes the last piece from Saturday too.
    holder = await _register(client, w["friday"].id, "friday-holder@example.com", [{"item_id": tee["id"], "qty": 1}])
    assert holder.status_code == 200, holder.text
    assert (await _catalog(client, w["saturday"].id)).json()["items"] == []
    refused = await _register(client, w["saturday"].id, "saturday-buyer@example.com", [{"item_id": tee["id"], "qty": 1}])
    assert refused.status_code == 422 and refused.json()["error_code"] == "SOLD_OUT", refused.text

    # The club's catalogue: the shelf across both events, and who offers it.
    club_row = await _club_item(client, w["h"], tee["id"])
    assert (club_row["sold"], club_row["held"], club_row["left"], club_row["earned"]) == (1, 1, 0, 450.0)
    assert [e["id"] for e in club_row["events"]] == [w["friday"].id, w["saturday"].id]
    assert club_row["events"][0] == {"id": w["friday"].id, "name": "Friday Build", "slug": w["friday"].slug, "event_datetime": club_row["events"][0]["event_datetime"]}
    assert club_row["events"][0]["event_datetime"]

    # Stock raised once, for both events.
    raised = await client.patch(f"{API}/clubs/store/{tee['id']}", json={"stock": 4}, headers=w["h"])
    assert raised.status_code == 200 and raised.json()["left"] == 2
    assert (await _catalog(client, w["saturday"].id)).json()["items"][0]["left"] == 2
    assert (await _catalog(client, w["friday"].id)).json()["items"][0]["left"] == 2


async def test_sizes_are_shared_and_each_event_counts_its_own(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Sized tee", variants=[{"label": "S", "stock": 1}, {"label": "M", "stock": 3}])
    for e in (w["friday"], w["saturday"]):
        await _offer(client, w["h"], e.id, [tee["id"]])
    m_id = tee["variants"][1]["id"]
    a = (await _register(client, w["friday"].id, "sizes-a@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 2}])).json()
    await _pay(client, session, w["friday"].id, a, 1000)
    b = (await _register(client, w["saturday"].id, "sizes-b@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 1}])).json()
    await _pay(client, session, w["saturday"].id, b, 550)
    fri = (await client.get(f"{API}/events/{w['friday'].id}/store/summary", headers=w["h"])).json()
    sat = (await client.get(f"{API}/events/{w['saturday'].id}/store/summary", headers=w["h"])).json()
    assert fri["sizes"] == [{"label": "S", "sold": 0}, {"label": "M", "sold": 2}] and fri["units_sold"] == 2 and fri["revenue"] == 900.0
    assert sat["sizes"] == [{"label": "S", "sold": 0}, {"label": "M", "sold": 1}] and sat["units_sold"] == 1 and sat["revenue"] == 450.0
    club_row = await _club_item(client, w["h"], tee["id"])
    assert [(v["label"], v["sold"], v["left"]) for v in club_row["variants"]] == [("S", 0, 1), ("M", 3, 0)]
    assert club_row["sold"] == 3 and club_row["earned"] == 1350.0
    # The M is gone everywhere; the S is still on both events' shelves.
    for e in (w["friday"], w["saturday"]):
        item = (await _catalog(client, e.id)).json()["items"][0]
        assert [v["label"] for v in item["variants"]] == ["S"]


# ─── Only the club's own events, only what they offer ────────────────────


async def test_another_clubs_event_can_neither_offer_nor_sell_the_item(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Members tee", stock=10)
    refused = await _offer(client, w["rival_h"], w["theirs"].id, [tee["id"]])
    assert refused.status_code == 422 and refused.json()["error_code"] == "ITEM_UNAVAILABLE"
    assert refused.json()["errors"]["item_ids"] == [tee["id"]]
    # Not even through the organiser's own routes for their event, or the club's.
    assert (await client.patch(f"{API}/events/{w['theirs'].id}/store/{tee['id']}", json={"price": 1}, headers=w["rival_h"])).status_code == 404
    assert (await client.patch(f"{API}/clubs/store/{tee['id']}", json={"price": 1}, headers=w["rival_h"])).status_code == 404
    assert (await client.delete(f"{API}/clubs/store/{tee['id']}", headers=w["rival_h"])).status_code == 404
    assert (await client.get(f"{API}/clubs/store", headers=w["rival_h"])).json() == {"items": []}
    # And not by the club itself on the rival's event.
    assert (await client.get(f"{API}/events/{w['theirs'].id}/store/offers", headers=w["h"])).status_code == 403

    # An offer written past the checks still sells nothing there.
    await session.execute(insert(EventStoreOffers).values(event_id=w["theirs"].id, item_id=uuid.UUID(tee["id"]), sort_order=0))
    await session.commit()
    assert (await _catalog(client, w["theirs"].id)).json() == {"enabled": True, "items": []}
    r = await _register(client, w["theirs"].id, "crossover@example.com", [{"item_id": tee["id"], "qty": 1}])
    assert r.status_code == 422 and r.json()["error_code"] == "ITEM_UNAVAILABLE"
    assert (await client.get(f"{API}/events/{w['theirs'].id}/store", headers=w["rival_h"])).json() == []


async def test_an_item_the_event_does_not_offer_is_not_for_sale_there(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Friday only", stock=10)
    await _offer(client, w["h"], w["friday"].id, [tee["id"]])
    assert (await _catalog(client, w["saturday"].id)).json()["items"] == []
    r = await _register(client, w["saturday"].id, "not-offered@example.com", [{"item_id": tee["id"], "qty": 1}])
    assert r.status_code == 422 and r.json()["error_code"] == "ITEM_UNAVAILABLE"
    assert (await client.patch(f"{API}/events/{w['saturday'].id}/store/{tee['id']}", json={"price": 1}, headers=w["h"])).status_code == 404


# ─── The offers ──────────────────────────────────────────────────────────


async def test_the_offers_are_replaced_in_the_order_given(client, session, club_shop):
    w = club_shop
    url = f"{API}/events/{w['friday'].id}/store/offers"
    assert (await client.get(url, headers=w["h"])).json() == {"merch_enabled": True, "item_ids": []}
    tee = await _make(client, w["h"], name="Tee", stock=5)
    cap = await _make(client, w["h"], name="Cap", stock=5)
    mug = await _make(client, w["h"], name="Mug", stock=5)

    r = await _offer(client, w["h"], w["friday"].id, [mug["id"], tee["id"], mug["id"]])
    assert r.status_code == 200 and r.json() == {"merch_enabled": True, "item_ids": [mug["id"], tee["id"]]}
    assert [i["name"] for i in (await _catalog(client, w["friday"].id)).json()["items"]] == ["Mug", "Tee"]
    assert [i["name"] for i in (await client.get(f"{API}/events/{w['friday'].id}/store", headers=w["h"])).json()] == ["Mug", "Tee"]

    r = await _offer(client, w["h"], w["friday"].id, [cap["id"], mug["id"]])
    assert r.json()["item_ids"] == [cap["id"], mug["id"]]
    assert (await client.get(url, headers=w["h"])).json()["item_ids"] == [cap["id"], mug["id"]]

    # Each id must be a live item of the club: a deleted one, an unknown one, garbage.
    await client.delete(f"{API}/clubs/store/{tee['id']}", headers=w["h"])
    for bad in ([tee["id"]], [str(uuid.uuid4())]):
        r = await _offer(client, w["h"], w["friday"].id, [cap["id"], *bad])
        assert r.status_code == 422 and r.json()["error_code"] == "ITEM_UNAVAILABLE" and r.json()["errors"]["item_ids"] == bad, r.text
    assert (await _offer(client, w["h"], w["friday"].id, ["not-an-id"])).status_code == 422
    assert (await client.get(url, headers=w["h"])).json()["item_ids"] == [cap["id"], mug["id"]]

    # Nothing offered: nothing to buy, and the switch says so.
    assert (await _offer(client, w["h"], w["friday"].id, [])).json() == {"merch_enabled": True, "item_ids": []}
    assert (await _catalog(client, w["friday"].id)).json() == {"enabled": True, "items": []}
    await client.patch(f"{API}/events/{w['friday'].id}/store/settings", json={"merch_enabled": False}, headers=w["h"])
    assert (await client.get(url, headers=w["h"])).json()["merch_enabled"] is False


async def test_taking_an_item_off_an_event_leaves_its_orders(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Order tee", stock=5)
    await _offer(client, w["h"], w["friday"].id, [tee["id"]])
    buyer = (await _register(client, w["friday"].id, "kept-order@example.com", [{"item_id": tee["id"], "qty": 1}])).json()
    await _offer(client, w["h"], w["friday"].id, [])
    # The pending order is paid and sold as it was placed.
    await _pay(client, session, w["friday"].id, buyer, 550)
    order = await session.scalar(select(EventStoreOrders).where(EventStoreOrders.registration_id == uuid.UUID(buyer["event_registration_id"])).execution_options(populate_existing=True))
    assert order.status == "paid"
    assert (await client.get(f"{API}/events/{w['friday'].id}/store/orders", headers=w["h"])).json()["total"] == 1
    assert (await _club_item(client, w["h"], tee["id"]))["sold"] == 1


# ─── The event's own routes, as the old dashboard uses them ──────────────


async def test_making_an_item_from_an_event_puts_it_in_the_clubs_store(client, session, club_shop):
    w = club_shop
    made = await client.post(f"{API}/events/{w['friday'].id}/store", json={"name": "Event tee", "price": 300, "stock": 4}, headers=w["h"])
    assert made.status_code == 201 and made.json()["event_id"] == w["friday"].id
    row = await session.get(EventStoreItems, uuid.UUID(made.json()["id"]))
    assert (row.club_id, row.event_id) == (w["club"].id, w["friday"].id)
    club_row = await _club_item(client, w["h"], made.json()["id"])
    assert [e["id"] for e in club_row["events"]] == [w["friday"].id]
    # Offered on Saturday too, and changed from Saturday's store: one item.
    await _offer(client, w["h"], w["saturday"].id, [made.json()["id"]])
    r = await client.patch(f"{API}/events/{w['saturday'].id}/store/{made.json()['id']}", json={"price": 350}, headers=w["h"])
    assert r.status_code == 200 and r.json()["event_id"] == w["saturday"].id and r.json()["price"] == 350.0
    assert (await _catalog(client, w["friday"].id)).json()["items"][0]["price"] == 350.0


async def test_delete_on_an_event_takes_the_item_off_that_event_only(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Shared tee", stock=5)
    for e in (w["friday"], w["saturday"]):
        await _offer(client, w["h"], e.id, [tee["id"]])
    r = await client.delete(f"{API}/events/{w['friday'].id}/store/{tee['id']}", headers=w["h"])
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert await _listed(client, w["h"], w["friday"].id, tee["id"]) is None
    assert await _listed(client, w["h"], w["saturday"].id, tee["id"]) is not None
    assert [e["id"] for e in (await _club_item(client, w["h"], tee["id"]))["events"]] == [w["saturday"].id]
    # Off its last event with nothing sold or held: it leaves the club's store too.
    await client.delete(f"{API}/events/{w['saturday'].id}/store/{tee['id']}", headers=w["h"])
    assert await _club_item(client, w["h"], tee["id"]) is None
    assert (await _item_row(session, tee["id"])).is_deleted is True


async def test_an_item_with_sales_stays_in_the_clubs_store_when_its_last_event_drops_it(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Sold tee", stock=5)
    await _offer(client, w["h"], w["friday"].id, [tee["id"]])
    buyer = (await _register(client, w["friday"].id, "sold-before@example.com", [{"item_id": tee["id"], "qty": 1}])).json()
    await _pay(client, session, w["friday"].id, buyer, 550)
    assert (await client.delete(f"{API}/events/{w['friday'].id}/store/{tee['id']}", headers=w["h"])).json() == {"deleted": True}
    kept = await _club_item(client, w["h"], tee["id"])
    assert kept is not None and kept["events"] == [] and kept["sold"] == 1 and kept["on_sale"] is True
    # The club's own delete: out of the store, off sale (it was sold).
    assert (await client.delete(f"{API}/clubs/store/{tee['id']}", headers=w["h"])).json() == {"deleted": True}
    row = await _item_row(session, tee["id"])
    assert row.is_deleted is True and row.on_sale is False
    assert await _club_item(client, w["h"], tee["id"]) is None


# ─── The club's catalogue routes ─────────────────────────────────────────


async def test_the_clubs_store_lists_makes_edits_and_photographs(client, session, club_shop, make_admin, mint_token, monkeypatch):
    from app.core.storage import objects

    w = club_shop
    put = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: put.__setitem__(key, content_type))
    import io

    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 40, 40)).save(png, "PNG")
    photo = await client.post(f"{API}/clubs/store/photo", files={"file": ("tee.png", png.getvalue(), "image/png")}, headers=w["h"])
    assert photo.status_code == 200, photo.text
    assert f"/clubs/{w['club'].id}/merch/v-" in photo.json()["key"] and photo.json()["key"] in put and photo.json()["url"]
    assert (await client.post(f"{API}/clubs/store/photo", files={"file": ("x.txt", b"not an image", "text/plain")}, headers=w["h"])).status_code == 422

    tee = await _make(client, w["h"], name="Photo tee", stock=3, photo_key=photo.json()["key"], available_roles=["attendee"], free_roles=["speaker"])
    assert set(tee) == {
        "id", "name", "description", "photo_url", "photo_key", "price", "stock", "variants", "available_roles", "free_roles",
        "on_sale", "sort_order", "sold", "held", "left", "earned", "events",
    }
    assert tee["photo_key"] == photo.json()["key"] and tee["photo_url"] and tee["available_roles"] == ["attendee"] and tee["free_roles"] == ["speaker"]
    mug = await _make(client, w["h"], name="Mug", variants=[{"label": "Big", "stock": 2}])
    assert (mug["sort_order"], tee["sort_order"]) == (1, 0)
    assert mug["variants"][0] | {"id": None} == {"id": None, "label": "Big", "stock": 2, "sold": 0, "held": 0, "left": 2}
    assert (await client.post(f"{API}/clubs/store", json={"name": "x", "price": 10}, headers=w["h"])).status_code == 422

    edited = await client.patch(f"{API}/clubs/store/{tee['id']}", json={"name": "  Renamed tee ", "on_sale": False, "sort_order": 5}, headers=w["h"])
    assert edited.status_code == 200 and (edited.json()["name"], edited.json()["on_sale"], edited.json()["sort_order"]) == ("Renamed tee", False, 5)
    assert [i["name"] for i in (await client.get(f"{API}/clubs/store", headers=w["h"])).json()["items"]] == ["Mug", "Renamed tee"]

    # An admin names the club; without one, they are asked to.
    admin = await make_admin(email="store-admin@example.com")
    ah = auth_header(await mint_token(admin))
    assert (await client.get(f"{API}/clubs/store", headers=ah)).status_code == 422
    listed = await client.get(f"{API}/clubs/store", params={"club_id": w["club"].id}, headers=ah)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 2
    made = await client.post(f"{API}/clubs/store", params={"club_id": w["rival"].id}, json={"name": "Rival cap", "price": 99, "stock": 1}, headers=ah)
    assert made.status_code == 201
    assert [i["name"] for i in (await client.get(f"{API}/clubs/store", headers=w["rival_h"])).json()["items"]] == ["Rival cap"]
    assert (await client.get(f"{API}/clubs/store", params={"club_id": 10**8}, headers=ah)).status_code == 404
    # A club's own ?club_id= names nothing but its own club.
    assert len((await client.get(f"{API}/clubs/store", params={"club_id": w["rival"].id}, headers=w["h"])).json()["items"]) == 2


# ─── The guest's catalogue keeps its shape ───────────────────────────────


async def test_the_catalogue_answers_in_the_same_shape(client, session, club_shop):
    w = club_shop
    tee = await _make(client, w["h"], name="Shape tee", description="Soft", variants=[{"label": "M", "stock": 2}])
    kit = await _make(client, w["h"], name="Shape kit", price=890, stock=1)
    free = await _make(client, w["h"], name="Sticker", price=60, stock=9, free_roles=["attendee"])
    await _offer(client, w["h"], w["friday"].id, [kit["id"], tee["id"], free["id"]])
    body = (await _catalog(client, w["friday"].id)).json()
    assert set(body) == {"enabled", "items"} and body["enabled"] is True
    assert [i["name"] for i in body["items"]] == ["Shape kit", "Shape tee", "Sticker"]
    for item in body["items"]:
        assert set(item) == {"id", "name", "description", "photo_url", "price", "left", "variants"}
        for v in item["variants"]:
            assert set(v) == {"id", "label", "left"}
    k, t, s = body["items"]
    assert (k["price"], k["left"], k["variants"]) == (890.0, 1, [])
    assert (t["description"], t["left"], t["variants"]) == ("Soft", None, [{"id": tee["variants"][0]["id"], "label": "M", "left": 2}])
    assert s["price"] == 0.0
    # The switch off: the same empty answer as before.
    await client.patch(f"{API}/events/{w['friday'].id}/store/settings", json={"merch_enabled": False}, headers=w["h"])
    assert (await _catalog(client, w["friday"].id)).json() == {"enabled": False, "items": []}


# ─── Creating, editing and copying an event ──────────────────────────────


async def _category(session, owner) -> EventCategories:
    category = EventCategories(name="Builds", created_by_id=owner.id)
    session.add(category)
    await session.commit()
    return category


def _form(category, **extra) -> dict:
    return {
        "name": extra.pop("name", "Sunday Hack"), "event_datetime": (NOW + timedelta(days=9)).isoformat(), "duration": "2",
        "category_id": str(category.id), "location_name": "Lab 1", **extra,
    }


async def _offers_of(session, event_id) -> list[str]:
    return [str(i) for i in (await session.scalars(select(EventStoreOffers.item_id).where(EventStoreOffers.event_id == event_id).order_by(EventStoreOffers.sort_order))).all()]


async def test_an_event_is_created_with_its_store_switched_on_and_its_items(client, session, club_shop):
    w = club_shop
    category = await _category(session, w["owner"])
    tee, cap = await _make(client, w["h"], name="Tee", stock=5), await _make(client, w["h"], name="Cap", stock=5)
    import json

    r = await client.post(f"{API}/events/create", data=_form(category, merch_enabled="true", swag_enabled="true", store_item_ids=json.dumps([cap["id"], tee["id"]])), headers=w["h"])
    assert r.status_code == 200, r.text
    event = await session.get(Events, r.json()["id"])
    await session.refresh(event)
    assert event.merch_enabled is True and event.swag_enabled is True
    assert await _offers_of(session, event.id) == [cap["id"], tee["id"]]
    assert [i["name"] for i in (await _catalog(client, event.id)).json()["items"]] == ["Cap", "Tee"]

    # Without them, both switches stay off and nothing is offered.
    plain = await client.post(f"{API}/events/create", data=_form(category, name="Plain Night"), headers=w["h"])
    plain_event = await session.get(Events, plain.json()["id"])
    assert plain_event.merch_enabled is False and plain_event.swag_enabled is False and await _offers_of(session, plain_event.id) == []

    # Another club's item, or not a list: refused, and no event is made.
    before = await session.scalar(select(func.count(Events.id)))
    rival_item = await _make(client, w["rival_h"], name="Rival tee", stock=1)
    for bad in (json.dumps([tee["id"], rival_item["id"]]), "not json", json.dumps({"id": tee["id"]})):
        r = await client.post(f"{API}/events/create", data=_form(category, name="Bad Night", store_item_ids=bad), headers=w["h"])
        assert r.status_code == 422, (bad, r.text)
    assert await session.scalar(select(func.count(Events.id))) == before


async def test_an_edit_sets_the_offers_only_when_it_sends_them(client, session, club_shop):
    w = club_shop
    import json

    category = await _category(session, w["owner"])
    w["friday"].category_id = category.id
    await session.commit()
    tee, cap = await _make(client, w["h"], name="Tee", stock=5), await _make(client, w["h"], name="Cap", stock=5)
    await _offer(client, w["h"], w["friday"].id, [tee["id"]])
    url = f"{API}/events/update/{w['friday'].id}"
    form = _form(category, name="Friday Build", interest_ids="0", has_fee="true", reg_fee="100")
    r = await client.put(url, data=form, headers=w["h"])
    assert r.status_code == 200, r.text
    assert await _offers_of(session, w["friday"].id) == [tee["id"]]
    r = await client.put(url, data={**form, "store_item_ids": json.dumps([cap["id"], tee["id"]])}, headers=w["h"])
    assert r.status_code == 200 and await _offers_of(session, w["friday"].id) == [cap["id"], tee["id"]]
    r = await client.put(url, data={**form, "store_item_ids": json.dumps([str(uuid.uuid4())]), "name": "Renamed Build"}, headers=w["h"])
    assert r.status_code == 422 and r.json()["error_code"] == "ITEM_UNAVAILABLE"
    await session.refresh(w["friday"])
    assert w["friday"].name == "Friday Build" and await _offers_of(session, w["friday"].id) == [cap["id"], tee["id"]]
    r = await client.put(url, data={**form, "store_item_ids": "[]"}, headers=w["h"])
    assert r.status_code == 200 and await _offers_of(session, w["friday"].id) == []


async def test_a_copy_offers_the_same_items_and_keeps_the_switches(client, session, club_shop):
    w = club_shop
    w["friday"].swag_enabled = True
    await session.commit()
    tee, cap = await _make(client, w["h"], name="Tee", stock=5), await _make(client, w["h"], name="Cap", stock=5)
    await _offer(client, w["h"], w["friday"].id, [cap["id"], tee["id"]])
    items_before = await session.scalar(select(func.count(EventStoreItems.id)))
    r = await client.post(f"{API}/events/{w['friday'].id}/duplicate", json={"name": "Friday Build II", "event_datetime": (NOW + timedelta(days=10)).isoformat()}, headers=w["h"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["copied"]["store"] == 2 and body["skipped"] == []
    copy = await session.get(Events, body["id"])
    await session.refresh(copy)
    assert copy.merch_enabled is True and copy.swag_enabled is True
    assert await _offers_of(session, copy.id) == [cap["id"], tee["id"]]
    # The same items: one shelf for the original and its copy.
    assert await session.scalar(select(func.count(EventStoreItems.id))) == items_before
    assert [e["id"] for e in (await _club_item(client, w["h"], tee["id"]))["events"]] == [w["friday"].id, copy.id]
    # Without the merchandise, the offers stay behind; the switches still come.
    r = await client.post(f"{API}/events/{w['friday'].id}/duplicate", json={"name": "Friday Build III", "event_datetime": (NOW + timedelta(days=11)).isoformat(), "copy_merch": False}, headers=w["h"])
    third = await session.get(Events, r.json()["id"])
    assert await _offers_of(session, third.id) == [] and third.merch_enabled is True
