"""Covers the soft-deleted-user login bypass fix: get_user() must exclude
is_deleted rows, so a soft-deleted account can neither log in nor keep using
an existing token."""
from tests.conftest import auth_header


async def test_soft_deleted_user_cannot_log_in(client, make_user, session):
    user = await make_user(email="deleted1@example.com", username="deleted1", password="Password123!")
    user.is_deleted = True
    await session.commit()

    response = await client.post(
        "/api/v1/auth/token", data={"username": "deleted1", "password": "Password123!"}
    )
    assert response.status_code == 401


async def test_soft_deleted_users_existing_token_is_rejected(client, make_user, mint_token, session):
    user = await make_user(email="deleted2@example.com", username="deleted2", password="Password123!")
    token = await mint_token(user)

    # Token works while the account is active.
    me = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert me.status_code == 200

    user.is_deleted = True
    await session.commit()

    me_after = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert me_after.status_code == 401
