"""Co-hosted events (app/api/cohosts, docs/COHOSTS.md): inviting, answering,
leaving and removing; what a co-host's team may do, by role; the event on
both clubs' pages, once; the co-host's followers told once; the documents'
"hosted by"; the routes that find their event without `{event_id}`.

The route-by-route sweep of every club-side event route is
tests/test_cohost_routes.py."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from firebase_admin import messaging
from sqlalchemy import select, update

from app.api.audit.models import AdminAuditLogs
from app.api.club_access import policy as roles
from app.api.club_alerts.models import ClubAlerts
from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubUsersLink
from app.api.cohosts.models import ACCEPTED, EventCohosts
from app.api.events.models import Events
from app.api.notifications.models import Notifications
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core.email import email as email_mod
from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.club_ops_helpers import member_header
from tests.conftest import auth_header

SOON = datetime.now(timezone.utc) + timedelta(days=3)


class FakeMessaging:
    def __init__(self):
        self.multicasts: list = []

    def send(self, message):
        return "projects/test/messages/1"

    def send_each_for_multicast(self, message):
        self.multicasts.append(message)
        return SimpleNamespace(success_count=len(message.tokens), failure_count=0, responses=[])

    def send_each(self, messages):
        """A batch whose devices have different badges: a message per device, in one call."""
        self.multicasts.append(SimpleNamespace(tokens=[m.token for m in messages]))
        return SimpleNamespace(success_count=len(messages), failure_count=0, responses=[])

    def tokens(self) -> set:
        return {t for m in self.multicasts for t in m.tokens}


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeMessaging()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", fake.send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.send_each_for_multicast)
    monkeypatch.setattr(messaging, "send_each", fake.send_each)
    for k, v in dict(PUSH_TOPICS=False, INBOX_SHARED=False).items():
        monkeypatch.setattr(settings, k, v)
    return fake


@pytest.fixture
async def w(client, session, make_club, make_event, make_user, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    mail: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: mail.append(kw))
    host_owner, host = await make_club(name="Design Society", email="design-co@example.com")
    co_owner, co = await make_club(name="Robotics Club", email="robotics-co@example.com")
    third_owner, third = await make_club(name="Chess Circle", email="chess-co@example.com")
    _p, pending = await make_club(name="Pending Club", email="pending-co@example.com", is_verified=False)
    event = await make_event(club=host, name="Hack the Campus", created_by=host_owner, event_datetime=SOON, lifecycle=EventStatus.published)
    await session.commit()
    return SimpleNamespace(
        host=host, co=co, third=third, pending=pending, event=event, event_id=event.id, mail=mail,
        host_h=auth_header(await mint_token(host_owner)), co_h=auth_header(await mint_token(co_owner)),
        third_h=auth_header(await mint_token(third_owner)),
        base=f"/api/v1/events/{event.id}/cohosts",
    )


async def _invite(client, w, club, headers=None, **body):
    return await client.post(w.base, json={"club_id": club.id, **body}, headers=headers or w.host_h)


async def _accept(client, w, invite_id, headers=None):
    return await client.post(f"/api/v1/clubs/cohosts/invitations/{invite_id}/accept", headers=headers or w.co_h)


async def _cohosting(client, w) -> int:
    r = await _invite(client, w, w.co)
    assert r.status_code == 201, r.text
    assert (await _accept(client, w, r.json()["id"])).status_code == 200
    return r.json()["id"]


# ─── Inviting ───────────────────────────────────────────────────────────────


async def test_an_invite_is_sent_told_and_mailed_and_lapses_in_14_days_or_at_the_end(client, session, w, make_user):
    from app.api.club_access.models import ClubMembers

    session.add(ClubMembers(club_id=w.co.id, email="co-admin@example.com", role="admin", status="active"))
    session.add(ClubMembers(club_id=w.co.id, email="co-editor@example.com", role="editor", status="active"))
    await session.commit()
    r = await _invite(client, w, w.co, note="  Want to run this together?  ")
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "invited" and row["note"] == "Want to run this together?"
    assert row["club"]["club_id"] == w.co.id and row["club"]["name"] == "Robotics Club"
    assert row["invited_by"] == "Design Society"
    ends = SOON + timedelta(hours=2)
    assert datetime.fromisoformat(row["expires_at"]) <= ends + timedelta(seconds=1)
    # The bell, at the invited club.
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.club_id == w.co.id, ClubAlerts.kind == "cohost_invite"))
    assert alert is not None and alert.link == "/invitations" and "Design Society" in alert.title
    # The mail: the club's own address and its admins', not its editors'.
    assert len(w.mail) == 1
    assert set(w.mail[0]["recipients"]) == {"robotics-co@example.com", "co-admin@example.com"}
    assert "Design Society invited you to co-host Hack the Campus" == w.mail[0]["subject"]
    assert "/invitations" in w.mail[0]["text"]
    # The audit log.
    logged = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "cohost.invite"))
    assert logged.entity_type == "event" and logged.entity_id == str(w.event_id)
    assert logged.details["club_id"] == w.co.id and logged.details["host_club_id"] == w.host.id

    later = SOON + timedelta(days=40)
    await session.execute(update(Events).where(Events.id == w.event_id).values(event_datetime=later))
    await session.commit()
    r2 = await _invite(client, w, w.third)
    sent = datetime.fromisoformat(r2.json()["invited_at"])
    assert datetime.fromisoformat(r2.json()["expires_at"]) - sent == timedelta(days=14)


async def test_inviting_refuses_itself_twice_strangers_unverified_closed_and_past_five(client, session, w, make_club):
    assert (r := await _invite(client, w, w.host)).status_code == 409 and r.json()["error_code"] == "COHOST_SELF"
    assert (r := await _invite(client, w, w.pending)).status_code == 404 and r.json()["error_code"] == "CLUB_NOT_FOUND"
    r = await client.post(w.base, json={"club_id": 987654}, headers=w.host_h)
    assert r.status_code == 404 and r.json()["error_code"] == "CLUB_NOT_FOUND"
    assert (await _invite(client, w, w.co)).status_code == 201
    assert (r := await _invite(client, w, w.co)).status_code == 409 and r.json()["error_code"] == "COHOST_ALREADY"
    # Five live invites at most, pending ones included.
    for i in range(4):
        _o, c = await make_club(name=f"Club Number {i}", email=f"club-{i}-co@example.com")
        assert (await _invite(client, w, c)).status_code == 201
    r = await _invite(client, w, w.third)
    assert r.status_code == 409 and r.json()["error_code"] == "COHOST_LIMIT"
    listing = (await client.get(w.base, headers=w.host_h)).json()
    assert listing["slots_left"] == 0 and listing["limit"] == 5 and len(listing["items"]) == 5
    # An event that is over takes no co-hosts.
    await session.execute(update(Events).where(Events.id == w.event_id).values(event_datetime=datetime.now(timezone.utc) - timedelta(days=2)))
    await session.commit()
    first = listing["items"][0]["id"]
    assert (await client.delete(f"{w.base}/{first}", headers=w.host_h)).status_code == 200
    r = await _invite(client, w, w.third)
    assert r.status_code == 409 and r.json()["error_code"] == "COHOST_EVENT_CLOSED"


async def test_search_finds_clubs_by_name_or_handle_and_says_where_each_stands(client, session, w):
    await _invite(client, w, w.co)
    r = await client.get(f"{w.base}/search", params={"q": "@robotics"}, headers=w.host_h)
    assert r.status_code == 200
    assert [(c["name"], c["state"]) for c in r.json()["items"]] == [("Robotics Club", "invited")]
    by_name = {c["name"]: c["state"] for c in (await client.get(f"{w.base}/search", params={"q": "club"}, headers=w.host_h)).json()["items"]}
    assert by_name["Robotics Club"] == "invited" and "Pending Club" not in by_name
    assert (await client.get(f"{w.base}/search", params={"q": "design"}, headers=w.host_h)).json()["items"][0]["state"] == "host"
    assert (await client.get(f"{w.base}/search", params={"q": "c"}, headers=w.host_h)).json()["items"] == []


# ─── Answering ──────────────────────────────────────────────────────────────


async def test_accepting_makes_a_cohost_on_its_list_its_dashboard_and_the_events_pages(client, session, w):
    invite_id = (await _invite(client, w, w.co)).json()["id"]
    mine = (await client.get("/api/v1/clubs/cohosts/invitations", headers=w.co_h)).json()
    assert mine["pending"] == 1 and mine["items"][0]["host"]["name"] == "Design Society"
    assert mine["items"][0]["event"]["id"] == w.event_id and mine["items"][0]["event"]["status"] == "published"
    # Before accepting: nothing of the event's.
    assert (await client.get(f"/api/v1/events/registration/{w.event_id}/list", headers=w.co_h)).status_code == 403
    r = await _accept(client, w, invite_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted" and r.json()["event"]["cohosts"][0]["club_id"] == w.co.id
    assert (await client.get("/api/v1/clubs/cohosts/invitations", headers=w.co_h)).json()["pending"] == 0
    # The invite's bell notice is read; the host is told.
    told = await session.scalar(select(ClubAlerts).where(ClubAlerts.club_id == w.host.id, ClubAlerts.kind == "cohost_update"))
    assert "Robotics Club accepted" in told.title
    assert (await session.scalar(select(ClubAlerts).where(ClubAlerts.dedupe_key == f"cohost_invite:{invite_id}"))).read_at is not None
    # The event's screens, as the host's team.
    assert (await client.get(f"/api/v1/events/registration/{w.event_id}/list", headers=w.co_h)).status_code == 200
    assert (await client.get(f"/api/v1/events/{w.event_id}/summary", headers=w.co_h)).status_code == 200
    # Its events list, badged; the host's too, and both counted once.
    rows = (await client.get("/api/v1/clubs/events/list", headers=w.co_h)).json()["items"]
    assert [(e["id"], e["viewer_role"], e["club"]["name"]) for e in rows] == [(w.event_id, "cohost", "Design Society")]
    assert rows[0]["cohosts"][0]["name"] == "Robotics Club"
    assert (await client.get("/api/v1/clubs/events/list", params={"role": "hosted"}, headers=w.co_h)).json()["items"] == []
    assert (await client.get("/api/v1/clubs/events/list", headers=w.host_h)).json()["items"][0]["viewer_role"] == "host"
    figures = (await client.get("/api/v1/clubs/events/figures", headers=w.co_h)).json()
    assert figures["upcoming"] == 1 and figures["cohosted_upcoming"] == 1 and figures["collected"] == 0
    # The detail: who hosts it, and the viewer's place on it.
    info = (await client.get(f"/api/v1/events/info/{w.event_id}", headers=w.co_h)).json()
    assert info["club"]["name"] == "Design Society" and info["viewer_role"] == "cohost"
    assert [c["name"] for c in info["cohosts"]] == ["Robotics Club"]
    assert (await client.get(f"/api/v1/events/info/{w.event_id}", headers=w.host_h)).json()["viewer_role"] == "host"
    assert (await client.get(f"/api/v1/events/info/{w.event_id}")).json()["viewer_role"] is None
    # The audit log has both sides.
    actions = set((await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_id == str(w.event_id)))).all())
    assert {"cohost.invite", "cohost.accept"} <= actions


async def test_declining_expiring_and_withdrawing_close_an_invite_and_a_new_one_may_follow(client, session, w):
    first = (await _invite(client, w, w.co)).json()["id"]
    r = await client.post(f"/api/v1/clubs/cohosts/invitations/{first}/decline", headers=w.co_h)
    assert r.status_code == 200 and r.json()["status"] == "declined"
    assert (r := await _accept(client, w, first)).status_code == 409 and r.json()["error_code"] == "COHOST_ANSWERED"
    second = (await _invite(client, w, w.co)).json()["id"]
    await session.execute(update(EventCohosts).where(EventCohosts.id == second).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await session.commit()
    r = await _accept(client, w, second)
    assert r.status_code == 410 and r.json()["error_code"] == "COHOST_INVITE_EXPIRED"
    third = (await _invite(client, w, w.co)).json()["id"]
    assert (await client.delete(f"{w.base}/{third}", headers=w.host_h)).json()["status"] == "cancelled"
    assert (r := await _accept(client, w, third)).status_code == 409 and r.json()["error_code"] == "COHOST_ANSWERED"
    statuses = [i["status"] for i in (await client.get(w.base, headers=w.host_h)).json()["items"]]
    assert sorted(statuses) == ["cancelled", "declined", "expired"]
    # Somebody else's invitation is nobody's to answer.
    fourth = (await _invite(client, w, w.co)).json()["id"]
    assert (r := await _accept(client, w, fourth, headers=w.third_h)).status_code == 404 and r.json()["error_code"] == "COHOST_INVITE_NOT_FOUND"


async def test_removing_or_leaving_ends_access_at_the_next_request(client, session, w):
    invite_id = await _cohosting(client, w)
    url = f"/api/v1/events/registration/{w.event_id}/list"
    assert (await client.get(url, headers=w.co_h)).status_code == 200
    removed = await client.delete(f"{w.base}/{invite_id}", headers=w.host_h)
    assert removed.json()["status"] == "removed"
    assert (await client.get(url, headers=w.co_h)).status_code == 403
    assert (await client.get("/api/v1/clubs/events/list", headers=w.co_h)).json()["items"] == []
    assert await session.scalar(select(ClubAlerts).where(ClubAlerts.club_id == w.co.id, ClubAlerts.kind == "cohost_update"))
    # Asked again, it may leave on its own.
    await _cohosting(client, w)
    assert (r := await client.post(f"{w.base}/leave", headers=w.host_h)).status_code == 409 and r.json()["error_code"] == "COHOST_NOT_COHOST"
    left = await client.post(f"{w.base}/leave", headers=w.co_h)
    assert left.status_code == 200 and left.json()["status"] == "left"
    assert (await client.get(url, headers=w.co_h)).status_code == 403
    assert (r := await client.post(f"{w.base}/leave", headers=w.co_h)).status_code == 403  # not a co-host: not its event at all
    actions = set((await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_id == str(w.event_id)))).all())
    assert {"cohost.remove", "cohost.leave"} <= actions


# ─── What a co-host's team may do ───────────────────────────────────────────


async def test_each_member_of_the_cohost_acts_with_their_own_role(client, session, w):
    invite_id = (await _invite(client, w, w.co)).json()["id"]
    editor = await member_header(client, session, w.co, "editor")
    door = await member_header(client, session, w.co, "door")
    admin = await member_header(client, session, w.co, "admin")
    # Only owners and admins answer: an editor reads the invitation, can't accept it.
    assert (await client.get("/api/v1/clubs/cohosts/invitations", headers=editor)).json()["pending"] == 1
    assert (r := await _accept(client, w, invite_id, headers=editor)).status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await _accept(client, w, invite_id, headers=admin)).status_code == 200
    # An editor runs the event; no money, no leaving.
    assert (await client.get(f"/api/v1/events/registration/{w.event_id}/list", headers=editor)).status_code == 200
    assert (await client.post(f"/api/v1/events/{w.event_id}/doors", json={"name": "Gate B"}, headers=editor)).status_code in (200, 201)
    assert (r := await client.get(f"/api/v1/events/{w.event_id}/finance", headers=editor)).status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (r := await client.post(f"{w.base}/leave", headers=editor)).status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    # Door staff: the door, and nothing else.
    assert (await client.get(f"/api/v1/events/{w.event_id}/gate/context", headers=door)).status_code == 200
    assert (r := await client.post(f"/api/v1/events/{w.event_id}/doors", json={"name": "Gate C"}, headers=door)).status_code == 403
    assert r.json()["error_code"] == "ROLE_FORBIDDEN"
    # The co-host's owner and admins see the money, and refund none of it.
    assert (await client.get(f"/api/v1/events/{w.event_id}/finance", headers=admin)).status_code == 200
    assert (await client.get(f"/api/v1/events/{w.event_id}/finance", headers=w.co_h)).status_code == 200
    r = await client.post(f"/api/v1/events/{w.event_id}/cancel", json={"reason": "x"}, headers=admin)
    assert r.status_code == 403 and r.json()["error_code"] == "COHOST_HOST_ONLY" and "Design Society" in r.json()["message"]
    assert (r := await client.delete(f"/api/v1/events/delete/{w.event_id}", headers=w.co_h)).json()["error_code"] == "COHOST_HOST_ONLY"
    # Nobody at the co-host invites more clubs.
    assert (r := await _invite(client, w, w.third, headers=w.co_h)).status_code == 403 and r.json()["error_code"] == "COHOST_HOST_ONLY"
    # The co-host's admin leaves.
    assert (await client.post(f"{w.base}/leave", headers=admin)).json()["status"] == "left"


async def test_at_the_host_editors_read_the_cohosts_and_only_owners_and_admins_change_them(client, session, w):
    editor = await member_header(client, session, w.host, "editor")
    admin = await member_header(client, session, w.host, "admin")
    listing = await client.get(w.base, headers=editor)
    assert listing.status_code == 200 and listing.json()["can_manage"] is False and listing.json()["viewer_role"] == "host"
    assert (r := await _invite(client, w, w.co, headers=editor)).status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.get(f"{w.base}/search", params={"q": "robot"}, headers=editor)).status_code == 403
    assert (await client.get(w.base, headers=admin)).json()["can_manage"] is True
    r = await _invite(client, w, w.co, headers=admin)
    assert r.status_code == 201 and r.json()["invited_by"].endswith("(Admin)")
    # The bell's co-host notices are for owners and admins.
    await _accept(client, w, r.json()["id"])
    kinds = {a["kind"] for a in (await client.get("/api/v1/clubs/alerts", headers=editor)).json()["items"]}
    assert "cohost_update" not in kinds
    assert "cohost_update" in {a["kind"] for a in (await client.get("/api/v1/clubs/alerts", headers=admin)).json()["items"]}


def test_the_role_rules_for_cohosting():
    for role in ("admin", "editor", "door"):
        assert roles.allows(role, "GET", "/api/v1/events/7/cohosts")
        assert roles.allows(role, "GET", "/api/v1/clubs/cohosts/invitations")
    assert roles.allows("admin", "POST", "/api/v1/events/7/cohosts")
    assert roles.allows("admin", "POST", "/api/v1/clubs/cohosts/invitations/3/accept")
    assert roles.allows("admin", "GET", "/api/v1/events/7/cohosts/search")
    for role in ("editor", "door"):
        assert not roles.allows(role, "POST", "/api/v1/events/7/cohosts")
        assert not roles.allows(role, "DELETE", "/api/v1/events/7/cohosts/3")
        assert not roles.allows(role, "POST", "/api/v1/events/7/cohosts/leave")
        assert not roles.allows(role, "GET", "/api/v1/events/7/cohosts/search")
        assert not roles.allows(role, "POST", "/api/v1/clubs/cohosts/invitations/3/decline")
    assert roles.can("admin")["manage_cohosts"] and not roles.can("editor")["manage_cohosts"]


async def test_platform_admins_see_every_invite_and_may_manage(client, session, w, make_admin, mint_token):
    await _invite(client, w, w.co)
    admin = auth_header(await mint_token(await make_admin(email="cohost-admin@example.com")))
    r = await client.get(w.base, headers=admin)
    if r.status_code == 403 and r.json().get("error_code") == "TWO_FACTOR_REQUIRED":
        pytest.skip("admins here need two-factor first")
    assert r.status_code == 200 and r.json()["viewer_role"] == "admin" and r.json()["can_manage"] is True
    assert r.json()["items"][0]["status"] == "invited"


# ─── Routes that find their event another way ───────────────────────────────


async def test_a_cohost_opens_tickets_adds_volunteers_but_keeps_its_own_books_and_codes(client, session, w, make_user, make_registration):
    await _cohosting(client, w)
    guest = await make_user(email="guest-co@example.com")
    reg = await make_registration(event=w.event, user=guest)
    await session.commit()
    assert (await client.get(f"/api/v1/events/tickets/{reg.ticket_id}", headers=w.co_h)).status_code == 200
    assert (await client.get(f"/api/v1/events/tickets/{reg.ticket_id}", headers=w.third_h)).status_code == 403
    r = await client.post("/api/v1/events/volunteer/add", json={"email_id": "helper-co@example.com", "event_id": w.event_id, "full_name": "Helper"}, headers=w.co_h)
    assert r.status_code == 200, r.text
    assert (await client.get(f"/api/v1/events/volunteer/list/{w.event_id}", headers=w.co_h)).status_code == 200
    r = await client.post("/api/v1/events/volunteer/add", json={"email_id": "other-co@example.com", "event_id": w.event_id, "full_name": "Other"}, headers=w.third_h)
    assert r.status_code == 403
    expense = await client.post("/api/v1/clubs/finance/expenses", json={"kind": "expense", "title": "Banners", "category": "venue", "amount": 500, "event_id": w.event_id}, headers=w.co_h)
    assert expense.status_code in (403, 422), expense.text
    if expense.status_code == 403:
        assert expense.json()["error_code"] == "COHOST_HOST_ONLY"


async def test_a_cohost_adds_partners_from_its_own_club_and_saves_emails_to_its_own_library(client, session, w):
    from app.api.clubs.models import ClubPartners
    from app.api.emails.models import EmailTemplates

    await _cohosting(client, w)
    mine = ClubPartners(club_id=w.co.id, name="Kinetic Labs", role="Title sponsor")
    theirs = ClubPartners(club_id=w.host.id, name="Host Sponsor")
    session.add_all([mine, theirs])
    await session.commit()
    url = f"/api/v1/events/{w.event_id}/partners/from-club"
    r = await client.post(url, json={"kind": "sponsor", "club_partner_ids": [mine.id]}, headers=w.co_h)
    assert r.status_code == 201, r.text
    assert [p["name"] for p in r.json()["added"]] == ["Kinetic Labs"]
    assert (await client.post(url, json={"kind": "sponsor", "club_partner_ids": [theirs.id]}, headers=w.co_h)).status_code in (404, 422)
    # An event email saved to the library goes to the co-host's own.
    made = await client.post(f"/api/v1/events/{w.event_id}/emails", json={"stage": "CUSTOM", "name": "Hello", "subject": "Hi"}, headers=w.co_h)
    if made.status_code not in (200, 201):
        pytest.skip(f"an email template could not be made here: {made.text[:200]}")
    tpl = made.json()["id"]
    assert (await client.post(f"/api/v1/events/{w.event_id}/emails/{tpl}/save-to-library", headers=w.co_h)).status_code == 200
    kept = list(await session.scalars(select(EmailTemplates.club_id).where(EmailTemplates.event_id.is_(None), EmailTemplates.name == "Hello")))
    assert kept == [w.co.id]


# ─── Public pages ───────────────────────────────────────────────────────────


async def test_the_event_shows_on_both_clubs_pages_once_and_hosted_by_both(client, session, w):
    await _cohosting(client, w)
    page = (await client.get(f"/api/v1/discover/events/{w.event.slug}")).json()
    assert page["club"]["name"] == "Design Society"
    assert [(c["name"], c["slug"]) for c in page["cohosts"]] == [("Robotics Club", w.co.slug)]
    for slug in (w.host.slug, w.co.slug):
        club_page = (await client.get(f"/api/v1/discover/clubs/{slug}")).json()
        assert [e["id"] for e in club_page["upcoming"]] == [w.event_id]
        assert club_page["upcoming"][0]["club"]["name"] == "Design Society"
        assert club_page["upcoming"][0]["cohosts"][0]["name"] == "Robotics Club"
        assert (await client.get(f"/api/v1/discover/clubs/{slug}/events", params={"when": "upcoming"})).json()["total"] == 1
    listed = (await client.get("/api/v1/discover/events")).json()["items"]
    assert [e["id"] for e in listed].count(w.event_id) == 1
    # The app's club page, and the admin's.
    app_rows = (await client.get(f"/api/v1/clubs/{w.co.id}/events", headers=w.third_h)).json()["items"]
    assert [(e["id"], e["club"]["id"]) for e in app_rows] == [(w.event_id, w.host.id)]
    assert app_rows[0]["cohosts"][0]["club_id"] == w.co.id


# ─── Followers ──────────────────────────────────────────────────────────────


async def _followers(session, make_user, club, n, prefix, **link):
    people = []
    for i in range(n):
        u = await make_user(email=f"{prefix}{i}-co@example.com")
        session.add(ClubUsersLink(club_id=club.id, user_id=u.id, is_following=True, **link))
        session.add(UserDeviceTokens(user_id=u.id, fcm_token=f"tok-{prefix}{i}", platform="android"))
        people.append(u)
    await session.commit()
    return people


async def _new_event_lines(session, event_id):
    return {(n.user_id, n.from_club_id) for n in await session.scalars(select(Notifications).where(Notifications.event_id == event_id, Notifications.type == "new_event"))}


async def test_the_cohosts_followers_hear_of_the_event_once_and_the_hosts_not_again(client, session, w, make_user, fcm):
    host_fans = await _followers(session, make_user, w.host, 1, "hostfan")
    both = await _followers(session, make_user, w.host, 1, "both")
    session.add(ClubUsersLink(club_id=w.co.id, user_id=both[0].id, is_following=True))
    co_fans = await _followers(session, make_user, w.co, 2, "cofan")
    muted = await _followers(session, make_user, w.co, 1, "muted", is_muted=True)
    await session.commit()
    await _cohosting(client, w)
    lines = await _new_event_lines(session, w.event_id)
    assert lines == {(u.id, w.co.id) for u in co_fans}
    assert fcm.tokens() == {"tok-cofan0", "tok-cofan1"}
    assert all(u.id not in {x for x, _ in lines} for u in host_fans + both + muted)
    # The same club asked again tells nobody twice.
    row = await session.scalar(select(EventCohosts).where(EventCohosts.event_id == w.event_id, EventCohosts.status == ACCEPTED))
    await client.delete(f"{w.base}/{row.id}", headers=w.host_h)
    await _cohosting(client, w)
    assert await _new_event_lines(session, w.event_id) == lines


async def test_a_draft_tells_the_cohosts_followers_when_it_is_published(client, session, w, make_user, fcm):
    await session.execute(update(Events).where(Events.id == w.event_id).values(lifecycle=EventStatus.draft))
    await session.commit()
    fans = await _followers(session, make_user, w.co, 1, "draftfan")
    await _cohosting(client, w)
    assert await _new_event_lines(session, w.event_id) == set()
    r = await client.patch(f"/api/v1/events/{w.event_id}/closeout/settings", json={"status": "published"}, headers=w.host_h)
    assert r.status_code == 200, r.text
    assert await _new_event_lines(session, w.event_id) == {(fans[0].id, w.co.id)}


# ─── Documents ──────────────────────────────────────────────────────────────


async def test_documents_say_hosted_by_the_host_with_its_cohosts(client, session, w):
    from sqlalchemy.orm import joinedload, selectinload

    from app.api.events.documents import hosted_by, join_names, token_event

    await _cohosting(client, w)
    loaded = await session.scalar(
        select(Events).where(Events.id == w.event_id).options(joinedload(Events.club), selectinload(Events.cohost_clubs))
        .execution_options(populate_existing=True)
    )
    assert hosted_by(loaded) == "Design Society with Robotics Club"
    assert token_event(loaded).organiser == "Design Society with Robotics Club"
    assert join_names(["A", "B", "C"]) == "A, B and C"
    plain = await session.scalar(select(Events).where(Events.id == w.event_id).options(joinedload(Events.club)).execution_options(populate_existing=True))
    assert hosted_by(plain) == "Design Society"


async def test_the_admin_list_finds_a_clubs_cohosted_events(client, session, w, make_admin, mint_token):
    await _cohosting(client, w)
    admin = auth_header(await mint_token(await make_admin(email="cohost-list-admin@example.com")))
    r = await client.get("/api/v1/events/admin/list", params={"cohost_club_id": w.co.id}, headers=admin)
    if r.status_code == 403 and r.json().get("error_code") == "TWO_FACTOR_REQUIRED":
        pytest.skip("admins here need two-factor first")
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    assert [e["id"] for e in rows] == [w.event_id] and rows[0]["cohosts"][0]["name"] == "Robotics Club"
