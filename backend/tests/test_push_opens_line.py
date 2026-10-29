"""A tap on a push marks its inbox line read, so the app's icon badge drops.

Every push that has an inbox line names it in its data as `notification_id`
(fanout.PUSH_LINE), and the app posts it to POST /notifications/read/{id}:

- a line written for one person: its own id;
- the lines a fan-out writes for many (a club's announcement, a followed
  club's new event, a broadcast device by device): one push, so it carries
  the fan-out's key, and each line's id is derived from the key and its
  person (fanout.line_id). The endpoint finds the reader's own line from
  the key, never anyone else's;
- a shared broadcast's line (INBOX_SHARED): the broadcast's id, as before;
- a push with no line (a waiting-list offer, a push to every device):
  nothing.

FCM is faked at firebase-admin's calls, as in tests/test_push_badge.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from firebase_admin import messaging
from sqlalchemy import func, select

from app.api.notifications import service as inbox
from app.api.notifications.models import NotificationStatus, Notifications
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core import jobs
from app.core.notifications import fanout, triggers
from app.core.notifications import service as push
from tests.conftest import auth_header

API = "/api/v1"


def _ok(n: int) -> SimpleNamespace:
    return SimpleNamespace(success_count=n, failure_count=0, responses=[SimpleNamespace(success=True, exception=None) for _ in range(n)])


class Fcm:
    """What each device (or topic) would be sent: its data."""

    def __init__(self):
        self.data: dict[str, dict] = {}

    def _keep(self, message):
        self.data[message.token or message.topic or message.condition] = dict(message.data or {})

    def send(self, message):
        self._keep(message)
        return "projects/test/messages/1"

    def send_each_for_multicast(self, m):
        for token in m.tokens:
            self.data[token] = dict(m.data or {})
        return _ok(len(m.tokens))

    def send_each(self, messages):
        for message in messages:
            self._keep(message)
        return _ok(len(messages))


@pytest.fixture
def fcm(monkeypatch):
    fake = Fcm()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", fake.send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.send_each_for_multicast)
    monkeypatch.setattr(messaging, "send_each", fake.send_each)
    return fake


@pytest.fixture
def flags(monkeypatch):
    for k, v in dict(PUSH_TOPICS=False, INBOX_SHARED=False).items():
        monkeypatch.setattr(settings, k, v)
    return settings


async def _person(session, make_user, mint_token, name: str, token: str):
    user = await make_user(email=f"{name}-line@example.com", username=f"{name}-line")
    session.add(UserDeviceTokens(user_id=user.id, fcm_token=token, platform="ios"))
    await session.commit()
    return SimpleNamespace(user=user, id=user.id, h=auth_header(await mint_token(user)), token=token)


async def _open(client, person, line_id: str):
    """What the app does on a tap: mark the line the push named."""
    return await client.post(f"{API}/notifications/read/{line_id}", headers=person.h)


async def _status(session, line_id) -> NotificationStatus:
    row = await session.scalar(
        select(Notifications).where(Notifications.id == uuid.UUID(str(line_id))).execution_options(populate_existing=True)
    )
    return row.status


# ─── One person's line ──────────────────────────────────────────────────────


async def test_a_push_to_one_person_names_their_line_and_a_tap_reads_it(client, session, make_user, make_club, make_event, mint_token, fcm, flags):
    owner, club = await make_club(name="Line Club", email="line-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Line Night")
    asha = await _person(session, make_user, mint_token, "asha", "tok-line-asha")

    assert await triggers.notify_user_added_as_volunteer(
        session, user_id=asha.id, event_id=event.id, event_name="Line Night", club_name="Line Club", club_id=club.id,
    ) is True
    data = fcm.data["tok-line-asha"]
    line = await session.scalar(select(Notifications).where(Notifications.user_id == asha.id, Notifications.type == "volunteer_added"))
    assert data["notification_id"] == str(line.id)
    assert data["type"] == "volunteer_assigned" and data["event_id"] == str(event.id)  # what it always carried
    assert await inbox.get_unread_count(session, asha.id) == 1

    r = await _open(client, asha, data["notification_id"])
    assert r.status_code == 200 and r.json()["status"] == "read"
    assert await inbox.get_unread_count(session, asha.id) == 0
    # Tapped twice (or the inbox opened it too): still read, no error.
    assert (await _open(client, asha, data["notification_id"])).status_code == 200


# ─── A fan-out's lines ──────────────────────────────────────────────────────


@pytest.fixture
async def hall(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Line Society", email="line-society@example.com")
    event = await make_event(club=club, created_by=owner, name="Line Hack")
    people = []
    for name in ("ravi", "mira"):
        p = await _person(session, make_user, mint_token, name, f"tok-line-{name}")
        await make_registration(event=event, user=p.user)
        people.append(p)
    outsider = await _person(session, make_user, mint_token, "omar", "tok-line-omar")
    await session.commit()
    return SimpleNamespace(club=club, event=event, ravi=people[0], mira=people[1], outsider=outsider)


async def test_an_announcement_names_each_persons_line_by_one_key(client, session, hall, fcm, flags):
    sent, told = await triggers.notify_event_participants(session, hall.event.id, "Doors at 6", "Lab 3B", club_id=hall.club.id)
    assert sent == 2 and sorted(told) == sorted([hall.ravi.id, hall.mira.id])

    # One push for both: the same key, which is no line's id.
    key = fcm.data["tok-line-ravi"]["notification_id"]
    assert fcm.data["tok-line-mira"]["notification_id"] == key
    assert await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.id == uuid.UUID(key))) == 0
    # Each line's id is derived from the key and its person.
    for p in (hall.ravi, hall.mira):
        assert await _status(session, fanout.line_id(key, p.id)) is NotificationStatus.unread

    r = await _open(client, hall.ravi, key)
    assert r.status_code == 200 and r.json()["id"] == str(fanout.line_id(key, hall.ravi.id)) and r.json()["status"] == "read"
    assert await _status(session, fanout.line_id(key, hall.ravi.id)) is NotificationStatus.read
    # Only the reader's own line.
    assert await _status(session, fanout.line_id(key, hall.mira.id)) is NotificationStatus.unread
    assert (await _open(client, hall.outsider, key)).status_code == 404
    # Someone else's line by its own id is still refused.
    assert (await _open(client, hall.outsider, str(fanout.line_id(key, hall.mira.id)))).status_code == 403


async def test_the_derived_ids_match_and_a_fan_out_written_again_adds_nothing(session, hall):
    key = uuid.uuid4()
    audience = fanout.users_in([hall.ravi.id, hall.mira.id, hall.ravi.id])
    reached = await fanout.insert_for_audience(session, audience, title="T", description="B", type="club_announcement", line_key=key)
    assert sorted(reached) == sorted([hall.ravi.id, hall.mira.id])
    again = await fanout.insert_for_audience(session, audience, title="T", description="B", type="club_announcement", line_key=key)
    assert again == []
    ids = set((await session.scalars(select(Notifications.id).where(Notifications.type == "club_announcement"))).all())
    assert ids == {fanout.line_id(key, hall.ravi.id), fanout.line_id(key, hall.mira.id)}


async def test_a_followed_clubs_new_event_names_the_line_by_its_event(client, session, make_club, make_event, make_user, mint_token, fcm, flags):
    from app.api.clubs.models import ClubUsersLink

    owner, club = await make_club(name="Follow Line", email="follow-line@example.com")
    fan = await _person(session, make_user, mint_token, "fan", "tok-line-fan")
    session.add(ClubUsersLink(user_id=fan.id, club_id=club.id, is_following=True))
    event = await make_event(club=club, created_by=owner, name="Robot Fair")
    await session.commit()

    await triggers.notify_followers_of_new_event(session, club.id, club.name, event.id, event.name)
    await jobs.settle()
    key = fcm.data["tok-line-fan"]["notification_id"]
    assert key == str(fanout.line_key("new_event", event.id))
    assert (await _open(client, fan, key)).json()["status"] == "read"
    assert await inbox.get_unread_count(session, fan.id) == 0


async def test_a_broadcast_device_by_device_names_the_line_by_the_broadcast(client, session, make_club, make_user, mint_token, fcm, flags):
    from app.api.clubs.models import ClubUsersLink
    from app.api.notifications.models import NotificationBroadcasts

    _, club = await make_club(name="Post Line", email="post-line@example.com")
    fan = await _person(session, make_user, mint_token, "reader", "tok-line-reader")
    session.add(ClubUsersLink(user_id=fan.id, club_id=club.id, is_following=True))
    await session.commit()

    assert await fanout.announce(session, fanout.Broadcast(
        kind="club_thread", ref="line-post", title="Post Line posted", body="Lab open", club_id=club.id, from_club_id=club.id,
    ), topic="club_x")
    await jobs.settle()
    broadcast = await session.scalar(select(NotificationBroadcasts).where(NotificationBroadcasts.push_id == "club_thread:line-post"))
    data = fcm.data["tok-line-reader"]
    assert data["notification_id"] == str(broadcast.id) and data["push_id"] == "club_thread:line-post"
    assert (await _open(client, fan, data["notification_id"])).json()["status"] == "read"
    assert await inbox.get_unread_count(session, fan.id) == 0


async def test_a_topic_send_carries_the_key_too(session, make_club, fcm, monkeypatch):
    """One message for every subscriber: the same key reaches each, and each
    reader's tap finds their own line."""
    monkeypatch.setattr(settings, "PUSH_TOPICS", True)
    monkeypatch.setattr(settings, "PUSH_TOPICS_LEGACY", False)
    monkeypatch.setattr(settings, "INBOX_SHARED", False)
    _, club = await make_club(name="Topic Line", email="topic-line@example.com")
    assert await fanout.announce(session, fanout.Broadcast(
        kind="club_thread", ref="topic-post", title="Topic Line posted", body="Hi", club_id=club.id, from_club_id=club.id,
    ), topic="club_topic_line")
    await jobs.settle()
    from app.api.notifications.models import NotificationBroadcasts

    broadcast = await session.scalar(select(NotificationBroadcasts).where(NotificationBroadcasts.push_id == "club_thread:topic-post"))
    assert fcm.data["club_topic_line"]["notification_id"] == str(broadcast.id)


async def test_with_the_shared_inbox_the_key_is_the_shared_lines_id(client, session, make_club, make_user, mint_token, fcm, monkeypatch):
    from app.api.clubs.models import ClubUsersLink

    monkeypatch.setattr(settings, "PUSH_TOPICS", False)
    monkeypatch.setattr(settings, "INBOX_SHARED", True)
    _, club = await make_club(name="Shared Line", email="shared-line@example.com")
    fan = await _person(session, make_user, mint_token, "sharer", "tok-line-sharer")
    session.add(ClubUsersLink(user_id=fan.id, club_id=club.id, is_following=True))
    await session.commit()

    assert await fanout.announce(session, fanout.Broadcast(
        kind="club_thread", ref="shared-post", title="Shared Line posted", body="Hi", club_id=club.id, from_club_id=club.id,
    ), topic="club_y")
    await jobs.settle()
    assert await inbox.get_unread_count(session, fan.id) == 1
    key = fcm.data["tok-line-sharer"]["notification_id"]
    r = await _open(client, fan, key)
    assert r.status_code == 200 and r.json()["id"] == key and r.json()["status"] == "read"
    assert await inbox.get_unread_count(session, fan.id) == 0


# ─── No line, nothing named ─────────────────────────────────────────────────


async def test_a_push_with_no_line_names_none(session, make_user, mint_token, fcm, flags):
    lone = await _person(session, make_user, mint_token, "lone", "tok-line-lone")
    # A waiting-list offer: a push and no inbox line.
    assert await push.send_notification_to_user(session, lone.id, "A place opened up", "Tap", data={"type": "waitlist_offer"})
    assert "notification_id" not in fcm.data["tok-line-lone"]

    # The platform's announcement to every device: no line either.
    assert await fanout.announce(session, fanout.Broadcast(
        kind="platform", ref="line-all", title="Hello", body="Everyone", everyone=True,
    ), topic="all")
    await jobs.settle()
    assert "notification_id" not in fcm.data["tok-line-lone"]
