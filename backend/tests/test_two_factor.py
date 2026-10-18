"""Two-factor for admin accounts.

A stolen admin password was the whole chain: impersonation, refunds and
deletion all sit behind it and nothing else stood in the way.
"""

import time

import pyotp
import pytest

from tests.conftest import auth_header


def next_code(secret: str) -> str:
    """The code after the current one. A code is good once, and enrolling
    used the current one; the next is inside the one-step allowance."""
    return pyotp.TOTP(secret).at(time.time() + 30)


async def enrol(client, token) -> str:
    """Runs the two-step enrolment and returns the secret."""
    setup = await client.post("/api/v1/auth/2fa/setup", headers=auth_header(token))
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]

    enable = await client.post(
        "/api/v1/auth/2fa/enable",
        headers=auth_header(token),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert enable.status_code == 200, enable.text
    return secret


@pytest.mark.asyncio
async def test_setup_does_not_enable_on_its_own(client, make_admin, mint_token):
    """
    Enabling on generation would lock out anyone whose authenticator failed
    to scan, with no way back in.
    """
    admin = await make_admin()
    token = await mint_token(admin)

    await client.post("/api/v1/auth/2fa/setup", headers=auth_header(token))

    status = await client.get("/api/v1/auth/2fa", headers=auth_header(token))
    assert status.json()["enabled"] is False


@pytest.mark.asyncio
async def test_a_wrong_code_does_not_enable_it(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)
    await client.post("/api/v1/auth/2fa/setup", headers=auth_header(token))

    response = await client.post(
        "/api/v1/auth/2fa/enable",
        headers=auth_header(token),
        json={"code": "000000"},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_enrolment_returns_recovery_codes_once(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)
    setup = await client.post("/api/v1/auth/2fa/setup", headers=auth_header(token))
    secret = setup.json()["secret"]

    enable = await client.post(
        "/api/v1/auth/2fa/enable",
        headers=auth_header(token),
        json={"code": pyotp.TOTP(secret).now()},
    )

    codes = enable.json()["recovery_codes"]
    assert len(codes) == 8
    # Only hashes are stored, so they are never returned again.
    status = await client.get("/api/v1/auth/2fa", headers=auth_header(token))
    assert status.json()["recovery_codes_remaining"] == 8
    assert "recovery_codes" not in status.json()


@pytest.mark.asyncio
async def test_login_demands_a_code_once_enabled(client, make_admin, mint_token):
    admin = await make_admin(email="twofa@example.com")
    admin.password = __import__(
        "app.core.auth.authentication", fromlist=["get_password_hash"]
    ).get_password_hash("Password123!")
    token = await mint_token(admin)
    await enrol(client, token)

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "twofa@example.com", "password": "Password123!"},
    )

    assert response.status_code == 401, response.text
    # The distinct code is what lets the UI show a code field rather than
    # claiming the password was wrong.
    assert response.json()["error_code"] == "TOTP_REQUIRED"


@pytest.mark.asyncio
async def test_login_succeeds_with_a_valid_code(client, make_admin, mint_token):
    from app.core.auth.authentication import get_password_hash

    admin = await make_admin(email="twofaok@example.com")
    admin.password = get_password_hash("Password123!")
    token = await mint_token(admin)
    secret = await enrol(client, token)

    response = await client.post(
        "/api/v1/auth/token",
        data={
            "username": "twofaok@example.com",
            "password": "Password123!",
            "totp_code": next_code(secret),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


@pytest.mark.asyncio
async def test_a_recovery_code_works_and_is_then_spent(client, make_admin, mint_token):
    """
    Single use. Leaving one active would turn a single leaked code into
    permanent access.
    """
    from app.core.auth.authentication import get_password_hash

    admin = await make_admin(email="recovery@example.com")
    admin.password = get_password_hash("Password123!")
    token = await mint_token(admin)

    setup = await client.post("/api/v1/auth/2fa/setup", headers=auth_header(token))
    secret = setup.json()["secret"]
    enable = await client.post(
        "/api/v1/auth/2fa/enable",
        headers=auth_header(token),
        json={"code": pyotp.TOTP(secret).now()},
    )
    recovery = enable.json()["recovery_codes"][0]

    creds = {"username": "recovery@example.com", "password": "Password123!"}

    first = await client.post(
        "/api/v1/auth/token", data={**creds, "totp_code": recovery}
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/api/v1/auth/token", data={**creds, "totp_code": recovery}
    )
    assert second.status_code == 401, second.text


@pytest.mark.asyncio
async def test_disabling_requires_a_current_code(client, make_admin, mint_token):
    """
    Otherwise anyone at an unlocked screen could remove the second factor,
    which is the one thing it exists to prevent.
    """
    admin = await make_admin()
    token = await mint_token(admin)
    secret = await enrol(client, token)

    refused = await client.post(
        "/api/v1/auth/2fa/disable",
        headers=auth_header(token),
        json={"code": "000000"},
    )
    assert refused.status_code == 400, refused.text

    allowed = await client.post(
        "/api/v1/auth/2fa/disable",
        headers=auth_header(token),
        json={"code": next_code(secret)},
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_an_account_without_2fa_logs_in_normally(client, make_user):
    """The second factor must not become mandatory for everyone."""
    await make_user(email="no2fa@example.com", password="Password123!")

    response = await client.post(
        "/api/v1/auth/token",
        data={"username": "no2fa@example.com", "password": "Password123!"},
    )

    assert response.status_code == 200, response.text
