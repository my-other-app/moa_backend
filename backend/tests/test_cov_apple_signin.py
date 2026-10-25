"""Sign in with Apple (POST /auth/apple): Apple's token checked for real --
signature, audience, issuer, expiry -- against a key this test holds, then
the account found or made from the address Apple signed, never the one typed
beside it."""
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.api.auth import service as auth_service
from app.api.users.models import UserTypes, Users
from app.config import settings

URL = "/api/v1/auth/apple"
ISSUER = "https://appleid.apple.com"
APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    """Apple's published keys: this test's one public key (or no answer at all)."""

    reachable = True

    def __init__(self, url, **kwargs):
        assert url == auth_service.APPLE_KEYS_URL

    def get_signing_key_from_jwt(self, token):
        if not _Keys.reachable:
            raise jwt.PyJWKClientConnectionError("appleid.apple.com is unreachable")
        return type("Key", (), {"key": APPLE_KEY.public_key()})()


@pytest.fixture(autouse=True)
def apple(monkeypatch):
    monkeypatch.setattr(jwt, "PyJWKClient", _Keys)
    monkeypatch.setattr(auth_service, "_apple_keys", None)
    _Keys.reachable = True
    yield
    monkeypatch.setattr(auth_service, "_apple_keys", None)


def _token(*, key=APPLE_KEY, sub=None, email="", aud=None, iss=ISSUER, expires_in=600) -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud or settings.APPLE_CLIENT_ID, "sub": sub or f"001234.{uuid.uuid4().hex}", "iat": now, "exp": now + expires_in}
    if email:
        claims["email"] = email
        claims["email_verified"] = "true"
    return jwt.encode(claims, key, algorithm="RS256")


async def _sign_in(client, token, **extra):
    return await client.post(URL, json={"identity_token": token, "authorization_code": "c0de", **extra})


async def _account(session, email) -> Users | None:
    return await session.scalar(select(Users).where(Users.email == email, Users.is_deleted == False).execution_options(populate_existing=True))  # noqa: E712


async def test_a_first_sign_in_makes_the_account_and_the_next_finds_it(client, session):
    sub = "001234.first"
    first = await _sign_in(client, _token(sub=sub, email="ada-apple@example.com"), user_name="Ada Lovelace")
    assert first.status_code == 200, first.text
    assert first.json()["access_token"] and first.json()["refresh_token"] and first.json()["token_type"] == "Bearer"
    user = await _account(session, "ada-apple@example.com")
    assert user.full_name == "Ada Lovelace" and user.user_type == UserTypes.app_user and user.provider.value == "apple"
    assert user.email_verified  # Apple vouched for the address

    again = await _sign_in(client, _token(sub=sub, email="ada-apple@example.com"))
    assert again.status_code == 200
    assert len((await session.scalars(select(Users).where(Users.email == "ada-apple@example.com"))).all()) == 1
    # The tokens are the account's: they open its profile.
    me = await client.get("/api/v1/user/profile/me", headers={"Authorization": f"Bearer {again.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "ada-apple@example.com"


async def test_without_a_name_the_account_is_called_apple_user(client, session):
    assert (await _sign_in(client, _token(email="anon-apple@example.com"))).status_code == 200
    assert (await _account(session, "anon-apple@example.com")).full_name == "Apple User"


async def test_the_address_typed_beside_the_token_is_never_used(client, session, make_user):
    victim = await make_user(email="victim-apple@example.com")
    await session.commit()
    r = await _sign_in(client, _token(sub="001234.hidden"), user_email="victim-apple@example.com")
    assert r.status_code == 200
    # Apple signed no address: a private placeholder of its own, not the victim's account.
    me = (await client.get("/api/v1/user/profile/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})).json()
    assert me["email"] == "001234.hidden@privaterelay.appleid.com" and me["id"] != victim.id


async def test_a_token_apple_did_not_sign_for_this_app_is_refused(client, session):
    for bad in (
        _token(key=STRANGER_KEY, email="forged-apple@example.com"),       # someone else's key
        _token(email="forged-apple@example.com", aud="com.someone.else"),  # another app's
        _token(email="forged-apple@example.com", iss="https://evil.example.com"),
        "not-a-jwt",
    ):
        r = await _sign_in(client, bad)
        assert r.status_code == 401, (bad[:20], r.text)
    expired = await _sign_in(client, _token(email="forged-apple@example.com", expires_in=-60))
    assert expired.status_code == 401 and "expired" in expired.json()["message"]
    assert await _account(session, "forged-apple@example.com") is None


async def test_apples_keys_out_of_reach_is_a_503_not_a_500(client):
    _Keys.reachable = False
    r = await _sign_in(client, _token(email="later-apple@example.com"))
    assert r.status_code == 503 and r.json()["error_code"] == "APPLE_UNAVAILABLE"


async def test_a_suspended_account_is_refused_at_the_door(client, session, make_user):
    user = await make_user(email="banned-apple@example.com")
    user.is_active = False
    await session.commit()
    r = await _sign_in(client, _token(email="banned-apple@example.com"))
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_SUSPENDED"
