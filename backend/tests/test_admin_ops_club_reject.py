"""Rejecting a club's application with a reason, and telling the applicant
(app/api/clubs/router.py, app/api/admin_ops/club_reject.py)."""
import pytest
from sqlalchemy import select

from app.api.admin_ops import club_reject
from app.api.audit.models import AdminAuditLogs
from app.api.clubs.models import Clubs
from tests.admin_ops_support import role_headers

API = "/api/v1"


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(club_reject, "send_rejected_email", lambda to, **kw: sent.append({"to": to, **kw}))
    return sent


async def test_a_moderator_rejects_a_pending_club_with_a_reason_and_the_applicant_is_told(client, session, make_admin, make_club, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    _, club = await make_club(name="Hopeful", email="hopeful@example.com", is_verified=False)
    assert (await client.post(f"{API}/clubs/{club.id}/reject", json={"reason": "x"}, headers=h["viewer"])).status_code == 403
    r = await client.post(f"{API}/clubs/{club.id}/reject", json={"reason": "We couldn't verify the college"}, headers=h["support"])
    assert r.status_code == 200, r.text
    deleted = await session.scalar(select(Clubs).where(Clubs.id == club.id).execution_options(include_deleted=True))
    assert deleted.is_deleted is True
    assert mails == [{"to": "hopeful@example.com", "club_name": "Hopeful", "reason": "We couldn't verify the college"}]
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club.reject"))
    assert audit.details == {"reason": "We couldn't verify the college", "applicant_mailed": True, "was_approved": False}


async def test_rejecting_without_a_body_still_works(client, make_admin, make_club, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    _, club = await make_club(name="Quiet", email="quiet@example.com", is_verified=False)
    r = await client.post(f"{API}/clubs/{club.id}/reject", headers=h["owner"])
    assert r.status_code == 200
    assert mails[0]["reason"] is None


async def test_an_approved_club_needs_destroy_and_is_not_mailed(client, make_admin, make_club, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    _, club = await make_club(name="Established", email="established@example.com", is_verified=True)
    assert (await client.post(f"{API}/clubs/{club.id}/reject", json={"reason": "spam"}, headers=h["support"])).status_code == 403
    assert (await client.post(f"{API}/clubs/{club.id}/reject", json={"reason": "spam"}, headers=h["owner"])).status_code == 200
    assert mails == []


def test_the_mail_gives_the_reason(monkeypatch):
    from app.core.email import email as email_mod

    sent = {}
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.update(kw))
    club_reject.send_rejected_email("a@b.com", club_name="Robotics <Club>", reason="Not a student body")
    assert sent["recipients"] == ["a@b.com"] and "Not a student body" in sent["text"]
    assert "Robotics &lt;Club&gt;" in sent["html"]
