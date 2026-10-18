"""Kiosks: paired from the dashboard, a ticket read at the screen, "this
isn't me", staff mode, and a guest checking themselves in from a phone."""
import re
import uuid
from datetime import datetime, timezone

import pytest

from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: (sent.append(kw), {"MessageId": "ses-1"})[1])
    return sent


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="kiosk@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0, location_name="Innovation Hall")
    other = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=START, duration=2.0)
    a = await make_user(email="ananya-k@example.com")
    b = await make_user(email="bala-k@example.com")
    c = await make_user(email="chitra-k@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")
    r_c = await make_registration(event=other, user=c, full_name="Chitra Nair")
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = ticket_starter("classic")
    assert (await client.put(f"/api/v1/events/{event.id}/designs/ticket/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=headers)).status_code == 200
    paired = await client.post(f"/api/v1/events/{event.id}/kiosks", json={"name": "Lobby kiosk", "shift_hours": 8}, headers=headers)
    assert paired.status_code == 200, paired.text
    return {"event": event, "a": r_a, "b": r_b, "c": r_c, "club": headers, "kiosk": {"X-Kiosk-Token": paired.json()["token"]}, "device": paired.json()["device"], "pair": paired.json()}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/kiosks{path}"


async def test_pairing_rotating_and_unpairing(client, world):
    h = world["club"]
    assert world["pair"]["url"].startswith("https://clubs-next.myotherapp.in/kiosk?token=kiosk_")
    assert world["device"]["door_label"] == "Lobby kiosk" and world["device"]["active"] is True
    doors = await client.get(f"/api/v1/events/{world['event'].id}/doors", headers=h)
    assert [(d["name"], d["kind"]) for d in doors.json()] == [("Lobby kiosk", "kiosk")]

    ctx = await client.get("/api/v1/kiosk/context", headers=world["kiosk"])
    assert ctx.status_code == 200, ctx.text
    assert ctx.json()["event"]["name"] == "Build Night" and ctx.json()["device"]["door"] == "Lobby kiosk"
    assert ctx.json()["counters"] == {"registered": 2, "checked_in": 0, "checked_in_here": 0}
    listed = await client.get(_url(world), headers=h)
    assert listed.json()[0]["online"] is True

    rotated = await client.post(_url(world, f"/{world['device']['id']}/rotate"), json={"shift_hours": 4}, headers=h)
    assert rotated.status_code == 200
    assert (await client.get("/api/v1/kiosk/context", headers=world["kiosk"])).status_code == 401
    fresh = {"X-Kiosk-Token": rotated.json()["token"]}
    assert (await client.get("/api/v1/kiosk/context", headers=fresh)).status_code == 200
    # The token in the query string would land in every access log: the API
    # takes it from the header only (the frame-loaded card PDFs excepted, for now).
    assert (await client.get("/api/v1/kiosk/context", params={"token": rotated.json()["token"]})).status_code == 401
    assert (await client.delete(_url(world, f"/{world['device']['id']}"), headers=h)).json()["active"] is False
    assert (await client.get("/api/v1/kiosk/context", headers=fresh)).status_code == 401


async def test_a_ticket_at_the_kiosk_and_this_is_not_me(client, world):
    h = world["club"]
    k = world["kiosk"]
    first = await client.post("/api/v1/kiosk/redeem", json={"code": world["a"].ticket_id, "client_ref": uuid.uuid4().hex}, headers=k)
    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "MATCHED" and first.json()["guest"]["first_name"] == "Ananya" and first.json()["guest"]["already_checked_in"] is False
    assert first.json()["auto_print"] is False and first.json()["redemption_token"]
    again = await client.post("/api/v1/kiosk/redeem", json={"code": world["a"].ticket_id, "client_ref": uuid.uuid4().hex}, headers=k)
    assert again.json()["guest"]["already_checked_in"] is True
    log = await client.get(f"/api/v1/events/{world['event'].id}/check-ins", headers=h)
    (row,) = log.json()["items"]
    assert row["via"] == "kiosk" and row["door"]["name"] == "Lobby kiosk" and row["by"]["kind"] == "KIOSK"

    # Only the scan that admitted them may take it back; a resweep's token may not.
    assert (await client.post("/api/v1/kiosk/not-me", json={"token": again.json()["redemption_token"]}, headers=k)).json() == {"ok": True}
    assert (await client.get(f"/api/v1/events/{world['event'].id}/check-ins", headers=h)).json()["total"] == 1
    assert (await client.post("/api/v1/kiosk/not-me", json={"token": first.json()["redemption_token"]}, headers=k)).json() == {"ok": True}
    assert (await client.get(f"/api/v1/events/{world['event'].id}/check-ins", headers=h)).json()["total"] == 0
    assert (await client.post("/api/v1/kiosk/not-me", json={"token": "nonsense.token"}, headers=k)).status_code == 410

    wrong = await client.post("/api/v1/kiosk/redeem", json={"code": world["c"].ticket_id, "client_ref": uuid.uuid4().hex}, headers=k)
    assert wrong.json()["outcome"] == "WRONG_EVENT" and wrong.json()["other_event"]["name"] == "Demo Day"
    assert (await client.post("/api/v1/kiosk/redeem", json={"code": "MOA-NOPE-00000", "client_ref": uuid.uuid4().hex}, headers=k)).json()["outcome"] == "NO_MATCH"

    assert (await client.post("/api/v1/kiosk/staff", json={"pin": "2468"}, headers=k)).status_code == 403
    assert (await client.patch(_url(world, "/settings"), json={"kiosk_pin": "2468"}, headers=h)).json()["has_pin"] is True
    staff = await client.post("/api/v1/kiosk/staff", json={"pin": "2468"}, headers=k)
    assert staff.status_code == 200 and staff.json()["device"]["name"] == "Lobby kiosk"


async def test_the_kiosk_prints_a_card_on_a_first_admission_when_asked(client, world):
    h = world["club"]
    k = world["kiosk"]
    assert (await client.patch(f"/api/v1/events/{world['event'].id}/idcards/settings", json={"auto_print_badges": True}, headers=h)).status_code == 200
    r = await client.post("/api/v1/kiosk/redeem", json={"code": world["a"].ticket_id, "client_ref": uuid.uuid4().hex}, headers=k)
    assert r.json()["auto_print"] is True and r.json()["print_url"].startswith("/api/v1/kiosk/card.pdf?redemption=")
    pdf = await client.get("/api/v1/kiosk/card.pdf", params={"redemption": r.json()["redemption_token"]}, headers=k)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    jobs = await client.get(f"/api/v1/events/{world['event'].id}/idcards/jobs", headers=h)
    assert jobs.json()[0]["source"] == "kiosk" and jobs.json()[0]["door"] == "Lobby kiosk"
    again = await client.post("/api/v1/kiosk/redeem", json={"code": world["a"].ticket_id, "client_ref": uuid.uuid4().hex}, headers=k)
    assert again.json()["auto_print"] is False and again.json()["guest"]["has_card"] is True


async def test_a_guest_checks_themselves_in_from_a_phone(client, world, outbox):
    h = world["club"]
    k = world["kiosk"]
    off = await client.get("/api/v1/kiosk/self-qr", headers=k)
    assert off.json()["enabled"] is False and off.json()["url"] is None
    assert (await client.patch(_url(world, "/settings"), json={"self_check_in_verify": "phone"}, headers=h)).status_code == 422
    on = await client.patch(_url(world, "/settings"), json={"self_check_in_enabled": True, "self_check_in_verify": "email"}, headers=h)
    assert on.json()["self_check_in_enabled"] is True and on.json()["self_check_in_verify"] == "email"

    qr = await client.get("/api/v1/kiosk/self-qr", headers=k)
    assert qr.json()["enabled"] is True and "/s/" in qr.json()["url"]
    token = qr.json()["url"].rsplit("/s/", 1)[1]
    started = await client.post("/api/v1/self/start", json={"token": token})
    assert started.status_code == 200, started.text
    sid = started.json()["session"]
    assert started.json()["event"]["name"] == "Build Night" and started.json()["verify"] == "email"

    assert (await client.post("/api/v1/self/lookup", json={"session": sid, "name": "Ananya"})).json()["outcome"] == "NOT_FOUND"
    sent = await client.post("/api/v1/self/lookup", json={"session": sid, "name": "ananya iyer"})
    assert sent.json()["outcome"] == "CODE_SENT" and sent.json()["channel"] == "EMAIL" and sent.json()["masked"].endswith("@example.com")
    assert outbox[-1]["recipients"] == ["ananya-k@example.com"]
    code = re.search(r"\b(\d{6})\b", outbox[-1]["subject"]).group(1)
    wrong = await client.post("/api/v1/self/verify", json={"session": sid, "code": "000000" if code != "000000" else "111111"})
    assert wrong.json() == {"outcome": "WRONG_CODE", "attempts_left": 2}
    assert (await client.post("/api/v1/self/verify", json={"session": sid, "code": code})).json()["outcome"] == "READY"

    done = await client.post("/api/v1/self/check-in", json={"session": sid})
    assert done.status_code == 200, done.text
    assert done.json()["outcome"] == "CHECKED_IN" and done.json()["guest"]["first_name"] == "Ananya" and done.json()["can_print"] is True
    log = await client.get(f"/api/v1/events/{world['event'].id}/check-ins", headers=h)
    (row,) = log.json()["items"]
    assert row["via"] == "phone" and row["door"]["name"] == "Lobby kiosk"

    assert (await client.post("/api/v1/self/print", json={"session": sid})).json()["outcome"] == "QUEUED"
    recent = (await client.get("/api/v1/kiosk/self-qr", headers=k)).json()["recent"]
    assert recent[0]["name"] == "Ananya Iyer" and recent[0]["print_requested"] is True
    card = await client.get("/api/v1/kiosk/self-card.pdf", params={"session": sid}, headers=k)
    assert card.status_code == 200 and card.content[:4] == b"%PDF"
    assert (await client.get("/api/v1/self/status", params={"session": sid})).json() == {"checked_in": True, "print": {"status": "DONE"}}
    assert (await client.post("/api/v1/self/print", json={"session": sid})).json()["outcome"] == "ALREADY_PRINTED"

    # The name alone, for a low-stakes meet-up; and a guest already in is told so.
    assert (await client.patch(_url(world, "/settings"), json={"self_check_in_verify": "name"}, headers=h)).status_code == 200
    sid2 = (await client.post("/api/v1/self/start", json={"token": token})).json()["session"]
    assert (await client.post("/api/v1/self/lookup", json={"session": sid2, "name": "Bala Krishnan"})).json()["outcome"] == "READY"
    assert (await client.post("/api/v1/self/check-in", json={"session": sid2})).json()["outcome"] == "CHECKED_IN"
    sid3 = (await client.post("/api/v1/self/start", json={"token": token})).json()["session"]
    already = await client.post("/api/v1/self/lookup", json={"session": sid3, "name": "Ananya Iyer"})
    assert already.json()["outcome"] == "ALREADY_IN" and already.json()["door"] == "Lobby kiosk"
    assert (await client.post("/api/v1/self/start", json={"token": "bad.token"})).status_code == 410
