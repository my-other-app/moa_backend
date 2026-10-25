"""Two-factor for every admin (app/api/admin_ops/two_factor.py): a grace for
the admins already there, none for a new one, and the routes that turn it on
open throughout."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.admin_ops import runtime
from app.api.admin_ops.models import AdminInvites
from app.api.users.models import AdminRoles
from tests.conftest import auth_header

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_runtime():
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


async def _grace_started(session, days_ago: float):
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    await runtime.write(session, runtime.GRACE_STARTED_AT, at.isoformat(), actor_id=None)
    await session.commit()


async def test_an_admin_without_two_factor_works_during_the_grace_and_sees_its_end(client, session, make_admin, mint_token):
    admin = await make_admin(email="grace@example.com")
    await _grace_started(session, 2)
    h = auth_header(await mint_token(admin))
    assert (await client.get(f"{API}/admin/payouts", headers=h)).status_code == 200
    me = (await client.get(f"{API}/admin/ops/me", headers=h)).json()
    assert me["two_factor"]["blocked"] is False and me["two_factor"]["enabled"] is False
    ends = datetime.fromisoformat(me["two_factor"]["grace_ends_at"])
    assert timedelta(days=4, hours=23) < ends - datetime.now(timezone.utc) < timedelta(days=5, hours=1)


async def test_after_the_grace_every_admin_route_refuses_but_two_factor_set_up_stays_open(client, session, make_admin, mint_token):
    admin = await make_admin(email="late@example.com", role=AdminRoles.viewer)
    await _grace_started(session, 8)
    h = auth_header(await mint_token(admin))
    refused = await client.get(f"{API}/admin/payouts", headers=h)
    assert refused.status_code == 403 and refused.json()["error_code"] == "ADMIN_2FA_REQUIRED"
    assert (await client.get(f"{API}/clubs/admin/list", headers=h)).status_code == 403
    # What turning it on needs, and who you are, still answer.
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 200
    assert (await client.get(f"{API}/auth/2fa", headers=h)).status_code == 200
    setup = await client.post(f"{API}/auth/2fa/setup", headers=h)
    assert setup.status_code == 200, setup.text
    me = (await client.get(f"{API}/admin/ops/me", headers=h)).json()
    assert me["two_factor"]["blocked"] is True


async def test_an_admin_with_two_factor_is_never_refused(client, session, make_admin, mint_token):
    admin = await make_admin(email="secure@example.com")
    admin.totp_enabled = True
    await session.flush()
    await _grace_started(session, 30)
    h = auth_header(await mint_token(admin))
    assert (await client.get(f"{API}/admin/payouts", headers=h)).status_code == 200


async def test_an_admin_added_after_the_rule_began_has_no_grace(client, session, make_admin, mint_token):
    await _grace_started(session, 1)
    newcomer = await make_admin(email="newcomer@example.com", role=AdminRoles.support)
    session.add(AdminInvites(user_id=newcomer.id, email=newcomer.email, role="support", token_hash="x" * 64,
                             expires_at=datetime.now(timezone.utc) + timedelta(days=7)))
    await session.commit()
    h = auth_header(await mint_token(newcomer))
    r = await client.get(f"{API}/admin/payouts", headers=h)
    assert r.status_code == 403 and r.json()["error_code"] == "ADMIN_2FA_REQUIRED"


async def test_turning_the_rule_off_lets_everyone_in_and_back_on_restarts_the_grace(client, session, make_admin, mint_token):
    owner = await make_admin(email="boss@example.com")
    owner.totp_enabled = True
    late = await make_admin(email="late2@example.com", role=AdminRoles.viewer)
    await _grace_started(session, 10)
    oh, lh = auth_header(await mint_token(owner)), auth_header(await mint_token(late))
    assert (await client.get(f"{API}/admin/payouts", headers=lh)).status_code == 403

    off = await client.put(f"{API}/admin/settings/admin_2fa_required", json={"value": False}, headers=oh)
    assert off.status_code == 200, off.text
    assert (await client.get(f"{API}/admin/payouts", headers=lh)).status_code == 200

    on = await client.put(f"{API}/admin/settings/admin_2fa_required", json={"value": True}, headers=oh)
    assert on.status_code == 200
    # Back on: seven days again from now.
    assert (await client.get(f"{API}/admin/payouts", headers=lh)).status_code == 200
    me = (await client.get(f"{API}/admin/ops/me", headers=lh)).json()
    assert me["two_factor"]["blocked"] is False and me["two_factor"]["grace_ends_at"]


async def test_no_recorded_start_means_the_grace_runs_from_now(client, session, make_admin, mint_token):
    """A database without the migration's row locks nobody out."""
    admin = await make_admin(email="fresh@example.com")
    h = auth_header(await mint_token(admin))
    assert (await client.get(f"{API}/admin/payouts", headers=h)).status_code == 200


async def test_app_users_and_clubs_are_untouched(client, session, make_user, make_club, mint_token):
    await _grace_started(session, 30)
    user = await make_user(email="appuser@example.com")
    club_user, _ = await make_club(name="Untouched", email="untouched@example.com")
    assert (await client.get(f"{API}/auth/me", headers=auth_header(await mint_token(user)))).status_code == 200
    assert (await client.get(f"{API}/clubs/finance", headers=auth_header(await mint_token(club_user)))).status_code == 200


async def test_a_database_the_migration_has_not_reached_refuses_nobody(client, session, make_admin, mint_token):
    """Code deployed before its migration ran: the rule can't be read, so it
    isn't enforced, and the admin's request still answers."""
    from sqlalchemy import text

    admin = await make_admin(email="early@example.com")
    await session.execute(text("ALTER TABLE platform_settings RENAME TO platform_settings_not_yet"))
    await session.commit()
    h = auth_header(await mint_token(admin))
    assert (await client.get(f"{API}/admin/payouts", headers=h)).status_code == 200
    assert (await client.get(f"{API}/clubs/admin/list", headers=h)).status_code == 200
