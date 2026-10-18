"""The store's stock under concurrency: pieces held when picked, sold when the
money lands, given back when dropped or when the hold runs out, an order paid
for after its pieces went left `oversold` for the organiser, and a paid order
handed over exactly once."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.admin_alerts.models import AdminNotifications
from app.api.events.models import EventRegistrationsLink
from app.api.payments import service as payments
from app.api.payments.handlers import handle_event_registration_payment
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store import service as store
from app.api.store.models import EventStoreItems, EventStoreOrders, EventStoreVariants
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def shop(client, session, make_club, make_event, mint_token, monkeypatch):
    owner, club = await make_club(name="Holds Society", email="holds@example.com")
    event = await make_event(club=club, created_by=owner, name="Holds Night", event_datetime=NOW + timedelta(days=7), has_fee=True, reg_fee=100, merch_enabled=True)
    await session.commit()
    counter = {"n": 0}

    def fake_order_create(data):
        counter["n"] += 1
        return {"id": f"order_holds_{counter['n']}", "amount": data["amount"], "currency": "INR", "status": "created"}

    monkeypatch.setattr(payments.razorpay_client.order, "create", fake_order_create)
    return dict(owner=owner, club=club, event=event, h=auth_header(await mint_token(owner)))


def _store(event_id, path=""):
    return f"/api/v1/events/{event_id}/store{path}"


async def _item(client, shop, **body):
    r = await client.post(_store(shop["event"].id), json={"name": "Tee", "price": 450, **body}, headers=shop["h"])
    assert r.status_code == 201, r.text
    return r.json()


async def _register(client, event_id, email, picks=None, headers=None):
    payload = {"full_name": "Guest Buyer", "email": email, "phone": f"9{uuid.uuid4().int % 10**9:09d}"}
    if picks is not None:
        payload["store"] = picks
    return await client.post(f"/api/v1/events/registration/{event_id}/register", json=payload, headers=headers or {})


async def _order(client, event_id, reg):
    return await client.post(
        "/api/v1/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": event_id, "event_registration_id": reg["event_registration_id"]}},
        headers=auth_header(reg["auth_token"]["access_token"]),
    )


async def _capture(session, razorpay_order_id: str, amount):
    """What Razorpay's capture does to our tables, without Razorpay."""
    order = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == razorpay_order_id))
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{uuid.uuid4().hex[:10]}", status=PaymentStatus.captured, amount=Decimal(amount), payment_method="upi"))
    order.status = OrderStatus.paid
    await session.commit()
    await handle_event_registration_payment(session, order)
    return order


async def _counts(session, model, row_id):
    row = (await session.execute(select(model.stock, model.sold_count, model.reserved_count).where(model.id == uuid.UUID(str(row_id))))).one()
    return tuple(row)


async def _order_row(session, registration_id) -> EventStoreOrders:
    return await session.scalar(
        select(EventStoreOrders).where(EventStoreOrders.registration_id == uuid.UUID(registration_id))
        .order_by(EventStoreOrders.created_at.desc()).execution_options(populate_existing=True)
    )


async def test_two_guests_cannot_both_hold_the_last_piece(client, session, shop):
    """Stock was only checked against what had sold: two guests picking the
    last piece both got it, and both could pay for it."""
    kit = await _item(client, shop, name="Kit", stock=1)
    first = await _register(client, shop["event"].id, "first@example.com", [{"item_id": kit["id"], "qty": 1}])
    assert first.status_code == 200, first.text
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 0, 1)

    second = await _register(client, shop["event"].id, "second@example.com", [{"item_id": kit["id"], "qty": 1}])
    assert second.status_code == 422 and second.json()["error_code"] == "SOLD_OUT", second.text
    assert second.json()["errors"]["available"] == 0
    # Held pieces are not offered to the next guest either.
    assert (await client.get(_store(shop["event"].id, "/catalog"))).json()["items"] == []
    listed = next(i for i in (await client.get(_store(shop["event"].id), headers=shop["h"])).json() if i["id"] == kit["id"])
    assert (listed["sold"], listed["held"], listed["left"], listed["sold_out"]) == (0, 1, 0, True)


async def test_the_capture_turns_the_hold_into_a_sale(client, session, shop):
    tee = await _item(client, shop, variants=[{"label": "M", "stock": 2}])
    m_id = tee["variants"][0]["id"]
    reg = (await _register(client, shop["event"].id, "buyer@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 2}])).json()
    assert await _counts(session, EventStoreVariants, m_id) == (2, 0, 2)
    order = (await _order(client, shop["event"].id, reg)).json()
    await _capture(session, order["razorpay_order_id"], 1000)
    assert await _counts(session, EventStoreVariants, m_id) == (2, 2, 0)
    row = await _order_row(session, reg["event_registration_id"])
    assert row.status == "paid" and row.held_until is None


async def test_changing_the_picks_and_walking_away_give_the_pieces_back(client, session, shop):
    from app.api.events.registration.service import retire_if_abandoned

    kit = await _item(client, shop, name="Kit", stock=1)
    cap = await _item(client, shop, name="Cap", stock=1)
    reg = (await _register(client, shop["event"].id, "changer@example.com", [{"item_id": kit["id"], "qty": 1}])).json()
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 0, 1)
    # Back with other picks: the kit goes back on the shelf, the cap is held.
    again = await _register(client, shop["event"].id, "changer@example.com", [{"item_id": cap["id"], "qty": 1}], headers=auth_header(reg["auth_token"]["access_token"]))
    assert again.status_code == 200, again.text
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 0, 0)
    assert await _counts(session, EventStoreItems, cap["id"]) == (1, 0, 1)

    # Walked away from: the registration is retired, and the cap with it.
    row = await session.get(EventRegistrationsLink, uuid.UUID(reg["event_registration_id"]))
    row.created_at = NOW - timedelta(hours=2)
    await session.commit()
    assert await retire_if_abandoned(session, shop["event"], row) is True
    assert await _counts(session, EventStoreItems, cap["id"]) == (1, 0, 0)
    assert (await _order_row(session, reg["event_registration_id"])).status == "cancelled"


async def test_a_hold_runs_out_and_a_late_payment_is_oversold_for_the_organiser_to_settle(client, session, shop):
    from app.api.store import sweep as store_sweep

    assert store.HOLD_FOR == timedelta(minutes=30)
    from app.api.events.registration.service import ABANDONED_AFTER

    assert store.HOLD_FOR == ABANDONED_AFTER
    kit = await _item(client, shop, name="Kit", stock=1)
    late = (await _register(client, shop["event"].id, "late@example.com", [{"item_id": kit["id"], "qty": 1}])).json()
    late_order = (await _order(client, shop["event"].id, late)).json()

    # Half an hour on, the hold runs out and the sweep gives the kit back.
    row = await _order_row(session, late["event_registration_id"])
    row.held_until = NOW - timedelta(minutes=1)
    await session.commit()
    assert await store_sweep.run_sweep(session) == 1
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 0, 0)

    # Someone else takes it and pays.
    quick = (await _register(client, shop["event"].id, "quick@example.com", [{"item_id": kit["id"], "qty": 1}])).json()
    quick_order = (await _order(client, shop["event"].id, quick)).json()
    await _capture(session, quick_order["razorpay_order_id"], 550)
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 1, 0)

    # The first guest's payment lands after all: the ticket is theirs, the kit
    # is not there -- oversold, nothing refunded on its own, the organiser and
    # the admins told.
    await _capture(session, late_order["razorpay_order_id"], 550)
    assert await _counts(session, EventStoreItems, kit["id"]) == (1, 1, 0)
    reg_row = await session.get(EventRegistrationsLink, uuid.UUID(late["event_registration_id"]))
    await session.refresh(reg_row)
    assert reg_row.is_paid is True and reg_row.paid_amount == Decimal("100.00")
    over = await _order_row(session, late["event_registration_id"])
    assert over.status == "oversold"
    alerts = list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "store_oversold"))).all())
    assert len(alerts) == 1 and alerts[0].data["order_id"] == str(over.id)
    summary = (await client.get(_store(shop["event"].id, "/summary"), headers=shop["h"])).json()
    assert summary["oversold"] == 1
    listed = (await client.get(_store(shop["event"].id, "/orders"), params={"status": "oversold"}, headers=shop["h"])).json()
    assert listed["total"] == 1

    # Fulfilled once the organiser adds stock; refused while there is none.
    url = _store(shop["event"].id, f"/orders/{over.id}/fulfil")
    assert (await client.post(url, headers=shop["h"])).json()["error_code"] == "SOLD_OUT"
    assert (await client.patch(_store(shop["event"].id, f"/{kit['id']}"), json={"stock": 2}, headers=shop["h"])).status_code == 200
    done = await client.post(url, headers=shop["h"])
    assert done.status_code == 200 and done.json()["status"] == "paid", done.text
    assert await _counts(session, EventStoreItems, kit["id"]) == (2, 2, 0)


async def test_selling_does_not_lose_a_sale_another_worker_made(client, session, shop):
    """Sales were written read-modify-write from the session's copy of the
    row: a sale committed by another worker meanwhile was overwritten."""
    kit = await _item(client, shop, name="Kit", stock=5)
    a = (await _register(client, shop["event"].id, "a@example.com", [{"item_id": kit["id"], "qty": 1}])).json()
    order = (await _order(client, shop["event"].id, a)).json()
    # This session's copy of the item says nothing is sold...
    stale = await session.get(EventStoreItems, uuid.UUID(kit["id"]))
    assert stale.sold_count == 0
    # ...while another worker sells two (and holds nothing more).
    await session.execute(update(EventStoreItems.__table__).where(EventStoreItems.__table__.c.id == stale.id).values(sold_count=2))
    await _capture(session, order["razorpay_order_id"], 550)
    assert await _counts(session, EventStoreItems, kit["id"]) == (5, 3, 0)


async def test_more_sold_than_stocked_is_refused_by_the_database(client, session, shop):
    kit = await _item(client, shop, name="Kit", stock=1)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(update(EventStoreItems.__table__).where(EventStoreItems.__table__.c.id == uuid.UUID(kit["id"])).values(sold_count=2))
    # And the organiser cannot lower stock below what is sold or held.
    reg = await _register(client, shop["event"].id, "holder@example.com", [{"item_id": kit["id"], "qty": 1}])
    assert reg.status_code == 200
    r = await client.patch(_store(shop["event"].id, f"/{kit['id']}"), json={"stock": 0}, headers=shop["h"])
    assert r.status_code == 422 and "held" in r.json()["message"]


async def test_a_paid_order_is_handed_over_once_and_says_by_whom(client, session, shop):
    tee = await _item(client, shop, variants=[{"label": "M", "stock": 3}])
    m_id = tee["variants"][0]["id"]
    reg = (await _register(client, shop["event"].id, "door@example.com", [{"item_id": tee["id"], "variant_id": m_id, "qty": 1}])).json()
    order = (await _order(client, shop["event"].id, reg)).json()
    await _capture(session, order["razorpay_order_id"], 550)
    oid = str((await _order_row(session, reg["event_registration_id"])).id)

    url = _store(shop["event"].id, f"/orders/{oid}/handover")
    first = await client.post(url, headers=shop["h"])
    assert first.status_code == 200 and first.json()["handed_over_by"] == shop["owner"].id
    # A second door scanning the same order: refused, with who and when.
    second = await client.post(url, headers=shop["h"])
    assert second.status_code == 409 and second.json()["error_code"] == "ALREADY_HANDED_OVER", second.text
    errors = second.json()["errors"]
    assert errors["handed_over_by"] == shop["owner"].id and errors["handed_over_at"] == first.json()["handed_over_at"]
    assert errors["handed_over_by_name"]
    gate = await client.post(f"/api/v1/events/{shop['event'].id}/gate/store/{oid}/handover", headers=shop["h"])
    assert gate.status_code == 409

    # Refunded after it left with the guest: the piece does not go back on the shelf.
    log = await session.scalar(select(PaymentLogs).join(PaymentOrders).where(PaymentOrders.razorpay_order_id == order["razorpay_order_id"]))
    await session.refresh(log, ["order"])
    await payments._apply_refund(session, log)
    assert (await _order_row(session, reg["event_registration_id"])).status == "refunded"
    assert await _counts(session, EventStoreVariants, m_id) == (3, 1, 0)
