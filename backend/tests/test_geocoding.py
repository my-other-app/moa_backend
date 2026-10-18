"""A point for a venue typed as words, and which point wins.

The geocoder (OpenStreetMap's Nominatim) is faked here: every request it
would send is recorded and answered from `nominatim["answers"]`. Map links
are faked the same way. A pin from the dashboard's map beats a map link's
point, which beats a geocoded one, and nothing weaker ever replaces
something stronger."""
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text, update

from app.api.closeout.models import EventStatus
from app.api.discover import geo, geocode
from app.api.discover.geocode import GeocodeCache
from app.api.events.models import EventCategories, Events
from app.config import settings
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

KOZHIKODE = [{"lat": "11.3216", "lon": "75.9338", "display_name": "NIT Calicut"}]
KOCHI = [{"lat": "9.9312", "lon": "76.2673", "display_name": "Kochi"}]
PIN = (12.3456, 76.5432)
# The geocoder's own client, before the suite's guard replaces it for each test.
REAL_HTTP = geocode._http
LINKED = "https://www.google.com/maps/place/Lab/@18.5293,73.8565,17z"


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture
def nominatim(monkeypatch, session):
    """The geocoder on, and the only Nominatim there is: `answers` maps the
    words asked about to a JSON body, a status code or an exception (a miss,
    [], when nothing is said). Work after a response runs on the test's own
    session, one piece at a time."""
    state = {"answers": {}, "calls": [], "at": [], "latency": 0.0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["at"].append(time.monotonic())
        state["calls"].append(request)
        await asyncio.sleep(state["latency"])
        q = parse_qs(urlparse(str(request.url)).query)["q"][0]
        answer = state["answers"].get(q, [])
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(settings, "GEOCODER_ENABLED", True)

    def fake_http():
        client = REAL_HTTP()  # the real headers and timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=client.headers, timeout=client.timeout)

    monkeypatch.setattr(geocode, "_http", fake_http)
    monkeypatch.setattr(geocode, "SPACING", 0.0)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(geo, "session_scope", scope)
    yield state


@pytest.fixture
def maps(monkeypatch):
    """Map links: `routes` maps a URL prefix to a response."""
    state = {"routes": {}, "calls": []}

    def handler(request):
        url = str(request.url)
        state["calls"].append(url)
        for prefix, answer in state["routes"].items():
            if url.startswith(prefix):
                return answer
        return httpx.Response(404)

    monkeypatch.setattr(geo, "_http", lambda timeout=5.0: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))
    return state


def _asked(state) -> list[str]:
    return [parse_qs(urlparse(str(r.url)).query)["q"][0] for r in state["calls"]]


@pytest.fixture
async def club_owner(session, make_club, mint_token):
    from app.api.interests.models import InterestCategory, Interests

    owner, club = await make_club(name="Geo Society", email="geo-owner@example.com")
    tech = EventCategories(name="Geo Tech")
    icat = InterestCategory(name="Geo interests")
    session.add_all([tech, icat])
    await session.flush()
    interest = Interests(name="Maps", category_id=icat.id)
    session.add(interest)
    await session.commit()
    return dict(owner=owner, club=club, tech=tech, interest=interest, h=auth_header(await mint_token(owner)))


def _form(world, **kw) -> dict:
    form = {
        "name": "Venue Night", "event_datetime": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(),
        "duration": "2", "has_fee": "false", "status": "published", "category_id": str(world["tech"].id),
    }
    form.update({k: str(v) for k, v in kw.items()})
    return form


async def _event(session, event_id) -> Events:
    e = await session.get(Events, event_id)
    await session.refresh(e)
    return e


def _place(e) -> tuple:
    return (e.lat, e.lng, e.geo_source)


# ─── The words ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("words, query", [
    ("  Seminar Hall,   NIT Calicut. ", "seminar hall, nit calicut"),
    ("MAIN AUDITORIUM - CET", "main auditorium - cet"),
    ("Online", None), ("zoom", None), (" TBA ", None), ("N/A", None), ("", None), (None, None),
    ("12345", None), ("x" * 201, None), ("Hall 7", "hall 7"),
])
def test_words_are_asked_about_as_one_shape(words, query):
    assert geocode.normalise(words) == query


def test_the_pace_is_one_request_a_second():
    assert geocode.SPACING == 1.0
    assert geocode.MISS_TTL == timedelta(days=30)


# ─── Asking ──────────────────────────────────────────────────────────────


async def test_words_are_asked_about_once_under_the_usage_policy(session, nominatim):
    nominatim["answers"]["seminar hall, nit calicut"] = KOZHIKODE
    assert await geocode.lookup(session, "Seminar Hall,  NIT Calicut") == (11.3216, 75.9338)

    [request] = nominatim["calls"]
    assert str(request.url).startswith(settings.GEOCODER_URL)
    params = parse_qs(urlparse(str(request.url)).query)
    assert params == {"q": ["seminar hall, nit calicut"], "format": ["jsonv2"], "limit": ["1"], "countrycodes": ["in"]}
    agent = request.headers["user-agent"]
    assert "MyOtherApp" in agent and settings.GEOCODER_EMAIL in agent
    row = await session.get(GeocodeCache, "seminar hall, nit calicut")
    assert (row.found, row.lat, row.lng) == (True, 11.3216, 75.9338)

    # The same words however typed: from the cache, nothing asked.
    assert await geocode.lookup(session, "seminar hall, NIT calicut.") == (11.3216, 75.9338)
    assert len(nominatim["calls"]) == 1


async def test_a_miss_is_kept_and_asked_about_again_after_thirty_days(session, nominatim):
    assert await geocode.lookup(session, "Nowhere Hall") is None
    assert await geocode.lookup(session, "Nowhere Hall") is None
    assert _asked(nominatim) == ["nowhere hall"]
    row = (await session.execute(select(GeocodeCache.found, GeocodeCache.lat).where(GeocodeCache.query == "nowhere hall"))).one()
    assert tuple(row) == (False, None)

    await session.execute(update(GeocodeCache).where(GeocodeCache.query == "nowhere hall").values(fetched_at=datetime.now(timezone.utc) - timedelta(days=31)))
    nominatim["answers"]["nowhere hall"] = KOCHI
    assert await geocode.lookup(session, "Nowhere Hall") == (9.9312, 76.2673)
    assert _asked(nominatim) == ["nowhere hall", "nowhere hall"]


@pytest.mark.parametrize("failure", [500, 502, httpx.ConnectTimeout("slow"), httpx.ConnectError("down")])
async def test_no_answer_is_not_kept(session, nominatim, failure):
    nominatim["answers"]["flaky hall"] = failure
    assert await geocode.lookup(session, "Flaky Hall") is None
    assert await session.get(GeocodeCache, "flaky hall") is None
    nominatim["answers"]["flaky hall"] = KOCHI
    assert await geocode.lookup(session, "Flaky Hall") == (9.9312, 76.2673)


async def test_a_refusal_pauses_every_lookup(session, nominatim):
    nominatim["answers"]["first hall"] = 429
    assert await geocode.lookup(session, "First Hall") is None
    assert geocode.paused()
    assert await geocode.lookup(session, "Second Hall") is None
    assert _asked(nominatim) == ["first hall"]


async def test_requests_go_one_at_a_time_a_gap_apart(session, nominatim, monkeypatch):
    monkeypatch.setattr(geocode, "SPACING", 0.15)
    for words in ("hall a", "hall b", "hall c"):
        nominatim["answers"][words] = KOCHI
    await asyncio.gather(*(geocode._ask(w) for w in ("hall a", "hall b", "hall c")))
    gaps = [b - a for a, b in zip(nominatim["at"], nominatim["at"][1:])]
    assert len(nominatim["at"]) == 3 and all(g >= 0.15 for g in gaps), gaps


async def test_the_same_words_asked_together_are_asked_once(session, nominatim, monkeypatch):
    nominatim["latency"] = 0.05
    nominatim["answers"]["busy hall"] = KOCHI
    got = await asyncio.gather(*(geocode._ask_once("busy hall") for _ in range(3)))
    assert got == [(9.9312, 76.2673)] * 3
    assert _asked(nominatim) == ["busy hall"]


async def test_the_kill_switch_stops_everything(client, session, nominatim, club_owner, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "GEOCODER_ENABLED", False)
    nominatim["answers"]["main hall, cet"] = KOCHI
    assert await geocode.lookup(session, "Main Hall, CET") is None
    assert geo.needs_locating(SimpleNamespace(location_name="Main Hall, CET", location_link=None, lat=None, geo_source=None)) is False
    made = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Main Hall, CET"), headers=club_owner["h"])
    assert made.status_code == 200, made.text
    assert (await _event(session, made.json()["id"])).lat is None
    admin = await make_admin(email="geo-admin-off@example.com")
    r = await client.post("/api/v1/admin/discover/geocode-backfill", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False and r.json()["requests"] == 0
    assert nominatim["calls"] == [] and await session.get(GeocodeCache, "main hall, cet") is None


# ─── Which point wins ────────────────────────────────────────────────────


def _row(**kw):
    base = dict(location_link=None, location_name=None, lat=None, lng=None, geo_source=None, is_online=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_pin_beats_a_link_until_it_is_cleared():
    row = _row(location_name="Lab 3")
    geo.apply_place(row, link=LINKED)
    assert _place(row) == (18.5293, 73.8565, "link")
    geo.apply_place(row, manual=PIN)
    assert _place(row) == (*PIN, "manual")
    # A new link, new words, the same link again: the pin stays, the link is kept.
    geo.apply_place(row, link="https://maps.google.com/?q=12.5,77.5")
    geo.apply_place(row, venue="Lab 4")
    geo.apply_place(row, link="https://maps.app.goo.gl/Short")
    assert _place(row) == (*PIN, "manual") and row.location_link == "https://maps.app.goo.gl/Short"
    assert geo.needs_locating(row) is False
    # New pin values move it.
    geo.apply_place(row, manual=(12.0, 77.0))
    assert _place(row) == (12.0, 77.0, "manual")
    # clear_point: back to what the link gives; a short one is followed after the save.
    geo.apply_place(row, clear_point=True)
    assert _place(row) == (None, None, None) and geo.needs_resolving(row)
    geo.apply_place(row, link=LINKED, manual=PIN)
    geo.apply_place(row, clear_point=True)
    assert _place(row) == (18.5293, 73.8565, "link")


def test_a_link_beats_words_and_cleared_sources_take_their_points(monkeypatch):
    monkeypatch.setattr(settings, "GEOCODER_ENABLED", True)
    row = _row(location_name="Town Hall", lat=9.9, lng=76.2, geo_source="geocoded")
    # Same words: the geocoded point stands.
    geo.apply_place(row, venue="town  hall")
    assert _place(row) == (9.9, 76.2, "geocoded")
    # A link that holds a point takes over.
    geo.apply_place(row, link=LINKED)
    assert _place(row) == (18.5293, 73.8565, "link")
    # New words leave a link's point alone.
    geo.apply_place(row, venue="Somewhere Else")
    assert _place(row) == (18.5293, 73.8565, "link")
    # Clearing the link takes its point; the words are geocoded after the save.
    geo.apply_place(row, link=None)
    assert _place(row) == (None, None, None) and geo.needs_locating(row)
    # Changed or cleared words take a geocoded point.
    row.lat, row.lng, row.geo_source = 9.9, 76.2, "geocoded"
    geo.apply_place(row, venue="New Venue")
    assert _place(row) == (None, None, None)
    row.lat, row.lng, row.geo_source = 9.9, 76.2, "geocoded"
    geo.apply_place(row, venue=None)
    assert _place(row) == (None, None, None) and geo.needs_locating(row) is False
    # A short link replaces a geocoded point once followed (cleared until then).
    row = _row(location_name="Town Hall", lat=9.9, lng=76.2, geo_source="geocoded")
    geo.apply_place(row, link="https://maps.app.goo.gl/Better")
    assert _place(row) == (None, None, None) and geo.needs_resolving(row)


def test_only_a_stronger_source_may_replace_a_point():
    assert geo._may_place(_row(), "geocoded")
    assert geo._may_place(_row(lat=1, geo_source="geocoded"), "link")
    assert not geo._may_place(_row(lat=1, geo_source="geocoded"), "geocoded")
    assert not geo._may_place(_row(lat=1, geo_source="link"), "geocoded")
    assert not geo._may_place(_row(lat=1, geo_source="manual"), "link")
    assert not geo._may_place(_row(lat=1, geo_source="manual"), "geocoded")
    assert not geo._may_place(_row(lat=1, geo_source=None), "link")


def test_online_events_and_non_places_are_not_geocoded(monkeypatch):
    monkeypatch.setattr(settings, "GEOCODER_ENABLED", True)
    assert geo.needs_locating(_row(location_name="Main Hall")) is True
    assert geo.needs_locating(_row(location_name="Main Hall", is_online=True)) is False
    assert geo.needs_locating(_row(location_name="Online")) is False
    assert geo.needs_locating(_row(location_name="Main Hall", lat=1.0, lng=2.0, geo_source="link")) is False


# ─── Saving events and clubs ─────────────────────────────────────────────


async def test_an_event_with_only_its_venues_words_is_geocoded_after_the_save(client, session, nominatim, club_owner):
    nominatim["answers"]["seminar hall, nit calicut"] = KOZHIKODE
    made = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Seminar Hall, NIT Calicut"), headers=club_owner["h"])
    assert made.status_code == 200, made.text
    e = await _event(session, made.json()["id"])
    assert _place(e) == (11.3216, 75.9338, "geocoded")
    # The dashboard reads it back.
    info = (await client.get(f"/api/v1/events/info/{e.id}", headers=club_owner["h"])).json()
    assert (info["lat"], info["lng"], info["geo_source"]) == (11.3216, 75.9338, "geocoded")

    # Saved again with the same words: nothing asked, the point kept.
    update_form = _form(club_owner, location_name="Seminar Hall, NIT Calicut", interest_ids=club_owner["interest"].id)
    r = await client.put(f"/api/v1/events/update/{e.id}", data=update_form, headers=club_owner["h"])
    assert r.status_code == 200, r.text
    assert (r.json()["lat"], r.json()["geo_source"]) == (11.3216, "geocoded")
    assert len(nominatim["calls"]) == 1
    # New words: placed again.
    nominatim["answers"]["kochi marriott"] = KOCHI
    await client.put(f"/api/v1/events/update/{e.id}", data={**update_form, "location_name": "Kochi Marriott"}, headers=club_owner["h"])
    assert _place(await _event(session, e.id)) == (9.9312, 76.2673, "geocoded")


async def test_online_events_links_and_pins_are_never_geocoded(client, session, nominatim, maps, club_owner):
    h = club_owner["h"]
    nominatim["answers"]["cet auditorium"] = KOCHI
    online = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="CET Auditorium", is_online="true"), headers=h)
    linked = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="CET Auditorium", location_link=LINKED), headers=h)
    pinned = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="CET Auditorium", lat=PIN[0], lng=PIN[1]), headers=h)
    for r in (online, linked, pinned):
        assert r.status_code == 200, r.text
    assert (await _event(session, online.json()["id"])).lat is None
    assert _place(await _event(session, linked.json()["id"])) == (18.5293, 73.8565, "link")
    assert _place(await _event(session, pinned.json()["id"])) == (*PIN, "manual")
    assert (pinned.json()["lat"], pinned.json()["lng"], pinned.json()["geo_source"]) == (*PIN, "manual")
    assert nominatim["calls"] == [] and maps["calls"] == []


async def test_a_short_link_that_leads_nowhere_falls_back_to_the_words(client, session, nominatim, maps, club_owner):
    maps["routes"]["https://maps.app.goo.gl/Good"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/@12.91,77.62,15z"})
    maps["routes"]["https://www.google.com/maps/@12.91"] = httpx.Response(200)
    nominatim["answers"]["innovation centre"] = KOCHI
    good = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Innovation Centre", location_link="https://maps.app.goo.gl/Good"), headers=club_owner["h"])
    dead = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Innovation Centre", location_link="https://maps.app.goo.gl/Dead"), headers=club_owner["h"])
    assert _place(await _event(session, good.json()["id"])) == (12.91, 77.62, "link")
    assert _place(await _event(session, dead.json()["id"])) == (9.9312, 76.2673, "geocoded")
    assert _asked(nominatim) == ["innovation centre"]  # only for the one whose link led nowhere


async def test_a_pin_stays_through_link_changes_until_cleared(client, session, nominatim, club_owner):
    h = club_owner["h"]
    nominatim["answers"]["hall of fame"] = KOCHI
    made = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Hall of Fame", lat=PIN[0], lng=PIN[1]), headers=h)
    event_id = made.json()["id"]
    base = _form(club_owner, location_name="Hall of Fame", interest_ids=club_owner["interest"].id)

    # A new map link is kept, the pin stays.
    r = await client.put(f"/api/v1/events/update/{event_id}", data={**base, "location_link": "https://maps.google.com/?q=12.5,77.5"}, headers=h)
    assert r.status_code == 200, r.text
    e = await _event(session, event_id)
    assert _place(e) == (*PIN, "manual") and e.location_link == "https://maps.google.com/?q=12.5,77.5"
    # New pin values move it.
    await client.put(f"/api/v1/events/update/{event_id}", data={**base, "lat": "12.4", "lng": "76.6"}, headers=h)
    assert _place(await _event(session, event_id)) == (12.4, 76.6, "manual")
    # clear_point drops back to the link's point.
    r = await client.put(f"/api/v1/events/update/{event_id}", data={**base, "clear_point": "true"}, headers=h)
    assert (r.json()["lat"], r.json()["lng"], r.json()["geo_source"]) == (12.5, 77.5, "link")
    # Without the link, to the words.
    await client.put(f"/api/v1/events/update/{event_id}", data={**base, "clear_location_link": "true"}, headers=h)
    assert _place(await _event(session, event_id)) == (9.9312, 76.2673, "geocoded")
    assert _asked(nominatim) == ["hall of fame"]


@pytest.mark.parametrize("pin", [
    {"lat": "12.9"}, {"lng": "77.5"}, {"lat": "91", "lng": "77.5"}, {"lat": "12.9", "lng": "-181"},
    {"lat": "0", "lng": "0"}, {"lat": "nan", "lng": "77.5"}, {"lat": "north", "lng": "77.5"},
])
async def test_a_pin_must_be_both_halves_of_a_real_point(client, session, nominatim, club_owner, pin):
    r = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Hall", **pin), headers=club_owner["h"])
    assert r.status_code == 422, r.text
    assert set(r.json()["errors"]) & {"lat", "lng"}, r.json()
    made = await client.post("/api/v1/events/create", data=_form(club_owner, location_name="Hall", name="Pinned Later"), headers=club_owner["h"])
    r = await client.put(f"/api/v1/events/update/{made.json()['id']}", data={**_form(club_owner, location_name="Hall", interest_ids=club_owner["interest"].id), **pin}, headers=club_owner["h"])
    assert r.status_code == 422, r.text
    r = await client.put("/api/v1/clubs/update", data={"location_name": "Hall", **pin}, headers=club_owner["h"])
    assert r.status_code == 422, r.text


async def test_a_clubs_place_follows_the_same_rules(client, session, nominatim, club_owner):
    h, club = club_owner["h"], club_owner["club"]
    nominatim["answers"]["kozhikode beach"] = KOZHIKODE
    r = await client.put("/api/v1/clubs/update", data={"location_name": "Kozhikode Beach"}, headers=h)
    assert r.status_code == 200, r.text
    await session.refresh(club)
    assert _place(club) == (11.3216, 75.9338, "geocoded")
    info = (await client.get("/api/v1/clubs/info", headers=h)).json()
    assert (info["lat"], info["lng"], info["geo_source"]) == (11.3216, 75.9338, "geocoded")

    r = await client.put("/api/v1/clubs/update", data={"location_name": "Kozhikode Beach", "lat": PIN[0], "lng": PIN[1]}, headers=h)
    assert (r.json()["lat"], r.json()["lng"], r.json()["geo_source"]) == (*PIN, "manual")
    # A map link later keeps the pin.
    await client.put("/api/v1/clubs/update", data={"location_name": "Kozhikode Beach", "location_link": LINKED}, headers=h)
    await session.refresh(club)
    assert _place(club) == (*PIN, "manual") and club.location_link == LINKED
    # Until it is cleared: then the link's point.
    await client.put("/api/v1/clubs/update", data={"location_name": "Kozhikode Beach", "location_link": LINKED, "clear_point": "true"}, headers=h)
    await session.refresh(club)
    assert _place(club) == (18.5293, 73.8565, "link")
    # The public detail carries it too.
    public = (await client.get(f"/api/v1/clubs/info/{club.id}", headers=h)).json()
    assert (public["lat"], public["geo_source"]) == (18.5293, "link")
    assert _asked(nominatim) == ["kozhikode beach"]


async def test_a_club_signing_up_with_a_place_is_geocoded(client, session, nominatim):
    nominatim["answers"]["mysuru palace"] = KOCHI
    r = await client.post("/api/v1/clubs/create", data={
        "email": "geo-signup@example.com", "password": "Password123!", "name": "Signup Geo", "location_name": "Mysuru Palace",
    })
    assert r.status_code == 200, r.text
    assert (r.json()["lat"], r.json()["geo_source"]) == (None, None)  # found after the answer
    from app.api.clubs.models import Clubs

    club = await session.get(Clubs, r.json()["id"])
    await session.refresh(club)
    assert _place(club) == (9.9312, 76.2673, "geocoded")


async def test_an_answer_that_comes_after_a_pin_is_dropped_is_not_kept(session, nominatim, make_event, club_owner, monkeypatch):
    e = await make_event(club=club_owner["club"], created_by=club_owner["owner"], name="Race", location_name="Race Hall", lifecycle=EventStatus.published)
    await session.commit()

    async def slow_lookup(s, words):
        # Meanwhile, the organiser drops a pin.
        e.lat, e.lng, e.geo_source = (*PIN, "manual")
        await session.commit()
        return (9.9, 76.2)

    monkeypatch.setattr(geocode, "lookup", slow_lookup)
    assert await geo.locate_and_store("event", e.id) is None
    assert _place(await _event(session, e.id)) == (*PIN, "manual")

    # Nor over a link's point; but a link's point does replace a geocoded one.
    e.lat, e.lng, e.geo_source = (18.5, 73.8, "link")
    await session.commit()
    assert await geo._store("event", e.id, (9.9, 76.2), "geocoded", link=e.location_link, venue=e.location_name) is False
    e.lat, e.lng, e.geo_source, e.location_link = (9.9, 76.2, "geocoded", "https://maps.app.goo.gl/Up")
    await session.commit()
    assert await geo._store("event", e.id, (18.5, 73.8), "link", link=e.location_link, venue=e.location_name) is True
    assert _place(await _event(session, e.id)) == (18.5, 73.8, "link")


async def test_a_short_link_still_upgrades_a_geocoded_point_later(session, nominatim, maps, make_event, club_owner):
    maps["routes"]["https://maps.app.goo.gl/Later"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/@12.93,77.63,15z"})
    maps["routes"]["https://www.google.com/maps/@12.93"] = httpx.Response(200)
    e = await make_event(
        club=club_owner["club"], created_by=club_owner["owner"], name="Upgrade", location_name="Old Hall",
        location_link="https://maps.app.goo.gl/Later", lat=9.9, lng=76.2, geo_source="geocoded",
    )
    plain = await make_event(club=club_owner["club"], created_by=club_owner["owner"], name="Plain", location_name="Plain Hall", lat=9.9, lng=76.2, geo_source="geocoded")
    await session.commit()
    pending = await geo.missing(session)
    assert ("event", e.id) in pending and ("event", plain.id) not in pending
    assert await geo.resolve_and_store("event", e.id) is True
    assert _place(await _event(session, e.id)) == (12.93, 77.63, "link")


# ─── The backfill ────────────────────────────────────────────────────────


async def test_the_backfill_geocodes_what_has_no_point_and_touches_nothing_else(client, session, nominatim, make_event, make_club, make_admin, mint_token, club_owner):
    club, owner, now = club_owner["club"], club_owner["owner"], datetime.now(timezone.utc)

    async def ev(name, words, start, **kw):
        return await make_event(club=club, created_by=owner, name=name, location_name=words, event_datetime=start, lifecycle=EventStatus.published, **kw)

    soon = await ev("Soon", "Main Hall, CET", now + timedelta(days=2))
    soon_too = await ev("Soon Too", "main hall,  cet", now + timedelta(days=3))
    gone_by = await ev("Gone By", "Old Library", now - timedelta(days=20))
    unknown = await ev("Unknown", "Nowhere Hall", now + timedelta(days=4))
    online = await ev("Online", "Studio 5", now + timedelta(days=2), is_online=True)
    pinned = await ev("Pinned", "Main Hall, CET", now + timedelta(days=2), lat=PIN[0], lng=PIN[1], geo_source="manual")
    linked = await ev("Linked", "Main Hall, CET", now + timedelta(days=2), location_link=LINKED, lat=18.5293, lng=73.8565, geo_source="link")
    tba = await ev("TBA", "TBA", now + timedelta(days=2))
    club.location_name = "Calicut Beach"
    await session.commit()
    nominatim["answers"].update({"main hall, cet": KOCHI, "old library": KOZHIKODE, "calicut beach": KOZHIKODE})

    assert (await client.post("/api/v1/admin/discover/geocode-backfill", headers=club_owner["h"])).status_code == 403
    admin_h = auth_header(await mint_token(await make_admin(email="geo-admin@example.com")))
    r = await client.post("/api/v1/admin/discover/geocode-backfill", params={"max_requests": 2}, headers=admin_h)
    assert r.status_code == 200, r.text
    first = r.json()
    # The soonest first: "main hall, cet" (both rows, one request), then "nowhere hall".
    assert first["enabled"] is True and first["requests"] == 2
    assert first["events"] == {"checked": 3, "geocoded": 2, "not_found": 1}
    assert first["remaining"] == 2  # the club and the past event, for the next run
    assert _asked(nominatim) == ["main hall, cet", "nowhere hall"]

    r = await client.post("/api/v1/admin/discover/geocode-backfill", headers=admin_h)
    second = r.json()
    assert second["requests"] == 2 and second["remaining"] == 0
    assert second["clubs"] == {"checked": 1, "geocoded": 1, "not_found": 0}
    # The miss is remembered: asked once in all.
    assert second["events"] == {"checked": 2, "geocoded": 1, "not_found": 1}
    assert _asked(nominatim).count("nowhere hall") == 1

    for row in (soon, soon_too, gone_by, unknown, online, pinned, linked, tba, club):
        await session.refresh(row)
    assert _place(soon) == _place(soon_too) == (9.9312, 76.2673, "geocoded")
    assert _place(gone_by) == (11.3216, 75.9338, "geocoded") and _place(club) == (11.3216, 75.9338, "geocoded")
    assert unknown.lat is None and online.lat is None and tba.lat is None
    assert _place(pinned) == (*PIN, "manual") and _place(linked) == (18.5293, 73.8565, "link")


# ─── The migration ───────────────────────────────────────────────────────


def _migration():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "discovery_polish.py"
    spec = importlib.util.spec_from_file_location("discovery_polish", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_marks_todays_points_as_from_links_and_undoes_cleanly(session):
    """The real upgrade and downgrade, on tables shaped as production's are,
    in a schema of their own inside the test's transaction."""
    module = _migration()
    assert module.down_revision == "discover_perf"
    connection = await session.connection()

    def run(sync):
        sync.execute(text("CREATE SCHEMA discovery_polish_migration"))
        sync.execute(text("SET LOCAL search_path TO discovery_polish_migration"))
        sync.execute(text("CREATE TYPE eventstatus AS ENUM ('draft', 'published', 'live', 'closed', 'archived')"))
        sync.execute(text("CREATE TABLE users (id serial PRIMARY KEY)"))
        sync.execute(text("CREATE TABLE clubs (id serial PRIMARY KEY, lat float, lng float)"))
        sync.execute(text(
            "CREATE TABLE events (id serial PRIMARY KEY, event_datetime timestamptz NOT NULL, duration float NOT NULL, "
            "is_deleted boolean NOT NULL DEFAULT false, status eventstatus NOT NULL DEFAULT 'published', lat float, lng float)"
        ))
        sync.execute(text("INSERT INTO clubs (id, lat, lng) VALUES (1, 12.9, 77.6), (2, NULL, NULL)"))
        sync.execute(text(
            "INSERT INTO events (id, event_datetime, duration, lat, lng, status) VALUES "
            "(1, now(), 2, 12.9, 77.6, 'published'), (2, now(), -5, NULL, NULL, 'closed'), (3, now(), 0, NULL, NULL, 'draft')"
        ))
        context = MigrationContext.configure(connection=sync)
        with Operations.context(context):
            module.upgrade()
            sources = (
                dict(sync.execute(text("SELECT id, geo_source FROM events")).all()),
                dict(sync.execute(text("SELECT id, geo_source FROM clubs")).all()),
            )
            indexes = {r[0] for r in sync.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'discovery_polish_migration'"))}
            # A negative duration cannot fail a write: the span is never inverted.
            sync.execute(text("INSERT INTO events (id, event_datetime, duration) VALUES (4, now(), -10)"))
            module.downgrade()
            left = {r[0] for r in sync.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'discovery_polish_migration'"
            ))}
            columns = {r[0] for r in sync.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'discovery_polish_migration' AND column_name = 'geo_source'"
            ))}
            module.upgrade()
        sync.execute(text("SET LOCAL search_path TO public"))
        return sources, indexes, left, columns

    sources, indexes, left, columns = await connection.run_sync(run)
    assert sources == ({1: "link", 2: None, 3: None}, {1: "link", 2: None})
    assert {"ix_events_span", "geocode_cache_pkey", "ix_password_reset_codes_key_sent", "ix_password_reset_codes_sent"} <= indexes
    assert left == {"users", "clubs", "events"} and columns == set()
