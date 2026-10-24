"""Accounts, sign-in, push and admin at scale (fix/accounts-integrity).

One test (or a few) per finding of the audit, each failing on the code before
the fix: pushes past 500 devices, the fan-out's round trips, the inbox's
indexes, a deleted account signing up again, bcrypt and the identity
providers off the event loop, single-use second factors, the races, refresh
rotation, impersonation, rate limits, the soft-delete filter and partial
indexes, the closeout lock, the error handlers, the admin list and the
housekeeping sweep.
"""
import asyncio
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pyotp
import pytest
from firebase_admin import messaging
from sqlalchemy import event, func, insert, select, text, update

from app.api.auth.sessions import UserSessions
from app.api.clubs.models import ClubUsersLink
from app.api.notifications.models import Notifications
from app.api.security.models import SignInAttempts
from app.api.users.models import AdminRoles, UserDeviceTokens, UserTypes, Users
from app.config import settings
from app.core import jobs
from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)


async def _bulk_users(session, n: int, prefix: str, *, tokens: bool = False) -> list[int]:
    """n app accounts (and a device each), in two statements: make_user
    hashes a password per account, which is a quarter of a second each."""
    now = datetime.now(timezone.utc)
    rows = [
        dict(full_name=f"{prefix} {i}", username=f"{prefix}-{i}", email=f"{prefix}-{i}@example.com",
             user_type=UserTypes.app_user, token_version=0, is_active=True, totp_enabled=False,
             notify_new_device=True, admin_role=AdminRoles.owner, is_deleted=False, created_at=now, updated_at=now)
        for i in range(n)
    ]
    ids = list((await session.execute(insert(Users).returning(Users.id), rows)).scalars().all())
    if tokens:
        await session.execute(
            insert(UserDeviceTokens),
            [dict(user_id=uid, fcm_token=f"{prefix}-tok-{uid}", platform="android", is_deleted=False, created_at=now, updated_at=now) for uid in ids],
        )
    await session.flush()
    return ids


class _FakeFcm:
    """send_each_for_multicast as firebase-admin 6.5 behaves: a ValueError
    past 500 tokens, before anything is sent."""

    def __init__(self, dead: set[str] = frozenset()):
        self.batches: list[int] = []
        self.threads: set[int] = set()
        self.dead = set(dead)

    def __call__(self, message):
        self.threads.add(threading.get_ident())
        if len(message.tokens) > 500:
            raise ValueError("messages must not contain more than 500 elements.")
        self.batches.append(len(message.tokens))
        outcomes = [messaging.UnregisteredError("gone") if t in self.dead else None for t in message.tokens]
        return SimpleNamespace(
            success_count=sum(1 for o in outcomes if o is None),
            failure_count=sum(1 for o in outcomes if o is not None),
            responses=[SimpleNamespace(success=o is None, exception=o) for o in outcomes],
        )


@pytest.fixture
def fcm(monkeypatch):
    fake = _FakeFcm()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake)
    return fake


# ─── 1. Push past 500 devices ──────────────────────────────────────────────


async def test_a_push_reaches_more_than_500_devices_in_batches_off_the_loop(session, fcm):
    ids = await _bulk_users(session, 1200, "fanpush", tokens=True)
    loop_thread = threading.get_ident()

    sent = await push.send_notification_to_users(session, ids, "Hello", "World")

    assert sent == 1200
    assert sorted(fcm.batches) == [200, 500, 500]
    # The SDK blocks until FCM answers: never on the event loop's thread.
    assert fcm.threads and loop_thread not in fcm.threads


async def test_tokens_are_looked_up_past_postgres_bind_limit(session):
    ids = await _bulk_users(session, 3, "manyids", tokens=True)
    # One bind per id failed past 32,767 of them.
    lookup = list(range(10_000_000, 10_040_000)) + ids
    tokens = await push.get_fcm_tokens_for_users(session, lookup)
    assert sorted(tokens) == sorted(f"manyids-tok-{i}" for i in ids)


async def test_dead_tokens_are_pruned_in_one_statement_however_many(session):
    ids = await _bulk_users(session, 2, "prunemany", tokens=True)
    dead = [f"prunemany-tok-{i}" for i in ids] + [f"never-{n}" for n in range(40_000)]
    assert await push.prune_tokens(session, dead) == 2
    assert await push.get_fcm_tokens_for_users(session, ids) == []


async def test_nothing_delivered_is_logged_as_an_error(session, monkeypatch, caplog):
    ids = await _bulk_users(session, 3, "silent", tokens=True)
    fake = _FakeFcm(dead={f"silent-tok-{i}" for i in ids})
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake)
    with caplog.at_level("ERROR"):
        assert await push.send_notification_to_users(session, ids, "Hi", "There") == 0
    assert any("PUSH FAILED" in r.getMessage() for r in caplog.records)


async def test_a_single_push_goes_to_every_device_of_the_person(session, fcm):
    (uid,) = await _bulk_users(session, 1, "twodevices", tokens=True)
    session.add(UserDeviceTokens(user_id=uid, fcm_token="twodevices-second-phone", platform="android"))
    await session.flush()
    assert await push.send_notification_to_user(session, uid, "Hi", "Both") is True
    assert fcm.batches == [2]


# ─── 2. The fan-out ────────────────────────────────────────────────────────


def _count_inserts(session, table: str):
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(f"INSERT INTO {table.upper()}"):
            seen.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine, "before_cursor_execute", before)


async def test_followers_are_told_in_one_insert(session, make_club, make_event, fcm):
    from app.core.notifications.triggers import notify_followers_of_new_event

    owner, club = await make_club(name="Fan Out Society", email="fanout@example.com")
    ev = await make_event(club=club, created_by=owner, name="Hack Night")
    ids = await _bulk_users(session, 300, "follower", tokens=True)
    now = datetime.now(timezone.utc)
    await session.execute(
        insert(ClubUsersLink),
        [dict(club_id=club.id, user_id=uid, is_following=True, is_pinned=False, is_hidden=False, is_deleted=False, created_at=now, updated_at=now) for uid in ids],
    )
    await session.flush()

    seen, stop = _count_inserts(session, "notifications")
    try:
        # A job after the commit (app.core.jobs); in the suite it runs when waited for.
        assert await notify_followers_of_new_event(session, club.id, club.name, ev.id, "Hack Night") is None
        assert fcm.batches == []
        await jobs.settle()
    finally:
        stop()
    assert len(seen) == 1, seen
    assert fcm.batches == [300]
    rows = await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.from_club_id == club.id, Notifications.type == "new_event"))
    assert rows == 300
    # Once per event: told again (a retry, a second call), nobody gets a second row.
    await notify_followers_of_new_event(session, club.id, club.name, ev.id, "Hack Night")
    await jobs.settle()
    rows = await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.from_club_id == club.id, Notifications.type == "new_event"))
    assert rows == 300


async def test_a_list_of_recipients_is_one_insert_too(session):
    from app.api.notifications.service import create_notifications_batch

    ids = await _bulk_users(session, 250, "listed")
    seen, stop = _count_inserts(session, "notifications")
    try:
        assert await create_notifications_batch(session, ids, "T", "D", "club_thread", data={"a": 1}) == 250
    finally:
        stop()
    assert len(seen) == 1


async def test_the_fan_out_runs_after_the_caller_returns_on_a_session_of_its_own(_engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core import background

    caller = AsyncSession(bind=_engine)
    release = asyncio.Event()
    ran: list = []

    async def job(s):
        await release.wait()
        ran.append((s is not caller, await s.scalar(text("SELECT 1"))))

    try:
        assert await background.dispatch(caller, job, name="test") is None
        # The caller is answered before the job has done anything.
        assert ran == []
        release.set()
        await background.drain()
        assert ran == [(True, 1)]
    finally:
        await caller.close()


async def test_the_interest_push_is_capped_per_event_and_per_person_per_day(session, make_club, make_event, fcm, monkeypatch):
    from app.api.interests.models import InterestCategory, Interests
    from app.api.users.models import UserInterests
    from app.core.notifications.triggers import notify_users_by_interest

    monkeypatch.setattr(settings, "INTEREST_PUSH_MAX_PER_EVENT", 3)
    monkeypatch.setattr(settings, "INTEREST_PUSH_MAX_PER_USER_PER_DAY", 2)
    owner, club = await make_club(name="Capped Club", email="capped@example.com")
    ev = await make_event(club=club, created_by=owner, name="Drone Day")
    category = InterestCategory(name="Robotics capped")
    session.add(category)
    await session.flush()
    interest = Interests(name="Drones capped", category_id=category.id)
    session.add(interest)
    await session.flush()
    ids = await _bulk_users(session, 6, "interested", tokens=True)
    for uid in ids:
        session.add(UserInterests(user_id=uid, interest_id=interest.id))
    # The newest account already had two of these today; the next one follows the club.
    tired, follower = ids[-1], ids[-2]
    for _ in range(2):
        session.add(Notifications(user_id=tired, title="x", description="y", type="nearby_event"))
    session.add(ClubUsersLink(club_id=club.id, user_id=follower, is_following=True))
    await session.flush()

    await notify_users_by_interest(session, category.id, category.name, club.id, ev.id, "Drone Day")
    await jobs.settle()

    told = set((await session.scalars(select(Notifications.user_id).where(Notifications.type == "nearby_event", Notifications.event_id == ev.id))).all())
    assert told == {ids[-3], ids[-4], ids[-5]}


# ─── 3. The inbox ──────────────────────────────────────────────────────────


async def _plan(session, stmt) -> str:
    """EXPLAIN of what `stmt` sends once the session's soft-delete filter is
    on, with sequential scans priced out so any usable index is chosen."""
    captured: list = []

    def before(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        await session.execute(stmt)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    statement, parameters = captured[-1]
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    raw = await session.connection()
    rows = (await raw.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
    await session.execute(text("SET LOCAL enable_seqscan = on"))
    return "\n".join(r[0] for r in rows)


async def test_the_soft_delete_filter_lets_postgres_use_partial_indexes(session):
    """`is_deleted IS false` matched no partial index built WHERE NOT
    is_deleted; `= false` folds into NOT is_deleted and does. Nothing here
    says is_deleted itself: the session's filter adds it."""
    (uid,) = await _bulk_users(session, 1, "planner")
    plan = await _plan(session, select(Notifications).where(Notifications.user_id == uid).order_by(Notifications.created_at.desc()).limit(20))
    assert "ix_notifications_user_created" in plan, plan
    unread = await _plan(session, select(func.count(Notifications.id)).where(Notifications.user_id == uid, Notifications.status == "unread"))
    assert "ix_notifications_user_unread" in unread, unread


async def test_the_inbox_prints_nothing(client, session, make_user, mint_token, capsys):
    user = await make_user(email="quiet-inbox@example.com")
    await session.commit()
    r = await client.get(f"{API}/notifications/fetch-list", headers=auth_header(await mint_token(user)))
    assert r.status_code == 200
    assert "DEBUG" not in capsys.readouterr().out


async def test_a_push_to_every_device_is_for_owners_only(client, session, make_admin, mint_token, fcm):
    support = await make_admin(email="support-bcast@example.com", role=AdminRoles.support)
    owner = await make_admin(email="owner-bcast@example.com")
    await _bulk_users(session, 3, "bcast", tokens=True)
    await session.commit()
    body = {"title": "Hello everyone", "body": "Test"}
    assert (await client.post(f"{API}/notifications/test/send-all", json=body, headers=auth_header(await mint_token(support)))).status_code == 403
    r = await client.post(f"{API}/notifications/test/send-all", json=body, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    # Answered before the sends; by the time the client has it, the jobs ran (the suite runs them after each request).
    assert r.json()["tokens_count"] >= 3 and sum(fcm.batches) == r.json()["tokens_count"]
    assert all(b <= 500 for b in fcm.batches)


# ─── 4. A deleted account signs up again ───────────────────────────────────


async def _register(client, email, password="Password123!", name="Again Person", phone=None):
    body = {"full_name": name, "email": email, "password": password}
    if phone:
        body["phone"] = phone
    return await client.post(f"{API}/user/register", json=body)


async def test_a_deleted_account_can_sign_up_again_and_its_details_are_scrubbed(client, session, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    first = await _register(client, "comeback@example.com", phone="9876512345")
    assert first.status_code == 200, first.text
    h = auth_header(first.json()["access_token"])
    me = await session.scalar(select(Users).where(Users.email == "comeback@example.com"))
    session.add(UserDeviceTokens(user_id=me.id, fcm_token="comeback-phone", platform="android"))
    await session.commit()

    assert (await client.delete(f"{API}/user/delete", headers=h)).status_code == 200
    gone = await session.scalar(select(Users).where(Users.id == me.id).execution_options(include_deleted=True, populate_existing=True))
    assert gone.is_deleted and gone.email.endswith("@deleted.invalid") and gone.phone is None
    assert gone.full_name == "Deleted user" and gone.password is None and gone.username.startswith("deleted_")
    assert await session.scalar(select(func.count()).select_from(UserDeviceTokens).where(UserDeviceTokens.user_id == me.id).execution_options(include_deleted=True)) == 0
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 401

    again = await _register(client, "comeback@example.com", phone="9876512345")
    assert again.status_code == 200, again.text


async def test_a_deleted_account_still_holding_its_address_does_not_block_a_new_one(client, session, make_user, monkeypatch):
    """Accounts deleted before the scrub (or by an admin) keep their address;
    the unique index no longer counts them."""
    old = await make_user(email="kept-address@example.com", username="kept-address")
    old.phone = "9000000001"
    old.soft_delete()
    await session.commit()
    monkeypatch.setattr("app.api.auth.service.id_token.verify_oauth2_token",
                        lambda token, request, client_id: {"email": "kept-address@example.com", "email_verified": True, "name": "Kept"})
    r = await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "android"})
    assert r.status_code == 200, r.text
    assert (await _register(client, "kept-address@example.com")).status_code == 400  # the Google account has it now


async def test_an_integrity_error_is_a_409_with_a_safe_message(client):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import IntegrityError

    from doth import application

    class _Unique(Exception):
        sqlstate = "23505"
        constraint_name = "uq_secret_internal_name"

    async def boom():
        orig = Exception("dup")
        orig.__cause__ = _Unique()
        raise IntegrityError("INSERT INTO x", {}, orig)

    application.add_api_route("/__test/conflict", boom, methods=["POST"])
    try:
        async with AsyncClient(transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://testserver") as c:
            r = await c.post("/__test/conflict")
    finally:
        application.router.routes = [rt for rt in application.router.routes if getattr(rt, "path", None) != "/__test/conflict"]
    assert r.status_code == 409 and r.json()["error_code"] == "CONFLICT"
    assert "uq_secret_internal_name" not in r.text and r.json()["message"] == "That already exists."


# ─── 5. Off the event loop ─────────────────────────────────────────────────


async def test_bcrypt_runs_on_a_worker_thread_and_a_missing_account_costs_the_same(session, make_user, monkeypatch):
    import bcrypt

    from app.core.auth import authentication

    await make_user(email="timing@example.com", username="timing")
    real = bcrypt.checkpw
    calls: list[int] = []

    def counted(password, hashed):
        calls.append(threading.get_ident())
        return real(password, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", counted)
    loop_thread = threading.get_ident()
    assert await authentication.authenticate_user(session, "timing@example.com", "Password123!")
    assert await authentication.authenticate_user(session, "nobody-here@example.com", "Password123!") is False
    # One check each: the account that does not exist checks the dummy hash.
    assert len(calls) == 2 and loop_thread not in calls


async def test_apples_keys_are_one_client_kept_for_the_process(monkeypatch, session):
    import jwt

    from app.api.auth import service as auth_service

    made: list = []

    class Keys:
        def __init__(self, url, **kwargs):
            made.append(kwargs)

        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key="k")

    monkeypatch.setattr(jwt, "PyJWKClient", Keys)
    monkeypatch.setattr(jwt, "decode", lambda *a, **kw: {"sub": f"apple.{uuid.uuid4().hex[:6]}", "email": f"apple-{uuid.uuid4().hex[:6]}@example.com"})
    for _ in range(3):
        await auth_service.apple_signin(session, identity_token="t", authorization_code="c")
    assert len(made) == 1 and made[0]["lifespan"] == 3600 and made[0]["cache_keys"] is True


def test_googles_certificates_are_fetched_once_per_max_age(monkeypatch):
    import httpx

    from app.api.auth import service as auth_service

    fetched: list[str] = []

    def fake_get(url, timeout=None):
        fetched.append(url)
        return httpx.Response(200, content=b'{"kid": "cert"}', headers={"cache-control": "public, max-age=2"})

    monkeypatch.setattr(auth_service.httpx, "get", fake_get)
    certs = auth_service._GoogleCerts()
    for _ in range(3):
        assert certs("https://www.googleapis.com/oauth2/v1/certs").data == b'{"kid": "cert"}'
    assert len(fetched) == 1
    real_clock = time.monotonic
    monkeypatch.setattr(auth_service.time, "monotonic", lambda: real_clock() + 5)
    certs("https://www.googleapis.com/oauth2/v1/certs")
    assert len(fetched) == 2


async def test_google_verification_runs_on_a_worker_thread(client, monkeypatch):
    from app.api.auth import service as auth_service

    threads: list[int] = []

    def verify(token, request, client_id):
        threads.append(threading.get_ident())
        return {"email": "threaded-google@example.com", "email_verified": True, "name": "Threaded"}

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", verify)
    r = await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "android"})
    assert r.status_code == 200 and threads and threading.get_ident() not in threads


async def test_a_sign_in_never_looks_up_a_place_inside_its_transaction(client, session, make_user, monkeypatch):
    from app.api.security import service as security

    await make_user(email="placelater@example.com", username="placelater")
    await session.commit()
    order: list[str] = []
    real_record = security.record

    async def record(*a, **kw):
        order.append("record")
        row = await real_record(*a, **kw)
        order.append("recorded")
        return row

    async def lookup(ip):
        order.append("lookup")
        return "Kochi, Kerala"

    monkeypatch.setattr(security, "record", record)
    monkeypatch.setattr(security, "lookup_place", lookup)
    r = await client.post(f"{API}/auth/token", data={"username": "placelater@example.com", "password": "Password123!"}, headers={"x-forwarded-for": "103.21.58.99"})
    assert r.status_code == 200
    # Recorded with what the cache knew; looked up afterwards, not inside.
    assert order == ["record", "recorded", "lookup"]
    place = await session.scalar(select(SignInAttempts.place).where(SignInAttempts.identifier == "placelater@example.com"))
    assert place == "Kochi, Kerala"


# ─── 5b. Second factor ─────────────────────────────────────────────────────


async def _enrolled_admin(client, session, make_admin, mint_token, email):
    from app.core.auth.authentication import get_password_hash

    admin = await make_admin(email=email)
    admin.password = get_password_hash("Password123!")
    await session.commit()
    token = await mint_token(admin)
    secret = (await client.post(f"{API}/auth/2fa/setup", headers=auth_header(token))).json()["secret"]
    enabled = await client.post(f"{API}/auth/2fa/enable", headers=auth_header(token), json={"code": pyotp.TOTP(secret).now()})
    assert enabled.status_code == 200, enabled.text
    return admin, secret, enabled.json()["recovery_codes"]


async def test_an_authenticator_code_is_good_once(client, session, make_admin, mint_token):
    _admin, secret, _codes = await _enrolled_admin(client, session, make_admin, mint_token, "replay@example.com")
    code = pyotp.TOTP(secret).at(time.time() + 30)
    creds = {"username": "replay@example.com", "password": "Password123!", "totp_code": code}
    assert (await client.post(f"{API}/auth/token", data=creds)).status_code == 200
    again = await client.post(f"{API}/auth/token", data=creds)
    assert again.status_code == 401 and again.json()["error_code"] == "TOTP_INVALID"


async def test_recovery_codes_are_64_bit_digests_and_old_bcrypt_ones_still_work_once(client, session, make_admin, mint_token):
    from app.core.auth.authentication import get_password_hash

    admin, _secret, codes = await _enrolled_admin(client, session, make_admin, mint_token, "digests@example.com")
    stored = await session.scalar(select(Users.totp_recovery_codes).where(Users.id == admin.id).execution_options(populate_existing=True))
    assert len(codes) == 8 and all(len(c.replace("-", "")) == 16 for c in codes)
    assert all(h.startswith("hmac-sha256:") for h in stored)
    creds = {"username": "digests@example.com", "password": "Password123!"}
    assert (await client.post(f"{API}/auth/token", data={**creds, "totp_code": codes[0]})).status_code == 200
    assert (await client.post(f"{API}/auth/token", data={**creds, "totp_code": codes[0]})).status_code == 401

    # A code from before the digests: a bcrypt hash of xxxx-xxxx.
    await session.execute(update(Users).where(Users.id == admin.id).values(totp_recovery_codes=[get_password_hash("ab12-cd34")]))
    await session.commit()
    assert (await client.post(f"{API}/auth/token", data={**creds, "totp_code": "AB12CD34"})).status_code == 200
    assert (await client.post(f"{API}/auth/token", data={**creds, "totp_code": "ab12-cd34"})).status_code == 401


async def test_the_secret_is_encrypted_when_the_server_has_a_key(client, session, make_admin, mint_token, monkeypatch):
    from cryptography.fernet import Fernet

    # The keyring (app/core/keyring.py) reads the setting afresh on every call: nothing to reset.
    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    admin, secret, _codes = await _enrolled_admin(client, session, make_admin, mint_token, "sealed@example.com")
    stored = await session.scalar(select(Users.totp_secret).where(Users.id == admin.id).execution_options(populate_existing=True))
    assert stored.startswith("fernet:") and secret not in stored
    creds = {"username": "sealed@example.com", "password": "Password123!", "totp_code": pyotp.TOTP(secret).at(time.time() + 30)}
    assert (await client.post(f"{API}/auth/token", data=creds)).status_code == 200


# ─── 6. Races that were 500s ───────────────────────────────────────────────


async def test_a_username_is_one_probe_and_a_taken_one_is_retried_not_a_500(session, monkeypatch):
    from app.api.users import service as users_service

    a = await users_service.create_user(session, full_name="Rahul Nair", email="rahul1@example.com", password="Password123!")
    b = await users_service.create_user(session, full_name="Rahul Nair", email="rahul2@example.com", password="Password123!")
    assert a.username == "rahul_nair" and b.username.startswith("rahul_nair_") and len(b.username) == len("rahul_nair_") + 6

    # Two sign-ups picking one name at once: the insert decides, and the
    # loser gets a fresh tail.
    async def same_name(user, s, is_guest=False):
        user.username = "rahul_nair"
        return "rahul_nair"

    monkeypatch.setattr(users_service, "generate_username", same_name)
    c = await users_service.create_user(session, full_name="Rahul Nair", email="rahul3@example.com", password="Password123!")
    assert c.username.startswith("rahul_nair_") and c.username != b.username


async def test_a_place_found_by_two_sign_ins_at_once_is_upserted(session, monkeypatch):
    from app.api.security import service as security

    async def lookup(ip):
        # Another sign-in from this address kept the place meanwhile.
        await session.execute(text("INSERT INTO ip_places (ip_address, place, looked_up_at) VALUES (:ip, 'Delhi', now())"), {"ip": ip})
        return "New Delhi, Delhi"

    monkeypatch.setattr(security, "lookup_place", lookup)
    assert await security.place_for(session, "49.36.200.1") == "New Delhi, Delhi"
    assert await session.scalar(text("SELECT place FROM ip_places WHERE ip_address = '49.36.200.1'")) == "New Delhi, Delhi"


async def test_two_first_profiles_at_once_make_one(client, session, make_user, mint_token, monkeypatch):
    from app.api.users import service as users_service
    from app.api.users.models import UserProfiles

    user = await make_user(email="twoprofiles@example.com", username="twoprofiles")
    await session.commit()
    h = auth_header(await mint_token(user))
    real = users_service.validate_unique

    async def racing(s, unique_together):
        # The other request's profile lands between the check and the insert.
        if not await s.scalar(select(func.count()).select_from(UserProfiles).where(UserProfiles.user_id == user.id)):
            s.add(UserProfiles(user_id=user.id, full_name="Other Request"))
            await s.flush()
        return await real(s, unique_together=unique_together)

    monkeypatch.setattr(users_service, "validate_unique", racing)
    r = await client.post(f"{API}/user/profile/create", data={"full_name": "Two Profiles"}, headers=h)
    assert r.status_code == 400 and r.json()["error_code"] == "PROFILE_EXISTS"
    assert await session.scalar(select(func.count()).select_from(UserProfiles).where(UserProfiles.user_id == user.id)) == 1


async def test_a_missing_profile_is_made_once_with_the_accounts_name(session, make_user):
    """update_profile_picture made `UserProfiles(user_id=...)` with no name,
    which the table refuses; and two requests could each make one."""
    from app.api.users import service as users_service
    from app.api.users.models import UserProfiles

    user = await make_user(email="picfirst@example.com", username="picfirst")
    one = await users_service._ensure_profile(session, user.id)
    two = await users_service._ensure_profile(session, user.id)
    assert one.id == two.id and one.full_name == user.full_name
    assert await session.scalar(select(func.count()).select_from(UserProfiles).where(UserProfiles.user_id == user.id)) == 1


async def test_selecting_interests_is_one_insert_and_never_duplicates(client, session, make_user, mint_token):
    from app.api.interests.models import InterestCategory, Interests
    from app.api.users.models import UserInterests

    cat = InterestCategory(name="Pick cat")
    session.add(cat)
    await session.flush()
    a, b = Interests(name="Pick A", category_id=cat.id), Interests(name="Pick B", category_id=cat.id)
    session.add_all([a, b])
    user = await make_user(email="picker@example.com", username="picker")
    await session.commit()
    h = auth_header(await mint_token(user))
    seen, stop = _count_inserts(session, "user_interests")
    try:
        r = await client.post(f"{API}/user/interests/select", json={"interest_ids": [a.id, a.id, b.id, 999999]}, headers=h)
    finally:
        stop()
    assert r.status_code == 200, r.text
    assert len(seen) == 1
    assert sorted((await session.scalars(select(UserInterests.interest_id).where(UserInterests.user_id == user.id))).all()) == sorted([a.id, b.id])


async def test_an_address_held_by_an_app_and_a_club_account_is_a_409_for_the_admin_reset(client, session, make_admin, make_user, make_club, mint_token):
    owner = await make_admin(email="resetter@example.com")
    await make_user(email="shared-reset@example.com", username="shared-app")
    await make_club(name="Shared Reset Club", email="shared-reset@example.com")
    await session.commit()
    r = await client.post(f"{API}/auth/admin/reset-password", json={"email_or_username": "shared-reset@example.com", "new_password": "Another123!"}, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 409 and r.json()["error_code"] == "AMBIGUOUS_ACCOUNT"
    ok = await client.post(f"{API}/auth/admin/reset-password", json={"email_or_username": "shared-app", "new_password": "Another123!"}, headers=auth_header(await mint_token(owner)))
    assert ok.status_code == 200


async def test_my_events_come_newest_registration_first(session, make_club, make_event, make_user, make_registration):
    from app.api.users import service as users_service

    owner, club = await make_club(name="Order Club", email="order-club@example.com")
    person = await make_user(email="orderly@example.com", username="orderly")
    regs = []
    for i in range(4):
        ev = await make_event(club=club, created_by=owner, name=f"Ordered {i}")
        regs.append(await make_registration(event=ev, user=person, created_at=NOW + timedelta(minutes=i)))
    listed = await users_service.list_registered_events(session, person.id, limit=10)
    assert [r.id for r in listed] == [r.id for r in reversed(regs)]


async def test_an_unapproved_clubs_account_hears_403_not_a_dead_session(client, session, make_club, mint_token):
    owner, _club = await make_club(name="Pending Club", email="pending-club@example.com", is_verified=False)
    await session.commit()
    r = await client.get(f"{API}/clubs/info", headers=auth_header(await mint_token(owner)))
    assert r.status_code == 403 and r.json()["error_code"] == "CLUB_NOT_VERIFIED"


# ─── 7. Refresh rotation ───────────────────────────────────────────────────


async def _signed_in(client, make_user, email):
    await make_user(email=email, username=email.split("@")[0])
    r = await client.post(f"{API}/auth/token", data={"username": email, "password": "Password123!"})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()


async def test_each_refresh_hands_back_a_new_refresh_token(client, make_user):
    first = await _signed_in(client, make_user, "rotate@example.com")
    r1 = await client.post(f"{API}/auth/refresh", data={"token": first["refresh_token"]})
    assert r1.status_code == 200 and r1.json()["refresh_token"] != first["refresh_token"]
    client.cookies.clear()
    r2 = await client.post(f"{API}/auth/refresh", data={"token": r1.json()["refresh_token"]})
    assert r2.status_code == 200 and r2.json()["refresh_token"] not in (first["refresh_token"], r1.json()["refresh_token"])
    # Rotation does not stretch the session: the new token ends when the first did.
    from app.core.auth.jwt import decode_jwt_token

    assert decode_jwt_token(r2.json()["refresh_token"])["exp"] <= decode_jwt_token(first["refresh_token"])["exp"] + 1


async def test_a_lost_answer_is_given_again_even_an_hour_later(client, session, make_user):
    """The app was killed while its refresh was in flight: the new token
    never arrived. The next launch, whenever it is, presents the old one
    and gets the same new refresh token again; nothing is revoked."""
    from app.core.auth.jwt import decode_jwt_token

    first = await _signed_in(client, make_user, "lost-answer@example.com")
    lost = await client.post(f"{API}/auth/refresh", data={"token": first["refresh_token"]})
    assert lost.status_code == 200
    sid = decode_jwt_token(first["refresh_token"])["sid"]
    # Nothing depends on the clock: the rotation might as well be an hour old.
    await session.execute(update(UserSessions).where(UserSessions.id == uuid.UUID(sid)).values(rotated_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await session.commit()
    client.cookies.clear()

    again = await client.post(f"{API}/auth/refresh", data={"token": first["refresh_token"]})
    assert again.status_code == 200, again.text
    assert again.json()["refresh_token"] == lost.json()["refresh_token"]
    assert decode_jwt_token(again.json()["refresh_token"])["gen"] == 1
    # And the token it was given works as the newest.
    client.cookies.clear()
    onward = await client.post(f"{API}/auth/refresh", data={"token": again.json()["refresh_token"]})
    assert onward.status_code == 200 and decode_jwt_token(onward.json()["refresh_token"])["gen"] == 2


async def test_once_the_new_token_is_used_the_old_one_ends_the_session(client, session, make_user):
    first = await _signed_in(client, make_user, "stolen@example.com")
    new = await client.post(f"{API}/auth/refresh", data={"token": first["refresh_token"]})
    assert new.status_code == 200
    client.cookies.clear()
    newer = await client.post(f"{API}/auth/refresh", data={"token": new.json()["refresh_token"]})
    assert newer.status_code == 200
    client.cookies.clear()

    thief = await client.post(f"{API}/auth/refresh", data={"token": first["refresh_token"]})
    assert thief.status_code == 401 and thief.json()["error_code"] == "SESSION_REUSED"
    # Everyone holding the session is out: the newest refresh token and the access token too.
    assert (await client.post(f"{API}/auth/refresh", data={"token": newer.json()["refresh_token"]})).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers=auth_header(newer.json()["access_token"]))).status_code == 401
    assert await session.scalar(select(func.count()).select_from(SignInAttempts).where(SignInAttempts.result == "refresh_reused")) == 1


async def test_a_token_two_generations_back_is_reuse_at_once(client, session, make_user):
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token, decode_jwt_token

    first = await _signed_in(client, make_user, "twoback@example.com")
    claims = decode_jwt_token(first["refresh_token"])
    # The session is at generation 5; a token of generation 3 turns up a moment later.
    await session.execute(update(UserSessions).where(UserSessions.id == uuid.UUID(claims["sid"])).values(refresh_generation=5, rotated_at=datetime.now(timezone.utc)))
    await session.commit()
    old = create_access_token(
        AuthTokenData(user_id=claims["user_id"], token_type="refresh_token", token_version=claims["token_version"], sid=claims["sid"], gen=3).model_dump(),
        timedelta(days=1),
    )
    r = await client.post(f"{API}/auth/refresh", data={"token": old})
    assert r.status_code == 401 and r.json()["error_code"] == "SESSION_REUSED"
    assert await session.scalar(select(UserSessions.revoked).where(UserSessions.id == uuid.UUID(claims["sid"])).execution_options(populate_existing=True)) is True


# ─── 8. Impersonation ──────────────────────────────────────────────────────


async def _handoff(client, session, make_admin, make_user, mint_token, email):
    admin = await make_admin(email=f"imp-{email}")
    target = await make_user(email=email, username=email.split("@")[0])
    await session.commit()
    r = await client.post(f"{API}/auth/admin/impersonate/{target.id}", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200, r.text
    return admin, target, r.json()["token"]


async def test_a_handoff_is_exchanged_once_for_a_session_that_ends_and_never_renews(client, session, make_admin, make_user, mint_token):
    admin, target, handoff = await _handoff(client, session, make_admin, make_user, mint_token, "imp-target@example.com")
    first = await client.post(f"{API}/auth/impersonate/exchange", data={"token": handoff})
    assert first.status_code == 200, first.text
    assert first.json()["refresh_token"] == "" and "refresh_token" not in {c.name for c in client.cookies.jar if c.value}
    again = await client.post(f"{API}/auth/impersonate/exchange", data={"token": handoff})
    assert again.status_code == 401 and again.json()["error_code"] == "HANDOFF_USED"

    row = await session.scalar(select(UserSessions).where(UserSessions.user_id == target.id, UserSessions.impersonated_by == admin.id))
    assert row is not None
    ends = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=29) < ends - datetime.now(timezone.utc) <= timedelta(minutes=60)
    access = first.json()["access_token"]
    assert (await client.get(f"{API}/auth/me", headers=auth_header(access))).status_code == 200
    audit = await session.scalar(text("SELECT count(*) FROM admin_audit_logs WHERE action = 'user.impersonate.exchange' AND entity_id = :t"), {"t": str(target.id)})
    assert audit == 1

    # Past its end, the session is over whatever the token says.
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.commit()
    assert (await client.get(f"{API}/auth/me", headers=auth_header(access))).status_code == 401


async def test_an_impersonated_session_ends_when_the_admin_loses_the_right(client, session, make_admin, make_user, mint_token):
    admin, _target, handoff = await _handoff(client, session, make_admin, make_user, mint_token, "imp-demoted@example.com")
    access = (await client.post(f"{API}/auth/impersonate/exchange", data={"token": handoff})).json()["access_token"]
    assert (await client.get(f"{API}/auth/me", headers=auth_header(access))).status_code == 200
    admin.admin_role = AdminRoles.support
    await session.commit()
    r = await client.get(f"{API}/auth/me", headers=auth_header(access))
    assert r.status_code == 401


async def test_an_impersonated_refresh_token_is_never_renewed(client, session, make_user):
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    target = await make_user(email="old-imp@example.com", username="old-imp")
    old = create_access_token(
        AuthTokenData(user_id=target.id, token_type="refresh_token", token_version=target.token_version, impersonated_by=1).model_dump(),
        timedelta(days=7),
    )
    r = await client.post(f"{API}/auth/refresh", data={"token": old})
    assert r.status_code == 401 and r.json()["error_code"] == "IMPERSONATION_ENDED"


# ─── 9. Rate limits ────────────────────────────────────────────────────────


async def test_google_sign_in_is_rate_limited(client, monkeypatch):
    from app.api.auth import service as auth_service

    monkeypatch.setattr(auth_service.id_token, "verify_oauth2_token", lambda t, r, c: {"email": "ratelimited-google@example.com", "email_verified": True, "name": "R"})
    codes = [(await client.post(f"{API}/auth/google", json={"id_token": "t", "platform": "android"})).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


async def test_twenty_wrong_passwords_from_anywhere_lock_the_account_but_not_its_owners_address(client, session, make_user, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    await make_user(email="botnet@example.com", username="botnet")
    await session.commit()

    async def login(ip, password="nope"):
        return await client.post(f"{API}/auth/token", data={"username": "botnet@example.com", "password": password}, headers={"x-forwarded-for": ip})

    assert (await login("49.36.1.1", "Password123!")).status_code == 200  # the owner's address
    for i in range(20):
        assert (await login(f"185.220.{i}.9")).status_code == 401
    blocked = await login("185.220.99.9", "Password123!")
    assert blocked.status_code == 429 and blocked.json()["error_code"] == "LOGIN_LOCKED"
    assert (await login("49.36.1.1", "Password123!")).status_code == 200


def test_the_limiter_store_is_a_setting():
    assert limiter._storage_uri == settings.RATE_LIMIT_STORAGE


async def test_two_factor_changes_are_rate_limited(client, session, make_admin, mint_token):
    admin = await make_admin(email="2fa-limit@example.com")
    h = auth_header(await mint_token(admin))
    await client.post(f"{API}/auth/2fa/setup", headers=h)
    codes = [(await client.post(f"{API}/auth/2fa/enable", headers=h, json={"code": "000000"})).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429


# ─── 11. The closeout retention lock ───────────────────────────────────────


async def test_the_retention_lock_is_released_whatever_the_work_commits(_engine):
    """It was taken through the request's session, which hands its connection
    back at every commit; the unlock then ran on another one and released
    nothing. singleton holds it on a connection of its own."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.closeout.service import RETENTION_LOCK
    from app.db.locks import singleton

    work = AsyncSession(bind=_engine)
    try:
        async with singleton(work, (RETENTION_LOCK, 424242)) as got:
            assert got
            for _ in range(3):
                await work.execute(text("SELECT 1"))
                await work.commit()
            async with _engine.connect() as other:
                assert await other.scalar(text(f"SELECT pg_try_advisory_lock({RETENTION_LOCK}, 424242)")) is False
    finally:
        await work.close()
    async with _engine.connect() as other:
        assert await other.scalar(text(f"SELECT pg_try_advisory_lock({RETENTION_LOCK}, 424242)")) is True
        await other.scalar(text(f"SELECT pg_advisory_unlock({RETENTION_LOCK}, 424242)"))


async def test_a_retention_run_takes_the_lock_through_singleton(session, monkeypatch, make_club, make_event):
    from contextlib import asynccontextmanager

    from app.api.closeout import service as closeout

    owner, club = await make_club(name="Locked Club", email="locked-club@example.com")
    ev = await make_event(club=club, created_by=owner, name="Locked Night")
    keys: list = []

    @asynccontextmanager
    async def held(target, key):
        keys.append(key)
        yield False

    monkeypatch.setattr(closeout, "singleton", held)
    r = await closeout.run_retention(session, ev, manual=False)
    assert keys == [(closeout.RETENTION_LOCK, ev.id)] and r["error"] == "already running"


# ─── 12. Error handling ────────────────────────────────────────────────────


async def test_a_500_answers_without_waiting_for_discord_and_reports_once_per_kind(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.core.utils import discord
    from doth import application

    monkeypatch.setattr(settings, "DISCORD_ERROR_WEBHOOK", "https://discord.invalid/hook")
    posted: list = []

    async def slow(details, *a, **kw):
        await asyncio.sleep(3)
        posted.append(details)

    monkeypatch.setattr(discord, "send_discord_notification", slow)

    async def boom():
        raise RuntimeError("kaboom")

    application.add_api_route("/__test/boom", boom, methods=["GET"])
    try:
        async with AsyncClient(transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://testserver") as c:
            started = time.monotonic()
            r1 = await c.get("/__test/boom")
            r2 = await c.get("/__test/boom")
            took = time.monotonic() - started
    finally:
        application.router.routes = [rt for rt in application.router.routes if getattr(rt, "path", None) != "/__test/boom"]
    assert r1.status_code == r2.status_code == 500 and r1.json()["track_id"]
    assert took < 2.0
    assert len(discord._tasks) == 1  # the second of the same kind on the same path is left out
    for task in list(discord._tasks):
        task.cancel()


async def test_a_refusal_wrapped_by_sqlalchemy_is_answered_not_a_bare_500():
    """A regression guard rather than a fix: re-raising from the handler
    happened to work (the route's own exception wrapper and the app's both
    look handlers up), and now the handler answers directly."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import StatementError

    from app.response import CustomHTTPException
    from doth import application

    async def wrapped():
        raise StatementError("bad value", "UPDATE x", {}, CustomHTTPException(422, message="That file is not an image"))

    application.add_api_route("/__test/wrapped", wrapped, methods=["GET"])
    try:
        async with AsyncClient(transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://testserver") as c:
            r = await c.get("/__test/wrapped")
    finally:
        application.router.routes = [rt for rt in application.router.routes if getattr(rt, "path", None) != "/__test/wrapped"]
    assert r.status_code == 422 and r.json()["message"] == "That file is not an image"


def test_each_exception_handler_is_its_own_function():
    from app.response import CustomHTTPException
    from doth import application

    assert application.exception_handlers[Exception].__name__ == "unhandled_exception_handler"
    assert application.exception_handlers[CustomHTTPException].__name__ == "custom_http_exception_handler"


# ─── 13. The admin's user list ─────────────────────────────────────────────


async def test_the_user_list_pages_by_keyset_and_caps_its_total(client, session, make_admin, mint_token, monkeypatch):
    from app.api.users import router as users_router

    monkeypatch.setattr(users_router, "_TOTAL_CAP", 4)
    owner = await make_admin(email="lister@example.com")
    await _bulk_users(session, 7, "paged")
    await session.commit()
    h = auth_header(await mint_token(owner))
    url = f"{API}/user/admin/list?user_type=app_user&limit=3"
    seen: list[int] = []
    first = await client.get(url, headers=h)
    assert first.json()["total"] == 4 and first.headers.get("x-total-capped") == "true"
    page = first.json()
    while True:
        seen += [u["id"] for u in page["items"]]
        if not page["next"]:
            break
        assert "cursor=" in page["next"] and "offset=" not in page["next"]
        page = (await client.get(page["next"], headers=h)).json()
    assert len(seen) == len(set(seen)) == 7


async def test_the_search_finds_an_id_an_address_or_a_username_exactly(client, session, make_admin, make_user, mint_token):
    owner = await make_admin(email="searcher@example.com")
    target = await make_user(email="Exact.Person@example.com", username="exactperson")
    await make_user(email="exactperson-lookalike@example.com", username="exactperson_2")
    await session.commit()
    h = auth_header(await mint_token(owner))

    async def found(term):
        r = await client.get(f"{API}/user/admin/list", params={"search": term}, headers=h)
        return {u["id"] for u in r.json()["items"]}

    assert await found(str(target.id)) >= {target.id}
    assert await found("exact.person@EXAMPLE.com ") == {target.id}
    assert target.id in await found("exactperson")
    assert await found("ex") == set()  # too short for "contains", and nobody is called that


async def test_the_export_reads_by_keyset_not_offset(client, session, make_admin, mint_token, monkeypatch):
    from app.api.users import router as users_router

    monkeypatch.setattr(users_router, "_EXPORT_CHUNK", 2)
    owner = await make_admin(email="exporter@example.com")
    await _bulk_users(session, 5, "exported")
    await session.commit()
    captured: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if "FROM users" in statement and "LIMIT" in statement:
            captured.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        r = await client.get(f"{API}/user/admin/export?user_type=app_user", headers=auth_header(await mint_token(owner)))
    finally:
        event.remove(engine, "before_cursor_execute", before)
    rows = [line for line in r.text.splitlines() if ",exported " in line]
    # Five rows, each once -- they share one created_at, so the id breaks the ties.
    assert r.status_code == 200 and len(rows) == len(set(rows)) == 5
    assert captured and not any("OFFSET" in s for s in captured)


async def test_the_overview_is_kept_for_a_minute_and_estimates_huge_tables(client, session, make_admin, mint_token, monkeypatch):
    from app.api import admin_overview

    owner = await make_admin(email="overseer@example.com")
    await _bulk_users(session, 3, "overview")
    await session.commit()
    h = auth_header(await mint_token(owner))
    worked: list[int] = []
    real = admin_overview._work_out

    async def counted(*a, **kw):
        worked.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(admin_overview, "_work_out", counted)
    one = (await client.get(f"{API}/admin/overview", headers=h)).json()
    two = (await client.get(f"{API}/admin/overview", headers=h)).json()
    assert len(worked) == 1 and one == two and one["estimated"] == []

    admin_overview.reset_cache()
    await session.execute(text("ANALYZE users"))
    monkeypatch.setattr(admin_overview, "ESTIMATE_ABOVE", 0)
    three = (await client.get(f"{API}/admin/overview", headers=h)).json()
    assert "users" in three["estimated"] and three["totals"]["users"] > 0


# ─── 14. Housekeeping ──────────────────────────────────────────────────────


async def test_the_sweep_removes_spent_sessions_and_old_attempts_in_batches(session, make_user, monkeypatch):
    from app.api.auth import retention

    monkeypatch.setattr(retention, "BATCH", 2)
    user = await make_user(email="sweep-me@example.com", username="sweep-me")
    now = datetime.now(timezone.utc)
    days = settings.REFRESH_TOKEN_EXPIRE_DAYS
    keep = [UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now), UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now - timedelta(hours=2), revoked=True)]
    drop = [
        UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now - timedelta(days=days + 1)),
        UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now - timedelta(days=days + 2)),
        UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now - timedelta(days=2), revoked=True),
        UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now, expires_at=now - timedelta(days=2)),
        UserSessions(id=uuid.uuid4(), user_id=user.id, last_seen_at=now - timedelta(days=days + 3)),
    ]
    session.add_all(keep + drop)
    for age in (1, 179, 181, 400, 500):
        session.add(SignInAttempts(identifier="sweep-me", method="password", result="wrong_password", created_at=now - timedelta(days=age)))
    await session.commit()

    removed = await retention.purge(session, now)

    assert removed == {"sessions": 5, "sign_in_attempts": 3}
    left = set((await session.scalars(select(UserSessions.id).where(UserSessions.user_id == user.id))).all())
    assert left == {s.id for s in keep}


def test_last_seen_is_written_at_most_every_five_minutes():
    from app.api.auth import sessions

    assert sessions.TOUCH_EVERY_SECONDS == 300
