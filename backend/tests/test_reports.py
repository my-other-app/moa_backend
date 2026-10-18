"""Reports and blocks (App Store guideline 1.2): a reader reports a comment,
a post, a person or a club; three reports hide a comment from everyone but
its author; a reader blocks someone and stops seeing their comments; an
admin works the queue and resolves each report with everything on the same
thing, and is told when something is reported."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.admin_alerts.models import AdminNotifications
from app.api.audit.models import AdminAuditLogs
from app.api.reports.models import ContentReports
from app.api.threads.models import ClubThreads, ThreadComments
from app.api.users.models import AdminRoles, Users, UserTypes
from app.config import settings
from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.conftest import auth_header

API = "/api/v1"


@pytest.fixture
async def world(client, session, make_club, make_user, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)

    async def fake_push(*args, **kwargs):
        return 0

    monkeypatch.setattr(push, "send_notification_to_users", fake_push)
    owner, club = await make_club(name="Circuit Society", email="reports-club@example.com")
    people = {}
    for name in ("writer", "fan", "fan2", "fan3", "fan4"):
        people[name] = await make_user(email=f"{name}-reports@example.com", username=f"{name}-reports")
        people[name].full_name = name.capitalize()
        # Readers of some standing: a report from an account under a week old weighs half.
        people[name].created_at = datetime.now(timezone.utc) - timedelta(days=60)
    guest = await make_user(email="guest-reports@example.com", username="guest-reports", user_type=UserTypes.guest)
    owner_admin = await make_admin(email="owner-reports@example.com")
    support = await make_admin(email="support-reports@example.com", role=AdminRoles.support)
    viewer = await make_admin(email="viewer-reports@example.com", role=AdminRoles.viewer)
    await session.commit()
    h = {name: auth_header(await mint_token(u)) for name, u in people.items()}
    club_h = auth_header(await mint_token(owner))
    thread = (await client.post(f"{API}/clubs/threads", data={"body": "Hack Night 05 is on Friday.", "notify_followers": "false"}, headers=club_h)).json()
    comment = (await client.post(f"{API}/threads/{thread['id']}/comments", json={"body": "This club is a scam, avoid."}, headers=h["writer"])).json()
    return dict(
        owner=owner, club=club, people=people, h=h, club_h=club_h, thread=thread, comment=comment,
        guest_h=auth_header(await mint_token(guest)),
        owner_admin=owner_admin, admin_h=auth_header(await mint_token(owner_admin)),
        support_h=auth_header(await mint_token(support)), viewer_h=auth_header(await mint_token(viewer)),
    )


def _report(client, headers, target_type, target_id, reason="harassment", **extra):
    return client.post(f"{API}/reports", json={"target_type": target_type, "target_id": target_id, "reason": reason, **extra}, headers=headers)


async def _comments(client, thread_id, headers=None):
    r = await client.get(f"{API}/threads/{thread_id}/comments", headers=headers or {})
    assert r.status_code == 200, r.text
    return [c["body"] for c in r.json()]


async def _alerts(session):
    return list((await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "content_reported").order_by(AdminNotifications.id))).all())


# ─── Reporting ──────────────────────────────────────────────────────────


async def test_a_report_is_made_once_per_person_and_thing(client, session, world):
    h, comment, thread = world["h"], world["comment"], world["thread"]
    first = await _report(client, h["fan"], "comment", comment["id"], note="  Calling people names  ")
    assert first.status_code == 201, first.text
    assert set(first.json()) == {"id", "status"} and first.json()["status"] == "open"
    # Again, even for another reason: the open one comes back, 200.
    again = await _report(client, h["fan"], "comment", comment["id"], reason="spam")
    assert again.status_code == 200 and again.json() == first.json()
    rows = list((await session.scalars(select(ContentReports).where(ContentReports.target_id == comment["id"]))).all())
    assert len(rows) == 1 and rows[0].reason == "harassment" and rows[0].note == "Calling people names"
    assert rows[0].target_author_id == world["people"]["writer"].id and rows[0].club_id == world["club"].id

    # Every kind of thing; a user's or a club's id as a number (or its digits).
    assert (await _report(client, h["fan"], "thread", thread["id"], reason="misinformation")).status_code == 201
    assert (await _report(client, h["fan"], "user", world["people"]["writer"].id, reason="spam")).status_code == 201
    assert (await _report(client, h["fan"], "club", str(world["club"].id), reason="other")).status_code == 201


async def test_reports_are_refused_with_codes(client, session, world):
    h, comment = world["h"], world["comment"]
    assert (await _report(client, {}, "comment", comment["id"])).status_code == 401
    assert (await _report(client, world["guest_h"], "comment", comment["id"])).json()["error_code"] == "INSUFFICIENT_PERMISSIONS"
    for target_type, target_id in (("comment", "not-a-uuid"), ("comment", str(uuid.uuid4())), ("thread", str(uuid.uuid4())), ("user", 99999999), ("club", "abc"), ("user", True)):
        r = await _report(client, h["fan"], target_type, target_id)
        assert r.status_code == 404 and r.json()["error_code"] == "TARGET_NOT_FOUND", (target_type, target_id, r.text)
    r = await _report(client, h["fan"], "user", world["people"]["fan"].id)
    assert r.status_code == 422 and r.json()["error_code"] == "CANNOT_REPORT_SELF"
    assert (await _report(client, h["fan"], "comment", comment["id"], reason="rude")).status_code == 422
    assert (await _report(client, h["fan"], "comment", comment["id"], note="x" * 501)).status_code == 422
    assert (await _report(client, h["fan"], "post", comment["id"])).status_code == 422
    # A removed comment can no longer be reported.
    await client.delete(f"{API}/threads/{world['thread']['id']}/comments/{comment['id']}", headers=h["writer"])
    assert (await _report(client, h["fan"], "comment", comment["id"])).json()["error_code"] == "TARGET_NOT_FOUND"


async def test_twenty_reports_an_hour_then_429(client, session, world, make_user):
    h = world["h"]
    targets = [await make_user(email=f"target{i}-reports@example.com", username=f"target{i}-reports") for i in range(21)]
    await session.commit()
    for t in targets[:20]:
        assert (await _report(client, h["fan"], "user", t.id, reason="spam")).status_code == 201
    r = await _report(client, h["fan"], "user", targets[20].id, reason="spam")
    assert r.status_code == 429, r.text
    assert r.json()["error_code"] == "RATE_LIMITED" and 0 < r.json()["errors"]["retry_after"] <= 3600
    # An open report already made still comes back; someone else is not limited.
    assert (await _report(client, h["fan"], "user", targets[0].id, reason="spam")).status_code == 200
    assert (await _report(client, h["fan2"], "user", targets[20].id, reason="spam")).status_code == 201


# ─── Three reports hide a comment ───────────────────────────────────────


async def test_three_reports_hide_a_comment_from_everyone_but_its_author_until_resolved(client, session, world):
    h, thread, comment = world["h"], world["thread"], world["comment"]
    body = comment["body"]
    for i, who in enumerate(("fan", "fan2")):
        await _report(client, h[who], "comment", comment["id"])
        # Repeating one's own report does not count twice.
        await _report(client, h[who], "comment", comment["id"])
        assert body in await _comments(client, thread["id"]), i
    await _report(client, h["fan3"], "comment", comment["id"])

    assert body not in await _comments(client, thread["id"])
    assert body not in await _comments(client, thread["id"], h["fan4"])
    assert body not in await _comments(client, thread["id"], h["fan"])
    club_list = (await client.get(f"{API}/clubs/threads/{thread['id']}/comments", headers=world["club_h"])).json()
    assert body not in [c["body"] for c in club_list]
    # Its author still sees it.
    assert body in await _comments(client, thread["id"], h["writer"])

    listed = (await client.get(f"{API}/admin/reports?status=open&target_type=comment", headers=world["admin_h"])).json()
    assert listed["total"] == 3 and {i["report_count"] for i in listed["items"]} == {3}

    # The admin dismisses: everyone sees it again.
    r = await client.post(f"{API}/admin/reports/{listed['items'][0]['id']}/resolve", json={"action": "dismiss", "note": "Strong words, not abuse"}, headers=world["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] == 3 and r.json()["report"]["status"] == "resolved" and r.json()["report"]["resolution"] == "dismiss"
    assert body in await _comments(client, thread["id"])
    assert body in await _comments(client, thread["id"], h["fan4"])


async def test_fresh_accounts_weigh_half_and_the_threshold_is_a_setting(client, session, world, make_user, mint_token, monkeypatch):
    """Three accounts made today cannot hide a comment on their own: each
    report from an account under a week old weighs half."""
    h, thread, comment = world["h"], world["thread"], world["comment"]
    body = comment["body"]
    fresh = []
    for i in range(5):
        u = await make_user(email=f"fresh-{i}-reports@example.com", username=f"fresh-{i}-reports")
        fresh.append(auth_header(await mint_token(u)))
    for who in fresh[:3]:
        assert (await _report(client, who, "comment", comment["id"])).status_code == 201
    assert body in await _comments(client, thread["id"])  # 1.5 of 3
    # Two more fresh ones and one reader of standing: 2.5 + 1 = 3.5.
    for who in fresh[3:]:
        await _report(client, who, "comment", comment["id"])
    assert body in await _comments(client, thread["id"])  # 2.5
    await _report(client, h["fan"], "comment", comment["id"])
    assert body not in await _comments(client, thread["id"])
    # The admin's queue still counts people, not weight.
    listed = (await client.get(f"{API}/admin/reports?status=open&target_type=comment", headers=world["admin_h"])).json()
    assert {i["report_count"] for i in listed["items"]} == {6}
    # The threshold is a setting.
    monkeypatch.setattr(settings, "REPORTS_TO_HIDE", 4.0)
    assert body in await _comments(client, thread["id"])


# ─── Blocks ─────────────────────────────────────────────────────────────


async def test_a_blocked_persons_comments_never_show_to_the_blocker(client, session, world):
    h, thread, people = world["h"], world["thread"], world["people"]
    body = world["comment"]["body"]
    reply = await client.post(f"{API}/clubs/threads/{thread['id']}/comments", json={"body": "We are not a scam."}, headers=world["club_h"])
    assert reply.status_code == 201
    listed = (await client.get(f"{API}/threads/{thread['id']}/comments", headers=h["fan"])).json()
    writer_item = next(c for c in listed if c["body"] == body)
    # Comment items carry the author, picture included, for "Block {name}".
    assert writer_item["author"] == {"id": people["writer"].id, "name": "Writer", "is_club": False, "avatar": None}

    assert (await client.get(f"{API}/user/blocks", headers=h["fan"])).json() == []
    b = await client.post(f"{API}/user/blocks", json={"user_id": people["writer"].id}, headers=h["fan"])
    assert b.status_code == 201 and b.json() == {"user_id": people["writer"].id, "name": "Writer", "avatar": None}
    # Blocking twice is blocking once.
    assert (await client.post(f"{API}/user/blocks", json={"user_id": people["writer"].id}, headers=h["fan"])).status_code == 201
    assert (await client.get(f"{API}/user/blocks", headers=h["fan"])).json() == [{"user_id": people["writer"].id, "name": "Writer", "avatar": None}]

    assert body not in await _comments(client, thread["id"], h["fan"])
    assert "We are not a scam." in await _comments(client, thread["id"], h["fan"])
    # Nobody else is affected, and the blocked person still sees their own.
    assert body in await _comments(client, thread["id"], h["fan2"])
    assert body in await _comments(client, thread["id"])
    assert body in await _comments(client, thread["id"], h["writer"])

    # Blocking the club's account hides the club's replies too.
    await client.post(f"{API}/user/blocks", json={"user_id": world["owner"].id}, headers=h["fan"])
    assert await _comments(client, thread["id"], h["fan"]) == []
    blocks = (await client.get(f"{API}/user/blocks", headers=h["fan"])).json()
    assert [x["name"] for x in blocks] == ["Circuit Society", "Writer"]

    assert (await client.delete(f"{API}/user/blocks/{people['writer'].id}", headers=h["fan"])).status_code == 204
    assert (await client.delete(f"{API}/user/blocks/{people['writer'].id}", headers=h["fan"])).status_code == 204
    assert body in await _comments(client, thread["id"], h["fan"])

    r = await client.post(f"{API}/user/blocks", json={"user_id": people["fan"].id}, headers=h["fan"])
    assert r.status_code == 422 and r.json()["error_code"] == "CANNOT_BLOCK_SELF"
    r = await client.post(f"{API}/user/blocks", json={"user_id": 99999999}, headers=h["fan"])
    assert r.status_code == 404 and r.json()["error_code"] == "USER_NOT_FOUND"
    assert (await client.get(f"{API}/user/blocks")).status_code == 401


# ─── The admin's queue ──────────────────────────────────────────────────


async def test_the_queue_shows_each_report_in_full(client, session, world):
    h, comment, thread, people, club = world["h"], world["comment"], world["thread"], world["people"], world["club"]
    await _report(client, h["fan"], "comment", comment["id"], reason="hate", note="slur in the second line")
    await _report(client, h["fan2"], "comment", comment["id"], reason="harassment")
    await _report(client, h["fan"], "thread", thread["id"], reason="spam")
    await _report(client, h["fan"], "user", people["writer"].id, reason="harassment")

    page = await client.get(f"{API}/admin/reports?status=open", headers=world["viewer_h"])
    assert page.status_code == 200, page.text
    body = page.json()
    assert set(body) == {"items", "total"} and body["total"] == 4
    newest = body["items"][0]
    assert set(newest) >= {"id", "target_type", "target_id", "reason", "note", "status", "reporter", "target_preview", "target_author", "club", "report_count", "created_at", "resolved_at", "resolution"}
    assert newest["target_type"] == "user" and newest["target_id"] == str(people["writer"].id) and newest["club"] is None
    assert newest["target_preview"] == "Writer" and newest["target_author"] == {"id": people["writer"].id, "name": "Writer"}

    hate = next(i for i in body["items"] if i["reason"] == "hate")
    assert hate["target_id"] == comment["id"] and hate["note"] == "slur in the second line" and hate["status"] == "open"
    assert hate["reporter"] == {"id": people["fan"].id, "name": "Fan"}
    assert hate["target_preview"] == "This club is a scam, avoid." and hate["target_author"] == {"id": people["writer"].id, "name": "Writer"}
    assert hate["club"] == {"id": club.id, "name": "Circuit Society", "slug": club.slug}
    assert hate["report_count"] == 2 and hate["resolved_at"] is None and hate["resolution"] is None and hate["target_removed"] is False
    spam = next(i for i in body["items"] if i["target_type"] == "thread")
    assert spam["target_author"] == {"id": world["owner"].id, "name": "Circuit Society"} and spam["report_count"] == 1

    only_threads = (await client.get(f"{API}/admin/reports?target_type=thread", headers=world["admin_h"])).json()
    assert only_threads["total"] == 1
    paged = (await client.get(f"{API}/admin/reports?offset=1&limit=2", headers=world["admin_h"])).json()
    assert paged["total"] == 4 and len(paged["items"]) == 2
    assert (await client.get(f"{API}/admin/reports?status=resolved", headers=world["admin_h"])).json() == {"items": [], "total": 0}
    assert (await client.get(f"{API}/admin/reports?status=closed", headers=world["admin_h"])).status_code == 422
    assert (await client.get(f"{API}/admin/reports", headers=h["fan"])).status_code == 403


async def test_remove_content_takes_down_the_comment_or_the_thread(client, session, world):
    h, comment, thread = world["h"], world["comment"], world["thread"]
    a = (await _report(client, h["fan"], "comment", comment["id"])).json()
    await _report(client, h["fan2"], "comment", comment["id"], reason="spam")
    r = await client.post(f"{API}/admin/reports/{a['id']}/resolve", json={"action": "remove_content"}, headers=world["support_h"])
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] == 2 and r.json()["report"]["target_removed"] is True
    # The admin still reads what was said.
    assert r.json()["report"]["target_preview"] == "This club is a scam, avoid."
    assert comment["body"] not in await _comments(client, thread["id"], h["writer"])
    row = await session.scalar(select(ThreadComments).where(ThreadComments.id == uuid.UUID(comment["id"])).execution_options(include_deleted=True))
    await session.refresh(row)
    assert row.is_deleted is True
    t = await session.scalar(select(ClubThreads).where(ClubThreads.id == uuid.UUID(thread["id"])))
    await session.refresh(t)
    assert t.comments_count == 0
    again = await client.post(f"{API}/admin/reports/{a['id']}/resolve", json={"action": "dismiss"}, headers=world["support_h"])
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_RESOLVED"

    tr = (await _report(client, h["fan"], "thread", thread["id"], reason="spam")).json()
    r = await client.post(f"{API}/admin/reports/{tr['id']}/resolve", json={"action": "remove_content", "note": "Spam"}, headers=world["admin_h"])
    assert r.status_code == 200 and r.json()["report"]["resolution_note"] == "Spam"
    assert (await client.get(f"{API}/threads/{thread['id']}")).status_code == 404

    u = (await _report(client, h["fan"], "user", world["people"]["writer"].id)).json()
    r = await client.post(f"{API}/admin/reports/{u['id']}/resolve", json={"action": "remove_content"}, headers=world["admin_h"])
    assert r.status_code == 422 and r.json()["error_code"] == "NOT_REMOVABLE"
    assert (await client.post(f"{API}/admin/reports/99999999/resolve", json={"action": "dismiss"}, headers=world["admin_h"])).json()["error_code"] == "REPORT_NOT_FOUND"
    assert (await client.post(f"{API}/admin/reports/{u['id']}/resolve", json={"action": "ban"}, headers=world["admin_h"])).status_code == 422


async def test_suspend_author_suspends_the_account_and_takes_its_comment_down(client, session, world):
    h, comment, thread, writer = world["h"], world["comment"], world["thread"], world["people"]["writer"]
    a = (await _report(client, h["fan"], "comment", comment["id"])).json()

    # Moderation needs the moderate capability: a viewer admin reads the queue but cannot act.
    r = await client.post(f"{API}/admin/reports/{a['id']}/resolve", json={"action": "suspend_author"}, headers=world["viewer_h"])
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    assert (await client.post(f"{API}/admin/reports/{a['id']}/resolve", json={"action": "dismiss"}, headers=h["fan2"])).status_code == 403

    r = await client.post(f"{API}/admin/reports/{a['id']}/resolve", json={"action": "suspend_author", "note": "Repeated abuse"}, headers=world["support_h"])
    assert r.status_code == 200, r.text
    assert r.json()["report"]["resolution"] == "suspend_author" and r.json()["report"]["target_removed"] is True
    account = await session.scalar(select(Users).where(Users.id == writer.id))
    await session.refresh(account)
    assert account.is_active is False and account.suspended_reason == "Reported comment: harassment"
    # Every token already issued is refused.
    refused = await client.get(f"{API}/user/profile/me", headers=h["writer"])
    assert refused.status_code in (401, 403)
    assert comment["body"] not in await _comments(client, thread["id"])

    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "report.resolve"))
    assert log is not None and log.entity_type == "comment" and log.entity_id == comment["id"]
    assert log.details["action"] == "suspend_author" and log.details["resolved"] == 1

    # A report on a user suspends that user.
    u = (await _report(client, h["fan"], "user", world["people"]["fan4"].id)).json()
    assert (await client.post(f"{API}/admin/reports/{u['id']}/resolve", json={"action": "suspend_author"}, headers=world["admin_h"])).status_code == 200
    fan4 = await session.scalar(select(Users).where(Users.id == world["people"]["fan4"].id))
    await session.refresh(fan4)
    assert fan4.is_active is False

    # No admin suspends themselves this way.
    me = (await _report(client, h["fan"], "user", world["owner_admin"].id)).json()
    r = await client.post(f"{API}/admin/reports/{me['id']}/resolve", json={"action": "suspend_author"}, headers=world["admin_h"])
    assert r.status_code == 422 and r.json()["error_code"] == "CANNOT_SUSPEND_SELF"


# ─── The alert ──────────────────────────────────────────────────────────


async def test_each_report_alerts_the_admins_once_per_thing(client, session, world):
    h, comment, thread = world["h"], world["comment"], world["thread"]
    await _report(client, h["fan"], "comment", comment["id"], reason="hate")
    alerts = await _alerts(session)
    assert len(alerts) == 1
    assert alerts[0].data == {"target_type": "comment", "target_id": comment["id"], "reason": "hate", "report_count": 1}
    assert alerts[0].club_id == world["club"].id and alerts[0].body == "This club is a scam, avoid."

    # More reports on the same comment: still one alert in the window.
    await _report(client, h["fan2"], "comment", comment["id"])
    await _report(client, h["fan3"], "comment", comment["id"])
    assert len(await _alerts(session)) == 1
    # A repeat by the same person sends nothing either.
    assert (await _report(client, h["fan"], "comment", comment["id"])).status_code == 200
    assert len(await _alerts(session)) == 1

    # Another thing is another alert.
    await _report(client, h["fan"], "user", world["people"]["writer"].id, reason="spam")
    alerts = await _alerts(session)
    assert len(alerts) == 2 and alerts[1].data == {"target_type": "user", "target_id": str(world["people"]["writer"].id), "reason": "spam", "report_count": 1}
    await _report(client, h["fan"], "thread", thread["id"], reason="spam")
    assert len(await _alerts(session)) == 3
    # It is in the admins' bell.
    bell = (await client.get(f"{API}/admin/alerts?kind=content_reported", headers=world["admin_h"])).json()
    assert bell["total"] == 3
