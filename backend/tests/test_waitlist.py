"""Waiting lists (app/api/ticketing/waitlist.py, docs/TICKETING.md): joining a
sold-out type, a place offered as one frees up, the claim link, offers that
run out moving to the next person, the club's list, and a paid type's claim
taking its place when the payment lands."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.api.events.models import EventRegistrationsLink
from app.api.payments import service as payments
from app.api.ticketing import sweep, waitlist
from app.api.ticketing.models import TicketTypes, WaitlistEntries
from app.core import jobs
from tests.conftest import auth_header
from tests.test_money_integrity import FakeRazorpay, rzp_fake  # noqa: F401
from tests.test_ticketing import register, set_types

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest_asyncio.fixture
async def line(client, session, make_club, make_event, mint_token, mail):
    """A free event whose one "Seat" type has one place; its waiting list on."""
    owner, club = await make_club(name="Line Club", email="line-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Line Night", event_datetime=NOW + timedelta(days=5))
    await session.commit()
    h = auth_header(await mint_token(owner))
    shop = SimpleNamespace(owner=owner, club=club, h=h)
    types = await set_types(client, shop, event, [{"name": "Seat", "capacity": 1}, {"name": "Standing"}])
    return SimpleNamespace(owner=owner, club=club, event=event, h=h, seat=types["Seat"]["id"], standing=types["Standing"]["id"], mail=mail)


def join(client, w, n, **extra):
    body = {"full_name": f"Waiting Person {n}", "email": f"wait{n}@example.com", "ticket_type_id": w.seat}
    body.update(extra)
    return client.post(f"{API}/events/{w.event.id}/waitlist", json=body)


async def enable(client, w, **extra):
    r = await client.put(f"{API}/events/{w.event.id}/ticketing/waitlist/settings", json={"enabled": True, **extra}, headers=w.h)
    assert r.status_code == 200, r.text
    return r.json()


async def entry(session, entry_id) -> WaitlistEntries:
    return await session.scalar(select(WaitlistEntries).where(WaitlistEntries.id == entry_id).execution_options(populate_existing=True))


async def remove_registration(client, w, reg_id):
    r = await client.delete(
        f"{API}/events/registration/{w.event.id}/info/{reg_id}", params={"confirm_slug": w.event.slug}, headers=w.h,
    )
    assert r.status_code == 200, r.text


async def test_joining_needs_the_list_on_and_the_type_sold_out(client, session, line):
    closed = await join(client, line, 1)
    assert closed.status_code == 409 and closed.json()["error_code"] == "WAITLIST_CLOSED"
    assert (await enable(client, line)) == {"enabled": True, "offer_hours": 24, "allowed": True}
    early = await join(client, line, 1)
    assert early.status_code == 409 and early.json()["error_code"] == "WAITLIST_NOT_NEEDED"
    assert (await register(client, line.event, ticket_type_id=line.seat)).status_code == 200
    # Sold out: the refusal says the list can be joined.
    refused = await register(client, line.event, ticket_type_id=line.seat)
    assert refused.status_code == 409 and refused.json()["errors"]["waitlist"] is True

    first = await join(client, line, 1)
    assert first.status_code == 200, first.text
    assert (first.json()["status"], first.json()["position"], first.json()["created"]) == ("waiting", 1, True)
    again = await join(client, line, 1, email="WAIT1@example.com ")
    assert (again.json()["id"], again.json()["created"]) == (first.json()["id"], False)
    second = (await join(client, line, 2)).json()
    assert second["position"] == 2
    no_contact = await client.post(f"{API}/events/{line.event.id}/waitlist", json={"full_name": "Nobody", "ticket_type_id": line.seat})
    assert no_contact.status_code == 422 and no_contact.json()["error_code"] == "WAITLIST_CONTACT_REQUIRED"
    page = (await client.get(f"{API}/events/{line.event.id}/ticket-types")).json()
    seat = next(t for t in page["types"] if t["id"] == line.seat)
    assert (seat["status"], seat["waitlist"], page["waitlist"]["enabled"]) == ("sold_out", True, True)


async def test_a_freed_place_is_offered_to_the_next_in_line_and_claimed(client, session, line):
    await enable(client, line, offer_hours=12)
    taken = (await register(client, line.event, ticket_type_id=line.seat)).json()
    first = (await join(client, line, 1, phone="9811111111", whatsapp_opt_in=True)).json()
    second = (await join(client, line, 2)).json()
    # Nothing free: the sweep offers nothing.
    assert (await sweep.run_sweep(session))["offered"] == 0

    await remove_registration(client, line, taken["event_registration_id"])
    # The place is held for the line: nobody new takes it, before or after the offer.
    held = await register(client, line.event, ticket_type_id=line.seat)
    assert held.status_code == 409 and held.json()["error_code"] == "TICKET_TYPE_SOLD_OUT"
    done = await sweep.run_sweep(session)
    await jobs.settle()
    assert done["offered"] == 1
    offered = await entry(session, first["id"])
    assert offered.status == "offered" and offered.offer_token and offered.offers_made == 1
    assert timedelta(hours=11, minutes=59) < offered.offer_expires_at - offered.offered_at <= timedelta(hours=12)
    assert (await entry(session, second["id"])).status == "waiting"
    # Told by email, with the claim link.
    sent = [m for m in line.mail if m["recipients"] == ["wait1@example.com"]]
    assert len(sent) == 1 and waitlist.offer_url(offered.offer_token) in sent[0]["html"]
    assert waitlist.offer_url(offered.offer_token).endswith(f"/claim/{offered.offer_token}")
    held = await register(client, line.event, ticket_type_id=line.seat)
    assert held.status_code == 409

    page = (await client.get(f"{API}/waitlist/offers/{offered.offer_token}")).json()
    assert (page["status"], page["needs_payment"], page["needs_email"]) == ("offered", False, False)
    assert page["email"] == "wa•••@example.com" and page["ticket_type"]["name"] == "Seat"
    claimed = await client.post(f"{API}/waitlist/offers/{offered.offer_token}/claim", json={})
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["ticket_type"]["name"] == "Seat" and body["pay_amount"] == 0 and body["auth_token"]
    assert (await entry(session, first["id"])).status == "claimed"
    assert await session.scalar(select(TicketTypes.taken_count).where(TicketTypes.id == line.seat).execution_options(populate_existing=True)) == 1
    # The link is spent.
    again = await client.post(f"{API}/waitlist/offers/{offered.offer_token}/claim", json={})
    assert again.status_code == 409 and again.json()["error_code"] == "OFFER_CLAIMED"


async def test_an_offer_that_runs_out_moves_to_the_next_person(client, session, line):
    await enable(client, line)
    taken = (await register(client, line.event, ticket_type_id=line.seat)).json()
    first = (await join(client, line, 1)).json()
    second = (await join(client, line, 2)).json()
    await remove_registration(client, line, taken["event_registration_id"])
    await sweep.run_sweep(session)
    offered = await entry(session, first["id"])
    token = offered.offer_token
    offered.offer_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    # Run out, even before the sweep closes it: it holds nothing and claims nothing.
    late = await client.post(f"{API}/waitlist/offers/{token}/claim", json={})
    assert late.status_code == 410 and late.json()["error_code"] == "OFFER_EXPIRED"
    done = await sweep.run_sweep(session)
    assert done == {"expired_events": 1, "offered": 1}
    assert (await entry(session, first["id"])).status == "expired"
    assert (await entry(session, second["id"])).status == "offered"
    assert (await client.get(f"{API}/waitlist/offers/{token}")).json()["status"] == "expired"


async def test_declining_gives_the_place_to_the_next(client, session, line):
    await enable(client, line)
    taken = (await register(client, line.event, ticket_type_id=line.seat)).json()
    first = (await join(client, line, 1)).json()
    second = (await join(client, line, 2)).json()
    await remove_registration(client, line, taken["event_registration_id"])
    await sweep.run_sweep(session)
    token = (await entry(session, first["id"])).offer_token
    assert (await client.post(f"{API}/waitlist/offers/{token}/decline")).status_code == 200
    await jobs.settle()
    assert (await entry(session, first["id"])).status == "left"
    assert (await entry(session, second["id"])).status == "offered"


async def test_the_clubs_list_export_offer_and_remove(client, session, line):
    await enable(client, line)
    assert (await register(client, line.event, ticket_type_id=line.seat)).status_code == 200
    first = (await join(client, line, 1)).json()
    second = (await join(client, line, 2)).json()
    third = (await join(client, line, 3)).json()
    listed = (await client.get(f"{API}/events/{line.event.id}/ticketing/waitlist", headers=line.h)).json()
    assert listed["counts"]["waiting"] == 3 and listed["total"] == 3
    assert [(e["full_name"], e["position"]) for e in listed["entries"]] == [
        ("Waiting Person 1", 1), ("Waiting Person 2", 2), ("Waiting Person 3", 3),
    ]
    # No place free: a manual offer is refused, never over the cap.
    nope = await client.post(f"{API}/events/{line.event.id}/ticketing/waitlist/{third['id']}/offer", headers=line.h)
    assert nope.status_code == 409 and nope.json()["error_code"] == "WAITLIST_NO_ROOM"
    # One more place: the next in line is offered it, as soon as it is saved.
    r = await client.patch(f"{API}/events/{line.event.id}/ticketing/types/{line.seat}", json={"capacity": 2}, headers=line.h)
    assert r.status_code == 200, r.text
    await jobs.settle()
    assert (await entry(session, first["id"])).status == "offered"
    # The club takes someone off: their place in line goes.
    assert (await client.delete(f"{API}/events/{line.event.id}/ticketing/waitlist/{second['id']}", headers=line.h)).status_code == 200
    assert (await entry(session, second["id"])).status == "removed"
    csv = await client.get(f"{API}/events/{line.event.id}/ticketing/waitlist/export", headers=line.h)
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("Position,Name,Email") and any("Waiting Person 3" in row for row in lines)
    listed = (await client.get(f"{API}/events/{line.event.id}/ticketing/waitlist", headers=line.h)).json()
    offered = next(e for e in listed["entries"] if e["id"] == first["id"])
    assert offered["offer_url"].endswith(offered["offer_url"].rsplit("/", 1)[-1]) and "/claim/" in offered["offer_url"]
    # Raised again, and offered by hand ahead of the line.
    await client.patch(f"{API}/events/{line.event.id}/ticketing/types/{line.seat}", json={"capacity": 3}, headers=line.h)
    by_hand = await client.post(f"{API}/events/{line.event.id}/ticketing/waitlist/{third['id']}/offer", headers=line.h)
    assert by_hand.status_code in (200, 409)
    await jobs.settle()
    assert (await entry(session, third["id"])).status == "offered"
    # Other clubs see nothing of it.
    assert (await client.get(f"{API}/events/{line.event.id}/ticketing/waitlist")).status_code == 401


async def test_the_event_total_full_holds_its_freed_place_for_the_line(client, session, make_club, make_event, mint_token, mail):
    owner, club = await make_club(name="Total Club", email="total-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Total Night", event_datetime=NOW + timedelta(days=5), max_participants=2)
    await session.commit()
    w = SimpleNamespace(event=event, h=auth_header(await mint_token(owner)), seat=None)
    await enable(client, w)
    regs = [(await register(client, event)).json() for _ in range(2)]
    full = await register(client, event)
    assert full.status_code == 400 and full.json()["error_code"] == "EVENT_FULL" and full.json()["errors"]["waitlist"] is True
    waiting = (await client.post(f"{API}/events/{event.id}/waitlist", json={"full_name": "Total Wait", "email": "total-wait@example.com"})).json()
    assert waiting["status"] == "waiting"
    await remove_registration(client, w, regs[0]["event_registration_id"])
    # Freed, and before any offer: still nobody else's.
    assert (await register(client, event)).status_code == 400
    await sweep.run_sweep(session)
    claimed_entry = await entry(session, waiting["id"])
    assert claimed_entry.status == "offered"
    r = await client.post(f"{API}/waitlist/offers/{claimed_entry.offer_token}/claim", json={"full_name": "Total Wait"})
    assert r.status_code == 200, r.text
    assert (await register(client, event)).status_code == 400


async def test_a_paid_claim_is_the_claimers_once_paid(client, session, make_club, make_event, mint_token, mail, rzp_fake):
    owner, club = await make_club(name="Paid Line Club", email="paidline-owner@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Paid Line", event_datetime=NOW + timedelta(days=5), has_fee=True,
        reg_fee=Decimal("400.00"), max_participants=1,
    )
    await session.commit()
    w = SimpleNamespace(event=event, h=auth_header(await mint_token(owner)))
    await enable(client, w)
    holder = (await register(client, event)).json()
    await session.execute(
        update(EventRegistrationsLink).where(EventRegistrationsLink.id == holder["event_registration_id"]).values(is_paid=True, paid_amount=400)
    )
    await session.commit()
    waiting = (await client.post(f"{API}/events/{event.id}/waitlist", json={"full_name": "Paid Waiter", "email": "paid-wait@example.com"})).json()
    await remove_registration(client, w, holder["event_registration_id"])
    await sweep.run_sweep(session)
    offered = await entry(session, waiting["id"])
    assert offered.status == "offered"
    claim = (await client.post(f"{API}/waitlist/offers/{offered.offer_token}/claim", json={})).json()
    assert claim["pay_amount"] == 400.0 and claim["is_paid"] is False
    # Still the claimer's while they pay: claiming again is their same registration.
    again = (await client.post(f"{API}/waitlist/offers/{offered.offer_token}/claim", json={})).json()
    assert again["event_registration_id"] == claim["event_registration_id"]
    assert (await entry(session, waiting["id"])).registration_id is not None
    # Someone else's checkout is refused before money moves.
    other = (await register(client, event))
    assert other.status_code == 400
    order = (await client.post(
        f"{API}/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": event.id, "event_registration_id": claim["event_registration_id"]}},
        headers=auth_header(claim["auth_token"]["access_token"]),
    )).json()
    assert rzp_fake.orders[order["razorpay_order_id"]] == 40000
    p = rzp_fake.pay(order["razorpay_order_id"])
    await payments.verify_razorpay_payment(session, order["razorpay_order_id"], p["id"], payment_details=p, expand_payment_details=False)
    reg = await session.scalar(
        select(EventRegistrationsLink).where(EventRegistrationsLink.id == claim["event_registration_id"]).execution_options(populate_existing=True)
    )
    assert reg.is_paid is True and p["id"] not in rzp_fake.refunded()
    assert (await entry(session, waiting["id"])).status == "claimed"


async def test_a_signed_in_person_sees_and_leaves_their_place(client, session, line, make_user, mint_token):
    await enable(client, line)
    assert (await register(client, line.event, ticket_type_id=line.seat)).status_code == 200
    user = await make_user(email="app-waiter@example.com")
    await session.commit()
    h = auth_header(await mint_token(user))
    joined = await client.post(f"{API}/events/{line.event.id}/waitlist", json={"full_name": "App Waiter", "ticket_type_id": line.seat}, headers=h)
    assert joined.status_code == 200 and joined.json()["position"] == 1
    info = (await client.get(f"{API}/events/info/{line.event.id}", headers=h)).json()
    assert [e["status"] for e in info["ticketing"]["viewer_waitlist"]] == ["waiting"]
    mine = (await client.get(f"{API}/events/{line.event.id}/waitlist/me", headers=h)).json()
    assert mine[0]["ticket_type_name"] == "Seat"
    assert (await client.delete(f"{API}/events/{line.event.id}/waitlist/me", headers=h)).json() == {"left": 1}
    assert (await client.get(f"{API}/events/{line.event.id}/waitlist/me", headers=h)).json() == []


async def test_someone_registered_cannot_join(client, session, line):
    await enable(client, line)
    body = (await register(client, line.event, 900, ticket_type_id=line.seat)).json()
    assert body["ticket_type"]["id"] == line.seat
    r = await join(client, line, 900, email="guest900@example.com")
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_REGISTERED"


async def test_an_events_line_closes_once_it_is_over(client, session, line):
    await enable(client, line)
    assert (await register(client, line.event, ticket_type_id=line.seat)).status_code == 200
    waiting = (await join(client, line, 1)).json()
    assert line.event.id in await waitlist.events_waiting(session)
    line.event.event_datetime = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()
    await sweep.run_sweep(session)
    assert (await entry(session, waiting["id"])).status == "expired"
    assert line.event.id not in await waitlist.events_waiting(session)


async def test_an_offer_reaches_the_app_and_whatsapp_too(client, session, line, monkeypatch, make_user, mint_token):
    from app.core import whatsapp
    from app.core.notifications import service as push

    pushed, texted = [], []

    async def fake_push(session_, user_id, title, body, data=None):
        pushed.append((user_id, data))
        return True

    async def fake_send(number, template, **kwargs):
        texted.append((number, template, kwargs.get("meter")))
        return {"MessageId": "wamid.1", "Provider": "whatsapp", "Address": number, "Via": "platform"}

    monkeypatch.setattr(push, "send_notification_to_user", fake_push)
    monkeypatch.setattr(whatsapp, "configured", lambda: True)
    monkeypatch.setattr(whatsapp, "send", fake_send)
    await enable(client, line)
    taken = (await register(client, line.event, ticket_type_id=line.seat)).json()
    user = await make_user(email="app-offer@example.com")
    await session.commit()
    h = auth_header(await mint_token(user))
    joined = await client.post(
        f"{API}/events/{line.event.id}/waitlist",
        json={"full_name": "App Offer", "ticket_type_id": line.seat, "phone": "9822222222", "whatsapp_opt_in": True}, headers=h,
    )
    assert joined.status_code == 200, joined.text
    await remove_registration(client, line, taken["event_registration_id"])
    await sweep.run_sweep(session)
    await jobs.settle()
    offered = await entry(session, joined.json()["id"])
    assert pushed == [(user.id, {"type": "waitlist_offer", "event_id": str(line.event.id), "url": waitlist.offer_url(offered.offer_token), "token": offered.offer_token})]
    (number, template, meter), = texted
    assert number == "9822222222" and template.name == "waitlist_offer" and template.button == offered.offer_token
    assert template.body[0] == "App" and template.body[1] == "Line Night" and template.body[2] == "Seat"
    assert meter["club_id"] == line.club.id and meter["kind"] == "ticket"
    assert [m["recipients"] for m in line.mail] == [["app-offer@example.com"]]
    # The email is the club's message too: counted once, as a ticket's would be.
    from app.api.message_quota.models import MessageUsage

    usage = await session.scalar(select(MessageUsage).where(MessageUsage.club_id == line.club.id, MessageUsage.channel == "email"))
    assert usage is not None and usage.used == 1 and (usage.kinds or {}).get("ticket") == 1
