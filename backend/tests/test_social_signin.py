"""Covers a real production bug: google_signin/apple_signin (and the shared
password-login path) commit() then hand the user object to
create_access_refresh_tokens, which reads user.id/token_version. commit()
expires those attributes, and depending on connection-pool timing the
resulting lazy-load can hit a pool_pre_ping/greenlet interaction bug on the
async engine (MissingGreenlet), turning a normal sign-in into a 500. Fixed
by an explicit session.refresh(user) right after commit in all three call
sites — this test exercises the actual endpoint end-to-end (not just the
service function) so a regression shows up as a real HTTP failure."""
from app.api.auth import service as auth_service


async def test_google_signin_returns_tokens_for_a_new_user(client, monkeypatch):
    def _fake_verify(id_token_str, request, client_id):
        return {
            "email": "newgoogleuser@example.com",
            "email_verified": True,
            "name": "New Google User",
        }

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", _fake_verify)

    response = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "fake-token", "platform": "android"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]


async def test_google_signin_merges_an_existing_guest_account(
    client, session, monkeypatch, make_user
):
    from app.api.users.models import UserTypes

    email = "guestmerge@example.com"
    await make_user(email=email, user_type=UserTypes.guest)
    await session.commit()

    def _fake_verify(id_token_str, request, client_id):
        return {"email": email, "email_verified": True, "name": "Merged User"}

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", _fake_verify)

    response = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "fake-token", "platform": "android"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]
