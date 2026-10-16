"""The followers page: each follower with what they did with the club, the segments, and the audience above the list."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.clubs.models import ClubUsersLink
from app.api.links.models import TrackingLinks
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Circuit Society", email="fol@example.com")
    past1 = await make_event(club=club, created_by=owner, name="Hack Night 02", event_datetime=NOW - timedelta(days=120), duration=3.0)
    past2 = await make_event(club=club, created_by=owner, name="Hack Night 03", event_datetime=NOW - timedelta(days=40), duration=3.0)
    ahead = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW + timedelta(days=5), duration=3.0)
    link = TrackingLinks(club_id=club.id, name="Instagram bio", code="ig12345", utm_source="instagram", utm_medium="social", utm_campaign="bio")
    session.add(link)
    await session.flush()
    regular = await make_user(email="regular@example.com")
    regular.full_name = "Ananya Iyer"
    once = await make_user(email="once@example.com")
    once.full_name = "Rohan Patil"
    fresh = await make_user(email="fresh@example.com")
    fresh.full_name = "Sneha J"
    quiet = await make_user(email="quiet@example.com")
    quiet.full_name = "Old Timer"
    session.add_all([
        ClubUsersLink(club_id=club.id, user_id=regular.id, is_following=True, created_at=NOW - timedelta(days=200)),
        ClubUsersLink(club_id=club.id, user_id=once.id, is_following=True, created_at=NOW - timedelta(days=50), tracking_link_id=link.id),
        ClubUsersLink(club_id=club.id, user_id=fresh.id, is_following=True, created_at=NOW - timedelta(days=3)),
        ClubUsersLink(club_id=club.id, user_id=quiet.id, is_following=True, created_at=NOW - timedelta(days=300)),
    ])
    await make_registration(event=past1, user=regular, full_name="Ananya Iyer", is_attended=True)
    await make_registration(event=past2, user=regular, full_name="Ananya Iyer", is_attended=True)
    await make_registration(event=ahead, user=regular, full_name="Ananya Iyer")
    await make_registration(event=past2, user=once, full_name="Rohan Patil", is_attended=True)
    await make_registration(event=ahead, user=fresh, full_name="Sneha J")
    await session.commit()
    return {"h": auth_header(await mint_token(owner)), "club": club}


async def test_each_follower_carries_what_they_did_with_the_club(client, world):
    page = (await client.get("/api/v1/clubs/followers/list", headers=world["h"])).json()
    assert page["total"] == 4 and [r["user"]["full_name"] for r in page["items"]] == ["Sneha J", "Rohan Patil", "Ananya Iyer", "Old Timer"]  # newest first
    by = {r["user"]["full_name"]: r for r in page["items"]}
    a = by["Ananya Iyer"]
    assert a["registrations"] == 3 and a["attended"] == 2 and a["upcoming"] == 1 and "regulars" in a["segments"] and "new" not in a["segments"]
    assert by["Rohan Patil"]["source"] == "Instagram bio" and "once" in by["Rohan Patil"]["segments"]
    assert "new" in by["Sneha J"]["segments"] and "never" in by["Sneha J"]["segments"] and by["Sneha J"]["upcoming"] == 1
    assert "quiet" in by["Old Timer"]["segments"] and "never" in by["Old Timer"]["segments"]
    assert "quiet" not in by["Ananya Iyer"]["segments"]  # in for something ahead
    assert page["counts"] == {"all": 4, "new": 1, "regulars": 1, "once": 1, "never": 2, "quiet": 1}

    active = (await client.get("/api/v1/clubs/followers/list?sort=active", headers=world["h"])).json()["items"]
    assert [r["user"]["full_name"] for r in active][:2] == ["Ananya Iyer", "Rohan Patil"]
    regs = (await client.get("/api/v1/clubs/followers/list?segment=regulars", headers=world["h"])).json()
    assert regs["total"] == 1 and regs["items"][0]["user"]["full_name"] == "Ananya Iyer"
    found = (await client.get("/api/v1/clubs/followers/list?search=roh", headers=world["h"])).json()["items"]
    assert [r["user"]["full_name"] for r in found] == ["Rohan Patil"]
    assert (await client.get("/api/v1/clubs/followers/list?segment=vip", headers=world["h"])).status_code == 422


async def test_the_audience_above_the_list(client, world):
    i = (await client.get("/api/v1/clubs/followers/insights", headers=world["h"])).json()
    assert i["total"] == 4 and i["new"] == 1 and i["came"] == 2 and i["regulars"] == 1 and i["registered_ahead"] == 2 and i["quiet"] == 1
    assert i["next_event"]["name"] == "Hack Night 04"
    assert len(i["growth"]) == 12 and i["growth"][-1]["total"] == 4 and i["growth"][-1]["joined"] >= 1
    assert i["sources"][0]["name"] == "On the app" and {s["name"] for s in i["sources"]} == {"On the app", "Instagram bio"}
    assert i["by_org"][0] == {"name": "Not given", "count": 4}
    assert i["segments"]["all"] == 4

    csv = await client.get("/api/v1/clubs/followers/export.csv?segment=never", headers=world["h"])
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("name,username,organisation") and len(lines) == 3
