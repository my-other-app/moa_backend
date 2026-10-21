"""Where things are: a point out of every shape of map link, a short link
followed to where it lands, and a visitor placed by their address — all
without the network, which every test here fakes."""
import math
from types import SimpleNamespace

import httpx
import pytest

from app.api.discover import geo


@pytest.fixture(autouse=True)
def _fresh():
    geo._ip_cache.clear()
    yield
    geo._ip_cache.clear()


@pytest.fixture
def net(monkeypatch):
    """A fake internet: `routes` maps a URL prefix to a response (or an exception); every request is logged."""
    state = {"routes": {}, "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state["calls"].append(url)
        for prefix, answer in state["routes"].items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return httpx.Response(404)

    def fake_http(timeout: float = 5.0):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True, timeout=timeout)

    monkeypatch.setattr(geo, "_http", fake_http)
    return state


# ─── Reading links ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url, point", [
    ("https://www.google.com/maps/@12.9716,77.5946,15z", (12.9716, 77.5946)),
    ("https://www.google.co.in/maps/@-33.8688,151.2093,12z?entry=ttu", (-33.8688, 151.2093)),
    ("https://maps.google.com/?q=12.97,77.59", (12.97, 77.59)),
    ("https://maps.google.com/maps?q=12.97,+77.59&z=15", (12.97, 77.59)),
    ("https://www.google.com/maps?q=loc:12.97+77.59", (12.97, 77.59)),
    ("https://www.google.com/maps/search/?api=1&query=12.9716%2C77.5946", (12.9716, 77.5946)),
    ("https://www.google.com/maps/search/?api=1&query=12.9716,77.5946", (12.9716, 77.5946)),
    ("https://maps.google.com/?ll=28.6139,77.2090&z=12", (28.6139, 77.209)),
    ("https://www.google.com/maps/@?api=1&map_action=map&center=19.0760,72.8777&zoom=12", (19.076, 72.8777)),
    # The /place/ link: the pin (!3d…!4d…) wins over the view's centre (@…).
    ("https://www.google.com/maps/place/Cubbon+Park/@12.9763,77.5929,17z/data=!3m1!4b1!4m6!3m5!1s0x3bae1670c9b44e6d:0xf8dfc3e8517e4fe0!8m2!3d12.9763472!4d77.5928919!16s", (12.976347, 77.592892)),
    ("https://www.google.com/maps/place/COEP/@18.5293,73.8565,16z", (18.5293, 73.8565)),
    ("https://www.google.com/maps/dir//12.97,77.59/", (12.97, 77.59)),
    ("https://www.google.com/maps/dir/?api=1&destination=12.97,77.59", (12.97, 77.59)),
    ("https://www.google.com/maps/search/12.9716,+77.5946", (12.9716, 77.5946)),
    ("maps.google.com/?q=12.97,77.59", (12.97, 77.59)),
    ("https://maps.apple.com/?ll=12.97,77.59&q=Venue", (12.97, 77.59)),
    ("https://www.openstreetmap.org/?mlat=12.97&mlon=77.59#map=15/12.97/77.59", (12.97, 77.59)),
    ("https://www.openstreetmap.org/#map=15/12.9716/77.5946", (12.9716, 77.5946)),
    ("geo:12.97,77.59?z=15", (12.97, 77.59)),
    ("  12.9716, 77.5946 ", (12.9716, 77.5946)),
    # A consent page that wraps the real address.
    ("https://consent.google.com/ml?continue=https://www.google.com/maps/place/X/@12.97,77.59,17z&gl=IN", (12.97, 77.59)),
])
def test_a_point_is_read_from_every_shape_of_map_link(url, point):
    assert geo.coords_from_link(url) == point


@pytest.mark.parametrize("junk", [
    None, "", "   ", "not a link", "https://example.com/", "https://example.com/?page=2",
    "https://www.google.com/maps/place/Cubbon+Park", "https://maps.google.com/?q=Cubbon+Park,+Bengaluru",
    "https://maps.app.goo.gl/AbCdEf123",  # a short link holds nothing until it is followed
    "https://maps.google.com/?q=95.0,77.0",  # latitude out of range
    "https://www.google.com/maps/@12.97,200.5,15z",  # longitude out of range
    "https://maps.google.com/?q=0,0", "0,0", "Block 12, 77 Main Road", "x" * 5000, 12345,
])
def test_junk_holds_no_point(junk):
    assert geo.coords_from_link(junk) is None


@pytest.mark.parametrize("url, short", [
    ("https://maps.app.goo.gl/AbCdEf123", True),
    ("maps.app.goo.gl/AbCdEf123", True),
    ("https://goo.gl/maps/xyz123", True),
    ("https://g.co/kgs/abc", True),
    ("https://g.page/some-cafe", True),
    ("https://maps.google.com/?cid=1234567890", True),
    ("https://www.google.com/maps?cid=1234567890", True),
    ("https://goo.gl/xyz", False),
    ("https://g.co/other", False),
    ("https://example.com/maps", False),
    # Look-alikes of Google's hosts are never followed.
    ("https://maps.google.example.com/?cid=1", False),
    ("https://google.example.com/maps?cid=1", False),
    ("https://maps.app.goo.gl.example.com/x", False),
    ("https://maps.app.goo.gl:8080/x", False),
    ("", False),
    (None, False),
])
def test_short_links_are_told_apart(url, short):
    assert geo.is_short_link(url) is short


def test_distances_are_great_circle_kilometres():
    bengaluru, mysuru = (12.9716, 77.5946), (12.2958, 76.6394)
    assert 125 < geo.km(*bengaluru, *mysuru) < 130
    assert geo.km(*bengaluru, *bengaluru) == 0
    assert math.isclose(geo.km(0, 0, 0, 180), math.pi * 6371.0088, rel_tol=1e-6)


# ─── Following short links ────────────────────────────────────────────────


async def test_a_short_link_is_followed_to_where_it_lands(net):
    net["routes"]["https://maps.app.goo.gl/AbC"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/Lab/@18.5293,73.8565,17z/data=!4m2!3d18.5294!4d73.8566"})
    net["routes"]["https://www.google.com/maps/place/"] = httpx.Response(200, text="<html>a big page</html>")
    assert await geo.resolve_link("https://maps.app.goo.gl/AbC") == (18.5294, 73.8566)
    assert net["calls"][0] == "https://maps.app.goo.gl/AbC"


async def test_a_short_link_landing_on_a_consent_page_is_read_through_it(net):
    net["routes"]["https://maps.app.goo.gl/Consent"] = httpx.Response(302, headers={"location": "https://consent.google.com/ml?continue=https://www.google.com/maps/@12.97,77.59,15z"})
    net["routes"]["https://consent.google.com/"] = httpx.Response(200, text="consent")
    assert await geo.resolve_link("https://maps.app.goo.gl/Consent") == (12.97, 77.59)


async def test_a_point_on_a_redirect_hop_counts_when_the_last_page_has_none(net):
    net["routes"]["https://goo.gl/maps/hop"] = httpx.Response(301, headers={"location": "https://maps.google.com/?q=12.5,77.5"})
    net["routes"]["https://maps.google.com/?q=12.5"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/Somewhere"})
    net["routes"]["https://www.google.com/maps/place/Somewhere"] = httpx.Response(200)
    assert await geo.resolve_link("https://goo.gl/maps/hop") == (12.5, 77.5)


async def test_following_never_raises(net):
    net["routes"]["https://maps.app.goo.gl/down"] = httpx.ConnectError("no network")
    assert await geo.resolve_link("https://maps.app.goo.gl/down") is None
    net["routes"]["https://maps.app.goo.gl/nowhere"] = httpx.Response(302, headers={"location": "https://www.google.com/maps/place/NoPoint"})
    assert await geo.resolve_link("https://maps.app.goo.gl/nowhere") is None
    assert await geo.resolve_link(None) is None


async def test_a_redirect_off_google_is_never_requested(net):
    # A short link that points into our own network, or anywhere else, stops there.
    net["routes"]["https://maps.app.goo.gl/inside"] = httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    assert await geo.resolve_link("https://maps.app.goo.gl/inside") is None
    net["routes"]["https://maps.app.goo.gl/away"] = httpx.Response(302, headers={"location": "https://example.com/maps/place/X"})
    assert await geo.resolve_link("https://maps.app.goo.gl/away") is None
    net["routes"]["https://maps.app.goo.gl/rel"] = httpx.Response(302, headers={"location": "/maps/place/Here/@12.97,77.59,15z"})
    assert await geo.resolve_link("https://maps.app.goo.gl/rel") == (12.97, 77.59)
    assert all("169.254" not in c and "example.com" not in c for c in net["calls"])


async def test_redirects_stop_after_a_few_hops(net):
    net["routes"]["https://maps.app.goo.gl/loop"] = httpx.Response(302, headers={"location": "https://maps.app.goo.gl/loop"})
    assert await geo.resolve_link("https://maps.app.goo.gl/loop") is None
    assert len(net["calls"]) == geo.MAX_HOPS


async def test_a_link_that_holds_its_point_or_is_not_a_short_link_is_never_followed(net):
    assert await geo.resolve_link("https://www.google.com/maps/@12.97,77.59,15z") == (12.97, 77.59)
    assert await geo.resolve_link("https://example.com/somewhere") is None
    assert all("example.com" not in c for c in net["calls"])
    assert all("@12.97" not in c for c in net["calls"])


def test_setting_a_link_reads_its_point_or_asks_for_it_to_be_followed():
    row = SimpleNamespace(location_link=None, lat=None, lng=None)
    assert geo.set_link(row, " https://maps.google.com/?q=12.97,77.59 ") is False
    assert (row.location_link, row.lat, row.lng) == ("https://maps.google.com/?q=12.97,77.59", 12.97, 77.59)
    assert geo.needs_resolving(row) is False
    assert geo.set_link(row, "https://maps.app.goo.gl/xyz") is True
    assert (row.lat, row.lng) == (None, None) and geo.needs_resolving(row) is True
    assert geo.set_link(row, "Main Hall, second floor") is False  # no point, nothing to follow
    assert row.location_link == "Main Hall, second floor" and row.lat is None and geo.needs_resolving(row) is False
    assert geo.set_link(row, "   ") is False
    assert (row.location_link, row.lat, row.lng) == (None, None, None)


# ─── The visitor ──────────────────────────────────────────────────────────


def _request(ip=None, forwarded=None):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=ip) if ip else None)


def test_the_visitor_address_is_the_first_forwarded_hop_else_the_peer():
    assert geo.client_ip(_request("10.0.0.5", "49.37.10.1, 10.0.0.1")) == "49.37.10.1"
    assert geo.client_ip(_request("49.37.10.2")) == "49.37.10.2"
    assert geo.client_ip(_request()) == ""


IPAPI_BLR = {"ip": "49.37.10.1", "city": "Bengaluru", "region": "Karnataka", "country_name": "India", "latitude": 12.9634, "longitude": 77.5855}


async def test_an_address_is_placed_once_and_remembered(net):
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    place = await geo.locate_ip("49.37.10.1")
    assert place == {"city": "Bengaluru", "region": "Karnataka", "country": "India", "lat": 12.9634, "lng": 77.5855}
    assert await geo.locate_ip("49.37.10.1") == place
    assert len(net["calls"]) == 1  # the second came from the cache


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.9", "172.16.0.4", "::1", "fe80::1", "not-an-ip", "", None])
async def test_private_and_local_addresses_are_never_looked_up(net, ip):
    assert await geo.locate_ip(ip) is None
    assert net["calls"] == []


async def test_a_lookup_that_fails_is_no_place_and_never_raises(net):
    net["routes"]["https://ipapi.co/49.37.10.3/"] = httpx.Response(200, json={"error": True, "reason": "RateLimited"})
    net["routes"]["https://ipapi.co/49.37.10.4/"] = httpx.Response(429)
    net["routes"]["https://ipapi.co/49.37.10.5/"] = httpx.ConnectError("down")
    net["routes"]["https://ipapi.co/49.37.10.6/"] = httpx.Response(200, text="not json")
    for ip in ("49.37.10.3", "49.37.10.4", "49.37.10.5", "49.37.10.6"):
        assert await geo.locate_ip(ip) is None


async def test_the_browser_point_wins_and_is_named_when_the_address_agrees(net):
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    # With the browser's point, the address is never waited for: the first
    # request is answered unnamed and the lookup runs in the background...
    first = await geo.where(_request("10.0.0.1", "49.37.10.1"), 12.97, 77.59)
    assert first == {"city": None, "region": None, "lat": 12.97, "lng": 77.59, "source": "browser"}
    await geo.settle_lookups()
    assert len(net["calls"]) == 1
    # ...and the next is named from what it found.
    near = await geo.where(_request("10.0.0.1", "49.37.10.1"), 12.97, 77.59)
    assert near == {"city": "Bengaluru", "region": "Karnataka", "lat": 12.97, "lng": 77.59, "source": "browser"}
    far = await geo.where(_request("10.0.0.1", "49.37.10.1"), 28.61, 77.21)  # the browser says Delhi
    assert far == {"city": None, "region": None, "lat": 28.61, "lng": 77.21, "source": "browser"}
    by_address = await geo.where(_request("10.0.0.1", "49.37.10.1"), None, None)
    assert by_address == {"city": "Bengaluru", "region": "Karnataka", "lat": 12.9634, "lng": 77.5855, "source": "ip"}
    # Half a point is no point.
    assert (await geo.where(_request("10.0.0.1", "49.37.10.1"), 12.97, None))["source"] == "ip"
    nowhere = await geo.where(_request("127.0.0.1"), None, None)
    assert nowhere == {"city": None, "region": None, "lat": None, "lng": None, "source": "none"}


async def test_a_refusal_pauses_lookups_and_one_address_is_asked_once_at_a_time(net, monkeypatch):
    import asyncio

    calls = []
    gate = asyncio.Event()

    async def slow_fetch(ip):
        calls.append(ip)
        await gate.wait()
        return {"city": "Bengaluru", "region": "Karnataka", "country": "India", "lat": 12.96, "lng": 77.58}

    monkeypatch.setattr(geo, "_fetch_ip", slow_fetch)
    waiting = [asyncio.ensure_future(geo.locate_ip("49.37.10.9")) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    got = await asyncio.gather(*waiting)
    assert calls == ["49.37.10.9"] and all(g["city"] == "Bengaluru" for g in got)  # five asked, one request

    async def refused(ip):
        calls.append(ip)
        raise geo._Refused(429)

    monkeypatch.setattr(geo, "_fetch_ip", refused)
    assert await geo.locate_ip("49.37.10.10") is None
    # Paused: the next address is not asked about at all, and costs nothing to wait for.
    assert await geo.locate_ip("49.37.10.11") is None
    assert calls == ["49.37.10.9", "49.37.10.10"]
    # A miss is kept too (for 15 minutes), so the refused address is not asked again either.
    assert geo.known_place("49.37.10.10") == (True, None)
    geo.reset_scan()


async def test_a_local_geolite_database_answers_without_a_request(net, monkeypatch):
    """settings.GEOIP_MMDB_PATH: the address is read from the database, nothing is asked over the network."""
    from app.config import settings

    record = {
        "city": {"names": {"en": "Kochi"}}, "subdivisions": [{"names": {"en": "Kerala"}}],
        "country": {"names": {"en": "India"}}, "location": {"latitude": 9.9312, "longitude": 76.2673},
    }

    class Reader:
        def get(self, ip):
            return record if ip == "49.37.20.1" else None

    monkeypatch.setattr(settings, "GEOIP_MMDB_PATH", "/nowhere/GeoLite2-City.mmdb")
    monkeypatch.setattr(geo, "_mmdb", Reader())
    assert await geo.locate_ip("49.37.20.1") == {"city": "Kochi", "region": "Kerala", "country": "India", "lat": 9.9312, "lng": 76.2673}
    assert await geo.locate_ip("49.37.20.2") is None
    assert net["calls"] == []
    # A path that cannot be opened falls back to the URL lookup, once logged.
    monkeypatch.setattr(geo, "_mmdb", None)
    monkeypatch.setattr(geo, "_mmdb_failed", False)
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    assert (await geo.locate_ip("49.37.10.1"))["city"] == "Bengaluru"
    assert geo._mmdb_failed is True


# ─── When the address lookup is refused ───────────────────────────────────


class Clock:
    """geo's clock, moved by hand."""

    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(geo, "_clock", c)
    geo.reset_ip()
    yield c
    geo.reset_ip()


async def test_a_found_place_is_kept_for_a_week(net, clock):
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    place = await geo.locate_ip("49.37.10.1")
    clock.t += 6 * 24 * 3600
    assert await geo.locate_ip("49.37.10.1") == place
    assert len(net["calls"]) == 1
    clock.t += 2 * 24 * 3600
    assert await geo.locate_ip("49.37.10.1") == place
    assert len(net["calls"]) == 2  # a week on, asked again


async def test_a_refusal_rests_the_lookup_and_known_addresses_still_answer(net, clock):
    """The provider's free tier refusing used to place everyone nowhere for
    ten minutes. Now only addresses never seen go unplaced while it rests:
    one it placed before is answered from what it said then, even past its
    week, and nothing is asked meanwhile."""
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    place = await geo.locate_ip("49.37.10.1")
    clock.t += 8 * 24 * 3600  # past its week
    net["routes"]["https://ipapi.co/"] = httpx.Response(429)
    del net["routes"]["https://ipapi.co/49.37.10.1/json/"]
    assert await geo.locate_ip("49.37.10.7") is None  # refused: the lookup rests
    calls = len(net["calls"])
    assert await geo.locate_ip("49.37.10.1") == place  # known: answered without asking
    assert await geo.locate_ip("49.37.10.8") is None  # never seen: unplaced, and not asked
    assert len(net["calls"]) == calls


async def test_the_rest_starts_short_and_grows_with_refusals_in_a_row(net, clock):
    net["routes"]["https://ipapi.co/"] = httpx.Response(429)
    assert await geo.locate_ip("49.37.11.1") is None
    assert len(net["calls"]) == 1
    clock.t += geo.IP_PAUSE - 1
    assert await geo.locate_ip("49.37.11.2") is None and len(net["calls"]) == 1  # resting
    clock.t += 2
    assert await geo.locate_ip("49.37.11.3") is None and len(net["calls"]) == 2  # a minute on, asked again: refused again
    clock.t += geo.IP_PAUSE + 1
    assert await geo.locate_ip("49.37.11.4") is None and len(net["calls"]) == 2  # twice as long now
    clock.t += geo.IP_PAUSE
    net["routes"] = {"https://ipapi.co/49.37.11.5/json/": httpx.Response(200, json=IPAPI_BLR), "https://ipapi.co/": httpx.Response(429)}
    assert (await geo.locate_ip("49.37.11.5"))["city"] == "Bengaluru"
    # An answer ends the run: the next refusal rests it a minute again.
    assert await geo.locate_ip("49.37.11.6") is None
    clock.t += geo.IP_PAUSE + 1
    assert await geo.locate_ip("49.37.11.7") is None and len(net["calls"]) == 5
    # Never longer than IP_PAUSE_MAX.
    for _ in range(12):
        clock.t += geo.IP_PAUSE_MAX + 1
        await geo.locate_ip("49.37.11.8")
    assert geo._ip_rest["seconds"] == geo.IP_PAUSE_MAX


async def test_a_failed_refresh_keeps_the_place_it_had(net, clock):
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.Response(200, json=IPAPI_BLR)
    place = await geo.locate_ip("49.37.10.1")
    clock.t += 8 * 24 * 3600
    net["routes"]["https://ipapi.co/49.37.10.1/json/"] = httpx.ConnectError("down")
    assert await geo.locate_ip("49.37.10.1") == place
    # Too old to stand in for an answer: a month on, it is no place.
    clock.t += geo.IP_STALE
    assert await geo.locate_ip("49.37.10.1") is None


async def test_a_full_memory_forgets_its_oldest_not_everything(net, clock, monkeypatch):
    monkeypatch.setattr(geo, "_IP_CACHE_MAX", 8)
    for n in range(1, 13):
        ip = f"49.37.12.{n}"
        net["routes"][f"https://ipapi.co/{ip}/json/"] = httpx.Response(200, json={**IPAPI_BLR, "ip": ip})
        await geo.locate_ip(ip)
        clock.t += 1
    # Full, it forgets the oldest quarter at a time: the newest seven are all still known.
    assert len(geo._ip_cache) <= 8
    assert all(geo.known_place(f"49.37.12.{n}")[0] for n in range(6, 13))
    assert not geo.known_place("49.37.12.1")[0]
