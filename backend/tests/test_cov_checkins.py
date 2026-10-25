"""The door by hand: staff find a guest, admit them without a scan, take a
mistaken check-in back, and read the log of every admission with its filters.

GET /events/{id}/gate/search, POST and DELETE /events/{id}/gate/check-in/{reg},
GET /events/{id}/check-ins (app/api/checkins/router.py, service.py).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.api.checkins.models import CheckIns
from app.api.events.models import EventRegistrationsLink
from app.api.events.volunteer.models import Volunteer
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
API = "/api/v1"


@pytest.fixture
async def door(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Door Club", email="door-club@example.com")
    owner.full_name = "Owner Olga"
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=START, duration=3.0)
    other = await make_event(club=club, created_by=owner, name="Other Night", event_datetime=START + timedelta(days=3))
    people = {}
    for key, name, email, extra in (
        ("ananya", "Ananya Iyer", "ananya-c@example.com", {"admission_no": "B21CS042"}),
        ("anand", "Anand Rao", "anand-c@example.com", {}),
        ("bala", "Bala Krishnan", "bala-c@example.com", {}),
        ("chitra", "Chitra Nair", "chitra-c@example.com", {}),
    ):
        u = await make_user(email=email)
        people[key] = await make_registration(event=event, user=u, full_name=name, **extra)
    # Someone on the other event, and someone who left this one: never found here.
    elsewhere = await make_user(email="ananya-elsewhere@example.com")
    people["elsewhere"] = await make_registration(event=other, user=elsewhere, full_name="Ananya Elsewhere")
    gone = await make_user(email="ananya-gone@example.com")
    people["gone"] = await make_registration(event=event, user=gone, full_name="Ananya Gone", is_deleted=True)

    staff = await make_user(email="vol-c@example.com")
    staff.full_name = "Vani Volunteer"
    session.add(Volunteer(email=staff.email, event_id=event.id, user_id=staff.id, is_approved=True))
    looker = await make_user(email="looker-c@example.com")
    session.add(Volunteer(email=looker.email, event_id=event.id, user_id=looker.id, is_approved=True, can_check_in=False))
    rival_owner, _ = await make_club(name="Rival Door", email="rival-door@example.com")
    await session.commit()
    return {
        "event": event, "other": other, **people,
        "club": auth_header(await mint_token(owner)), "owner": owner,
        "vol": auth_header(await mint_token(staff)), "staff": staff,
        "looker": auth_header(await mint_token(looker)),
        "rival": auth_header(await mint_token(rival_owner)),
    }


def _url(w, path):
    return f"{API}/events/{w['event'].id}{path}"


async def _search(client, w, q, headers=None):
    r = await client.get(_url(w, "/gate/search"), params={"q": q}, headers=headers or w["club"])
    assert r.status_code == 200, r.text
    return r.json()


# --- search -------------------------------------------------------------------


async def test_search_finds_a_guest_by_name_email_ticket_or_admission_number(client, door):
    w = door
    # Fewer than two characters is not a search.
    assert await _search(client, w, "a") == []
    assert await _search(client, w, "  ") == []

    # By name, case-blind, in name order; never another event's guest or a removed one.
    assert [r["name"] for r in await _search(client, w, "ANAN")] == ["Anand Rao", "Ananya Iyer"]
    # By email.
    (bala,) = await _search(client, w, "bala-c@")
    assert bala["id"] == str(w["bala"].id) and bala["email"] == "bala-c@example.com" and bala["checked_in_at"] is None
    # By part of the ticket.
    (chitra,) = await _search(client, w, w["chitra"].ticket_id[-6:].lower())
    assert chitra["name"] == "Chitra Nair" and chitra["ticket_id"] == w["chitra"].ticket_id
    # By admission number, typed whole in any case: that guest first.
    found = await _search(client, w, "b21cs042")
    assert found[0]["name"] == "Ananya Iyer" and found[0]["admission_no"] == "B21CS042"


async def test_search_answers_at_most_eight(client, session, door, make_user, make_registration):
    w = door
    for i in range(10):
        u = await make_user(email=f"many-{i}@example.com")
        await make_registration(event=w["event"], user=u, full_name=f"Manoj {i:02d}")
    await session.commit()
    found = await _search(client, w, "manoj")
    assert [r["name"] for r in found] == [f"Manoj {i:02d}" for i in range(8)]


async def test_search_needs_the_check_in_permission(client, door):
    w = door
    assert (await client.get(_url(w, "/gate/search"), params={"q": "anan"}, headers=w["looker"])).status_code == 403
    assert [r["name"] for r in await _search(client, w, "anan", headers=w["vol"])] == ["Anand Rao", "Ananya Iyer"]
    assert (await client.get(_url(w, "/gate/search"), params={"q": "anan"}, headers=w["rival"])).status_code == 403
    assert (await client.get(_url(w, "/gate/search"), params={"q": "anan"})).status_code == 401


# --- by hand, and undone ------------------------------------------------------


async def test_a_guest_checked_in_by_hand_and_the_check_in_taken_back(client, session, door):
    w = door
    reg = w["bala"]
    hand = await client.post(_url(w, f"/gate/check-in/{reg.id}"), json={}, headers=w["club"])
    assert hand.status_code == 200, hand.text
    assert hand.json()["outcome"] == "CHECKED_IN" and hand.json()["attendee"]["name"] == "Bala Krishnan"
    # Recorded at the desk, by the person who did it.
    row = await session.scalar(select(CheckIns).where(CheckIns.registration_id == reg.id))
    assert row.via.value == "desk" and row.door_label == "Desk" and row.checked_in_by == w["owner"].id
    await session.refresh(reg)
    assert reg.is_attended is True and reg.attended_on is not None
    # The search now shows them in.
    (found,) = await _search(client, w, "bala")
    assert found["checked_in_at"] is not None

    # A second press is not a second admission.
    again = await client.post(_url(w, f"/gate/check-in/{reg.id}"), json={"door": "Gate B"}, headers=w["club"])
    assert again.json()["outcome"] == "ALREADY_CHECKED_IN"
    assert again.json()["checked_in_by"]["door"] == "Desk"

    # The organiser takes it back: the row goes, and the registration's mirror with it.
    undo = await client.delete(_url(w, f"/gate/check-in/{reg.id}"), headers=w["club"])
    assert undo.status_code == 200 and undo.json() == {"ok": True}
    assert await session.scalar(select(CheckIns).where(CheckIns.registration_id == reg.id)) is None
    await session.refresh(reg)
    assert reg.is_attended is False and reg.attended_on is None
    (found,) = await _search(client, w, "bala")
    assert found["checked_in_at"] is None
    # Nothing left to undo.
    assert (await client.delete(_url(w, f"/gate/check-in/{reg.id}"), headers=w["club"])).status_code == 404

    # And they can come in again, at a named door.
    back = await client.post(_url(w, f"/gate/check-in/{reg.id}"), json={"door": "Gate B"}, headers=w["club"])
    assert back.json()["outcome"] == "CHECKED_IN"
    row = await session.scalar(select(CheckIns).where(CheckIns.registration_id == reg.id))
    assert row.door_label == "Gate B"


async def test_by_hand_and_undo_stay_on_this_event_and_its_people(client, session, door):
    w = door
    # Another event's guest, a removed one, or an id that is nobody's: 404, nothing written.
    for reg_id in (w["elsewhere"].id, w["gone"].id, uuid.uuid4()):
        assert (await client.post(_url(w, f"/gate/check-in/{reg_id}"), json={}, headers=w["club"])).status_code == 404
        assert (await client.delete(_url(w, f"/gate/check-in/{reg_id}"), headers=w["club"])).status_code == 404
    assert await session.scalar(select(CheckIns).where(CheckIns.event_id.in_([w["event"].id, w["other"].id]))) is None

    # A volunteer may admit by hand, but only the organiser may undo.
    by_vol = await client.post(_url(w, f"/gate/check-in/{w['chitra'].id}"), json={"door": "Gate A"}, headers=w["vol"])
    assert by_vol.json()["outcome"] == "CHECKED_IN"
    assert (await client.delete(_url(w, f"/gate/check-in/{w['chitra'].id}"), headers=w["vol"])).status_code == 403
    # One without the check-in switch may do neither.
    assert (await client.post(_url(w, f"/gate/check-in/{w['anand'].id}"), json={}, headers=w["looker"])).status_code == 403
    # Another club's owner may not touch this event's door.
    assert (await client.delete(_url(w, f"/gate/check-in/{w['chitra'].id}"), headers=w["rival"])).status_code == 403
    assert await session.scalar(select(CheckIns).where(CheckIns.registration_id == w["chitra"].id)) is not None


# --- the log --------------------------------------------------------------------


async def _admit_four(client, session, w):
    """Ananya at Gate A by the owner, Anand at Gate B by the volunteer, Bala
    at the desk by the owner, Chitra scanned at Gate A by the volunteer; then
    spread their times out so the order is known."""
    h, v = w["club"], w["vol"]

    def scan(code, door):
        return {"code": code, "door": door, "client_ref": uuid.uuid4().hex}

    assert (await client.post(_url(w, "/gate/scan"), json=scan(w["ananya"].ticket_id, "Gate A"), headers=h)).json()["outcome"] == "CHECKED_IN"
    assert (await client.post(_url(w, "/gate/scan"), json=scan(w["anand"].ticket_id, "Gate B"), headers=v)).json()["outcome"] == "CHECKED_IN"
    assert (await client.post(_url(w, f"/gate/check-in/{w['bala'].id}"), json={}, headers=h)).json()["outcome"] == "CHECKED_IN"
    assert (await client.post(_url(w, "/gate/scan"), json=scan(w["chitra"].ticket_id, "Gate A"), headers=v)).json()["outcome"] == "CHECKED_IN"
    base = START - timedelta(minutes=30)
    for minutes, key in ((0, "ananya"), (5, "anand"), (10, "bala"), (15, "chitra")):
        await session.execute(update(CheckIns).where(CheckIns.registration_id == w[key].id).values(checked_in_at=base + timedelta(minutes=minutes)))
    await session.commit()


async def _log(client, w, headers=None, **params):
    r = await client.get(_url(w, "/check-ins"), params=params, headers=headers or w["club"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_log_lists_every_admission_with_where_how_and_by_whom(client, session, door):
    w = door
    await _admit_four(client, session, w)
    log = await _log(client, w)
    assert log["total"] == 4 and log["page"] == 1 and log["page_size"] == 50
    # Newest first by default.
    assert [x["name"] for x in log["items"]] == ["Chitra Nair", "Bala Krishnan", "Anand Rao", "Ananya Iyer"]
    rows = {x["name"]: x for x in log["items"]}
    assert rows["Ananya Iyer"]["door"]["name"] == "Gate A" and rows["Ananya Iyer"]["via"] == "gate"
    assert rows["Ananya Iyer"]["by"] == {"kind": "PERSON", "id": w["owner"].id, "name": "Owner Olga"}
    assert rows["Anand Rao"]["by"]["name"] == "Vani Volunteer" and rows["Anand Rao"]["door"]["name"] == "Gate B"
    assert rows["Bala Krishnan"]["via"] == "desk" and rows["Bala Krishnan"]["door"]["name"] == "Desk"
    assert rows["Bala Krishnan"]["registration_id"] == str(w["bala"].id)
    assert rows["Bala Krishnan"]["ticket_id"] == w["bala"].ticket_id and rows["Bala Krishnan"]["role"] == "attendee"

    # The facets describe the whole event.
    f = log["facets"]
    assert f["total"] == 4
    assert {d["name"]: d["count"] for d in f["doors"]} == {"Gate A": 2, "Gate B": 1, "Desk": 1}
    assert {m["via"]: m["count"] for m in f["methods"]} == {"gate": 3, "desk": 1}
    assert sorted((s["name"], s["count"]) for s in f["staff"]) == [("Owner Olga", 2), ("Vani Volunteer", 2)]
    assert datetime.fromisoformat(f["first_at"]) == START - timedelta(minutes=30)
    assert datetime.fromisoformat(f["last_at"]) == START - timedelta(minutes=15)

    # Oldest first.
    assert [x["name"] for x in (await _log(client, w, order="oldest"))["items"]] == ["Ananya Iyer", "Anand Rao", "Bala Krishnan", "Chitra Nair"]


async def test_the_logs_filters_and_pages_leave_the_facets_whole(client, session, door):
    w = door
    await _admit_four(client, session, w)
    gate_a = next(d for d in (await _log(client, w))["facets"]["doors"] if d["name"] == "Gate A")

    by_door = await _log(client, w, door_id=gate_a["id"])
    assert {x["name"] for x in by_door["items"]} == {"Ananya Iyer", "Chitra Nair"} and by_door["total"] == 2
    assert by_door["facets"]["total"] == 4  # chips never vanish while a filter is on

    by_via = await _log(client, w, via="desk")
    assert [x["name"] for x in by_via["items"]] == ["Bala Krishnan"]
    assert (await _log(client, w, via="kiosk"))["items"] == []

    by_staff = await _log(client, w, by=w["staff"].id)
    assert {x["name"] for x in by_staff["items"]} == {"Anand Rao", "Chitra Nair"}

    # The words search name, email and ticket.
    assert [x["name"] for x in (await _log(client, w, q="anand-c@"))["items"]] == ["Anand Rao"]
    assert [x["name"] for x in (await _log(client, w, q=w["bala"].ticket_id.lower()))["items"]] == ["Bala Krishnan"]
    assert {x["name"] for x in (await _log(client, w, q="ANAN"))["items"]} == {"Ananya Iyer", "Anand Rao"}

    # Filters combine.
    assert [x["name"] for x in (await _log(client, w, door_id=gate_a["id"], by=w["staff"].id))["items"]] == ["Chitra Nair"]

    # Pages of two, newest first, with the total over all of them.
    p1 = await _log(client, w, page_size=2)
    p2 = await _log(client, w, page_size=2, page=2)
    p3 = await _log(client, w, page_size=2, page=3)
    assert [x["name"] for x in p1["items"]] == ["Chitra Nair", "Bala Krishnan"]
    assert [x["name"] for x in p2["items"]] == ["Anand Rao", "Ananya Iyer"]
    assert p3["items"] == [] and p1["total"] == p2["total"] == p3["total"] == 4

    # Nonsense is refused at the edge.
    assert (await client.get(_url(w, "/check-ins"), params={"via": "teleport"}, headers=w["club"])).status_code == 422
    assert (await client.get(_url(w, "/check-ins"), params={"page_size": 500}, headers=w["club"])).status_code == 422
    assert (await client.get(_url(w, "/check-ins"), params={"order": "random"}, headers=w["club"])).status_code == 422


async def test_the_log_is_the_clubs_alone(client, session, door):
    w = door
    await _admit_four(client, session, w)
    assert (await client.get(_url(w, "/check-ins"), headers=w["rival"])).status_code == 403
    assert (await client.get(_url(w, "/check-ins"))).status_code == 401
    # A volunteer is not the club: the log is the organiser's.
    assert (await client.get(_url(w, "/check-ins"), headers=w["vol"])).status_code in (401, 403)
    # An undone admission leaves the log.
    assert (await client.delete(_url(w, f"/gate/check-in/{w['bala'].id}"), headers=w["club"])).status_code == 200
    log = await _log(client, w)
    assert "Bala Krishnan" not in {x["name"] for x in log["items"]} and log["facets"]["total"] == 3
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == w["bala"].id))
    assert reg.is_attended is False
