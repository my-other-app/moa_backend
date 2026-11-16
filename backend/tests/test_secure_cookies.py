"""Whether the API's cookies are Secure (app/core/cookies.py): always in
production, whatever scheme the proxies pass on; elsewhere as the request
came."""

from types import SimpleNamespace

from app.config import settings
from app.core import cookies


def _request(scheme: str):
    return SimpleNamespace(url=SimpleNamespace(scheme=scheme))


async def test_a_sign_in_behind_a_proxy_that_drops_the_scheme_still_sets_secure_cookies(
    client, make_user, monkeypatch
):
    await make_user(email="secure-cookie@example.com", username="secure-cookie")
    plain = await client.post(
        "/api/v1/auth/token",
        data={"username": "secure-cookie@example.com", "password": "Password123!"},
    )
    assert plain.status_code == 200 and not any(
        "secure" in c.lower() for c in plain.headers.get_list("set-cookie")
    )
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    r = await client.post(
        "/api/v1/auth/token",
        data={"username": "secure-cookie@example.com", "password": "Password123!"},
    )
    assert r.status_code == 200, r.text
    lines = [
        c
        for c in r.headers.get_list("set-cookie")
        if c.split("=", 1)[0] in ("access_token", "refresh_token")
    ]
    assert lines and all("secure" in c.lower() for c in lines)


def test_production_cookies_are_always_secure(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", False)
    assert (
        cookies.secure(_request("http"))
        and cookies.secure(_request("https"))
        and cookies.secure()
    )


def test_elsewhere_the_scheme_decides(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "test")
    assert cookies.secure(_request("https")) and not cookies.secure(_request("http"))
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", True)
    assert not cookies.secure(_request("http"))
