"""The club's events list: what a row carries, the scopes and sorts, and the season figures above it."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.closeout.models import EventStatus
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Circuit Society", email="list@example.com")
    soon = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW + timedelta(days=2), duration=3.0, location_name="Lab 3", has_fee=True, reg_fee=100)
    soon.max_participants = 3
    soon.reg_enddate = NOW + timedelta(hours=20)
    later = await make_event(club=club, created_by=owner, name="Annual Summit", event_datetime=NOW + timedelta(days=20), duration=6.0)
    draft = await make_event(club=club, created_by=owner, name="Sketch", event_datetime=NOW + timedelta(days=3), duration=2.0, lifecycle=EventStatus.draft)
    done = await make_event(club=club, created_by=owner, name="Hack Night 03", event_datetime=NOW - timedelta(days=30), duration=3.0)
    users = [await make_user(email=f"u{i}-list@example.com") for i in range(4)]
    for u in users[:2]:
        await make_registration(event=soon, user=u, full_name=u.email, is_paid=True, paid_amount=100, actual_amount=100)
    r3 = await make_registration(event=done, user=users[2], full_name="Came")
    await make_registration(event=done, user=users[3], full_name="Did not")
    session.add(CheckIns(event_id=done.id, registration_id=r3.id, via=CheckInVia.gate, checked_in_at=NOW - timedelta(days=30), door_label="Gate A"))
    soon.page_views = 50
    await session.commit()
    return {"h": auth_header(await mint_token(owner)), "soon": soon, "later": later, "draft": draft, "done": done}


async def test_a_row_carries_what_the_organiser_checks_it_for(client, world):
    rows = (await client.get("/api/v1/clubs/events/list?scope=upcoming", headers=world["h"])).json()["items"]
    assert [r["name"] for r in rows] == ["Hack Night 04", "Annual Summit"]  # soonest first, the draft left out
    soon = rows[0]
    assert soon["registration_count"] == 2 and soon["max_participants"] == 3 and soon["registration_open"] is True
    assert soon["collected"] == 200.0 and soon["outstanding"] == 0.0 and soon["checked_in"] == 0 and soon["door_open"] is False  # two days out: the gate is not open yet
    assert soon["ends_at"]

    past = (await client.get("/api/v1/clubs/events/list?scope=past", headers=world["h"])).json()["items"]
    assert [r["name"] for r in past] == ["Hack Night 03"]
    assert past[0]["checked_in"] == 1 and past[0]["registration_open"] is False and past[0]["door_open"] is False

    drafts = (await client.get("/api/v1/clubs/events/list?scope=draft", headers=world["h"])).json()["items"]
    assert [r["name"] for r in drafts] == ["Sketch"] and drafts[0]["registration_open"] is False

    everything = (await client.get("/api/v1/clubs/events/list?scope=all&sort=registered", headers=world["h"])).json()
    assert everything["total"] == 4 and [r["name"] for r in everything["items"]][:2] == ["Hack Night 04", "Hack Night 03"]
    viewed = (await client.get("/api/v1/clubs/events/list?scope=all&sort=viewed", headers=world["h"])).json()["items"]
    assert viewed[0]["name"] == "Hack Night 04"
    assert (await client.get("/api/v1/clubs/events/list?scope=whatever", headers=world["h"])).status_code == 422

    # The old switch still answers.
    assert [r["name"] for r in (await client.get("/api/v1/clubs/events/list?is_ended=true", headers=world["h"])).json()["items"]] == ["Hack Night 03"]
    found = (await client.get("/api/v1/clubs/events/list?scope=all&search=summit", headers=world["h"])).json()["items"]
    assert [r["name"] for r in found] == ["Annual Summit"]


async def test_the_figures_above_the_list(client, session, world):
    f = (await client.get("/api/v1/clubs/events/figures", headers=world["h"])).json()
    assert f["upcoming"] == 2 and f["next_event"]["name"] == "Hack Night 04"
    assert f["registered"] == 2 and f["capacity"] == 3 and f["collected"] == 200.0
    assert f["past"] == 1 and f["drafts"] == 1
    a = f["attention"]
    assert [x["name"] for x in a["closing_soon"]] == ["Hack Night 04"]
    assert [x["name"] for x in a["unmarked"]] == []  # someone was checked in
    assert [x["name"] for x in a["stale_drafts"]] == ["Sketch"]

    # Full up: registration shuts and the closing-soon note goes with it.
    world["soon"].max_participants = 2
    await session.commit()
    f = (await client.get("/api/v1/clubs/events/figures", headers=world["h"])).json()
    assert f["attention"]["closing_soon"] == []
    row = next(r for r in (await client.get("/api/v1/clubs/events/list?scope=upcoming", headers=world["h"])).json()["items"] if r["name"] == "Hack Night 04")
    assert row["registration_open"] is False
