"""Ticket types and discount codes (app/api/ticketing, docs/TICKETING.md):
the migration's default type, registering with and without a type, per-type
places and the event's total, sales windows, hidden types, codes of every
kind and their limits, what is charged (worked out by the server alone), and
the plan's gates. Razorpay is a fake throughout."""
import importlib.util
import itertools
import pathlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink, Events
from app.api.payments import service as payments
from app.api.payments.models import PaymentLogs
from app.api.subscriptions import gates
from app.api.subscriptions import service as subscriptions
from app.api.subscriptions.models import BillingCycle, PlanFeatures, PlanLimits, Plans
from app.api.ticketing.models import DiscountCodes, TicketTypes
from app.config import settings
from tests.conftest import auth_header
from tests.test_money_integrity import FakeRazorpay, rzp_fake  # noqa: F401

API = "/api/v1"
NOW = datetime.now(timezone.utc)
_n = itertools.count(1)


def _migration(name="ticketing"):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def shop(session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Ticket Club", email="tickets-owner@example.com")
    free = await make_event(club=club, created_by=owner, name="Free Night", event_datetime=NOW + timedelta(days=5))
    paid = await make_event(
        club=club, created_by=owner, name="Paid Night", event_datetime=NOW + timedelta(days=5),
        has_fee=True, reg_fee=Decimal("250.00"),
    )
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, free=free, paid=paid, h=auth_header(await mint_token(owner)))


def register(client, event, n=None, **extra):
    n = n if n is not None else next(_n)
    body = {"full_name": f"Guest Number {n}", "email": f"guest{n}@example.com", "phone": "98765" + f"{n:05d}"[-5:]}
    body.update(extra)
    return client.post(f"{API}/events/registration/{event.id}/register", json=body)


async def set_types(client, shop, event, types, **extra):
    r = await client.put(f"{API}/events/{event.id}/ticketing/types", json={"types": types, **extra}, headers=shop.h)
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["types"]}


async def make_code(client, shop, **body):
    r = await client.post(f"{API}/clubs/discount-codes", json=body, headers=shop.h)
    assert r.status_code == 201, r.text
    return r.json()


async def _reg(session, reg_id) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == reg_id).execution_options(populate_existing=True)
    )


async def _taken(session, type_id) -> int:
    return await session.scalar(
        select(TicketTypes.taken_count).where(TicketTypes.id == type_id).execution_options(populate_existing=True)
    )


# ─── The migration: one default type per event ──────────────────────────


async def test_the_migration_gives_every_event_one_default_type_at_its_own_price_and_cap(
    session, make_club, make_event, make_user, make_registration,
):
    owner, club = await make_club(name="Old Club", email="old-owner@example.com")
    free = await make_event(club=club, created_by=owner, name="Old Free", max_participants=50)
    paid = await make_event(club=club, created_by=owner, name="Old Paid", has_fee=True, reg_fee=Decimal("199.50"), max_participants=10)
    stale = await make_event(club=club, created_by=owner, name="Old Stale", has_fee=False, reg_fee=Decimal("80.00"), max_participants=0)
    for i in range(3):
        await make_registration(event=free, user=await make_user(email=f"oldfree{i}@example.com"))
    for i, (is_paid, deleted) in enumerate(((True, False), (True, False), (False, False), (True, True))):
        await make_registration(
            event=paid, user=await make_user(email=f"oldpaid{i}@example.com"), is_paid=is_paid,
            paid_amount=Decimal("199.50") if is_paid else 0, actual_amount=Decimal("199.50"), is_deleted=deleted,
        )
    await session.commit()
    module = _migration()
    connection = await session.connection()

    def run(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            sync.exec_driver_sql(module.DEFAULT_TYPES)
            module._adopt_registrations()
            sync.exec_driver_sql(module.DEFAULT_TYPES)  # again: nothing more

    await connection.run_sync(run)
    rows = {
        t.event_id: t for t in (await session.scalars(
            select(TicketTypes).where(TicketTypes.event_id.in_([free.id, paid.id, stale.id])).execution_options(populate_existing=True)
        )).all()
    }
    assert len(rows) == 3 and all(t.is_default and t.name == "General admission" for t in rows.values())
    assert (rows[free.id].price, rows[free.id].capacity, rows[free.id].taken_count) == (Decimal("0.00"), 50, 3)
    # The paid event: its fee and cap; its two live paid registrations hold places.
    assert (rows[paid.id].price, rows[paid.id].capacity, rows[paid.id].taken_count) == (Decimal("199.50"), 10, 2)
    # Free with a stale fee is free; a cap of 0 was never a cap.
    assert (rows[stale.id].price, rows[stale.id].capacity) == (Decimal("0.00"), None)
    untyped = await session.scalar(
        select(EventRegistrationsLink.id).where(
            EventRegistrationsLink.event_id.in_([free.id, paid.id]), EventRegistrationsLink.ticket_type_id.is_(None),
        ).execution_options(include_deleted=True).limit(1)
    )
    assert untyped is None


# ─── Registering with and without a type ────────────────────────────────


async def test_an_event_with_one_type_registers_as_it_always_did(client, session, shop):
    r = await register(client, shop.free)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket_type"]["name"] == "General admission"
    assert (body["pay_amount"], body["price"], body["discount"]) == (0, 0.0, None)
    info = (await client.get(f"{API}/events/info/{shop.free.id}")).json()
    tickets = info["ticketing"]
    assert tickets["requires_choice"] is False and len(tickets["types"]) == 1
    assert tickets["types"][0]["status"] == "on_sale" and tickets["types"][0]["is_free"] is True


async def test_a_single_type_follows_the_events_own_price(client, session, shop):
    first = (await register(client, shop.paid)).json()
    assert (first["pay_amount"], first["price"], first["is_paid"]) == (250.0, 250.0, False)
    # The fee changed by the old event form (or any client): the next one pays that.
    shop.paid.reg_fee = Decimal("300.00")
    await session.commit()
    second = (await register(client, shop.paid)).json()
    assert second["pay_amount"] == 300.0
    listed = (await client.get(f"{API}/events/{shop.paid.id}/ticketing/types", headers=shop.h)).json()
    assert listed["follows_event"] is True and listed["types"][0]["price"] == 300.0


async def test_an_event_selling_several_types_needs_one_named(client, session, shop):
    types = await set_types(client, shop, shop.paid, [
        {"name": "General", "price": 250, "capacity": 100},
        {"name": "Student", "price": 99, "capacity": 20, "description": "Bring your college ID"},
        {"name": "Sponsor", "price": 0, "is_hidden": True},
    ])
    r = await register(client, shop.paid)
    assert r.status_code == 422 and r.json()["error_code"] == "TICKET_TYPE_REQUIRED"
    assert set(r.json()["errors"]["ticket_types"]) == {types["General"]["id"], types["Student"]["id"]}
    r = await register(client, shop.paid, ticket_type_id=999999)
    assert r.status_code == 422 and r.json()["error_code"] == "TICKET_TYPE_NOT_FOUND"

    student = (await register(client, shop.paid, ticket_type_id=types["Student"]["id"])).json()
    assert student["ticket_type"] == {"id": types["Student"]["id"], "name": "Student"}
    assert student["pay_amount"] == 99.0 and student["is_paid"] is False
    reg = await _reg(session, student["event_registration_id"])
    assert reg.ticket_label == "Student" and reg.actual_amount == Decimal("99.00")

    # The page lists what is on sale; the hidden type only to a link that names it.
    page = (await client.get(f"{API}/events/{shop.paid.slug}/ticket-types")).json()
    assert [t["name"] for t in page["types"]] == ["General", "Student"] and page["requires_choice"] is True
    linked = (await client.get(f"{API}/events/{shop.paid.id}/ticket-types", params={"ticket_type": types["Sponsor"]["id"]})).json()
    assert "Sponsor" in [t["name"] for t in linked["types"]]
    # A free type on a paid event: confirmed at once, as paid at nothing.
    sponsor = (await register(client, shop.paid, ticket_type_id=types["Sponsor"]["id"])).json()
    assert (sponsor["pay_amount"], sponsor["is_paid"]) == (0, True)
    # The event's own fee is what its types charge: paid, from the lowest price.
    event = await session.get(Events, shop.paid.id, populate_existing=True)
    assert event.has_fee is True and event.reg_fee == Decimal("99.00")


async def test_a_type_sells_only_in_its_window(client, session, shop):
    types = await set_types(client, shop, shop.free, [
        {"name": "Early", "sales_end": (NOW - timedelta(hours=1)).isoformat()},
        {"name": "Late", "sales_start": (NOW + timedelta(days=1)).isoformat()},
        {"name": "Now"},
    ])
    early = await register(client, shop.free, ticket_type_id=types["Early"]["id"])
    assert early.status_code == 409 and early.json()["errors"]["closed_reason"] == "ended"
    late = await register(client, shop.free, ticket_type_id=types["Late"]["id"])
    assert late.status_code == 409 and late.json()["error_code"] == "TICKET_TYPE_NOT_ON_SALE"
    assert late.json()["errors"]["closed_reason"] == "not_started"
    assert (await register(client, shop.free, ticket_type_id=types["Now"]["id"])).status_code == 200
    page = (await client.get(f"{API}/events/{shop.free.id}/ticket-types")).json()
    assert {t["name"]: t["status"] for t in page["types"]} == {"Early": "ended", "Late": "not_started", "Now": "on_sale"}
    # Saving a window that ends before it starts is refused.
    bad = await client.put(f"{API}/events/{shop.free.id}/ticketing/types", json={"types": [
        {"name": "Odd", "sales_start": NOW.isoformat(), "sales_end": (NOW - timedelta(days=1)).isoformat()},
    ]}, headers=shop.h)
    assert bad.status_code == 422


async def test_each_type_has_its_own_places_and_the_event_its_total(client, session, shop):
    types = await set_types(client, shop, shop.free, [
        {"name": "Hall", "capacity": 2}, {"name": "Balcony", "capacity": 5},
    ], event_capacity=3)
    hall, balcony = types["Hall"]["id"], types["Balcony"]["id"]
    for _ in range(2):
        assert (await register(client, shop.free, ticket_type_id=hall)).status_code == 200
    third = await register(client, shop.free, ticket_type_id=hall)
    assert third.status_code == 409 and third.json()["error_code"] == "TICKET_TYPE_SOLD_OUT"
    assert third.json()["errors"] == {"ticket_type_id": hall, "waitlist": False}
    assert (await register(client, shop.free, ticket_type_id=balcony)).status_code == 200
    # Balcony has room of its own, but the event's total is reached.
    full = await register(client, shop.free, ticket_type_id=balcony)
    assert full.status_code == 400 and full.json()["error_code"] == "EVENT_FULL"
    assert await _taken(session, hall) == 2 and await _taken(session, balcony) == 1
    page = (await client.get(f"{API}/events/{shop.free.id}/ticket-types")).json()
    assert page["available"] == 0 and page["sold_out"] is True
    assert {t["name"]: (t["available"], t["status"]) for t in page["types"]} == {"Hall": (0, "sold_out"), "Balcony": (0, "sold_out")}


async def test_a_person_holds_as_many_of_a_type_as_it_allows(client, session, shop):
    types = await set_types(client, shop, shop.free, [{"name": "One each", "per_person_limit": 1}, {"name": "Open"}])
    one = types["One each"]["id"]
    assert (await register(client, shop.free, 501, ticket_type_id=one, phone="9000000501")).status_code == 200
    # Another address, the same phone: the same person.
    again = await register(client, shop.free, 502, ticket_type_id=one, phone="+91 90000 00501")
    assert again.status_code == 409 and again.json()["error_code"] == "TICKET_TYPE_LIMIT"


async def test_a_type_people_hold_is_hidden_not_removed(client, session, shop):
    types = await set_types(client, shop, shop.free, [{"name": "A"}, {"name": "B"}])
    assert (await register(client, shop.free, ticket_type_id=types["A"]["id"])).status_code == 200
    r = await client.put(f"{API}/events/{shop.free.id}/ticketing/types", json={"types": [{"id": types["B"]["id"], "name": "B"}]}, headers=shop.h)
    assert r.status_code == 409 and r.json()["error_code"] == "TICKET_TYPE_HAS_REGISTRATIONS"
    r = await client.delete(f"{API}/events/{shop.free.id}/ticketing/types/{types['A']['id']}", headers=shop.h)
    assert r.status_code == 409
    assert (await client.delete(f"{API}/events/{shop.free.id}/ticketing/types/{types['B']['id']}", headers=shop.h)).status_code == 200
    listed = (await client.get(f"{API}/events/{shop.free.id}/ticketing/types", headers=shop.h)).json()
    assert [t["name"] for t in listed["types"]] == ["A"] and listed["types"][0]["has_registrations"] is True
    last = await client.delete(f"{API}/events/{shop.free.id}/ticketing/types/{types['A']['id']}", headers=shop.h)
    assert last.status_code == 409 and last.json()["error_code"] == "LAST_TICKET_TYPE"


async def test_the_organiser_adding_a_guest_gets_the_default_type(client, session, shop):
    await set_types(client, shop, shop.free, [{"name": "Main"}, {"name": "VIP", "is_hidden": True}])
    r = await client.post(
        f"{API}/events/registration/{shop.free.id}/attendees",
        json={"full_name": "Desk Guest", "email": "desk-guest@example.com"}, headers=shop.h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ticket_label"] == "Main"


async def test_a_copied_event_sells_the_same_types(client, session, shop):
    await set_types(client, shop, shop.paid, [{"name": "General", "price": 250}, {"name": "Student", "price": 99, "capacity": 20}])
    r = await client.post(
        f"{API}/events/{shop.paid.id}/duplicate", json={"name": "Paid Night II", "event_datetime": (NOW + timedelta(days=30)).isoformat()},
        headers=shop.h,
    )
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["copied"]["ticket_types"] == 2
    listed = (await client.get(f"{API}/events/{copy['id']}/ticketing/types", headers=shop.h)).json()
    assert [(t["name"], t["price"], t["sold"]) for t in listed["types"]] == [("General", 250.0, 0), ("Student", 99.0, 0)]


# ─── Tickets show their type ────────────────────────────────────────────


async def test_the_ticket_the_gate_and_the_export_show_the_type(client, session, shop):
    from app.api.checkins.service import summary
    from app.api.events.documents import layout_values
    from app.api.events.wallet_service import generate_pass_json

    types = await set_types(client, shop, shop.free, [{"name": "Main"}, {"name": "Backstage"}])
    body = (await register(client, shop.free, ticket_type_id=types["Backstage"]["id"])).json()
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == body["event_registration_id"])
        .execution_options(populate_existing=True)
    )
    event = await session.get(Events, shop.free.id)
    await session.refresh(event, attribute_names=["club"])
    assert layout_values(reg, event, "ticket")["ticket_type"] == "Backstage"
    reg.event = event
    assert summary(reg)["ticket_type"] == "Backstage"
    fields = generate_pass_json("MOA-1", "Free Night", NOW, None, "Guest", ticket_type=reg.ticket_label)["eventTicket"]["auxiliaryFields"]
    assert {"key": "ticketType", "label": "TICKET", "value": "Backstage"} in fields
    listed = (await client.get(f"{API}/events/registration/{shop.free.id}/list", headers=shop.h)).json()
    assert listed["items"][0]["ticket_type_name"] == "Backstage"


async def test_a_single_type_events_ticket_still_says_the_role(client, session, shop):
    from app.api.events.documents import layout_values

    body = (await register(client, shop.free)).json()
    reg = await _reg(session, body["event_registration_id"])
    event = await session.get(Events, shop.free.id)
    await session.refresh(event, attribute_names=["club"])
    assert reg.ticket_label is None and layout_values(reg, event, "ticket")["ticket_type"] == "Attendee"


# ─── Discount codes ─────────────────────────────────────────────────────


async def test_a_percent_code_and_an_amount_code_and_the_check(client, session, shop):
    await make_code(client, shop, code="early20", kind="percent", value=20, event_id=shop.paid.id)
    await make_code(client, shop, code="FLAT100", kind="amount", value=100)
    check = await client.post(f"{API}/events/{shop.paid.id}/discount-codes/check", json={"code": " Early20 "})
    assert check.status_code == 200, check.text
    # Only what it does to the price: nothing about its limits or uses.
    assert check.json() == {"code": "EARLY20", "ticket_type_id": check.json()["ticket_type_id"], "price": 250.0, "discount": 50.0, "amount": 200.0, "free": False}
    pct = (await register(client, shop.paid, discount_code="early20")).json()
    assert (pct["price"], pct["discount"], pct["pay_amount"]) == (250.0, {"code": "EARLY20", "amount": 50.0}, 200.0)
    flat = (await register(client, shop.paid, discount_code="flat100")).json()
    assert flat["pay_amount"] == 150.0
    bad = await client.post(f"{API}/events/{shop.paid.id}/discount-codes/check", json={"code": "NOPE"})
    assert bad.status_code == 422 and bad.json()["error_code"] == "DISCOUNT_CODE_INVALID"
    assert "discount_code" in bad.json()["errors"]


async def test_a_code_that_takes_it_all_needs_no_payment(client, session, shop, monkeypatch):
    from app.api.deliveries import service as deliveries

    sent = []

    async def ticket(session_, event, registration, background_tasks=None):
        sent.append(registration.id)

    monkeypatch.setattr(deliveries, "send_ticket", ticket)
    await make_code(client, shop, code="GUESTLIST", kind="percent", value=100, event_id=shop.paid.id)
    body = (await register(client, shop.paid, discount_code="guestlist")).json()
    assert (body["pay_amount"], body["is_paid"], body["discount"]["amount"]) == (0, True, 250.0)
    reg = await _reg(session, body["event_registration_id"])
    assert reg.is_paid is True and reg.actual_amount == 0 and reg.paid_amount == 0
    assert sent == [reg.id]
    event = await session.get(Events, shop.paid.id, populate_existing=True)
    # A paid place, as a payment would have taken.
    assert event.paid_count == 1
    order = await client.post(
        f"{API}/payments/orders", json={"source": "event_registration", "payload": {"event_id": shop.paid.id, "event_registration_id": body["event_registration_id"]}},
        headers=auth_header(body["auth_token"]["access_token"]),
    )
    assert order.status_code == 409 and order.json()["error_code"] == "ALREADY_PAID"


async def test_a_codes_limits_its_window_and_its_types(client, session, shop):
    types = await set_types(client, shop, shop.paid, [{"name": "General", "price": 250}, {"name": "VIP", "price": 900}])
    general, vip = types["General"]["id"], types["VIP"]["id"]
    await make_code(client, shop, code="VIPONLY", kind="amount", value=100, event_id=shop.paid.id, ticket_type_ids=[vip])
    await make_code(client, shop, code="GONE", kind="percent", value=10, ends_at=(NOW - timedelta(minutes=1)).isoformat())
    await make_code(client, shop, code="SOON", kind="percent", value=10, starts_at=(NOW + timedelta(days=1)).isoformat())
    await make_code(client, shop, code="OFF", kind="percent", value=10, is_active=False)

    def errs(r):
        return r.status_code, r.json()["error_code"]

    assert errs(await register(client, shop.paid, ticket_type_id=general, discount_code="VIPONLY")) == (409, "DISCOUNT_CODE_NOT_FOR_TYPE")
    assert (await register(client, shop.paid, ticket_type_id=vip, discount_code="VIPONLY")).json()["pay_amount"] == 800.0
    assert errs(await register(client, shop.paid, ticket_type_id=general, discount_code="GONE")) == (409, "DISCOUNT_CODE_EXPIRED")
    assert errs(await register(client, shop.paid, ticket_type_id=general, discount_code="SOON")) == (409, "DISCOUNT_CODE_NOT_STARTED")
    assert errs(await register(client, shop.paid, ticket_type_id=general, discount_code="OFF")) == (422, "DISCOUNT_CODE_INVALID")
    # A type restriction is for one event's code only.
    r = await client.post(f"{API}/clubs/discount-codes", json={"code": "WIDE", "kind": "percent", "value": 5, "ticket_type_ids": [vip]}, headers=shop.h)
    assert r.status_code == 422
    # The same text twice is refused.
    r = await client.post(f"{API}/clubs/discount-codes", json={"code": "viponly", "kind": "percent", "value": 5}, headers=shop.h)
    assert r.status_code == 409 and r.json()["error_code"] == "DISCOUNT_CODE_TAKEN"


async def test_uses_count_paid_and_confirmed_and_come_back_on_refund_or_cancel(client, session, shop, rzp_fake):
    code = await make_code(client, shop, code="ONCE", kind="percent", value=100, max_uses=1, event_id=shop.free.id)
    paid_code = await make_code(client, shop, code="HALF", kind="percent", value=50, max_uses=1, per_person_limit=1, event_id=shop.paid.id)
    # The free ticket with the code is confirmed at once: the one use is taken.
    first = (await register(client, shop.free, discount_code="ONCE")).json()
    used = await client.post(f"{API}/events/{shop.free.id}/discount-codes/check", json={"code": "ONCE"})
    assert used.status_code == 409 and used.json()["error_code"] == "DISCOUNT_CODE_USED_UP"
    listed = {c["code"]: c for c in (await client.get(f"{API}/clubs/discount-codes", headers=shop.h)).json()}
    assert (listed["ONCE"]["uses"], listed["ONCE"]["status"]) == (1, "used_up")
    # Cancelled: the use comes back.
    r = await client.delete(
        f"{API}/events/registration/{shop.free.id}/info/{first['event_registration_id']}",
        params={"confirm_slug": shop.free.slug}, headers=shop.h,
    )
    assert r.status_code == 200
    assert (await register(client, shop.free, discount_code="ONCE")).status_code == 200

    # The paid one: paid for, it counts; refunded, it does not.
    body = (await register(client, shop.paid, discount_code="HALF")).json()
    assert body["pay_amount"] == 125.0
    headers = auth_header(body["auth_token"]["access_token"])
    order = (await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": shop.paid.id, "event_registration_id": body["event_registration_id"]}},
        headers=headers,
    )).json()
    # The server's amount, in paise: the type's price less the code.
    assert rzp_fake.orders[order["razorpay_order_id"]] == 12500
    payment = rzp_fake.pay(order["razorpay_order_id"])
    await payments.verify_razorpay_payment(session, order["razorpay_order_id"], payment["id"], payment_details=payment, expand_payment_details=False)
    usage = {c["code"]: c for c in (await client.get(f"{API}/clubs/discount-codes", headers=shop.h)).json()}["HALF"]
    assert (usage["uses"], usage["discount_total"]) == (1, 125.0)
    again = await register(client, shop.paid, discount_code="HALF")
    assert again.status_code == 409 and again.json()["error_code"] in ("DISCOUNT_CODE_USED_UP", "DISCOUNT_CODE_LIMIT")
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == payment["id"]))
    await payments.refund_payment(session, log.id)
    usage = {c["code"]: c for c in (await client.get(f"{API}/clubs/discount-codes", headers=shop.h)).json()}["HALF"]
    assert usage["uses"] == 0
    assert (await register(client, shop.paid, discount_code="HALF")).status_code == 200
    assert code["id"] and paid_code["id"]


async def test_a_code_per_person(client, session, shop):
    await make_code(client, shop, code="ONEEACH", kind="percent", value=100, per_person_limit=1)
    assert (await register(client, shop.free, 701, discount_code="ONEEACH")).status_code == 200
    second_event = await register(client, shop.paid, 701, discount_code="ONEEACH")
    assert second_event.status_code == 409 and second_event.json()["error_code"] == "DISCOUNT_CODE_LIMIT"
    assert (await register(client, shop.paid, 702, discount_code="ONEEACH")).status_code == 200


async def test_back_to_pay_with_a_code_is_priced_again(client, session, shop, make_user, mint_token):
    user = await make_user(email="returning@example.com")
    await session.commit()
    h = auth_header(await mint_token(user))
    body = {"full_name": "Returning Guest", "email": "returning@example.com"}
    first = (await client.post(f"{API}/events/registration/{shop.paid.id}/register", json=body, headers=h)).json()
    assert first["pay_amount"] == 250.0
    await make_code(client, shop, code="COMEBACK", kind="amount", value=50)
    again = (await client.post(f"{API}/events/registration/{shop.paid.id}/register", json={**body, "discount_code": "COMEBACK"}, headers=h)).json()
    assert again["event_registration_id"] == first["event_registration_id"]
    assert (again["pay_amount"], again["discount"]["code"]) == (200.0, "COMEBACK")
    # Coming back without one keeps what it has (the app sends nothing new).
    third = (await client.post(f"{API}/events/registration/{shop.paid.id}/register", json=body, headers=h)).json()
    assert third["pay_amount"] == 200.0


# ─── The amount is the server's ─────────────────────────────────────────


async def test_the_amount_charged_is_worked_out_by_the_server(client, session, shop, rzp_fake):
    types = await set_types(client, shop, shop.paid, [{"name": "General", "price": 250}, {"name": "VIP", "price": 999.99}])
    await make_code(client, shop, code="TENOFF", kind="percent", value=10, event_id=shop.paid.id)
    # Whatever a client adds about money is not read.
    body = (await register(
        client, shop.paid, ticket_type_id=types["VIP"]["id"], discount_code="tenoff", price=1, amount=1, actual_amount=1, pay_amount=1,
    )).json()
    assert body["pay_amount"] == 899.99
    order = (await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": shop.paid.id, "event_registration_id": body["event_registration_id"], "amount": 1}},
        headers=auth_header(body["auth_token"]["access_token"]),
    )).json()
    assert rzp_fake.orders[order["razorpay_order_id"]] == 89999
    # Short by a paisa: the ticket is not paid.
    payment = rzp_fake.pay(order["razorpay_order_id"], paise=89998)
    await payments.verify_razorpay_payment(session, order["razorpay_order_id"], payment["id"], payment_details=payment, expand_payment_details=False)
    assert (await _reg(session, body["event_registration_id"])).is_paid is False


async def test_a_paid_types_last_place_goes_to_the_first_payment(client, session, shop, rzp_fake):
    types = await set_types(client, shop, shop.paid, [{"name": "General", "price": 250}, {"name": "Last one", "price": 500, "capacity": 1}])
    last = types["Last one"]["id"]
    a = (await register(client, shop.paid, ticket_type_id=last)).json()
    b = (await register(client, shop.paid, ticket_type_id=last)).json()

    async def pay(body):
        order = (await client.post(
            f"{API}/payments/orders",
            json={"source": "event_registration", "payload": {"event_id": shop.paid.id, "event_registration_id": body["event_registration_id"]}},
            headers=auth_header(body["auth_token"]["access_token"]),
        ))
        return order

    order_a, order_b = (await pay(a)).json(), (await pay(b)).json()
    pa, pb = rzp_fake.pay(order_a["razorpay_order_id"]), rzp_fake.pay(order_b["razorpay_order_id"])
    await payments.verify_razorpay_payment(session, order_a["razorpay_order_id"], pa["id"], payment_details=pa, expand_payment_details=False)
    await payments.verify_razorpay_payment(session, order_b["razorpay_order_id"], pb["id"], payment_details=pb, expand_payment_details=False)
    assert (await _reg(session, a["event_registration_id"])).is_paid is True
    assert (await _reg(session, b["event_registration_id"])).is_paid is False
    # The second payment went back in full, on its own.
    assert pb["id"] in rzp_fake.refunded() and pa["id"] not in rzp_fake.refunded()
    assert await _taken(session, last) == 1
    # And a new checkout for it is refused before any money moves.
    c = (await register(client, shop.paid, ticket_type_id=last))
    assert c.status_code == 409 and c.json()["error_code"] == "TICKET_TYPE_SOLD_OUT"


async def test_per_type_sales_on_the_overview(client, session, shop, rzp_fake):
    types = await set_types(client, shop, shop.paid, [{"name": "General", "price": 250, "capacity": 10}, {"name": "Free pass", "price": 0}])
    await make_code(client, shop, code="FIFTY", kind="amount", value=50, event_id=shop.paid.id)
    body = (await register(client, shop.paid, ticket_type_id=types["General"]["id"], discount_code="FIFTY")).json()
    order = (await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": shop.paid.id, "event_registration_id": body["event_registration_id"]}},
        headers=auth_header(body["auth_token"]["access_token"]),
    )).json()
    p = rzp_fake.pay(order["razorpay_order_id"])
    await payments.verify_razorpay_payment(session, order["razorpay_order_id"], p["id"], payment_details=p, expand_payment_details=False)
    await register(client, shop.paid, ticket_type_id=types["General"]["id"])
    await register(client, shop.paid, ticket_type_id=types["Free pass"]["id"])
    sales = (await client.get(f"{API}/events/{shop.paid.id}/ticketing/sales", headers=shop.h)).json()
    by = {t["name"]: t for t in sales["types"]}
    assert (by["General"]["sold"], by["General"]["paid"], by["General"]["revenue"], by["General"]["pending"], by["General"]["available"]) == (1, 1, 200.0, 1, 9)
    assert (by["Free pass"]["sold"], by["Free pass"]["revenue"]) == (1, 0.0)
    assert sales["totals"]["revenue"] == 200.0 and sales["totals"]["sold"] == 2
    assert sales["codes"] == [{"id": sales["codes"][0]["id"], "code": "FIFTY", "uses": 1, "discount_total": 50.0}]


# ─── The plan's gates ───────────────────────────────────────────────────


@pytest_asyncio.fixture
async def on_free(session, shop, monkeypatch):
    """Enforcement on, the club on a Free plan: one type, no codes, no waiting list."""
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", datetime(2020, 1, 1, tzinfo=timezone.utc))
    gates.reset()
    free = Plans(key="free", name="Free", price_monthly_paise=0, price_yearly_paise=0, trial_days=0, visibility="public")
    pro = Plans(key="pro", name="Pro", price_monthly_paise=149_900, price_yearly_paise=1_499_000, trial_days=0, visibility="public", sort_order=3)
    session.add_all([free, pro])
    await session.flush()
    session.add_all([
        PlanLimits(plan_id=free.id, ticket_types_per_event=1, coupons_per_event=0),
        PlanFeatures(plan_id=free.id, tickets=True),
        PlanLimits(plan_id=pro.id, ticket_types_per_event=10, coupons_per_event=1),
        PlanFeatures(plan_id=pro.id, tickets=True, coupons=True, waitlist=True),
    ])
    await session.commit()
    yield SimpleNamespace(free=free, pro=pro)
    gates.reset()


async def test_the_plan_gates_types_codes_and_the_waiting_list(client, session, shop, on_free):
    r = await client.post(f"{API}/events/{shop.free.id}/ticketing/types", json={"name": "Second"}, headers=shop.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_LIMIT_REACHED"
    assert r.json()["errors"]["limit"] == "ticket_types_per_event" and r.json()["errors"]["required_plan"] == "pro"
    r = await client.post(f"{API}/clubs/discount-codes", json={"code": "NOPE", "kind": "percent", "value": 10}, headers=shop.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED" and r.json()["errors"]["feature"] == "coupons"
    r = await client.put(f"{API}/events/{shop.free.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=shop.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "waitlist"

    await subscriptions.assign_plan(session, club_id=shop.club.id, plan=on_free.pro, cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert (await client.post(f"{API}/events/{shop.free.id}/ticketing/types", json={"name": "Second"}, headers=shop.h)).status_code == 201
    assert (await client.post(f"{API}/clubs/discount-codes", json={"code": "PROCODE", "kind": "percent", "value": 10}, headers=shop.h)).status_code == 201
    # One code usable at an event on this plan.
    r = await client.post(f"{API}/clubs/discount-codes", json={"code": "TWO", "kind": "percent", "value": 10, "event_id": shop.free.id}, headers=shop.h)
    assert r.status_code == 403 and r.json()["errors"]["limit"] == "coupons_per_event"

    # Back on Free: the code stays, but is not taken at registration.
    sub = await subscriptions.assign_plan(session, club_id=shop.club.id, plan=on_free.free, cycle=BillingCycle.monthly)
    assert sub is not None
    await session.commit()
    gates.reset()
    used = await register(client, shop.paid, discount_code="PROCODE")
    assert used.status_code == 422 and used.json()["error_code"] == "DISCOUNT_CODE_INVALID"
    codes = (await client.get(f"{API}/clubs/discount-codes", headers=shop.h)).json()
    assert [c["code"] for c in codes] == ["PROCODE"]


async def test_a_paid_type_on_a_free_event_needs_paid_tickets(client, session, shop, on_free):
    from sqlalchemy import update

    await session.execute(update(PlanFeatures).where(PlanFeatures.plan_id == on_free.free.id).values(tickets=False))
    await session.commit()
    gates.reset()
    r = await client.patch(
        f"{API}/events/{shop.free.id}/ticketing/types/{(await client.get(f'{API}/events/{shop.free.id}/ticketing/types', headers=shop.h)).json()['types'][0]['id']}",
        json={"price": 100}, headers=shop.h,
    )
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "tickets"


async def test_codes_are_never_another_clubs(client, session, shop, make_club, make_event, mint_token):
    other_owner, other_club = await make_club(name="Other Club", email="other-owner@example.com")
    other_event = await make_event(club=other_club, created_by=other_owner, name="Other Night", event_datetime=NOW + timedelta(days=4))
    await session.commit()
    await make_code(client, shop, code="MINE", kind="percent", value=50)
    r = await client.post(f"{API}/events/{other_event.id}/discount-codes/check", json={"code": "MINE"})
    assert r.status_code == 422 and r.json()["error_code"] == "DISCOUNT_CODE_INVALID"
    other_h = auth_header(await mint_token(other_owner))
    mine = (await client.get(f"{API}/clubs/discount-codes", headers=shop.h)).json()[0]
    assert (await client.patch(f"{API}/clubs/discount-codes/{mine['id']}", json={"value": 90}, headers=other_h)).status_code == 404
    assert (await client.get(f"{API}/events/{shop.free.id}/ticketing/types", headers=other_h)).status_code in (403, 404)
    rows = (await session.scalars(select(DiscountCodes).where(DiscountCodes.club_id == shop.club.id))).all()
    assert [r.code for r in rows] == ["MINE"]
