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


async def test_the_followers_export_as_a_workbook(client, world):
    import csv as csv_mod
    import io
    from datetime import date

    from tests.spreadsheet_helpers import assert_same_as_csv, records, workbook_of

    as_csv = await client.get("/api/v1/clubs/followers/export.csv", params={"segment": "never"}, headers=world["h"])
    head, *rows = list(csv_mod.reader(io.StringIO(as_csv.text.lstrip("\ufeff"))))
    r = await client.get("/api/v1/clubs/followers/export.csv", params={"segment": "never", "format": "xlsx"}, headers=world["h"])
    sheet = workbook_of(r, "followers-never.xlsx").active
    assert r.headers.get("cache-control") == as_csv.headers.get("cache-control")  # whatever the CSV gets
    assert [c.value for c in sheet[1]] == head and sheet.title == "Followers"
    assert_same_as_csv(sheet, as_csv.text)
    got = records(sheet)
    assert [g["name"] for g in got] == [row[0] for row in rows] and {g["name"] for g in got} == {"Sneha J", "Old Timer"}
    sneha = next(g for g in got if g["name"] == "Sneha J")
    # The day a date, the counts numbers, as the CSV writes them.
    assert sneha["followed_on"].date() == date.fromisoformat(next(row for row in rows if row[0] == "Sneha J")[3])
    assert (sneha["registrations"], sneha["came"]) == (1, 0)
    assert (await client.get("/api/v1/clubs/followers/export.csv", params={"format": "ods"}, headers=world["h"])).status_code == 422
    assert (await client.get("/api/v1/clubs/followers/export.csv", params={"segment": "vip", "format": "xlsx"}, headers=world["h"])).status_code == 422


async def test_a_page_of_followers_is_the_size_asked_and_the_next_one_follows_on(client, world):
    """limit=2 gives two followers and says 2 (it gave three and said 3, so
    the next page began with the last one again)."""
    seen = []
    for offset in (0, 2):
        page = (await client.get("/api/v1/clubs/followers/list", params={"limit": 2, "offset": offset}, headers=world["h"])).json()
        assert (page["limit"], page["offset"], page["total"], len(page["items"])) == (2, offset, 4, 2)
        seen += [r["user"]["full_name"] for r in page["items"]]
    assert seen == ["Sneha J", "Rohan Patil", "Ananya Iyer", "Old Timer"]
    last = (await client.get("/api/v1/clubs/followers/list", params={"limit": 2, "offset": 4}, headers=world["h"])).json()
    assert last["items"] == [] and last["total"] == 4


async def test_a_follow_can_be_deleted_and_made_again_more_than_once(client, session, world, make_user, mint_token):
    """One live follow per person and club, and any number of deleted ones:
    the key used to count deleted rows too, so the second delete of the same
    person's follow collided with the first."""
    from datetime import datetime, timezone

    from sqlalchemy import func, select, update

    club = world["club"]
    fan = await make_user(email="again-fan@example.com")
    h = auth_header(await mint_token(fan))
    for _ in range(3):
        assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=h)).status_code == 200
        await session.execute(
            update(ClubUsersLink)
            .where(ClubUsersLink.club_id == club.id, ClubUsersLink.user_id == fan.id, ClubUsersLink.is_deleted == False)  # noqa: E712
            .values(is_deleted=True, deleted_at=datetime.now(timezone.utc))
        )
        await session.commit()
    deleted = await session.scalar(
        select(func.count()).select_from(ClubUsersLink)
        .where(ClubUsersLink.club_id == club.id, ClubUsersLink.user_id == fan.id, ClubUsersLink.is_deleted == True)  # noqa: E712
        .execution_options(include_deleted=True)
    )
    assert deleted == 3
    # Still one live row at most: a second live follow is refused by the key.
    assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=h)).status_code == 200
    assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=h)).status_code == 409
