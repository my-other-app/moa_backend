"""Closing an event: the report, the status clock, and retention, forward only."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.checkins import service as checkins
from app.api.checkins.models import CheckIns, CheckInVia
from app.api.closeout import service
from app.api.closeout.models import ArchivedContacts, EventStatus, RetentionJobs
from app.api.events.models import EventRegistrationsLink
from app.core import jobs
from app.core.email import email as email_mod
from tests.conftest import auth_header

FIELDS = [{"key": "tshirt", "label": "T-shirt size", "field_type": "select", "options": ["S", "M", "L"], "required": False}]


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    state = {"refuse": None}

    def send(**kwargs):
        if state["refuse"]:
            raise RuntimeError(state["refuse"])
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    monkeypatch.setattr(email_mod, "send_email_with_attachment", send)
    return {"sent": sent, "state": state}


@pytest.fixture
def store(monkeypatch):
    """Report PDFs go to object storage; here they go to a dict."""
    from app.core.storage import objects

    held: dict[str, bytes] = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, store):
    owner, club = await make_club(name="Yukthi", email="closeout@example.com")
    start = datetime.now(timezone.utc) - timedelta(hours=10)
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=start, duration=2.0, location_name="Innovation Hall", additional_details=FIELDS)
    a = await make_user(email="ananya-c@example.com")
    s = await make_user(email="sita-c@example.com")
    w = await make_user(email="guest-7@walkup.myotherapp.in")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", phone="9876543210", additional_details={"tshirt": "L"}, is_attended=True, attended_on=start + timedelta(minutes=5))
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")
    r_w = await make_registration(event=event, user=w, full_name="Walk-up Seven", email="guest-7@walkup.myotherapp.in", is_attended=True, attended_on=start + timedelta(minutes=30))
    session.add(CheckIns(event_id=event.id, registration_id=r_a.id, via=CheckInVia.gate, checked_in_at=start + timedelta(minutes=5), door_label="Gate A"))
    session.add(CheckIns(event_id=event.id, registration_id=r_w.id, via=CheckInVia.walkup, checked_in_at=start + timedelta(minutes=30), door_label="Gate A"))
    await session.commit()
    return {"event": event, "a": r_a, "s": r_s, "w": r_w, "club": auth_header(await mint_token(owner))}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/closeout{path}"


async def test_the_figures_become_a_report_that_is_kept_and_downloadable(client, world):
    h = world["club"]
    f = await client.get(_url(world, "/figures"), headers=h)
    assert f.status_code == 200, f.text
    body = f.json()
    assert body["registered"] == 3 and body["checked_in"] == 2 and body["turnout_pct"] == 67 and body["no_shows"] == 1
    assert {r["role"]: (r["registered"], r["checked_in"]) for r in body["by_role"]} == {"attendee": (2, 2), "speaker": (1, 0)}
    assert {v["via"]: v["count"] for v in body["by_via"]} == {"gate": 1, "walkup": 1}
    assert body["walk_ins"] == 1 and body["with_phone"] == 1 and body["org_name"] == "Yukthi"

    assert (await client.get(_url(world, "/reports"), headers=h)).json() == []
    made = await client.post(_url(world, "/reports"), headers=h)
    assert made.status_code == 201, made.text
    assert made.json()["filename"].startswith("build-day-") and "-closeout-" in made.json()["filename"] and made.json()["figures"]["registered"] == 3 and made.json()["delivered_to"] == []
    listed = await client.get(_url(world, "/reports"), headers=h)
    assert len(listed.json()) == 1 and listed.json()[0]["generated_by_name"]
    pdf = await client.get(_url(world, f"/reports/{made.json()['id']}.pdf"), headers=h)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and pdf.headers["content-disposition"].endswith('.pdf"')


async def test_the_status_clock_moves_the_event_and_closed_locks_the_door_and_the_settings(client, session, world):
    h = world["club"]
    event = world["event"]
    got = await client.get(_url(world, "/settings"), headers=h)
    assert got.status_code == 200 and got.json()["status"] == "published" and got.json()["status_clock"] is True
    # Every status, with its label; "cancelled" is reached only by cancelling (app/api/club_ops), never picked here.
    assert [s["key"] for s in got.json()["statuses"]] == ["draft", "published", "live", "closed", "archived", "cancelled"]

    # The event ended ten hours ago: one sweep takes it through live to closed.
    assert await service.advance_clock(session) == 1
    await session.refresh(event)
    assert event.lifecycle is EventStatus.closed
    assert checkins.door_open(event) is False
    with pytest.raises(Exception) as refused:
        service.assert_writable(event)
    assert "read-only" in str(refused.value.message)
    # Reopened by hand: that takes it off the clock.
    back = await client.patch(_url(world, "/settings"), json={"status": "live"}, headers=h)
    assert back.status_code == 200 and back.json()["status"] == "live" and back.json()["status_clock"] is False
    await session.refresh(event)
    assert checkins.door_open(event) is True
    assert await service.advance_clock(session) == 0
    # A draft refuses the door too.
    assert (await client.patch(_url(world, "/settings"), json={"status": "draft"}, headers=h)).json()["status"] == "draft"
    await session.refresh(event)
    assert checkins.door_open(event) is False
    # Published puts it back on the clock, and the next sweep closes it; a month on it is archived.
    assert (await client.patch(_url(world, "/settings"), json={"status": "published", "status_clock": True}, headers=h)).json()["status_clock"] is True
    assert await service.advance_clock(session) == 1
    await session.refresh(event)
    assert event.lifecycle is EventStatus.closed
    assert await service.advance_clock(session, now=datetime.now(timezone.utc) + timedelta(days=31)) == 1
    await session.refresh(event)
    assert event.lifecycle is EventStatus.archived
    # Archived events leave the public list, but a guest's own registrations still show them.
    public = await client.get("/api/v1/events/list", params={"is_ended": "true"}, headers=h)
    assert public.status_code == 200, public.text
    body = public.json()
    listed = body.get("items") if isinstance(body, dict) else body
    assert all(e["id"] != event.id for e in listed)


async def test_retention_sends_the_report_then_scrubs_and_never_runs_twice(client, session, world, outbox):
    h = world["club"]
    event = world["event"]
    saved = await client.patch(_url(world, "/settings"), json={"retention_mode": "delete", "retention_hours": 1, "report_recipients": ["Ops@example.com", "ops@example.com", ""]}, headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["report_recipients"] == ["ops@example.com"]
    assert (await client.patch(_url(world, "/settings"), json={"report_recipients": ["not an email"]}, headers=h)).status_code == 422

    st = await client.get(_url(world, "/retention"), headers=h)
    assert st.status_code == 200, st.text
    assert st.json()["state"] == "pending" and st.json()["mode"] == "delete" and st.json()["ended"] is True
    assert st.json()["preview"] == {"registrations": 3, "walkups": 1, "phones": 1, "answers": 1, "deliveries": 0, "message_deliveries": 0, "self_check_in_sessions": 0, "requests": 0}
    assert datetime.fromisoformat(st.json()["run_at"]) < datetime.now(timezone.utc)

    # The slug typed back confirms it; a refused delivery scrubs nothing.
    assert (await client.post(_url(world, "/retention/run"), json={"confirm_slug": "wrong"}, headers=h)).status_code == 422
    outbox["state"]["refuse"] = "SES down"
    failed = await client.post(_url(world, "/retention/run"), json={"confirm_slug": event.slug}, headers=h)
    assert failed.status_code == 409 and "Nothing was scrubbed" in failed.json()["message"]
    st = await client.get(_url(world, "/retention"), headers=h)
    assert st.json()["state"] == "failed" and st.json()["attempts"] == 1 and "SES down" in st.json()["last_error"]
    await session.refresh(world["a"])
    assert world["a"].phone == "9876543210"

    outbox["state"]["refuse"] = None
    ran = await client.post(_url(world, "/retention/run"), json={"confirm_slug": event.slug}, headers=h)
    assert ran.status_code == 200, ran.text
    assert ran.json()["state"] == "deleted" and ran.json()["deleted"]["contacts_archived"] == 3 and ran.json()["deleted"]["walkups"] == 1
    assert len(outbox["sent"]) == 1 and outbox["sent"][0]["recipients"] == "ops@example.com" and outbox["sent"][0]["attachment_filename"].endswith(".pdf")
    reports = (await client.get(_url(world, "/reports"), headers=h)).json()
    assert len(reports) == 2 and reports[0]["delivered_to"] == ["ops@example.com"]

    # Phones and answers are gone, the walk-up record is gone, the guests' own registrations stay; contacts were kept for the club.
    await session.refresh(world["a"])
    await session.refresh(world["w"])
    assert world["a"].phone is None and world["a"].additional_details is None and world["a"].is_deleted is False and world["a"].full_name == "Ananya Iyer"
    assert world["w"].is_deleted is True and world["w"].full_name == "Walk-up guest" and "removed-" in world["w"].email
    kept = list((await session.scalars(__import__("sqlalchemy").select(ArchivedContacts).where(ArchivedContacts.event_id == event.id))).all())
    assert {c.name: c.attended for c in kept} == {"Ananya Iyer": True, "Sita Raman": False, "Walk-up Seven": True}
    assert next(c for c in kept if c.name == "Ananya Iyer").phone == "9876543210"
    await session.refresh(event)
    assert event.lifecycle is EventStatus.archived
    st = await client.get(_url(world, "/retention"), headers=h)
    assert st.json()["state"] == "deleted" and st.json()["contacts_kept"] == 3 and st.json()["completed_at"]
    assert (await client.post(_url(world, "/retention/run"), json={"confirm_slug": event.slug}, headers=h)).status_code == 409


async def test_a_scheduled_run_waits_for_the_event_to_end(client, session, world, make_club, make_event, outbox):
    owner, club = await make_club(name="Later", email="later@example.com")
    future = await make_event(club=club, created_by=owner, name="Next Month", event_datetime=datetime.now(timezone.utc) + timedelta(days=20), duration=2.0, retention_mode="delete")
    r = await service.run_retention(session, future, manual=False)
    assert r == {"state": "pending", "refusal": "NOT_ENDED", "error": None, "deleted": None}
    job = await session.scalar(__import__("sqlalchemy").select(RetentionJobs).where(RetentionJobs.event_id == future.id))
    assert job.last_refusal == "NOT_ENDED" and job.refused_at is not None
    # The ended event on "delete" with no recipients: the sweep generates the report, keeps it, and scrubs.
    event = world["event"]
    await client.patch(_url(world, "/settings"), json={"retention_mode": "delete", "retention_hours": 1}, headers=world["club"])
    done = await service.sweep(session)
    # Queued as a job, run after the sweep's commit (the suite runs it when waited for).
    assert done["queued"] == 1
    await jobs.settle()
    assert outbox["sent"] == []
    reg = await session.get(EventRegistrationsLink, world["a"].id)
    await session.refresh(reg)
    assert reg.phone is None
    assert (await client.get(_url(world, "/retention"), headers=world["club"])).json()["state"] == "deleted"
