"""Runtime platform settings (app/api/admin_ops/runtime.py, settings_router.py):
stored in the database, laid over the env value in the process, audited,
owner-only to change; secrets shown as set or not, never their value."""
import pytest

from app.api.admin_ops import runtime
from app.api.audit.models import AdminAuditLogs
from app.config import settings
from sqlalchemy import select
from tests.admin_ops_support import role_headers

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_runtime():
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


async def test_every_admin_reads_the_settings_and_no_secret_value_leaves(client, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_supersecretvalue123")
    h = await role_headers(make_admin, mint_token)
    r = await client.get(f"{API}/admin/settings", headers=h["viewer"])
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {s["key"] for s in body["settings"]}
    assert {"AI_ENABLED", "MCP_ENABLED", "MAIL_PROVIDER", "PUSH_TOPICS", "LEDGER_READS",
            "maintenance_banner", "admin_2fa_required"} <= keys
    # Retired by prepaid credits (read by nothing), so not offered as a switch.
    assert "MESSAGE_OVERAGE_BILLING" not in keys
    assert all(s["restart_required"] is False for s in body["settings"])
    assert body["can_edit"] is False
    resend = next(s for s in body["secrets"] if s["name"] == "APP_RESEND_API_KEY")
    assert resend["set"] is True
    assert "re_supersecretvalue123" not in r.text
    assert "test-razorpay-key-secret" not in r.text


async def test_only_an_owner_changes_a_setting(client, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    for role in ("viewer", "support"):
        r = await client.put(f"{API}/admin/settings/AI_ENABLED", json={"value": True}, headers=h[role])
        assert r.status_code == 403, r.text


async def test_a_stored_value_stands_in_for_the_env_value_and_reset_puts_it_back(client, session, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    before = settings.AI_ENABLED
    r = await client.put(f"{API}/admin/settings/AI_ENABLED", json={"value": (not before)}, headers=h["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["value"] is (not before) and r.json()["overridden"] is True
    assert settings.AI_ENABLED is (not before)
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "platform_setting.update").order_by(AdminAuditLogs.id))).all()
    assert audit and audit[-1].entity_id == "AI_ENABLED" and audit[-1].details == {"from": before, "to": not before}

    listed = (await client.get(f"{API}/admin/settings", headers=h["owner"])).json()
    ai = next(s for s in listed["settings"] if s["key"] == "AI_ENABLED")
    assert ai["value"] is (not before) and ai["server_value"] is before
    assert listed["history"][0]["key"] == "AI_ENABLED"

    r = await client.delete(f"{API}/admin/settings/AI_ENABLED", headers=h["owner"])
    assert r.status_code == 200, r.text
    assert settings.AI_ENABLED is before
    assert r.json()["overridden"] is False


async def test_mail_provider_takes_only_a_known_provider(client, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    bad = await client.put(f"{API}/admin/settings/MAIL_PROVIDER", json={"value": "mailgun"}, headers=h["owner"])
    assert bad.status_code == 422
    good = await client.put(f"{API}/admin/settings/MAIL_PROVIDER", json={"value": "resend"}, headers=h["owner"])
    assert good.status_code == 200 and settings.MAIL_PROVIDER == "resend"


async def test_ses_over_smtp_is_a_provider_and_its_password_never_leaves(client, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "email-smtp.ap-south-1.amazonaws.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "smtp-user-for-tests")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "smtp-password-for-tests-0123")
    h = await role_headers(make_admin, mint_token)
    r = await client.put(f"{API}/admin/settings/MAIL_PROVIDER", json={"value": "ses_smtp"}, headers=h["owner"])
    assert r.status_code == 200 and settings.MAIL_PROVIDER == "ses_smtp"
    listed = await client.get(f"{API}/admin/settings", headers=h["owner"])
    secrets = {s["name"]: s for s in listed.json()["secrets"]}
    assert secrets["APP_SMTP_PASSWORD"]["set"] is True and secrets["APP_SMTP_PASSWORD"]["mode"] == "in use"
    assert secrets["APP_SMTP_HOST"]["set"] is True and secrets["APP_SES_ACCESS_KEY"]["mode"] == "not in use"
    assert "smtp-password-for-tests-0123" not in listed.text and "smtp-user-for-tests" not in listed.text


async def test_an_unknown_key_is_not_a_setting(client, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    r = await client.put(f"{API}/admin/settings/SECRET_KEY", json={"value": "x"}, headers=h["owner"])
    assert r.status_code == 404 and r.json()["error_code"] == "UNKNOWN_SETTING"


async def test_the_refresh_follows_another_process_changing_a_setting(session):
    """A row written by another process is applied on the next read, and its
    removal puts the env value back."""
    before = settings.LEDGER_READS
    await runtime.write(session, "LEDGER_READS", not before, actor_id=None)
    await runtime.load(session)
    assert settings.LEDGER_READS is (not before)
    await runtime.remove(session, "LEDGER_READS")
    await runtime.load(session)
    assert settings.LEDGER_READS is before


async def test_the_maintenance_banner_is_public_and_shown_in_ops_me(client, make_admin, mint_token):
    h = await role_headers(make_admin, mint_token)
    assert (await client.get(f"{API}/platform/banner")).json() == {"message": None}
    r = await client.put(f"{API}/admin/settings/maintenance_banner", json={"value": "  Payments  pause at 10pm  "}, headers=h["owner"])
    assert r.status_code == 200 and r.json()["value"] == "Payments pause at 10pm"
    assert (await client.get(f"{API}/platform/banner")).json() == {"message": "Payments pause at 10pm"}
    me = (await client.get(f"{API}/admin/ops/me", headers=h["viewer"])).json()
    assert me["maintenance_banner"] == "Payments pause at 10pm"
    assert "payouts" in me["features"] and me["role"] == "viewer"
    too_long = await client.put(f"{API}/admin/settings/maintenance_banner", json={"value": "x" * 301}, headers=h["owner"])
    assert too_long.status_code == 422


async def test_ops_me_refuses_anyone_but_an_admin(client, make_user, mint_token):
    from tests.conftest import auth_header

    user = await make_user(email="plain@example.com")
    r = await client.get(f"{API}/admin/ops/me", headers=auth_header(await mint_token(user)))
    assert r.status_code == 403
