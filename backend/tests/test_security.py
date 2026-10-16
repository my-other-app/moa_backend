"""Security: the door keeps a record, locks after too many wrong passwords, names devices and places,
mails a new device, and ends other sessions at once."""
import pytest

from app.api.security import service as security
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.conftest import auth_header

PLACES = {"103.21.58.14": "Pune, Maharashtra", "185.220.101.4": "Frankfurt, Germany", "49.36.112.7": "Pune, Maharashtra"}
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
LINUX = "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101"


@pytest.fixture
async def world(client, session, make_club, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)  # the per-minute throttle is not what this file tests
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def fake_lookup(ip):
        return PLACES.get(ip)

    monkeypatch.setattr(security, "lookup_place", fake_lookup)
    owner, club = await make_club(name="Circuit Society", email="security@example.com")
    await session.commit()
    return dict(owner=owner, club=club, sent=sent)


def _from(ip, ua):
    return {"x-forwarded-for": ip, "user-agent": ua}


async def _login(client, ip, ua, password="Password123!"):
    return await client.post("/api/v1/auth/token", data={"username": "security@example.com", "password": password}, headers=_from(ip, ua))


def test_user_agents_become_device_names():
    assert security.parse_agent(WINDOWS) == ("Chrome on Windows", "computer")
    assert security.parse_agent(IPHONE) == ("Safari on iPhone", "phone")
    assert security.parse_agent(LINUX) == ("Unknown browser on Linux", "computer")
    assert security.parse_agent("python-httpx/0.28") == ("App or script", "unknown")
    assert security.parse_agent(None) == ("Unknown device", "unknown")


async def test_the_page_shows_this_device_its_place_and_the_history(client, world):
    r = await _login(client, "103.21.58.14", WINDOWS)
    assert r.status_code == 200, r.text
    h = auth_header(r.json()["access_token"])
    s = (await client.get("/api/v1/auth/security/overview", headers=h)).json()
    assert s["account"] == "Circuit Society"
    assert len(s["sessions"]) == 1 and s["sessions"][0]["current"] is True
    assert s["sessions"][0] | {"id": "", "created_at": "", "last_seen_at": ""} == {"id": "", "device": "Chrome on Windows", "kind": "computer", "place": "Pune, Maharashtra", "ip_address": "103.21.58.14", "created_at": "", "last_seen_at": "", "current": True, "who": None}
    assert s["counts"]["active_sessions"] == 1 and s["counts"]["sessions_30d"] == 1 and s["counts"]["places_30d"] == 1 and s["counts"]["failed_30d"] == 0
    assert s["by_place"] == [{"place": "Pune, Maharashtra", "total": 1, "failed": 0}]
    assert s["history"][0]["result"] == "signed_in" and s["history"][0]["method"] == "password" and s["history"][0]["device"] == "Chrome on Windows"
    assert s["two_factor"] == {"enabled": False, "recovery_codes_remaining": 0} and s["notify_new_device"] is True
    assert s["lockout"] == {"max_failed": 5, "window_minutes": 15}
    # The first sign-in from a device earns an email; the same device again does not.
    assert len(world["sent"]) == 1 and world["sent"][0]["recipients"] == ["security@example.com"] and "Chrome on Windows" in world["sent"][0]["text"] and "Pune, Maharashtra" in world["sent"][0]["text"]
    await _login(client, "103.21.58.14", WINDOWS)
    assert len(world["sent"]) == 1
    # Switched off, a genuinely new device stays quiet.
    assert (await client.patch("/api/v1/auth/security/preferences", json={"notify_new_device": False}, headers=h)).json() == {"notify_new_device": False}
    await _login(client, "49.36.112.7", IPHONE)
    assert len(world["sent"]) == 1
    assert (await client.get("/api/v1/auth/security/overview", headers=h)).json()["notify_new_device"] is False


async def test_five_wrong_passwords_from_one_address_lock_that_door(client, world):
    for _ in range(5):
        assert (await _login(client, "185.220.101.4", LINUX, password="nope")).status_code == 401
    blocked = await _login(client, "185.220.101.4", LINUX, password="nope")
    assert blocked.status_code == 429 and blocked.json()["error_code"] == "LOGIN_LOCKED"
    # Even the right password waits now, from that address...
    assert (await _login(client, "185.220.101.4", LINUX)).status_code == 429
    # ...while the owner at home walks straight in.
    me = await _login(client, "103.21.58.14", WINDOWS)
    assert me.status_code == 200
    s = (await client.get("/api/v1/auth/security/overview", headers=auth_header(me.json()["access_token"]))).json()
    assert s["counts"]["failed_30d"] == 7 and s["counts"]["last_blocked_place"] == "Frankfurt, Germany"
    assert s["by_place"][0] == {"place": "Frankfurt, Germany", "total": 7, "failed": 7}
    results = [r["result"] for r in s["history"]]
    assert results[0] == "signed_in" and results[1:3] == ["blocked", "blocked"] and results.count("wrong_password") == 5
    assert all(r["device"] == "Unknown browser on Linux" for r in s["history"][1:])


async def test_signing_out_others_ends_them_at_once(client, world):
    laptop = (await _login(client, "103.21.58.14", WINDOWS)).json()["access_token"]
    phone = (await _login(client, "49.36.112.7", IPHONE)).json()["access_token"]
    assert (await client.get("/api/v1/auth/security/overview", headers=auth_header(laptop))).json()["counts"]["active_sessions"] == 2
    assert (await client.post("/api/v1/auth/security/sign-out-others", headers=auth_header(phone))).json() == {"signed_out": 1}
    # The phone keeps going; the laptop's token is dead before any refresh.
    s = (await client.get("/api/v1/auth/security/overview", headers=auth_header(phone))).json()
    assert [x["device"] for x in s["sessions"]] == ["Safari on iPhone"] and s["sessions"][0]["current"] is True
    assert (await client.get("/api/v1/auth/security/overview", headers=auth_header(laptop))).status_code == 401
    assert (await client.get("/api/v1/clubs/info", headers=auth_header(laptop))).status_code == 401


async def test_a_session_from_before_the_record_still_shows_as_a_sign_in(client, session, world):
    from app.api.auth import sessions as session_store

    class Req:
        headers = {"user-agent": WINDOWS, "x-forwarded-for": "103.21.58.14"}
        client = None

    await session_store.start(session, world["owner"].id, Req())
    await session.commit()
    me = await _login(client, "49.36.112.7", IPHONE)
    s = (await client.get("/api/v1/auth/security/overview", headers=auth_header(me.json()["access_token"]))).json()
    assert s["counts"]["sessions_30d"] == 2 and s["counts"]["active_sessions"] == 2
    assert [h["device"] for h in s["history"]] == ["Safari on iPhone", "Chrome on Windows"] and all(h["result"] == "signed_in" for h in s["history"])
