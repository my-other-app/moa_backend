"""Clubs for the app: notes (short posts on the club page), the club list
with its filters, one club's details, and the team on the club's website."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink, ClubSocials, ClubUsersLink
from app.api.events.models import EventRatingsLink
from app.api.interests.models import InterestCategory, Interests
from app.api.orgs.models import Organizations, OrgTypes
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
API = "/api/v1/clubs"


@pytest.fixture
async def world(session, make_club, make_user, mint_token):
    owner, club = await make_club(name="Note Makers", email="cov-clubs@example.com")
    rival_owner, rival = await make_club(name="Note Rivals", email="cov-clubs-rival@example.com")
    pending_owner, pending = await make_club(name="Note Pending", email="cov-clubs-pending@example.com", is_verified=False)
    fan = await make_user(email="cov-clubs-fan@example.com")
    await session.commit()
    return dict(
        owner=owner, club=club, rival=rival, pending=pending, fan=fan,
        h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
        pending_h=auth_header(await mint_token(pending_owner)), fan_h=auth_header(await mint_token(fan)),
    )


# ─── Notes ────────────────────────────────────────────────────────────────


async def _note(client, h, title, note="..."):
    r = await client.post(f"{API}/notes/create", json={"title": title, "note": note}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_club_posts_notes_and_anyone_reads_them_newest_first_a_page_at_a_time(client, world):
    h, club = world["h"], world["club"]
    first = await _note(client, h, "Kit list", "Servos, a frame and a battery")
    assert set(first) >= {"id", "title", "note"} and first["title"] == "Kit list" and first["note"] == "Servos, a frame and a battery"
    await _note(client, h, "Venue moved")
    await _note(client, h, "Results are up")
    await _note(client, world["rival_h"], "Their note")

    public = (await client.get(f"{API}/notes/list/{club.id}", headers=world["fan_h"])).json()["items"]
    assert [n["title"] for n in public] == ["Results are up", "Venue moved", "Kit list"]
    assert public[0]["created_at"] and public[0]["updated_at"]
    page = (await client.get(f"{API}/notes/list/{club.id}", params={"limit": 2, "offset": 2}, headers=world["fan_h"])).json()["items"]
    assert [n["title"] for n in page] == ["Kit list"]
    # The app's list: signed in.
    assert (await client.get(f"{API}/notes/list/{club.id}")).status_code == 401

    # The club's own list is its own, a page at a time like the public one.
    mine = (await client.get(f"{API}/notes/list", params={"limit": 1}, headers=h)).json()["items"]
    assert [n["title"] for n in mine] == ["Results are up"]
    assert [n["title"] for n in (await client.get(f"{API}/notes/list", params={"limit": 2, "offset": 1}, headers=h)).json()["items"]] == ["Venue moved", "Kit list"]
    assert [n["title"] for n in (await client.get(f"{API}/notes/list", headers=world["rival_h"])).json()["items"]] == ["Their note"]


async def test_only_a_club_posts_a_note_and_a_note_needs_its_words(client, session, world):
    assert (await client.post(f"{API}/notes/create", json={"title": "Fan mail", "note": "hi"}, headers=world["fan_h"])).status_code == 403
    assert (await client.post(f"{API}/notes/create", json={"title": "Signed out", "note": "hi"})).status_code == 401
    assert (await client.post(f"{API}/notes/create", json={"note": "no title"}, headers=world["h"])).status_code == 422
    assert (await client.post(f"{API}/notes/create", json={"title": "no words"}, headers=world["h"])).status_code == 422
    assert (await client.get(f"{API}/notes/list/999999", headers=world["fan_h"])).status_code == 404
    world["rival"].is_deleted = True
    await session.commit()
    assert (await client.get(f"{API}/notes/list/{world['rival'].id}", headers=world["fan_h"])).status_code == 404


# ─── The club list ────────────────────────────────────────────────────────


@pytest.fixture
async def listing(session, world, make_club):
    topic = InterestCategory(name="Cov clubs topic")
    session.add(topic)
    await session.flush()
    robots, music = Interests(name="Cov clubs robots", category_id=topic.id), Interests(name="Cov clubs music", category_id=topic.id)
    session.add_all([robots, music])
    org = Organizations(name="COEP Pune", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    _, band = await make_club(name="Note Band", email="cov-clubs-band@example.com")
    band.org_id = org.id
    session.add_all([ClubInterestsLink(club_id=world["club"].id, interest_id=robots.id), ClubInterestsLink(club_id=band.id, interest_id=music.id)])
    fan = world["fan"].id
    session.add_all([
        ClubUsersLink(club_id=world["club"].id, user_id=fan, is_following=True, is_pinned=True),
        ClubUsersLink(club_id=world["rival"].id, user_id=fan, is_following=False, is_hidden=True),
    ])
    await session.commit()
    return dict(robots=robots, music=music, org=org, band=band)


async def _club_names(client, h, **params):
    r = await client.get(f"{API}/list", params=params, headers=h)
    assert r.status_code == 200, r.text
    return [c["name"] for c in r.json()["items"]]


async def test_the_club_list_shows_listed_clubs_newest_first_with_what_i_did_with_each(client, world, listing):
    h = world["fan_h"]
    body = (await client.get(f"{API}/list", headers=h)).json()
    rows = {c["name"]: c for c in body["items"]}
    assert [c["name"] for c in body["items"]] == ["Note Band", "Note Rivals", "Note Makers"]  # never the pending club
    assert body["total"] == 3
    assert rows["Note Makers"]["is_following"] is True and rows["Note Makers"]["followers_count"] == 1
    assert rows["Note Rivals"]["is_following"] is False and rows["Note Rivals"]["followers_count"] == 0
    assert [i["name"] for i in rows["Note Makers"]["interests"]] == ["Cov clubs robots"] and rows["Note Makers"]["topics"][0]["name"] == "Cov clubs topic"
    assert (await client.get(f"{API}/list")).status_code == 401  # the app's list: signed in
    assert (await client.get(f"{API}/list", params={"limit": 1, "offset": 1}, headers=h)).json()["items"][0]["name"] == "Note Rivals"


async def test_the_club_list_filters(client, world, listing):
    h = world["fan_h"]
    assert await _club_names(client, h, is_following="true") == ["Note Makers"]
    assert await _club_names(client, h, is_following="false") == ["Note Band", "Note Rivals"]
    assert await _club_names(client, h, is_pinned="true") == ["Note Makers"]
    assert await _club_names(client, h, is_hidden="true") == ["Note Rivals"]
    assert await _club_names(client, h, interest_ids=str(listing["music"].id)) == ["Note Band"]
    assert await _club_names(client, h, interest_ids=f"{listing['music'].id},{listing['robots'].id}") == ["Note Band", "Note Makers"]
    assert await _club_names(client, h, org_id=listing["org"].id) == ["Note Band"]
    assert await _club_names(client, h, search="band") == ["Note Band"]
    # Not a list of numbers: a 422, not a 500.
    assert (await client.get(f"{API}/list", params={"interest_ids": "1,x"}, headers=h)).status_code == 422


async def test_following_pinning_and_hiding_change_what_the_list_says(client, world, listing):
    h, rival = world["fan_h"], world["rival"]
    followed = await client.post(f"{API}/follow/{rival.id}", headers=h)
    assert followed.status_code == 200 and followed.json()["is_following"] is True
    assert await _club_names(client, h, is_following="true") == ["Note Rivals", "Note Makers"]
    pinned = (await client.post(f"{API}/pin/{rival.id}", headers=h)).json()
    assert pinned["is_pinned"] is True
    assert await _club_names(client, h, is_pinned="true") == ["Note Rivals", "Note Makers"]
    assert (await client.post(f"{API}/hide/{rival.id}", headers=h)).json()["is_hidden"] is False  # it was hidden: the tap shows it again
    assert await _club_names(client, h, is_hidden="true") == []
    assert (await client.post(f"{API}/unfollow/{rival.id}", headers=h)).json()["is_following"] is False
    assert await _club_names(client, h, is_following="true") == ["Note Makers"]


# ─── One club ─────────────────────────────────────────────────────────────


async def test_a_clubs_details_carry_its_counts_rating_links_and_my_standing(client, session, world, listing, make_event, make_user):
    club, owner = world["club"], world["owner"]
    past = await make_event(club=club, created_by=owner, name="Done", event_datetime=NOW - timedelta(days=5), lifecycle=EventStatus.closed)
    await make_event(club=club, created_by=owner, name="Soon", event_datetime=NOW + timedelta(days=5))
    raters = [await make_user(email=f"cov-clubs-rater{i}@example.com") for i in range(2)]
    session.add_all([EventRatingsLink(event_id=past.id, user_id=raters[0].id, rating=4.0), EventRatingsLink(event_id=past.id, user_id=raters[1].id, rating=5.0)])
    session.add(ClubSocials(club_id=club.id, instagram="https://instagram.com/notemakers"))
    club.about = "We make notes about robots"
    await session.commit()

    r = await client.get(f"{API}/info/{club.id}", headers=world["fan_h"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["id"], d["name"], d["about"]) == (club.id, "Note Makers", "We make notes about robots")
    assert (d["total_events"], d["live_events"], d["past_events"]) == (2, 1, 1)
    assert (d["rating"], d["total_ratings"], d["followers_count"]) == (4.5, 2, 1)
    assert d["socials"]["instagram"] == "https://instagram.com/notemakers" and d["org"] is None
    assert d["user_data"] == {"is_following": True, "is_pinned": True, "is_hidden": False, "club_updates_whatsapp": False}
    assert [i["name"] for i in d["interests"]] == ["Cov clubs robots"]
    assert (await client.get(f"{API}/info/{club.id}")).status_code == 401
    band = (await client.get(f"{API}/info/{listing['band'].id}", headers=world["fan_h"])).json()
    assert band["org"]["name"] == "COEP Pune" and band["org"]["type"] == "college"
    assert band["user_data"] == {"is_following": False, "is_pinned": False, "is_hidden": False, "club_updates_whatsapp": False}


async def test_a_pending_club_is_hidden_and_the_own_page_is_a_clubs(client, world):
    pending = world["pending"]
    assert (await client.get(f"{API}/info/{pending.id}", headers=world["fan_h"])).status_code == 404
    assert (await client.get(f"{API}/info/999999", headers=world["fan_h"])).status_code == 404
    # A pending club's login is turned away until it is approved; a person has no club page of their own.
    assert (await client.get(f"{API}/info", headers=world["pending_h"])).status_code == 403
    assert (await client.get(f"{API}/info", headers=world["fan_h"])).status_code == 403
    own = await client.get(f"{API}/info", headers=world["h"])
    assert own.status_code == 200 and own.json()["id"] == world["club"].id and own.json()["user_data"] is not None


async def test_a_draft_is_counted_on_the_clubs_own_page_only(client, world, make_event):
    club, owner = world["club"], world["owner"]
    await make_event(club=club, created_by=owner, name="Secret", event_datetime=NOW + timedelta(days=9), lifecycle=EventStatus.draft)
    d = (await client.get(f"{API}/info/{club.id}", headers=world["fan_h"])).json()
    assert (d["total_events"], d["live_events"]) == (0, 0)
    own = (await client.get(f"{API}/info", headers=world["h"])).json()
    assert (own["total_events"], own["live_events"]) == (1, 1)


# ─── The team on the club's website ───────────────────────────────────────


async def test_the_team_appears_on_the_clubs_website_and_leaves_with_its_member(client, world, fake_s3):
    h = world["h"]
    assert (await client.get(f"{API}/site", headers=h)).json()["available"]["team"] is False
    lead = (await client.post(f"{API}/team", json={"name": "Asha Rao", "role": "President"}, headers=h)).json()
    await client.post(f"{API}/team", json={"name": "Vikram Shah", "role": "Treasurer"}, headers=h)
    await client.post(f"{API}/team", json={"name": "Their Member", "role": "Lead"}, headers=world["rival_h"])
    assert (await client.get(f"{API}/site", headers=h)).json()["available"]["team"] is True
    page = (await client.post(f"{API}/site/preview", json={}, headers=h)).json()["html"]
    assert "Asha Rao" in page and "President" in page and "Vikram Shah" in page and "Their Member" not in page
    assert (await client.delete(f"{API}/team/{lead['id']}", headers=h)).json() == {"deleted": True}
    page = (await client.post(f"{API}/site/preview", json={}, headers=h)).json()["html"]
    assert "Asha Rao" not in page and "Vikram Shah" in page
