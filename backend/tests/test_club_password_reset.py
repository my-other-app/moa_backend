"""A forgotten password on the clubs dashboard: the club's own account and a
team member's. A six-digit code by email, then a new password; nothing says
whether an address has a login; limited per caller and per address; every
session of that login ends."""
import asyncio
import re
import uuid
from contextlib import asynccontextmanager

import pyotp
import pytest
from sqlalchemy import select

from app.api.auth import password_reset as app_reset
from app.api.club_access.models import ClubMembers
from app.api.club_ops import password as club_reset
from app.api.club_ops.models import ClubPasswordResetCodes
from app.api.security import service as security
from app.core.auth.authentication import get_password_hash
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

FORGOT = "/api/v1/auth/club/password/forgot"
RESET = "/api/v1/auth/club/password/reset"
SUBJECT = "Your MyOtherApp club dashboard password reset code"


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture(autouse=True)
def background(monkeypatch, session):
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(club_reset, "session_scope", scope)
    monkeypatch.setattr(app_reset, "session_scope", scope)

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)


@pytest.fixture
def mail(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: sent.append(kwargs))
    return sent


def _codes(mail, to=None) -> list[str]:
    return [re.search(r"\b(\d{6})\b", m["text"]).group(1) for m in mail
            if m["subject"] == SUBJECT and (to is None or m["recipients"] == [to])]


@pytest.fixture
async def club(session, make_club):
    owner, club = await make_club(name="Reset Club", email="Owner.Reset@Example.com")
    member = ClubMembers(club_id=club.id, email="crew.reset@example.com", full_name="Crew Member", role="editor",
                         status="active", password=get_password_hash("Old-member-pass1"))
    session.add(member)
    await session.commit()
    return owner, club, member


async def _forgot(client, email, ip="203.0.113.20"):
    r = await client.post(FORGOT, json={"email": email}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True}
    return r


async def _reset(client, email, code, password="Brand-new-pass9", ip="203.0.113.20", **extra):
    return await client.post(RESET, json={"email": email, "code": code, "new_password": password, **extra}, headers={"X-Forwarded-For": ip})


async def _sign_in(client, email, password):
    return await client.post("/api/v1/auth/token", data={"username": email, "password": password, "scope": "club"})


async def test_asking_answers_the_same_for_a_club_a_member_and_nobody(client, mail, club, make_user):
    await make_user(email="just.an.app.user@example.com")
    for address in ("owner.reset@example.com", "crew.reset@example.com", "nobody@example.com", "just.an.app.user@example.com"):
        await _forgot(client, address)
    assert len(_codes(mail, "Owner.Reset@Example.com")) == 1
    assert len(_codes(mail, "crew.reset@example.com")) == 1
    # No code goes to an address with no club login (an app account is not one).
    assert len(_codes(mail)) == 2


async def test_the_club_account_resets_its_password_and_every_session_ends(client, session, mail, club, mint_token):
    owner, _, _ = club
    old = auth_header(await mint_token(owner))
    assert (await client.get("/api/v1/clubs/access/me", headers=old)).status_code == 200
    await _forgot(client, "owner.reset@example.com")
    [code] = _codes(mail)
    r = await _reset(client, "OWNER.reset@example.com ", code)
    assert r.status_code == 200, r.text and r.json() == {"reset": True}
    assert (await client.get("/api/v1/clubs/access/me", headers=old)).status_code == 401
    assert (await _sign_in(client, "Owner.Reset@Example.com", "Password123!")).status_code == 401
    signed = await _sign_in(client, "Owner.Reset@Example.com", "Brand-new-pass9")
    assert signed.status_code == 200, signed.text
    # The code is spent.
    again = await _reset(client, "owner.reset@example.com", code)
    assert again.status_code == 400 and again.json()["error_code"] == "CODE_EXPIRED"


async def test_a_team_member_resets_theirs_and_their_sessions_end(client, session, mail, club, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    member_session = await _sign_in(client, "crew.reset@example.com", "Old-member-pass1")
    assert member_session.status_code == 200
    h = auth_header(member_session.json()["access_token"])
    await _forgot(client, "crew.reset@example.com")
    [code] = _codes(mail)
    r = await _reset(client, "crew.reset@example.com", code, password="Crew-new-pass42")
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/clubs/access/me", headers=h)).status_code == 401
    me = await _sign_in(client, "crew.reset@example.com", "Crew-new-pass42")
    assert me.status_code == 200
    who = (await client.get("/api/v1/clubs/access/me", headers=auth_header(me.json()["access_token"]))).json()
    assert who["role"] == "editor" and who["member"]["email"] == "crew.reset@example.com"


async def test_wrong_codes_count_down_and_read_the_same_with_or_without_a_login(client, mail, club):
    await _forgot(client, "owner.reset@example.com")
    await _forgot(client, "ghost@example.com")
    [code] = _codes(mail)
    wrong = "000000" if code != "000000" else "111111"
    a = await _reset(client, "owner.reset@example.com", wrong)
    b = await _reset(client, "ghost@example.com", wrong)
    assert (a.status_code, a.json()["error_code"], a.json()["errors"]["attempts_left"]) == (400, "CODE_INVALID", 4)
    assert (b.status_code, b.json()["error_code"], b.json()["errors"]["attempts_left"]) == (400, "CODE_INVALID", 4)
    for _ in range(3):
        await _reset(client, "owner.reset@example.com", wrong)
    locked = await _reset(client, "owner.reset@example.com", wrong)
    assert locked.json()["error_code"] == "CODE_LOCKED"
    assert (await _reset(client, "owner.reset@example.com", code)).json()["error_code"] == "CODE_LOCKED"


async def test_a_short_password_is_refused_without_spending_the_code(client, mail, club):
    await _forgot(client, "owner.reset@example.com")
    [code] = _codes(mail)
    weak = await _reset(client, "owner.reset@example.com", code, password="short")
    assert weak.status_code == 422 and weak.json()["error_code"] == "WEAK_PASSWORD"
    assert (await _reset(client, "owner.reset@example.com", code)).status_code == 200


async def test_two_factor_on_the_club_account_still_stands_in_the_way(client, session, mail, club):
    owner, _, _ = club
    secret = pyotp.random_base32()
    owner.totp_secret, owner.totp_enabled = secret, True
    await session.commit()
    await _forgot(client, "owner.reset@example.com")
    [code] = _codes(mail)
    r = await _reset(client, "owner.reset@example.com", code)
    assert (r.status_code, r.json()["error_code"]) == (401, "TOTP_REQUIRED")
    r = await _reset(client, "owner.reset@example.com", code, totp_code=pyotp.TOTP(secret).now())
    assert r.status_code == 200, r.text


async def test_one_code_a_minute_per_address_and_the_caller_is_limited(client, mail, club, session):
    await _forgot(client, "owner.reset@example.com")
    await _forgot(client, "owner.reset@example.com")
    assert len(_codes(mail)) == 1
    rows = (await session.scalars(select(ClubPasswordResetCodes))).all()
    assert len(rows) == 1 and rows[0].user_id is not None and rows[0].member_id is None
    statuses = [(await client.post(FORGOT, json={"email": f"x{i}@example.com"}, headers={"X-Forwarded-For": "198.51.100.7"})).status_code
                for i in range(6)]
    assert statuses[:5] == [200] * 5 and statuses[5] == 429


async def test_the_apps_reset_codes_and_the_dashboards_are_kept_apart(client, mail, club, session, make_user):
    # An app account at the member's address asks the app for a code; the dashboard does not take it.
    app_user = await make_user(email="crew.reset@example.com", username="crewapp", password="AppPass1!")
    from app.api.users.models import SigninProviders

    app_user.provider = SigninProviders.email
    await session.commit()
    r = await client.post("/api/v1/auth/password/forgot", json={"email": "crew.reset@example.com"}, headers={"X-Forwarded-For": "203.0.113.99"})
    assert r.status_code == 200
    [app_code] = [re.search(r"\b(\d{6})\b", m["text"]).group(1) for m in mail if m["subject"] != SUBJECT]
    refused = await _reset(client, "crew.reset@example.com", app_code)
    assert refused.status_code == 400 and refused.json()["error_code"] == "CODE_INVALID"
    member = await session.scalar(select(ClubMembers).where(ClubMembers.email == "crew.reset@example.com"))
    assert member.password and (await _sign_in(client, "crew.reset@example.com", "Old-member-pass1")).status_code == 200
    assert uuid.UUID  # keep the import honest
