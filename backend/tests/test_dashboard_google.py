"""Sign in with Google on the club dashboard (scope "club") and the main
portal (scope "admin"): POST /auth/google/dashboard.

Google only signs in accounts that already exist, never an app user or a
guest, and every miss is the same 401 GOOGLE_NO_ACCOUNT. Past the token,
the door is /auth/token's: the same order, refusals, lockout, two-factor,
record, new-device mail and cookies.

Google is faked at the one place it is reached, the certificate fetch:
the tokens here are real RS256 JWTs signed with a key made for the suite,
so google-auth's own checks (signature, audience, issuer, expiry) run on
them, with no network."""
import json
import time

import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as google_jwt
from sqlalchemy import func, select

from app.api.auth import service as auth_service
from app.api.club_access.models import ClubMembers
from app.api.security import service as security
from app.api.security.models import SignInAttempts
from app.api.users.models import Users, UserTypes
from app.config import settings
from app.core.auth.jwt import decode_jwt_token
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.conftest import auth_header

URL = "/api/v1/auth/google/dashboard"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
KID = "suite-key"
NO_ACCOUNT = {
    "message": "No club dashboard / admin account uses this Google account. Sign in with your email and password, or ask to be invited.",
    "error_code": "GOOGLE_NO_ACCOUNT",
}

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
_PUBLIC_PEM = _KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
_SIGNER = crypt.RSASigner.from_string(_PRIVATE_PEM, key_id=KID)


def google_token(email, *, verified=True, aud=None, iss="https://accounts.google.com", expires_in=3600, signer=None, **extra) -> str:
    """An ID token as Google's sign-in button hands it over."""
    now = int(time.time())
    claims = {
        "iss": iss, "aud": aud or settings.GOOGLE_WEB_CLIENT_ID, "sub": f"g-{email}", "email": email,
        "email_verified": verified, "name": "Google Person", "iat": now - 5, "exp": now + expires_in, **extra,
    }
    return google_jwt.encode(signer or _SIGNER, claims).decode()


class _Certs:
    """Google's certificate endpoint, as service._google_certs reaches it."""

    def reset(self):
        pass

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        return auth_service._GoogleCerts._Response(200, json.dumps({KID: _PUBLIC_PEM}).encode())


@pytest.fixture
async def world(client, session, make_club, make_user, make_admin, monkeypatch):
    monkeypatch.setattr(auth_service, "_google_certs", _Certs())
    monkeypatch.setattr(limiter, "enabled", False)  # the rate-limit test turns it back on
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    owner, club = await make_club(name="Circuit Society", email="circuit@example.com")
    member = ClubMembers(club_id=club.id, email="priya@example.com", full_name="Priya", role="editor", status="active")
    session.add(member)
    admin = await make_admin(email="ops@example.com")
    await make_user(email="fan@example.com")  # an ordinary app user
    await session.commit()
    return dict(owner=owner, club=club, member=member, admin=admin, sent=sent)


async def sign_in(client, email, scope, *, ip="103.21.58.14", **body):
    token = body.pop("id_token", None) or google_token(email)
    return await client.post(URL, json={"id_token": token, "scope": scope, **body}, headers={"x-forwarded-for": ip, "user-agent": CHROME})


def said(body: dict) -> dict:
    return {k: body.get(k) for k in NO_ACCOUNT}


async def attempts(session, **where):
    q = select(SignInAttempts).order_by(SignInAttempts.created_at)
    for k, v in where.items():
        q = q.where(getattr(SignInAttempts, k) == v)
    return list((await session.scalars(q)).all())


# ─── Club dashboard ────────────────────────────────────────────────────────


async def test_a_club_team_member_opens_the_clubs_dashboard_as_themselves(client, session, world):
    r = await sign_in(client, "Priya@Example.com", "club")
    assert r.status_code == 200, r.text
    body = r.json()
    claims = decode_jwt_token(body["access_token"])
    # A session on the club's own account, with the member behind it.
    assert claims["user_id"] == world["owner"].id and claims["member_id"] == world["member"].id and claims["sid"]
    assert decode_jwt_token(body["refresh_token"])["member_id"] == world["member"].id
    await session.refresh(world["member"])
    assert world["member"].last_seen_at is not None
    me = await client.get("/api/v1/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200 and me.json()["id"] == world["owner"].id
    # Recorded as a Google sign-in, under the member's address.
    [row] = await attempts(session, result="signed_in")
    assert (row.method, row.identifier, row.user_id, str(row.session_id)) == ("google", "priya@example.com", world["owner"].id, claims["sid"])
    # A first sign-in from this device: the member is told, naming the club.
    assert len(world["sent"]) == 1 and world["sent"][0]["recipients"] == ["priya@example.com"] and "Circuit Society" in world["sent"][0]["subject"]


async def test_the_clubs_own_account_signs_in_with_google(client, world):
    r = await sign_in(client, "circuit@example.com", "club")
    assert r.status_code == 200, r.text
    claims = decode_jwt_token(r.json()["access_token"])
    assert claims["user_id"] == world["owner"].id and claims["member_id"] is None


async def test_a_member_comes_before_the_club_account_with_the_same_address(client, session, world, make_club):
    other_owner, other = await make_club(name="Robotics Guild", email="robotics@example.com")
    session.add(ClubMembers(club_id=world["club"].id, email="robotics@example.com", role="door", status="active"))
    await session.commit()
    claims = decode_jwt_token((await sign_in(client, "robotics@example.com", "club")).json()["access_token"])
    assert claims["user_id"] == world["owner"].id and claims["member_id"] is not None


async def test_an_invited_or_removed_member_is_no_one(client, session, world):
    session.add(ClubMembers(club_id=world["club"].id, email="later@example.com", role="editor", status="invited"))
    session.add(ClubMembers(club_id=world["club"].id, email="gone@example.com", role="editor", status="removed"))
    await session.commit()
    for email in ("later@example.com", "gone@example.com"):
        r = await sign_in(client, email, "club")
        assert r.status_code == 401 and said(r.json()) == NO_ACCOUNT


async def test_an_unapproved_club_is_told_so(client, session, world, make_club):
    await make_club(name="Waiting Room", email="waiting@example.com", is_verified=False)
    await session.commit()
    r = await sign_in(client, "waiting@example.com", "club")
    assert r.status_code == 403 and r.json()["error_code"] == "CLUB_NOT_VERIFIED"
    [row] = await attempts(session, identifier="waiting@example.com")
    assert (row.method, row.result) == ("google", "blocked")


async def test_a_member_of_a_club_whose_owner_is_suspended_is_turned_away(client, session, world):
    world["owner"].is_active = False
    await session.commit()
    r = await sign_in(client, "priya@example.com", "club")
    assert r.status_code == 403 and r.json()["error_code"] == "CLUB_UNAVAILABLE"
    [row] = await attempts(session, identifier="priya@example.com")
    assert (row.method, row.result, row.user_id) == ("google", "blocked", None)


async def test_a_suspended_club_account_is_suspended(client, session, world):
    world["owner"].is_active = False
    await session.commit()
    r = await sign_in(client, "circuit@example.com", "club")
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_SUSPENDED"


# ─── Main portal ───────────────────────────────────────────────────────────


async def test_an_admin_without_two_factor_signs_in(client, session, world):
    r = await sign_in(client, "ops@example.com", "admin")
    assert r.status_code == 200, r.text
    claims = decode_jwt_token(r.json()["access_token"])
    assert claims["user_id"] == world["admin"].id and claims["member_id"] is None
    [row] = await attempts(session, result="signed_in")
    assert (row.method, row.user_id) == ("google", world["admin"].id)


async def test_an_admin_with_two_factor_is_asked_for_the_code(client, session, world, mint_token):
    h = auth_header(await mint_token(world["admin"]))
    secret = (await client.post("/api/v1/auth/2fa/setup", headers=h)).json()["secret"]
    assert (await client.post("/api/v1/auth/2fa/enable", headers=h, json={"code": pyotp.TOTP(secret).now()})).status_code == 200
    token = google_token("ops@example.com")

    asked = await sign_in(client, "ops@example.com", "admin", id_token=token)
    assert asked.status_code == 401 and asked.json()["error_code"] == "TOTP_REQUIRED"
    # The same Google token again, with the code.
    ok = await sign_in(client, "ops@example.com", "admin", id_token=token, totp_code=pyotp.TOTP(secret).at(time.time() + 30))
    assert ok.status_code == 200, ok.text
    assert decode_jwt_token(ok.json()["access_token"])["user_id"] == world["admin"].id
    wrong = await sign_in(client, "ops@example.com", "admin", id_token=token, totp_code="000000")
    assert wrong.status_code == 401 and wrong.json()["error_code"] == "TOTP_INVALID"
    assert [(a.method, a.result) for a in await attempts(session, identifier="ops@example.com")] == [
        ("google", "totp_required"), ("google", "signed_in"), ("google", "totp_invalid"),
    ]


async def test_a_club_account_with_two_factor_is_asked_for_the_code_too(client, session, world):
    """As on /auth/token: Google never steps around a second factor."""
    world["owner"].totp_enabled = True
    world["owner"].totp_secret = pyotp.random_base32()
    await session.commit()
    r = await sign_in(client, "circuit@example.com", "club")
    assert r.status_code == 401 and r.json()["error_code"] == "TOTP_REQUIRED"


async def test_a_suspended_admin_is_suspended(client, session, world):
    world["admin"].is_active = False
    await session.commit()
    r = await sign_in(client, "ops@example.com", "admin")
    assert r.status_code == 403 and r.json()["error_code"] == "ACCOUNT_SUSPENDED"


# ─── Nobody else ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["club", "admin"])
async def test_an_app_user_a_guest_or_a_stranger_gets_one_plain_answer(client, session, world, make_user, scope):
    await make_user(email="walkin@example.com", user_type=UserTypes.guest, password="x")
    await session.commit()
    before = await session.scalar(select(func.count()).select_from(Users))
    answers = []
    for email in ("fan@example.com", "walkin@example.com", "nobody@example.com"):
        r = await sign_in(client, email, scope)
        assert r.status_code == 401, r.text
        answers.append(r.json())
    # The same words for each: the endpoint does not say which addresses exist.
    assert answers[0] == answers[1] == answers[2] and said(answers[0]) == NO_ACCOUNT
    # Nobody was made, and nobody was signed in.
    assert await session.scalar(select(func.count()).select_from(Users)) == before
    assert not await attempts(session, result="signed_in")
    misses = await attempts(session, method="google")
    assert [(a.identifier, a.result, a.user_id) for a in misses] == [
        ("fan@example.com", "no_account", None), ("walkin@example.com", "no_account", None), ("nobody@example.com", "no_account", None),
    ]


async def test_each_door_opens_only_its_own_accounts(client, world):
    assert (await sign_in(client, "ops@example.com", "club")).json()["error_code"] == "GOOGLE_NO_ACCOUNT"
    assert (await sign_in(client, "circuit@example.com", "admin")).json()["error_code"] == "GOOGLE_NO_ACCOUNT"
    assert (await sign_in(client, "priya@example.com", "admin")).json()["error_code"] == "GOOGLE_NO_ACCOUNT"


# ─── The token ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [
    pytest.param(lambda: google_token("ops@example.com", verified=False), id="email-not-verified"),
    pytest.param(lambda: google_token("ops@example.com", aud=settings.GOOGLE_ANDROID_CLIENT_ID), id="wrong-audience"),
    pytest.param(lambda: google_token("ops@example.com", aud="someone-elses-app.apps.googleusercontent.com"), id="another-apps-token"),
    pytest.param(lambda: google_token("ops@example.com", iss="https://evil.example.com"), id="wrong-issuer"),
    pytest.param(lambda: google_token("ops@example.com", expires_in=-600), id="expired"),
    pytest.param(lambda: google_token("ops@example.com", signer=crypt.RSASigner.from_string(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id=KID)), id="not-signed-by-google"),
    pytest.param(lambda: "not-a-token", id="garbage"),
])
async def test_a_token_google_would_not_vouch_for_is_refused(client, session, world, token):
    r = await sign_in(client, "ops@example.com", "admin", id_token=token())
    assert r.status_code == 401 and r.json()["error_code"] == "INVALID_GOOGLE_TOKEN", r.text
    assert not await attempts(session, result="signed_in")


async def test_a_bad_body_is_a_422(client, world):
    assert (await client.post(URL, json={"id_token": google_token("ops@example.com"), "scope": "app"})).status_code == 422
    assert (await client.post(URL, json={"scope": "admin"})).status_code == 422
    assert (await client.post(URL, json={"id_token": "", "scope": "admin"})).status_code == 422


async def test_google_unreachable_is_a_503(client, world, monkeypatch):
    def down(url, method="GET", **kwargs):
        return auth_service._GoogleCerts._Response(500, b"")

    down.reset = lambda: None
    monkeypatch.setattr(auth_service, "_google_certs", down)
    r = await sign_in(client, "ops@example.com", "admin")
    assert r.status_code == 503 and r.json()["error_code"] == "GOOGLE_UNAVAILABLE"


async def test_the_apps_google_sign_in_refuses_a_wrong_issuer_with_a_401_not_a_500(client, world):
    """The check is shared: /auth/google used to answer a token from another
    issuer with a 500."""
    r = await client.post("/api/v1/auth/google", json={"id_token": google_token("fan@example.com", iss="https://evil.example.com"), "platform": "web"})
    assert r.status_code == 401 and r.json()["error_code"] == "INVALID_GOOGLE_TOKEN"
    ok = await client.post("/api/v1/auth/google", json={"id_token": google_token("fan@example.com"), "platform": "web"})
    assert ok.status_code == 200, ok.text


# ─── Same as /auth/token ───────────────────────────────────────────────────


def _cookies(response) -> dict[str, str]:
    """Each Set-Cookie with its value taken out, attributes lower-cased."""
    out = {}
    for line in response.headers.get_list("set-cookie"):
        name, rest = line.split("=", 1)
        out[name] = "; ".join(sorted(p.strip().lower() for p in rest.split(";")[1:]))
    return out


async def test_the_cookies_are_the_ones_the_password_sign_in_sets(client, session, world):
    world["owner"].password = __import__("app.core.auth.authentication", fromlist=["get_password_hash"]).get_password_hash("Password123!")
    await session.commit()
    password = await client.post("/api/v1/auth/token", data={"username": "circuit@example.com", "password": "Password123!", "scope": "club"})
    google = await sign_in(client, "circuit@example.com", "club")
    assert password.status_code == google.status_code == 200
    assert set(_cookies(google)) == {"access_token", "refresh_token"}
    assert _cookies(google) == _cookies(password)
    assert google.cookies.get("access_token") == google.json()["access_token"]
    assert google.cookies.get("refresh_token") == google.json()["refresh_token"]
    assert "httponly" in _cookies(google)["access_token"] and "samesite=lax" in _cookies(google)["access_token"]


async def test_the_new_device_mail_goes_once_and_names_the_account(client, world):
    assert (await sign_in(client, "ops@example.com", "admin")).status_code == 200
    assert len(world["sent"]) == 1 and world["sent"][0]["recipients"] == ["ops@example.com"]
    assert (await sign_in(client, "ops@example.com", "admin")).status_code == 200
    assert len(world["sent"]) == 1


async def test_an_account_locked_by_wrong_passwords_is_locked_for_google_too(client, world):
    for _ in range(5):
        r = await client.post("/api/v1/auth/token", data={"username": "ops@example.com", "password": "nope"}, headers={"x-forwarded-for": "185.220.101.4"})
        assert r.status_code == 401
    r = await sign_in(client, "ops@example.com", "admin", ip="185.220.101.4")
    assert r.status_code == 429 and r.json()["error_code"] == "LOGIN_LOCKED"
    # Another address is not locked out by that one's guessing.
    assert (await sign_in(client, "ops@example.com", "admin", ip="49.36.112.7")).status_code == 200


async def test_wrong_codes_through_google_lock_the_door(client, session, world):
    world["admin"].totp_enabled = True
    world["admin"].totp_secret = pyotp.random_base32()
    await session.commit()
    token = google_token("ops@example.com")
    for _ in range(5):
        r = await sign_in(client, "ops@example.com", "admin", id_token=token, totp_code="000000", ip="185.220.101.4")
        assert r.json()["error_code"] == "TOTP_INVALID"
    r = await sign_in(client, "ops@example.com", "admin", id_token=token, totp_code="000000", ip="185.220.101.4")
    assert r.status_code == 429 and r.json()["error_code"] == "LOGIN_LOCKED"


async def test_the_rate_limit_is_the_password_sign_ins(client, world, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    token = google_token("nobody@example.com")
    codes = [(await client.post(URL, json={"id_token": token, "scope": "club"})).status_code for _ in range(21)]
    assert codes[:20] == [401] * 20 and codes[20] == 429


async def test_signed_in_is_a_working_session(client, world):
    """The refresh token renews like the password sign-in's, member and all."""
    r = await sign_in(client, "priya@example.com", "club")
    renewed = await client.post("/api/v1/auth/refresh", data={"token": r.json()["refresh_token"]})
    assert renewed.status_code == 200, renewed.text
    assert decode_jwt_token(renewed.json()["access_token"])["member_id"] == world["member"].id

