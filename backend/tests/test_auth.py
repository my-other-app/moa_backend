"""Covers the auth flow and the token-revocation fix: password change/reset
must invalidate previously issued access + refresh tokens."""
from tests.conftest import auth_header


async def _login(client, username, password):
    return await client.post(
        "/api/v1/auth/token", data={"username": username, "password": password}
    )


async def test_login_succeeds_with_correct_credentials(client, make_user):
    await make_user(email="alice@example.com", username="alice", password="Password123!")
    response = await _login(client, "alice", "Password123!")
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_fails_with_wrong_password(client, make_user):
    await make_user(email="bob@example.com", username="bob", password="Password123!")
    response = await _login(client, "bob", "WrongPassword!")
    assert response.status_code == 401


async def test_password_change_invalidates_old_tokens(client, make_user):
    await make_user(email="carol@example.com", username="carol", password="OldPassword123!")

    login = await _login(client, "carol", "OldPassword123!")
    old_access = login.json()["access_token"]
    old_refresh = login.json()["refresh_token"]

    # Old access token works before the change.
    me = await client.get("/api/v1/auth/me", headers=auth_header(old_access))
    assert me.status_code == 200

    change = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "OldPassword123!", "new_password": "NewPassword456!"},
        headers=auth_header(old_access),
    )
    assert change.status_code == 200

    # The old access token must now be rejected.
    me_after = await client.get("/api/v1/auth/me", headers=auth_header(old_access))
    assert me_after.status_code == 401

    # The old refresh token must also be rejected.
    refresh_after = await client.post(
        "/api/v1/auth/refresh", data={"token": old_refresh}
    )
    assert refresh_after.status_code == 401

    # A fresh login with the new password works.
    fresh_login = await _login(client, "carol", "NewPassword456!")
    assert fresh_login.status_code == 200
    fresh_me = await client.get(
        "/api/v1/auth/me", headers=auth_header(fresh_login.json()["access_token"])
    )
    assert fresh_me.status_code == 200


async def test_login_is_rate_limited(client, make_user):
    await make_user(email="dave@example.com", username="dave", password="Password123!")

    for _ in range(5):
        response = await _login(client, "dave", "WrongPassword!")
        assert response.status_code == 401

    limited = await _login(client, "dave", "WrongPassword!")
    assert limited.status_code == 429
