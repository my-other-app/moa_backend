"""Revoking Sign in with Apple's token when an account deletes itself (App
Store guideline 5.1.1(v)).

The code the app sends at sign-in is exchanged for a refresh token, kept
encrypted; DELETE /user/delete revokes it. Apple is never called: its two
endpoints are a fake here that records what it was sent.
"""
import time
import uuid

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from sqlalchemy import select

from app.api.auth import apple_tokens
from app.api.auth import service as auth_service
from app.api.users.models import Users
from app.config import settings
from app.core import config_checks

APPLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SIGNIN_KEY = ec.generate_private_key(ec.SECP256R1())
SIGNIN_PEM = SIGNIN_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()


class _Keys:
    def __init__(self, url, **kwargs):
        pass

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": APPLE_KEY.public_key()})()


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeApple:
    """Apple's /auth/token and /auth/revoke."""

    def __init__(self):
        self.calls = []
        self.token_status = 200
        self.revoke_status = 200
        self.unreachable = False

    async def post(self, url, data):
        self.calls.append((url, dict(data)))
        if self.unreachable:
            raise OSError("appleid.apple.com is unreachable")
        if url == apple_tokens.TOKEN_URL:
            if self.token_status != 200:
                return _Response(self.token_status, {"error": "invalid_grant"})
            return _Response(200, {"access_token": "a", "refresh_token": f"rt-{data['code']}", "id_token": "i"})
        assert url == apple_tokens.REVOKE_URL
        return _Response(self.revoke_status, {} if self.revoke_status == 200 else {"error": "invalid_client"})

    def sent(self, url):
        return [d for u, d in self.calls if u == url]


@pytest.fixture
def apple(monkeypatch):
    monkeypatch.setattr(jwt, "PyJWKClient", _Keys)
    monkeypatch.setattr(auth_service, "_apple_keys", None)
    monkeypatch.setattr(settings, "APPLE_TEAM_ID", "TEAM123456")
    monkeypatch.setattr(settings, "APPLE_KEY_ID", "KEY1234567")
    # As an env file holds it: one line, with \n for each break.
    monkeypatch.setattr(settings, "APPLE_PRIVATE_KEY", SIGNIN_PEM.replace("\n", "\\n"))
    monkeypatch.setattr(settings, "APPLE_TOKENS_SECRET", Fernet.generate_key().decode())
    fake = FakeApple()
    monkeypatch.setattr(apple_tokens, "_post", fake.post)
    yield fake
    monkeypatch.setattr(auth_service, "_apple_keys", None)


def _identity(email: str) -> str:
    now = int(time.time())
    claims = {"iss": "https://appleid.apple.com", "aud": settings.APPLE_CLIENT_ID, "sub": f"001.{uuid.uuid4().hex}",
              "iat": now, "exp": now + 600, "email": email, "email_verified": "true"}
    return jwt.encode(claims, APPLE_KEY, algorithm="RS256")


async def _sign_in(client, email, code="c0de"):
    r = await client.post("/api/v1/auth/apple", json={"identity_token": _identity(email), "authorization_code": code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _account(session, user_id) -> Users:
    return await session.scalar(select(Users).where(Users.id == user_id).execution_options(populate_existing=True, include_deleted=True))


async def _user_id(session, email) -> int:
    return await session.scalar(select(Users.id).where(Users.email == email))


async def test_sign_in_keeps_apples_refresh_token_encrypted(client, session, apple):
    await _sign_in(client, "keep@example.com", code="first")
    (sent,) = apple.sent(apple_tokens.TOKEN_URL)
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "first"
    assert sent["client_id"] == settings.APPLE_CLIENT_ID
    # The client secret is an ES256 JWT from the team, for the app, signed with the key.
    secret = jwt.decode(sent["client_secret"], SIGNIN_KEY.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com")
    assert secret["iss"] == "TEAM123456" and secret["sub"] == settings.APPLE_CLIENT_ID
    assert jwt.get_unverified_header(sent["client_secret"])["kid"] == "KEY1234567"

    user = await _account(session, await _user_id(session, "keep@example.com"))
    assert user.apple_refresh_token and "rt-first" not in user.apple_refresh_token
    assert apple_tokens._keys.decrypt(user.apple_refresh_token) == "rt-first"

    # The next sign-in's code replaces it.
    await _sign_in(client, "keep@example.com", code="second")
    user = await _account(session, user.id)
    assert apple_tokens._keys.decrypt(user.apple_refresh_token) == "rt-second"


async def test_deleting_the_account_revokes_the_token(client, session, apple):
    access = await _sign_in(client, "leave@example.com", code="bye")
    user_id = await _user_id(session, "leave@example.com")
    r = await client.delete("/api/v1/user/delete", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    (revoked,) = apple.sent(apple_tokens.REVOKE_URL)
    assert revoked["token"] == "rt-bye" and revoked["token_type_hint"] == "refresh_token"
    assert revoked["client_id"] == settings.APPLE_CLIENT_ID
    user = await _account(session, user_id)
    assert user.is_deleted and user.apple_refresh_token is None


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
async def test_a_failed_revocation_does_not_stop_the_deletion(client, session, apple, failure, caplog):
    access = await _sign_in(client, f"stay-{failure}@example.com")
    user_id = await _user_id(session, f"stay-{failure}@example.com")
    if failure == "refused":
        apple.revoke_status = 400
    else:
        apple.unreachable = True
    r = await client.delete("/api/v1/user/delete", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    assert len(apple.sent(apple_tokens.REVOKE_URL)) == 1
    user = await _account(session, user_id)
    assert user.is_deleted and user.apple_refresh_token is None
    assert "revoke" in caplog.text


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
async def test_a_failed_exchange_does_not_stop_the_sign_in(client, session, apple, failure):
    if failure == "refused":
        apple.token_status = 400
    else:
        apple.unreachable = True
    await _sign_in(client, f"in-{failure}@example.com")
    user = await _account(session, await _user_id(session, f"in-{failure}@example.com"))
    assert user.apple_refresh_token is None


async def test_without_apples_settings_nothing_is_kept_or_called(client, session, apple, monkeypatch):
    monkeypatch.setattr(settings, "APPLE_KEY_ID", "")
    access = await _sign_in(client, "plain@example.com")
    user = await _account(session, await _user_id(session, "plain@example.com"))
    assert user.apple_refresh_token is None
    r = await client.delete("/api/v1/user/delete", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    assert apple.calls == []


class _Settings:
    """Production settings with nothing else wrong, for the checks."""

    def __init__(self, **values):
        self.__dict__.update(values)


def _apple_findings(**values):
    base = dict(ENV="production", DEBUG=False)
    return {p.setting: p for p in config_checks._pairs(_Settings(**base, **values)) + config_checks._formats(_Settings(**base, **values)) + config_checks._warnings(_Settings(**base, **values)) if p.setting.startswith("APPLE_")}


def test_the_config_checks_want_all_of_apples_settings_or_none():
    full = dict(APPLE_TEAM_ID="TEAM123456", APPLE_KEY_ID="KEY1234567", APPLE_PRIVATE_KEY=SIGNIN_PEM.replace("\n", "\\n"),
                APPLE_TOKENS_SECRET=Fernet.generate_key().decode())
    assert _apple_findings(**full) == {}

    half = _apple_findings(APPLE_KEY_ID="KEY1234567")
    assert {"APPLE_TEAM_ID", "APPLE_PRIVATE_KEY", "APPLE_TOKENS_SECRET"} <= set(half)
    assert all(p.fatal for p in half.values())

    wrong = _apple_findings(**{**full, "APPLE_PRIVATE_KEY": "not a key", "APPLE_TOKENS_SECRET": "short"})
    assert wrong["APPLE_PRIVATE_KEY"].fatal and wrong["APPLE_TOKENS_SECRET"].fatal

    none = _apple_findings()
    assert set(none) == {"APPLE_KEY_ID"} and not none["APPLE_KEY_ID"].fatal
