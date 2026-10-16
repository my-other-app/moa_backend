"""The public discover API behind the showcase site: what is on near the
visitor or anywhere, the clubs, their pages and posts — and the points that
"near you" turns on, read from map links when events and clubs are saved.

No test here reaches the network: map links and addresses are answered by a
fake, and anything else asked of it fails the test."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink, ClubPartners, ClubSocials, ClubTeamMembers, ClubUsersLink
from app.api.discover import geo, service
from app.api.events.models import EventCategories, Events
from app.api.interests.models import InterestCategory, Interests
from app.api.sites import service as sites
from app.api.sites.models import ClubSites
from app.api.threads.models import ClubThreads, ThreadLikes, ThreadViews
from tests.conftest import auth_header

API = "/api/v1/discover"
BLR = (12.9716, 77.5946)          # Bengaluru, the visitor
KORAMANGALA = (12.9352, 77.6245)  # about 5 km away
MYSURU = (12.2958, 76.6394)       # about 128 km away
DELHI = (28.6139, 77.2090)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def net(monkeypatch):
    """The only internet there is: `routes` maps a URL prefix to a response. A request nothing answers is recorded as a stray."""
    geo._ip_cache.clear()
    geo._tried.clear()
    state = {"routes": {}, "calls": [], "strays": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["calls"].append(url)
        for prefix, answer in state["routes"].items():
            if url.startswith(prefix):
                return answer
        state["strays"].append(url)
        return httpx.Response(404)

    def fake_http(timeout: float = 5.0):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True, timeout=timeout)

    monkeypatch.setattr(geo, "_http", fake_http)
    yield state
    geo._ip_cache.clear()
    geo._tried.clear()


@pytest.fixture(autouse=True)
def background(monkeypatch, session):
    """Work after the response runs on the test's own session, one piece at a time."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(geo, "session_scope", scope)


def _ids(items):
    return [x["id"] for x in items]


async def _thread(session, club, body, at, **kw):
    t = ClubThreads(id=uuid.uuid4(), club_id=club.id, body=body, created_at=at, **kw)
    session.add(t)
    await session.flush()
    return t


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    now = datetime.now(timezone.utc)
    d = timedelta(days=1)
    tech = EventCategories(name="Tech", icon="💻")
    music = EventCategories(name="Music", icon="🎵")
    dance = EventCategories(name="Dance", icon="💃")
    session.add_all([tech, music, dance])
    await session.flush()

    owner_a, a = await make_club(name="Circuit Society", email="disc-a@example.com")
    a.tagline, a.location_name, a.about, a.contact_email = "Build things that think.", "Bengaluru", "Hack nights and build weekends.", "hi@circuit.example"
    a.lat, a.lng = BLR
    a.created_at = now - 30 * d
    owner_b, b = await make_club(name="Rhythm Collective", email="disc-b@example.com")
    b.tagline, b.location_name = "Music for everyone", "Mysuru"
    b.created_at = now - 10 * d
    owner_c, c = await make_club(name="Ghost Club", email="disc-c@example.com", is_verified=False)
    owner_d, gone = await make_club(name="Gone Club", email="disc-d@example.com")
    gone.is_deleted = True

    async def ev(club, owner, name, start, category, **kw):
        kw.setdefault("lifecycle", EventStatus.published)
        return await make_event(club=club, created_by=owner, name=name, event_datetime=start, category_id=category.id, **kw)

    e = dict(
        live=await ev(a, owner_a, "Robot Build Day", now - timedelta(hours=1), tech, duration=3.0, location_name="Lab 3", lifecycle=EventStatus.live),
        online=await ev(a, owner_a, "Remote Rust Workshop", now + d, tech, is_online=True, location_name="Zoom"),
        near=await ev(a, owner_a, "Hack Night", now + 2 * d, tech, duration=3.0, location_name="Koramangala Hub", lat=KORAMANGALA[0], lng=KORAMANGALA[1], reg_enddate=now + timedelta(hours=10)),
        far=await ev(b, owner_b, "Mysuru Jam", now + 3 * d, music, location_name="Mysuru Palace Grounds", lat=MYSURU[0], lng=MYSURU[1], has_fee=True, reg_fee=200, max_participants=2),
        nocoords=await ev(b, owner_b, "Town Hall Sessions", now + 20 * d, music, location_name="Town Hall", page_views=50),
        # Never in a list:
        draft=await ev(a, owner_a, "Secret Draft", now + 2 * d, tech, lifecycle=EventStatus.draft, lat=KORAMANGALA[0], lng=KORAMANGALA[1]),
        deleted=await ev(a, owner_a, "Cancelled Meetup", now + 2 * d, tech, is_deleted=True, lat=KORAMANGALA[0], lng=KORAMANGALA[1]),
        unverified=await ev(c, owner_c, "Ghost Party", now + 2 * d, music, lat=KORAMANGALA[0], lng=KORAMANGALA[1]),
        gone=await ev(gone, owner_d, "Gone Gig", now + 2 * d, music),
        # Over: only on the club page's past list.
        ended=await ev(a, owner_a, "Intro to KiCad", now - 3 * d, tech, duration=2.0, location_name="Lab 1"),
        closed=await ev(a, owner_a, "Soldering 101", now - 30 * d, tech, lifecycle=EventStatus.closed, location_name="Lab 2"),
    )

    u1 = await make_user(email="disc-u1@example.com")
    u2 = await make_user(email="disc-u2@example.com")
    u3 = await make_user(email="disc-u3@example.com")
    await make_registration(event=e["far"], user=u1)
    await make_registration(event=e["far"], user=u2)
    await make_registration(event=e["far"], user=u3, is_deleted=True)  # cancelled: not going
    await make_registration(event=e["near"], user=u1)

    session.add_all([
        ClubUsersLink(club_id=b.id, user_id=u1.id, is_following=True),
        ClubUsersLink(club_id=b.id, user_id=u2.id, is_following=True),
        ClubUsersLink(club_id=b.id, user_id=u3.id, is_following=True),
        ClubUsersLink(club_id=a.id, user_id=u1.id, is_following=True),
        ClubUsersLink(club_id=a.id, user_id=u2.id, is_following=False),
        ClubUsersLink(club_id=a.id, user_id=u3.id, is_following=True, is_deleted=True),
    ])

    icat = InterestCategory(name="Discover interests")
    session.add(icat)
    await session.flush()
    interests = {n: Interests(name=n, category_id=icat.id) for n in ("Robotics", "AI", "IoT", "Drones", "Music", "Haunting")}
    session.add_all(interests.values())
    await session.flush()
    for n in ("Robotics", "AI", "IoT", "Drones"):
        session.add(ClubInterestsLink(club_id=a.id, interest_id=interests[n].id))
        await session.flush()
    session.add(ClubInterestsLink(club_id=b.id, interest_id=interests["Music"].id))
    session.add(ClubInterestsLink(club_id=b.id, interest_id=interests["AI"].id))
    session.add(ClubInterestsLink(club_id=c.id, interest_id=interests["Haunting"].id))  # an unverified club's

    session.add(ClubSocials(club_id=a.id, instagram="circuitsoc", website="https://circuit.example"))
    session.add(ClubTeamMembers(club_id=a.id, name="Asha Rao", role="Lead", instagram="@asha", linkedin="asha", sort_order=0))
    session.add(ClubTeamMembers(club_id=a.id, name="Left Already", role="Ex", sort_order=1, is_deleted=True))
    session.add(ClubPartners(club_id=a.id, name="Acme Labs", role="Title partner"))
    await session.commit()
    return dict(now=now, a=a, b=b, c=c, gone=gone, owner_a=owner_a, owner_b=owner_b, e=e, tech=tech, music=music, dance=dance,
                users=(u1, u2, u3), interests=interests, h_a=auth_header(await mint_token(owner_a)))


# ─── Visibility and the event card ─────────────────────────────────────────


async def test_events_lists_what_is_on_and_nothing_else(client, world):
    e = world["e"]
    r = await client.get(f"{API}/events")
    assert r.status_code == 200, r.text
    body = r.json()
    # Soonest first; no drafts, deleted events, unverified or deleted clubs, nothing over.
    assert _ids(body["items"]) == [e[k].id for k in ("live", "online", "near", "far", "nocoords")]
    assert body["total"] == 5 and body["offset"] == 0 and body["limit"] == 24
    assert body["where"] == {"city": None, "region": None, "lat": None, "lng": None, "source": "none"}
    assert body["categories"] == [
        {"id": world["tech"].id, "name": "Tech", "upcoming": 3},
        {"id": world["music"].id, "name": "Music", "upcoming": 2},
    ]

    cards = {x["id"]: x for x in body["items"]}
    far = cards[e["far"].id]
    assert far["has_fee"] is True and far["fee"] == 200.0 and far["going"] == 2 and far["capacity"] == 2 and far["status"] == "full"
    assert far["category"] == {"id": world["music"].id, "name": "Music"}
    assert far["club"] == {"id": world["b"].id, "name": "Rhythm Collective", "slug": world["b"].slug, "logo": None}
    assert far["slug"] == e["far"].slug and far["url"] == f"https://myotherapp.in/events/{e['far'].slug}"
    assert far["lat"] == MYSURU[0] and far["lng"] == MYSURU[1] and far["distance_km"] is None and far["is_online"] is False
    start = datetime.fromisoformat(far["starts_at"])
    assert start == e["far"].event_datetime and datetime.fromisoformat(far["ends_at"]) == start + timedelta(hours=e["far"].duration)
    assert cards[e["near"].id]["status"] == "closing_soon" and cards[e["near"].id]["going"] == 1
    assert cards[e["live"].id]["status"] == "live"
    online = cards[e["online"].id]
    assert online["status"] == "upcoming" and online["is_online"] is True and online["has_fee"] is False and online["fee"] is None and online["capacity"] is None


async def test_images_are_the_right_size_urls(client, session, world):
    a, near = world["a"], world["e"]["near"]
    a.logo = "clubs/logos/circuit.png"
    near.poster = "clubs/logos/poster.png"
    await session.commit()
    await session.refresh(a)
    await session.refresh(near)
    card = next(x for x in (await client.get(f"{API}/events")).json()["items"] if x["id"] == near.id)
    assert "poster.medium.png" in card["poster"] and "circuit.thumbnail.png" in card["club"]["logo"]
    club = (await client.get(f"{API}/clubs/{a.slug}")).json()["club"]
    assert "circuit.thumbnail.png" in club["logo"] and club["cover"] is None


# ─── Filters ──────────────────────────────────────────────────────────────


async def test_type_price_category_and_words_filter_events(client, world):
    e = world["e"]

    async def ids(**params):
        r = await client.get(f"{API}/events", params=params)
        assert r.status_code == 200, r.text
        return _ids(r.json()["items"])

    assert await ids(type="online") == [e["online"].id]
    assert await ids(type="in_person") == [e[k].id for k in ("live", "near", "far", "nocoords")]
    assert await ids(price="paid") == [e["far"].id]
    assert await ids(price="free") == [e[k].id for k in ("live", "online", "near", "nocoords")]
    assert await ids(category=world["music"].id) == [e["far"].id, e["nocoords"].id]
    assert await ids(category=world["dance"].id) == []
    assert await ids(q="rhythm") == [e["far"].id, e["nocoords"].id]  # the club's name
    assert await ids(q="TOWN HALL") == [e["nocoords"].id]  # the event's name and place
    assert await ids(q="koramangala") == [e["near"].id]  # the place
    assert await ids(q="musi") == [e["far"].id, e["nocoords"].id]  # the category
    assert await ids(q="hack") == [e["near"].id]
    assert await ids(q="ghost") == []  # an unverified club's event stays hidden
    assert await ids(q="%") == [] and await ids(q="_") == []  # wildcards are only characters
    assert await ids(q="rhythm", price="paid") == [e["far"].id]
    for bad in ({"type": "hybrid"}, {"price": "cheap"}, {"when": "someday"}, {"sort": "random"}, {"limit": 61}, {"offset": -1}):
        assert (await client.get(f"{API}/events", params=bad)).status_code == 422


def test_when_means_days_in_india():
    def at(y, m, dd, h=10, mi=0):
        return datetime(y, m, dd, h, mi, tzinfo=IST)

    wed = at(2026, 10, 14)
    assert service.window("any", wed) == (None, None)
    assert service.window("today", wed) == (at(2026, 10, 14, 0), at(2026, 10, 15, 0))
    assert service.window("tomorrow", wed) == (at(2026, 10, 15, 0), at(2026, 10, 16, 0))
    assert service.window("this_week", wed) == (wed, wed + timedelta(days=7))
    assert service.window("this_weekend", wed) == (at(2026, 10, 17, 0), at(2026, 10, 19, 0))
    assert service.window("next_week", wed) == (at(2026, 10, 19, 0), at(2026, 10, 26, 0))
    assert service.window("this_month", wed) == (wed, at(2026, 11, 1, 0))
    # On the weekend, "this weekend" is the one under way.
    assert service.window("this_weekend", at(2026, 10, 17)) == (at(2026, 10, 17, 0), at(2026, 10, 19, 0))
    assert service.window("this_weekend", at(2026, 10, 18)) == (at(2026, 10, 17, 0), at(2026, 10, 19, 0))
    assert service.window("next_week", at(2026, 10, 18)) == (at(2026, 10, 19, 0), at(2026, 10, 26, 0))
    assert service.window("this_month", at(2026, 12, 20)) == (at(2026, 12, 20), at(2027, 1, 1, 0))
    # 20:00 UTC on the 14th is already the 15th in India.
    late = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    assert service.window("today", late) == (at(2026, 10, 15, 0), at(2026, 10, 16, 0))


async def test_when_filters_events_by_those_days(client, session, world, make_event):
    now, a, owner, tech = datetime.now(timezone.utc), world["a"], world["owner_a"], world["tech"]

    async def inside(when, name):
        """An event wholly inside the window, and still to come."""
        w0, w1 = service.window(when, now)
        start = max(w0, now) + (w1 - max(w0, now)) / 2
        hours = (w1 - start).total_seconds() / 3600 / 2
        return await make_event(club=a, created_by=owner, name=name, event_datetime=start, duration=hours, category_id=tech.id, lifecycle=EventStatus.published)

    async def at(when_edge, name):
        return await make_event(club=a, created_by=owner, name=name, event_datetime=when_edge, duration=1.0, category_id=tech.id, lifecycle=EventStatus.published)

    today = await inside("today", "Later Today")
    tomorrow = await inside("tomorrow", "Tomorrow Talk")
    weekend = await inside("this_weekend", "Weekend Hack")
    next_week = await inside("next_week", "Next Week Meetup")
    after_weekend = await at(service.window("this_weekend", now)[1] + timedelta(hours=12), "Monday After")
    next_month = await at(service.window("this_month", now)[1] + timedelta(days=1), "Next Month Fest")
    await session.commit()

    async def ids(when):
        r = await client.get(f"{API}/events", params={"when": when, "limit": 60})
        assert r.status_code == 200, r.text
        return set(_ids(r.json()["items"]))

    got = await ids("today")
    assert today.id in got and tomorrow.id not in got and next_week.id not in got and next_month.id not in got
    got = await ids("tomorrow")
    assert tomorrow.id in got and today.id not in got and next_week.id not in got
    got = await ids("this_week")
    assert today.id in got and world["e"]["online"].id in got and world["e"]["nocoords"].id not in got and next_month.id not in got
    got = await ids("this_weekend")
    assert weekend.id in got and after_weekend.id not in got and next_week.id not in got
    got = await ids("next_week")
    assert next_week.id in got and today.id not in got
    if service.window("tomorrow", now)[0] < service.window("next_week", now)[0]:  # not a Sunday
        assert tomorrow.id not in got
    got = await ids("this_month")
    assert today.id in got and next_month.id not in got
    assert {today.id, tomorrow.id, weekend.id, next_week.id, after_weekend.id, next_month.id} <= await ids("any")


# ─── Distance ─────────────────────────────────────────────────────────────


async def test_a_known_place_keeps_events_within_the_radius_and_sorts_by_distance(client, world):
    e = world["e"]
    here = {"lat": BLR[0], "lng": BLR[1]}
    body = (await client.get(f"{API}/events", params=here)).json()
    assert body["where"] == {"city": None, "region": None, "lat": BLR[0], "lng": BLR[1], "source": "browser"}
    # Mysuru is past 50 km; online events and events without a point are never ruled out.
    assert _ids(body["items"]) == [e[k].id for k in ("live", "online", "near", "nocoords")] and body["total"] == 4
    near = next(x for x in body["items"] if x["id"] == e["near"].id)
    assert near["distance_km"] == 5.2
    assert next(x for x in body["items"] if x["id"] == e["online"].id)["distance_km"] is None

    wide = (await client.get(f"{API}/events", params={**here, "radius_km": 200})).json()
    assert e["far"].id in _ids(wide["items"]) and 125 < next(x for x in wide["items"] if x["id"] == e["far"].id)["distance_km"] < 130
    assert (await client.get(f"{API}/events", params={**here, "radius_km": 0})).json()["total"] == 5

    by_distance = (await client.get(f"{API}/events", params={**here, "radius_km": 200, "sort": "distance"})).json()
    assert _ids(by_distance["items"]) == [e[k].id for k in ("near", "far", "live", "online", "nocoords")]
    # Without a place, distance falls back to the date.
    assert _ids((await client.get(f"{API}/events", params={"sort": "distance"})).json()["items"]) == [e[k].id for k in ("live", "online", "near", "far", "nocoords")]
    # From Delhi nothing in person is near; online is still there.
    far_away = (await client.get(f"{API}/events", params={"lat": DELHI[0], "lng": DELHI[1], "type": "online"})).json()
    assert _ids(far_away["items"]) == [e["online"].id]


async def test_without_a_browser_point_the_visitor_is_placed_by_address(client, net, world):
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json={"city": "Bengaluru", "region": "Karnataka", "country_name": "India", "latitude": BLR[0], "longitude": BLR[1]})
    h = {"X-Forwarded-For": "49.37.10.1, 10.0.0.1"}
    body = (await client.get(f"{API}/events", headers=h)).json()
    assert body["where"] == {"city": "Bengaluru", "region": "Karnataka", "lat": BLR[0], "lng": BLR[1], "source": "ip"}
    assert world["e"]["far"].id not in _ids(body["items"]) and body["total"] == 4
    assert (await client.get(f"{API}/locate", headers=h)).json()["source"] == "ip"
    assert (await client.get(f"{API}/locate", headers=h, params={"lat": 12.97, "lng": 77.6})).json() == {"city": "Bengaluru", "region": "Karnataka", "lat": 12.97, "lng": 77.6, "source": "browser"}
    assert (await client.get(f"{API}/locate")).json()["source"] == "none"
    assert (await client.get(f"{API}/locate", params={"lat": 91, "lng": 10})).status_code == 422
    assert [c for c in net["calls"] if "ipapi" in c] == ["https://ipapi.co/49.37.10.1/json/"]  # looked up once
    assert net["strays"] == []


async def test_popular_sorts_by_going_and_views_and_pages_after_filtering(client, world):
    e = world["e"]
    popular = (await client.get(f"{API}/events", params={"sort": "popular"})).json()
    assert _ids(popular["items"]) == [e[k].id for k in ("nocoords", "far", "near", "live", "online")]
    first = (await client.get(f"{API}/events", params={"limit": 2})).json()
    rest = (await client.get(f"{API}/events", params={"limit": 2, "offset": 2})).json()
    last = (await client.get(f"{API}/events", params={"limit": 2, "offset": 4})).json()
    assert first["total"] == rest["total"] == last["total"] == 5
    assert _ids(first["items"]) + _ids(rest["items"]) + _ids(last["items"]) == [e[k].id for k in ("live", "online", "near", "far", "nocoords")]
    assert (await client.get(f"{API}/events", params={"limit": 2, "offset": 2, "lat": BLR[0], "lng": BLR[1]})).json()["total"] == 4


# ─── Home ─────────────────────────────────────────────────────────────────


async def test_home_from_a_place(client, session, world):
    e, a, b, now = world["e"], world["a"], world["b"], world["now"]
    for i in range(7):
        await _thread(session, a if i % 2 else b, f"Post {i}", now - timedelta(hours=i + 1))
    await _thread(session, world["c"], "Unverified post", now - timedelta(minutes=5))
    await _thread(session, a, "Deleted post", now - timedelta(minutes=6), is_deleted=True)
    await session.commit()

    r = await client.get(f"{API}/home", params={"lat": BLR[0], "lng": BLR[1]})
    assert r.status_code == 200, r.text
    home = r.json()
    assert home["where"]["source"] == "browser"
    assert _ids(home["near"]) == [e["near"].id] and home["near"][0]["distance_km"] == 5.2
    assert _ids(home["this_week"]) == [e[k].id for k in ("live", "online", "near", "far")]
    assert _ids(home["online"]) == [e["online"].id]
    assert _ids(home["popular"])[:3] == [e["nocoords"].id, e["far"].id, e["near"].id]
    assert home["categories"] == [
        {"id": world["tech"].id, "name": "Tech", "icon": "💻", "upcoming": 3},
        {"id": world["music"].id, "name": "Music", "icon": "🎵", "upcoming": 2},
    ]
    # The nearest club first (its own point), then the one placed by its events in Mysuru.
    assert _ids(home["clubs"]) == [a.id, b.id]
    assert home["clubs"][0]["distance_km"] == 0.0 and 125 < home["clubs"][1]["distance_km"] < 130
    assert [t["body"] for t in home["threads"]] == [f"Post {i}" for i in range(6)]
    assert home["stats"] == {"upcoming_events": 5, "clubs": 2, "cities": 6}


async def test_home_without_a_place(client, world):
    home = (await client.get(f"{API}/home")).json()
    assert home["where"]["source"] == "none" and home["near"] == []
    # The most followed club first.
    assert _ids(home["clubs"]) == [world["b"].id, world["a"].id]
    assert all(c["distance_km"] is None for c in home["clubs"])
    assert home["threads"] == []


async def test_home_reads_points_still_missing_after_it_answers(client, session, net, world, make_event):
    a, owner, tech, now = world["a"], world["owner_a"], world["tech"], world["now"]
    net["routes"]["https://maps.app.goo.gl/Venue"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/V/@12.95,77.6,17z"})
    net["routes"]["https://www.google.com/maps/place/V/"] = httpx.Response(200)
    net["routes"]["https://maps.app.goo.gl/Dead"] = httpx.Response(404)
    short = await make_event(club=a, created_by=owner, name="Short Link Night", event_datetime=now + timedelta(days=4), category_id=tech.id, location_link="https://maps.app.goo.gl/Venue")
    dead = await make_event(club=a, created_by=owner, name="Dead Link Night", event_datetime=now + timedelta(days=4), category_id=tech.id, location_link="https://maps.app.goo.gl/Dead")
    world["b"].location_link = "https://maps.google.com/?q=12.30,76.64"  # saved before points were read
    await session.commit()

    assert (await client.get(f"{API}/home")).status_code == 200
    await session.refresh(short)
    await session.refresh(dead)
    await session.refresh(world["b"])
    assert (short.lat, short.lng) == (12.95, 77.6)
    assert (world["b"].lat, world["b"].lng) == (12.3, 76.64)
    assert dead.lat is None
    # A link that leads nowhere is not followed again on the next page view.
    await client.get(f"{API}/home")
    assert net["calls"].count("https://maps.app.goo.gl/Dead") == 1
    assert net["strays"] == []


# ─── Clubs ────────────────────────────────────────────────────────────────


async def test_clubs_search(client, world):
    a, b = world["a"], world["b"]

    async def get(**params):
        r = await client.get(f"{API}/clubs", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    body = await get()
    assert _ids(body["items"]) == [b.id, a.id] and body["total"] == 2  # most followed first; no unverified or deleted club
    assert body["where"]["source"] == "none"
    cards = {c["id"]: c for c in body["items"]}
    assert cards[a.id] == {
        "id": a.id, "name": "Circuit Society", "slug": a.slug, "logo": None, "cover": None, "tagline": "Build things that think.",
        "location_name": "Bengaluru", "followers": 1, "upcoming_events": 3, "interests": ["Robotics", "AI", "IoT"],
        "distance_km": None, "website_url": None,
    }
    assert cards[b.id]["followers"] == 3 and cards[b.id]["upcoming_events"] == 2 and sorted(cards[b.id]["interests"]) == ["AI", "Music"]
    top = {i["name"]: i["clubs"] for i in body["interests"]}
    assert top["AI"] == 2 and top["Robotics"] == 1 and "Haunting" not in top
    assert body["interests"][0]["name"] == "AI"

    assert _ids((await get(q="circuit"))["items"]) == [a.id]
    assert _ids((await get(q="THINK"))["items"]) == [a.id]  # the tagline
    assert _ids((await get(q="mysuru"))["items"]) == [b.id]  # the place
    assert _ids((await get(q="ghost"))["items"]) == []
    assert _ids((await get(interest=world["interests"]["Robotics"].id))["items"]) == [a.id]
    assert _ids((await get(interest=world["interests"]["AI"].id))["items"]) == [b.id, a.id]
    assert _ids((await get(sort="newest"))["items"]) == [b.id, a.id]

    here = {"lat": BLR[0], "lng": BLR[1]}
    near = await get(**here)
    assert _ids(near["items"]) == [a.id] and near["items"][0]["distance_km"] == 0.0  # Mysuru is past 50 km
    wide = await get(**here, radius_km=500, sort="nearby")
    assert _ids(wide["items"]) == [a.id, b.id] and 125 < wide["items"][1]["distance_km"] < 130
    assert _ids((await get(**here, radius_km=500))["items"]) == [b.id, a.id]  # popular, still
    page = await get(limit=1, offset=1)
    assert _ids(page["items"]) == [a.id] and page["total"] == 2
    assert (await client.get(f"{API}/clubs", params={"sort": "loudest"})).status_code == 422


async def test_a_club_page(client, session, world):
    a, e, now = world["a"], world["e"], world["now"]
    for i in range(6):
        await _thread(session, a, f"A post {i}", now - timedelta(hours=i + 1))
    await _thread(session, a, "Gone post", now - timedelta(minutes=1), is_deleted=True)
    await session.commit()

    r = await client.get(f"{API}/clubs/{a.slug}")
    assert r.status_code == 200, r.text
    page = r.json()
    club = page["club"]
    assert club["name"] == "Circuit Society" and club["about"] == "Hack nights and build weekends." and club["contact_email"] == "hi@circuit.example"
    assert club["socials"] == {"instagram": "https://instagram.com/circuitsoc", "linkedin": None, "youtube": None, "website": "https://circuit.example"}
    assert club["events_held"] == 2 and club["followers"] == 1 and club["upcoming_events"] == 3 and club["website_url"] is None
    assert _ids(page["upcoming"]) == [e[k].id for k in ("live", "online", "near")]
    assert _ids(page["past"]) == [e["ended"].id, e["closed"].id] and {x["status"] for x in page["past"]} == {"ended"}
    assert [t["body"] for t in page["threads"]] == [f"A post {i}" for i in range(5)]
    assert page["team"] == [{"name": "Asha Rao", "role": "Lead", "photo": None, "links": {"instagram": "https://instagram.com/asha", "linkedin": "https://linkedin.com/in/asha", "x": None, "website": None}}]
    assert page["partners"] == [{"name": "Acme Labs", "role": "Title partner", "logo": None}]

    # The website address only once the site is published.
    session.add(ClubSites(club_id=a.id, sections={}, published_at=now))
    session.add(ClubSites(club_id=world["b"].id, sections={}))
    await session.commit()
    assert (await client.get(f"{API}/clubs/{a.slug}")).json()["club"]["website_url"] == sites.public_url(a)
    assert (await client.get(f"{API}/clubs/{world['b'].slug}")).json()["club"]["website_url"] is None
    listed = {c["id"]: c for c in (await client.get(f"{API}/clubs")).json()["items"]}
    assert listed[a.id]["website_url"] == sites.public_url(a) and listed[world["b"].id]["website_url"] is None


async def test_a_club_page_is_only_for_a_verified_club(client, world):
    for slug in (world["c"].slug, world["gone"].slug, "nobody-here"):
        assert (await client.get(f"{API}/clubs/{slug}")).status_code == 404


# ─── Threads and categories ───────────────────────────────────────────────


async def test_the_threads_feed_is_newest_first_across_verified_clubs(client, session, world, mint_token):
    a, b, now = world["a"], world["b"], world["now"]
    a1 = await _thread(session, a, "A one", now - timedelta(hours=1))
    b1 = await _thread(session, b, "B one", now - timedelta(hours=2))
    a2 = await _thread(session, a, "A two", now - timedelta(hours=3))
    await _thread(session, world["c"], "Unverified", now - timedelta(minutes=30))
    await _thread(session, world["gone"], "Deleted club", now - timedelta(minutes=20))
    await _thread(session, a, "Deleted", now - timedelta(minutes=10), is_deleted=True)
    u1 = world["users"][0]
    session.add(ThreadLikes(thread_id=a1.id, user_id=u1.id))
    a1.likes_count = 1
    await session.commit()

    body = (await client.get(f"{API}/threads")).json()
    assert [t["id"] for t in body["items"]] == [str(a1.id), str(b1.id), str(a2.id)] and body["total"] == 3
    assert body["items"][0]["club"]["slug"] == a.slug and body["items"][0]["likes"] == 1 and body["items"][0]["liked"] is False
    page = (await client.get(f"{API}/threads", params={"limit": 2, "offset": 2})).json()
    assert [t["id"] for t in page["items"]] == [str(a2.id)] and page["total"] == 3 and page["limit"] == 2 and page["offset"] == 2
    mine = (await client.get(f"{API}/threads", headers=auth_header(await mint_token(u1)))).json()
    assert mine["items"][0]["liked"] is True
    # Reading the feed here counts no views.
    assert await session.scalar(select(func.count()).select_from(ThreadViews)) == 0
    await session.refresh(a1)
    assert a1.views_count == 0


async def test_categories_count_what_is_coming(client, world):
    body = (await client.get(f"{API}/categories")).json()
    by_id = {c["id"]: c for c in body}
    assert by_id[world["tech"].id] == {"id": world["tech"].id, "name": "Tech", "icon": "💻", "upcoming": 3}
    assert by_id[world["music"].id]["upcoming"] == 2
    assert by_id[world["dance"].id]["upcoming"] == 0  # one the product ships, with nothing on yet
    assert "General" not in {c["name"] for c in body}  # a club's own, with nothing on
    order = [c["id"] for c in body]
    assert order.index(world["tech"].id) < order.index(world["music"].id) < order.index(world["dance"].id)


# ─── Points set when events and clubs are saved ───────────────────────────


async def test_an_event_saved_with_a_map_link_gets_its_point(client, session, net, world):
    h, tech, now = world["h_a"], world["tech"], world["now"]
    net["routes"]["https://maps.app.goo.gl/NewVenue"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/Hall/@18.52,73.85,17z"})
    net["routes"]["https://www.google.com/maps/place/Hall/"] = httpx.Response(200)
    form = {"event_datetime": (now + timedelta(days=12)).isoformat(), "duration": "3", "has_fee": "false", "status": "published", "category_id": str(tech.id)}

    made = await client.post("/api/v1/events/create", data={**form, "name": "Linked Night", "location_link": "https://www.google.com/maps/place/Lab/@18.5293,73.8565,17z"}, headers=h)
    assert made.status_code in (200, 201), made.text
    event = await session.get(Events, made.json()["id"])
    assert (event.lat, event.lng) == (18.5293, 73.8565)
    assert net["calls"] == []  # read from the link, not fetched

    short = await client.post("/api/v1/events/create", data={**form, "name": "Short Linked Night", "location_link": "https://maps.app.goo.gl/NewVenue"}, headers=h)
    assert short.status_code in (200, 201), short.text
    short_event = await session.get(Events, short.json()["id"])
    await session.refresh(short_event)
    assert (short_event.lat, short_event.lng) == (18.52, 73.85) and short_event.location_link == "https://maps.app.goo.gl/NewVenue"

    plain = await client.post("/api/v1/events/create", data={**form, "name": "Unlinked Night"}, headers=h)
    assert (await session.get(Events, plain.json()["id"])).lat is None

    update = {**form, "name": "Linked Night", "interest_ids": str(world["interests"]["AI"].id)}
    # A form that does not send the link keeps it and its point.
    r = await client.put(f"/api/v1/events/update/{event.id}", data=update, headers=h)
    assert r.status_code == 200, r.text
    await session.refresh(event)
    assert (event.lat, event.lng) == (18.5293, 73.8565)
    # A new link moves the point.
    await client.put(f"/api/v1/events/update/{event.id}", data={**update, "location_link": "https://maps.google.com/?q=12.5,77.5"}, headers=h)
    await session.refresh(event)
    assert (event.lat, event.lng, event.location_link) == (12.5, 77.5, "https://maps.google.com/?q=12.5,77.5")
    # An empty field is what a form sends for one it left alone: it keeps the link.
    await client.put(f"/api/v1/events/update/{event.id}", data={**update, "location_link": ""}, headers=h)
    await session.refresh(event)
    assert (event.lat, event.lng) == (12.5, 77.5)
    # Removing it clears both.
    await client.put(f"/api/v1/events/update/{event.id}", data={**update, "clear_location_link": "true"}, headers=h)
    await session.refresh(event)
    assert (event.lat, event.lng, event.location_link) == (None, None, None)
    assert net["strays"] == []


async def test_a_club_saved_with_a_map_link_gets_its_point(client, session, net, world, mint_token):
    b = world["b"]
    h = auth_header(await mint_token(world["owner_b"]))
    net["routes"]["https://g.page/rhythm"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/R/@12.31,76.65,17z"})
    net["routes"]["https://www.google.com/maps/place/R/"] = httpx.Response(200)

    r = await client.put("/api/v1/clubs/update", data={"location_name": "Mysuru", "location_link": "https://maps.google.com/?q=12.3,76.6"}, headers=h)
    assert r.status_code == 200, r.text
    await session.refresh(b)
    assert (b.lat, b.lng) == (12.3, 76.6)
    # The same link again keeps the point; a short link is followed after the save.
    await client.put("/api/v1/clubs/update", data={"location_name": "Mysuru", "location_link": "https://maps.google.com/?q=12.3,76.6"}, headers=h)
    await session.refresh(b)
    assert (b.lat, b.lng) == (12.3, 76.6)
    await client.put("/api/v1/clubs/update", data={"location_name": "Mysuru", "location_link": "https://g.page/rhythm"}, headers=h)
    await session.refresh(b)
    assert (b.lat, b.lng) == (12.31, 76.65)
    # No link: no point.
    await client.put("/api/v1/clubs/update", data={"location_name": "Mysuru"}, headers=h)
    await session.refresh(b)
    assert (b.lat, b.lng, b.location_link) == (None, None, None)
    assert net["strays"] == []


async def test_the_admin_backfill_reads_every_missing_point(client, session, net, world, make_event, make_admin, mint_token):
    a, owner, tech, now = world["a"], world["owner_a"], world["tech"], world["now"]
    net["routes"]["https://maps.app.goo.gl/Backfill"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/@12.9,77.61,15z"})
    net["routes"]["https://www.google.com/maps/@12.9"] = httpx.Response(200)
    net["routes"]["https://g.page/rhythm"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/R/@12.31,76.65,17z"})
    net["routes"]["https://www.google.com/maps/place/R/"] = httpx.Response(200)
    legacy = await make_event(club=a, created_by=owner, name="Legacy", event_datetime=now + timedelta(days=5), category_id=tech.id, location_link="https://maps.google.com/?q=12.91,77.62")
    short = await make_event(club=a, created_by=owner, name="Short", event_datetime=now + timedelta(days=5), category_id=tech.id, location_link="https://maps.app.goo.gl/Backfill")
    text = await make_event(club=a, created_by=owner, name="Text", event_datetime=now + timedelta(days=5), category_id=tech.id, location_link="Main hall, block B")
    world["b"].location_link = "https://g.page/rhythm"
    await session.commit()

    assert (await client.post("/api/v1/admin/discover/geo-backfill", headers=world["h_a"])).status_code == 403
    admin = await make_admin(email="disc-admin@example.com")
    r = await client.post("/api/v1/admin/discover/geo-backfill", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200, r.text
    assert r.json() == {"events": {"checked": 3, "resolved": 2}, "clubs": {"checked": 1, "resolved": 1}}
    for row in (legacy, short, text, world["b"]):
        await session.refresh(row)
    assert (legacy.lat, legacy.lng) == (12.91, 77.62) and (short.lat, short.lng) == (12.9, 77.61) and text.lat is None
    assert (world["b"].lat, world["b"].lng) == (12.31, 76.65)
    assert net["strays"] == []


async def test_the_api_is_rate_limited_per_visitor(client):
    h = {"X-Forwarded-For": "203.0.113.7"}
    for _ in range(120):
        assert (await client.get(f"{API}/locate", headers=h)).status_code == 200
    assert (await client.get(f"{API}/locate", headers=h)).status_code == 429
    assert (await client.get(f"{API}/locate", headers={"X-Forwarded-For": "203.0.113.8"})).status_code == 200
