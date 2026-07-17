"""Covers the additive httpOnly-cookie auth path added for moa_clubs: login
and refresh must also set access_token/refresh_token cookies (in addition to
the existing JSON body), get_current_user must accept a cookie in place of
an Authorization header, /auth/refresh must accept the refresh token via
cookie instead of the `token` form field, and /auth/logout must clear both
cookies. None of this should change the existing header/JSON-body behavior
that the mobile app and the other two web frontends rely on."""


async def test_login_sets_cookies_and_keeps_json_body(client, make_user):
    await make_user(email="dana@example.com", username="dana", password="Password123!")
    response = await client.post(
        "/api/v1/auth/token", data={"username": "dana", "password": "Password123!"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert response.cookies.get("access_token") == body["access_token"]
    assert response.cookies.get("refresh_token") == body["refresh_token"]


async def test_cookie_alone_authenticates_a_request(client, make_user):
    await make_user(email="erin@example.com", username="erin", password="Password123!")
    login = await client.post(
        "/api/v1/auth/token", data={"username": "erin", "password": "Password123!"}
    )
    assert login.status_code == 200

    # No Authorization header at all — only the cookie jar from the login
    # response (httpx's AsyncClient persists cookies across requests
    # automatically within the same client instance).
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "erin@example.com"


async def test_refresh_accepts_cookie_without_form_field(client, make_user):
    await make_user(email="frank@example.com", username="frank", password="Password123!")
    login = await client.post(
        "/api/v1/auth/token", data={"username": "frank", "password": "Password123!"}
    )
    assert login.status_code == 200

    # No `token` form field — relies entirely on the refresh_token cookie.
    refresh = await client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 200
    body = refresh.json()
    assert body["access_token"]
    assert refresh.cookies.get("access_token") == body["access_token"]


async def test_refresh_without_token_or_cookie_is_rejected(client):
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_logout_clears_cookies(client, make_user):
    await make_user(email="grace@example.com", username="grace", password="Password123!")
    login = await client.post(
        "/api/v1/auth/token", data={"username": "grace", "password": "Password123!"}
    )
    assert login.status_code == 200
    assert client.cookies.get("access_token")

    logout = await client.post("/api/v1/auth/logout")
    assert logout.status_code == 200

    # A subsequent request relying solely on the (now-cleared) cookie must
    # no longer be authenticated.
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401
