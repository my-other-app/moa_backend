"""The event console's figures: the door as a shape, the live summary behind
the overview, the club's dashboard, its lifetime, and the platform's doors."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.analytics import service
from app.api.checkins.models import CheckIns, CheckInVia, DoorKind, Doors
from app.api.closeout import service as closeout
from app.api.closeout.models import EventStatus, RetentionJobs, RetentionMode, RetentionState
from app.api.deliveries.models import Deliveries, DeliveryChannel, DeliveryKind, DeliveryStatus
from app.api.events.volunteer.models import Volunteer
from app.api.idcards.models import PrintJobs, PrintSource
from tests.conftest import auth_header

# Started two hours ago and runs four: live, door open, arrivals under way.
START = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)


@pytest.fixture
def store(monkeypatch):
    from app.core.storage import objects

    held: dict[str, bytes] = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, make_admin, mint_token):
    owner, club = await make_club(name="Yukthi", email="console@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=START, duration=4.0, location_name="Innovation Hall", max_participants=50, lifecycle=EventStatus.live)
    gate = Doors(event_id=event.id, name="Gate A", kind=DoorKind.door, sort_order=0)
    session.add(gate)
    await session.flush()
    a = await make_user(email="ananya-k@example.com")
    b = await make_user(email="bala-k@example.com")
    c = await make_user(email="chitra-k@example.com")
    w = await make_user(email="guest-9@walkup.myotherapp.in")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", phone="9876543210", badge_printed_at=START)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", role="speaker")
    r_c = await make_registration(event=event, user=c, full_name="Chitra Nair", email_bounced_at=START)
    r_w = await make_registration(event=event, user=w, full_name="Walk-up Nine", email="guest-9@walkup.myotherapp.in")
    session.add(CheckIns(event_id=event.id, registration_id=r_a.id, via=CheckInVia.gate, door_id=gate.id, door_label="Gate A", checked_in_at=START + timedelta(minutes=5), checked_in_by=owner.id))
    session.add(CheckIns(event_id=event.id, registration_id=r_b.id, via=CheckInVia.kiosk, door_label="Kiosk", checked_in_at=START + timedelta(minutes=20)))
    session.add(CheckIns(event_id=event.id, registration_id=r_w.id, via=CheckInVia.walkup, door_id=gate.id, door_label="Gate A", checked_in_at=START + timedelta(minutes=35)))
    session.add(Deliveries(event_id=event.id, registration_id=r_a.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.sent, design_version=0))
    session.add(Deliveries(event_id=event.id, registration_id=r_c.id, kind=DeliveryKind.ticket, channel=DeliveryChannel.email, status=DeliveryStatus.failed, error="bounced"))
    session.add(PrintJobs(event_id=event.id, registration_id=r_a.id, source=PrintSource.checkin, cards=1))
    session.add(PrintJobs(event_id=event.id, registration_id=r_b.id, source=PrintSource.manual, cards=1, error="printer offline"))
    session.add(PrintJobs(event_id=event.id, source=PrintSource.test, cards=3))
    session.add(Volunteer(email="vani-k@example.com", event_id=event.id, is_approved=True))
    # A finished event from last month: the lifetime figures have something to average.
    past = await make_event(club=club, created_by=owner, name="Demo Day", event_datetime=START - timedelta(days=30), duration=2.0, lifecycle=EventStatus.closed)
    p1 = await make_user(email="p1-k@example.com")
    p2 = await make_user(email="p2-k@example.com")
    await make_registration(event=past, user=p1, full_name="Past One", is_attended=True, attended_on=START - timedelta(days=30))
    await make_registration(event=past, user=p2, full_name="Past Two")
    await session.commit()
    admin = await make_admin(email="admin-k@example.com")
    return {
        "event": event, "past": past, "club_row": club, "gate": gate, "a": r_a, "b": r_b, "c": r_c, "w": r_w, "owner": owner,
        "club": auth_header(await mint_token(owner)), "admin": auth_header(await mint_token(admin)), "guest": auth_header(await mint_token(a)),
    }


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}{path}"


def _at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).replace(second=0, microsecond=0).isoformat()


async def test_the_door_as_a_shape(client, world):
    r = await client.get(_url(world, "/figures"), headers=world["club"])
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["total_admitted"] == 3

    # Quarter hours run from the first admission to now, with no gaps, and the running total climbs through them.
    arrivals = f["arrivals"]
    assert arrivals[0]["starts_at"] == service._floor(START + timedelta(minutes=5)).isoformat()
    assert [b["admits"] for b in arrivals[:3]] == [1, 1, 0] or [b["admits"] for b in arrivals[:3]] == [1, 1, 1]
    assert arrivals[-1]["cumulative"] == 3
    starts = [datetime.fromisoformat(b["starts_at"]) for b in arrivals]
    assert all((n - p) == timedelta(minutes=15) for p, n in zip(starts, starts[1:]))
    assert len(arrivals) >= 8  # two hours of windows so far, give or take the clock

    doors = {d["name"]: d for d in f["by_door"]}
    assert doors["Gate A"]["admits"] == 2 and doors["Gate A"]["kind"] == "door" and doors["Gate A"]["share_pct"] == 66.7 and doors["Gate A"]["peak_admits"] == 1
    assert doors["Kiosk"]["admits"] == 1 and doors["Kiosk"]["door_id"] is None

    assert {t["role"]: (t["registered"], t["checked_in"]) for t in f["by_type"]} == {"attendee": (3, 2), "speaker": (1, 1)}
    assert f["deliveries"] == [{"channel": "email", "sent": 1, "failed": 1, "skipped": 0, "pending": 0}]
    assert {v["via"]: v["admits"] for v in f["via"]} == {"gate": 1, "kiosk": 1, "walkup": 1}
    assert f["cards"] == {"printed": 1, "failed": 1, "holders": 1}  # the test print never counts
    assert f["contacts"] == {"phone": 1, "email": 3, "bounced": 1}

    t = f["timing"]
    assert datetime.fromisoformat(t["first_admit_at"]) == START + timedelta(minutes=5) and datetime.fromisoformat(t["last_admit_at"]) == START + timedelta(minutes=35)
    assert t["first_admit_at"].endswith("+00:00")  # one zone on the wire, whatever the rows came back in
    assert t["peak_admits"] == 1 and t["peak_starts_at"] == arrivals[0]["starts_at"]
    # Half of three is reached on the second admission, at minute 20's window.
    assert t["half_house_at"] == service._floor(START + timedelta(minutes=20)).isoformat()
    assert 0 <= t["half_house_minutes"] <= 20


async def test_the_summary_behind_the_overview(client, world):
    r = await client.get(_url(world, "/summary"), headers=world["club"])
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["status"] == "live" and s["door_open"] is True and s["status_clock"] is True and s["capacity"] == 50
    assert s["registered"] == 4 and s["checked_in"] == 3 and s["from_report"] is False
    assert [c["name"] for c in s["recent"]] == ["Walk-up Nine", "Bala Krishnan", "Ananya Iyer"]
    assert s["recent"][1]["by"] == "Self check-in" and s["recent"][1]["door"] == "Kiosk" and s["recent"][2]["door"] == "Gate A" and s["recent"][2]["by"]
    assert {b["source"]: (b["registered"], b["checked_in"]) for b in s["by_source"]} == {"list": (3, 2), "walkup": (1, 1)}
    assert s["tickets"] == {"issued": 1, "missing": 3, "pending": 0, "older_design": 0, "revoked": 0, "design_version": 0}
    assert s["retention"] == {"mode": "keep", "state": None, "run_at": None}
    assert s["analytics"]["total_admitted"] == 3 and s["timezone"] == "Asia/Kolkata"


async def test_the_overview_reads_the_report_once_the_list_is_gone(client, session, world, store):
    event = world["event"]
    report = await closeout.generate(session, event, world["owner"])
    assert report.figures["analytics"]["total_admitted"] == 3 and report.figures["registered"] == 4
    # Retention ran: the figures are read back from the report, not recomputed.
    event.retention_mode = RetentionMode.delete
    session.add(RetentionJobs(event_id=event.id, state=RetentionState.deleted, run_at=START, report_id=report.id))
    await session.commit()
    s = (await client.get(_url(world, "/summary"), headers=world["club"])).json()
    assert s["from_report"] is True and s["analytics"] == report.figures["analytics"] and s["retention"]["state"] == "deleted"


async def test_who_may_read_the_figures(client, world):
    assert (await client.get(_url(world, "/summary"), headers=world["guest"])).status_code == 403
    assert (await client.get(_url(world, "/figures"), headers=world["admin"])).status_code == 200
    assert (await client.get("/api/v1/clubs/dashboard", headers=world["guest"])).status_code == 403


async def test_the_club_dashboard(client, world):
    r = await client.get("/api/v1/clubs/dashboard", headers=world["club"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stats"]["events"] == {"total": 2, "draft": 0, "published": 0, "live": 1, "closed": 1, "archived": 0}
    assert d["stats"]["on_file"] == 4 and d["stats"]["retention_due"] == 0 and d["stats"]["doors_open"] == 1
    kinds = {a["kind"]: a for a in d["attention"]}
    assert kinds["TICKETS_MISSING"]["event_id"] == world["event"].id and "3 on the list" in kinds["TICKETS_MISSING"]["body"]
    assert kinds["BOUNCED"]["title"].startswith("1 address bounced")
    assert "RETENTION_FAILED" not in kinds
    live = d["live"]
    assert len(live) == 1 and live[0]["name"] == "Build Day" and live[0]["registered"] == 4 and live[0]["checked_in"] == 3
    assert live[0]["gates"] == ["Gate A"] and live[0]["volunteers"] == 1 and live[0]["door_open"] is True


async def test_attention_sees_a_failed_retention_and_a_draft_that_has_started(client, session, world, make_event):
    event, past = world["event"], world["past"]
    past.retention_mode = RetentionMode.delete
    session.add(RetentionJobs(event_id=past.id, state=RetentionState.failed, run_at=START, last_error="SES refused the report."))
    late = await make_event(club=world["club_row"], created_by=world["owner"], name="Forgotten Draft", event_datetime=START - timedelta(days=1), duration=1.0, lifecycle=EventStatus.draft)
    await session.commit()
    d = (await client.get("/api/v1/clubs/dashboard", headers=world["club"])).json()
    assert d["stats"]["events"]["draft"] == 1 and d["stats"]["events"]["total"] == 3
    assert d["attention"][0]["kind"] == "RETENTION_FAILED" and d["attention"][0]["tone"] == "red" and "SES refused" in d["attention"][0]["body"] and d["attention"][0]["event_id"] == past.id
    kinds = {a["kind"]: a for a in d["attention"]}
    assert kinds["DRAFT_STARTED"]["event_id"] == late.id and kinds["NO_RECIPIENTS"]["event_id"] == past.id
    assert event.id == kinds["TICKETS_MISSING"]["event_id"]


async def test_lifetime_and_the_platform_doors_for_an_admin(client, world):
    club_id = world["club_row"].id
    assert (await client.get(f"/api/v1/clubs/admin/{club_id}/lifetime", headers=world["club"])).status_code == 403
    r = await client.get(f"/api/v1/clubs/admin/{club_id}/lifetime", headers=world["admin"])
    assert r.status_code == 200, r.text
    # Only the finished event counts as run; its turnout was one of two.
    assert r.json() == {"events_run": 1, "registered": 6, "guests_admitted": 4, "average_turnout_pct": 50, "reports_delivered": 0}
    assert (await client.get("/api/v1/clubs/admin/999999/lifetime", headers=world["admin"])).status_code == 404

    doors = await client.get("/api/v1/admin/doors", headers=world["admin"])
    assert doors.status_code == 200, doors.text
    body = doors.json()
    names = {e["name"]: e for e in body["live"]}
    assert names["Build Day"]["club"] == "Yukthi" and names["Build Day"]["gates"] == ["Gate A"]
    assert body["stats"]["events"]["live"] >= 1 and body["clubs_live"] >= 1
    assert (await client.get("/api/v1/admin/doors", headers=world["club"])).status_code == 403


async def test_the_status_clock_rides_along_on_lists_and_detail(client, world):
    rows = (await client.get("/api/v1/clubs/events/list", headers=world["club"])).json()["items"]
    assert {r["name"]: r["status"] for r in rows} == {"Build Day": "live", "Demo Day": "closed"}
    detail = (await client.get(_url(world).replace(f"/events/{world['event'].id}", f"/events/info/{world['event'].id}"), headers=world["club"])).json()
    assert detail["status"] == "live" and detail["status_clock"] is True and detail["retention_mode"] == "keep"
