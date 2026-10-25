"""Events in the app: the categories, rating an event (and the club reading
the ratings), and the app's event list with its filters."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import EventCategories, EventInterestsLink
from app.api.interests.models import InterestCategory, Interests
from app.api.users.models import AdminRoles
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
API = "/api/v1/events"


# ─── Categories ───────────────────────────────────────────────────────────


async def test_an_admin_who_manages_content_adds_a_category_and_everyone_signed_in_lists_them(client, session, make_admin, make_user, mint_token):
    owner = await make_admin(email="cov-cat-owner@example.com", role=AdminRoles.owner)
    support = await make_admin(email="cov-cat-support@example.com", role=AdminRoles.support)
    person = await make_user(email="cov-cat-person@example.com")
    made = await client.post(f"{API}/categories/create", json={"name": "Hackathon", "icon": "💻", "icon_type": "emoji"}, headers=auth_header(await mint_token(owner)))
    assert made.status_code == 200, made.text
    assert made.json()["name"] == "Hackathon" and made.json()["icon"] == "💻" and made.json()["id"]
    row = await session.scalar(select(EventCategories).where(EventCategories.id == made.json()["id"]))
    assert row.created_by_id == owner.id

    # Moderation is not the catalogue; an app user is not an admin.
    assert (await client.post(f"{API}/categories/create", json={"name": "Nope"}, headers=auth_header(await mint_token(support)))).status_code == 403
    assert (await client.post(f"{API}/categories/create", json={"name": "Nope"}, headers=auth_header(await mint_token(person)))).status_code == 403
    assert (await client.post(f"{API}/categories/create", json={"icon": "x"}, headers=auth_header(await mint_token(owner)))).status_code == 422

    listed = await client.get(f"{API}/categories/list", headers=auth_header(await mint_token(person)))
    assert listed.status_code == 200 and "Hackathon" in [c["name"] for c in listed.json()]
    assert (await client.get(f"{API}/categories/list")).status_code == 401


# ─── Ratings ──────────────────────────────────────────────────────────────


@pytest.fixture
async def rated(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Rated Club", email="cov-rate@example.com")
    rival_owner, _ = await make_club(name="Rated Rival", email="cov-rate-rival@example.com")
    held = await make_event(club=club, created_by=owner, name="Held Night", event_datetime=NOW - timedelta(days=2), duration=3.0, lifecycle=EventStatus.closed)
    other = await make_event(club=club, created_by=owner, name="Other Night", event_datetime=NOW - timedelta(days=9), duration=2.0, lifecycle=EventStatus.closed)
    paid = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=NOW - timedelta(days=1), duration=2.0, has_fee=True, reg_fee=100, lifecycle=EventStatus.closed)
    soon = await make_event(club=club, created_by=owner, name="Soon Night", event_datetime=NOW + timedelta(days=1), duration=2.0)
    guests = [await make_user(email=f"cov-rate-{i}@example.com") for i in range(3)]
    for g in guests[:2]:
        await make_registration(event=held, user=g)
    await make_registration(event=other, user=guests[2])
    await make_registration(event=paid, user=guests[0], is_paid=False)
    await make_registration(event=soon, user=guests[0])
    await session.commit()
    h = [auth_header(await mint_token(g)) for g in guests]
    return dict(club=club, held=held, other=other, paid=paid, soon=soon, h=h, owner_h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)))


async def test_a_guest_rates_a_held_event_once_and_the_clubs_rating_follows(client, session, rated):
    held, h = rated["held"], rated["h"]
    first = await client.post(f"{API}/rate/{held.id}", json={"rating": 4, "review": "Great kits"}, headers=h[0])
    assert first.status_code == 200, first.text
    assert (first.json()["rating"], first.json()["review"], first.json()["event_id"]) == (4.0, "Great kits", held.id)
    await client.post(f"{API}/rate/{held.id}", json={"rating": 2}, headers=h[1])
    await client.post(f"{API}/rate/{rated['other'].id}", json={"rating": 3}, headers=h[2])
    # A second rating from the same guest replaces the first.
    again = await client.post(f"{API}/rate/{held.id}", json={"rating": 5, "review": "Even better"}, headers=h[0])
    assert again.json()["id"] == first.json()["id"] and again.json()["rating"] == 5.0

    club = await session.scalar(select(Clubs).where(Clubs.id == rated["club"].id).execution_options(populate_existing=True))
    assert (club.total_ratings, club.rating) == (3, pytest.approx((5 + 2 + 3) / 3))

    # The club reads them, newest first, with who wrote each; nobody else may.
    rows = (await client.get(f"{API}/ratings/{held.id}", headers=rated["owner_h"])).json()["items"]
    assert [(r["rating"], r["review"]) for r in rows] == [(2.0, None), (5.0, "Even better")]
    assert (await client.get(f"{API}/ratings/{held.id}", headers=rated["rival_h"])).status_code == 403


async def test_a_rating_needs_a_held_event_a_ticket_and_a_payment(client, rated):
    h = rated["h"]
    assert (await client.post(f"{API}/rate/{rated['soon'].id}", json={"rating": 4}, headers=h[0])).status_code == 400  # not over yet
    assert (await client.post(f"{API}/rate/{rated['other'].id}", json={"rating": 4}, headers=h[0])).status_code == 400  # not registered
    assert (await client.post(f"{API}/rate/{rated['paid'].id}", json={"rating": 4}, headers=h[0])).status_code == 400  # never paid
    for bad in (-1, 5.5):
        assert (await client.post(f"{API}/rate/{rated['held'].id}", json={"rating": bad}, headers=h[0])).status_code == 422
    assert (await client.post(f"{API}/rate/999999", json={"rating": 4}, headers=h[0])).status_code == 400  # validate_relations' answer
    assert (await client.post(f"{API}/rate/{rated['held'].id}", json={"rating": 4})).status_code == 401


async def test_a_rating_checks_the_raters_and_the_clubs_badges(client, rated, monkeypatch):
    from app.api.badges import service as badges

    seen = []

    async def users(session, user_id):
        seen.append(("user", user_id))

    async def clubs(session, club_id):
        seen.append(("club", club_id))

    monkeypatch.setattr(badges, "check_and_award_user_badges", users)
    monkeypatch.setattr(badges, "check_and_award_club_badges", clubs)
    r = await client.post(f"{API}/rate/{rated['held'].id}", json={"rating": 4}, headers=rated["h"][0])
    assert r.status_code == 200
    assert [k for k, _ in seen] == ["user", "club"] and seen[1][1] == rated["club"].id


# ─── The app's event list ─────────────────────────────────────────────────


@pytest.fixture
async def listing(session, make_club, make_event, make_user, make_registration, mint_token):
    topic = InterestCategory(name="Cov list topic")
    session.add(topic)
    await session.flush()
    robots = Interests(name="Cov list robots", category_id=topic.id)
    session.add(robots)
    await session.flush()
    a_owner, makers = await make_club(name="List Makers", email="cov-list-a@example.com")
    b_owner, band = await make_club(name="List Band", email="cov-list-b@example.com")
    c_owner, ghost = await make_club(name="List Ghost", email="cov-list-c@example.com", is_verified=False)
    me = await make_user(email="cov-list-me@example.com")
    session.add(ClubUsersLink(club_id=band.id, user_id=me.id, is_following=True))

    async def ev(club, owner, name, days, **kw):
        return await make_event(club=club, created_by=owner, name=name, event_datetime=NOW + timedelta(days=days), duration=2.0, **kw)

    build = await ev(makers, a_owner, "Robot Build", 2)
    session.add(EventInterestsLink(event_id=build.id, interest_id=robots.id))
    jam = await ev(band, b_owner, "Jam Night", 1)
    past = await ev(makers, a_owner, "Robot Past", -3, lifecycle=EventStatus.closed)
    old = await ev(makers, a_owner, "Robot Archive", -60, lifecycle=EventStatus.archived)
    await ev(makers, a_owner, "Robot Draft", 4, lifecycle=EventStatus.draft)
    await ev(ghost, c_owner, "Ghost Meetup", 3)
    reg = await make_registration(event=past, user=me, is_attended=True, is_paid=True)
    await make_registration(event=old, user=me)
    await session.commit()
    return dict(h=auth_header(await mint_token(me)), robots=robots, build=build, jam=jam, past=past, reg=reg)


async def _list(client, h, **params):
    r = await client.get(f"{API}/list", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_the_event_list_is_the_public_events_soonest_first(client, listing):
    h = listing["h"]
    assert [e["name"] for e in await _list(client, h, is_ended="false")] == ["Jam Night", "Robot Build"]
    assert [e["name"] for e in await _list(client, h, is_ended="true")] == ["Robot Past"]
    # Without a filter: every public one, earliest first; never a draft, an
    # archived event or an unlisted club's.
    assert [e["name"] for e in await _list(client, h)] == ["Robot Past", "Jam Night", "Robot Build"]
    assert [e["name"] for e in await _list(client, h, is_ended="false", limit=1, offset=1)] == ["Robot Build"]


async def test_the_event_list_filters_by_follows_interests_and_my_tickets(client, listing):
    h = listing["h"]
    assert [e["name"] for e in await _list(client, h, is_following="true")] == ["Jam Night"]
    assert [e["name"] for e in await _list(client, h, interest_ids=str(listing["robots"].id))] == ["Robot Build"]
    # My tickets: the archived event I went to still shows, with the ticket's state.
    mine = await _list(client, h, is_registered="true")
    assert [e["name"] for e in mine] == ["Robot Archive", "Robot Past"]
    past = next(e for e in mine if e["name"] == "Robot Past")
    assert past["ticket_id"] == listing["reg"].ticket_id and past["is_paid"] is True and past["is_attended"] is True
    assert past["ticket_revoked"] is False and past["ticket_url"]
    assert [e["name"] for e in await _list(client, h, is_registered="false", is_ended="false")] == ["Jam Night", "Robot Build"]
    # Someone else's list carries no ticket of mine.
    assert all(e["ticket_id"] is None for e in await _list(client, h, is_registered="false"))


async def test_the_event_list_refuses_what_it_cannot_read(client, listing):
    assert (await client.get(f"{API}/list", params={"interest_ids": "1,x"}, headers=listing["h"])).status_code == 422
    assert (await client.get(f"{API}/list")).status_code == 401
