"""The discover lists, the For You feed and the app's lists, moved into the
database for scale (scripts/bench/RESULTS.md): what they answer must not
change, and what they cost must not grow with the number of events.

- Every list is compared, row for row and number for number, with the way it
  was computed before (tests/discover_reference.py), over a random world
  that has the awkward cases in it: events with no point, online events, a
  radius's edge, the antimeridian, a pole, clubs placed only by their events.
- Statements per request are counted with 5 events and with 60.
- The going and follower counters, kept by triggers, are checked after every
  kind of write that changes the rows they count.
- The front page's minute cache never carries anything of the reader's, and
  never answers with what an uncached page would not.
"""
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Float, event as sa_event, literal, select, text, update

from app.api.closeout import service as closeout
from app.api.closeout.models import EventStatus, RetentionJobs, RetentionMode, RetentionState
from app.api.clubs.models import ClubInterestsLink, Clubs, ClubUsersLink
from app.api.discover import cache, counters, geo, service
from app.api.events import recommendations
from app.api.events.models import EventCategories, EventInterestsLink, EventRegistrationsLink, Events
from app.api.interests.models import InterestCategory, Interests
from app.api.threads.models import ClubThreads, ThreadLikes, ThreadPollOptions, ThreadVotes
from app.api.users.models import UserInterests, UserProfiles, Users, UserTypes
from app.db.listeners import add_loader_criteria
from tests import discover_reference as ref
from tests.conftest import auth_header

API = "/api/v1/discover"
BLR = (12.9716, 77.5946)
KOCHI = (9.9312, 76.2673)
SUVA = (-18.1416, 178.4419)       # just west of the antimeridian
TAVEUNI = (-16.85, -179.95)       # just east of it, ~170 km from Suva
LONGYEARBYEN = (78.2232, 15.6267)  # far north


@pytest.fixture(autouse=True)
def _as_production(session):
    """The soft-delete filter production puts on every session."""
    add_loader_criteria(session)


@pytest.fixture(autouse=True)
def _no_ticket_mail(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)


@pytest.fixture
def statements(session):
    """Every statement the test's connection runs, in order."""
    seen: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)


@pytest.fixture
def shared_cache():
    cache.reset(enabled=True)
    yield cache.shared
    cache.reset(enabled=False)


# ─── A world ─────────────────────────────────────────────────────────────


def _near(rng, centre, km):
    r = km * math.sqrt(rng.random())
    t = rng.random() * 2 * math.pi
    lat = centre[0] + (r * math.cos(t)) / 111.2
    lng = centre[1] + (r * math.sin(t)) / (111.2 * math.cos(math.radians(centre[0])))
    if lng > 180:
        lng -= 360
    if lng < -180:
        lng += 360
    return round(lat, 6), round(lng, 6)


class World:
    def __init__(self, session, seed=7):
        self.session = session
        self.rng = random.Random(seed)
        self.now = datetime.now(timezone.utc)
        self.clubs: list[Clubs] = []
        self.events: list[Events] = []
        self.users: list[Users] = []
        self.categories: list[EventCategories] = []
        self.interests: list[Interests] = []

    async def base(self, n_users=24):
        s, rng = self.session, self.rng
        tag = uuid.uuid4().hex[:6]
        self.categories = [EventCategories(name=n) for n in ("Tech Talks", "Music", "Dance", "Robotics Lab", "Quiz")]
        s.add_all(self.categories)
        icat = InterestCategory(name=f"Scale {tag}")
        s.add(icat)
        await s.flush()
        self.interests = [Interests(name=f"{n} {tag}", category_id=icat.id) for n in ("AI", "Music", "Robotics", "Design", "Startups", "Chess")]
        s.add_all(self.interests)
        self.users = [Users(full_name=f"Person {i}", username=f"scale-{tag}-{i}", email=f"scale-{tag}-{i}@example.com", user_type=UserTypes.app_user) for i in range(n_users)]
        s.add_all(self.users)
        await s.flush()
        s.add_all([UserProfiles(user_id=u.id, full_name=u.full_name) for u in self.users])
        for u in self.users:
            for i in rng.sample(range(len(self.interests)), rng.randint(0, 3)):
                s.add(UserInterests(user_id=u.id, interest_id=self.interests[i].id))
        await s.flush()

    async def clubs_at(self, places, *, verified=True):
        made = []
        for name, point, own_point in places:
            c = Clubs(
                slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}", name=name, is_verified=verified,
                tagline=f"{name} for everyone", location_name=name.split()[0],
                lat=point[0] if own_point and point else None, lng=point[1] if own_point and point else None,
                created_at=self.now - timedelta(days=self.rng.randint(1, 900)),
            )
            self.session.add(c)
            made.append(c)
        await self.session.flush()
        for c in made:
            for i in self.rng.sample(range(len(self.interests)), self.rng.randint(1, 3)):
                self.session.add(ClubInterestsLink(club_id=c.id, interest_id=self.interests[i].id))
        self.clubs.extend(made)
        return made

    async def events_for(self, club, centre, n, *, spread_km=60.0, **kw):
        rng, made = self.rng, []
        for _ in range(n):
            kind = rng.random()
            online = kind < 0.15
            pointless = 0.15 <= kind < 0.25
            point = None if (online or pointless or centre is None) else _near(rng, centre, spread_km)
            start = self.now + timedelta(hours=rng.uniform(-3, 24 * 40)) if rng.random() < 0.9 else self.now - timedelta(days=rng.uniform(2, 300))
            duration = rng.choice([1.0, 2.0, 3.0, 5.0, 48.0])
            # Keep clear of the moment the comparisons are made: nothing ends within two minutes of now.
            end = start + timedelta(hours=duration)
            if abs((end - self.now).total_seconds()) < 120:
                start += timedelta(minutes=5)
            lifecycle = rng.choice([EventStatus.published] * 8 + [EventStatus.live, EventStatus.draft, EventStatus.closed])
            e = Events(
                slug=f"ev-{uuid.uuid4().hex[:10]}", name=rng.choice(["Robotics Night", "Jazz Jam", "AI Meetup", "Quiz Bowl", "Design Sprint", "Chess Open", "Startup Pitch", "Salsa Social"]) + f" {len(self.events)}",
                images=[], event_datetime=start, duration=duration, club_id=club.id,
                category_id=rng.choice(self.categories).id, is_online=online if rng.random() < 0.9 else None,
                lat=point[0] if point else None, lng=point[1] if point else None,
                location_name=None if online else rng.choice(["Main Hall", "Open Air Theatre", "Koramangala Hub", "Lab 3", None]),
                has_fee=rng.random() < 0.3, reg_fee=rng.choice([99, 199]), max_participants=rng.choice([None, 2, 5, 50]),
                page_views=rng.randint(0, 40), lifecycle=lifecycle, is_deleted=rng.random() < 0.04,
                reg_enddate=start - timedelta(hours=rng.choice([1, 30])) if rng.random() < 0.4 else None,
                **kw,
            )
            self.session.add(e)
            made.append(e)
        await self.session.flush()
        for e in made:
            for i in self.rng.sample(range(len(self.interests)), self.rng.randint(0, 2)):
                self.session.add(EventInterestsLink(event_id=e.id, interest_id=self.interests[i].id))
        self.events.extend(made)
        return made

    async def activity(self):
        """Registrations for the events, and follows for the clubs, made since the last call."""
        rng, s = self.rng, self.session
        done = getattr(self, "_active", set())
        self._active = done
        for e in self.events:
            if ("e", e.id) in done:
                continue
            done.add(("e", e.id))
            for u in rng.sample(self.users, rng.randint(0, 6)):
                s.add(EventRegistrationsLink(event_id=e.id, user_id=u.id, full_name=u.full_name, email=u.email, ticket_id=f"T-{uuid.uuid4().hex[:12]}", is_deleted=rng.random() < 0.2))
        for c in self.clubs:
            if ("c", c.id) in done:
                continue
            done.add(("c", c.id))
            for u in rng.sample(self.users, rng.randint(0, 10)):
                s.add(ClubUsersLink(club_id=c.id, user_id=u.id, is_following=rng.random() < 0.8, is_pinned=rng.random() < 0.1, is_hidden=rng.random() < 0.05, is_deleted=rng.random() < 0.1))
        await s.flush()


async def _big_world(session) -> World:
    w = World(session)
    await w.base()
    blr = await w.clubs_at([(f"Bengaluru Club {i}", _near(w.rng, BLR, 20), i % 4 != 0) for i in range(8)])
    kochi = await w.clubs_at([(f"Kochi Club {i}", _near(w.rng, KOCHI, 15), i % 3 != 0) for i in range(4)])
    fiji = await w.clubs_at([("Suva Club", SUVA, True), ("Taveuni Club", TAVEUNI, False)])
    north = await w.clubs_at([("Arctic Club", LONGYEARBYEN, True)])
    nowhere = await w.clubs_at([("Online Only Club", None, False)])
    hidden = await w.clubs_at([("Unverified Club", BLR, True)], verified=False)
    for c in blr:
        await w.events_for(c, BLR, 7)
    for c in kochi:
        await w.events_for(c, KOCHI, 6)
    await w.events_for(fiji[0], SUVA, 6, spread_km=30)
    await w.events_for(fiji[1], TAVEUNI, 6, spread_km=30)
    await w.events_for(north[0], LONGYEARBYEN, 5, spread_km=200)
    await w.events_for(nowhere[0], None, 5)
    await w.events_for(hidden[0], BLR, 3)
    await w.activity()
    await session.commit()
    return w


def _ids(items):
    return [x["id"] for x in items]


# ─── Geometry ────────────────────────────────────────────────────────────


def test_the_box_holds_every_point_the_circle_does():
    """Including next to a pole, across the antimeridian, and radii from a metre to half the planet."""
    rng = random.Random(11)
    centres = [BLR, SUVA, TAVEUNI, LONGYEARBYEN, (89.9, 10.0), (-89.95, -170.0), (0.0, 179.999), (0.0, -180.0), (45.0, 0.0)]
    centres += [(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(40)]
    for lat, lng in centres:
        for r in (0.001, 1, 50, 300, 2500, 9000, 19000):
            box = geo.bbox(lat, lng, r)
            for _ in range(150):
                # Points around the edge of the circle, and some anywhere.
                bearing = rng.uniform(0, 2 * math.pi)
                d = r * rng.uniform(0.9, 1.0001) if rng.random() < 0.8 else rng.uniform(0, 20000)
                plat, plng = _destination(lat, lng, bearing, d)
                if geo.km(lat, lng, plat, plng) <= r:
                    assert box is None or _inside(box, plat, plng), (lat, lng, r, plat, plng, box)
            if box is not None and r <= 300 and abs(lat) < 80:
                south, north, ranges = box
                # Not a degenerate everything-box either.
                assert north - south < 2 * r / 111.0 + 0.01 and sum(e - w for w, e in ranges) < 60


def _destination(lat, lng, bearing, km):
    d = km / geo.EARTH_KM
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(bearing))
    l2 = l1 + math.atan2(math.sin(bearing) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    lng2 = (math.degrees(l2) + 540) % 360 - 180
    return math.degrees(p2), lng2


def _inside(box, lat, lng):
    south, north, ranges = box
    return south <= lat <= north and any(w <= lng <= e for w, e in ranges)


async def test_the_database_measures_distance_as_the_app_does(session):
    rng = random.Random(5)
    pairs = [((rng.uniform(-80, 80), rng.uniform(-180, 180)), (rng.uniform(-80, 80), rng.uniform(-180, 180))) for _ in range(60)]
    pairs += [(BLR, BLR), (SUVA, TAVEUNI), (LONGYEARBYEN, (78.5, -170.0))]
    for a, b in pairs:
        got = await session.scalar(select(geo.km_sql(a[0], a[1], literal(b[0], Float), literal(b[1], Float))))
        assert got == pytest.approx(geo.km(*a, *b), abs=1e-9)


# ─── Same answers as before ──────────────────────────────────────────────

SEARCHES = [
    {},
    {"q": "robotics"}, {"q": "JAZZ"}, {"q": "hall"}, {"q": "club 1"}, {"q": "lab"}, {"q": "%"}, {"q": "_"},
    {"when": "this_week"}, {"when": "this_weekend"}, {"when": "next_week"}, {"when": "this_month"}, {"when": "today"},
    {"type": "online"}, {"type": "in_person"}, {"price": "free"}, {"price": "paid"},
    {"sort": "popular"}, {"sort": "popular", "offset": 10, "limit": 7},
    {"lat": BLR[0], "lng": BLR[1]},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 0},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 12, "sort": "distance"},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 400, "sort": "distance", "limit": 5, "offset": 0},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 400, "sort": "distance", "limit": 5, "offset": 45},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 400, "sort": "distance", "limit": 60, "offset": 0},
    {"lat": BLR[0], "lng": BLR[1], "radius_km": 0, "sort": "distance", "limit": 60},
    {"lat": SUVA[0], "lng": SUVA[1], "radius_km": 200, "sort": "distance", "limit": 60},
    {"lat": TAVEUNI[0], "lng": TAVEUNI[1], "radius_km": 60, "limit": 60},
    {"lat": 89.5, "lng": 0.0, "radius_km": 1500, "sort": "distance", "limit": 60},
    {"lat": KOCHI[0], "lng": KOCHI[1], "type": "online", "sort": "distance"},
    {"lat": KOCHI[0], "lng": KOCHI[1], "sort": "popular", "q": "a"},
    {"sort": "distance"},
    {"offset": 30, "limit": 10}, {"offset": 500},
]


async def test_event_search_answers_as_before(client, session):
    w = await _big_world(session)
    cat = w.categories[0].id
    answered = 0
    for params in SEARCHES + [{"category": cat}, {"category": cat, "sort": "popular"}]:
        r = await client.get(f"{API}/events", params=params)
        assert r.status_code == 200, (params, r.text)
        body = r.json()
        here = (params["lat"], params["lng"]) if "lat" in params else None
        want = await ref.search_events(
            session, here, q=params.get("q"), when=params.get("when", "any"), category=params.get("category"),
            type_=params.get("type", "any"), price=params.get("price", "any"), radius_km=params.get("radius_km", 50),
            sort=params.get("sort", "date"), offset=params.get("offset", 0), limit=params.get("limit", 24), now=service.now_utc(),
        )
        assert _ids(body["items"]) == want["ids"], params
        assert body["total"] == want["total"], params
        assert [x["distance_km"] for x in body["items"]] == want["distance"], params
        assert [x["going"] for x in body["items"]] == want["going"], params
        answered += bool(want["ids"])
    assert answered >= 25  # the comparisons are of real lists, not of empty ones


CLUB_SEARCHES = [
    {}, {"q": "club 1"}, {"q": "BENGALURU"}, {"q": "kochi"}, {"q": "everyone"}, {"sort": "newest"}, {"sort": "popular", "limit": 3, "offset": 2},
    {"lat": BLR[0], "lng": BLR[1]}, {"lat": BLR[0], "lng": BLR[1], "sort": "nearby"}, {"lat": BLR[0], "lng": BLR[1], "radius_km": 0, "sort": "nearby"},
    {"lat": SUVA[0], "lng": SUVA[1], "radius_km": 300, "sort": "nearby"}, {"lat": KOCHI[0], "lng": KOCHI[1], "radius_km": 1000, "sort": "nearby", "offset": 3, "limit": 4},
]


async def test_club_search_answers_as_before(client, session):
    w = await _big_world(session)
    for params in CLUB_SEARCHES + [{"interest": w.interests[0].id}, {"interest": w.interests[2].id, "lat": BLR[0], "lng": BLR[1], "sort": "nearby"}]:
        r = await client.get(f"{API}/clubs", params=params)
        assert r.status_code == 200, (params, r.text)
        body = r.json()
        here = (params["lat"], params["lng"]) if "lat" in params else None
        want = await ref.search_clubs(
            session, here, q=params.get("q"), interest=params.get("interest"), radius_km=params.get("radius_km", 50),
            sort=params.get("sort", "popular"), offset=params.get("offset", 0), limit=params.get("limit", 24),
        )
        assert _ids(body["items"]) == want["ids"], params
        assert body["total"] == want["total"], params
        assert [x["distance_km"] for x in body["items"]] == want["distance"], params
        assert [x["followers"] for x in body["items"]] == want["followers"], params
        assert want["ids"] or params.get("q"), params


def _home_view(home):
    def ev(items):
        return [(x["id"], x["distance_km"], x["going"]) for x in items]

    return dict(
        near=ev(home["near"]), this_week=ev(home["this_week"]), online=ev(home["online"]), popular=ev(home["popular"]),
        clubs=[(x["id"], x["distance_km"], x["followers"]) for x in home["clubs"]], upcoming=home["stats"]["upcoming_events"],
    )


@pytest.mark.parametrize("cached", [False, True])
async def test_the_front_page_answers_as_before_wherever_the_visitor_is(client, session, cached):
    """With the minute cache on, many points in one cell share a cached pool:
    each must still get exactly its own answer."""
    await _big_world(session)
    cache.reset(enabled=cached)
    rng = random.Random(3)
    points = [None, BLR, KOCHI, SUVA, TAVEUNI, LONGYEARBYEN]
    # Points scattered over the cells around Bengaluru and Suva, corners included.
    for centre in (BLR, SUVA):
        c = geo.cell(*centre)
        lat0, lng0 = c[0] * geo.CELL_DEG, c[1] * geo.CELL_DEG
        points += [(lat0 + geo.CELL_DEG * rng.random(), lng0 + geo.CELL_DEG * rng.random()) for _ in range(12)]
        points += [(lat0 + 1e-9, lng0 + 1e-9), (lat0 + geo.CELL_DEG - 1e-9, lng0 + geo.CELL_DEG - 1e-9)]
    for here in points:
        params = {"lat": here[0], "lng": here[1]} if here else {}
        r = await client.get(f"{API}/home", params=params)
        assert r.status_code == 200, r.text
        want = await ref.home(session, here, now=service.now_utc())
        got = _home_view(r.json())
        for key in ("near", "this_week", "online", "popular", "clubs", "upcoming"):
            assert got[key] == want[key], (here, key)
        assert got["this_week"] and got["popular"] and got["clubs"] and (here is None or here == LONGYEARBYEN or got["near"]), here
    cache.reset(enabled=False)


async def test_for_you_ranks_and_scores_as_before(client, session, mint_token):
    w = await _big_world(session)
    # A history and ties: registrations on past events of a few clubs, and an organisation shared with one club.
    org_user = w.users[0]
    org_club = w.clubs[1]
    from app.api.orgs.models import Organizations, OrgTypes

    org = Organizations(name="Scale College", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    org_club.org_id = org.id
    (await session.scalar(select(UserProfiles).where(UserProfiles.user_id == org_user.id))).org_id = org.id
    await session.commit()
    for user in w.users[:8]:
        for offset in (0, 7):
            want_ids, want_scores = await ref.for_you(session, user.id, limit=8, offset=offset)
            got, scores = await recommendations.for_you_events(session, user.id, limit=8, offset=offset)
            assert [e.id for e in got] == want_ids, (user.id, offset)
            assert scores == pytest.approx(want_scores, abs=1e-9), (user.id, offset)
    r = await client.get("/api/v1/events/for-you?limit=5", headers=auth_header(await mint_token(org_user)))
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["items"]] == (await ref.for_you(session, org_user.id, limit=6))[0][:5]


# ─── Distance at the edges ───────────────────────────────────────────────


async def test_the_radius_edge_the_antimeridian_and_events_without_a_point(client, session):
    w = World(session, seed=1)
    await w.base(n_users=2)
    [club] = await w.clubs_at([("Edge Club", (0.0, 179.9), True)])
    start = w.now + timedelta(days=2)
    centre = (0.0, 179.9)

    async def at(name, point, **kw):
        e = Events(slug=f"edge-{uuid.uuid4().hex[:8]}", name=name, images=[], event_datetime=start + timedelta(minutes=len(w.events)),
                   duration=2.0, club_id=club.id, category_id=w.categories[0].id, lat=point[0] if point else None, lng=point[1] if point else None,
                   lifecycle=EventStatus.published, **kw)
        session.add(e)
        w.events.append(e)
        return e

    inside_east = await at("Inside across the line", _destination(*centre, math.pi / 2, 49.99))
    outside_east = await at("Outside across the line", _destination(*centre, math.pi / 2, 50.01))
    inside_north = await at("Inside north", _destination(*centre, 0.0, 49.99))
    outside_south = await at("Outside south", _destination(*centre, math.pi, 50.01))
    close = await at("Close by", _destination(*centre, 1.0, 3.0))
    online = await at("Online", (0.0, 179.9), is_online=True)
    no_point = await at("No point", None)
    await session.commit()
    assert inside_east.lng < -179  # it really is across the antimeridian

    body = (await client.get(f"{API}/events", params={"lat": centre[0], "lng": centre[1], "radius_km": 50, "sort": "distance"})).json()
    assert _ids(body["items"]) == [close.id, inside_north.id, inside_east.id, online.id, no_point.id]
    assert body["total"] == 5
    assert outside_east.id not in _ids(body["items"]) and outside_south.id not in _ids(body["items"])
    assert [x["distance_km"] for x in body["items"]] == [3.0, 50.0, 50.0, None, None]
    paged = [_ids((await client.get(f"{API}/events", params={"lat": centre[0], "lng": centre[1], "radius_km": 50, "sort": "distance", "limit": 2, "offset": o})).json()["items"]) for o in (0, 2, 4)]
    assert sum(paged, []) == _ids(body["items"])


# ─── Words ───────────────────────────────────────────────────────────────


async def test_words_match_as_substrings_and_a_typo_is_forgiven(client, session):
    w = World(session, seed=2)
    await w.base(n_users=1)
    [club] = await w.clubs_at([("Circuit Society", BLR, True)])
    start = w.now + timedelta(days=3)

    async def ev(name, **kw):
        e = Events(slug=f"w-{uuid.uuid4().hex[:8]}", name=name, images=[], event_datetime=start + timedelta(minutes=len(w.events)), duration=2.0,
                   club_id=club.id, category_id=kw.pop("category_id", w.categories[1].id), lifecycle=EventStatus.published, **kw)
        session.add(e)
        w.events.append(e)
        return e

    robots = await ev("Advanced Robotics Workshop", location_name="Lab 3")
    corner = await ev("Evening Social", location_name="Jazz Corner")
    jazz = await ev("Jazz on the Lawn", location_name="Open Air Theatre")
    quiz = await ev("Pub Quiz", location_name="The 100% Cafe", category_id=w.categories[4].id)
    await session.commit()

    async def ids(**params):
        r = await client.get(f"{API}/events", params=params)
        assert r.status_code == 200, r.text
        return _ids(r.json()["items"])

    assert await ids(q="OBOT") == [robots.id]  # inside a word, any case
    assert await ids(q="open air") == [jazz.id]  # the place
    assert await ids(q="circuit") == [robots.id, corner.id, jazz.id, quiz.id]  # the club
    assert await ids(q="quiz") == [quiz.id]  # the name and the category both
    assert await ids(q="100%") == [quiz.id] and await ids(q="1%") == []  # % is a character, not "anything"
    # A typo: nothing has these letters in order, so near words are tried.
    assert await ids(q="robtics") == [robots.id]
    assert await ids(q="circut society") == [robots.id, corner.id, jazz.id, quiz.id]
    # Not for two letters, and never when the substring already found something.
    assert await ids(q="zq") == []
    assert await ids(q="jazz") == [corner.id, jazz.id]  # soonest first, as always
    # Best match first, when asked: the name before the place.
    assert await ids(q="jazz", sort="relevance") == [jazz.id, corner.id]
    clubs = (await client.get(f"{API}/clubs", params={"q": "circit"})).json()
    assert _ids(clubs["items"]) == [club.id] and clubs["total"] == 1


# ─── Bounded statements ──────────────────────────────────────────────────


async def test_statements_per_request_do_not_grow_with_the_number_of_events(client, session, statements, mint_token):
    w = World(session, seed=4)
    await w.base(n_users=12)
    clubs = await w.clubs_at([(f"Bounded Club {i}", _near(w.rng, BLR, 10), i % 2 == 0) for i in range(5)])
    token = auth_header(await mint_token(w.users[0]))
    # Five events that fill every section of every page: near, this week,
    # online, popular, no point, paid, elsewhere.
    day = timedelta(days=1)
    first = [
        dict(name="Robotics Near", lat=BLR[0] + 0.02, lng=BLR[1], event_datetime=w.now + day, page_views=9),
        dict(name="Online Talk", is_online=True, event_datetime=w.now + 2 * day),
        dict(name="No Point Jam", event_datetime=w.now + 3 * day),
        dict(name="Paid Robotics", lat=BLR[0], lng=BLR[1] + 0.05, has_fee=True, reg_fee=99, event_datetime=w.now + 10 * day),
        dict(name="Kochi Meetup", lat=KOCHI[0], lng=KOCHI[1], event_datetime=w.now + 4 * day),
    ]
    # The first event's club holds the second (more from the club), and the
    # third shares its category (similar), so its page has every part too.
    homes = [(0, 0), (0, 1), (2, 0), (3, 3), (4, 4)]
    for (club_i, cat_i), kw in zip(homes, first):
        e = Events(slug=f"b-{uuid.uuid4().hex[:8]}", images=[], duration=2.0, club_id=clubs[club_i].id, category_id=w.categories[cat_i].id, lifecycle=EventStatus.published, **kw)
        session.add(e)
        w.events.append(e)
    # And one the first club held, so its past list has a page from the start.
    session.add(Events(slug=f"b-{uuid.uuid4().hex[:8]}", name="Held Before", images=[], duration=2.0, club_id=clubs[0].id, category_id=w.categories[0].id, lifecycle=EventStatus.closed, event_datetime=w.now - 5 * day))
    await session.flush()
    for e in w.events:
        session.add(EventInterestsLink(event_id=e.id, interest_id=w.interests[0].id))
    await w.activity()
    thread = ClubThreads(id=uuid.uuid4(), club_id=clubs[0].id, body="A poll", kind="poll", created_at=w.now - timedelta(hours=1))
    session.add(thread)
    await session.flush()
    session.add(ThreadPollOptions(thread_id=thread.id, label="Yes", sort_order=0))
    await session.commit()
    page_slug = w.events[0].slug
    today = w.now.astimezone(service.IST).date()

    paths = [
        (f"{API}/home", {}, {}), (f"{API}/home", {"lat": BLR[0], "lng": BLR[1]}, {}), (f"{API}/home", {}, token),
        (f"{API}/events", {}, {}), (f"{API}/events", {"q": "robotics"}, {}), (f"{API}/events", {"q": "zzzzzz"}, {}),
        (f"{API}/events", {"lat": BLR[0], "lng": BLR[1], "sort": "distance"}, {}), (f"{API}/events", {"sort": "popular"}, {}),
        (f"{API}/events", {"when": "this_month", "type": "in_person"}, {}),
        (f"{API}/clubs", {}, {}), (f"{API}/clubs", {"lat": BLR[0], "lng": BLR[1], "sort": "nearby"}, {}), (f"{API}/clubs", {"q": "bounded"}, {}),
        (f"{API}/clubs/{clubs[0].slug}", {}, {}), (f"{API}/events/{page_slug}", {}, {}), (f"{API}/threads", {}, {}),
        (f"{API}/categories", {}, {}), (f"{API}/sitemap", {}, {}),
        (f"{API}/clubs/{clubs[0].slug}/events", {"when": "upcoming"}, {}), (f"{API}/clubs/{clubs[0].slug}/events", {"when": "past"}, {}),
        (f"{API}/events", {"from": today.isoformat(), "to": (today + timedelta(days=30)).isoformat()}, {}),
        (f"{API}/calendar", {"month": today.strftime("%Y-%m")}, {}),
        (f"{API}/calendar", {"month": today.strftime("%Y-%m"), "lat": BLR[0], "lng": BLR[1], "q": "robotics"}, {}),
        ("/api/v1/events/for-you", {}, token), ("/api/v1/events/list", {"is_ended": "false"}, token),
        ("/api/v1/events/list", {"search": "a"}, token), ("/api/v1/events/list", {"interest_ids": str(w.interests[0].id)}, token),
    ]

    async def counts():
        out = []
        for path, params, headers in paths:
            statements.clear()
            r = await client.get(path, params=params, headers=headers)
            assert r.status_code == 200, (path, r.text)
            out.append(len(statements))
        return out

    few = await counts()
    for c in clubs:
        await w.events_for(c, BLR, 11)
    await w.activity()
    await session.commit()
    many = await counts()
    assert len(w.events) == 60
    table = list(zip([p for p, *_ in paths], few, many))
    # Never more with more events (a paged list can need one query fewer
    # once its first part fills the page), and never many at all.
    assert all(m <= f for _, f, m in table), [t for t in table if t[2] > t[1]]
    assert sum(1 for _, f, m in table if m == f) >= len(table) - 2, table
    # The most is the front page for a place with nothing cached (26): every
    # section, the nearest clubs' cards and the categories, a query or two each.
    assert max(few) <= 30, table


# ─── The counters ────────────────────────────────────────────────────────


async def _check_counts(session):
    """Every event's going_count and club's follower_count, against the rows."""
    assert await counters.drift(session) == {"events": [], "clubs": []}


async def test_going_and_follower_counts_stay_exact_through_every_kind_of_write(client, session, make_club, make_event, make_user, mint_token):
    owner, club = await make_club(name="Counted Club", email="counted@example.com")
    other_owner, other_club = await make_club(name="Other Club", email="counted-other@example.com")
    start = datetime.now(timezone.utc) + timedelta(days=4)
    a = await make_event(club=club, created_by=owner, name="Counted A", event_datetime=start, lifecycle=EventStatus.published)
    b = await make_event(club=club, created_by=owner, name="Counted B", event_datetime=start, lifecycle=EventStatus.published)
    users = [await make_user(email=f"counted-{i}@example.com") for i in range(6)]
    await session.commit()

    async def going(e):
        return await session.scalar(select(Events.going_count).where(Events.id == e.id))

    async def followers(c):
        return await session.scalar(select(Clubs.follower_count).where(Clubs.id == c.id))

    # The app's own registration, signed in.
    for u in users[:3]:
        r = await client.post(f"/api/v1/events/registration/{a.id}/register", json={"full_name": u.full_name, "email": u.email, "phone": "9999999999"}, headers=auth_header(await mint_token(u)))
        assert r.status_code == 200, r.text
    assert await going(a) == 3
    await _check_counts(session)

    # The organiser takes one off the list.
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == a.id, EventRegistrationsLink.user_id == users[0].id))
    r = await client.delete(f"/api/v1/events/registration/{a.id}/info/{reg.id}", params={"confirm_slug": a.slug}, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    assert await going(a) == 2

    # ORM add, soft delete, restore, move between events, hard delete.
    extra = EventRegistrationsLink(event_id=b.id, user_id=users[3].id, full_name="x", email="x@example.com", ticket_id=f"T-{uuid.uuid4().hex[:10]}")
    session.add(extra)
    await session.flush()
    assert (await going(a), await going(b)) == (2, 1)
    extra.is_deleted = True
    await session.flush()
    assert await going(b) == 0
    extra.is_deleted = False
    await session.flush()
    assert await going(b) == 1
    extra.event_id = a.id
    await session.flush()
    assert (await going(a), await going(b)) == (3, 0)
    await session.delete(extra)
    await session.flush()
    assert await going(a) == 2
    # Bulk and raw SQL, which no application hook would see.
    await session.execute(update(EventRegistrationsLink).where(EventRegistrationsLink.event_id == a.id).values(is_deleted=True).execution_options(synchronize_session=False))
    assert await going(a) == 0
    await session.execute(text("UPDATE event_registrations_link SET is_deleted = false WHERE event_id = :e AND user_id = :u"), {"e": a.id, "u": users[1].id})
    await session.execute(text("INSERT INTO event_registrations_link (event_id, user_id, full_name, email, ticket_id, role, is_attended, is_won, is_paid, paid_amount, actual_amount, certificate_design_version, created_at, updated_at, is_deleted) "
                               "VALUES (:e, :u, 'Raw', 'raw@example.com', :t, 'attendee', false, false, false, 0, 0, 0, now(), now(), false)"), {"e": b.id, "u": users[4].id, "t": f"T-{uuid.uuid4().hex[:10]}"})
    assert (await going(a), await going(b)) == (1, 1)
    await session.execute(text("DELETE FROM event_registrations_link WHERE event_id = :e"), {"e": b.id})
    assert await going(b) == 0
    await _check_counts(session)

    # Follows: the app's endpoints, then flags flipped and rows removed by hand.
    for u in users[:4]:
        assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=auth_header(await mint_token(u)))).status_code == 200
    assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=auth_header(await mint_token(users[0])))).status_code in (200, 400, 409)
    assert await followers(club) == 4
    assert (await client.post(f"/api/v1/clubs/unfollow/{club.id}", headers=auth_header(await mint_token(users[1])))).status_code == 200
    assert await followers(club) == 3
    await session.execute(update(ClubUsersLink).where(ClubUsersLink.club_id == club.id, ClubUsersLink.user_id == users[2].id).values(club_id=other_club.id).execution_options(synchronize_session=False))
    assert (await followers(club), await followers(other_club)) == (2, 1)
    await session.execute(text("UPDATE club_users_link SET is_deleted = true WHERE club_id = :c"), {"c": club.id})
    assert await followers(club) == 0
    await session.execute(text("DELETE FROM club_users_link WHERE club_id = :c"), {"c": other_club.id})
    assert await followers(other_club) == 0
    await _check_counts(session)


@pytest.mark.parametrize("vouched_by", ["google", "verified_code"])
async def test_the_guest_merge_keeps_the_counts_exact(session, make_club, make_event, make_user, vouched_by):
    """Into an account Google vouched for, or a password account whose owner
    typed back the code sent to the address (merges_into)."""
    from app.api.users import service as users_service
    from app.api.users.models import SigninProviders

    owner, club = await make_club(name="Merge Count Club", email="merge-count@example.com")
    event = await make_event(club=club, created_by=owner, name="Merge Count", event_datetime=datetime.now(timezone.utc) + timedelta(days=3))
    app_user = await make_user(email="merger@example.com", username=f"merger-{uuid.uuid4().hex[:6]}")
    if vouched_by == "google":
        app_user.provider = SigninProviders.google
    else:
        app_user.email_verified_at = datetime.now(timezone.utc)
    assert users_service.merges_into(app_user)
    guest = await make_user(email="MERGER@example.com", user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:6]}")
    for u in (app_user, guest):
        session.add(EventRegistrationsLink(event_id=event.id, user_id=u.id, full_name="M", email=u.email, ticket_id=f"T-{uuid.uuid4().hex[:10]}"))
    await session.flush()
    assert await session.scalar(select(Events.going_count).where(Events.id == event.id)) == 2
    summary = await users_service.merge_guest_into_user(session, app_user)
    assert summary["registrations_retired"] == 1
    assert await session.scalar(select(Events.going_count).where(Events.id == event.id)) == 1
    await _check_counts(session)


async def test_a_counter_put_out_by_hand_is_found_and_recounted(client, session, make_club, make_event, make_user, make_admin, mint_token):
    owner, club = await make_club(name="Drift Club", email="drift@example.com")
    event = await make_event(club=club, created_by=owner, name="Drift", event_datetime=datetime.now(timezone.utc) + timedelta(days=2))
    u = await make_user(email="drift-u@example.com")
    session.add(EventRegistrationsLink(event_id=event.id, user_id=u.id, full_name="D", email=u.email, ticket_id=f"T-{uuid.uuid4().hex[:10]}"))
    session.add(ClubUsersLink(club_id=club.id, user_id=u.id, is_following=True))
    await session.flush()
    # What a data-only restore with triggers off can leave behind.
    await session.execute(text("UPDATE events SET going_count = 40 WHERE id = :i"), {"i": event.id})
    await session.execute(text("UPDATE clubs SET follower_count = 0 WHERE id = :i"), {"i": club.id})
    assert await counters.drift(session) == {"events": [event.id], "clubs": [club.id]}
    assert (await client.post("/api/v1/admin/discover/reconcile-counters", headers=auth_header(await mint_token(owner)))).status_code == 403
    admin = await make_admin(email="drift-admin@example.com")
    r = await client.post("/api/v1/admin/discover/reconcile-counters", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200, r.text
    assert r.json() == {"events": 1, "clubs": 1}
    assert await session.scalar(select(Events.going_count).where(Events.id == event.id)) == 1
    assert await session.scalar(select(Clubs.follower_count).where(Clubs.id == club.id)) == 1
    assert (await client.post("/api/v1/admin/discover/reconcile-counters", headers=auth_header(await mint_token(admin)))).json() == {"events": 0, "clubs": 0}
    # When it runs on its own (every six hours, one process): tests/test_write_path.py.


# ─── The front page's minute cache ───────────────────────────────────────


async def test_the_shared_front_page_never_carries_the_readers_likes_or_votes(client, session, shared_cache, statements, mint_token):
    w = World(session, seed=9)
    await w.base(n_users=2)
    [club] = await w.clubs_at([("Liked Club", BLR, True)])
    await w.events_for(club, BLR, 4)
    post = ClubThreads(id=uuid.uuid4(), club_id=club.id, body="Which night?", kind="poll", created_at=w.now - timedelta(hours=1))
    session.add(post)
    await session.flush()
    option = ThreadPollOptions(thread_id=post.id, label="Friday", sort_order=0)
    session.add(option)
    await session.flush()
    reader = w.users[0]
    session.add(ThreadLikes(thread_id=post.id, user_id=reader.id))
    session.add(ThreadVotes(thread_id=post.id, option_id=option.id, user_id=reader.id))
    await session.commit()

    anon = (await client.get(f"{API}/home")).json()["threads"][0]
    assert anon["liked"] is False and anon["poll"]["voted_option_id"] is None
    statements.clear()
    mine = (await client.get(f"{API}/home", headers=auth_header(await mint_token(reader)))).json()["threads"][0]
    assert mine["liked"] is True and mine["poll"]["voted_option_id"] == str(option.id)
    # The shared part came from the cache: only the reader's own likes and votes were read.
    assert [s for s in statements if "thread_likes" in s or "thread_votes" in s] and not [s for s in statements if "FROM events" in s]
    again = (await client.get(f"{API}/home")).json()["threads"][0]
    assert again["liked"] is False and again["poll"]["voted_option_id"] is None
    other = (await client.get(f"{API}/home", headers=auth_header(await mint_token(w.users[1])))).json()["threads"][0]
    assert other["liked"] is False and other["poll"]["voted_option_id"] is None


async def test_the_minute_cache_expires_and_a_new_event_shows_within_the_minute(client, session, shared_cache, monkeypatch):
    w = World(session, seed=10)
    await w.base(n_users=1)
    [club] = await w.clubs_at([("Fresh Club", BLR, True)])
    await session.commit()
    now = service.now_utc()
    first = await service.home(session, {"lat": None, "lng": None}, None, now=now)
    assert first["this_week"] == []
    late = Events(slug=f"late-{uuid.uuid4().hex[:6]}", name="Just Published", images=[], event_datetime=now + timedelta(days=1), duration=2.0,
                  club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.published)
    session.add(late)
    await session.commit()
    # Within the same minute the page is the one already made...
    assert (await service.home(session, {"lat": None, "lng": None}, None, now=now))["this_week"] == []
    # ...and the next minute is a new key.
    later = await service.home(session, {"lat": None, "lng": None}, None, now=now + timedelta(seconds=61))
    assert _ids(later["this_week"]) == [late.id]
    # Entries also expire by age, and the cache holds no more than its cap.
    small = cache.TTLCache(ttl=10, maxsize=3)
    clock = [100.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock[0])
    small.put("a", 1)
    assert small.get("a") == 1
    clock[0] += 11
    assert small.get("a") is cache._MISSING
    for k in "bcde":
        small.put(k, k)
    assert len(small) == 3 and small.get("b") is cache._MISSING and small.get("e") == "e"


async def test_concurrent_misses_build_once(shared_cache):
    import asyncio

    calls = []

    async def build():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"built": len(calls)}

    got = await asyncio.gather(*(shared_cache.remember(("once",), build) for _ in range(5)))
    assert calls == [1] and all(g == {"built": 1} for g in got)


# ─── The status clock ────────────────────────────────────────────────────


async def test_the_status_clock_moves_exactly_what_its_rules_move(session, make_club, make_event, statements):
    owner, club = await make_club(name="Clock Club", email="clock@example.com")
    now = datetime.now(timezone.utc)
    h = timedelta(hours=1)
    cases = [
        dict(lifecycle=EventStatus.published, event_datetime=now + h),                       # not yet
        dict(lifecycle=EventStatus.published, event_datetime=now - h, duration=3.0),          # started
        dict(lifecycle=EventStatus.published, event_datetime=now - 30 * h, duration=1.0),     # started and long over: to closed
        dict(lifecycle=EventStatus.live, event_datetime=now - 5 * h, duration=2.0),           # within the four-hour grace
        dict(lifecycle=EventStatus.live, event_datetime=now - 7 * h, duration=2.0),           # past it
        dict(lifecycle=EventStatus.live, event_datetime=now - 4 * h, duration=2.0, close_after_minutes=60),
        dict(lifecycle=EventStatus.closed, event_datetime=now - 29 * 24 * h),                 # under a month
        dict(lifecycle=EventStatus.closed, event_datetime=now - 31 * 24 * h),                 # a month on: archived
        dict(lifecycle=EventStatus.closed, event_datetime=now - 31 * 24 * h, retention_mode=RetentionMode.delete),
        dict(lifecycle=EventStatus.closed, event_datetime=now - 2 * 24 * h, retention_mode=RetentionMode.delete),  # scrubbed: archived
        dict(lifecycle=EventStatus.closed, event_datetime=now - 2 * 24 * h, retention_mode=RetentionMode.delete),  # job pending
        dict(lifecycle=EventStatus.published, event_datetime=now - h, status_clock=False),    # off the clock
        dict(lifecycle=EventStatus.draft, event_datetime=now - h),
        dict(lifecycle=EventStatus.live, event_datetime=now - 9 * h, is_deleted=True),
    ]
    events = [await make_event(club=club, created_by=owner, name=f"Clock {i}", **kw) for i, kw in enumerate(cases)]
    for e, state in ((events[9], RetentionState.deleted), (events[10], RetentionState.pending)):
        session.add(RetentionJobs(event_id=e.id, state=state, run_at=now))
    await session.flush()
    jobs = {e.id: s for e, s in ((events[9], RetentionState.deleted), (events[10], RetentionState.pending))}
    expected = {e.id: (ref.clock_outcome(e, jobs.get(e.id), now) if e.status_clock and not e.is_deleted and e.lifecycle in (EventStatus.published, EventStatus.live, EventStatus.closed) else e.lifecycle) for e in events}
    moves = sum(1 for e in events if expected[e.id] is not e.lifecycle)
    statements.clear()
    assert await closeout.advance_clock(session, now=now) == moves
    assert len(statements) <= 4  # one read, the jobs, the write (not one per event)
    conn = await session.connection()
    table = Events.__table__
    for e in events:
        # Read past the soft-delete filter: the deleted one must not have moved either.
        assert (await conn.execute(select(table.c.status).where(table.c.id == e.id))).scalar() is expected[e.id], e.name


# ─── The app's lists ─────────────────────────────────────────────────────


async def test_the_app_list_filters_by_interest_once_each_in_date_order(client, session, mint_token):
    w = World(session, seed=12)
    await w.base(n_users=1)
    [club] = await w.clubs_at([("Interest Club", BLR, True)])
    start = w.now + timedelta(days=1)
    made = []
    for i in range(4):
        e = Events(slug=f"int-{uuid.uuid4().hex[:6]}", name=f"Interest {i}", images=[], event_datetime=start + timedelta(hours=i // 2), duration=1.0,
                   club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.published, additional_details=[{"key": "x"}])
        session.add(e)
        made.append(e)
    await session.flush()
    a, b = w.interests[0], w.interests[1]
    for e in made[:3]:
        session.add_all([EventInterestsLink(event_id=e.id, interest_id=a.id), EventInterestsLink(event_id=e.id, interest_id=b.id)])
    await session.commit()
    token = auth_header(await mint_token(w.users[0]))
    r = await client.get("/api/v1/events/list", params={"interest_ids": f"{a.id},{b.id}", "is_ended": "false", "limit": 10}, headers=token)
    assert r.status_code == 200, r.text  # was a 500: DISTINCT cannot compare json[]
    assert [x["id"] for x in r.json()["items"]] == [e.id for e in sorted(made[:3], key=lambda e: (e.event_datetime, e.id))]


async def test_the_sitemap_lists_public_pages_and_is_served_from_the_minute_cache(client, session, shared_cache, statements):
    w = World(session, seed=13)
    await w.base(n_users=1)
    [club] = await w.clubs_at([("Mapped Club", BLR, True)])
    [ghost] = await w.clubs_at([("Hidden Mapped Club", BLR, True)], verified=False)
    now = w.now
    shown = Events(slug=f"sm-{uuid.uuid4().hex[:6]}", name="On Soon", images=[], event_datetime=now + timedelta(days=2), duration=2.0, club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.published)
    held = Events(slug=f"sm-{uuid.uuid4().hex[:6]}", name="Held", images=[], event_datetime=now - timedelta(days=20), duration=2.0, club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.closed)
    draft = Events(slug=f"sm-{uuid.uuid4().hex[:6]}", name="Draft", images=[], event_datetime=now + timedelta(days=2), duration=2.0, club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.draft)
    old = Events(slug=f"sm-{uuid.uuid4().hex[:6]}", name="Long Ago", images=[], event_datetime=now - timedelta(days=400), duration=2.0, club_id=club.id, category_id=w.categories[0].id, lifecycle=EventStatus.archived)
    session.add_all([shown, held, draft, old])
    await session.commit()
    first = await client.get(f"{API}/sitemap")
    assert first.status_code == 200 and first.headers["content-type"].startswith("application/json")
    body = first.json()
    assert [e["slug"] for e in body["events"]] == [shown.slug, held.slug]
    assert [c["slug"] for c in body["clubs"]] == [club.slug] and ghost.slug not in str(body)
    assert all(set(e) == {"slug", "updated_at"} for e in body["events"] + body["clubs"])
    statements.clear()
    assert (await client.get(f"{API}/sitemap")).json() == body
    assert not [s for s in statements if "FROM events" in s]
