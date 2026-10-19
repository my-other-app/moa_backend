"""Club team access: the owner invites people by email with a role, they set a
password from the link and sign in to the club's dashboard as themselves, the
role holds on every request, and taking access away ends their sessions now."""
import re

import pytest

from app.api.club_access import policy
from app.api.security import service as security
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_user, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    owner, club = await make_club(name="Circuit Society", email="circuit@example.com")
    other_owner, other_club = await make_club(name="Robotics Guild", email="robotics@example.com")
    await session.commit()
    return dict(owner=owner, club=club, other=other_club, sent=sent, h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)))


def _token_from(mail: dict) -> str:
    return re.search(r"token=([A-Za-z0-9_\-]+)", mail["text"]).group(1)


async def _invite(client, world, email, role, name=None):
    r = await client.post("/api/v1/clubs/access/invite", json={"email": email, "role": role, "full_name": name}, headers=world["h"])
    assert r.status_code == 200, r.text
    return r.json(), _token_from(world["sent"][-1])


async def _join(client, token, password="Member-pass-1", name=None):
    r = await client.post(f"/api/v1/clubs/access/invite/{token}/accept", json={"password": password, "full_name": name})
    assert r.status_code == 200, r.text
    return r.json()


async def _sign_in(client, email, password="Member-pass-1", scope="club"):
    data = {"username": email, "password": password}
    if scope:
        data["scope"] = scope
    return await client.post("/api/v1/auth/token", data=data)


async def _member_session(client, world, email, role, name=None):
    _, token = await _invite(client, world, email, role, name)
    await _join(client, token)
    r = await _sign_in(client, email)
    assert r.status_code == 200, r.text
    return auth_header(r.json()["access_token"]), r.json()["refresh_token"]


async def test_an_invite_is_mailed_previewed_accepted_and_opens_the_clubs_dashboard(client, world):
    body, token = await _invite(client, world, "Priya@Example.com", "editor", "Priya")
    assert body["member"]["email"] == "priya@example.com" and body["member"]["status"] == "invited" and body["member"]["role_label"] == "Editor"
    assert body["invite_url"].endswith(f"/join?token={token}")
    mail = world["sent"][-1]
    assert mail["recipients"] == ["priya@example.com"] and "Circuit Society" in mail["subject"] and "Editor" in mail["text"]

    preview = (await client.get(f"/api/v1/clubs/access/invite/{token}")).json()
    assert preview["club_name"] == "Circuit Society" and preview["role"] == "editor" and preview["expired"] is False

    # A short password is refused and the invite still stands.
    weak = await client.post(f"/api/v1/clubs/access/invite/{token}/accept", json={"password": "short"})
    assert weak.status_code == 422 and weak.json()["error_code"] == "WEAK_PASSWORD"
    joined = await _join(client, token, name="Priya Nair")
    assert joined == {"email": "priya@example.com", "club_name": "Circuit Society"}
    # The link is spent.
    assert (await client.get(f"/api/v1/clubs/access/invite/{token}")).status_code == 404

    signed = await _sign_in(client, "priya@example.com")
    assert signed.status_code == 200
    h = auth_header(signed.json()["access_token"])
    me = (await client.get("/api/v1/clubs/access/me", headers=h)).json()
    assert me["role"] == "editor" and me["club_name"] == "Circuit Society" and me["member"]["full_name"] == "Priya Nair"
    assert me["can"]["money"] is False and me["can"]["edit_events"] is True and me["can"]["manage_access"] is False
    # The session acts as the club: club routes answer for Circuit Society.
    assert (await client.get("/api/v1/clubs/site", headers=h)).json()["url"] == f"https://{world['club'].slug}.myotherapp.in"

    owner_view = (await client.get("/api/v1/clubs/access", headers=world["h"])).json()
    assert owner_view["can_manage"] is True and [m["email"] for m in owner_view["members"]] == ["priya@example.com"]
    assert owner_view["members"][0]["status"] == "active" and owner_view["members"][0]["last_seen_at"]
    owner_me = (await client.get("/api/v1/clubs/access/me", headers=world["h"])).json()
    assert owner_me["role"] == "owner" and owner_me["member"] is None and owner_me["can"]["manage_access"] is True


async def test_a_members_password_works_only_where_the_dashboard_asks_for_a_club(client, world):
    await _member_session(client, world, "dev@example.com", "editor")
    assert (await _sign_in(client, "dev@example.com", scope=None)).status_code == 401
    assert (await _sign_in(client, "dev@example.com", password="wrong-password")).status_code == 401
    # The club's own account still signs in the same way, with or without the scope.
    assert (await _sign_in(client, "circuit@example.com", password="Password123!")).status_code == 200


async def test_an_editor_runs_events_but_not_money_profile_access_or_the_owners_account(client, world):
    h, _ = await _member_session(client, world, "editor@example.com", "editor")
    assert (await client.get("/api/v1/clubs/events/figures", headers=h)).status_code == 200
    made = await client.post("/api/v1/clubs/links", json={"name": "Poster", "utm_source": "poster", "utm_medium": "print"}, headers=h)
    assert made.status_code in (200, 201), made.text

    for method, path, body in [
        ("GET", "/api/v1/clubs/finance", None),
        ("PUT", "/api/v1/clubs/update", {"name": "Taken"}),
        ("POST", "/api/v1/clubs/access/invite", {"email": "x@example.com", "role": "door"}),
        ("GET", "/api/v1/clubs/access", None),
        ("GET", "/api/v1/auth/security/overview", None),
        ("POST", "/api/v1/auth/change-password", {"old_password": "Password123!", "new_password": "Hijacked-1"}),
    ]:
        r = await client.request(method, path, json=body, headers=h)
        assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", (method, path, r.status_code, r.text)
    # The owner's password was not touched.
    assert (await _sign_in(client, "circuit@example.com", password="Password123!")).status_code == 200


async def test_door_staff_look_and_let_people_in_but_change_nothing_else(client, world):
    h, _ = await _member_session(client, world, "door@example.com", "door")
    assert (await client.get("/api/v1/clubs/events/figures", headers=h)).status_code == 200
    refused = await client.post("/api/v1/clubs/links", json={"name": "Poster", "utm_source": "poster", "utm_medium": "print"}, headers=h)
    assert refused.status_code == 403 and "Door" in refused.json()["message"]
    assert (await client.get("/api/v1/clubs/finance", headers=h)).status_code == 403


async def test_an_admin_reads_the_access_list_but_only_the_owner_changes_it(client, world):
    h, _ = await _member_session(client, world, "admin@example.com", "admin")
    listed = await client.get("/api/v1/clubs/access", headers=h)
    assert listed.status_code == 200 and listed.json()["can_manage"] is False
    assert (await client.get("/api/v1/clubs/finance", headers=h)).status_code == 200
    r = await client.post("/api/v1/clubs/access/invite", json={"email": "x@example.com", "role": "door"}, headers=h)
    assert r.status_code == 403


async def test_a_role_change_holds_on_the_next_request_and_removal_ends_the_session_now(client, world):
    h, refresh = await _member_session(client, world, "temp@example.com", "admin")
    member_id = (await client.get("/api/v1/clubs/access", headers=world["h"])).json()["members"][0]["id"]
    assert (await client.get("/api/v1/clubs/finance", headers=h)).status_code == 200

    demoted = await client.patch(f"/api/v1/clubs/access/{member_id}", json={"role": "door"}, headers=world["h"])
    assert demoted.status_code == 200 and demoted.json()["role"] == "door"
    assert (await client.get("/api/v1/clubs/finance", headers=h)).status_code == 403

    assert (await client.delete(f"/api/v1/clubs/access/{member_id}", headers=world["h"])).status_code == 200
    gone = await client.get("/api/v1/clubs/events/figures", headers=h)
    assert gone.status_code == 401
    client.cookies.clear()
    assert (await client.post("/api/v1/auth/refresh", data={"token": refresh})).status_code == 401
    assert (await _sign_in(client, "temp@example.com")).status_code == 401
    assert (await client.get("/api/v1/clubs/access", headers=world["h"])).json()["members"] == []


async def test_refresh_keeps_the_member_behind_the_session(client, world):
    _, refresh = await _member_session(client, world, "keep@example.com", "door")
    client.cookies.clear()
    renewed = await client.post("/api/v1/auth/refresh", data={"token": refresh})
    assert renewed.status_code == 200
    me = (await client.get("/api/v1/clubs/access/me", headers=auth_header(renewed.json()["access_token"]))).json()
    assert me["role"] == "door" and me["member"]["email"] == "keep@example.com"


async def test_a_member_changes_their_own_password_and_the_owners_stays(client, world):
    h, _ = await _member_session(client, world, "self@example.com", "editor")
    wrong = await client.post("/api/v1/clubs/access/me/password", json={"current_password": "nope", "new_password": "Another-pass-2"}, headers=h)
    assert wrong.status_code == 400
    ok = await client.post("/api/v1/clubs/access/me/password", json={"current_password": "Member-pass-1", "new_password": "Another-pass-2"}, headers=h)
    assert ok.status_code == 200
    assert (await _sign_in(client, "self@example.com", password="Another-pass-2")).status_code == 200
    not_member = await client.post("/api/v1/clubs/access/me/password", json={"current_password": "Password123!", "new_password": "Another-pass-2"}, headers=world["h"])
    assert not_member.status_code == 400 and not_member.json()["error_code"] == "NOT_A_MEMBER"


async def test_invites_refuse_duplicates_club_logins_bad_roles_and_expired_links(client, world, session):
    await _invite(client, world, "one@example.com", "door")
    again = await client.post("/api/v1/clubs/access/invite", json={"email": "ONE@example.com", "role": "editor"}, headers=world["h"])
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_MEMBER"
    elsewhere = await client.post("/api/v1/clubs/access/invite", json={"email": "one@example.com", "role": "editor"}, headers=world["other_h"])
    assert elsewhere.status_code == 409 and elsewhere.json()["error_code"] == "MEMBER_ELSEWHERE"
    club_login = await client.post("/api/v1/clubs/access/invite", json={"email": "robotics@example.com", "role": "editor"}, headers=world["h"])
    assert club_login.status_code == 409 and club_login.json()["error_code"] == "CLUB_LOGIN_EMAIL"
    bad = await client.post("/api/v1/clubs/access/invite", json={"email": "two@example.com", "role": "owner"}, headers=world["h"])
    assert bad.status_code == 422
    assert (await client.post("/api/v1/clubs/access/invite", json={"email": "not-an-email", "role": "door"}, headers=world["h"])).status_code == 422

    # Another club cannot touch this club's list.
    member_id = (await client.get("/api/v1/clubs/access", headers=world["h"])).json()["members"][0]["id"]
    assert (await client.delete(f"/api/v1/clubs/access/{member_id}", headers=world["other_h"])).status_code == 404

    # An expired link says so; a resend issues a fresh one that works.
    from datetime import datetime, timedelta, timezone

    from app.api.club_access.models import ClubMembers

    old_token = _token_from(world["sent"][-1])
    m = await session.get(ClubMembers, member_id)
    m.invite_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    assert (await client.get(f"/api/v1/clubs/access/invite/{old_token}")).json()["expired"] is True
    assert (await client.post(f"/api/v1/clubs/access/invite/{old_token}/accept", json={"password": "Member-pass-1"})).status_code == 410
    resent = await client.post(f"/api/v1/clubs/access/{member_id}/resend", headers=world["h"])
    assert resent.status_code == 200
    fresh = _token_from(world["sent"][-1])
    assert fresh != old_token and (await client.get(f"/api/v1/clubs/access/invite/{old_token}")).status_code == 404
    await _join(client, fresh)
    assert (await _sign_in(client, "one@example.com")).status_code == 200


async def test_the_owner_sees_who_is_signed_in_on_each_device(client, world):
    await _member_session(client, world, "seen@example.com", "editor", name="Seen Person")
    overview = (await client.get("/api/v1/auth/security/overview", headers=world["h"])).json()
    assert "Seen Person" in [s.get("who") for s in overview["sessions"]]


@pytest.mark.parametrize("role,method,path,allowed", [
    ("admin", "GET", "/api/v1/clubs/finance", True),
    ("admin", "DELETE", "/api/v1/events/delete/7", True),
    ("admin", "GET", "/api/v1/clubs/access", True),
    ("admin", "POST", "/api/v1/clubs/access/invite", False),
    ("admin", "PATCH", "/api/v1/clubs/access/3", False),
    ("admin", "POST", "/api/v1/auth/change-password", False),
    ("admin", "GET", "/api/v1/auth/sessions/", False),
    ("admin", "POST", "/api/v1/clubs/subscription/start", False),
    ("admin", "GET", "/api/v1/clubs/access/me", True),
    ("editor", "GET", "/api/v1/events/12/finance", False),
    ("editor", "POST", "/api/v1/payments/logs/5/refund", False),
    ("editor", "PUT", "/api/v1/clubs/update", False),
    ("editor", "PATCH", "/api/v1/clubs/partners/2", False),
    ("editor", "DELETE", "/api/v1/clubs/team/2", False),
    ("editor", "DELETE", "/api/v1/events/delete/7", False),
    ("editor", "POST", "/api/v1/events/7/closeout/retention/run", False),
    ("editor", "PUT", "/api/v1/events/update/7", True),
    ("editor", "POST", "/api/v1/events/7/emails/3/send", True),
    ("editor", "PATCH", "/api/v1/clubs/site", True),
    ("editor", "GET", "/api/v1/clubs/team", True),
    ("door", "GET", "/api/v1/events/registration/7/attendees", True),
    ("door", "POST", "/api/v1/events/7/gate/scan", True),
    ("door", "POST", "/api/v1/events/7/gate/check-in/44", True),
    ("door", "DELETE", "/api/v1/events/7/gate/check-in/44", True),
    ("door", "POST", "/api/v1/events/registration/7/attendance/44", True),
    # Handout items are uuids: a number never named one, and door staff could not hand swag out by hand.
    ("door", "POST", "/api/v1/events/7/merch/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40/handout", True),
    ("door", "POST", "/api/v1/events/7/merch/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40/handout/undo", True),
    ("door", "PATCH", "/api/v1/events/7/merch/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", False),
    ("door", "POST", "/api/v1/events/7/merch/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40/handout/../../x", False),
    # The club's store: editors and admins run it, door staff only read it.
    ("editor", "GET", "/api/v1/clubs/store", True),
    ("editor", "POST", "/api/v1/clubs/store", True),
    ("editor", "POST", "/api/v1/clubs/store/photo", True),
    ("editor", "PATCH", "/api/v1/clubs/store/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", True),
    ("editor", "DELETE", "/api/v1/clubs/store/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", True),
    ("admin", "DELETE", "/api/v1/clubs/store/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", True),
    ("door", "GET", "/api/v1/clubs/store", True),
    ("door", "POST", "/api/v1/clubs/store", False),
    ("door", "POST", "/api/v1/clubs/store/photo", False),
    ("door", "PATCH", "/api/v1/clubs/store/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", False),
    ("door", "DELETE", "/api/v1/clubs/store/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40", False),
    ("nobody", "GET", "/api/v1/clubs/store", False),
    ("editor", "GET", "/api/v1/events/7/store/offers", True),
    ("editor", "PUT", "/api/v1/events/7/store/offers", True),
    ("door", "GET", "/api/v1/events/7/store/offers", True),
    ("door", "PUT", "/api/v1/events/7/store/offers", False),
    ("door", "POST", "/api/v1/events/7/store/orders/ab-12/handover/undo", True),
    ("door", "POST", "/api/v1/events/7/tickets/44/send", True),
    ("door", "POST", "/api/v1/events/7/tickets/send", False),
    ("door", "PUT", "/api/v1/events/update/7", False),
    ("door", "POST", "/api/v1/events/7/doors", False),
    ("door", "GET", "/api/v1/clubs/finance/statement.csv", False),
    ("door", "POST", "/api/v1/clubs/access/me/password", True),
    ("nobody", "GET", "/api/v1/clubs/events", False),
])
def test_the_role_rules(role, method, path, allowed):
    assert policy.allows(role, method, path) is allowed


def test_what_the_dashboard_offers_matches_the_rules():
    for role in ("admin", "editor", "door"):
        can = policy.can(role)
        assert can["money"] == policy.allows(role, "GET", "/api/v1/clubs/finance")
        assert can["club_profile"] == policy.allows(role, "PUT", "/api/v1/clubs/update")
        assert can["edit_events"] == policy.allows(role, "PUT", "/api/v1/events/update/1")
        assert can["club_store"] == can["edit_events"] == policy.allows(role, "POST", "/api/v1/clubs/store")
        assert can["delete_events"] == policy.allows(role, "DELETE", "/api/v1/events/delete/1")
        assert can["see_access"] == policy.allows(role, "GET", "/api/v1/clubs/access")
        assert can["manage_access"] == policy.allows(role, "POST", "/api/v1/clubs/access/invite")
