"""The public website's own server (app/core/site.py): with a valid
X-Site-Token its public reads count in a bucket of its own rather than the
few Vercel addresses every visitor's page comes from, and it may say which
visitor a page is for (X-Visitor-IP). Without one, nothing about a request
changes. No test here reaches the network."""
import hashlib
import secrets
from types import SimpleNamespace

import httpx
import pytest

from app.api.discover import geo
from app.config import settings
from app.core import rate_limit, site
from app.core.net import client_address

API = "/api/v1/discover"
VERCEL = "76.76.21.21"     # one of the site's few egress addresses
VISITOR = "49.37.10.1"     # the person whose page it is rendering
TOKEN = secrets.token_hex(32)
NEXT_TOKEN = secrets.token_hex(32)  # the one being rotated in


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", f"{TOKEN}, {NEXT_TOKEN}")


@pytest.fixture(autouse=True)
def net(monkeypatch):
    """ipapi.co, faked: Vercel's address is in Virginia, the visitor's in Bengaluru."""
    geo._ip_cache.clear()
    calls = []
    places = {
        VERCEL: {"city": "Ashburn", "region": "Virginia", "country_name": "United States", "latitude": 39.0438, "longitude": -77.4874},
        VISITOR: {"city": "Bengaluru", "region": "Karnataka", "country_name": "India", "latitude": 12.9634, "longitude": 77.5855},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        ip = request.url.path.strip("/").split("/")[0]
        return httpx.Response(200, json=places[ip]) if ip in places else httpx.Response(404)

    monkeypatch.setattr(geo, "_http", lambda timeout=5.0: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))
    yield calls
    geo._ip_cache.clear()


def from_site(token=TOKEN, **headers) -> dict:
    return {"X-Forwarded-For": VERCEL, "X-Site-Token": token, **headers}


async def _statuses(client, n, path, headers) -> list[int]:
    return [(await client.get(path, headers=headers)).status_code for _ in range(n)]


# ─── The rate limit ────────────────────────────────────────────────────────


async def test_without_a_token_the_sites_address_shares_one_budget(client, tokens):
    """As before: everything from Vercel's address is one visitor to the limiter."""
    h = {"X-Forwarded-For": VERCEL}
    assert set(await _statuses(client, 120, f"{API}/locate", h)) == {200}
    assert (await client.get(f"{API}/locate", headers=h)).status_code == 429


async def test_the_site_reads_in_a_bucket_of_its_own(client, tokens):
    assert set(await _statuses(client, 150, f"{API}/locate", from_site())) == {200}
    # And spent none of the address's own budget doing it: a request from the
    # same address without the token still has all 120.
    h = {"X-Forwarded-For": VERCEL}
    assert set(await _statuses(client, 120, f"{API}/locate", h)) == {200}
    assert (await client.get(f"{API}/locate", headers=h)).status_code == 429
    # While the site carries on.
    assert (await client.get(f"{API}/locate", headers=from_site())).status_code == 200


async def test_the_sites_bucket_is_bounded_and_shared_by_all_its_reads(client, tokens, monkeypatch):
    monkeypatch.setattr(settings, "SITE_RATE_LIMIT", "5/minute")
    assert set(await _statuses(client, 3, f"{API}/locate", from_site())) == {200}
    assert set(await _statuses(client, 2, f"{API}/categories", from_site())) == {200}
    r = await client.get(f"{API}/locate", headers=from_site())
    assert r.status_code == 429
    assert (await client.get(f"{API}/categories", headers=from_site())).status_code == 429
    # The pricing page and the list of interests are among the site's reads too.
    assert (await client.get("/api/v1/plans", headers=from_site())).status_code == 429
    assert (await client.get("/api/v1/interests/list", headers=from_site())).status_code == 429


async def test_each_token_has_its_own_bucket_so_one_can_be_rotated_in(client, tokens, monkeypatch):
    monkeypatch.setattr(settings, "SITE_RATE_LIMIT", "3/minute")
    assert set(await _statuses(client, 3, f"{API}/locate", from_site(TOKEN))) == {200}
    assert (await client.get(f"{API}/locate", headers=from_site(TOKEN))).status_code == 429
    assert set(await _statuses(client, 3, f"{API}/locate", from_site(NEXT_TOKEN))) == {200}


async def test_a_wrong_token_is_no_token(client, tokens):
    h = from_site(secrets.token_hex(32))
    assert set(await _statuses(client, 120, f"{API}/locate", h)) == {200}
    assert (await client.get(f"{API}/locate", headers=h)).status_code == 429


async def test_with_no_tokens_configured_the_header_means_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", "")
    h = from_site()
    assert set(await _statuses(client, 120, f"{API}/locate", h)) == {200}
    assert (await client.get(f"{API}/locate", headers=h)).status_code == 429


async def test_the_token_lifts_no_limit_outside_the_public_reads(client, tokens):
    """An invite link's preview is a GET with its own 30 a minute, against
    guessing; the site's token buys nothing there."""
    path = "/api/v1/clubs/access/invite/not-a-real-invite"
    codes = await _statuses(client, 30, path, from_site())
    assert 429 not in codes
    assert (await client.get(path, headers=from_site())).status_code == 429


async def test_the_token_lifts_no_limit_on_sign_in(client, tokens):
    # A different account each time, so the per-account lockout stays out of it.
    async def attempt(i):
        data = {"username": f"nobody{i}@example.com", "password": "wrong-password"}
        return (await client.post("/api/v1/auth/token", data=data, headers=from_site())).status_code

    codes = [await attempt(i) for i in range(20)]
    assert 429 not in codes
    assert await attempt(20) == 429


def test_only_the_public_reads_are_the_sites():
    names = rate_limit.site_read_names()
    assert {
        "app.api.discover.router.locate", "app.api.discover.router.event_page", "app.api.discover.router.sitemap",
        "app.api.subscriptions.router.pricing", "app.api.interests.router.list_interests",
    } <= names
    assert not any(n.startswith("app.api.interests.") and n != "app.api.interests.router.list_interests" for n in names)
    assert not any(n.startswith(("app.api.auth.", "app.api.club_access.", "app.api.threads.", "app.api.live.")) for n in names)
    assert not any("geo_backfill" in n or "reconcile" in n for n in names)  # the admin's discover routes are not reads
    # The hook the bucket hangs on: slowapi's every @limit-ed route goes through it.
    assert type(rate_limit.limiter)._check_request_limit is rate_limit.SiteAwareLimiter._check_request_limit


# ─── The visitor's address ────────────────────────────────────────────────


async def _city(client, headers) -> str:
    r = await client.get(f"{API}/locate", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["city"]


async def test_the_site_names_the_visitor_and_is_believed(client, tokens):
    assert await _city(client, from_site(**{"X-Visitor-IP": VISITOR})) == "Bengaluru"
    # Without saying, the site is placed where it is.
    assert await _city(client, from_site()) == "Ashburn"


async def test_a_visitor_header_without_the_token_is_ignored(client, tokens, net):
    # Spoofed by a caller of its own: it is placed by its own address.
    assert await _city(client, {"X-Forwarded-For": VERCEL, "X-Visitor-IP": VISITOR}) == "Ashburn"
    # Or behind a token that is not one.
    assert await _city(client, from_site(secrets.token_hex(32), **{"X-Visitor-IP": VISITOR})) == "Ashburn"
    assert not any(VISITOR in url for url in net)


async def test_a_visitor_header_that_is_not_an_address_is_ignored(client, tokens):
    assert await _city(client, from_site(**{"X-Visitor-IP": "not-an-address"})) == "Ashburn"
    # A whole forwarded chain: its first entry is the visitor.
    assert await _city(client, from_site(**{"X-Visitor-IP": f"{VISITOR}, 10.0.0.1"})) == "Bengaluru"


def _request(headers, peer="10.0.0.2"):
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()}, client=SimpleNamespace(host=peer))


def test_the_visitor_header_is_for_placing_only(tokens):
    """Sign-in records, audit rows and every per-address limit read the
    connection's address (app.core.net), which never looks at X-Visitor-IP."""
    request = _request(from_site(**{"X-Visitor-IP": VISITOR}))
    assert geo.client_ip(request) == VISITOR
    assert client_address(request) == VERCEL


# ─── The token itself ─────────────────────────────────────────────────────


def test_a_valid_token_is_named_by_its_digest_never_itself(tokens):
    name = site.token_id(_request(from_site()))
    assert name == "site:" + hashlib.sha256(TOKEN.encode()).hexdigest()[:12]
    assert TOKEN not in name
    assert site.token_id(_request(from_site(f"  {TOKEN} "))) == name  # stray spaces from a pasted value
    assert site.token_id(_request(from_site(NEXT_TOKEN))) != name
    assert site.token_id(_request(from_site(TOKEN[:-1]))) is None
    assert site.token_id(_request({"X-Forwarded-For": VERCEL})) is None


def test_a_short_token_is_never_accepted(monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", "letmein, " + TOKEN)
    assert site.token_id(_request(from_site("letmein"))) is None
    assert site.token_id(_request(from_site(TOKEN))) is not None


def test_tokens_are_compared_in_constant_time(tokens, monkeypatch):
    """Every configured token is compared, by hmac.compare_digest, on digests
    of one length: neither which token matched nor how long a guess was
    shows in the time it takes."""
    compared = []
    real = site.hmac.compare_digest

    def spy(a, b):
        compared.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(site.hmac, "compare_digest", spy)
    for given in (TOKEN, NEXT_TOKEN, "x", "y" * 500):
        compared.clear()
        site.token_id(_request(from_site(given)))
        assert compared == [(32, 32), (32, 32)], given
