"""The do-not-mail list (app/api/admin_ops/suppressions.py) and the platform
number's WhatsApp templates (app/api/admin_ops/whatsapp_templates.py)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.api.admin_ops import whatsapp_templates
from app.api.audit.models import AdminAuditLogs
from app.api.club_whatsapp import graph
from app.config import settings
from app.core.mail.suppression import EmailSuppressions
from tests.admin_ops_support import role_headers

API = "/api/v1"


@pytest.fixture
async def listed(session):
    now = datetime.now(timezone.utc)
    session.add_all([
        EmailSuppressions(email="gone@example.com", reason="bounce", detail="550 mailbox unavailable", created_at=now - timedelta(days=2)),
        EmailSuppressions(email="angry@example.org", reason="complaint", detail="abuse report", created_at=now - timedelta(days=40)),
    ])
    await session.commit()


async def test_the_list_searches_and_filters(client, make_admin, mint_token, listed):
    h = await role_headers(make_admin, mint_token)
    r = await client.get(f"{API}/admin/mail/suppressions", headers=h["viewer"])
    assert r.status_code == 200, r.text
    assert [i["email"] for i in r.json()["items"]] == ["gone@example.com", "angry@example.org"]
    assert r.json()["counts"] == {"bounce": 1, "complaint": 1, "last_30_days": 1}
    found = (await client.get(f"{API}/admin/mail/suppressions", params={"q": "ANGRY"}, headers=h["viewer"])).json()
    assert [i["email"] for i in found["items"]] == ["angry@example.org"]
    bounces = (await client.get(f"{API}/admin/mail/suppressions", params={"reason": "bounce"}, headers=h["viewer"])).json()
    assert bounces["total"] == 1 and bounces["items"][0]["detail"] == "550 mailbox unavailable"


async def test_unblocking_is_for_moderators_and_audited(client, session, make_admin, mint_token, listed):
    h = await role_headers(make_admin, mint_token)
    assert (await client.delete(f"{API}/admin/mail/suppressions/gone@example.com", headers=h["viewer"])).status_code == 403
    r = await client.delete(f"{API}/admin/mail/suppressions/Gone@Example.com", headers=h["support"])
    assert r.status_code == 200 and r.json() == {"email": "gone@example.com", "removed": True}
    assert await session.get(EmailSuppressions, "gone@example.com") is None
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "mail.unsuppress"))
    assert audit is not None and audit.details["reason"] == "bounce"
    missing = await client.delete(f"{API}/admin/mail/suppressions/gone@example.com", headers=h["support"])
    assert missing.status_code == 404


# ─── WhatsApp templates ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_template_cache():
    whatsapp_templates.reset_for_tests()
    yield
    whatsapp_templates.reset_for_tests()


async def test_without_the_platform_number_the_page_says_so(client, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    h = await role_headers(make_admin, mint_token)
    r = await client.get(f"{API}/admin/whatsapp/templates", headers=h["viewer"])
    assert r.status_code == 200 and r.json()["configured"] is False
    assert {t["name"] for t in r.json()["missing"]} >= {"ticket_confirmed", "event_reminder"}


def _meta(calls: list, *, waba_known: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"is_valid": True, "granular_scopes": [
                {"scope": "whatsapp_business_messaging", "target_ids": ["999"]},
                {"scope": "whatsapp_business_management", "target_ids": ["4242"]},
            ]}})
        if request.url.path.endswith("/message_templates"):
            return httpx.Response(200, json={"data": [
                {"id": "1", "name": "ticket_confirmed", "language": "en", "status": "APPROVED", "category": "UTILITY",
                 "components": [{"type": "BODY", "text": "Hi {{1}}, your ticket"}], "quality_score": {"score": "GREEN"}},
                {"id": "2", "name": "certificate_ready", "language": "en", "status": "REJECTED", "category": "UTILITY",
                 "rejected_reason": "INVALID_FORMAT", "components": []},
                {"id": "3", "name": "event_reminder", "language": "en", "status": "PENDING", "category": "UTILITY", "components": []},
            ], "paging": {}})
        return httpx.Response(404, json={"error": {"message": "unexpected", "code": 100}})

    return handler


async def test_templates_are_read_from_meta_with_their_verdicts_and_cached(client, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "EAAtoken")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "1234")
    monkeypatch.setattr(settings, "WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    calls: list = []
    monkeypatch.setattr(graph, "_client", lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(_meta(calls, waba_known=False))))
    h = await role_headers(make_admin, mint_token)
    r = await client.get(f"{API}/admin/whatsapp/templates", headers=h["viewer"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["waba_id"] == "4242" and body["cached"] is False
    by = {t["name"]: t for t in body["templates"]}
    assert by["ticket_confirmed"]["status"] == "approved" and by["ticket_confirmed"]["quality"] == "GREEN"
    assert by["certificate_ready"]["status"] == "rejected" and by["certificate_ready"]["rejected_reason"] == "INVALID_FORMAT"
    assert {m["name"] for m in body["missing"]} == {"payment_pending", "event_update"}
    asked = len(calls)
    again = (await client.get(f"{API}/admin/whatsapp/templates", headers=h["viewer"])).json()
    assert again["cached"] is True and len(calls) == asked
    assert "EAAtoken" not in r.text


async def test_meta_being_down_says_so(client, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "EAAtoken")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "1234")
    monkeypatch.setattr(settings, "WHATSAPP_BUSINESS_ACCOUNT_ID", "4242")
    h = await role_headers(make_admin, mint_token)
    # The suite's own stand-in: Meta unreachable.
    r = await client.get(f"{API}/admin/whatsapp/templates", headers=h["viewer"])
    assert r.status_code == 502 and r.json()["error_code"] == "META_UNREACHABLE"
