"""The door: every admission recorded with where, how and by whom, whatever
way the guest came in -- a scan on a gate link, the volunteer app, by hand
from the attendees page, or a walk-up registered on the spot."""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.api.deliveries import service as deliveries
from app.api.events.volunteer.models import Volunteer
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: (sent.append(kw), {"MessageId": f"ses-{len(sent)}"})[1])
    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kw: (sent.append(kw), {"MessageId": f"plain-{len(sent)}"})[1])
    return sent


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="doors@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0, location_name="Innovation Hall")
    other = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=START + timedelta(days=7), duration=2.0)
    a = await make_user(email="ananya-d@example.com")
    b = await make_user(email="bala-d@example.com")
    c = await make_user(email="chitra-d@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")
    r_c = await make_registration(event=other, user=c, full_name="Chitra Nair")
    vol_user = await make_user(email="vani-d@example.com")
    vol_user.full_name = "Vani Rao"
    volunteer = Volunteer(email=vol_user.email, event_id=event.id, user_id=vol_user.id, is_approved=True)
    session.add(volunteer)
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = ticket_starter("classic")
    r = await client.put(f"/api/v1/events/{event.id}/designs/ticket/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=headers)
    assert r.status_code == 200, r.text
    return {"event": event, "other": other, "a": r_a, "b": r_b, "c": r_c, "club": headers, "volunteer": volunteer, "vol": auth_header(await mint_token(vol_user)), "guest": auth_header(await mint_token(a))}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}{path}"


def _scan(code, door="Gate A", ref=None):
    return {"code": code, "door": door, "scanned_at": datetime.now(timezone.utc).isoformat(), "client_ref": ref or uuid.uuid4().hex}


async def test_every_outcome_the_door_can_answer(client, world):
    h = world["club"]
    ok = await client.post(_url(world, "/gate/scan"), json=_scan(world["a"].ticket_id), headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["outcome"] == "CHECKED_IN"
    assert ok.json()["attendee"]["name"] == "Ananya Iyer"
    assert ok.json()["replayed"] is False

    again = await client.post(_url(world, "/gate/scan"), json=_scan(world["a"].ticket_id.lower(), door="Gate B"), headers=h)
    assert again.json()["outcome"] == "ALREADY_CHECKED_IN"
    assert again.json()["checked_in_by"]["door"] == "Gate A"

    wrong = await client.post(_url(world, "/gate/scan"), json=_scan(world["c"].ticket_id), headers=h)
    assert wrong.json()["outcome"] == "WRONG_EVENT"
    assert wrong.json()["other_event"]["name"] == "Demo Day"

    assert (await client.post(_url(world, "/gate/scan"), json=_scan("MOA-NOPE-00000"), headers=h)).json()["outcome"] == "INVALID"

    assert (await client.post(_url(world, f"/tickets/{world['b'].id}/revoke"), json={}, headers=h)).status_code == 200
    revoked = await client.post(_url(world, "/gate/scan"), json=_scan(world["b"].ticket_id), headers=h)
    assert revoked.json()["outcome"] == "REVOKED" and revoked.json()["attendee"]["name"] == "Bala Krishnan"
    assert (await client.post(_url(world, f"/tickets/{world['b'].id}/reinstate"), headers=h)).status_code == 200

    closed = await client.patch(_url(world, "/gate-settings"), json={"door_mode": "closed"}, headers=h)
    assert closed.status_code == 200 and closed.json()["door_mode"] == "closed"
    assert (await client.post(_url(world, "/gate/scan"), json=_scan(world["b"].ticket_id), headers=h)).json()["outcome"] == "EVENT_NOT_OPEN"
    assert (await client.patch(_url(world, "/gate-settings"), json={"door_mode": "open"}, headers=h)).status_code == 200

    # The same scan sent twice -- a retry -- admits once and says so.
    ref = uuid.uuid4().hex
    first = await client.post(_url(world, "/gate/scan"), json=_scan(world["b"].ticket_id, ref=ref), headers=h)
    second = await client.post(_url(world, "/gate/scan"), json=_scan(world["b"].ticket_id, ref=ref), headers=h)
    assert first.json()["outcome"] == "CHECKED_IN" and second.json()["outcome"] == "CHECKED_IN" and second.json()["replayed"] is True

    ctx = await client.get(_url(world, "/gate/context"), headers=h)
    assert ctx.json()["counters"] == {"registered": 2, "checked_in": 2}
    assert ctx.json()["permissions"]["full"] is True
    # Doors are made by admissions, so the refused re-scan at "Gate B" made none.
    assert [d["name"] for d in ctx.json()["doors"]] == ["Gate A"]


async def test_a_gate_link_opens_the_console_on_one_door(client, world):
    h = world["club"]
    door = await client.post(_url(world, "/doors"), json={"name": "Loading bay", "kind": "door"}, headers=h)
    assert door.status_code == 200, door.text
    link = await client.post(_url(world, f"/doors/{door.json()['id']}/link"), json={"can_walkup": False}, headers=h)
    assert link.status_code == 200, link.text
    assert link.json()["url"].startswith("https://club-dashboard.myotherapp.in/gate/")
    key = link.json()["key"]

    ctx = await client.get(_url(world, "/gate/context"), headers={"X-Gate-Key": key})
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["permissions"] == {"check_in": True, "walkup": False, "merch": False, "full": False}
    assert ctx.json()["expected_door_id"] == door.json()["id"]
    assert ctx.json()["me"] == {"name": "Loading bay", "kind": "door"}

    scan = await client.post(_url(world, "/gate/scan"), json={"code": world["a"].ticket_id, "client_ref": uuid.uuid4().hex}, headers={"X-Gate-Key": key})
    assert scan.json()["outcome"] == "CHECKED_IN"
    log = await client.get(_url(world, "/check-ins"), headers=h)
    (row,) = log.json()["items"]
    assert row["door"]["name"] == "Loading bay" and row["via"] == "gate" and row["by"]["kind"] == "NOBODY"

    walk = await client.post(_url(world, "/gate/walkup"), json={"name": "X", "client_ref": uuid.uuid4().hex}, headers={"X-Gate-Key": key})
    assert walk.status_code == 403
    assert (await client.delete(_url(world, f"/gate/check-in/{world['a'].id}"), headers={"X-Gate-Key": key})).status_code == 403

    assert (await client.delete(_url(world, f"/doors/{door.json()['id']}/link"), headers=h)).status_code == 200
    assert (await client.get(_url(world, "/gate/context"), headers={"X-Gate-Key": key})).status_code == 403
    assert (await client.get(_url(world, "/gate/context"))).status_code == 401
    assert (await client.get(_url(world, "/gate/context"), headers=world["guest"])).status_code == 403

    # A door someone was admitted at is retired, not deleted.
    assert (await client.delete(_url(world, f"/doors/{door.json()['id']}"), headers=h)).status_code == 409
    second = await client.post(_url(world, "/doors"), json={"name": "Gate B"}, headers=h)
    retired = await client.patch(_url(world, f"/doors/{door.json()['id']}"), json={"active": False}, headers=h)
    assert retired.json()["active"] is False
    assert (await client.delete(_url(world, f"/doors/{second.json()['id']}"), headers=h)).status_code == 200
    assert (await client.post(_url(world, "/doors"), json={"name": "loading BAY"}, headers=h)).status_code == 409


async def test_the_volunteer_app_and_the_attendees_page_record_the_same_way(client, session, world):
    from doth import application

    h = world["club"]
    app_path = application.url_path_for("checkin_participant", event_id=world["event"].id)
    r = await client.post(app_path, json={"ticket_id": world["a"].ticket_id}, headers=world["vol"])
    assert r.status_code == 200, r.text
    r = await client.post(app_path, json={"ticket_id": world["a"].ticket_id}, headers=world["vol"])
    assert r.status_code == 400 and r.json().get("error_code", r.json().get("errorCode")) in ("TICKET_ALREADY_USED", None)

    by_hand = application.url_path_for("mark_attendance", event_id=world["event"].id, registration_id=str(world["b"].id))
    r = await client.post(by_hand, json={"is_attended": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["is_attended"] is True

    log = await client.get(_url(world, "/check-ins"), headers=h)
    rows = {x["name"]: x for x in log.json()["items"]}
    assert rows["Ananya Iyer"]["via"] == "app" and rows["Ananya Iyer"]["by"]["name"] == "Vani Rao"
    assert rows["Bala Krishnan"]["via"] == "desk" and rows["Bala Krishnan"]["door"]["name"] == "Desk"
    assert log.json()["facets"]["total"] == 2
    assert {m["via"]: m["count"] for m in log.json()["facets"]["methods"]} == {"app": 1, "desk": 1}
    assert [s["name"] for s in log.json()["facets"]["staff"]][0] in ("Vani Rao", "Test User")

    # Undone from the page: the row goes and the mirror follows.
    r = await client.post(by_hand, json={"is_attended": False}, headers=h)
    assert r.status_code == 200 and r.json()["is_attended"] is False
    log = await client.get(_url(world, "/check-ins"), params={"via": "desk"}, headers=h)
    assert log.json()["items"] == [] and log.json()["facets"]["total"] == 1

    # The volunteer's own switches and door, from the team page.
    team = await client.get(_url(world, "/team"), headers=h)
    assert team.status_code == 200, team.text
    (v,) = [x for x in team.json()["volunteers"] if x["email"] == "vani-d@example.com"]
    assert v["can_check_in"] is True and v["can_walkup"] is False and v["check_ins"] == 1
    door = await client.post(_url(world, "/doors"), json={"name": "Gate A"}, headers=h)
    upd = await client.patch(_url(world, f"/team/{v['id']}"), json={"can_walkup": True, "door_id": door.json()["id"]}, headers=h)
    (v2,) = [x for x in upd.json()["volunteers"] if x["id"] == v["id"]]
    assert v2["can_walkup"] is True and v2["door_id"] == door.json()["id"]
    gate_a = next(d for d in upd.json()["doors"] if d["id"] == door.json()["id"])
    assert gate_a["volunteers"] == [{"id": v["id"], "name": "Vani Rao"}]
    ctx = await client.get(_url(world, "/gate/context"), headers=world["vol"])
    assert ctx.json()["permissions"]["walkup"] is True and ctx.json()["expected_door_id"] == door.json()["id"]
    assert ctx.json()["walkup_roles"] == ["attendee"]


async def test_a_walk_up_is_registered_admitted_and_sent_their_ticket(client, world, outbox, background):
    h = world["club"]
    walk = await client.post(_url(world, "/gate/walkup"), json={"name": "Devika Menon", "email": "devika-d@example.com", "door": "Gate A", "client_ref": uuid.uuid4().hex}, headers=h)
    assert walk.status_code == 200, walk.text
    assert walk.json()["outcome"] == "REGISTERED" and walk.json()["checked_in"] is True and walk.json()["ticket_sent"] is True
    assert walk.json()["attendee"]["ticket_id"].startswith("MOA-")
    assert len(outbox) == 1 and outbox[0]["recipients"] == ["devika-d@example.com"]

    # The same address again is the same person; a namesake with no contact is asked about.
    dup = await client.post(_url(world, "/gate/walkup"), json={"name": "Devika M", "email": "devika-d@example.com", "client_ref": uuid.uuid4().hex}, headers=h)
    assert dup.json()["outcome"] == "DUPLICATE" and dup.json()["duplicate_by"] == "CONTACT"
    namesake = await client.post(_url(world, "/gate/walkup"), json={"name": "ananya iyer", "client_ref": uuid.uuid4().hex}, headers=h)
    assert namesake.json()["outcome"] == "DUPLICATE" and namesake.json()["duplicate_by"] == "NAME"
    confirmed = await client.post(_url(world, "/gate/walkup"), json={"name": "Ananya Iyer", "confirm_new": True, "client_ref": uuid.uuid4().hex}, headers=h)
    assert confirmed.json()["outcome"] == "REGISTERED" and confirmed.json()["ticket_sent"] is False

    ctx = await client.get(_url(world, "/gate/context"), headers=h)
    assert ctx.json()["counters"] == {"registered": 4, "checked_in": 2}
    found = await client.get(_url(world, "/gate/search"), params={"q": "devika"}, headers=h)
    assert found.json()[0]["name"] == "Devika Menon" and found.json()[0]["checked_in_at"] is not None

    # By hand from the console, for a guest who left the phone at home.
    hand = await client.post(_url(world, f"/gate/check-in/{world['b'].id}"), json={"door": "Gate A"}, headers=h)
    assert hand.json()["outcome"] == "CHECKED_IN"
    log = await client.get(_url(world, "/check-ins"), headers=h)
    assert {(x["name"], x["via"]) for x in log.json()["items"]} == {("Devika Menon", "desk"), ("Ananya Iyer", "desk"), ("Bala Krishnan", "desk")}


async def test_the_offline_queue_drains_idempotently(client, world):
    h = world["club"]
    ref = uuid.uuid4().hex
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    batch = {"scans": [
        {"code": world["a"].ticket_id, "door": "Gate C", "scanned_at": earlier, "client_ref": ref},
        {"code": world["a"].ticket_id, "door": "Gate C", "scanned_at": earlier, "client_ref": ref},
        {"code": world["c"].ticket_id, "door": "Gate C", "scanned_at": earlier, "client_ref": uuid.uuid4().hex},
    ]}
    r = await client.post(_url(world, "/gate/scan-batch"), json=batch, headers=h)
    assert r.status_code == 200, r.text
    outcomes = [x["response"]["outcome"] for x in r.json()["results"]]
    assert outcomes == ["CHECKED_IN", "CHECKED_IN", "WRONG_EVENT"]
    assert r.json()["results"][1]["response"]["replayed"] is True
    log = await client.get(_url(world, "/check-ins"), headers=h)
    (row,) = log.json()["items"]
    # Admitted when the phone scanned them, not when the queue drained.
    assert row["via"] == "offline_sync"
    assert abs((datetime.fromisoformat(row["checked_in_at"]) - datetime.fromisoformat(earlier)).total_seconds()) < 60
