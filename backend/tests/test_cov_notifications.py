"""Pushes a club or the platform sends, and the club dashboard's bell.

- A club's announcement to an event's registrants, attendees or no-shows:
  the inbox lines and the pushes themselves (POST /clubs/notifications/events/{id}/send).
- A volunteer told when the club adds them (triggers.notify_user_added_as_volunteer).
- The owners' test tools: a push to every device (owners only, twice an hour,
  audited), the newest tokens, Firebase's status, one push to one token.
- The bell: notices from the platform, read by the whole team (GET/POST /clubs/alerts).
- The older push history (GET /clubs/notifications/history): one row per send.

FCM is faked where firebase-admin is called (messaging.send and
send_each_for_multicast), as tests/test_device_tokens.py does, so everything
up to the SDK runs for real.
"""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from firebase_admin import exceptions as firebase_exceptions
from firebase_admin import messaging
from sqlalchemy import func, insert, select

from app.api.audit.models import AdminAuditLogs
from app.api.club_alerts import service as alerts
from app.api.club_alerts.models import ClubAlerts
from app.api.events.volunteer.models import Volunteer
from app.api.notifications.models import NotificationStatus, Notifications
from app.api.security import service as security
from app.api.users.models import AdminRoles, UserDeviceTokens, UserTypes, Users
from app.core.email import email as email_mod
from app.core.notifications import fanout
from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)


class FakeFcm:
    """firebase-admin's two send calls, recording what would have gone."""

    def __init__(self):
        self.multicasts: list = []
        self.singles: list = []
        self.fail_single: Exception | None = None

    def multicast(self, message):
        self.multicasts.append(message)
        return SimpleNamespace(success_count=len(message.tokens), failure_count=0, responses=[])

    def send(self, message):
        if self.fail_single is not None:
            raise self.fail_single
        self.singles.append(message)
        return f"projects/test/messages/{len(self.singles)}"

    def tokens(self) -> list[str]:
        return sorted([t for m in self.multicasts for t in m.tokens] + [m.token for m in self.singles])


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeFcm()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.multicast)
    monkeypatch.setattr(messaging, "send", fake.send)
    return fake


def _device(session, user, token, platform="android"):
    session.add(UserDeviceTokens(user_id=user.id, fcm_token=token, platform=platform))


async def _rows(session, **where):
    q = select(Notifications)
    for k, v in where.items():
        q = q.where(getattr(Notifications, k) == v)
    return list(await session.scalars(q))


# ─── A club's announcement ─────────────────────────────────────────────────


@pytest.fixture
async def hall(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Circuit Society", email="announce-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW - timedelta(hours=1), duration=4.0)
    other = await make_event(club=club, created_by=owner, name="Next Month", event_datetime=NOW + timedelta(days=30))
    people = {}
    for key, attended, tokens in (
        ("asha", True, ["tok-asha-phone", "tok-asha-tablet"]),
        ("bala", True, []),
        ("chitra", False, ["tok-chitra"]),
        ("dev", False, ["tok-dev"]),
    ):
        u = await make_user(email=f"{key}-announce@example.com")
        await make_registration(event=event, user=u, is_attended=attended)
        for t in tokens:
            _device(session, u, t)
        people[key] = u
    # A guest who left, and someone on the other event: never reached.
    left = await make_user(email="left-announce@example.com")
    await make_registration(event=event, user=left, is_deleted=True)
    _device(session, left, "tok-left")
    elsewhere = await make_user(email="elsewhere-announce@example.com")
    await make_registration(event=other, user=elsewhere)
    _device(session, elsewhere, "tok-elsewhere")
    rival_owner, _ = await make_club(name="Rival Society", email="announce-rival@example.com")
    app_user = await make_user(email="plain-announce@example.com")
    await session.commit()
    return SimpleNamespace(
        club=club, event=event, other=other, **people, left=left, elsewhere=elsewhere,
        h=auth_header(await mint_token(owner)), rival=auth_header(await mint_token(rival_owner)), app=auth_header(await mint_token(app_user)),
    )


async def _send(client, hall, audience, headers=None, event=None, **extra):
    body = {"title": f"Doors for {audience}", "body": "Lab 3B from 6", "audience": audience, **extra}
    r = await client.post(f"{API}/clubs/notifications/events/{(event or hall.event).id}/send", json=body, headers=headers or hall.h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_an_announcement_reaches_every_registrant_on_every_device(client, session, hall, fcm):
    r = await _send(client, hall, "all", image_url="https://cdn.example.com/doors.png")
    assert r == {"success": True, "message": "Notification sent to 4 of 4 registrants", "notifications_sent": 4}
    # One inbox line each, the club's, about the event.
    rows = await _rows(session, type="club_announcement")
    assert sorted(n.user_id for n in rows) == sorted([hall.asha.id, hall.bala.id, hall.chitra.id, hall.dev.id])
    assert all(n.event_id == hall.event.id and n.from_club_id == hall.club.id and n.title == "Doors for all" and n.description == "Lab 3B from 6" for n in rows)
    assert all(n.data == {"audience": "all", "has_image": True} and n.status is NotificationStatus.unread for n in rows)
    # Every device of theirs, the picture with it; never the guest who left or another event's.
    assert fcm.tokens() == ["tok-asha-phone", "tok-asha-tablet", "tok-chitra", "tok-dev"]
    (m,) = fcm.multicasts
    assert m.notification.title == "Doors for all" and m.notification.body == "Lab 3B from 6" and m.notification.image == "https://cdn.example.com/doors.png"
    # One push for all: it names their lines by the send's key, from which
    # each line's id is derived (a tap marks the reader's own).
    key = m.data.pop("notification_id")
    assert m.data == {"type": "club_announcement", "event_id": str(hall.event.id), "image_url": "https://cdn.example.com/doors.png"}
    assert all(n.id == fanout.line_id(key, n.user_id) for n in rows)


async def test_an_announcement_to_attendees_or_no_shows_reaches_only_them(client, session, hall, fcm):
    r = await _send(client, hall, "attendees")
    assert r == {"success": True, "message": "Notification sent to 2 of 2 attendees", "notifications_sent": 2}
    assert sorted(n.user_id for n in await _rows(session, title="Doors for attendees")) == sorted([hall.asha.id, hall.bala.id])
    assert fcm.tokens() == ["tok-asha-phone", "tok-asha-tablet"]
    assert (await _rows(session, title="Doors for attendees"))[0].data == {"audience": "attendees", "has_image": False}

    fcm.multicasts.clear()
    r = await _send(client, hall, "non_attendees")
    assert r == {"success": True, "message": "Notification sent to 2 of 2 non-attendees", "notifications_sent": 2}
    assert sorted(n.user_id for n in await _rows(session, title="Doors for non_attendees")) == sorted([hall.chitra.id, hall.dev.id])
    assert fcm.tokens() == ["tok-chitra", "tok-dev"]
    assert fcm.multicasts[0].data["image_url"] == ""


async def test_an_announcement_with_nobody_to_reach_says_so(client, session, hall, fcm, make_user, make_registration):
    # Nobody registered for the other event's audience of attendees.
    r = await _send(client, hall, "attendees", event=hall.other)
    assert r == {"success": False, "message": "No attendees found for this event.", "notifications_sent": 0}
    assert await _rows(session, event_id=hall.other.id) == [] and fcm.multicasts == []

    # People there, but no phone signed in: the inbox lines stay, and the club is told why nothing was pushed.
    lone = await make_user(email="nophone-announce@example.com")
    await make_registration(event=hall.other, user=lone)
    await session.execute(UserDeviceTokens.__table__.delete().where(UserDeviceTokens.user_id == hall.elsewhere.id))
    await session.commit()
    r = await _send(client, hall, "all", event=hall.other)
    assert r["success"] is False and r["notifications_sent"] == 0
    assert "Found 2 registrants" in r["message"] and "none have active push notifications" in r["message"]
    assert sorted(n.user_id for n in await _rows(session, event_id=hall.other.id)) == sorted([hall.elsewhere.id, lone.id])


async def test_only_the_events_club_may_announce(client, session, hall, fcm):
    r = await _send(client, hall, "all", headers=hall.rival)
    assert r == {"success": False, "message": "You don't have permission to send notifications for this event", "notifications_sent": 0}
    r = await client.post(f"{API}/clubs/notifications/events/987654/send", json={"title": "x", "body": "y"}, headers=hall.h)
    assert r.json() == {"success": False, "message": "Event not found", "notifications_sent": 0}
    assert await _rows(session, type="club_announcement") == [] and fcm.multicasts == []
    # Not a club account at all, or nobody: refused at the door.
    assert (await client.post(f"{API}/clubs/notifications/events/{hall.event.id}/send", json={"title": "x", "body": "y"}, headers=hall.app)).status_code == 403
    assert (await client.post(f"{API}/clubs/notifications/events/{hall.event.id}/send", json={"title": "x", "body": "y"})).status_code == 401
    # An audience that isn't one, or a title past its length, is refused.
    assert (await client.post(f"{API}/clubs/notifications/events/{hall.event.id}/send", json={"title": "x", "body": "y", "audience": "vips"}, headers=hall.h)).status_code == 422
    assert (await client.post(f"{API}/clubs/notifications/events/{hall.event.id}/send", json={"title": "x" * 256, "body": "y"}, headers=hall.h)).status_code == 422


async def test_the_events_send_stats_count_what_was_read(client, session, hall, fcm):
    await _send(client, hall, "all")
    first = (await _rows(session, user_id=hall.asha.id))[0]
    first.status = NotificationStatus.read
    await session.commit()
    stats = (await client.get(f"{API}/clubs/notifications/events/{hall.event.id}/stats", headers=hall.h)).json()
    assert stats == {"total_sent": 4, "total_read": 1, "read_percentage": 25.0}
    # Another club sees nothing of it.
    assert (await client.get(f"{API}/clubs/notifications/events/{hall.event.id}/stats", headers=hall.rival)).json() == {"total_sent": 0, "total_read": 0, "read_percentage": 0}


# ─── A volunteer told ───────────────────────────────────────────────────────


@pytest.fixture
async def crew(client, session, make_club, make_event, make_user, mint_token):
    owner, club = await make_club(name="Robotics Guild", email="crew-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Drone Day", event_datetime=NOW + timedelta(days=4))
    vani = await make_user(email="vani-crew@example.com")
    _device(session, vani, "tok-vani", platform="ios")
    await session.commit()
    return SimpleNamespace(club=club, event=event, vani=vani, h=auth_header(await mint_token(owner)))


async def _add(client, crew, email, **extra):
    r = await client.post(f"{API}/events/volunteer/add", json={"email_id": email, "event_id": crew.event.id, **extra}, headers=crew.h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_volunteer_added_to_an_event_is_told_in_the_inbox_and_on_the_phone(client, session, crew, fcm):
    listed = await _add(client, crew, "Vani-Crew@Example.com")
    assert [v["user_id"] for v in listed] == [crew.vani.id]
    (note,) = await _rows(session, user_id=crew.vani.id)
    assert note.type == "volunteer_added" and note.event_id == crew.event.id
    assert note.from_club_id == crew.club.id  # the inbox shows which club it came from
    assert note.title == "🎖️ You're now a volunteer!"
    assert note.description == "Robotics Guild added you as a volunteer for Drone Day"
    assert note.data == {"club_name": "Robotics Guild", "event_name": "Drone Day"}
    # One device: one direct send, which opens the volunteer's event.
    (m,) = fcm.singles
    assert m.token == "tok-vani" and m.notification.title == "🎖️ You're now a volunteer!"
    # It names its line: a tap marks it read.
    assert m.data == {"type": "volunteer_assigned", "event_id": str(crew.event.id), "notification_id": str(note.id)}


async def test_a_new_guest_volunteer_gets_the_inbox_line_but_no_push(client, session, crew, fcm):
    await _add(client, crew, "newcomer-crew@example.com", full_name="New Comer")
    guest = await session.scalar(select(Users).where(Users.email == "newcomer-crew@example.com"))
    assert guest.user_type is UserTypes.guest
    (note,) = await _rows(session, user_id=guest.id)
    assert note.type == "volunteer_added"
    assert fcm.singles == [] and fcm.multicasts == []


async def test_a_push_that_fails_does_not_undo_the_volunteer(client, session, crew, fcm):
    fcm.fail_single = firebase_exceptions.UnavailableError("FCM is down")
    await _add(client, crew, "vani-crew@example.com")
    assert await session.scalar(select(Volunteer).where(Volunteer.user_id == crew.vani.id, Volunteer.event_id == crew.event.id)) is not None
    assert len(await _rows(session, user_id=crew.vani.id)) == 1
    # And the device is kept: FCM being down says nothing about the token.
    assert await session.scalar(select(UserDeviceTokens).where(UserDeviceTokens.fcm_token == "tok-vani")) is not None


async def test_a_volunteer_added_twice_is_told_once(client, session, crew, fcm):
    await _add(client, crew, "vani-crew@example.com")
    again = await client.post(f"{API}/events/volunteer/add", json={"email_id": "vani-crew@example.com", "event_id": crew.event.id}, headers=crew.h)
    assert again.status_code == 400
    assert len(await _rows(session, user_id=crew.vani.id)) == 1 and len(fcm.singles) == 1


# ─── The owners' test tools ─────────────────────────────────────────────────


@pytest.fixture
async def admins(session, make_admin, make_user, mint_token):
    owner = await make_admin(email="owner-tools@example.com")
    owner2 = await make_admin(email="owner2-tools@example.com")
    support = await make_admin(email="support-tools@example.com", role=AdminRoles.support)
    app_user = await make_user(email="app-tools@example.com")
    await session.commit()
    return SimpleNamespace(
        owner=owner, h=auth_header(await mint_token(owner)), h2=auth_header(await mint_token(owner2)),
        support=auth_header(await mint_token(support)), app=auth_header(await mint_token(app_user)),
    )


async def _bulk_devices(session, n, prefix, token=lambda i, prefix: f"{prefix}-tok-{i:03d}"):
    now = datetime.now(timezone.utc)
    ids = list((await session.execute(insert(Users).returning(Users.id), [
        dict(full_name=f"{prefix} {i}", username=f"{prefix}-{i}", email=f"{prefix}-{i}@example.com", user_type=UserTypes.app_user, created_at=now, updated_at=now)
        for i in range(n)
    ])).scalars())
    await session.execute(insert(UserDeviceTokens), [dict(user_id=uid, fcm_token=token(i, prefix), platform="android" if i % 2 else "ios", created_at=now, updated_at=now) for i, uid in enumerate(ids)])
    await session.commit()
    return ids


async def test_a_push_to_every_device_is_audited_and_allowed_twice_an_hour(client, session, admins, fcm):
    await _bulk_devices(session, 3, "everyone")
    body = {"title": "Planned upgrade", "body": "Back in ten"}
    for _ in range(2):
        r = await client.post(f"{API}/notifications/test/send-all", json=body, headers=admins.h)
        assert r.status_code == 200, r.text
        assert r.json() == {"success": True, "tokens_count": 3, "sent_count": 0, "message": "Sending to 3 devices"}
    # Each device twice, from jobs that ran after the answer.
    assert fcm.tokens() == sorted([f"everyone-tok-{i:03d}" for i in range(3)] * 2)
    logs = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "notifications.broadcast")))
    assert len(logs) == 2 and all(log.actor_id == admins.owner.id and log.summary == "Pushed to every device: Planned upgrade" for log in logs)

    # A third within the hour is refused, for that owner only.
    third = await client.post(f"{API}/notifications/test/send-all", json=body, headers=admins.h)
    assert third.status_code == 429
    assert (await client.post(f"{API}/notifications/test/send-all", json=body, headers=admins.h2)).status_code == 200


async def test_a_push_to_every_device_with_none_registered_sends_nothing(client, session, admins, fcm):
    r = await client.post(f"{API}/notifications/test/send-all", json={"title": "Hi", "body": "There"}, headers=admins.h)
    assert r.json() == {"success": False, "tokens_count": 0, "sent_count": 0, "message": "No registered devices found"}
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "notifications.broadcast")) == 0
    assert fcm.multicasts == []
    # Only owners may, and never an app account.
    assert (await client.post(f"{API}/notifications/test/send-all", json={"title": "Hi", "body": "There"}, headers=admins.support)).status_code == 403
    assert (await client.post(f"{API}/notifications/test/send-all", json={"title": "Hi", "body": "There"}, headers=admins.app)).status_code == 403


async def test_the_token_list_is_the_newest_hundred_shortened_for_admins(client, session, admins):
    long_one = "x" * 80
    await _bulk_devices(session, 104, "many", token=lambda i, p: long_one if i == 103 else f"{p}-tok-{i:03d}")
    gone = await session.scalar(select(UserDeviceTokens).where(UserDeviceTokens.fcm_token == "many-tok-050"))
    gone.is_deleted = True
    await session.commit()

    r = await client.get(f"{API}/notifications/test/tokens", headers=admins.support)  # any admin
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 103  # the deleted one is not counted
    assert len(body["tokens"]) == 100
    assert body["tokens"][0]["fcm_token"] == "x" * 50 + "..."  # the newest first, cut to 50
    assert body["tokens"][1]["fcm_token"] == "many-tok-102" and body["tokens"][1]["platform"] == "ios"
    assert "many-tok-050" not in {t["fcm_token"] for t in body["tokens"]}
    assert (await client.get(f"{API}/notifications/test/tokens", headers=admins.app)).status_code == 403
    assert (await client.get(f"{API}/notifications/test/tokens")).status_code == 401


async def test_firebase_status_says_whether_the_sdk_started(client, admins, monkeypatch):
    monkeypatch.setattr(push.settings, "FIREBASE_SERVICE_ACCOUNT_PATH", "")
    monkeypatch.setattr(push.settings, "FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    monkeypatch.setattr(push, "_firebase_app", None)
    off = (await client.get(f"{API}/notifications/test/firebase-status", headers=admins.h)).json()
    assert off == {"has_path_config": False, "has_base64_config": False, "base64_length": 0, "firebase_initialized": False, "error": "Firebase initialization returned None - check credentials"}

    monkeypatch.setattr(push.settings, "FIREBASE_SERVICE_ACCOUNT_BASE64", "e30=")
    monkeypatch.setattr(push, "initialize_firebase", lambda: SimpleNamespace(project_id="moa-test"))
    on = (await client.get(f"{API}/notifications/test/firebase-status", headers=admins.h)).json()
    assert on["firebase_initialized"] is True and on["project_id"] == "moa-test" and on["error"] is None
    assert on["has_base64_config"] is True and on["base64_length"] == 4

    def broken():
        raise RuntimeError("bad key")

    monkeypatch.setattr(push, "initialize_firebase", broken)
    assert (await client.get(f"{API}/notifications/test/firebase-status", headers=admins.h)).json()["error"] == "bad key"
    assert (await client.get(f"{API}/notifications/test/firebase-status", headers=admins.app)).status_code == 403


async def test_one_push_to_one_token_for_owners(client, admins, fcm, monkeypatch):
    params = {"token": "tok-debug", "title": "Ping", "body": "Pong"}
    ok = await client.post(f"{API}/notifications/test/send-single", params=params, headers=admins.h)
    assert ok.json() == {"success": True, "message_id": "projects/test/messages/1"}
    (m,) = fcm.singles
    assert m.token == "tok-debug" and m.notification.title == "Ping" and m.notification.body == "Pong"
    # A token alone says nobody's unread count: no badge, not a made-up 1.
    assert messaging._MessagingService.encode_message(m)["apns"]["payload"]["aps"] == {"sound": "default"}

    fcm.fail_single = messaging.UnregisteredError("Requested entity was not found.")
    failed = (await client.post(f"{API}/notifications/test/send-single", params=params, headers=admins.h)).json()
    assert failed["success"] is False and failed["error_type"] == "UnregisteredError"

    monkeypatch.setattr(push, "initialize_firebase", lambda: None)
    assert (await client.post(f"{API}/notifications/test/send-single", params=params, headers=admins.h)).json() == {"success": False, "error": "Firebase not initialized"}
    assert (await client.post(f"{API}/notifications/test/send-single", params=params, headers=admins.support)).status_code == 403


# ─── The bell ───────────────────────────────────────────────────────────────


@pytest.fixture
def clock(monkeypatch):
    """Each notice a minute after the last, so their order is known."""
    t = {"now": NOW - timedelta(hours=1)}

    def tick():
        t["now"] += timedelta(minutes=1)
        return t["now"]

    monkeypatch.setattr(alerts, "_now", tick)
    return t


@pytest.fixture
async def bell(client, session, make_club, make_user, mint_token, clock):
    owner, club = await make_club(name="Bell Club", email="bell-owner@example.com")
    rival_owner, rival = await make_club(name="Other Bell", email="bell-rival@example.com")
    app_user = await make_user(email="bell-app@example.com")
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, rival=rival, h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)), app=auth_header(await mint_token(app_user)))


async def _bell(client, headers, **params):
    r = await client.get(f"{API}/clubs/alerts", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_bell_shows_the_clubs_notices_newest_first_with_the_unread_count(client, session, bell):
    first = await alerts.notify(session, bell.club.id, "message_quota_80", title="80% of your emails", body="Ten left", link="/billing", data={"used": 80}, dedupe_key="q80:2026-10")
    second = await alerts.notify(session, bell.club.id, "message_quota_100", title="All your emails", link="/billing", dedupe_key="q100:2026-10")
    third = await alerts.notify(session, bell.club.id, "whatsapp_quality", title="Quality dropped")
    # The same dedupe key again is nothing.
    assert await alerts.notify(session, bell.club.id, "message_quota_80", title="80% again", dedupe_key="q80:2026-10") is None
    await alerts.notify(session, bell.rival.id, "message_quota_80", title="Not yours")
    await session.commit()
    assert None not in (first, second, third)

    b = await _bell(client, bell.h)
    assert [a["title"] for a in b["items"]] == ["Quality dropped", "All your emails", "80% of your emails"]
    assert b["unread"] == 3
    q80 = b["items"][2]
    assert q80 == {**q80, "id": first, "kind": "message_quota_80", "body": "Ten left", "link": "/billing", "data": {"used": 80}, "read": False}
    assert b["items"][0]["link"] is None and b["items"][0]["data"] == {} and b["items"][0]["body"] == ""
    # A shorter bell still counts every unread one.
    short = await _bell(client, bell.h, limit=1)
    assert [a["title"] for a in short["items"]] == ["Quality dropped"] and short["unread"] == 3
    assert (await client.get(f"{API}/clubs/alerts", params={"limit": 101}, headers=bell.h)).status_code == 422

    # Only club accounts have a bell.
    assert (await client.get(f"{API}/clubs/alerts", headers=bell.app)).status_code == 403
    assert (await client.get(f"{API}/clubs/alerts")).status_code == 401
    assert [a["title"] for a in (await _bell(client, bell.rival_h))["items"]] == ["Not yours"]


async def test_notices_are_marked_read_one_by_one_or_all_at_once(client, session, bell):
    ids = [await alerts.notify(session, bell.club.id, "message_quota_80", title=f"Notice {i}") for i in range(3)]
    theirs = await alerts.notify(session, bell.rival.id, "message_quota_80", title="Theirs")
    await session.commit()

    one = await client.post(f"{API}/clubs/alerts/read", json={"ids": [ids[0], theirs]}, headers=bell.h)
    assert one.json() == {"marked": 1}  # another club's notice is not this club's to mark
    b = await _bell(client, bell.h)
    assert b["unread"] == 2 and {a["title"]: a["read"] for a in b["items"]} == {"Notice 0": True, "Notice 1": False, "Notice 2": False}
    assert (await _bell(client, bell.rival_h))["unread"] == 1

    assert (await client.post(f"{API}/clubs/alerts/read", json={}, headers=bell.h)).json() == {"marked": 2}
    assert (await client.post(f"{API}/clubs/alerts/read", json={"ids": []}, headers=bell.h)).json() == {"marked": 0}
    assert (await _bell(client, bell.h))["unread"] == 0
    assert (await client.post(f"{API}/clubs/alerts/read", json={"ids": list(range(201))}, headers=bell.h)).status_code == 422


async def test_a_notice_goes_with_the_change_it_is_about(session, bell):
    # Written in the caller's transaction: rolled back with it, it never shows.
    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await alerts.notify(session, bell.club.id, "message_quota_80", title="Rolled back")
            raise RuntimeError("the send failed")
    assert (await alerts.listing(session, bell.club.id))["items"] == []
    # And it never raises: a notice for a club that doesn't exist is just not written.
    assert await alerts.notify(session, 987654321, "message_quota_80", title="Nobody's") is None
    # Over-long parts are cut to fit, not refused.
    nid = await alerts.notify(session, bell.club.id, "k" * 60, title="t" * 300, dedupe_key="d" * 300)
    row = await session.get(ClubAlerts, nid)
    assert len(row.kind) == 40 and len(row.title) == 200 and len(row.dedupe_key) == 160


@pytest.fixture
def invites(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    return sent


async def _member(client, owner_h, sent, email, role):
    r = await client.post(f"{API}/clubs/access/invite", json={"email": email, "role": role}, headers=owner_h)
    assert r.status_code == 200, r.text
    token = re.search(r"token=([A-Za-z0-9_\-]+)", sent[-1]["text"]).group(1)
    assert (await client.post(f"{API}/clubs/access/invite/{token}/accept", json={"password": "Member-pass-1"})).status_code == 200
    signed = await client.post(f"{API}/auth/token", data={"username": email, "password": "Member-pass-1", "scope": "club"})
    assert signed.status_code == 200, signed.text
    return auth_header(signed.json()["access_token"])


async def test_the_whole_team_reads_one_bell(client, session, bell, invites):
    await alerts.notify(session, bell.club.id, "message_quota_80", title="80% of your emails")
    await alerts.notify(session, bell.club.id, "message_quota_100", title="All your emails")
    await session.commit()
    editor = await _member(client, bell.h, invites, "editor-bell@example.com", "editor")
    door = await _member(client, bell.h, invites, "door-bell@example.com", "door")

    for h in (editor, door):
        b = await _bell(client, h)
        assert [a["title"] for a in b["items"]] == ["All your emails", "80% of your emails"] and b["unread"] == 2
    # One person reading a notice reads it for everyone.
    newest = (await _bell(client, door))["items"][0]["id"]
    assert (await client.post(f"{API}/clubs/alerts/read", json={"ids": [newest]}, headers=door)).json() == {"marked": 1}
    assert (await _bell(client, bell.h))["unread"] == 1
    assert (await client.post(f"{API}/clubs/alerts/read", json={}, headers=editor)).json() == {"marked": 1}
    assert (await _bell(client, bell.h))["unread"] == 0 and (await _bell(client, door))["unread"] == 0


# ─── The older push history ──────────────────────────────────────────────────


async def test_the_old_push_history_is_one_row_per_send_newest_first(client, session, hall):
    """Pinned as it is: every announcement row of the club's events is read
    into Python and grouped by (title, event, clock hour) there, before
    paging. The answer is right; the work grows with every push ever sent."""
    e1, e2 = hall.event, hall.other
    users = [hall.asha, hall.bala, hall.chitra, hall.dev]
    base = datetime(2026, 10, 20, 10, 35, tzinfo=timezone.utc)  # 16:05 in India: inside one clock hour either way

    def rows(event, title, when, n, read=0, kind="club_announcement", **kw):
        for i in range(n):
            session.add(Notifications(
                user_id=users[i].id, title=title, description=f"{title} body", type=kind, event_id=event.id, from_club_id=hall.club.id,
                status=NotificationStatus.read if i < read else NotificationStatus.unread, created_at=when, updated_at=when, **kw,
            ))

    rows(e1, "Doors open", base, 3, read=1)
    rows(e1, "Doors open", base + timedelta(minutes=20), 1, read=1)  # the same send, finished in the same hour
    rows(e2, "Doors open", base + timedelta(hours=2), 1)
    rows(e2, "Venue change", base + timedelta(hours=4), 2, read=2)
    rows(e1, "You're now a volunteer!", base + timedelta(hours=5), 1, kind="volunteer_added")  # not an announcement
    rows(e1, "Withdrawn", base + timedelta(hours=6), 2, is_deleted=True)  # dismissed lines are gone
    gone = hall.other.__class__
    deleted_event = gone(slug=f"gone-{hall.club.id}", name="Cancelled", images=[], event_datetime=base, duration=1.0, club_id=hall.club.id, category_id=e1.category_id, is_deleted=True)
    session.add(deleted_event)
    await session.flush()
    rows(deleted_event, "For a cancelled event", base + timedelta(hours=7), 1)
    await session.commit()

    page = (await client.get(f"{API}/clubs/notifications/history", headers=hall.h)).json()
    assert page["total"] == 3
    got = [(i["title"], i["event_id"], i["sent_count"], i["read_count"], i["read_percentage"]) for i in page["items"]]
    assert got == [
        ("Venue change", e2.id, 2, 2, 100.0),
        ("Doors open", e2.id, 1, 0, 0.0),
        ("Doors open", e1.id, 4, 2, 50.0),
    ]
    first = page["items"][0]
    assert first["event_name"] == "Next Month" and first["description"] == "Venue change body"
    assert datetime.fromisoformat(first["created_at"]) == base + timedelta(hours=4)
    # A group is dated by its latest line.
    assert datetime.fromisoformat(page["items"][2]["created_at"]) == base + timedelta(minutes=20)

    # Pages over the groups, with the total over all of them; each row's id is
    # its event and its place in the whole list, so no two rows share one.
    p1 = (await client.get(f"{API}/clubs/notifications/history", params={"limit": 2}, headers=hall.h)).json()
    p2 = (await client.get(f"{API}/clubs/notifications/history", params={"limit": 2, "offset": 2}, headers=hall.h)).json()
    assert [i["title"] for i in p1["items"]] == ["Venue change", "Doors open"] and p1["total"] == p2["total"] == 3
    assert [(i["title"], i["event_id"]) for i in p2["items"]] == [("Doors open", e1.id)]
    one_by_one = [
        (await client.get(f"{API}/clubs/notifications/history", params={"limit": 1, "offset": k}, headers=hall.h)).json()["items"][0]["id"]
        for k in range(3)
    ]
    assert len(set(one_by_one)) == 3

    # Another club's history has none of it; an account with no club has none at all.
    assert (await client.get(f"{API}/clubs/notifications/history", headers=hall.rival)).json() == {"items": [], "total": 0}
    assert (await client.get(f"{API}/clubs/notifications/history", headers=hall.app)).status_code == 403
