"""Admission numbers: a format per event, a number per ticket, never rewritten; the gate reads them."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.checkins import service as checkins
from app.api.events.documents import layout_values
from app.api.events.models import EventRegistrationsLink
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="adm@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=3), duration=3.0)
    a = await make_user(email="ananya-adm@example.com")
    early = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    return {"event": event, "early": early, "h": auth_header(await mint_token(owner))}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/admission{path}"


async def test_tickets_are_numbered_in_the_events_format_and_old_numbers_stay(client, session, world, make_user, mint_token):
    h = world["h"]
    event = world["event"]
    got = await client.get(_url(world), headers=h)
    assert got.status_code == 200, got.text
    assert got.json() == {"prefix": None, "digits": 4, "include_code": True, "code": f"E{event.id}", "next_number": f"MOA-0001-E{event.id}", "numbered": 0, "unnumbered": 1}

    # Everyone registered so far gets a number, in the order they came.
    done = await client.post(_url(world, "/number-all"), headers=h)
    assert done.status_code == 200 and done.json() == {"numbered": 1, "first": f"MOA-0001-E{event.id}", "last": f"MOA-0001-E{event.id}"}
    assert (await client.post(_url(world, "/number-all"), headers=h)).json()["numbered"] == 0

    # A new format applies to the next ticket minted, not to numbers already given.
    assert (await client.patch(_url(world), json={"prefix": "gddb", "digits": 3, "include_code": False}, headers=h)).json()["next_number"] == "GDDB-002"
    assert (await client.patch(_url(world), json={"prefix": "bad prefix!"}, headers=h)).status_code == 422
    u = await make_user(email="bala-adm@example.com")
    reg = await client.post(f"/api/v1/events/registration/{event.id}/register", json={"full_name": "Bala Krishnan", "email": "bala-adm@example.com", "phone": "9876543210"}, headers=auth_header(await mint_token(u)))
    assert reg.status_code in (200, 201), reg.text
    bala = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.user_id == u.id))
    assert bala.admission_no == "GDDB-002"
    await session.refresh(world["early"])
    assert world["early"].admission_no == f"MOA-0001-E{event.id}"
    assert (await client.get(_url(world), headers=h)).json()["next_number"] == "GDDB-003"


async def test_the_gate_reads_an_admission_number_and_the_ticket_prints_it(client, session, world):
    h = world["h"]
    event = world["event"]
    await client.post(_url(world, "/number-all"), headers=h)
    await session.refresh(world["early"])
    number = world["early"].admission_no
    assert number == f"MOA-0001-E{event.id}"
    # The attendees list and the gate's search carry it.
    rows = await client.get(f"/api/v1/events/registration/{event.id}/list", headers=h)
    assert rows.json()["items"][0]["admission_no"] == number
    found = await client.get(f"/api/v1/events/{event.id}/gate/search", params={"q": number}, headers=h)
    assert found.status_code == 200 and [r["admission_no"] for r in found.json()] == [number]
    # Scanned (or typed) at the door, the number admits like the ticket code does.
    scan = await client.post(f"/api/v1/events/{event.id}/gate/scan", json={"code": number.lower(), "door": "Gate A", "client_ref": "abcdefgh1234"}, headers=h)
    assert scan.status_code == 200, scan.text
    assert scan.json()["outcome"] == "CHECKED_IN"
    # The ticket's own token reads the number, not the scan code; the editor's sample guest reads the format.
    st = ticket_starter("classic")
    assert (await client.put(f"/api/v1/events/{event.id}/designs/ticket/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)).status_code == 200
    layout = await client.get(f"/api/v1/events/{event.id}/designs/ticket/layout", headers=h)
    assert layout.json()["sampleValues"]["admission_no"] == f"MOA-0041-E{event.id}"
    reg = await checkins.lookup(session, number)
    assert layout_values(reg, reg.event, "ticket")["admission_no"] == number
    assert (await client.patch(_url(world), json={"prefix": "GDDB", "include_code": False}, headers=h)).status_code == 200
    assert (await client.get(f"/api/v1/events/{event.id}/designs/ticket/layout", headers=h)).json()["sampleValues"]["admission_no"] == "GDDB-0041"
