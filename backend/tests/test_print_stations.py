"""Print stations: a tablet or the dashboard queues a guest's ID card, a
paired printer device claims it, prints it and confirms
(feature-register/print-station/CONTRACT.md)."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.idcards.models import PrintJobs
from app.api.kiosk import print_requests
from app.api.kiosk.models import CardPrintRequests, KioskDevices
from app.api.users.models import UserTypes
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from tests.conftest import _make_user, auth_header
from tests.racing import committed_keys, sweep

# A card with no saved design is the designer's default, drawn by the render service (faked).
pytestmark = pytest.mark.usefixtures("doc_designs")

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
API = "/api/v1"


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Print Club", email="printclub@example.com")
    event = await make_event(club=club, created_by=owner, name="Print Night", event_datetime=START, duration=3.0)
    regs = []
    for i, name in enumerate(["Ananya Iyer", "Bala Krishnan", "Chitra Nair"]):
        user = await make_user(email=f"guest{i}-ps@example.com")
        regs.append(await make_registration(event=event, user=user, full_name=name))
    outsider = await make_user(email="outsider-ps@example.com")
    await session.commit()
    headers = auth_header(await mint_token(owner))
    kiosk = await client.post(f"{API}/events/{event.id}/kiosks", json={"name": "Lobby tablet", "shift_hours": 8}, headers=headers)
    printer = await client.post(f"{API}/events/{event.id}/kiosks", json={"name": "Desk printer", "shift_hours": 8, "role": "printer"}, headers=headers)
    printer2 = await client.post(f"{API}/events/{event.id}/kiosks", json={"name": "Stage printer", "shift_hours": 8, "role": "printer"}, headers=headers)
    for r in (kiosk, printer, printer2):
        assert r.status_code == 200, r.text
    return {
        "event": event, "regs": regs, "club": headers, "outsider": auth_header(await mint_token(outsider)),
        "kiosk": {"X-Kiosk-Token": kiosk.json()["token"]}, "kiosk_id": kiosk.json()["device"]["id"],
        "printer": {"X-Kiosk-Token": printer.json()["token"]}, "printer_id": printer.json()["device"]["id"], "printer_pair": printer.json(),
        "printer2": {"X-Kiosk-Token": printer2.json()["token"]}, "printer2_id": printer2.json()["device"]["id"],
    }


def _ev(world, path):
    return f"{API}/events/{world['event'].id}{path}"


async def _beat(session, device_id, seconds_ago=0):
    """Make a printer online (or not) without it claiming anything."""
    await session.execute(update(KioskDevices).where(KioskDevices.id == uuid.UUID(device_id)).values(last_seen_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)))
    await session.commit()


async def _admit(client, world, reg):
    r = await client.post(f"{API}/kiosk/redeem", json={"code": reg.ticket_id, "client_ref": uuid.uuid4().hex}, headers=world["kiosk"])
    assert r.json()["outcome"] == "MATCHED", r.text
    return r.json()["redemption_token"]


async def _ask(client, world, reg, headers=None):
    return await client.post(f"{API}/kiosk/print-requests", json={"redemption": await _admit(client, world, reg)}, headers=headers or world["kiosk"])


async def _print_one(client, world, reg_headers=None):
    """The station claims the oldest card, fetches the PDF and confirms it."""
    claimed = await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])
    req = claimed.json()["request"]
    pdf = await client.get(f"{API}{req['card_url']}", headers=world["printer"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF", pdf.text
    done = await client.post(f"{API}/kiosk/printer/requests/{req['id']}/done", json={"ok": True}, headers=world["printer"])
    assert done.json()["status"] == "printed"
    return req


async def _set_policy(client, world, policy):
    r = await client.patch(_ev(world, "/idcards/settings"), json={"reprint_policy": policy}, headers=world["club"])
    assert r.status_code == 200, r.text


# --- pairing, roles ------------------------------------------------------------------------------


async def test_a_printer_is_paired_like_a_kiosk_and_is_not_a_door(client, world):
    assert world["printer_pair"]["url"].startswith("https://club-dashboard.myotherapp.in/print-station?token=kiosk_")
    assert world["printer_pair"]["device"]["role"] == "printer"
    listed = (await client.get(_ev(world, "/kiosks"), headers=world["club"])).json()
    assert sorted((d["name"], d["role"]) for d in listed) == [("Desk printer", "printer"), ("Lobby tablet", "kiosk"), ("Stage printer", "printer")]
    doors = (await client.get(_ev(world, "/doors"), headers=world["club"])).json()
    assert [d["name"] for d in doors] == ["Lobby tablet"]
    rotated = await client.post(_ev(world, f"/kiosks/{world['printer_id']}/rotate"), json={"shift_hours": 4}, headers=world["club"])
    assert rotated.status_code == 200 and "/print-station?token=" in rotated.json()["url"]
    assert (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).status_code == 401
    assert (await client.post(f"{API}/kiosk/printer/claim", headers={"X-Kiosk-Token": rotated.json()["token"]})).status_code == 200
    assert (await client.delete(_ev(world, f"/kiosks/{world['printer_id']}"), headers=world["club"])).json()["active"] is False
    assert (await client.post(f"{API}/kiosk/printer/claim", headers={"X-Kiosk-Token": rotated.json()["token"]})).status_code == 401


async def test_a_kiosk_token_cannot_claim_and_a_printer_token_cannot_redeem_or_ask(client, session, world):
    await _beat(session, world["printer_id"])
    asked = await _ask(client, world, world["regs"][0])
    assert asked.status_code == 201
    rid = asked.json()["id"]
    k, p = world["kiosk"], world["printer"]
    for call in (
        client.post(f"{API}/kiosk/printer/claim", headers=k),
        client.get(f"{API}/kiosk/printer/requests/{rid}/card.pdf", headers=k),
        client.post(f"{API}/kiosk/printer/requests/{rid}/done", json={"ok": True}, headers=k),
        client.post(f"{API}/kiosk/printer/test", headers=k),
    ):
        r = await call
        assert r.status_code == 403 and r.json()["error_code"] == "WRONG_DEVICE_ROLE"
    token = await _admit(client, world, world["regs"][1])
    for call in (
        client.post(f"{API}/kiosk/redeem", json={"code": world["regs"][1].ticket_id, "client_ref": uuid.uuid4().hex}, headers=p),
        client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=p),
        client.get(f"{API}/kiosk/print-requests/{rid}", headers=p),
        client.get(f"{API}/kiosk/context", headers=p),
        client.post(f"{API}/kiosk/staff", json={"pin": "2468"}, headers=p),
    ):
        r = await call
        assert r.status_code == 403 and r.json()["error_code"] == "WRONG_DEVICE_ROLE"


# --- the tablet ------------------------------------------------------------------------------------


async def test_the_context_counts_printers_seen_in_the_last_30_seconds(client, session, world):
    k = world["kiosk"]
    assert (await client.get(f"{API}/kiosk/context", headers=k)).json()["printers"] == {"online": 0}
    await _beat(session, world["printer_id"])
    await _beat(session, world["printer2_id"], seconds_ago=45)
    assert (await client.get(f"{API}/kiosk/context", headers=k)).json()["printers"] == {"online": 1}
    await _beat(session, world["printer2_id"], seconds_ago=5)
    assert (await client.get(f"{API}/kiosk/context", headers=k)).json()["printers"] == {"online": 2}
    await client.delete(_ev(world, f"/kiosks/{world['printer2_id']}"), headers=world["club"])
    assert (await client.get(f"{API}/kiosk/context", headers=k)).json()["printers"] == {"online": 1}


async def test_no_printer_online_is_refused(client, session, world):
    asked = await _ask(client, world, world["regs"][0])
    assert asked.status_code == 409 and asked.json()["error_code"] == "NO_PRINTER"
    await _beat(session, world["printer_id"], seconds_ago=60)
    assert (await _ask(client, world, world["regs"][0])).json()["error_code"] == "NO_PRINTER"
    staff = await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id)}, headers=world["club"])
    assert staff.status_code == 409 and staff.json()["error_code"] == "NO_PRINTER"
    # A chosen printer that is offline, while another is online.
    await _beat(session, world["printer2_id"])
    chosen = await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id), "printer_id": world["printer_id"]}, headers=world["club"])
    assert chosen.status_code == 409 and chosen.json()["error_code"] == "NO_PRINTER"
    ok = await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id), "printer_id": world["printer2_id"]}, headers=world["club"])
    assert ok.status_code == 201 and ok.json()["printer"] == {"id": world["printer2_id"], "name": "Stage printer"}


async def test_a_guest_must_be_admitted_and_a_double_tap_makes_one_request(client, session, world):
    await _beat(session, world["printer_id"])
    reg = world["regs"][0]
    # Not admitted: a redemption cannot exist, and a forged one is refused.
    assert (await client.post(f"{API}/kiosk/print-requests", json={"redemption": "nonsense.token"}, headers=world["kiosk"])).status_code == 410
    assert (await client.post(f"{API}/kiosk/print-requests", json={}, headers=world["kiosk"])).status_code == 422
    token = await _admit(client, world, reg)
    first = await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=world["kiosk"])
    again = await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=world["kiosk"])
    assert first.status_code == 201 and again.status_code == 200, (first.text, again.text)
    body = first.json()
    assert again.json()["id"] == body["id"]
    assert body["status"] == "queued" and body["position"] == 1 and body["name"] == "Ananya Iyer" and body["ticket_code"] == reg.ticket_id and body["source"] == "kiosk"
    assert await session.scalar(select(func.count()).select_from(CardPrintRequests)) == 1
    # The tablet can read its own request, and nobody else's.
    got = await client.get(f"{API}/kiosk/print-requests/{body['id']}", headers=world["kiosk"])
    assert got.status_code == 200 and got.json()["id"] == body["id"]
    other = (await client.post(f"{API}/events/{world['event'].id}/kiosks", json={"name": "Second tablet"}, headers=world["club"])).json()
    assert (await client.get(f"{API}/kiosk/print-requests/{body['id']}", headers={"X-Kiosk-Token": other["token"]})).status_code == 404


async def test_a_ticket_read_by_another_kiosk_cannot_be_used(client, session, world):
    await _beat(session, world["printer_id"])
    token = await _admit(client, world, world["regs"][0])
    other = (await client.post(f"{API}/events/{world['event'].id}/kiosks", json={"name": "Second tablet"}, headers=world["club"])).json()
    r = await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers={"X-Kiosk-Token": other["token"]})
    assert r.status_code == 403


async def test_a_phone_session_the_kiosk_serves_can_ask(client, session, world):
    await _beat(session, world["printer_id"])
    from app.api.kiosk.models import SelfCheckInSessions

    reg = world["regs"][1]
    await _admit(client, world, reg)
    sid = "sess-" + uuid.uuid4().hex
    session.add(SelfCheckInSessions(id=sid, event_id=world["event"].id, kiosk_id=uuid.UUID(world["kiosk_id"]), registration_id=reg.id, verified=True, checked_in=True, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    session.add(SelfCheckInSessions(id="sess-unverified-" + uuid.uuid4().hex, event_id=world["event"].id, kiosk_id=uuid.UUID(world["kiosk_id"]), registration_id=reg.id, verified=False, checked_in=True, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await session.commit()
    r = await client.post(f"{API}/kiosk/print-requests", json={"session": sid}, headers=world["kiosk"])
    assert r.status_code == 201 and r.json()["name"] == "Bala Krishnan"
    unverified = (await session.scalars(select(SelfCheckInSessions.id).where(SelfCheckInSessions.verified == False))).first()  # noqa: E712
    assert (await client.post(f"{API}/kiosk/print-requests", json={"session": unverified}, headers=world["kiosk"])).status_code == 404
    assert (await client.post(f"{API}/kiosk/print-requests", json={"session": "no-such-session-0"}, headers=world["kiosk"])).status_code == 410


async def test_an_unadmitted_guest_cannot_be_asked_for_by_a_kiosk(client, session, world):
    await _beat(session, world["printer_id"])
    from app.api.kiosk.service import _redemption
    from app.api.kiosk.models import KioskDevices as K

    device = await session.get(K, uuid.UUID(world["kiosk_id"]))
    forged = _redemption(world["regs"][2].id, device, True)
    r = await client.post(f"{API}/kiosk/print-requests", json={"redemption": forged}, headers=world["kiosk"])
    assert r.status_code == 409 and r.json()["error_code"] == "NOT_ADMITTED"


# --- the station -------------------------------------------------------------------------------------


async def test_a_card_goes_from_queued_to_printed_and_is_recorded_once(client, session, world):
    await _beat(session, world["printer_id"])
    asked = (await _ask(client, world, world["regs"][0])).json()
    claimed = await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])
    assert claimed.status_code == 200
    c = claimed.json()
    assert c["request"]["id"] == asked["id"] and c["request"]["attempt"] == 1 and c["request"]["source"] == "kiosk" and c["queued"] == 0
    assert c["request"]["name"] == "Ananya Iyer" and c["request"]["card_url"] == f"/kiosk/printer/requests/{asked['id']}/card.pdf"
    mid = (await client.get(f"{API}/kiosk/print-requests/{asked['id']}", headers=world["kiosk"])).json()
    assert mid["status"] == "printing" and mid["position"] is None and mid["printer"]["name"] == "Desk printer"
    pdf = await client.get(f"{API}{c['request']['card_url']}", headers=world["printer"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and pdf.headers["cache-control"] == "no-store"
    done = await client.post(f"{API}/kiosk/printer/requests/{asked['id']}/done", json={"ok": True}, headers=world["printer"])
    assert done.status_code == 200 and done.json()["status"] == "printed" and done.json()["printed_at"]
    again = await client.post(f"{API}/kiosk/printer/requests/{asked['id']}/done", json={"ok": True}, headers=world["printer"])
    assert again.json()["status"] == "printed"
    jobs = (await session.scalars(select(PrintJobs))).all()
    assert len(jobs) == 1 and jobs[0].source.value == "kiosk" and jobs[0].door_label == "Desk printer" and jobs[0].registration_id == world["regs"][0].id
    assert (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json() == {"request": None, "queued": 0}


async def test_the_station_reports_a_failure_in_words(client, session, world):
    await _beat(session, world["printer_id"])
    asked = (await _ask(client, world, world["regs"][0])).json()
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    r = await client.post(f"{API}/kiosk/printer/requests/{claimed['id']}/done", json={"ok": False, "error": "Out of paper" + "!" * 500}, headers=world["printer"])
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["error"].startswith("Out of paper") and len(r.json()["error"]) == 400
    assert (await client.get(f"{API}/kiosk/print-requests/{asked['id']}", headers=world["kiosk"])).json()["status"] == "failed"
    assert await session.scalar(select(func.count()).select_from(PrintJobs)) == 0


async def test_the_card_is_only_for_the_station_that_claimed_it(client, session, world):
    await _beat(session, world["printer_id"])
    await _beat(session, world["printer2_id"])
    asked = (await _ask(client, world, world["regs"][0])).json()
    url = f"{API}/kiosk/printer/requests/{asked['id']}"
    # Not claimed yet: nobody's.
    assert (await client.get(f"{url}/card.pdf", headers=world["printer"])).status_code == 404
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    assert claimed["id"] == asked["id"]
    assert (await client.get(f"{url}/card.pdf", headers=world["printer2"])).status_code == 404
    assert (await client.post(f"{url}/done", json={"ok": True}, headers=world["printer2"])).status_code == 404
    assert (await client.get(f"{url}/card.pdf", headers=world["kiosk"])).status_code == 403
    assert (await client.get(f"{url}/card.pdf")).status_code == 401
    assert (await client.get(f"{url}/card.pdf", headers=world["printer"])).status_code == 200
    await client.post(f"{url}/done", json={"ok": True}, headers=world["printer"])
    # Printed: no second card from the same request.
    assert (await client.get(f"{url}/card.pdf", headers=world["printer"])).status_code == 409
    # And a printer of another event is no owner of it.
    other_owner_event = (await client.get(f"{API}/kiosk/printer/requests/{uuid.uuid4()}/card.pdf", headers=world["printer"]))
    assert other_owner_event.status_code == 404


async def test_the_oldest_card_goes_first_and_a_targeted_one_waits_for_its_printer(client, session, world):
    await _beat(session, world["printer_id"])
    await _beat(session, world["printer2_id"])
    h = world["club"]
    a = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id), "printer_id": world["printer2_id"]}, headers=h)).json()
    b = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][1].id)}, headers=h)).json()
    c = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][2].id)}, headers=h)).json()
    assert [a["position"], b["position"], c["position"]] == [1, 2, 3]
    first = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()
    # a is for the other printer: the desk printer takes b, and c is left.
    assert first["request"]["id"] == b["id"] and first["queued"] == 1
    mine = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer2"])).json()
    assert mine["request"]["id"] == a["id"] and mine["queued"] == 1


async def test_two_printers_claiming_at_once_get_different_cards(_engine, session):
    """SKIP LOCKED, for real: sessions on their own connections that commit."""
    before = await committed_keys(_engine)

    def make():
        s = async_sessionmaker(bind=_engine, **SESSION_KWARGS)()
        add_loader_criteria(s)
        return s

    now = datetime.now(timezone.utc)
    async with make() as s:
        owner = await _make_user(s, email="racer-ps@example.com", user_type=UserTypes.club, username="racerps")
        from app.api.clubs.models import Clubs

        club = Clubs(slug="racer-ps-club", name="Racer Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        cat = EventCategories(name="racer-ps", created_by_id=owner.id)
        s.add(cat)
        await s.flush()
        event = Events(slug="racer-ps-night", name="Racer Night", images=[], duration=2.0, event_datetime=now + timedelta(days=1), club_id=club.id, category_id=cat.id)
        s.add(event)
        await s.flush()
        printers = []
        for i in range(2):
            d = KioskDevices(id=uuid.uuid4(), role="printer", event_id=event.id, name=f"P{i}", door_label=f"P{i}", token_hash=f"racehash{i}" + uuid.uuid4().hex, token_prefix="abcdef", expires_at=now + timedelta(hours=4))
            s.add(d)
            printers.append(d.id)
        ids = []
        for i in range(6):
            guest = await _make_user(s, email=f"racer-guest{i}@example.com", username=f"racerguest{i}")
            reg = EventRegistrationsLink(id=uuid.uuid4(), event_id=event.id, user_id=guest.id, full_name=f"Guest {i}", email=f"racer{i}@example.com", ticket_id=f"RACE-{uuid.uuid4().hex[:8]}")
            s.add(reg)
            await s.flush()
            s.add(CardPrintRequests(id=uuid.uuid4(), event_id=event.id, registration_id=reg.id, source="dashboard", status="queued", created_at=now + timedelta(milliseconds=i)))
            ids.append(reg.id)
        await s.commit()
        event_id = event.id

    async def station(printer_id, n):
        got = []
        async with make() as s:
            device = await s.scalar(select(KioskDevices).where(KioskDevices.id == printer_id))
            for _ in range(n):
                res = await print_requests.claim(s, device)
                if res["request"]:
                    got.append(res["request"]["id"])
        return got

    try:
        a, b = await asyncio.gather(station(printers[0], 3), station(printers[1], 3))
        assert len(a) == 3 and len(b) == 3
        assert not set(a) & set(b) and len(set(a) | set(b)) == 6
        async with make() as s:
            rows = (await s.scalars(select(CardPrintRequests).where(CardPrintRequests.event_id == event_id))).all()
            assert {r.id for r in rows} == set(a) | set(b) and all(r.status == "printing" and r.attempts == 1 for r in rows)
    finally:
        await sweep(_engine, before)


async def test_a_lease_that_runs_out_goes_back_in_the_queue_and_fails_on_the_third(client, session, world):
    await _beat(session, world["printer_id"])
    asked = (await _ask(client, world, world["regs"][0])).json()
    for attempt in (1, 2, 3):
        got = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()
        assert got["request"]["id"] == asked["id"] and got["request"]["attempt"] == attempt
        # Nobody else gets it while the lease holds.
        assert (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer2"])).json() == {"request": None, "queued": 0}
        await session.execute(update(CardPrintRequests).values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()
    last = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()
    assert last == {"request": None, "queued": 0}
    final = (await client.get(f"{API}/kiosk/print-requests/{asked['id']}", headers=world["kiosk"])).json()
    assert final["status"] == "failed" and final["attempts"] == 3 and final["error"] == "The printer didn't confirm this card."
    # The guest can be asked for again: a failed request is not open.
    again = await _ask(client, world, world["regs"][0])
    assert again.status_code == 201 and again.json()["id"] != asked["id"]


async def test_a_late_confirmation_from_the_same_station_is_not_printed_twice(client, session, world):
    await _beat(session, world["printer_id"])
    asked = (await _ask(client, world, world["regs"][0])).json()
    await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])
    await session.execute(update(CardPrintRequests).values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await session.commit()
    # The lease lapses and the next claim by anyone puts it back...
    other = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer2"])).json()
    assert other["request"]["id"] == asked["id"] and other["request"]["attempt"] == 2
    # ...the first station's word is then not its to give.
    assert (await client.post(f"{API}/kiosk/printer/requests/{asked['id']}/done", json={"ok": True}, headers=world["printer"])).status_code == 404


async def test_retry_and_cancel(client, session, world):
    await _beat(session, world["printer_id"])
    h = world["club"]
    reg = world["regs"][0]
    q = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(reg.id)}, headers=h)).json()
    # A queued card cannot be retried; it can be cancelled.
    assert (await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/retry"), headers=h)).json()["error_code"] == "NOT_RETRYABLE"
    cancelled = await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/cancel"), headers=h)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/cancel"), headers=h)).json()["status"] == "cancelled"
    assert (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json() == {"request": None, "queued": 0}
    # Retry puts it back, attempts kept.
    back = await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/retry"), headers=h)
    assert back.status_code == 200 and back.json()["status"] == "queued" and back.json()["position"] == 1
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    # A printing card is cancelled too; the station's word after that changes nothing.
    assert (await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/cancel"), headers=h)).json()["status"] == "cancelled"
    assert (await client.post(f"{API}/kiosk/printer/requests/{claimed['id']}/done", json={"ok": True}, headers=world["printer"])).json()["status"] == "cancelled"
    assert (await client.get(f"{API}{claimed['card_url']}", headers=world["printer"])).status_code == 409
    # Fail, retry, print.
    again = await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/retry"), headers=h)
    assert again.json()["status"] == "queued"
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    assert claimed["attempt"] == 2
    await client.post(f"{API}/kiosk/printer/requests/{claimed['id']}/done", json={"ok": False, "error": "Jam"}, headers=world["printer"])
    assert (await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/cancel"), headers=h)).json()["error_code"] == "NOT_CANCELLABLE"
    third = await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/retry"), headers=h)
    assert third.json()["status"] == "queued" and third.json()["error"] is None and third.json()["attempts"] == 2
    # Not another event's, not a stranger's.
    assert (await client.post(_ev(world, f"/idcards/print-requests/{uuid.uuid4()}/retry"), headers=h)).status_code == 404
    assert (await client.post(_ev(world, f"/idcards/print-requests/{q['id']}/cancel"), headers=world["outsider"])).status_code in (401, 403)


async def test_a_retry_that_would_double_up_is_refused(client, session, world):
    await _beat(session, world["printer_id"])
    h = world["club"]
    reg = str(world["regs"][0].id)
    first = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": reg}, headers=h)).json()
    await client.post(_ev(world, f"/idcards/print-requests/{first['id']}/cancel"), headers=h)
    second = (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": reg}, headers=h)).json()
    assert second["id"] != first["id"]
    r = await client.post(_ev(world, f"/idcards/print-requests/{first['id']}/retry"), headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "ALREADY_QUEUED"


# --- the reprint rules ----------------------------------------------------------------------------------


async def test_reprint_rules_for_a_guest_at_the_kiosk_and_for_staff(client, session, world):
    await _beat(session, world["printer_id"])
    h = world["club"]
    reg = world["regs"][0]
    ask_staff = lambda **kw: client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(reg.id), **kw}, headers=h)  # noqa: E731

    await _set_policy(client, world, "never")
    token = await _admit(client, world, reg)
    assert (await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=world["kiosk"])).status_code == 201
    await _print_one(client, world)

    # Once printed: a guest at the kiosk is turned away in plain words, whatever the policy but "anyone".
    for policy in ("never", "organiser"):
        await _set_policy(client, world, policy)
        refused = await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=world["kiosk"])
        assert refused.status_code == 409 and refused.json()["error_code"] == "REPRINT_NOT_ALLOWED"
        assert refused.json()["message"] == "Your card was already printed. Ask at the desk."

    # Staff: never needs the override; organiser lets the organiser through.
    await _set_policy(client, world, "never")
    no = await ask_staff()
    assert no.status_code == 409 and no.json()["error_code"] == "REPRINT_NOT_ALLOWED" and "does not reprint" in no.json()["message"]
    await _set_policy(client, world, "organiser")
    yes = await ask_staff()
    assert yes.status_code == 201 and yes.json()["source"] == "dashboard"
    await client.post(_ev(world, f"/idcards/print-requests/{yes.json()['id']}/cancel"), headers=h)
    await _set_policy(client, world, "never")
    over = await ask_staff(override=True)
    assert over.status_code == 201
    # The override rides with the request: the station's card is drawn though the rule says never, and the log says so.
    await _print_one(client, world)
    jobs = (await session.scalars(select(PrintJobs).order_by(PrintJobs.created_at))).all()
    assert [j.source.value for j in jobs] == ["kiosk", "manual"] and [j.overrode for j in jobs] == [False, True]

    # "anyone": a guest may ask again.
    await _set_policy(client, world, "anyone")
    again = await client.post(f"{API}/kiosk/print-requests", json={"redemption": token}, headers=world["kiosk"])
    assert again.status_code == 201


async def test_a_card_printed_meanwhile_is_not_printed_again_under_a_one_card_rule(client, session, world):
    await _beat(session, world["printer_id"])
    reg = world["regs"][0]
    await _set_policy(client, world, "never")
    asked = (await _ask(client, world, reg)).json()
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    # The door printed it while this one was in the queue.
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.id == reg.id).values(badge_printed_at=datetime.now(timezone.utc)))
    await session.commit()
    pdf = await client.get(f"{API}{claimed['card_url']}", headers=world["printer"])
    assert pdf.status_code == 409 and pdf.json()["error_code"] == "REPRINT_NOT_ALLOWED"
    got = (await client.get(f"{API}/kiosk/print-requests/{asked['id']}", headers=world["kiosk"])).json()
    assert got["status"] == "failed" and "already printed" in got["error"]


# --- the test card ---------------------------------------------------------------------------------------------


async def test_the_test_card_records_nothing(client, session, world):
    regs_before = await session.scalar(select(func.count()).select_from(EventRegistrationsLink))
    pdf = await client.post(f"{API}/kiosk/printer/test", headers=world["printer"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and pdf.headers["content-type"] == "application/pdf"
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(PrintJobs)) == 0
    assert await session.scalar(select(func.count()).select_from(CardPrintRequests)) == 0
    assert await session.scalar(select(func.count()).select_from(EventRegistrationsLink)) == regs_before
    assert await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.badge_printed_at.is_not(None))) == 0
    assert (await client.post(f"{API}/kiosk/printer/test")).status_code == 401


# --- the dashboard and the door --------------------------------------------------------------------------------


async def test_the_dashboard_lists_printers_and_the_queue(client, session, world):
    h = world["club"]
    await _beat(session, world["printer_id"])
    await _beat(session, world["printer2_id"], seconds_ago=120)
    printers = (await client.get(_ev(world, "/idcards/printers"), headers=h)).json()
    assert [(p["name"], p["online"]) for p in printers] == [("Desk printer", True), ("Stage printer", False)]
    assert set(printers[0]) == {"id", "name", "door_label", "online", "last_seen_at", "expires_at"}
    for i in range(3):
        r = await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][i].id)}, headers=h)
        assert r.status_code == 201 and r.json()["position"] == i + 1
    dup = await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id)}, headers=h)
    assert dup.status_code == 200
    open_ = (await client.get(_ev(world, "/idcards/print-requests"), headers=h)).json()
    assert open_["printers_online"] == 1 and [i["name"] for i in open_["items"]] == ["Chitra Nair", "Bala Krishnan", "Ananya Iyer"]
    assert [i["position"] for i in open_["items"]] == [3, 2, 1]
    await _print_one(client, world)
    assert [i["name"] for i in (await client.get(_ev(world, "/idcards/print-requests?status=done"), headers=h)).json()["items"]] == ["Ananya Iyer"]
    assert len((await client.get(_ev(world, "/idcards/print-requests?status=open"), headers=h)).json()["items"]) == 2
    claimed = (await client.post(f"{API}/kiosk/printer/claim", headers=world["printer"])).json()["request"]
    await client.post(f"{API}/kiosk/printer/requests/{claimed['id']}/done", json={"ok": False, "error": "Jam"}, headers=world["printer"])
    failed = (await client.get(_ev(world, "/idcards/print-requests?status=failed"), headers=h)).json()["items"]
    assert [(i["name"], i["error"], i["attempts"]) for i in failed] == [("Bala Krishnan", "Jam", 1)]
    assert len((await client.get(_ev(world, "/idcards/print-requests?status=all&limit=2"), headers=h)).json()["items"]) == 2
    assert (await client.get(_ev(world, "/idcards/print-requests?status=bogus"), headers=h)).status_code == 422
    # Only this event's club.
    for call in (client.get(_ev(world, "/idcards/printers"), headers=world["outsider"]), client.get(_ev(world, "/idcards/print-requests"), headers=world["outsider"]), client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(world["regs"][0].id)}, headers=world["outsider"])):
        assert (await call).status_code in (401, 403)
    assert (await client.get(_ev(world, "/idcards/printers"))).status_code in (401, 403)
    # A guest who is not on this event, and one whose ticket was cancelled.
    assert (await client.post(_ev(world, "/idcards/print-requests"), json={"registration_id": str(uuid.uuid4())}, headers=h)).status_code == 404


async def test_the_door_console_queues_a_card_with_the_door_permission(client, session, world):
    await _beat(session, world["printer_id"])
    h = world["club"]
    reg = world["regs"][0]
    url = _ev(world, f"/gate/print-request/{reg.id}")
    # Signed-in organiser.
    r = await client.post(url, json={}, headers=h)
    assert r.status_code == 201 and r.json()["source"] == "door"
    assert (await client.post(url, json={}, headers=h)).status_code == 200
    # A stranger, and nobody.
    assert (await client.post(url, json={}, headers=world["outsider"])).status_code == 403
    assert (await client.post(url, json={})).status_code == 401
    # A door's gate link: the same permission as printing at the door today, but the reprint rule is the organiser's to open.
    door = await client.post(_ev(world, "/doors"), json={"name": "Gate A", "kind": "door"}, headers=h)
    key = (await client.post(_ev(world, f"/doors/{door.json()['id']}/link"), json={"can_walkup": False}, headers=h)).json()["key"]
    other = world["regs"][1]
    link = await client.post(_ev(world, f"/gate/print-request/{other.id}"), json={}, headers={"X-Gate-Key": key})
    assert link.status_code == 201 and link.json()["source"] == "door"
    await _print_one(client, world)
    await _print_one(client, world)
    await _set_policy(client, world, "never")
    refused = await client.post(_ev(world, f"/gate/print-request/{other.id}"), json={"override": True}, headers={"X-Gate-Key": key})
    assert refused.status_code == 409 and refused.json()["error_code"] == "REPRINT_NOT_ALLOWED"
    allowed = await client.post(_ev(world, f"/gate/print-request/{other.id}"), json={"override": True}, headers=h)
    assert allowed.status_code == 201
    # And a request for a guest of another event is not found.
    assert (await client.post(_ev(world, f"/gate/print-request/{uuid.uuid4()}"), json={}, headers=h)).status_code == 404
