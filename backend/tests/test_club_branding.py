"""Brand colours on the club, falling through to designs and the gate; the contact book; an event copied onto a new date."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.agenda.models import AgendaItems
from app.api.checkins.models import Doors
from app.api.closeout.models import ArchivedContacts
from app.api.events.models import Events
from app.api.events.volunteer.models import Volunteer
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)
FIELDS = [{"key": "tshirt", "label": "T-shirt size", "field_type": "select", "options": ["S", "M", "L"], "required": False}]


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="brand@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=START, duration=8.0, location_name="Innovation Hall", additional_details=FIELDS, about="A day of building.")
    other_owner, other = await make_club(name="Other", email="other-brand@example.com")
    return {"event": event, "club": club, "owner": owner, "h": auth_header(await mint_token(owner)), "other": auth_header(await mint_token(other_owner)), "other_club": other}


async def test_the_clubs_colours_reach_its_designs_and_its_gate(client, session, world):
    h = world["h"]
    eid = world["event"].id
    before = await client.get(f"/api/v1/events/{eid}/designs/ticket/layout", headers=h)
    assert before.json()["brand"]["primary"] == "#1db9a0"
    saved = await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "brand_primary": "#0B5FFF", "brand_secondary": "#FFB000"}, headers=h)
    assert saved.status_code == 200, saved.text
    me = await client.get("/api/v1/clubs/info", headers=h)
    assert me.status_code == 200 and me.json()["brand_primary"] == "#0b5fff" and me.json()["brand_secondary"] == "#ffb000"
    assert (await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "brand_primary": "blue"}, headers=h)).status_code == 422
    # A design with no colours of its own takes the club's.
    after = await client.get(f"/api/v1/events/{eid}/designs/ticket/layout", headers=h)
    assert after.json()["brand"] == {"orgName": "Yukthi", "primary": "#0b5fff", "secondary": "#ffb000", "logoUrl": None}
    ctx = await client.get(f"/api/v1/events/{eid}/gate/context", headers=h)
    assert ctx.status_code == 200
    # Cleared again, the defaults return.
    assert (await client.put("/api/v1/clubs/update", data={"name": "Yukthi", "brand_primary": "", "brand_secondary": ""}, headers=h)).status_code == 200
    assert (await client.get("/api/v1/clubs/info", headers=h)).json()["brand_primary"] is None


async def test_the_contact_book_lists_what_retention_kept_for_this_club_only(client, session, world):
    club, event = world["club"], world["event"]
    ended = START + timedelta(hours=8)
    for name, email, attended in (("Ananya Iyer", "ananya@example.com", True), ("Sita Raman", "sita@example.com", False), ("Bala Krishnan", None, True)):
        session.add(ArchivedContacts(club_id=club.id, event_id=event.id, registration_id=__import__("uuid").uuid4(), name=name, email=email, phone="98765" if attended else None, role="attendee", attended=attended, event_name=event.name, event_ended_at=ended))
    session.add(ArchivedContacts(club_id=world["other_club"].id, event_id=event.id, registration_id=__import__("uuid").uuid4(), name="Not Yours", email="x@example.com", role="attendee", attended=True, event_name="Theirs", event_ended_at=ended))
    await session.commit()
    page = await client.get("/api/v1/clubs/contacts", headers=world["h"])
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 3 and [g["contacts"] for g in page.json()["groups"]] == [3] and page.json()["groups"][0]["event_name"] == "Build Day"
    assert {c["name"] for c in page.json()["items"]} == {"Ananya Iyer", "Sita Raman", "Bala Krishnan"}
    assert (await client.get("/api/v1/clubs/contacts", params={"q": "sita"}, headers=world["h"])).json()["total"] == 1
    assert (await client.get("/api/v1/clubs/contacts", params={"attended": "true"}, headers=world["h"])).json()["total"] == 2
    assert (await client.get("/api/v1/clubs/contacts", headers=world["other"])).json()["total"] == 1
    csv = await client.get("/api/v1/clubs/contacts/export.csv", params={"attended": "true"}, headers=world["h"])
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("Name,Email,Phone,Role,Attended,Event") and len(lines) == 3 and "Ananya Iyer" in csv.text and "Sita" not in csv.text


async def test_an_event_is_copied_onto_a_new_date_with_what_was_asked_for(client, session, world, make_user):
    h = world["h"]
    src = world["event"]
    st = ticket_starter("classic")
    assert (await client.put(f"/api/v1/events/{src.id}/designs/ticket/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)).status_code == 200
    assert (await client.post(f"/api/v1/events/{src.id}/merch", json={"name": "Tote", "stock": 50}, headers=h)).status_code == 201
    assert (await client.post(f"/api/v1/events/{src.id}/doors", json={"name": "Gate A", "kind": "door"}, headers=h)).status_code == 200
    assert (await client.post(f"/api/v1/events/{src.id}/agenda", json={"starts_at": "2026-11-20T04:00:00Z", "ends_at": "2026-11-20T05:00:00Z", "title": "Keynote"}, headers=h)).status_code == 201
    vol = await make_user(email="vol-dup@example.com")
    session.add(Volunteer(email=vol.email, event_id=src.id, user_id=vol.id, is_approved=True, can_check_in=True, can_merch=True))
    await session.commit()

    made = await client.post(f"/api/v1/events/{src.id}/duplicate", json={"name": "Build Day 2027", "event_datetime": "2027-11-20T03:30:00Z", "copy_doors": True, "copy_team": True, "copy_agenda": True}, headers=h)
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["name"] == "Build Day 2027" and body["copied"] == {"interests": 0, "designs": 1, "emails": 0, "merch": 1, "doors": 1, "team": 1, "agenda": 1}
    new = await session.get(Events, body["id"])
    assert new.additional_details == FIELDS and new.about == "A day of building." and new.location_name is None and new.status == "draft" and new.duration == 8.0
    layout = await client.get(f"/api/v1/events/{new.id}/designs/ticket/layout", headers=h)
    assert layout.json()["isOwn"] is True and layout.json()["version"] >= 1
    assert [m["name"] for m in (await client.get(f"/api/v1/events/{new.id}/merch", headers=h)).json()] == ["Tote"]
    assert [d.name for d in (await session.scalars(select(Doors).where(Doors.event_id == new.id))).all()] == ["Gate A"]
    agenda = list((await session.scalars(select(AgendaItems).where(AgendaItems.event_id == new.id))).all())
    assert len(agenda) == 1 and agenda[0].starts_at == datetime(2027, 11, 20, 4, 0, tzinfo=timezone.utc)
    team = (await client.get(f"/api/v1/events/{new.id}/team", headers=h)).json()
    assert [v["email"] for v in team["volunteers"]] == ["vol-dup@example.com"] and team["volunteers"][0]["can_merch"] is True
    # Nothing of the day came across.
    assert (await client.get(f"/api/v1/events/registration/{new.id}/list", headers=h)).json()["items"] == []
    # The other club cannot copy it.
    assert (await client.post(f"/api/v1/events/{src.id}/duplicate", json={"name": "Nope", "event_datetime": "2027-11-20T03:30:00Z"}, headers=world["other"])).status_code in (403, 404)
