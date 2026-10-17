"""A forgotten password, for an email/password app account.

A six-digit code mailed to the address and typed back with a new password
sets it, ends every other session, verifies the address and signs the person
in. Nothing on the way says whether an address has an account: asking always
answers the same, at once, with the work after the response; and codes are
kept per address, so a wrong one counts down and locks in the same steps for
an address with no account behind it."""
import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import event as sa_event, select, text, update

from app.api.auth import password_reset
from app.api.auth.password_reset import PasswordResetCodes
from app.api.auth.sessions import UserSessions
from app.api.events.models import EventRegistrationsLink
from app.api.security.models import SignInAttempts
from app.api.users.models import SigninProviders, UserTypes, Users
from app.core.auth.authentication import get_password_hash
from app.core.email import email as email_mod
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

FORGOT = "/api/v1/auth/password/forgot"
RESET = "/api/v1/auth/password/reset"
SUBJECT = "Your MyOtherApp password reset code"


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


@pytest.fixture(autouse=True)
def background(monkeypatch, session):
    """The work after the response runs on the test's own session."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(password_reset, "session_scope", scope)


@pytest.fixture
def mail(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: sent.append(kwargs))
    return sent


def _codes(mail) -> list[str]:
    return [re.search(r"\b(\d{6})\b", m["text"]).group(1) for m in mail if m["subject"] == SUBJECT]


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


@pytest.fixture
async def account(session, make_user):
    user = await make_user(email="Reset.Me@Example.com", username="resetme", password="OldPassword1!")
    user.provider = SigninProviders.email
    user.full_name = "Asha Rao"
    await session.commit()
    return user


async def _forgot(client, email, ip="203.0.113.10"):
    r = await client.post(FORGOT, json={"email": email}, headers={"X-Forwarded-For": ip})
    assert r.status_code == 200, r.text
    return r


async def _reset(client, email, code, password="NewPassword2!", ip="203.0.113.10", **extra):
    return await client.post(RESET, json={"email": email, "code": code, "new_password": password, **extra}, headers={"X-Forwarded-For": ip})


async def _backdate(session, by: timedelta):
    await session.execute(text("UPDATE password_reset_codes SET sent_at = sent_at - make_interval(secs => :s)"), {"s": by.total_seconds()})
    await session.commit()


# ─── Asking ──────────────────────────────────────────────────────────────


async def test_asking_says_the_same_whoever_asks_and_does_its_work_after_the_answer(client, session, mail, account, make_user, monkeypatch):
    google = await make_user(email="google.person@example.com", username="googler")
    google.provider = SigninProviders.google
    await make_user(email="a.guest@example.com", username=f"guest-{uuid.uuid4().hex[:6]}", user_type=UserTypes.guest)
    await session.commit()

    statements: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        statements.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    scheduled: list[str] = []
    real = password_reset.request_reset

    async def watched(email):
        statements.append("<after the response>")
        scheduled.append(email)
        await real(email)

    monkeypatch.setattr(password_reset, "request_reset", watched)
    answers = []
    try:
        for i, email in enumerate(["reset.me@example.com", "nobody.here@example.com", "google.person@example.com", "a.guest@example.com"]):
            statements.clear()
            r = await _forgot(client, email, ip=f"203.0.113.{20 + i}")
            answers.append((r.status_code, r.json(), r.headers.get("content-length")))
            # Nothing touched the database before the answer was made.
            assert statements[0] == "<after the response>", statements
    finally:
        sa_event.remove(conn, "before_cursor_execute", before)
    assert len({repr(a) for a in answers}) == 1 and answers[0][:2] == (200, {"sent": True})
    assert len(scheduled) == 4

    # Only the password account got a mail; every address got its code row.
    [sent] = [m for m in mail if m["subject"] == SUBJECT]
    assert sent["recipients"] == ["Reset.Me@Example.com"]
    rows = list(await session.scalars(select(PasswordResetCodes)))
    assert len(rows) == 4
    assert sorted(r.user_id is not None for r in rows) == [False, False, False, True]
    # The address is kept only as a keyed hash.
    assert all("@" not in r.email_key and len(r.email_key) == 64 for r in rows)


async def test_the_mail_carries_the_code_in_large_type(client, mail, account):
    await _forgot(client, "  RESET.ME@example.com ")
    [sent] = mail
    [code] = _codes(mail)
    assert sent["subject"] == SUBJECT
    assert "font-size:36px" in sent["html"] and code in sent["html"]
    assert "expires in 15 minutes" in sent["html"] and "expires in 15 minutes" in sent["text"]
    assert "if this wasn't you, ignore this email — your password is unchanged" in sent["text"].lower()
    assert "Hi Asha," in sent["text"]


async def test_codes_go_once_a_minute_and_ten_times_a_day(client, session, mail, account):
    await _forgot(client, "reset.me@example.com")
    # A minute has not passed: the same answer, and nothing more is sent.
    await _forgot(client, "reset.me@example.com", ip="203.0.113.11")
    assert len(_codes(mail)) == 1
    await _backdate(session, timedelta(seconds=61))
    await _forgot(client, "reset.me@example.com", ip="203.0.113.12")
    first, second = _codes(mail)
    # The newer code replaces the older: the older is just wrong now (as with verification).
    if first != second:
        r = await _reset(client, "reset.me@example.com", first)
        assert (r.json()["error_code"], r.json()["errors"]["attempts_left"]) == ("CODE_INVALID", 4)

    # Ten in a day at most.
    for i in range(8):
        await _backdate(session, timedelta(seconds=61))
        await _forgot(client, "reset.me@example.com", ip=f"203.0.113.{40 + i}")
    assert len(_codes(mail)) == 10
    await _backdate(session, timedelta(seconds=61))
    await _forgot(client, "reset.me@example.com", ip="203.0.113.60")
    assert len(_codes(mail)) == 10


async def test_asking_is_limited_per_address_of_the_caller(client, mail, account):
    for i in range(5):
        await _forgot(client, f"someone{i}@example.com", ip="198.51.100.7")
    r = await client.post(FORGOT, json={"email": "reset.me@example.com"}, headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code == 429, r.text
    # Another caller still can.
    await _forgot(client, "reset.me@example.com", ip="198.51.100.8")
    assert len(_codes(mail)) == 1


async def test_google_and_apple_accounts_get_nothing(client, session, mail, make_user):
    for provider in (SigninProviders.google, SigninProviders.apple):
        user = await make_user(email=f"{provider.value}.only@example.com", username=f"{provider.value}only")
        user.provider = provider
    await session.commit()
    for provider in ("google", "apple"):
        await _forgot(client, f"{provider}.only@example.com")
        r = await _reset(client, f"{provider}.only@example.com", "123456")
        assert (r.status_code, r.json()["error_code"]) == (400, "CODE_INVALID")
    assert mail == []


# ─── Using the code ──────────────────────────────────────────────────────


async def test_the_right_code_sets_the_password_ends_other_sessions_and_signs_in(client, session, mail, account, make_user, make_registration, make_club, make_event, mint_token):
    # Signed in elsewhere, with the old password.
    login = await client.post("/api/v1/auth/token", data={"username": "Reset.Me@Example.com", "password": "OldPassword1!"})
    assert login.status_code == 200, login.text
    old_token = login.json()["access_token"]
    # A ticket booked on the website as a guest under the address.
    owner, club = await make_club(name="Reset Club", email="reset-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Reset Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=3))
    guest = await make_user(email="reset.me@example.com", username=f"guest-{uuid.uuid4().hex[:6]}", user_type=UserTypes.guest)
    ticket = await make_registration(event=event, user=guest)
    await session.commit()
    assert account.email_verified_at is None

    await _forgot(client, "reset.me@example.com")
    [code] = _codes(mail)
    r = await _reset(client, " Reset.Me@example.COM ", f"{code[:3]} {code[3:]}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"token_type", "access_token", "refresh_token"} and body["token_type"] == "Bearer"
    assert "access_token" in r.cookies or any("access_token" in c for c in r.headers.get_list("set-cookie"))

    # Signed in with the new tokens; the old session is over.
    me = await client.get("/api/v1/auth/me", headers=auth_header(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["email_verified"] is True
    assert (await client.get("/api/v1/auth/me", headers=auth_header(old_token))).status_code == 401
    sessions = list(await session.scalars(select(UserSessions).where(UserSessions.user_id == account.id).order_by(UserSessions.created_at)))
    assert [s.revoked for s in sessions] == [True, False]

    # The new password works and the old one does not.
    assert (await client.post("/api/v1/auth/token", data={"username": "Reset.Me@Example.com", "password": "NewPassword2!"})).status_code == 200
    assert (await client.post("/api/v1/auth/token", data={"username": "Reset.Me@Example.com", "password": "OldPassword1!"})).status_code == 401

    # The address is verified, and the guest's ticket came into the account.
    await session.refresh(account)
    assert account.email_verified_at is not None
    moved = (await session.execute(select(EventRegistrationsLink.user_id).where(EventRegistrationsLink.id == ticket.id))).scalar()
    assert moved == account.id
    # Recorded on the Security page.
    attempt = await session.scalar(select(SignInAttempts).where(SignInAttempts.user_id == account.id, SignInAttempts.method == "password_reset"))
    assert attempt is not None and attempt.result == "signed_in"

    # The code is spent.
    again = await _reset(client, "reset.me@example.com", code, password="Third3!")
    assert (again.status_code, again.json()["error_code"]) == (400, "CODE_EXPIRED")


async def test_wrong_codes_count_down_and_the_fifth_kills_the_code(client, session, mail, account):
    await _forgot(client, "reset.me@example.com")
    [code] = _codes(mail)
    for left in (4, 3, 2, 1):
        r = await _reset(client, "reset.me@example.com", _wrong(code))
        assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (400, "CODE_INVALID", left)
    r = await _reset(client, "reset.me@example.com", _wrong(code))
    assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (400, "CODE_LOCKED", 0)
    r = await _reset(client, "reset.me@example.com", code)
    assert (r.status_code, r.json()["error_code"]) == (400, "CODE_LOCKED")
    await session.refresh(account)
    from app.core.auth.authentication import verify_password

    assert verify_password("OldPassword1!", account.password)


async def test_an_expired_code_is_refused(client, session, mail, account):
    await _forgot(client, "reset.me@example.com")
    [code] = _codes(mail)
    await session.execute(update(PasswordResetCodes).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await session.commit()
    r = await _reset(client, "reset.me@example.com", code)
    assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (400, "CODE_EXPIRED", 0)


async def test_a_wrong_address_reads_exactly_like_a_wrong_code(client, session, mail, account, monkeypatch):
    """Step for step, an address with no account answers as one with an
    account does to someone who does not have the mailbox."""
    monkeypatch.setattr(password_reset.verification, "new_code", lambda: "424242")

    async def story(email, ip):
        out = [(r.status_code, r.json()) for r in [await _reset(client, email, "999999", ip=ip)]]
        await _forgot(client, email, ip=ip)
        for _ in range(6):
            r = await _reset(client, email, "999999", ip=ip)
            out.append((r.status_code, r.json()))
        return out

    real = await story("reset.me@example.com", "198.51.100.1")
    unknown = await story("nobody.at.all@example.com", "198.51.100.2")
    assert real == unknown
    assert real[0][1]["error_code"] == "CODE_INVALID"
    assert [e["error_code"] for _, e in real[1:]] == ["CODE_INVALID"] * 4 + ["CODE_LOCKED", "CODE_LOCKED"]
    # The real account's code was mailed; the other address's went nowhere.
    assert _codes(mail) == ["424242"]
    # And the right digits for an address with no account are still just wrong.
    await _backdate(session, timedelta(seconds=61))
    await _forgot(client, "nobody.at.all@example.com", ip="203.0.113.99")
    r = await _reset(client, "nobody.at.all@example.com", "424242")
    assert (r.status_code, r.json()["error_code"]) == (400, "CODE_INVALID")


async def test_a_new_password_follows_the_sign_up_rule(client, mail, account):
    await _forgot(client, "reset.me@example.com")
    [code] = _codes(mail)
    r = await _reset(client, "reset.me@example.com", code, password="")
    assert r.status_code == 422, r.text
    # The code was not spent on it.
    assert (await _reset(client, "reset.me@example.com", code)).status_code == 200


async def test_two_factor_still_stands_in_the_way(client, session, mail, account):
    secret = pyotp.random_base32()
    account.totp_secret, account.totp_enabled = secret, True
    await session.commit()
    await _forgot(client, "reset.me@example.com")
    [code] = _codes(mail)

    r = await _reset(client, "reset.me@example.com", code)
    assert (r.status_code, r.json()["error_code"]) == (401, "TOTP_REQUIRED")
    r = await _reset(client, "reset.me@example.com", code, totp_code="000000" if pyotp.TOTP(secret).now() != "000000" else "111111")
    assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (401, "TOTP_INVALID", 4)
    r = await _reset(client, "reset.me@example.com", code, totp_code=pyotp.TOTP(secret).now())
    assert r.status_code == 200, r.text


async def test_a_reset_code_dies_when_google_takes_the_account_back(session, account):
    found, code = await password_reset.issue(session, "reset.me@example.com")
    assert found.id == account.id
    from app.api.auth import verification

    assert await verification.claim_for_provider(session, account, method="google") is True
    await session.commit()
    row = await session.scalar(select(PasswordResetCodes).where(PasswordResetCodes.user_id == account.id))
    assert row.closed_at is not None


async def test_legacy_password_accounts_without_a_provider_take_part(session, make_user):
    old = await make_user(email="old.timer@example.com", username="oldtimer")
    assert old.provider is None and old.password
    no_password = await make_user(email="no.password@example.com", username="nopassword")
    no_password.password = None
    club_user = await make_user(email="club.login@example.com", username="clublogin", user_type=UserTypes.club)
    club_user.password = get_password_hash("x")
    await session.commit()
    assert (await password_reset.find_account(session, " OLD.timer@example.com")).id == old.id
    assert await password_reset.find_account(session, "no.password@example.com") is None
    assert await password_reset.find_account(session, "club.login@example.com") is None
    assert isinstance(old, Users)
