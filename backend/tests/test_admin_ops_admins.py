"""Admin accounts (app/api/admin_ops/admins.py): add by email (promote or
make), the set-password link, take access away; owner only, audited, never
yourself, never the last owner."""
import re

import pytest
from sqlalchemy import select

from app.api.admin_ops import admins as admins_mod
from app.api.admin_ops.models import AdminInvites
from app.api.audit.models import AdminAuditLogs
from app.api.users.models import AdminRoles, UserTypes, Users
from tests.admin_ops_support import role_headers
from tests.conftest import auth_header

API = "/api/v1"


@pytest.fixture
def mails(monkeypatch):
    sent = []
    monkeypatch.setattr(admins_mod, "send_invite_email", lambda p: sent.append(p))
    return sent


def _token(mail) -> str:
    return re.search(r"token=([\w-]+)", mail["link"]).group(1)


async def test_an_owner_adds_an_admin_who_has_no_account(client, session, make_admin, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    r = await client.post(f"{API}/admin/admins/invite", json={"email": "  New.Person@Example.com ", "full_name": "New Person", "role": "support"}, headers=h["owner"])
    assert r.status_code == 201, r.text
    assert r.json()["promoted"] is False and r.json()["email"] == "new.person@example.com"
    user = await session.scalar(select(Users).where(Users.id == r.json()["user_id"]))
    assert user.user_type == UserTypes.admin and user.admin_role == AdminRoles.support and user.password is None
    assert len(mails) == 1 and mails[0]["to"] == "new.person@example.com"
    assert mails[0]["link"].startswith("https://main-portal.myotherapp.in/accept-invite?token=")
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "admin.invite"))
    assert audit is not None and audit.details["role"] == "support"

    token = _token(mails[0])
    check = await client.get(f"{API}/admin/invites/check", params={"token": token})
    assert check.status_code == 200 and check.json()["email"] == "new.person@example.com"
    weak = await client.post(f"{API}/admin/invites/accept", json={"token": token, "password": "short"})
    assert weak.status_code == 422 and weak.json()["error_code"] == "WEAK_PASSWORD"
    ok = await client.post(f"{API}/admin/invites/accept", json={"token": token, "password": "a-long-admin-password"})
    assert ok.status_code == 200, ok.text
    again = await client.post(f"{API}/admin/invites/accept", json={"token": token, "password": "a-long-admin-password"})
    assert again.status_code == 409 and again.json()["error_code"] == "INVITE_USED"

    login = await client.post(f"{API}/auth/token", data={"username": "new.person@example.com", "password": "a-long-admin-password"})
    assert login.status_code == 200, login.text


async def test_an_app_account_is_promoted_and_a_club_account_is_refused(client, session, make_admin, make_user, make_club, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    person = await make_user(email="member@example.com")
    r = await client.post(f"{API}/admin/admins/invite", json={"email": "member@example.com", "role": "viewer"}, headers=h["owner"])
    assert r.status_code == 201 and r.json()["promoted"] is True and r.json()["user_id"] == person.id
    await session.refresh(person)
    assert person.user_type == UserTypes.admin and person.admin_role == AdminRoles.viewer
    assert mails[0]["has_password"] is True

    again = await client.post(f"{API}/admin/admins/invite", json={"email": "member@example.com", "role": "owner"}, headers=h["owner"])
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_ADMIN"

    await make_club(name="Not An Admin", email="club@example.com")
    club = await client.post(f"{API}/admin/admins/invite", json={"email": "club@example.com", "role": "viewer"}, headers=h["owner"])
    assert club.status_code == 409 and club.json()["error_code"] == "CLUB_ACCOUNT"


async def test_only_an_owner_adds_or_removes_admins(client, make_admin, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    target = await make_admin(email="target@example.com", role=AdminRoles.viewer)
    for role in ("viewer", "support"):
        assert (await client.post(f"{API}/admin/admins/invite", json={"email": "x@example.com"}, headers=h[role])).status_code == 403
        assert (await client.delete(f"{API}/admin/admins/{target.id}", headers=h[role])).status_code == 403
    listed = await client.get(f"{API}/admin/admins", headers=h["viewer"])
    assert listed.status_code == 200 and listed.json()["can_manage"] is False
    assert any(a["email"] == "target@example.com" for a in listed.json()["items"])


async def test_removing_an_admin_ends_their_access(client, session, make_admin, mint_token, mails):
    h = await role_headers(make_admin, mint_token)
    target = await make_admin(email="leaving@example.com", role=AdminRoles.support)
    th = auth_header(await mint_token(target))
    assert (await client.get(f"{API}/admin/payouts", headers=th)).status_code == 200
    r = await client.delete(f"{API}/admin/admins/{target.id}", headers=h["owner"])
    assert r.status_code == 200, r.text
    await session.refresh(target)
    assert target.user_type == UserTypes.app_user
    # The token they held is dead.
    assert (await client.get(f"{API}/admin/payouts", headers=th)).status_code == 401
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "admin.remove"))
    assert audit is not None and audit.entity_id == str(target.id)


async def test_nobody_removes_themselves_or_the_last_owner(client, session, make_admin, mint_token):
    owner = await make_admin(email="only-owner@example.com", role=AdminRoles.owner)
    oh = auth_header(await mint_token(owner))
    me = await client.delete(f"{API}/admin/admins/{owner.id}", headers=oh)
    assert me.status_code == 409 and me.json()["error_code"] == "CANNOT_REMOVE_SELF"

    other = await make_admin(email="other-owner@example.com", role=AdminRoles.owner)
    # Two owners: one may remove the other.
    assert (await client.delete(f"{API}/admin/admins/{other.id}", headers=oh)).status_code == 200
    # Now the remaining owner is the last; a second owner can't be made to remove them by anyone but themselves.
    third = await make_admin(email="third-owner@example.com", role=AdminRoles.owner)
    owner.is_active = False
    await session.commit()
    th = auth_header(await mint_token(third))
    last = await client.delete(f"{API}/admin/admins/{third.id}", headers=th)
    assert last.status_code == 409


async def test_last_owner_is_kept(client, session, make_admin, mint_token):
    a = await make_admin(email="owner-a@example.com", role=AdminRoles.owner)
    b = await make_admin(email="owner-b@example.com", role=AdminRoles.owner)
    a.is_active = True
    b.is_active = False  # suspended: not an owner who can act
    await session.commit()
    c = await make_admin(email="owner-c@example.com", role=AdminRoles.owner)
    ch = auth_header(await mint_token(c))
    # Removing a leaves c as the only active owner: allowed.
    assert (await client.delete(f"{API}/admin/admins/{a.id}", headers=ch)).status_code == 200
    # c can't remove itself; nobody else active is an owner, so c stays.
    assert (await client.delete(f"{API}/admin/admins/{c.id}", headers=ch)).status_code == 409


async def test_a_new_link_replaces_the_old_and_an_expired_link_is_refused(client, session, make_admin, mint_token, mails):
    from datetime import datetime, timedelta, timezone

    h = await role_headers(make_admin, mint_token)
    r = await client.post(f"{API}/admin/admins/invite", json={"email": "slow@example.com", "role": "viewer"}, headers=h["owner"])
    first = _token(mails[0])
    resend = await client.post(f"{API}/admin/admins/{r.json()['user_id']}/resend-invite", headers=h["owner"])
    assert resend.status_code == 200
    second = _token(mails[1])
    assert (await client.get(f"{API}/admin/invites/check", params={"token": first})).status_code == 404
    invite = await session.scalar(select(AdminInvites).where(AdminInvites.revoked_at.is_(None), AdminInvites.email == "slow@example.com"))
    invite.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    expired = await client.post(f"{API}/admin/invites/accept", json={"token": second, "password": "long-enough-pass"})
    assert expired.status_code == 410 and expired.json()["error_code"] == "INVITE_EXPIRED"


def test_the_invite_mail_says_who_and_links_to_the_portal(monkeypatch):
    from app.core.email import email as email_mod

    sent = {}
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.update(kw))
    admins_mod.send_invite_email({"to": "a@b.com", "name": "Asha", "link": "https://portal/accept-invite?token=t", "by": "Owner",
                                  "role": "support", "has_password": False})
    assert sent["recipients"] == ["a@b.com"]
    assert "https://portal/accept-invite?token=t" in sent["text"] and "support" in sent["text"]
    assert "two-factor" in sent["text"]
