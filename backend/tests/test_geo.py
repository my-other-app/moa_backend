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
    geo._tried.clear()
    yield
    geo._ip_cache.clear()
    geo._tried.clear()


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
