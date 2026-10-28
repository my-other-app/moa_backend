"""The number on an iPhone's app icon: APNs' `badge` in a push is the
person's unread inbox count, what GET /notifications/unread-count says.

Every push used to carry `badge: 1`, and the app never set the number
itself, so after one push the icon said 1 for good. Now:

- a push to one person carries their count, the push's own line included
  (it is written before the push goes);
- a push to many gives each device its person's count, from one count per
  lookup batch, still FCM_BATCH devices a call: a batch of one count is a
  multicast, a batch of several is one send_each of a message per device;
- nothing unread is `badge: 0`, which takes the number off the icon;
- with INBOX_SHARED a push to many carries no badge at all (the shared part
  of each count is a query per person), and neither does a send to an FCM
  topic: the app sets the number when it is opened.

FCM is faked at firebase-admin's calls (send, send_each_for_multicast,
send_each), and each device's message is encoded as firebase-admin would
send it, so the badge checked is the one in the APNs payload.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from firebase_admin import messaging

from app.api.notifications import service as inbox
from app.api.notifications.models import NotificationStatus, Notifications
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core.notifications import service as push
from app.core.notifications import triggers

NOW = datetime.now(timezone.utc)


def _aps(message: messaging.Message) -> dict:
    """The `aps` dictionary of the APNs payload firebase-admin builds."""
    return messaging._MessagingService.encode_message(message)["apns"]["payload"]["aps"]


def _ok(n: int) -> SimpleNamespace:
    return SimpleNamespace(
        success_count=n, failure_count=0, responses=[SimpleNamespace(success=True, exception=None) for _ in range(n)],
    )


class Fcm:
    """firebase-admin's three send calls: which call each batch was, and the
    `aps` each device (or topic) would be sent."""

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.aps: dict[str, dict] = {}

    def _keep(self, message: messaging.Message) -> None:
        self.aps[message.token or message.topic or message.condition] = _aps(message)

    def send(self, message):
        self.calls.append(("send", 1))
        self._keep(message)
        return f"projects/test/messages/{len(self.calls)}"

    def send_each_for_multicast(self, m):
        # As firebase-admin does: the same message for each of the tokens.
        self.calls.append(("multicast", len(m.tokens)))
        for token in m.tokens:
            self._keep(messaging.Message(data=m.data, notification=m.notification, android=m.android, apns=m.apns, token=token))
        return _ok(len(m.tokens))

    def send_each(self, messages):
        self.calls.append(("send_each", len(messages)))
        for message in messages:
            self._keep(message)
        return _ok(len(messages))

    def badge(self, key: str):
        """The badge a device was sent, or "none" when the key was left out."""
        return self.aps[key].get("badge", "none")


@pytest.fixture
def fcm(monkeypatch):
    fake = Fcm()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", fake.send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.send_each_for_multicast)
    monkeypatch.setattr(messaging, "send_each", fake.send_each)
    return fake


async def _person(session, make_user, name: str, *tokens: str, unread: int = 0, read: int = 0):
    user = await make_user(email=f"{name}-badge@example.com", username=f"{name}-badge")
    for token in tokens:
        session.add(UserDeviceTokens(user_id=user.id, fcm_token=token, platform="ios"))
    for i in range(unread + read):
        session.add(Notifications(
            user_id=user.id, title=f"Earlier {i}", description="", type="club_announcement",
            status=NotificationStatus.unread if i < unread else NotificationStatus.read,
            created_at=NOW - timedelta(hours=i + 1),
        ))
    await session.commit()
    return user


# ─── A push to one person ───────────────────────────────────────────────────


async def test_a_push_to_one_person_carries_their_unread_count_with_its_own_line(session, make_user, make_club, make_event, fcm):
    owner, club = await make_club(name="Badge Club", email="badge-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Badge Night")
    asha = await _person(session, make_user, "asha", "tok-asha-iphone", unread=2, read=3)
    assert await inbox.get_unread_count(session, asha.id) == 2

    # The trigger writes the inbox line, then pushes: the count holds it.
    assert await triggers.notify_user_added_as_volunteer(
        session, user_id=asha.id, event_id=event.id, event_name="Badge Night", club_name="Badge Club", club_id=club.id,
    ) is True
    assert await inbox.get_unread_count(session, asha.id) == 3
    assert fcm.calls == [("send", 1)]
    assert fcm.badge("tok-asha-iphone") == 3
    assert fcm.aps["tok-asha-iphone"]["sound"] == "default"


async def test_every_device_of_one_person_gets_the_same_count_in_one_multicast(session, make_user, fcm):
    ravi = await _person(session, make_user, "ravi", "tok-ravi-iphone", "tok-ravi-ipad", unread=4)

    assert await push.send_notification_to_user(session, ravi.id, "Your place", "Tap to claim it") is True
    assert fcm.calls == [("multicast", 2)]
    assert fcm.badge("tok-ravi-iphone") == fcm.badge("tok-ravi-ipad") == 4


async def test_nothing_unread_is_a_badge_of_zero_never_one(session, make_user, fcm):
    """A push with no inbox line of its own (a waiting-list offer) to
    someone who has read everything: 0, which clears the icon."""
    mira = await _person(session, make_user, "mira", "tok-mira", read=2)

    assert await push.send_notification_to_user(session, mira.id, "A place opened up", "Tap to claim it") is True
    assert fcm.badge("tok-mira") == 0


async def test_a_count_that_cannot_be_read_sends_no_badge_and_the_push_still_goes(session, make_user, fcm, monkeypatch):
    from sqlalchemy import select, text

    nina = await _person(session, make_user, "nina", "tok-nina", unread=1)

    async def broken(session, user_id):
        await session.execute(text("SELECT 1 / 0"))  # the database refuses the statement


    monkeypatch.setattr(inbox, "get_unread_count", broken)
    assert await push.send_notification_to_user(session, nina.id, "Hello", "World") is True
    assert fcm.badge("tok-nina") == "none"
    # The savepoint kept the caller's transaction usable.
    assert await session.scalar(select(UserDeviceTokens.fcm_token).where(UserDeviceTokens.user_id == nina.id)) == "tok-nina"


async def test_with_the_shared_inbox_one_person_still_gets_their_whole_count(session, make_user, fcm, monkeypatch):
    monkeypatch.setattr(settings, "INBOX_SHARED", True)
    omar = await _person(session, make_user, "omar", "tok-omar", unread=2)

    assert await push.send_notification_to_user(session, omar.id, "Hello", "World") is True
    assert fcm.badge("tok-omar") == await inbox.get_unread_count(session, omar.id) == 2


# ─── A push to many ─────────────────────────────────────────────────────────


async def test_a_push_to_many_gives_each_device_its_persons_count_in_batches(session, make_user, fcm, monkeypatch):
    monkeypatch.setattr(push, "FCM_BATCH", 2)
    asha = await _person(session, make_user, "many-asha", "tok-m-asha-1", "tok-m-asha-2", unread=1)
    bala = await _person(session, make_user, "many-bala", "tok-m-bala")
    chitra = await _person(session, make_user, "many-chitra", "tok-m-chitra", unread=4, read=1)
    dev = await _person(session, make_user, "many-dev", unread=7)  # no device: not pushed, not counted
    ids = [asha.id, bala.id, chitra.id, dev.id]

    # As the club's announcement does: everyone's line, then the push.
    await inbox.create_notifications_batch(session, user_ids=ids, title="Doors at 6", description="Lab 3B", type="club_announcement")
    assert await push.send_notification_to_users(session, ids, "Doors at 6", "Lab 3B") == 4

    assert fcm.badge("tok-m-asha-1") == fcm.badge("tok-m-asha-2") == 2
    assert fcm.badge("tok-m-bala") == 1
    assert fcm.badge("tok-m-chitra") == 5
    # Each the number the app reads for that person.
    for user, token in ((asha, "tok-m-asha-1"), (bala, "tok-m-bala"), (chitra, "tok-m-chitra")):
        assert fcm.badge(token) == await inbox.get_unread_count(session, user.id)
    # Still FCM_BATCH devices a call, in device order: Asha's two share a
    # count (one multicast); Bala's and Chitra's differ (one send_each).
    assert fcm.calls == [("multicast", 2), ("send_each", 2)]


async def test_a_push_to_many_with_nothing_unread_sends_zeros(session, make_user, fcm):
    """The pushes that write no line (a live update's re-send, say) to people
    who have read everything: each badge 0, never 1."""
    a = await _person(session, make_user, "zero-a", "tok-z-a", read=1)
    b = await _person(session, make_user, "zero-b", "tok-z-b")

    assert await push.send_notification_to_users(session, [a.id, b.id], "Hello", "World") == 2
    assert fcm.calls == [("multicast", 2)]
    assert fcm.badge("tok-z-a") == fcm.badge("tok-z-b") == 0


async def test_with_the_shared_inbox_a_push_to_many_carries_no_badge(session, make_user, fcm, monkeypatch):
    monkeypatch.setattr(settings, "INBOX_SHARED", True)
    a = await _person(session, make_user, "shared-a", "tok-s-a", unread=1)
    b = await _person(session, make_user, "shared-b", "tok-s-b", unread=3)

    assert await push.send_notification_to_users(session, [a.id, b.id], "Hello", "World") == 2
    assert fcm.calls == [("multicast", 2)]
    assert fcm.badge("tok-s-a") == fcm.badge("tok-s-b") == "none"


async def test_a_broadcast_device_by_device_gives_each_its_count(session, make_user, make_club, fcm, monkeypatch):
    """A club's new event to its followers, one row each and a push per
    device (PUSH_TOPICS and INBOX_SHARED off): the push.devices job counts
    the slice's people once."""
    from app.api.clubs.models import ClubUsersLink
    from app.core import jobs
    from app.core.notifications import fanout

    monkeypatch.setattr(settings, "PUSH_TOPICS", False)
    monkeypatch.setattr(settings, "INBOX_SHARED", False)
    _, club = await make_club(name="Follow Club", email="badge-follow@example.com")
    keen = await _person(session, make_user, "keen", "tok-keen", unread=2)
    calm = await _person(session, make_user, "calm", "tok-calm")
    for user in (keen, calm):
        session.add(ClubUsersLink(user_id=user.id, club_id=club.id, is_following=True))
    await session.commit()

    assert await fanout.announce(session, fanout.Broadcast(
        kind="club_thread", ref="badge-post", title="Follow Club posted", body="Lab open till 9", club_id=club.id, from_club_id=club.id,
    ), topic="club_x") is True
    await jobs.settle()
    assert fcm.badge("tok-keen") == 3
    assert fcm.badge("tok-calm") == 1


# ─── Sends with no one person ───────────────────────────────────────────────


async def test_a_topic_send_carries_no_badge(fcm):
    assert await push.send_to_topic(topic="club_12", title="New event", body="Hack Night", data={"push_id": "new_event:1"})
    assert fcm.badge("club_12") == "none"
    assert fcm.aps["club_12"]["sound"] == "default"
