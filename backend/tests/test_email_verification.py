"""Email verification for password accounts, and what it unlocks.

A password account's address is only typed at sign-up. A six-digit code sent
there and typed back verifies it; from then on the account takes in what was
booked under the address as a guest, like a Google or Apple account does.
Google and Apple vouch for an address on their own, and a sign-in through
either takes an unverified password account back from whoever set its
password.
"""
import importlib.util
import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text, update

from app.api.auth import service as auth_service
from app.api.auth import verification
from app.api.auth.verification import EmailVerificationCodes
from app.api.events.models import EventRegistrationsLink
from app.api.security.models import SignInAttempts
from app.api.users import service as users
from app.api.users.models import SigninProviders, UserTypes, Users
from app.config import settings
from app.core.email import email as email_mod
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
SEND = "/api/v1/auth/email/send-code"
VERIFY = "/api/v1/auth/email/verify"
PROFILE = "/api/v1/user/profile/me"


@pytest.fixture(autouse=True)
def _soft_delete_filter(session):
    add_loader_criteria(session)


@pytest.fixture
def mail(monkeypatch):
    """Every mail the request would have sent, as the keyword arguments it was sent with."""
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kwargs: sent.append(kwargs))
    return sent


def _codes(mail) -> list[str]:
    """The codes in the verification mails, oldest first."""
    return [re.search(r"\b(\d{6})\b", m["text"]).group(1) for m in mail if "verification code" in m["subject"]]


async def _register(client, email, *, name="Asha Rao", password="Password123!") -> str:
    r = await client.post("/api/v1/user/register", json={"full_name": name, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _account(session, email) -> Users:
    return await session.scalar(select(Users).where(Users.email == email, Users.user_type == UserTypes.app_user))


async def _row(session, model, row_id) -> dict:
    """A row as it is, retired or not: read past the soft-delete filter."""
    table = model.__table__
    connection = await session.connection()
    return dict((await connection.execute(select(table).where(table.c.id == row_id))).mappings().one())


async def _backdate_sends(session, user_id, by: timedelta):
    await session.execute(
        text("UPDATE email_verification_codes SET sent_at = sent_at - make_interval(secs => :s) WHERE user_id = :u"),
        {"s": by.total_seconds(), "u": user_id},
    )
    await session.commit()


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def _fake_google(monkeypatch, email):
    monkeypatch.setattr(
        auth_service.id_token, "verify_oauth2_token",
        lambda token, request, client_id: {"email": email, "email_verified": True, "name": "Signed In"},
    )


@pytest.fixture
async def event(session, make_club, make_event):
    owner, club = await make_club(name="Verify Society", email=f"verify-club-{uuid.uuid4().hex[:6]}@example.com")
    event = await make_event(club=club, created_by=owner, name="Open Night", event_datetime=NOW + timedelta(days=3))
    await session.commit()
    return event


# ─── Codes ──────────────────────────────────────────────────────────────


async def test_a_code_is_six_digits_and_only_a_keyed_hash_of_it_is_kept(session, make_user):
    user = await make_user(email="Hash@Example.com", username="hash")
    user.provider = SigninProviders.email

    code = await verification.issue(session, user)

    assert re.fullmatch(r"\d{6}", code)
    row = await session.scalar(select(EmailVerificationCodes).where(EmailVerificationCodes.user_id == user.id))
    assert code not in row.code_hash and len(row.code_hash) == 64
    # HMAC under the app secret, over the account and its address (however typed).
    assert row.code_hash == verification.code_hash(user.id, " hash@example.com", code)
    assert verification.code_hash(user.id + 1, user.email, code) != row.code_hash
    assert verification.code_hash(user.id, "other@example.com", code) != row.code_hash
    assert (row.expires_at - row.sent_at, row.attempts, row.closed_at) == (timedelta(minutes=15), 0, None)


async def test_signing_up_sends_a_code_and_the_account_starts_unverified(client, session, mail):
    token = await _register(client, "asha@example.com")

    [sent] = [m for m in mail if "verification code" in m["subject"]]
    [code] = _codes(mail)
    assert sent["recipients"] == ["asha@example.com"]
    assert code in sent["subject"] and code in sent["html"]
    assert "expires in 15 minutes" in sent["text"] and "If this wasn't you, ignore" in sent["text"]
    me = await client.get(PROFILE, headers=auth_header(token))
    assert me.status_code == 200, me.text
    assert me.json()["email_verified"] is False
    assert (await client.get("/api/v1/auth/me", headers=auth_header(token))).json()["email_verified"] is False

    # Nobody is locked out for not verifying: the password still signs in.
    login = await client.post("/api/v1/auth/token", data={"username": "asha@example.com", "password": "Password123!"})
    assert login.status_code == 200, login.text


async def test_the_right_code_verifies_and_brings_in_the_website_ticket(client, session, mail, event):
    # Booked on the website, signed out, before the app account existed.
    r = await client.post(f"/api/v1/events/registration/{event.id}/register", json={"full_name": "Ravi Menon", "email": "Ravi@Example.com"})
    assert r.status_code == 200, r.text
    ticket = uuid.UUID(r.json()["event_registration_id"])

    token = await _register(client, "ravi@example.com", name="Ravi Menon")
    account = await _account(session, "ravi@example.com")
    # Not before the address is shown to be theirs, however often the app asks.
    assert (await client.get(PROFILE, headers=auth_header(token))).status_code == 200
    assert (await _row(session, EventRegistrationsLink, ticket))["user_id"] != account.id

    [code] = _codes(mail)
    r = await client.post(VERIFY, json={"code": f" {code[:3]} {code[3:]} "}, headers=auth_header(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["email_verified"], body["merged"], body["registrations_transferred"]) == (True, True, 1)
    assert body["email_verified_at"]
    assert (await _row(session, EventRegistrationsLink, ticket))["user_id"] == account.id
    assert (await client.get(PROFILE, headers=auth_header(token))).json()["email_verified"] is True
    row = await session.scalar(select(EmailVerificationCodes).where(EmailVerificationCodes.user_id == account.id))
    assert row.closed_at is not None

    # Done: nothing more to send or check.
    for path, payload in ((VERIFY, {"code": code}), (SEND, None)):
        again = await client.post(path, json=payload, headers=auth_header(token))
        assert again.status_code == 409, again.text
        assert again.json()["error_code"] == "ALREADY_VERIFIED"


async def test_wrong_codes_count_down_and_the_fifth_kills_the_code(client, session, mail):
    token = await _register(client, "nisha@example.com")
    [code] = _codes(mail)

    for left in (4, 3, 2, 1):
        r = await client.post(VERIFY, json={"code": _wrong(code)}, headers=auth_header(token))
        assert r.status_code == 400, r.text
        assert (r.json()["error_code"], r.json()["errors"]["attempts_left"]) == ("CODE_INVALID", left)
    r = await client.post(VERIFY, json={"code": _wrong(code)}, headers=auth_header(token))
    assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (400, "CODE_LOCKED", 0)

    # Dead: even the right one is refused now.
    r = await client.post(VERIFY, json={"code": code}, headers=auth_header(token))
    assert (r.status_code, r.json()["error_code"]) == (400, "CODE_LOCKED")

    # A new one works.
    account = await _account(session, "nisha@example.com")
    await _backdate_sends(session, account.id, timedelta(seconds=61))
    assert (await client.post(SEND, headers=auth_header(token))).status_code == 200
    r = await client.post(VERIFY, json={"code": _codes(mail)[-1]}, headers=auth_header(token))
    assert r.status_code == 200, r.text


async def test_an_expired_code_is_refused(client, session, mail):
    token = await _register(client, "omar@example.com")
    [code] = _codes(mail)
    account = await _account(session, "omar@example.com")
    await session.execute(
        update(EmailVerificationCodes)
        .where(EmailVerificationCodes.user_id == account.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await session.commit()

    r = await client.post(VERIFY, json={"code": code}, headers=auth_header(token))
    assert (r.status_code, r.json()["error_code"], r.json()["errors"]["attempts_left"]) == (400, "CODE_EXPIRED", 0)
    assert (await client.get(PROFILE, headers=auth_header(token))).json()["email_verified"] is False


async def test_a_new_code_waits_a_minute_and_replaces_the_old_one(client, session, mail):
    token = await _register(client, "priya@example.com")
    [first] = _codes(mail)

    # The sign-up just sent one.
    r = await client.post(SEND, headers=auth_header(token))
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["error_code"] == "CODE_RECENTLY_SENT"
    assert 1 <= body["errors"]["retry_after"] <= 60
    assert "seconds" in body["message"]

    account = await _account(session, "priya@example.com")
    await _backdate_sends(session, account.id, timedelta(seconds=61))
    r = await client.post(SEND, headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True, "email": "priya@example.com", "expires_in": 900, "retry_after": 60}
    first, second = _codes(mail)
    if first != second:
        r = await client.post(VERIFY, json={"code": first}, headers=auth_header(token))
        assert (r.status_code, r.json()["error_code"]) == (400, "CODE_INVALID")
    r = await client.post(VERIFY, json={"code": second}, headers=auth_header(token))
    assert r.status_code == 200, r.text


async def test_ten_codes_a_day_at_most(client, session, mail, make_user, mint_token):
    user = await make_user(email="daily@example.com", username="daily")
    user.provider = SigninProviders.email
    now = datetime.now(timezone.utc)
    # Ten inside the last day, oldest 23 hours ago; and one older that no longer counts.
    for sent_at in [now - timedelta(hours=23) + timedelta(minutes=10 * i) for i in range(10)] + [now - timedelta(hours=25)]:
        session.add(EmailVerificationCodes(
            user_id=user.id, email=user.email, code_hash="x" * 64, sent_at=sent_at,
            expires_at=sent_at + verification.CODE_TTL, closed_at=sent_at + timedelta(minutes=1),
        ))
    await session.commit()
    token = await mint_token(user)

    r = await client.post(SEND, headers=auth_header(token))
    assert r.status_code == 429, r.text
    assert r.json()["error_code"] == "CODE_DAILY_LIMIT"
    # Free again when the oldest of the ten turns a day old.
    assert 3590 <= r.json()["errors"]["retry_after"] <= 3600
    assert "1 hours" in r.json()["message"] or "60 minutes" in r.json()["message"]

    # Once the oldest has aged out, one more goes, and day-old rows are cleared.
    oldest = await session.scalar(
        select(EmailVerificationCodes).where(EmailVerificationCodes.user_id == user.id, EmailVerificationCodes.sent_at > now - timedelta(hours=24))
        .order_by(EmailVerificationCodes.sent_at).limit(1)
    )
    oldest.sent_at = now - timedelta(hours=24, minutes=1)
    await session.commit()
    r = await client.post(SEND, headers=auth_header(token))
    assert r.status_code == 200, r.text
    left = list(await session.scalars(select(EmailVerificationCodes.sent_at).where(EmailVerificationCodes.user_id == user.id)))
    assert len(left) == 10 and min(left) > now - timedelta(hours=24)


async def test_a_code_verifies_only_the_account_it_was_sent_for(client, session, mail, monkeypatch):
    codes = iter(["111111", "222222"])
    monkeypatch.setattr(verification, "new_code", lambda: next(codes))
    first = await _register(client, "first@example.com")
    second = await _register(client, "second@example.com")

    r = await client.post(VERIFY, json={"code": "111111"}, headers=auth_header(second))
    assert (r.status_code, r.json()["error_code"]) == (400, "CODE_INVALID")
    assert (await client.get(PROFILE, headers=auth_header(second))).json()["email_verified"] is False
    # The other account's tries are its own.
    r = await client.post(VERIFY, json={"code": "111111"}, headers=auth_header(first))
    assert r.status_code == 200, r.text

    for path in (SEND, VERIFY):
        refused = await client.post(path, json={"code": "111111"})
        assert refused.status_code == 401, refused.text


async def test_the_request_limiter_guards_each_account_on_its_own(client, session, mail):
    token = await _register(client, "flood@example.com")
    for _ in range(5):
        r = await client.post(SEND, headers=auth_header(token))
        assert (r.status_code, r.json().get("error_code")) == (429, "CODE_RECENTLY_SENT")
    r = await client.post(SEND, headers=auth_header(token))
    assert r.status_code == 429 and "Rate limit exceeded" in r.text

    # Every caller here has the same address, as they can behind the proxy;
    # the next account still has its own budget.
    other = await _register(client, "calm@example.com")
    r = await client.post(SEND, headers=auth_header(other))
    assert (r.status_code, r.json().get("error_code")) == (429, "CODE_RECENTLY_SENT")


async def test_a_mail_that_cannot_go_out_does_not_fail_the_sign_up(client, session, monkeypatch):
    def down(**kwargs):
        raise RuntimeError("mail provider down")

    monkeypatch.setattr(email_mod, "send_email", down)
    token = await _register(client, "nomail@example.com")
    assert (await client.get(PROFILE, headers=auth_header(token))).status_code == 200


# ─── The merge ──────────────────────────────────────────────────────────


async def test_a_password_account_takes_its_guests_only_once_verified(session, make_user, make_registration, event):
    typed = await make_user(email="typed@example.com", username="typed")
    typed.provider = SigninProviders.email
    guest = await make_user(email="TYPED@example.com", user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:8]}")
    reg = await make_registration(event=event, user=guest)
    await session.flush()

    assert users.merges_into(typed) is False
    assert await users.merge_guest_into_user(session, typed) == {"merged": False, "guests_merged": 0}
    assert (await users.merge_all_guests(session))["app_users_checked"] == 0
    assert (await _row(session, EventRegistrationsLink, reg.id))["user_id"] == guest.id

    typed.email_verified_at = datetime.now(timezone.utc)
    await session.flush()
    assert users.merges_into(typed) is True
    totals = await users.merge_all_guests(session)
    assert (totals["app_users_checked"], totals["app_users_merged"], totals["registrations_transferred"]) == (1, 1, 1)
    assert (await _row(session, EventRegistrationsLink, reg.id))["user_id"] == typed.id


async def test_an_address_an_admin_changes_has_to_be_verified_again(session, make_user):
    user = await make_user(email="old-address@example.com", username="old-address")
    user.provider = SigninProviders.email
    user.email_verified_at = datetime.now(timezone.utc)
    await session.flush()
    assert users.merges_into(user) is True

    await users.admin_update_user(session, user.id, email="new-address@example.com")

    assert (user.email_verified, users.merges_into(user)) == (False, False)


# ─── Google and Apple ───────────────────────────────────────────────────


async def test_google_takes_back_an_unverified_password_account(client, session, mail, monkeypatch, event):
    # Someone registers the address before its owner ever signs in...
    squatter_signup = await _register(client, "victim@example.com", password="Squatted123!")
    login = await client.post("/api/v1/auth/token", data={"username": "victim@example.com", "password": "Squatted123!"})
    assert login.status_code == 200, login.text
    squatter_session = login.json()["access_token"]
    account = await _account(session, "victim@example.com")
    version = account.token_version
    # ...and the owner had booked on the website.
    r = await client.post(f"/api/v1/events/registration/{event.id}/register", json={"full_name": "Victim", "email": "victim@example.com"})
    ticket = uuid.UUID(r.json()["event_registration_id"])

    _fake_google(monkeypatch, "victim@example.com")
    r = await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})
    assert r.status_code == 200, r.text
    owner = r.json()["access_token"]

    claims = jwt.decode(owner, settings.SECRET_KEY, algorithms=["HS256"])
    assert claims["user_id"] == account.id
    await session.refresh(account)
    assert account.password is None and account.email_verified_at is not None
    assert account.token_version == version + 1
    # Every session the password opened is over, and the password with them.
    for token in (squatter_signup, squatter_session):
        assert (await client.get(PROFILE, headers=auth_header(token))).status_code == 401
    again = await client.post("/api/v1/auth/token", data={"username": "victim@example.com", "password": "Squatted123!"})
    assert again.status_code == 401
    me = await client.get(PROFILE, headers=auth_header(owner))
    assert me.status_code == 200 and me.json()["email_verified"] is True
    assert (await _row(session, EventRegistrationsLink, ticket))["user_id"] == account.id
    recorded = await session.scalar(select(SignInAttempts).where(SignInAttempts.user_id == account.id, SignInAttempts.result == "reclaimed"))
    assert recorded is not None and recorded.method == "google"


async def test_apple_takes_back_an_unverified_password_account_too(client, session, mail, monkeypatch):
    await _register(client, "apple-victim@example.com", password="Squatted123!")
    account = await _account(session, "apple-victim@example.com")
    account.totp_enabled, account.totp_secret = True, "JBSWY3DPEHPK3PXP"
    await session.commit()
    version = account.token_version

    class Keys:
        def __init__(self, url):
            pass

        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": "k"})()

    monkeypatch.setattr(jwt, "PyJWKClient", Keys)
    monkeypatch.setattr(jwt, "get_unverified_header", lambda token: {})
    real_decode = jwt.decode
    monkeypatch.setattr(jwt, "decode", lambda *a, **kw: {"sub": "001122.owner", "email": "apple-victim@example.com", "email_verified": "true"})
    try:
        signed_in = await auth_service.apple_signin(session, identity_token="t", authorization_code="c")
    finally:
        monkeypatch.setattr(jwt, "decode", real_decode)

    assert signed_in.id == account.id
    await session.refresh(account)
    assert (account.password, account.totp_enabled, account.totp_secret) == (None, False, None)
    assert account.email_verified_at is not None and account.token_version == version + 1
    recorded = await session.scalar(select(SignInAttempts).where(SignInAttempts.user_id == account.id, SignInAttempts.result == "reclaimed"))
    assert recorded is not None and recorded.method == "apple"


async def test_a_verified_password_account_signs_in_with_google_as_before(client, session, monkeypatch, make_user):
    user = await make_user(email="both@example.com", username="both")
    user.provider = SigninProviders.email
    user.email_verified_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.commit()
    login = await client.post("/api/v1/auth/token", data={"username": "both@example.com", "password": "Password123!"})
    password_session = login.json()["access_token"]
    version = user.token_version

    _fake_google(monkeypatch, "both@example.com")
    r = await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})
    assert r.status_code == 200, r.text

    await session.refresh(user)
    assert user.password is not None and user.token_version == version
    assert (await client.get(PROFILE, headers=auth_header(password_session))).status_code == 200
    again = await client.post("/api/v1/auth/token", data={"username": "both@example.com", "password": "Password123!"})
    assert again.status_code == 200
    assert await session.scalar(select(SignInAttempts).where(SignInAttempts.user_id == user.id, SignInAttempts.result == "reclaimed")) is None


async def test_google_accounts_are_verified_new_or_old(client, session, monkeypatch, make_user, mint_token):
    _fake_google(monkeypatch, "fresh@example.com")
    r = await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})
    assert r.status_code == 200, r.text
    fresh = await _account(session, "fresh@example.com")
    assert fresh.email_verified_at is not None
    assert (await client.get(PROFILE, headers=auth_header(r.json()["access_token"]))).json()["email_verified"] is True

    # Made before accounts carried the stamp: verified all the same, stamped
    # at its next sign-in, and nothing taken from it.
    old = await make_user(email="old-google@example.com", username="old-google")
    old.provider, old.password = SigninProviders.google, None
    await session.commit()
    assert (await client.get(PROFILE, headers=auth_header(await mint_token(old)))).json()["email_verified"] is True
    version = old.token_version
    _fake_google(monkeypatch, "old-google@example.com")
    assert (await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})).status_code == 200
    await session.refresh(old)
    assert old.email_verified_at is not None and old.token_version == version


# ─── The migration ──────────────────────────────────────────────────────


def _migration():
    path = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions" / "add_email_verification.py"
    spec = importlib.util.spec_from_file_location("add_email_verification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_stamps_google_and_apple_app_accounts_only(session):
    """The real upgrade and downgrade, on a users table shaped as production's
    was before it, in a schema of its own inside the test's transaction (the
    suite's schema comes from the models, so it already has the column)."""
    module = _migration()
    connection = await session.connection()

    def run(sync):
        sync.execute(text("CREATE SCHEMA email_verification_migration"))
        sync.execute(text("SET LOCAL search_path TO email_verification_migration"))
        sync.execute(text("CREATE TYPE usertypes AS ENUM ('app_user', 'guest', 'club', 'admin')"))
        sync.execute(text("CREATE TYPE signinproviders AS ENUM ('google', 'email', 'apple')"))
        sync.execute(text("CREATE TABLE users (id serial PRIMARY KEY, email varchar(100) NOT NULL, user_type usertypes NOT NULL, provider signinproviders)"))
        sync.execute(text(
            "INSERT INTO users (id, email, user_type, provider) VALUES "
            "(1, 'g@example.com', 'app_user', 'google'), (2, 'a@example.com', 'app_user', 'apple'), "
            "(3, 'p@example.com', 'app_user', 'email'), (4, 'n@example.com', 'app_user', NULL), "
            "(5, 'guest@example.com', 'guest', 'google'), (6, 'club@example.com', 'club', 'google')"
        ))
        context = MigrationContext.configure(connection=sync)
        with Operations.context(context):
            module.upgrade()
            stamped = dict(sync.execute(text("SELECT id, email_verified_at IS NOT NULL FROM users")).all())
            columns = {r[0] for r in sync.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'email_verification_migration' AND table_name = 'email_verification_codes'"
            ))}
            module.downgrade()
            gone = sync.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = 'email_verification_migration' AND column_name = 'email_verified_at'"
            )).scalar()
            module.upgrade()
        sync.execute(text("SET LOCAL search_path TO public"))
        return stamped, columns, gone

    stamped, columns, gone = await connection.run_sync(run)
    assert stamped == {1: True, 2: True, 3: False, 4: False, 5: False, 6: False}
    assert columns == {"id", "user_id", "email", "code_hash", "sent_at", "expires_at", "attempts", "closed_at"}
    assert gone == 0
