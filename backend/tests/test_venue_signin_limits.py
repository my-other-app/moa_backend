"""Sign-in and refresh limits sized for a venue on one address, and a
sign-out that ends the session on the server.

A hall's Wi-Fi is one address for every phone in it: the refresh limit was
20 a minute per address, so the 21st phone to refresh in a minute got 429.
Now a refresh is limited per session, with a hall-sized ceiling per address.
And /auth/logout only cleared cookies, so a copied refresh cookie kept
working after "Sign out"; now it ends the session, even once the access
token has expired."""
from app.api.auth import limits as auth_limits
from app.config import settings
from tests.conftest import auth_header

API = "/api/v1"
HALL = {"x-forwarded-for": "49.36.20.20"}


async def _sign_in(client, email: str) -> dict:
    r = await client.post(f"{API}/auth/token", data={"username": email, "password": "Password123!"}, headers=HALL)
    assert r.status_code == 200, r.text
    return r.json()


async def _refresh(client, token: str):
    return await client.post(f"{API}/auth/refresh", data={"token": token}, headers=HALL)


async def test_many_phones_behind_one_address_all_refresh(client, make_user):
    await make_user(email="hall-phones@example.com", password="Password123!")
    tokens = [(await _sign_in(client, "hall-phones@example.com"))["refresh_token"] for _ in range(25)]
    client.cookies.clear()
    codes = [(await _refresh(client, t)).status_code for t in tokens]
    assert codes == [200] * 25


async def test_one_session_refreshing_in_a_loop_meets_its_own_limit(client, make_user, monkeypatch):
    assert settings.AUTH_REFRESH_PER_SESSION == "10/minute"
    await make_user(email="loop-phone@example.com", password="Password123!")
    await make_user(email="other-phone@example.com", password="Password123!")
    token = (await _sign_in(client, "loop-phone@example.com"))["refresh_token"]
    other = (await _sign_in(client, "other-phone@example.com"))["refresh_token"]
    client.cookies.clear()
    for _ in range(10):
        r = await _refresh(client, token)
        assert r.status_code == 200, r.text
        token = r.json()["refresh_token"]  # rotated: the same session, a new token
    limited = await _refresh(client, token)
    assert limited.status_code == 429 and limited.json()["error_code"] == "RATE_LIMITED"
    assert limited.json()["errors"]["retry_after"] >= 1
    # The phone beside it, on the same address, is not held up.
    assert (await _refresh(client, other)).status_code == 200


async def test_the_address_is_only_a_ceiling_for_a_hall(client, make_user, monkeypatch):
    assert auth_limits.refresh_per_address() == "3000/minute"
    assert auth_limits.sign_in_per_address() == "600/minute"
    assert auth_limits.password_per_address() == "300/minute"
    monkeypatch.setattr(settings, "AUTH_REFRESH_PER_ADDRESS", "3/minute")
    await make_user(email="ceiling@example.com", password="Password123!")
    tokens = [(await _sign_in(client, "ceiling@example.com"))["refresh_token"] for _ in range(4)]
    client.cookies.clear()
    assert [(await _refresh(client, t)).status_code for t in tokens] == [200, 200, 200, 429]
