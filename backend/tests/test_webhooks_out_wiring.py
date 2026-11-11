"""Every domain change the contract lists emits its webhook exactly once,
from the change itself -- whichever surface made it (app/api/webhooks_out/signals.py)."""
from __future__ import annotations

import itertools
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.webhooks_out import objects
from app.config import settings
from tests.conftest import auth_header
from tests.webhooks_helpers import add_endpoint, deliver_all, events_of, hub  # noqa: F401

API = "/api/v1"
_n = itertools.count(1)
SOON = datetime.now(timezone.utc) + timedelta(days=9)


@pytest.fixture
async def w(client, session, make_club, mint_token, hub):
    owner, club = await make_club(name="Wired Club", email="wired@club.example")
    hub.allocated.add(club.id)
    endpoint, _ = await add_endpoint(session, club.id, share_attendee_details=True, dpdp_confirmed=True)
    category = EventCategories(name="Wired")
    session.add(category)
    await session.commit()
    h = auth_header(await mint_token(owner))
    dash = {**h, "Origin": settings.PUBLIC_WEB_URL}
    form = {
        "name": "Wired Night", "event_datetime": SOON.isoformat(), "duration": "2", "has_fee": "false",
        "status": "published", "category_id": str(category.id), "reg_startdate": "2026-11-01T00:00:00+00:00",
    }
    return SimpleNamespace(owner=owner, club=club, h=h, dash=dash, form=form, category=category, endpoint=endpoint, hub=hub)


def sent(hub) -> Counter:
    return Counter(hub.types())


def last(hub, type_: str) -> dict:
    return [b for b in hub.bodies() if b["type"] == type_][-1]


async def _event(session, w, **values) -> Events:
    from app.core.utils.keys import generate_slug

    name = values.pop("name", f"Wired Event {next(_n)}")
    event = Events(slug=generate_slug(name), name=name, images=[], event_datetime=values.pop("event_datetime", SOON),
                   duration=2.0, club_id=w.club.id, category_id=w.category.id, **values)
    session.add(event)
    await session.commit()
    return event


async def _register(client, event, n: int, **extra):
    body = {"full_name": f"Guest Number {n}", "email": f"guest{n}@example.com", "phone": "98765" + f"{n:05d}"[-5:], **extra}
    r = await client.post(f"{API}/events/registration/{event.id}/register", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _reg(session, event_id, email) -> EventRegistrationsLink:
    return await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event_id, EventRegistrationsLink.email == email)
        .execution_options(include_deleted=True, populate_existing=True)
    )


# ─── Events ──────────────────────────────────────────────────────────────


async def test_an_event_made_edited_unpublished_published_and_cancelled_in_the_dashboard(client, session, w):
    made = await client.post(f"{API}/events/create", data=w.form, headers=w.dash)
    assert made.status_code in (200, 201), made.text
    event_id = made.json()["id"]
    assert sent(w.hub) == Counter({"event.created": 1, "event.published": 1})
    created = last(w.hub, "event.created")
    assert created["origin"] == {"type": "dashboard"}
    assert created["data"]["object"]["id"] == str(event_id) and created["data"]["object"]["status"] == "published"
    # Built after the whole save: the first ticket type the router adds is in.
    assert len(created["data"]["object"]["ticket_types"]) == 1
    w.hub.clear()

    r = await client.put(f"{API}/events/update/{event_id}", data={**w.form, "name": "Wired Night Two"}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"event.updated": 1})
    assert last(w.hub, "event.updated")["data"]["previous"] == {"name": "Wired Night"}
    w.hub.clear()
    # The same again changes nothing: nothing is sent.
    r = await client.put(f"{API}/events/update/{event_id}", data={**w.form, "name": "Wired Night Two"}, headers=w.dash)
    assert r.status_code == 200 and sent(w.hub) == Counter()

    r = await client.patch(f"{API}/events/{event_id}/closeout/settings", json={"status": "draft"}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"event.updated": 1})
    # A draft has no public page: its address goes with the status.
    previous = last(w.hub, "event.updated")["data"]["previous"]
    assert previous["status"] == "published" and set(previous) <= {"status", "public_url"}
    assert last(w.hub, "event.updated")["data"]["object"]["status"] == "draft"
    w.hub.clear()
    r = await client.patch(f"{API}/events/{event_id}/closeout/settings", json={"status": "published"}, headers=w.dash)
    assert r.status_code == 200 and sent(w.hub) == Counter({"event.published": 1})
    w.hub.clear()
    # Retention settings are not part of the event's shape: nothing is sent.
    r = await client.patch(f"{API}/events/{event_id}/closeout/settings", json={"retention_hours": 96}, headers=w.dash)
    assert r.status_code == 200 and sent(w.hub) == Counter()

    r = await client.post(f"{API}/events/{event_id}/cancel", json={"reason": "The hall flooded"}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"event.cancelled": 1})
    body = last(w.hub, "event.cancelled")
    assert body["data"]["object"]["status"] == "cancelled" and body["data"]["previous"] == {"status": "published"}
    w.hub.clear()
    r = await client.post(f"{API}/events/{event_id}/cancel", json={"reason": "Again"}, headers=w.dash)
    assert sent(w.hub) == Counter()  # once


async def test_deleting_an_event_nobody_registered_for(client, session, w):
    event = await _event(session, w)
    r = await client.delete(f"{API}/events/delete/{event.id}", headers=w.dash)
    assert r.status_code in (200, 204), r.text
    assert sent(w.hub) == Counter({"event.cancelled": 1})
    assert last(w.hub, "event.cancelled")["data"]["object"]["status"] == "cancelled"


async def test_the_clock_ending_an_event(session, w):
    from app.api.closeout import service as closeout

    event = await _event(session, w, event_datetime=datetime.now(timezone.utc) - timedelta(hours=8), lifecycle=EventStatus.live)
    assert await closeout.advance_clock(session) == 1
    await deliver_all(session)
    assert sent(w.hub) == Counter({"event.updated": 1})
    body = last(w.hub, "event.updated")
    assert body["data"]["object"]["id"] == str(event.id) and body["data"]["object"]["status"] == "ended"
    assert body["data"]["previous"] == {"status": "published"} and body["origin"] == {"type": "system"}


async def test_a_copied_event_and_one_drafted_by_an_assistant(session, w):
    from app.api.events.duplicate import duplicate_event
    from app.api.mcp import tools

    source = await _event(session, w, name="Source Night")
    out = await duplicate_event(session, source, w.owner, name="Copy Night", event_datetime=SOON + timedelta(days=7))
    await deliver_all(session)
    assert sent(w.hub) == Counter({"event.created": 1})
    assert last(w.hub, "event.created")["data"]["object"]["id"] == str(out["id"])
    assert last(w.hub, "event.created")["data"]["object"]["status"] == "draft"
    w.hub.clear()
    call = SimpleNamespace(session=session, club=w.club, request=None, audit_details=lambda **d: d,
                           principal=SimpleNamespace(user=w.owner, client_name="Claude", member=None))
    draft = await tools.create_event_draft(call, {"name": "Drafted Night", "starts_at": SOON.isoformat(), "duration_hours": 2})
    await deliver_all(session)
    assert sent(w.hub) == Counter({"event.created": 1})
    w.hub.clear()
    await tools.update_event_draft(call, {"event_id": int(draft["event_id"]), "name": "Drafted Night Two"})
    await deliver_all(session)
    assert sent(w.hub) == Counter({"event.updated": 1})
    assert last(w.hub, "event.updated")["data"]["previous"]["name"] == "Drafted Night"


async def test_a_new_ticket_type_is_an_event_update(session, w, monkeypatch):
    from app.api.subscriptions import gates
    from app.api.ticketing import types

    async def allowed(*a, **kw):
        return None

    monkeypatch.setattr(gates, "require_within", allowed)
    monkeypatch.setattr(gates, "require_feature", allowed)
    event = await _event(session, w)
    await types.create(session, event, {"name": "VIP", "price": 500, "capacity": 20})
    await deliver_all(session)
    assert sent(w.hub) == Counter({"event.updated": 1})
    body = last(w.hub, "event.updated")
    names = [t["name"] for t in body["data"]["object"]["ticket_types"]]
    assert "VIP" in names and "ticket_types" in body["data"]["previous"]


# ─── Registrations ───────────────────────────────────────────────────────


async def test_a_registration_made_corrected_and_taken_off(client, session, w):
    event = await _event(session, w)
    await _register(client, event, 1, college="CET")
    assert sent(w.hub) == Counter({"registration.created": 1})
    body = last(w.hub, "registration.created")
    assert body["data"]["object"]["person"]["email"] == "guest1@example.com"
    assert body["data"]["object"]["status"] == "confirmed" and body["origin"] == {"type": "system"}
    reg = await _reg(session, event.id, "guest1@example.com")
    w.hub.clear()
    r = await client.patch(f"{API}/events/registration/{event.id}/info/{reg.id}", json={"full_name": "Guest Renamed"}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"registration.updated": 1})
    previous = last(w.hub, "registration.updated")["data"]["previous"]
    assert previous["person"]["name"] == "Guest Number 1"
    w.hub.clear()
    r = await client.delete(f"{API}/events/registration/{event.id}/info/{reg.id}", params={"confirm_slug": event.slug}, headers=w.dash)
    assert r.status_code in (200, 204), r.text
    assert sent(w.hub) == Counter({"registration.cancelled": 1})
    assert last(w.hub, "registration.cancelled")["data"]["object"]["status"] == "cancelled"


async def test_the_website_origin(client, session, w):
    event = await _event(session, w)
    r = await client.post(f"{API}/events/registration/{event.id}/register",
                          json={"full_name": "Site Guest", "email": "site@example.com", "phone": "9876500001"},
                          headers={"Origin": "https://myotherapp.in"})
    assert r.status_code in (200, 201), r.text
    assert last(w.hub, "registration.created")["origin"] == {"type": "website"}


async def test_requests_confirmed_and_declined(client, session, w):
    from app.api.approvals.models import RegistrationRequests as R

    event = await _event(session, w, requires_approval=True)
    for n in (11, 12):
        r = await client.post(f"{API}/events/registration/{event.id}/register", json={
            "full_name": f"Asker {n}", "email": f"asker{n}@example.com", "phone": f"98765000{n}", "accepts_approval": True,
        })
        assert r.status_code in (200, 201), r.text
    assert sent(w.hub) == Counter()  # a request is not a registration
    first, second = list(await session.scalars(select(R).where(R.event_id == event.id).order_by(R.submitted_at)))
    r = await client.post(f"{API}/events/registration/{event.id}/requests/approve", json={"ids": [str(first.id)]}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"registration.approved": 1, "registration.created": 1})
    approved = last(w.hub, "registration.approved")["data"]["object"]
    assert approved["approval"]["status"] == "approved" and approved["status"] == "confirmed"
    w.hub.clear()
    r = await client.post(f"{API}/events/registration/{event.id}/requests/decline", json={"ids": [str(second.id)]}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"registration.declined": 1})
    declined = last(w.hub, "registration.declined")["data"]["object"]
    assert declined["id"] == str(second.id) and declined["status"] == "declined" and declined["ticket"] is None


async def test_an_account_deleted_cancels_its_registrations(session, w, make_user, make_registration):
    from app.api.users import service as users

    person = await make_user(email="leaving@example.com")
    event = await _event(session, w)
    await make_registration(event=event, user=person)
    await session.commit()
    await users.delete_user(session, person.id)
    await deliver_all(session)
    assert sent(w.hub) == Counter({"registration.cancelled": 1})


# ─── Check-ins and certificates ──────────────────────────────────────────


async def test_a_check_in_once_and_undone(client, session, w, make_user, make_registration):
    event = await _event(session, w, event_datetime=datetime.now(timezone.utc) - timedelta(minutes=30), lifecycle=EventStatus.live)
    person = await make_user(email="door@example.com")
    reg = await make_registration(event=event, user=person)
    await session.commit()
    url = f"{API}/events/{event.id}/gate/check-in/{reg.id}"
    r = await client.post(url, json={}, headers=w.dash)
    assert r.status_code == 200, r.text
    r = await client.post(url, json={}, headers=w.dash)
    assert sent(w.hub) == Counter({"checkin.created": 1})
    made = last(w.hub, "checkin.created")["data"]["object"]
    assert made["registration_id"] == str(reg.id) and made["deleted"] is False
    w.hub.clear()
    r = await client.delete(url, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"checkin.deleted": 1})
    gone = last(w.hub, "checkin.deleted")["data"]["object"]
    assert gone["id"] == made["id"] and gone["deleted"] is True


async def test_a_scan_at_the_gate(client, session, w, make_user, make_registration):
    event = await _event(session, w, event_datetime=datetime.now(timezone.utc) - timedelta(minutes=30), lifecycle=EventStatus.live)
    person = await make_user(email="scan@example.com")
    reg = await make_registration(event=event, user=person)
    await session.commit()
    body = {"code": reg.ticket_id, "door": "Gate A", "scanned_at": datetime.now(timezone.utc).isoformat(), "client_ref": "scan-ref-0001"}
    r = await client.post(f"{API}/events/{event.id}/gate/scan", json=body, headers=w.dash)
    assert r.status_code == 200 and r.json()["outcome"] == "CHECKED_IN", r.text
    assert sent(w.hub) == Counter({"checkin.created": 1})
    assert last(w.hub, "checkin.created")["data"]["object"]["gate"] == "Gate A"


async def test_certificates_issued_once_each(session, w, make_user, make_registration):
    from app.api.certificates import service as certificates

    event = await _event(session, w, event_datetime=datetime.now(timezone.utc) - timedelta(days=1), lifecycle=EventStatus.closed)
    regs = []
    for n in range(2):
        person = await make_user(email=f"cert{n}@example.com")
        regs.append(await make_registration(event=event, user=person, is_attended=True, attended_on=datetime.now(timezone.utc)))
    await session.commit()
    out = await certificates.issue(session, event, "everyone", [])
    assert out["issued"] == 2
    await deliver_all(session)
    assert sent(w.hub) == Counter({"certificate.issued": 2})
    numbers = {b["data"]["object"]["registration_id"] for b in w.hub.bodies()}
    assert numbers == {str(r.id) for r in regs}
    assert all(b["data"]["object"]["name"] for b in w.hub.bodies())
    w.hub.clear()
    await certificates.issue(session, event, "everyone", [])
    await deliver_all(session)
    assert sent(w.hub) == Counter()  # never twice


# ─── Payments ────────────────────────────────────────────────────────────


async def test_a_payment_captured_twice_is_told_once_and_its_refund(session, w, make_user, make_registration, monkeypatch):
    from app.api.payments import service as payments
    from app.api.payments.models import PaymentLogs

    monkeypatch.setattr(payments.razorpay_client.order, "create", lambda data: {"id": f"order_wired_{next(_n)}"})
    event = await _event(session, w, has_fee=True, reg_fee=100)
    payer = await make_user(email="payer@example.com")
    reg = await make_registration(event=event, user=payer, actual_amount=100.0, paid_amount=0.0)
    await session.commit()
    order = await payments.create_razorpay_order(
        session, source="event_registration", payload={"event_id": event.id, "event_registration_id": str(reg.id)}, user_id=payer.id,
    )
    entity = {"id": "pay_wired_1", "order_id": order.razorpay_order_id, "status": "captured", "method": "upi", "amount": 10000, "upi": {}}
    monkeypatch.setattr(payments.razorpay_client.payment, "fetch", lambda payment_id, *a, **kw: dict(entity))
    hook = {"entity": "event", "event": "payment.captured", "payload": {"payment": {"entity": entity}}}
    await payments.handle_razorpay_webhook(session, hook, event_id="evt_wired_1", signature="sig")
    await payments.handle_razorpay_webhook(session, hook, event_id="evt_wired_2", signature="sig")
    await deliver_all(session)
    assert sent(w.hub) == Counter({"payment.completed": 1, "registration.updated": 1})
    paid = last(w.hub, "payment.completed")["data"]["object"]
    assert paid == {**paid, "amount": 10000, "currency": "INR", "status": "captured", "registration_id": str(reg.id), "amount_refunded": 0}
    assert "email" not in json.dumps(paid)
    assert last(w.hub, "registration.updated")["data"]["previous"] == {"status": "awaiting_payment"}
    w.hub.clear()
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.razorpay_payment_id == "pay_wired_1").options(selectinload(PaymentLogs.order)))
    await payments._apply_refund(session, log)
    await deliver_all(session)
    assert sent(w.hub) == Counter({"payment.refunded": 1, "registration.updated": 1})
    refunded = last(w.hub, "payment.refunded")["data"]["object"]
    assert refunded["status"] == "refunded" and refunded["amount_refunded"] == 10000


async def test_part_of_a_payment_refunded(session, w, make_user, make_registration):
    from app.api.club_ops import refunds
    from app.api.club_ops.models import ClubRefunds
    from app.api.payments.models import PaymentLogs
    from tests.club_ops_helpers import captured

    event = await _event(session, w, has_fee=True, reg_fee=200)
    paid = await captured(session, make_user, make_registration, event, amount=200.0)
    r = ClubRefunds(club_id=w.club.id, event_id=event.id, registration_id=paid.reg.id, payment_log_id=paid.log.id,
                    amount=50, reason="Came late", status="succeeded")
    session.add(r)
    await session.commit()
    log = await session.scalar(select(PaymentLogs).where(PaymentLogs.id == paid.log.id).options(selectinload(PaymentLogs.order)))
    await refunds._apply(session, r, log, "rfnd_part_1")
    await deliver_all(session)
    assert sent(w.hub) == Counter({"payment.refunded": 1})
    body = last(w.hub, "payment.refunded")["data"]["object"]
    assert body["status"] == "partially_refunded" and body["amount_refunded"] == 5000 and body["amount"] == 20000


# ─── The club ────────────────────────────────────────────────────────────


async def test_the_club_profile(client, session, w):
    r = await client.put(f"{API}/clubs/update", data={"name": "Wired Club", "tagline": "Robots on Fridays"}, headers=w.dash)
    assert r.status_code == 200, r.text
    assert sent(w.hub) == Counter({"club.updated": 1})
    body = last(w.hub, "club.updated")
    assert body["data"]["object"]["tagline"] == "Robots on Fridays" and body["data"]["previous"] == {"tagline": None}
    w.hub.clear()
    r = await client.put(f"{API}/clubs/update", data={"name": "Wired Club", "tagline": "Robots on Fridays"}, headers=w.dash)
    assert sent(w.hub) == Counter()


def test_the_event_status_the_partner_api_shows():
    def ev(status, deleted=False):
        return SimpleNamespace(lifecycle=status, is_deleted=deleted)

    assert objects.event_status(ev(EventStatus.draft)) == "draft"
    assert objects.event_status(ev(EventStatus.live)) == "published"
    assert objects.event_status(ev(EventStatus.archived)) == "ended"
    assert objects.event_status(ev(EventStatus.published, deleted=True)) == "cancelled"
