"""Which engine answers a search, and that the two answer alike.

- The breaker: open after N failures in a row, resting M seconds, then one
  trial at a time; a success closes it, a failed trial opens it again.
- A search with words asks Meilisearch (a fake, tests/fake_meili.py) when
  it is configured, closed and built; without words it never does. Any
  failure is answered by Postgres in the same request, with the same rows;
  after N failures Meilisearch is not even asked until it has rested.
- A document a little stale never shows what the public may not see: every
  page is read back under Postgres's own conditions.
- Paging: Meilisearch's cursors carry an offset, which Postgres continues
  when it has to; a Postgres cursor stays with Postgres; past MAX_HITS
  Postgres answers.
- Parity: on a world with every awkward case in it (drafts, deleted and
  closed events, events over, unverified and blocked clubs, online and
  in-person, free and paid, interests under topics, points at a radius's
  edge, across the antimeridian), a battery of queries with every filter
  finds the same rows (and totals) through Meilisearch as through Postgres;
  the order may differ where the two rank differently (relevance).
- The app's event and club lists, searched, likewise.
- Opt-in: with MEILI_TEST_URL (and MEILI_TEST_KEY) set, the parity battery
  runs against a real Meilisearch too (`brew install meilisearch`).
"""
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink
from app.api.discover import service
from app.api.events.models import EventCategories, EventInterestsLink, Events
from app.api.interests.models import InterestCategory, Interests
from app.api.orgs.models import Organizations, OrgTypes
from app.api.search import engine, sync
from app.config import settings
from app.core import search
from app.core.rate_limit import limiter
from app.db.listeners import add_loader_criteria
from tests import fake_meili
from tests.conftest import auth_header

API = "/api/v1"
KOCHI = (9.9312, 76.2673)
SUVA = (-18.1416, 178.4419)


@pytest.fixture(autouse=True)
def _as_production(session, monkeypatch):
    add_loader_criteria(session)
    monkeypatch.setattr(limiter, "enabled", False)


# ─── The breaker ──────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_the_breaker_opens_rests_and_tries_one_at_a_time():
    clock = Clock()
    b = search.Breaker(failures=3, seconds=30, clock=clock)
    assert b.allow() and b.state() == "closed"
    b.failure()
    b.failure()
    assert b.allow()  # two in a row: still trying
    b.success()
    b.failure()
    b.failure()
    assert b.allow()  # a success in between started the count again
    b.failure()
    assert b.state() == "open" and not b.allow()
    clock.t += 29
    assert not b.allow()
    clock.t += 2
    assert b.state() == "half-open"
    assert b.allow()  # the trial
    assert not b.allow()  # one at a time
    b.failure()  # the trial failed: resting again
    assert b.state() == "open" and not b.allow()
    clock.t += 31
    assert b.allow()
    b.success()
    assert b.state() == "closed" and b.allow() and b.allow()


def test_a_trial_that_never_reports_back_is_given_up():
    clock = Clock()
    b = search.Breaker(failures=1, seconds=10, clock=clock)
    b.failure()
    clock.t += 11
    assert b.allow() and not b.allow()
    clock.t += b.trial_seconds + 1
    assert b.allow()


# ─── A world ──────────────────────────────────────────────────────────────


def _dest(lat, lng, km, bearing):
    from tests.test_geo_cells import _dest as dest

    return dest(lat, lng, km, bearing)


@pytest.fixture
async def world(session, make_club):
    """Events and clubs with every awkward case, worded so that a word is
    found the same way by a substring (Postgres) and by a word or its prefix
    (Meilisearch)."""
    rng = random.Random(5)
    tag = uuid.uuid4().hex[:5]
    now = datetime.now(timezone.utc)
    blocked_org = Organizations(name=f"Blocked {tag}", type=OrgTypes.college, is_blocked=True)
    session.add(blocked_org)
    await session.flush()
    _, kochi = await make_club(name=f"Malabar Makers {tag}", email=f"r-kochi-{tag}@example.com")
    kochi.location_name, kochi.lat, kochi.lng = "Kochi", *KOCHI
    _, suva = await make_club(name=f"Pacific Players {tag}", email=f"r-suva-{tag}@example.com")
    suva.location_name, suva.lat, suva.lng = "Suva", *SUVA
    _, hidden = await make_club(name=f"Quiet Guild {tag}", email=f"r-hidden-{tag}@example.com", is_verified=False)
    _, blocked = await make_club(name=f"Blocked Brigade {tag}", email=f"r-blocked-{tag}@example.com")
    blocked.org_id = blocked_org.id
    topic_tech = InterestCategory(name=f"Engineering {tag}", slug=f"engineering-{tag}")
    topic_arts = InterestCategory(name=f"Performing {tag}", slug=f"performing-{tag}")
    session.add_all([topic_tech, topic_arts])
    await session.flush()
    rob = Interests(name=f"Robotics {tag}", slug=f"robotics-{tag}", category_id=topic_tech.id)
    dro = Interests(name=f"Drones {tag}", slug=f"drones-{tag}", category_id=topic_tech.id)
    mus = Interests(name=f"Music {tag}", slug=f"music-{tag}", category_id=topic_arts.id)
    workshop = EventCategories(name="Workshop")
    concert = EventCategories(name="Concert")
    session.add_all([rob, dro, mus, workshop, concert])
    await session.flush()
    session.add_all([ClubInterestsLink(club_id=kochi.id, interest_id=rob.id), ClubInterestsLink(club_id=suva.id, interest_id=mus.id)])

    names = ["Rover Hackathon", "Drone Derby", "Music Festival", "Robotics Bootcamp", "Jazz Evening", "Coding Sprint", "Startup Pitch",
             "Robot Rumble", "Hackathon Finals", "Festival Parade"]
    venues = ["Lab Block, Kochi", "Marine Drive, Kochi", "Albert Park, Suva", "Online", None]
    events = []
    clubs_cycle = [kochi, suva, hidden, blocked, kochi, suva]
    for n in range(48):
        club = clubs_cycle[n % len(clubs_cycle)]
        centre = KOCHI if club is not suva else SUVA
        if n % 7 == 0:
            point = _dest(*centre, 50.0 + (0.004 if n % 2 else -0.004), rng.uniform(0, 360))  # at the radius's edge
        elif n % 5 == 0:
            point = (None, None)
        else:
            point = _dest(*centre, rng.uniform(0, 140), rng.uniform(0, 360))
        lifecycle = [EventStatus.published, EventStatus.published, EventStatus.live, EventStatus.draft, EventStatus.closed, EventStatus.published][n % 6]
        start = now + timedelta(hours=rng.choice([-30, -1, 2, 20, 50, 100, 200, 400, 900]))
        if n % 11 == 0:  # at a calendar day's first instant in India: where a date range starts
            day = (now + timedelta(days=4)).astimezone(service.IST).date()
            start = service.day_start(day)
        e = Events(
            slug=f"r{n}-{tag}", name=f"{names[n % len(names)]} {n}", images=[], event_datetime=start, duration=rng.choice([0.0, 1.0, 3.0, 26.0]),
            club_id=club.id, category_id=(workshop if n % 3 else concert).id, location_name=venues[n % len(venues)],
            lat=point[0], lng=point[1], is_online=(n % 4 == 0), has_fee=(n % 5 == 1), reg_fee=(250 if n % 5 == 1 else None),
            lifecycle=lifecycle, is_deleted=(n % 17 == 0), event_tag=("Featured" if n % 8 == 0 else None),
        )
        events.append(e)
    session.add_all(events)
    await session.flush()
    for n, e in enumerate(events):
        for interest in ([rob] if n % 2 == 0 else []) + ([dro] if n % 3 == 0 else []) + ([mus] if "Music" in e.name or "Jazz" in e.name else []):
            session.add(EventInterestsLink(event_id=e.id, interest_id=interest.id))
    await session.commit()
    return dict(tag=tag, now=now, kochi=kochi, suva=suva, hidden=hidden, blocked=blocked, events=events, rob=rob, dro=dro, mus=mus,
                topic_tech=topic_tech, topic_arts=topic_arts, workshop=workshop, concert=concert)


@pytest.fixture
async def meili(monkeypatch, session):
    """Search on (the fake, or a real instance with MEILI_TEST_URL), the indexes built from the world."""
    real = os.environ.get("MEILI_TEST_URL")
    if real:
        monkeypatch.setattr(settings, "SEARCH_URL", real)
        monkeypatch.setattr(settings, "SEARCH_KEY", os.environ.get("MEILI_TEST_KEY", ""))
        monkeypatch.setattr(settings, "SEARCH_INDEX_PREFIX", f"t{uuid.uuid4().hex[:6]}_")
        monkeypatch.setattr(settings, "SEARCH_TIMEOUT", 5.0)
        await search.reset()
        engine.reset()
        sync.reset()
        fake = None
    else:
        fake = await fake_meili.install(monkeypatch)
    yield fake
    if real:
        for kind in search.KINDS:
            await search.delete_index(search.index(kind))
    await search.reset()
    engine.reset()
    sync.reset()


async def _built(session):
    await sync.ensure_built(session)
    await sync.drain(session)
    engine.reset()


def _off(monkeypatch):
    """Postgres only, for the same request."""
    monkeypatch.setattr(settings, "SEARCH_URL", "")
    engine.reset()


def _on(monkeypatch, url):
    monkeypatch.setattr(settings, "SEARCH_URL", url)
    engine.reset()


async def _all(client, path, params, cap=12) -> tuple[list[int], int]:
    """Every page of a list (by its cursors): the ids in order, and the first page's total."""
    got, total, after = [], None, None
    for _ in range(cap):
        p = dict(params, limit=60) if after is None else dict(params, limit=60, after=after)
        r = await client.get(path, params=p)
        assert r.status_code == 200, r.text
        body = r.json()
        total = body["total"] if total is None else total
        got += [i["id"] for i in body["items"]]
        after = body.get("next_cursor")
        if not after:
            break
    return got, total


# ─── Routing and fallback ─────────────────────────────────────────────────


async def test_words_ask_meilisearch_and_no_words_never_do(client, session, world, meili):
    if meili is None:
        pytest.skip("counts the fake's requests")
    await _built(session)
    before = len(meili.searches())
    body = (await client.get(f"{API}/discover/events", params={"q": "hackathon", "radius_km": 0})).json()
    assert len(meili.searches()) == before + 1 and body["items"]
    await client.get(f"{API}/discover/events", params={"radius_km": 0})
    await client.get(f"{API}/discover/events", params={"topic": world["topic_tech"].slug, "radius_km": 0})
    assert len(meili.searches()) == before + 1


async def test_a_failure_is_answered_by_postgres_and_the_breaker_rests_it(client, session, world, meili, monkeypatch):
    if meili is None:
        pytest.skip("breaks the fake")
    await _built(session)
    params = {"q": "robotics", "radius_km": 0, "sort": "date"}
    want = (await client.get(f"{API}/discover/events", params=params)).json()
    meili.down = True
    for n in range(settings.SEARCH_BREAKER_FAILURES):
        got = (await client.get(f"{API}/discover/events", params=params)).json()
        assert [i["id"] for i in got["items"]] == [i["id"] for i in want["items"]] and got["total"] == want["total"]
    assert search.breaker.state() == "open"
    asked = len(meili.requests)
    got = (await client.get(f"{API}/discover/events", params=params)).json()
    assert len(meili.requests) == asked  # resting: not even asked
    assert [i["id"] for i in got["items"]] == [i["id"] for i in want["items"]]
    # Rested: one trial, which finds it well, and it answers again.
    meili.down = False
    monkeypatch.setattr(search.breaker, "open_until", 0.0)
    await client.get(f"{API}/discover/events", params=params)
    assert search.breaker.state() == "closed" and len(meili.requests) > asked
    # A timeout is a failure too, answered the same way.
    meili.slow = True
    got = (await client.get(f"{API}/discover/events", params=params)).json()
    assert [i["id"] for i in got["items"]] == [i["id"] for i in want["items"]] and search.breaker.failures == 1


async def test_a_stale_document_never_shows_what_the_public_may_not_see(client, session, world, meili):
    await _built(session)
    body = (await client.get(f"{API}/discover/events", params={"q": "festival", "radius_km": 0, "sort": "date"})).json()
    first = next(e for e in world["events"] if e.id == body["items"][0]["id"])
    first.lifecycle = EventStatus.draft  # unpublished; the outbox not drained yet
    await session.commit()
    body = (await client.get(f"{API}/discover/events", params={"q": "festival", "radius_km": 0, "sort": "date"})).json()
    assert first.id not in [i["id"] for i in body["items"]]


async def test_paging_carries_on_across_the_two_engines(client, session, world, meili, monkeypatch):
    await _built(session)
    url = settings.SEARCH_URL
    params = {"q": "kochi", "radius_km": 0, "sort": "date", "limit": 3}
    pg_pages = []
    _off(monkeypatch)
    for offset in (0, 3, 6):
        pg_pages.append([i["id"] for i in (await client.get(f"{API}/discover/events", params=dict(params, offset=offset))).json()["items"]])
    _on(monkeypatch, url)
    first = (await client.get(f"{API}/discover/events", params=params)).json()
    assert [i["id"] for i in first["items"]] == pg_pages[0]
    assert engine.offset_of(first["next_cursor"], "events:date") == 3  # Meilisearch's cursor: an offset
    second = (await client.get(f"{API}/discover/events", params=dict(params, after=first["next_cursor"]))).json()
    assert [i["id"] for i in second["items"]] == pg_pages[1]
    # Meilisearch goes away mid-way: Postgres carries on from its cursor.
    _off(monkeypatch)
    third = (await client.get(f"{API}/discover/events", params=dict(params, after=second["next_cursor"]))).json()
    assert [i["id"] for i in third["items"]] == pg_pages[2]
    assert third["next_cursor"] is None or engine.offset_of(third["next_cursor"], "events:date") is None  # Postgres's own from here
    # And a Postgres cursor stays with Postgres even once Meilisearch is back.
    _on(monkeypatch, url)
    if third["next_cursor"]:
        before = len(meili.searches()) if meili is not None else 0
        await client.get(f"{API}/discover/events", params=dict(params, after=third["next_cursor"]))
        if meili is not None:
            assert len(meili.searches()) == before
    # Past MAX_HITS deep, Postgres answers.
    assert await engine.discover_events(
        session, {}, q="kochi", here=None, when="any", category=None, type_="any", price="any", radius_km=0, sort="date",
        offset=engine.MAX_HITS, limit=10, now=world["now"], since=world["now"], days=None, topic=None, interest=None, after=None,
    ) is None


# ─── Parity ───────────────────────────────────────────────────────────────

QUERIES = ["hackathon", "festival", "robotics", "rover hackathon", "drone derby", "kochi", "suva", "workshop", "concert", "featured",
           "malabar", "pacific", "jazz", "music", "engineering", "performing", "bootcamp sprint", "startup", "parade", "robtics"]


def _filters(world):
    tech, arts = world["topic_tech"], world["topic_arts"]
    return [
        {},
        {"when": "today"}, {"when": "this_week"}, {"when": "this_weekend"}, {"when": "next_week"}, {"when": "this_month"},
        {"from": (world["now"] + timedelta(days=2)).date().isoformat(), "to": (world["now"] + timedelta(days=12)).date().isoformat()},
        {"to": (world["now"] + timedelta(days=3)).date().isoformat()},
        {"from": (world["now"] + timedelta(days=4)).astimezone(service.IST).date().isoformat()},
        {"category": world["workshop"].id}, {"category": world["concert"].id},
        {"topic": tech.slug}, {"topic": str(arts.id)}, {"topic": "no-such-topic"}, {"interest": world["dro"].id},
        {"interest": world["mus"].id, "topic": tech.slug},
        {"type": "online"}, {"type": "in_person"}, {"price": "free"}, {"price": "paid"},
        {"lat": KOCHI[0], "lng": KOCHI[1], "radius_km": 50}, {"lat": KOCHI[0], "lng": KOCHI[1], "radius_km": 50, "sort": "distance"},
        {"lat": SUVA[0], "lng": SUVA[1], "radius_km": 120, "type": "in_person"},
        {"lat": -16.85, "lng": -179.95, "radius_km": 300},  # across the antimeridian
        {"sort": "popular"}, {"sort": "newest"}, {"sort": "relevance"},
    ]


async def test_meilisearch_finds_what_postgres_finds(client, session, world, meili, monkeypatch):
    await _built(session)
    url = settings.SEARCH_URL
    compared = 0
    for q in QUERIES:
        for f in _filters(world):
            params = {"radius_km": 0, **f, "q": q}
            _off(monkeypatch)
            pg, pg_total = await _all(client, f"{API}/discover/events", params)
            _on(monkeypatch, url)
            ms, ms_total = await _all(client, f"{API}/discover/events", params)
            assert sorted(ms) == sorted(pg), (q, f)
            assert ms_total == pg_total, (q, f)
            if f.get("sort") in ("date", "popular", "newest"):
                assert ms == pg, (q, f)  # an explicit order is the same order
            compared += 1
    assert compared == len(QUERIES) * len(_filters(world))
    if meili is not None:
        assert len(meili.searches()) >= compared  # every one asked Meilisearch
    # The world is not empty for the words: most queries find something.
    _off(monkeypatch)
    found = [q for q in QUERIES if (await _all(client, f"{API}/discover/events", {"q": q, "radius_km": 0}))[0]]
    assert len(found) >= len(QUERIES) - 6


async def test_clubs_meilisearch_finds_what_postgres_finds(client, session, world, meili, monkeypatch):
    await _built(session)
    url = settings.SEARCH_URL
    tag = world["tag"]
    for q in ("malabar", "pacific", "quiet", "brigade", f"robotics {tag}", "kochi", "suva", "makers kochi", f"performing {tag}", "players"):
        for f in ({}, {"sort": "newest"}, {"sort": "nearby", "lat": KOCHI[0], "lng": KOCHI[1]}, {"lat": KOCHI[0], "lng": KOCHI[1], "radius_km": 50},
                  {"topic": world["topic_tech"].slug}, {"interest": world["mus"].id}):
            params = {"radius_km": 0, **f, "q": q}
            _off(monkeypatch)
            pg, pg_total = await _all(client, f"{API}/discover/clubs", params)
            _on(monkeypatch, url)
            ms, ms_total = await _all(client, f"{API}/discover/clubs", params)
            assert sorted(ms) == sorted(pg) and ms_total == pg_total, (q, f)
            assert world["hidden"].id not in ms and world["blocked"].id not in ms


async def test_the_apps_lists_find_the_same_through_either(client, session, world, meili, monkeypatch, make_user, mint_token):
    await _built(session)
    url = settings.SEARCH_URL
    h = auth_header(await mint_token(await make_user(email=f"lists-{world['tag']}@example.com")))
    for q in ("hackathon", "festival", "kochi", "robotics", "concert", "jazz evening"):
        for f in ({}, {"is_ended": "true"}, {"is_ended": "false"}, {"interest_ids": f"{world['rob'].id},{world['mus'].id}"}):
            for offset in (0, 4):
                params = {"search": q, "limit": 4, "offset": offset, **f}
                _off(monkeypatch)
                pg = [e["id"] for e in (await client.get(f"{API}/events/list", params=params, headers=h)).json()["items"]]
                _on(monkeypatch, url)
                ms = [e["id"] for e in (await client.get(f"{API}/events/list", params=params, headers=h)).json()["items"]]
                assert ms == pg, (q, f, offset)  # date order both ways
        _off(monkeypatch)
        pg = [c["id"] for c in (await client.get(f"{API}/clubs/list", params={"search": q, "limit": 50}, headers=h)).json()["items"]]
        _on(monkeypatch, url)
        ms = [c["id"] for c in (await client.get(f"{API}/clubs/list", params={"search": q, "limit": 50}, headers=h)).json()["items"]]
        assert ms == pg, q


async def test_synonyms_and_typos_find_more_through_meilisearch(client, session, world, meili, monkeypatch):
    """What Meilisearch is for: words people use for the same thing."""
    await _built(session)
    tag = world["tag"]
    ids = lambda body: {i["id"] for i in body["items"]}  # noqa: E731
    hack = (await client.get(f"{API}/discover/events", params={"q": "hack", "radius_km": 0})).json()
    assert ids(hack) and all("Hack" in i["name"] for i in hack["items"])
    cochin = (await client.get(f"{API}/discover/clubs", params={"q": "cochin", "radius_km": 0})).json()
    assert world["kochi"].id in ids(cochin)
    _off(monkeypatch)
    assert world["kochi"].id not in ids((await client.get(f"{API}/discover/clubs", params={"q": "cochin", "radius_km": 0})).json())
    # A typo Postgres only forgives when nothing matches exactly, Meilisearch ranks below the exact matches.
    _on(monkeypatch, fake_meili.URL if meili is not None else os.environ["MEILI_TEST_URL"])
    typo = (await client.get(f"{API}/discover/clubs", params={"q": f"malabr {tag}", "radius_km": 0})).json()
    assert world["kochi"].id in ids(typo)
