"""The app's older home feed and its global search (/home/feed/*,
app/api/home): what each list holds, what its filters do, and that nothing
the public may not see -- a draft, an archived event, a club that is not
listed -- reaches it."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink, ClubUsersLink, Notes
from app.api.events.models import EventInterestsLink
from app.api.interests.models import InterestCategory, Interests
from app.api.users.models import UserInterests
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
FEED = "/api/v1/home/feed"


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    topic = InterestCategory(name="Feed topic")
    session.add(topic)
    await session.flush()
    robots, music = Interests(name="Feed robots", category_id=topic.id), Interests(name="Feed music", category_id=topic.id)
    session.add_all([robots, music])
    await session.flush()

    me = await make_user(email="feed-me@example.com")
    session.add(UserInterests(user_id=me.id, interest_id=robots.id))

    a_owner, makers = await make_club(name="Feed Makers", email="feed-makers@example.com")
    b_owner, band = await make_club(name="Feed Band", email="feed-band@example.com")
    c_owner, ghost = await make_club(name="Feed Ghost", email="feed-ghost@example.com", is_verified=False)
    d_owner, other = await make_club(name="Feed Other", email="feed-other@example.com")
    makers.about = "We build robots on Saturdays"
    session.add_all([ClubInterestsLink(club_id=makers.id, interest_id=robots.id), ClubInterestsLink(club_id=ghost.id, interest_id=robots.id),
                     ClubInterestsLink(club_id=other.id, interest_id=music.id)])
    session.add(ClubUsersLink(club_id=band.id, user_id=me.id, is_following=True))

    async def ev(club, owner, name, start, interest=None, **kw):
        e = await make_event(club=club, created_by=owner, name=name, event_datetime=start, duration=2.0, **kw)
        if interest is not None:
            session.add(EventInterestsLink(event_id=e.id, interest_id=interest.id))
        await session.flush()
        return e

    build = await ev(makers, a_owner, "Robot Build", NOW + timedelta(days=2), robots, about="Bring a soldering iron")
    past = await ev(makers, a_owner, "Robot Past", NOW - timedelta(days=3), robots, lifecycle=EventStatus.closed)
    await ev(makers, a_owner, "Robot Draft", NOW + timedelta(days=4), robots, lifecycle=EventStatus.draft)
    await ev(makers, a_owner, "Robot Archive", NOW - timedelta(days=60), robots, lifecycle=EventStatus.archived)
    jam = await ev(band, b_owner, "Jam Night", NOW + timedelta(days=1))
    await ev(ghost, c_owner, "Robot Ghost", NOW + timedelta(days=1), robots)
    await ev(other, d_owner, "Music Elsewhere", NOW + timedelta(days=1), music)
    await make_registration(event=past, user=me)

    session.add_all([
        Notes(club_id=makers.id, user_id=a_owner.id, title="Robot kit list", note="Servos and a frame"),
        Notes(club_id=band.id, user_id=b_owner.id, title="Setlist", note="Robot rock first"),
        Notes(club_id=ghost.id, user_id=c_owner.id, title="Robot ghost note", note="Not public"),
        Notes(club_id=makers.id, user_id=a_owner.id, title="Robot old note", note="Gone", is_deleted=True),
    ])
    await session.commit()
    return dict(h=auth_header(await mint_token(me)), me=me.id, robots=robots, music=music, makers=makers, band=band, other=other, build=build, past=past, jam=jam)


async def _names(client, path, h, **params):
    r = await client.get(f"{FEED}/{path}", params=params, headers=h)
    assert r.status_code == 200, r.text
    return [x.get("name") or x.get("title") for x in r.json()["items"]]


async def test_the_event_feed_holds_my_interests_and_the_clubs_i_follow_and_nothing_private(client, world):
    h = world["h"]
    # Newest first; never a draft, an archived event or an unlisted club's.
    assert await _names(client, "events", h) == ["Jam Night", "Robot Past", "Robot Build"]
    assert await _names(client, "events", h, limit=1, offset=1) == ["Robot Past"]


async def test_the_event_feed_filters(client, world):
    h = world["h"]
    assert await _names(client, "events", h, is_ended="false") == ["Jam Night", "Robot Build"]
    assert await _names(client, "events", h, is_ended="true") == ["Robot Past"]
    assert await _names(client, "events", h, is_following="true") == ["Jam Night"]
    assert await _names(client, "events", h, is_registered="true") == ["Robot Past"]
    assert await _names(client, "events", h, is_registered="false") == ["Jam Night", "Robot Build"]
    row = (await client.get(f"{FEED}/events", params={"is_following": "true"}, headers=h)).json()["items"][0]
    assert row["club"]["id"] == world["band"].id and row["category"]["name"] == "General"


async def test_the_club_feed_holds_listed_clubs_that_share_my_interests(client, session, world):
    h = world["h"]
    assert await _names(client, "clubs", h) == ["Feed Makers"]
    assert await _names(client, "clubs", h, is_following="true") == []
    assert await _names(client, "clubs", h, is_following="false") == ["Feed Makers"]
    session.add(ClubUsersLink(club_id=world["makers"].id, user_id=world["me"], is_following=True))
    await session.commit()
    assert await _names(client, "clubs", h, is_following="true") == ["Feed Makers"]
    assert await _names(client, "clubs", h, is_following="false") == []


async def test_the_note_feed_is_newest_first_from_listed_clubs(client, world):
    h = world["h"]
    assert await _names(client, "notes", h) == ["Setlist", "Robot kit list"]
    assert await _names(client, "notes", h, is_following="true") == ["Setlist"]
    note = (await client.get(f"{FEED}/notes", headers=h)).json()["items"][0]
    assert note["club"]["name"] == "Feed Band" and note["note"] == "Robot rock first"


async def test_search_finds_events_clubs_and_notes_by_name_or_text(client, world):
    h = world["h"]
    found = (await client.get(f"{FEED}/search", params={"query": "ROBOT"}, headers=h)).json()
    # Case-blind, in names and in the text; never a draft, an archived event,
    # an unlisted club, its notes or a deleted note.
    assert sorted(e["name"] for e in found["events"]) == ["Robot Build", "Robot Past"]
    assert [c["name"] for c in found["clubs"]] == ["Feed Makers"]
    assert sorted(n["title"] for n in found["notes"]) == ["Robot kit list", "Setlist"]
    # Each list is one page: an ILIKE over every row's name and text, paged by offset.
    page = (await client.get(f"{FEED}/search", params={"query": "robot", "limit": 1, "offset": 1}, headers=h)).json()
    assert (len(page["events"]), len(page["clubs"]), len(page["notes"])) == (1, 0, 1)
    soldering = (await client.get(f"{FEED}/search", params={"query": "soldering"}, headers=h)).json()
    assert [e["name"] for e in soldering["events"]] == ["Robot Build"] and soldering["clubs"] == [] and soldering["notes"] == []


async def test_search_filters_by_interest_and_by_the_clubs_i_follow(client, world):
    h = world["h"]
    by_music = (await client.get(f"{FEED}/search", params={"query": "e", "interest_ids": str(world["music"].id)}, headers=h)).json()
    assert [e["name"] for e in by_music["events"]] == ["Music Elsewhere"] and [c["name"] for c in by_music["clubs"]] == ["Feed Other"]
    both = (await client.get(f"{FEED}/search", params={"query": "s", "interest_ids": f"{world['music'].id},{world['robots'].id}"}, headers=h)).json()
    assert sorted(e["name"] for e in both["events"]) == ["Music Elsewhere", "Robot Build", "Robot Past"]
    async def followed(query):
        return (await client.get(f"{FEED}/search", params={"query": query, "is_following": "true"}, headers=h)).json()

    assert [e["name"] for e in (await followed("t"))["events"]] == ["Jam Night"]  # not Robot Build or Robot Past
    assert [c["name"] for c in (await followed("feed"))["clubs"]] == ["Feed Band"]
    assert [n["title"] for n in (await followed("robot"))["notes"]] == ["Setlist"]  # not the kit list


async def test_search_refuses_what_it_cannot_read(client, world):
    h = world["h"]
    assert (await client.get(f"{FEED}/search", params={"query": ""}, headers=h)).status_code == 422
    assert (await client.get(f"{FEED}/search", headers=h)).status_code == 422
    # Not a list of numbers: a 422, not a 500.
    assert (await client.get(f"{FEED}/search", params={"query": "robot", "interest_ids": "1,two"}, headers=h)).status_code == 422


async def test_every_feed_needs_a_sign_in(client, world):
    for path in ("events", "clubs", "notes", "search?query=robot"):
        assert (await client.get(f"{FEED}/{path}")).status_code == 401, path
