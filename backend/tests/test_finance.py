"""Finance: what the club's events brought and cost, by month, by type and by event; one event's own ledger; the expense book; the statements."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories
from app.api.finance import service as finance
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """The service's clock is the one these tests build their dates from.

    NOW is read when the module is imported, and a full run reaches these
    tests many minutes later: a run started before midnight UTC (05:30 in
    India) reached them after it, when the service's today -- the default
    date of an expense, the end of the default window -- was a day (or, at
    the end of a month, a month) past the one asserted."""
    monkeypatch.setattr(finance, "_now", lambda: NOW)


async def _paid(make_registration, event, user, fee, *, is_paid=True):
    reg = await make_registration(event=event, user=user, is_paid=is_paid, paid_amount=fee if is_paid else 0, actual_amount=fee)
    reg.payment_receipt = f"er_{reg.ticket_id}"
    return reg


async def _refund(session, reg, amount):
    order = PaymentOrders(receipt=reg.payment_receipt, razorpay_order_id=f"order_{reg.ticket_id}", amount=amount, status=OrderStatus.refunded, source="event_registration", payload={})
    session.add(order)
    await session.flush()
    session.add(PaymentLogs(order_id=order.id, razorpay_payment_id=f"pay_{reg.ticket_id}", status=PaymentStatus.refunded, amount=amount, payment_method="upi"))
    reg.is_paid, reg.paid_amount = False, Decimal(0)
    await session.flush()


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="finance@example.com")
    rival_owner, rival = await make_club(name="Rivals", email="rivals-finance@example.com")
    hack = EventCategories(name="Hackathon", created_by_id=owner.id)
    session.add(hack)
    await session.flush()
    # A paid hackathon ten days ago: two paid guests, one refunded, one who never paid.
    past = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW - timedelta(days=10), has_fee=True, reg_fee=500, lifecycle=EventStatus.closed, category_id=hack.id)
    guests = [await make_user(email=f"guest{i}-finance@example.com") for i in range(7)]
    await _paid(make_registration, past, guests[0], 500)
    await _paid(make_registration, past, guests[1], 500)
    refunded = await _paid(make_registration, past, guests[2], 500)
    await _refund(session, refunded, 500)
    await _paid(make_registration, past, guests[3], 500, is_paid=False)
    # A free meetup next week with two sign-ups.
    soon = await make_event(club=club, created_by=owner, name="TinyML Circle", event_datetime=NOW + timedelta(days=5), has_fee=False, reg_fee=0)
    await make_registration(event=soon, user=guests[4])
    await make_registration(event=soon, user=guests[5])
    # A paid workshop eight months ago: outside the default window, inside the period before it.
    old = await make_event(club=club, created_by=owner, name="Intro to KiCad", event_datetime=NOW - timedelta(days=240), has_fee=True, reg_fee=1000, lifecycle=EventStatus.closed)
    await _paid(make_registration, old, guests[6], 1000)
    # The rival's own paid event must never show up.
    theirs = await make_event(club=rival, created_by=rival_owner, name="Rival Gala", event_datetime=NOW - timedelta(days=3), has_fee=True, reg_fee=900, lifecycle=EventStatus.closed)
    await _paid(make_registration, theirs, guests[0], 900)
    await session.commit()
    return dict(owner=owner, club=club, past=past, soon=soon, old=old, theirs=theirs, h=auth_header(await mint_token(owner)), rival=auth_header(await mint_token(rival_owner)), fan=auth_header(await mint_token(guests[0])))


async def _expense(client, h, **body):
    r = await client.post("/api/v1/clubs/finance/expenses", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def test_the_season_adds_up_by_month_by_type_and_by_event(client, world):
    h, past, soon = world["h"], world["past"], world["soon"]
    await _expense(client, h, event_id=past.id, title="Venue", category="venue", amount=300)
    await _expense(client, h, event_id=past.id, title="Pizza", category="Food", amount=200, note="two large")
    overhead = await _expense(client, h, title="Domain renewal", category="other", amount=100)
    assert overhead["event_id"] is None and overhead["event_name"] is None and overhead["incurred_on"] == NOW.date().isoformat() and overhead["kind"] == "expense"
    # The ledger takes income too: a sponsor for the hackathon, a grant to the club.
    sponsor = await _expense(client, h, event_id=past.id, kind="income", title="Acme sponsorship", category="sponsorship", amount=2000)
    assert sponsor["kind"] == "income" and sponsor["category"] == "sponsorship"
    await _expense(client, h, kind="income", title="College grant", category="grant", amount=300)
    # An income's category must be an income category, and the other way round.
    assert (await client.post("/api/v1/clubs/finance/expenses", json={"kind": "income", "title": "Odd", "category": "venue", "amount": 10}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/clubs/finance/expenses", json={"title": "Odd", "category": "grant", "amount": 10}, headers=h)).status_code == 422

    s = (await client.get("/api/v1/clubs/finance", headers=h)).json()
    # The window runs from four months back through the end of next month.
    end = (datetime(NOW.year + (NOW.month + 1) // 12, (NOW.month + 1) % 12 + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)).date()
    assert s["scope"] == "club" and s["period"]["end"] == end.isoformat() and s["period"]["start"].endswith("-01")
    f = s["figures"]
    # Two paid guests held ₹1,000 and ₹2,300 came in besides; the refund grosses it up; the three expenses come off.
    # One event conducted, as in the window before (the old workshop), so the arrow is flat.
    assert f["events"] == {"conducted": 1, "upcoming": 1, "delta": 0.0}
    assert (f["gross"], f["tickets"], f["other_income"], f["other_income_count"], f["refunds"], f["refund_count"]) == (3800.0, 1000.0, 2300.0, 2, 500.0, 1)
    assert (f["expenses"], f["expense_count"], f["net"]) == (600.0, 3, 2700.0)
    assert (f["outstanding"], f["outstanding_count"], f["paying"], f["regs"], f["avg_ticket"]) == (500.0, 1, 2, 6, 500.0)
    # The period before held the ₹1,000 workshop and spent nothing.
    assert s["deltas"] == {"gross": 280.0, "tickets": 0.0, "merch": None, "refunds": None, "expenses": None, "net": 170.0}
    assert len(s["by_month"]) == 6
    assert [sum(r[k] for r in s["by_month"]) for k in ("tickets", "other", "refunds", "expenses", "net")] == [1000.0, 2300.0, 500.0, 600.0, 2700.0]
    assert s["by_type"] == [
        {"type": "Hackathon", "events": 1, "tickets": 1000.0, "merch": 0.0, "other": 2000.0, "expenses": 500.0, "net": 2500.0},
        {"type": "General", "events": 1, "tickets": 0.0, "merch": 0.0, "other": 0.0, "expenses": 0.0, "net": 0.0},
        {"type": "Club", "events": 0, "tickets": 0.0, "merch": 0.0, "other": 300.0, "expenses": 100.0, "net": 200.0},
    ]
    rows = s["by_event"]
    assert [r["name"] for r in rows] == ["TinyML Circle", "Hack Night 04"]
    assert rows[0]["status"] == "published" and rows[0]["regs"] == 2 and rows[0]["tickets"] == 0.0
    assert rows[1] == rows[1] | {"id": past.id, "name": "Hack Night 04", "type": "Hackathon", "status": "closed", "held": True, "has_fee": True, "fee": 500.0, "regs": 4, "paying": 2, "tickets": 1000.0, "merch": 0.0, "other_income": 2000.0, "online": 1000.0, "cash": 0.0, "refunds": 500.0, "expenses": 500.0, "expense_count": 2, "net": 2500.0, "margin": 83.0, "outstanding": 500.0, "outstanding_count": 1}
    assert rows[0]["held"] is False and rows[0]["has_fee"] is False and rows[0]["margin"] is None
    # Both paid guests paid online; nothing was taken at the door; half of what came in stayed.
    assert (f["online"], f["online_count"], f["cash"], f["cash_count"], f["margin"], f["cost_per_head"]) == (1000.0, 2, 0.0, 0, 82.0, 100.0)
    assert s["channels"] == [{"key": "online", "label": "Paid online", "amount": 1000.0, "count": 2}, {"key": "cash", "label": "Cash at the door", "amount": 0.0, "count": 0}, {"key": "merch", "label": "Merchandise", "amount": 0.0, "count": 0}, {"key": "other", "label": "Sponsorship & other", "amount": 2300.0, "count": 2}]
    assert s["by_category"] == [{"category": "venue", "amount": 300.0, "count": 1}, {"category": "food", "amount": 200.0, "count": 1}, {"category": "other", "amount": 100.0, "count": 1}]
    assert s["income_by_category"] == [{"category": "sponsorship", "amount": 2000.0, "count": 1}, {"category": "grant", "amount": 300.0, "count": 1}]
    assert s["income_categories"][0] == "sponsorship"
    # One guest at the held hackathon still owes; nothing ran at a loss; its ledger has entries.
    assert s["attention"] == {"at_a_loss": [], "owed": [{"id": past.id, "name": "Hack Night 04", "outstanding": 500.0, "outstanding_count": 1}], "unlogged": []}
    assert s["ahead"] == {"events": 1, "paid_events": 0, "regs": 2, "tickets": 0.0, "outstanding": 0.0, "outstanding_count": 0, "expenses": 0.0}
    assert [x["title"] for x in s["expenses"]] == ["College grant", "Acme sponsorship", "Domain renewal", "Pizza", "Venue"] and s["expenses"][3]["category"] == "food" and s["expenses"][3]["event_name"] == "Hack Night 04"
    assert [x["kind"] for x in s["expenses"]] == ["income", "income", "expense", "expense", "expense"]
    # An entry can change kind, and its category must follow.
    turned = await client.patch(f"/api/v1/clubs/finance/expenses/{sponsor['id']}", json={"kind": "expense", "category": "venue"}, headers=h)
    assert turned.status_code == 200 and turned.json()["kind"] == "expense" and turned.json()["category"] == "venue"
    assert (await client.patch(f"/api/v1/clubs/finance/expenses/{sponsor['id']}", json={"category": "grant"}, headers=h)).status_code == 422
    assert (await client.patch(f"/api/v1/clubs/finance/expenses/{sponsor['id']}", json={"kind": "income"}, headers=h)).json()["category"] == "other"
    assert (await client.patch(f"/api/v1/clubs/finance/expenses/{sponsor['id']}", json={"category": "sponsorship"}, headers=h)).json()["category"] == "sponsorship"
    assert s["categories"][0] == "venue"

    # A wider window takes the old workshop in; the previous window is then empty.
    wide = (await client.get("/api/v1/clubs/finance", params={"from": (NOW - timedelta(days=300)).date().isoformat()}, headers=h)).json()
    assert wide["figures"]["tickets"] == 2000.0 and wide["figures"]["events"]["conducted"] == 2 and wide["deltas"]["gross"] is None
    assert (await client.get("/api/v1/clubs/finance", params={"from": "2030-01-01", "to": "2029-01-01"}, headers=h)).status_code == 422


async def test_one_event_has_its_own_ledger_and_expense_book(client, world):
    h, past = world["h"], world["past"]
    venue = await _expense(client, h, event_id=past.id, title="Venue", category="venue", amount=300, incurred_on=(NOW - timedelta(days=12)).date().isoformat())

    s = (await client.get(f"/api/v1/events/{past.id}/finance", headers=h)).json()
    assert s["scope"] == "event" and s["figures"]["events"] is None and s["by_type"] == [] and s["by_event"] == []
    assert (s["figures"]["gross"], s["figures"]["tickets"], s["figures"]["refunds"], s["figures"]["expenses"], s["figures"]["net"], s["figures"]["outstanding"], s["figures"]["avg_ticket"]) == (1500.0, 1000.0, 500.0, 300.0, 700.0, 500.0, 500.0)
    assert s["deltas"]["gross"] is None
    assert sum(r["tickets"] for r in s["by_month"]) == 1000.0 and sum(r["expenses"] for r in s["by_month"]) == 300.0
    assert [x["id"] for x in s["expenses"]] == [venue["id"]]

    # Corrections, and who may make them.
    fixed = await client.patch(f"/api/v1/clubs/finance/expenses/{venue['id']}", json={"amount": 350, "category": "Venue ", "note": "deposit back"}, headers=h)
    assert fixed.status_code == 200 and fixed.json()["amount"] == 350.0 and fixed.json()["category"] == "venue" and fixed.json()["note"] == "deposit back"
    assert (await client.get(f"/api/v1/events/{past.id}/finance", headers=h)).json()["figures"]["expenses"] == 350.0
    for bad in ({"amount": 0}, {"category": "bribes"}, {"title": "   "}):
        assert (await client.patch(f"/api/v1/clubs/finance/expenses/{venue['id']}", json=bad, headers=h)).status_code == 422, bad
    assert (await client.post("/api/v1/clubs/finance/expenses", json={"event_id": world["theirs"].id, "title": "Not ours", "amount": 10}, headers=h)).status_code == 404
    assert (await client.patch(f"/api/v1/clubs/finance/expenses/{venue['id']}", json={"amount": 1}, headers=world["rival"])).status_code == 404
    assert (await client.delete(f"/api/v1/clubs/finance/expenses/{venue['id']}", headers=world["rival"])).status_code == 404
    assert (await client.get(f"/api/v1/events/{past.id}/finance", headers=world["rival"])).status_code == 403
    assert (await client.get("/api/v1/clubs/finance", headers=world["fan"])).status_code == 403
    assert (await client.delete(f"/api/v1/clubs/finance/expenses/{venue['id']}", headers=h)).json() == {"deleted": True}
    assert (await client.get(f"/api/v1/events/{past.id}/finance", headers=h)).json()["figures"]["expenses"] == 0.0
    assert (await client.delete(f"/api/v1/clubs/finance/expenses/{venue['id']}", headers=h)).status_code == 404


async def test_statements_carry_the_ledger_and_the_totals(client, world):
    h, past = world["h"], world["past"]
    await _expense(client, h, event_id=past.id, title="=SUM(A1)", category="printing", amount=40)
    await _expense(client, h, event_id=past.id, kind="income", title="Sponsor", category="sponsorship", amount=60)

    r = await client.get("/api/v1/clubs/finance/statement.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.headers["content-disposition"].startswith('attachment; filename="yukthi') and r.headers["content-disposition"].endswith('.csv"')
    body = r.text.lstrip("﻿")
    assert "Hack Night 04,Hackathon,closed,4,2,1000.0,0.0,500.0,40.0,1020.0" in body
    assert "TinyML Circle,General,published,2,0,0.0,0.0,0.0,0.0,0.0" in body
    assert ",Hack Night 04,'=SUM(A1),printing,40.0," in body  # a title that looks like a formula is defused
    assert "Other income\n" in body and ",Hack Night 04,Sponsor,sponsorship,60.0," in body
    assert "Other income,60.0" in body and "Net surplus,1020.0" in body and "Outstanding,500.0" in body and "Rival Gala" not in body

    r = await client.get(f"/api/v1/events/{past.id}/finance/statement.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-disposition"].startswith('attachment; filename="hack-night-04') and r.headers["content-disposition"].endswith('-statement.csv"')
    lines = r.text.lstrip("﻿").splitlines()
    guests = [ln for ln in lines if "@example.com" in ln]
    assert len(guests) == 4 and sum(ln.endswith(",refunded") for ln in guests) == 1 and sum(ln.endswith(",paid") for ln in guests) == 2 and sum(ln.endswith(",unpaid") for ln in guests) == 1
    assert "Gross income,1560.0" in lines and "Net surplus,1020.0" in lines
