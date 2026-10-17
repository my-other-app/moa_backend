"""Who the caller is behind the proxy, and the sign-in and sign-up gaps the
website's email sign-in ran into.

In production every request reached the app from Docker's bridge address, so
each per-address rate limit was one budget for the whole platform, while
sign-in records and lockouts believed whatever X-Forwarded-For the caller
typed. app.core.net now believes the forwarded headers only from an internal
address, reading from the right.
"""
from app.core.net import real_client
from app.api.users.models import Users

PASSWORD = "Password123!"


# ─── The caller's address ────────────────────────────────────────────────


def test_a_public_connection_cannot_claim_another_address():
    assert real_client("49.37.10.2", "1.2.3.4", "5.6.7.8") == "49.37.10.2"


def test_behind_our_proxy_the_client_is_the_first_public_hop_from_the_right():
    # nginx appends what it saw; anything the client wrote sits to its left.
    assert real_client("172.18.0.1", "1.2.3.4, 49.37.10.9") == "49.37.10.9"
    # A chain of internal hops in front of the client.
    assert real_client("10.0.0.5", "49.37.10.1, 10.0.0.1") == "49.37.10.1"
    # X-Real-IP when the forwarded chain has nothing public.
    assert real_client("172.18.0.1", "10.0.0.3", "49.37.10.4") == "49.37.10.4"
    assert real_client("172.18.0.1", None, "49.37.10.5") == "49.37.10.5"
    # Nothing to go on: the connection itself.
    assert real_client("127.0.0.1") == "127.0.0.1"
    # Junk in the headers is skipped, never returned.
    assert real_client("172.18.0.1", "not-an-ip, 49.37.10.6") == "49.37.10.6"
    assert real_client(None) == ""


async def test_rate_limits_count_each_caller_not_the_proxy(client, make_user, session):
    await make_user(email="limits@example.com")
    await session.commit()

    def attempt(ip):
        return client.post(
            "/api/v1/auth/token",
            data={"username": "limits@example.com", "password": "wrong-password"},
            headers={"x-forwarded-for": ip},
        )

    codes = [(await attempt("49.37.20.1")).status_code for _ in range(21)]
    # The limit (20 a minute) or the account's own lockout stops this caller...
    assert 429 in codes, codes
    # ...and someone else on another address is not caught by it. (A 429
    # here could only be the per-account lockout, which carries its code.)
    other = await client.post(
        "/api/v1/auth/token",
        data={"username": "nobody-else@example.com", "password": "x"},
        headers={"x-forwarded-for": "49.37.20.2"},
    )
    assert other.status_code == 401, other.text


# ─── Signing in ──────────────────────────────────────────────────────────


async def test_an_address_signs_in_whatever_its_case(client, make_user, session):
    await make_user(email="Ravi.Menon@Example.com", username="ravimenon")
    await session.commit()
    r = await client.post("/api/v1/auth/token", data={"username": "ravi.menon@example.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/auth/token", data={"username": " RAVI.MENON@EXAMPLE.COM ", "password": PASSWORD})
    assert r.status_code == 200, r.text


async def test_two_accounts_differing_only_in_case_are_never_guessed_between(client, make_user, session):
    await make_user(email="Twin@Example.com", username="twin1")
    await make_user(email="twin@example.com", username="twin2")
    await session.commit()
    # Each exact address still works...
    assert (await client.post("/api/v1/auth/token", data={"username": "twin@example.com", "password": PASSWORD})).status_code == 200
    # ...but a third spelling matches both, so it opens neither.
    assert (await client.post("/api/v1/auth/token", data={"username": "TWIN@EXAMPLE.COM", "password": PASSWORD})).status_code == 401


async def test_a_suspended_account_is_refused_at_the_door(client, make_user, session):
    user = await make_user(email="suspended@example.com")
    user.is_active = False
    await session.commit()
    r = await client.post("/api/v1/auth/token", data={"username": "suspended@example.com", "password": PASSWORD})
    assert r.status_code == 403, r.text
    assert r.json()["error_code"] == "ACCOUNT_SUSPENDED"


# ─── Signing up ──────────────────────────────────────────────────────────


def _register(client, email, password="secret12"):
    return client.post("/api/v1/user/register", json={"full_name": "Asha Rao", "email": email, "password": password})


async def test_an_address_taken_in_another_case_is_taken(client, make_user, session):
    await make_user(email="Asha@Example.com", username="asha")
    await session.commit()
    r = await _register(client, "asha@example.com")
    assert r.status_code == 400, r.text
    assert r.json()["errors"]["email"] == "email already exists"


async def test_sign_up_holds_the_password_rule_stores_one_case_and_records_a_session(client, session):
    short = await _register(client, "newperson@example.com", password="12345")
    assert short.status_code == 400, short.text
    assert "password" in short.json()["errors"]

    r = await _register(client, "  New.Person@Example.COM ")
    assert r.status_code == 200, r.text
    from sqlalchemy import select

    from app.api.auth.sessions import UserSessions
    from app.core.auth.jwt import decode_jwt_token

    stored = await session.scalar(select(Users.email).where(Users.username == r.json()["username"]))
    assert stored == "new.person@example.com"
    sid = decode_jwt_token(r.json()["access_token"]).get("sid")
    assert sid, "the sign-up token carries a session"
    assert await session.scalar(select(UserSessions.id).where(UserSessions.id == sid)) is not None
