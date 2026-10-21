"""Near results hold only what is near.

- Find events with a point and a radius lists in-person events whose point
  is inside the radius, and nothing else: an online event is near no one,
  and an in-person one with no point cannot be said to be inside. They used
  to pass every radius and pad the list ("6 events near Thiruvananthapuram"
  was 4 there and 2 online; Mumbai's "2 events near" were both online).
  type=online still lists the online ones, wherever the visitor is. The
  calendar, and a search with words through Meilisearch, keep the same rule.
- The front page's `clubs`, for a visitor with a place, are the clubs within
  NEAR_KM (50 km, as "Events near you"), nearest first. The nearest beyond
  that come in `clubs_more`, each with its distance. With none near, `clubs`
  is the most followed, with no distance, as for a visitor with no place.
  The app in the stores reads only `clubs` and calls them near when any has
  a distance, so no club 126 to 412 km away is ever called near again.
"""
from datetime import timedelta

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubUsersLink
from app.api.discover import service
from app.api.events.models import EventCategories
from app.api.search import engine, sync
from app.core.rate_limit import limiter
from app.db.listeners import add_loader_criteria
from tests import fake_meili

API = "/api/v1/discover"
TVM = (8.5241, 76.9366)            # Thiruvananthapuram, the visitor
VETTUCAUD = (8.4931, 76.9037)      # 5.0 km
KAZHAKKOOTTAM = (8.5686, 76.8731)  # 8.6 km
KOTTAYAM = (9.5826, 76.5214)       # 126 km
KOCHI = (9.9589, 76.2961)          # 174 km
KANNUR = (11.8848, 75.3577)        # 412 km
MUMBAI = (19.0760, 72.8777)        # nothing within 1,000 km


@pytest.fixture(autouse=True)
def _as_production(session, monkeypatch):
    add_loader_criteria(session)
    monkeypatch.setattr(limiter, "enabled", False)


def _ids(items):
    return [x["id"] for x in items]


@pytest.fixture
async def world(session, make_club, make_event, make_user):
    now = service.now_utc()
    first = (now + timedelta(days=1)).astimezone(service.IST).date()
    talk = EventCategories(name="Talk")
    session.add(talk)
    await session.flush()

    clubs = {}
    for key, name, point, followers in [
        ("tide", "Tide Coders", VETTUCAUD, 1), ("verandah", "Verandah Founders", KAZHAKKOOTTAM, 2),
        ("ponnila", "Ponnila Players", KOTTAYAM, 3), ("borrow", "Borrow Checkers", KOCHI, 4),
        ("kandal", "Kandal Walkers", KANNUR, 5), ("nowhere", "Nowhere Readers", None, 6),
    ]:
        owner, club = await make_club(name=name, email=f"near-{key}@example.com")
        if point:
            club.lat, club.lng = point
        clubs[key] = (owner, club)
        for i in range(followers):
            fan = await make_user(email=f"near-{key}-fan{i}@example.com")
            session.add(ClubUsersLink(club_id=club.id, user_id=fan.id, is_following=True))
    await session.flush()

    async def ev(key, name, day, **kw):
        owner, club = clubs[key]
        # 10:00 in India on the day, two hours: on one calendar day only.
        start = service.day_start(first + timedelta(days=day)) + timedelta(hours=10)
        return await make_event(club=club, created_by=owner, name=name, event_datetime=start, duration=2.0, category_id=talk.id,
                                lifecycle=EventStatus.published, **kw)

    e = dict(
        tide=await ev("tide", "Robotics by the sea", 0, location_name="Vettucaud", lat=VETTUCAUD[0], lng=VETTUCAUD[1]),
        online=await ev("borrow", "Robotics online qualifiers", 0, is_online=True, location_name="Online"),
        verandah=await ev("verandah", "Robotics for founders", 1, location_name="Kazhakkoottam", lat=KAZHAKKOOTTAM[0], lng=KAZHAKKOOTTAM[1]),
        kochi=await ev("borrow", "Robotics weekend", 1, location_name="Panampilly Nagar", lat=KOCHI[0], lng=KOCHI[1]),
        nopoint=await ev("ponnila", "Robotics night", 2, location_name="Seminar Hall"),
    )
    await session.commit()
    return dict(now=now, clubs={k: c for k, (_, c) in clubs.items()}, e=e)


async def _events(client, **params):
    r = await client.get(f"{API}/events", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ─── Find events ──────────────────────────────────────────────────────────


async def test_a_radius_lists_only_in_person_events_inside_it(client, world):
    e = world["e"]
    here = {"lat": TVM[0], "lng": TVM[1]}
    near = await _events(client, **here)  # the default radius, 50 km
    assert _ids(near["items"]) == [e["tide"].id, e["verandah"].id] and near["total"] == 2
    assert all(not x["is_online"] and x["distance_km"] <= 50 for x in near["items"])

    by_distance = await _events(client, **here, sort="distance")
    assert _ids(by_distance["items"]) == [e["tide"].id, e["verandah"].id] and by_distance["total"] == 2
    wide = await _events(client, **here, radius_km=200, sort="distance")
    assert _ids(wide["items"]) == [e["tide"].id, e["verandah"].id, e["kochi"].id] and wide["total"] == 3
    assert (await _events(client, **here, type="in_person"))["total"] == 2
    # From Mumbai nothing is near: no online event stands in for one.
    assert (await _events(client, lat=MUMBAI[0], lng=MUMBAI[1]))["items"] == []


async def test_online_events_are_listed_when_asked_for_and_everywhere(client, world):
    e = world["e"]
    here = {"lat": TVM[0], "lng": TVM[1]}
    assert _ids((await _events(client, **here, type="online"))["items"]) == [e["online"].id]
    assert _ids((await _events(client, lat=MUMBAI[0], lng=MUMBAI[1], type="online"))["items"]) == [e["online"].id]
    # Anywhere (radius 0): every event, the placed ones nearest first, then the rest by date.
    anywhere = await _events(client, **here, radius_km=0, sort="distance")
    assert _ids(anywhere["items"]) == [e[k].id for k in ("tide", "verandah", "kochi", "online", "nopoint")]
    assert (await _events(client))["total"] == 5  # no place at all


async def test_the_calendar_counts_what_the_list_shows(client, world):
    here = {"lat": TVM[0], "lng": TVM[1]}
    first = (world["now"] + timedelta(days=1)).astimezone(service.IST).date()
    months = sorted({(first + timedelta(days=d)).strftime("%Y-%m") for d in range(3)})

    async def counted(**params):
        total = 0
        for month in months:
            r = await client.get(f"{API}/calendar", params={"month": month, **params})
            assert r.status_code == 200, r.text
            total += sum(r.json()["days"].values())
        return total

    assert await counted(**here) == 2
    assert await counted(**here, type="online") == 1
    assert await counted(**here, radius_km=0) == 5


async def test_a_word_search_through_meilisearch_keeps_the_rule(client, session, world, monkeypatch):
    e = world["e"]
    fake = await fake_meili.install(monkeypatch)
    try:
        await sync.ensure_built(session)
        await sync.drain(session)
        engine.reset()
        before = len(fake.searches())
        here = {"lat": TVM[0], "lng": TVM[1], "q": "robotics", "sort": "date"}
        near = await _events(client, **here)
        assert len(fake.searches()) > before  # Meilisearch answered
        assert _ids(near["items"]) == [e["tide"].id, e["verandah"].id] and near["total"] == 2
        assert _ids((await _events(client, **here, type="online"))["items"]) == [e["online"].id]
    finally:
        from app.core import search

        await search.reset()
        engine.reset()
        sync.reset()


def test_the_meilisearch_filter_leaves_online_and_unplaced_events_out_of_a_radius():
    now = service.now_utc()
    common = dict(when="any", days=None, category=None, price="any", topic_id=None, interest=None, here=TVM)
    f = " AND ".join(engine._event_filter(now, type_="any", radius_km=50, **common))
    assert "is_online = false" in f and "has_point = true" in f and "_geoRadius(" in f
    assert "is_online = true" not in f and "has_point = false" not in f
    online = " AND ".join(engine._event_filter(now, type_="online", radius_km=50, **common))
    assert "is_online = true" in online and "_geoRadius(" not in online


# ─── The front page's clubs ───────────────────────────────────────────────


def _called_near_by_old_clients(clubs: list[dict]) -> list[dict]:
    """What the app in the stores (and the site before this change) called
    "Clubs near you": all of `clubs`, when any of them has a distance."""
    return clubs if any(c["distance_km"] is not None for c in clubs) else []


async def test_clubs_near_are_within_50_km_and_the_rest_come_apart(client, world):
    c = world["clubs"]
    home = (await client.get(f"{API}/home", params={"lat": TVM[0], "lng": TVM[1]})).json()
    assert _ids(home["clubs"]) == [c["tide"].id, c["verandah"].id]
    assert [x["distance_km"] for x in home["clubs"]] == [5.0, 8.6]
    # Further away, nearest first, each with its distance; a club with no place is near no one.
    assert _ids(home["clubs_more"]) == [c["ponnila"].id, c["borrow"].id, c["kandal"].id]
    assert all(x["distance_km"] > 50 for x in home["clubs_more"])
    assert 125 < home["clubs_more"][0]["distance_km"] < 127 and 410 < home["clubs_more"][-1]["distance_km"] < 413
    assert all(x["distance_km"] <= 50 for x in _called_near_by_old_clients(home["clubs"]))


async def test_with_no_club_near_the_clubs_are_the_most_followed(client, world):
    c = world["clubs"]
    home = (await client.get(f"{API}/home", params={"lat": MUMBAI[0], "lng": MUMBAI[1]})).json()
    # Old clients still get clubs to show, and nothing tells them these are near.
    assert _ids(home["clubs"]) == [c[k].id for k in ("nowhere", "kandal", "borrow", "ponnila", "verandah", "tide")]
    assert all(x["distance_km"] is None for x in home["clubs"])
    assert _called_near_by_old_clients(home["clubs"]) == []
    assert _ids(home["clubs_more"]) == [c[k].id for k in ("kandal", "borrow", "ponnila", "verandah", "tide")]
    assert all(x["distance_km"] > 800 for x in home["clubs_more"])


async def test_with_no_place_the_clubs_are_the_most_followed_and_none_more(client, world):
    home = (await client.get(f"{API}/home")).json()
    assert len(home["clubs"]) == 6 and all(x["distance_km"] is None for x in home["clubs"])
    assert home["clubs_more"] == []


async def test_the_near_rule_holds_with_the_minute_cache_on(client, world):
    from app.api.discover import cache

    cache.reset(enabled=True)
    try:
        for point in (TVM, VETTUCAUD, KOCHI, MUMBAI, (8.6, 76.95)):
            home = (await client.get(f"{API}/home", params={"lat": point[0], "lng": point[1]})).json()
            assert all(x["distance_km"] <= 50 for x in _called_near_by_old_clients(home["clubs"])), point
            assert all(x["distance_km"] > 50 for x in home["clubs_more"]), point
    finally:
        cache.reset(enabled=False)


async def test_the_edge_is_the_distance_as_sent(client, session, make_club):
    """A client calls a club near when its distance_km is 50 or less: the
    API splits on that same rounded number, so the two never disagree."""
    from tests.test_geo_cells import _dest

    clubs = {}
    for key, km in (("inside", 50.04), ("outside", 50.06)):
        _, club = await make_club(name=f"Edge {key}", email=f"near-edge-{key}@example.com")
        club.lat, club.lng = _dest(*TVM, km, 90.0)
        clubs[key] = club
    await session.commit()
    home = (await client.get(f"{API}/home", params={"lat": TVM[0], "lng": TVM[1]})).json()
    assert [(x["id"], x["distance_km"]) for x in home["clubs"]] == [(clubs["inside"].id, 50.0)]
    assert [(x["id"], x["distance_km"]) for x in home["clubs_more"]] == [(clubs["outside"].id, 50.1)]
