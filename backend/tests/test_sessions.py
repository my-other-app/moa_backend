"""Per-session sign-out.

`token_version` could already end every session for a user at once, which is
right for a password change or a suspension and wrong for "sign out my old
laptop" -- there was no way to see which sessions existed, let alone end one.
"""

import pytest

from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_signing_in_records_a_session(client, make_user):
    await make_user(email="sessionuser@example.com", password="Password123!")

    login = await client.post(
        "/api/v1/auth/token",
        data={"username": "sessionuser@example.com", "password": "Password123!"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    sessions = await client.get("/api/v1/auth/sessions", headers=auth_header(token))
    assert sessions.status_code == 200, sessions.text
    assert len(sessions.json()) == 1


@pytest.mark.asyncio
async def test_the_session_in_use_is_marked_current(client, make_user):
    """So nobody ends the one they are sitting in by accident."""
    await make_user(email="current@example.com", password="Password123!")
    login = await client.post(
        "/api/v1/auth/token",
        data={"username": "current@example.com", "password": "Password123!"},
    )
    token = login.json()["access_token"]

    sessions = await client.get("/api/v1/auth/sessions", headers=auth_header(token))

    # Identified from the access-token cookie the login set.
    assert any(s["current"] for s in sessions.json())


@pytest.mark.asyncio
async def test_two_sign_ins_are_two_sessions(client, make_user):
    await make_user(email="twice@example.com", password="Password123!")
    creds = {"username": "twice@example.com", "password": "Password123!"}

    first = await client.post("/api/v1/auth/token", data=creds)
    await client.post("/api/v1/auth/token", data=creds)

    sessions = await client.get(
        "/api/v1/auth/sessions", headers=auth_header(first.json()["access_token"])
    )
    assert len(sessions.json()) == 2


@pytest.mark.asyncio
async def test_revoking_one_session_stops_its_refresh(client, make_user):
    """
    The whole point: one session ends while the other keeps working. It dies
    at the next refresh rather than surviving until the token expires.
    """
    await make_user(email="revoke@example.com", password="Password123!")
    creds = {"username": "revoke@example.com", "password": "Password123!"}

    doomed = await client.post("/api/v1/auth/token", data=creds)
    keeper = await client.post("/api/v1/auth/token", data=creds)

    listing = await client.get(
        "/api/v1/auth/sessions", headers=auth_header(keeper.json()["access_token"])
    )
    doomed_id = [s["id"] for s in listing.json() if not s["current"]][0]

    revoked = await client.delete(
        f"/api/v1/auth/sessions/{doomed_id}",
        headers=auth_header(keeper.json()["access_token"]),
    )
    assert revoked.status_code == 200, revoked.text

    dead = await client.post(
        "/api/v1/auth/refresh", data={"token": doomed.json()["refresh_token"]}
    )
    assert dead.status_code == 401, dead.text

    alive = await client.post(
        "/api/v1/auth/refresh", data={"token": keeper.json()["refresh_token"]}
    )
    assert alive.status_code == 200, alive.text


@pytest.mark.asyncio
async def test_one_user_cannot_revoke_another_users_session(client, make_user):
    await make_user(email="mineown@example.com", password="Password123!")
    await make_user(email="notmine@example.com", password="Password123!")

    victim = await client.post(
        "/api/v1/auth/token",
        data={"username": "notmine@example.com", "password": "Password123!"},
    )
    attacker = await client.post(
        "/api/v1/auth/token",
        data={"username": "mineown@example.com", "password": "Password123!"},
    )

    victim_sessions = await client.get(
        "/api/v1/auth/sessions", headers=auth_header(victim.json()["access_token"])
    )
    target_id = victim_sessions.json()[0]["id"]

    response = await client.delete(
        f"/api/v1/auth/sessions/{target_id}",
        headers=auth_header(attacker.json()["access_token"]),
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_signing_out_everything_ends_every_session(client, make_user):
    await make_user(email="sweep@example.com", password="Password123!")
    creds = {"username": "sweep@example.com", "password": "Password123!"}

    one = await client.post("/api/v1/auth/token", data=creds)
    two = await client.post("/api/v1/auth/token", data=creds)

    await client.delete(
        "/api/v1/auth/sessions", headers=auth_header(two.json()["access_token"])
    )

    for issued in (one, two):
        dead = await client.post(
            "/api/v1/auth/refresh", data={"token": issued.json()["refresh_token"]}
        )
        assert dead.status_code == 401


@pytest.mark.asyncio
async def test_a_token_without_a_session_id_still_refreshes(client, make_user):
    """
    Tokens issued before sessions existed carry no sid. Failing them closed
    would have signed out every user on deploy.
    """
    from datetime import timedelta

    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    user = await make_user(email="legacy@example.com")
    legacy = create_access_token(
        data=AuthTokenData(
            user_id=user.id, token_type="refresh_token",
            token_version=user.token_version,
        ).model_dump(exclude={"sid"}),
        expires_delta=timedelta(days=1),
    )

    response = await client.post("/api/v1/auth/refresh", data={"token": legacy})

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_listing_sessions_requires_authentication(client):
    response = await client.get("/api/v1/auth/sessions")
    assert response.status_code in (401, 403), response.text
