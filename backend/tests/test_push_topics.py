"""Push by FCM topics (feat/w2-push).

Which pushes are broadcasts; with PUSH_TOPICS off everything goes device by
device exactly as before, and on, a broadcast is one topic send plus the
devices that do not subscribe yet, never both to one device, with the same
push_id however it goes; the shared inbox answers exactly what the rows per
recipient did; the topics list and its version; mutes and switches; the
capability a device reports; the jobs' retries. FCM is never called: the
messaging module is faked.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from firebase_admin import exceptions as firebase_exceptions, messaging
from sqlalchemy import delete, event, func, select, text, update

from app.api.clubs.models import ClubUsersLink
from app.api.interests.models import InterestCategory, Interests
from app.api.notifications.models import (
    NotificationBroadcasts,
    NotificationBroadcastTargets,
    NotificationType,
    Notifications,
)
from app.api.threads import service as threads_service
from app.api.threads.models import ClubThreads
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core import jobs
from app.core.notifications import delivery, fanout, inbox, service as push, topics, triggers
from tests.conftest import auth_header

API = "/api/v1"


# ─── Fakes ────────────────────────────────────────────────────────────────


class FakeMessaging:
    """firebase_admin.messaging's sends, recorded. `fail`: exceptions the
    next topic sends raise, in order."""

    def __init__(self):
        self.sent: list = []
        self.multicasts: list = []
        self.unsubscribed: list = []
        self.fail: list = []

    def send(self, message):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(message)
        return f"projects/test/messages/{len(self.sent)}"

    def send_each_for_multicast(self, message):
        self.multicasts.append(message)
        return SimpleNamespace(success_count=len(message.tokens), failure_count=0, responses=[])

    def unsubscribe_from_topic(self, tokens, topic):
        self.unsubscribed.append((sorted(tokens), topic))
        return SimpleNamespace(success_count=len(tokens), errors=[])

    def device_tokens(self) -> list[str]:
        return sorted(t for m in self.multicasts for t in m.tokens)


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeMessaging()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", fake.send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.send_each_for_multicast)
    monkeypatch.setattr(messaging, "unsubscribe_from_topic", fake.unsubscribe_from_topic)
    return fake


@pytest.fixture
def flags(monkeypatch):
    def set_(**values):
        for k, v in values.items():
            monkeypatch.setattr(settings, k, v)

    set_(PUSH_TOPICS=False, INBOX_SHARED=False, PUSH_TOPICS_LEGACY=True)
    return set_


class Clock:
    """fanout's clock: the inbox rows' and broadcasts' times, set by the test."""

    def __init__(self, at: datetime):
        self.at = at

    def now(self, tz=None):
        return self.at


@pytest.fixture
async def scene(client, session, make_club, make_user, mint_token, monkeypatch, fcm, flags):
    """Circuit Society, followed by Asha (a device that subscribes to topics)
    and Ravi (an old app). Drone Club, followed by Ravi. The topic Robotics:
    Asha and Meera (an old app) picked an interest under it, and so did Ravi
    -- who follows Drone Club, so a Drone Club event by topic is not his."""

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(threads_service, "session_scope", scope)
    owner, club = await make_club(name="Circuit Society", email="circuit-push@example.com")
    drone_owner, drone = await make_club(name="Drone Club", email="drone-push@example.com")
    robotics = InterestCategory(name="Robotics push", slug="robotics-push")
    session.add(robotics)
    await session.flush()
    drones = Interests(name="Drones push", category_id=robotics.id)
    session.add(drones)
    asha = await make_user(email="asha-push@example.com", username="asha-push")
    ravi = await make_user(email="ravi-push@example.com", username="ravi-push")
    meera = await make_user(email="meera-push@example.com", username="meera-push")
    await session.commit()
    h = {u: auth_header(await mint_token(u)) for u in (asha, ravi, meera)}
    for who, token, caps in ((asha, "tok-asha", 1), (ravi, "tok-ravi", 0), (meera, "tok-meera", 0)):
        body = {"fcm_token": token, "platform": "android"}
        if caps:
            body["topics"] = caps
        assert (await client.post(f"{API}/user/fcm-token", json=body, headers=h[who])).status_code == 200
    for who, c in ((asha, club), (ravi, club), (ravi, drone)):
        assert (await client.post(f"{API}/clubs/follow/{c.id}", headers=h[who])).status_code == 200
    for who in (asha, ravi, meera):
        r = await client.post(f"{API}/user/interests/select", json={"interest_ids": [drones.id]}, headers=h[who])
        assert r.status_code == 200, r.text
    ev = await _event(session, club, owner, "Hack Night push")
    drone_ev = await _event(session, drone, drone_owner, "Drone Day push")
    thread = ClubThreads(club_id=club.id, body="Lab 3 from 6:30 tonight.", kind="post", notify_followers=False, created_by=owner.id)
    session.add(thread)
    await session.commit()
    return SimpleNamespace(
        club=club, drone=drone, owner=owner, robotics=robotics, drones=drones, asha=asha, ravi=ravi, meera=meera, h=h,
        ev=ev, drone_ev=drone_ev, thread=thread, owner_h=auth_header(await mint_token(owner)),
    )


async def _event(session, club, owner, name):
    from app.api.events.models import EventCategories, Events
    from app.core.utils.keys import generate_slug

    category = EventCategories(name=f"General {name}", created_by_id=owner.id)
    session.add(category)
    await session.flush()
    ev = Events(slug=generate_slug(name), name=name, images=[], event_datetime=datetime.now(timezone.utc), duration=2.0, club_id=club.id, category_id=category.id)
    session.add(ev)
    await session.flush()
    return ev


async def _job_rows(session, name):
    return list((await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == name).order_by(jobs.Jobs.id).execution_options(populate_existing=True))).all())


# ─── 1. The switch ───────────────────────────────────────────────────────


async def test_with_the_switch_off_a_post_goes_device_by_device_as_before(session, scene, fcm, flags):
    told = await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    assert told == 2
    assert fcm.sent == []  # no topic send
    assert fcm.device_tokens() == ["tok-asha", "tok-ravi"]
    [message] = fcm.multicasts
    assert "push_id" not in message.data and message.android.notification.tag is None and message.apns.headers is None
    assert await session.scalar(select(func.count()).select_from(NotificationBroadcasts)) == 0
    rows = (await session.scalars(select(Notifications.user_id).where(Notifications.type == "club_thread"))).all()
    assert sorted(rows) == sorted([scene.asha.id, scene.ravi.id])


async def test_with_the_switch_on_a_post_is_one_topic_send_and_old_apps_one_by_one(session, scene, fcm, flags):
    flags(PUSH_TOPICS=True)
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    pid = f"club_thread:{scene.thread.id}"
    [topic_send] = fcm.sent
    assert topic_send.topic == f"club_{scene.club.id}" and topic_send.condition is None
    assert topic_send.data["push_id"] == pid and topic_send.data["thread_id"] == str(scene.thread.id)
    assert topic_send.android.notification.tag == pid and topic_send.apns.headers == {"apns-collapse-id": pid}
    # Asha's device subscribes: the topic reached it, so it is not sent again.
    assert fcm.device_tokens() == ["tok-ravi"]
    assert {m.data["push_id"] for m in fcm.multicasts} == {pid}
    assert fcm.multicasts[0].android.notification.tag == pid
    # INBOX_SHARED off: a row per recipient, as before, and the broadcast recorded once.
    rows = (await session.scalars(select(Notifications.user_id).where(Notifications.type == "club_thread"))).all()
    assert sorted(rows) == sorted([scene.asha.id, scene.ravi.id])
    [b] = (await session.scalars(select(NotificationBroadcasts))).all()
    assert b.push_id == pid and b.shared is False and b.audience == {"club": scene.club.id}

    # Run again (the queue is at least once): nothing written or sent twice.
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    assert len(fcm.sent) == 1 and len(fcm.multicasts) == 1
    assert await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.type == "club_thread")) == 2


async def test_without_the_legacy_leg_a_broadcast_is_one_send(session, scene, fcm, flags):
    flags(PUSH_TOPICS=True, PUSH_TOPICS_LEGACY=False)
    await triggers.notify_followers_of_new_event(session, scene.club.id, scene.club.name, scene.ev.id, scene.ev.name)
    await jobs.settle()
    [send] = fcm.sent
    assert send.topic == f"club_{scene.club.id}" and send.data["push_id"] == f"new_event:{scene.ev.id}" and send.data["type"] == "new_event"
    assert fcm.multicasts == []


async def test_a_new_event_by_topic_skips_the_clubs_followers_and_is_capped_per_topic(session, scene, fcm, flags, monkeypatch):
    flags(PUSH_TOPICS=True)
    monkeypatch.setattr(settings, "INTEREST_TOPIC_PUSH_MAX_PER_DAY", 1)
    await triggers.notify_users_by_interest(session, scene.robotics.id, scene.robotics.name, scene.drone.id, scene.drone_ev.id, scene.drone_ev.name)
    await jobs.settle()
    [send] = fcm.sent
    assert send.topic is None
    assert send.condition == f"('topic_robotics-push' in topics) && !('club_{scene.drone.id}' in topics)"
    assert send.data["type"] == "interest_match" and send.data["push_id"] == f"nearby_event:{scene.drone_ev.id}"
    # Old apps one by one: Meera. Not Ravi (he follows Drone Club), not Asha (her device subscribes).
    assert fcm.device_tokens() == ["tok-meera"]

    # The topic had its one push today: the next event under it is not pushed.
    another = await _event(session, scene.drone, scene.owner, "Drone Night push")
    await session.commit()
    await triggers.notify_users_by_interest(session, scene.robotics.id, scene.robotics.name, scene.drone.id, another.id, another.name)
    await jobs.settle()
    assert len(fcm.sent) == 1 and fcm.device_tokens() == ["tok-meera"]


async def test_a_push_to_everyone_goes_to_the_all_topic(client, session, scene, fcm, flags, make_admin, mint_token):
    flags(PUSH_TOPICS=True)
    owner = await make_admin(email="owner-push@example.com")
    await session.commit()
    r = await client.post(f"{API}/notifications/test/send-all", json={"title": "Hello", "body": "Everyone"}, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200 and r.json()["tokens_count"] == 3
    [send] = fcm.sent
    assert send.topic == "all" and send.data["push_id"].startswith("platform:")
    assert fcm.device_tokens() == ["tok-meera", "tok-ravi"]
    assert {m.data["push_id"] for m in fcm.multicasts} == {send.data["push_id"]}
    # The platform's announcement was never an inbox line, and is not now.
    assert await session.scalar(select(func.count()).select_from(Notifications)) == 0


async def test_the_device_leg_is_a_chain_of_jobs_each_a_slice(session, scene, fcm, flags, monkeypatch, make_user):
    flags(PUSH_TOPICS=True)
    monkeypatch.setattr(fanout, "USERS_PER_JOB", 2)
    for i in range(3):
        u = await make_user(email=f"chain{i}-push@example.com", username=f"chain{i}-push")
        session.add(UserDeviceTokens(user_id=u.id, fcm_token=f"tok-chain-{i}", platform="ios"))
        session.add(ClubUsersLink(club_id=scene.club.id, user_id=u.id, is_following=True))
    await session.commit()
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    # Five followers, two a job: three jobs, each device once (Asha's by topic).
    devices = await _job_rows(session, "push.devices")
    assert len(devices) == 3 and all(j.finished_at is not None for j in devices)
    assert fcm.device_tokens() == ["tok-chain-0", "tok-chain-1", "tok-chain-2", "tok-ravi"]


# ─── 2. Retries ──────────────────────────────────────────────────────────


async def test_a_topic_send_that_fails_for_now_is_retried_with_the_same_push_id(session, scene, fcm, flags):
    flags(PUSH_TOPICS=True, PUSH_TOPICS_LEGACY=False)
    fcm.fail = [firebase_exceptions.UnavailableError("FCM is down")]
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    [job] = await _job_rows(session, "push.topic")
    assert fcm.sent == [] and job.finished_at is None and job.attempts == 1 and "TopicSendFailed" in job.last_error
    assert job.run_at > datetime.now(timezone.utc)  # backed off
    await session.execute(update(jobs.Jobs).where(jobs.Jobs.id == job.id).values(run_at=func.now() - timedelta(seconds=1)))
    await session.commit()
    await jobs.run_due(ids=[job.id])
    [job] = await _job_rows(session, "push.topic")
    assert job.finished_at is not None and job.attempts == 2
    [send] = fcm.sent
    assert send.data["push_id"] == f"club_thread:{scene.thread.id}"


async def test_a_topic_send_fcm_calls_wrong_is_not_retried(session, scene, fcm, flags):
    flags(PUSH_TOPICS=True, PUSH_TOPICS_LEGACY=False)
    fcm.fail = [firebase_exceptions.InvalidArgumentError("Invalid condition")]
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    [job] = await _job_rows(session, "push.topic")
    assert job.finished_at is not None and job.attempts == 1 and fcm.sent == []


# ─── 3. The topics list ──────────────────────────────────────────────────


async def _topics(client, headers) -> dict:
    r = await client.get(f"{API}/notifications/topics", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_topics_list_and_its_version(client, session, scene, fcm):
    h = scene.h[scene.asha]
    first = await _topics(client, h)
    assert first["topics"] == sorted(["all", f"club_{scene.club.id}", "topic_robotics-push"])
    assert first["version"] == topics.version_of(first["topics"]) and len(first["version"]) == 16
    assert (await _topics(client, h)) == first  # unchanged: the same version

    await client.post(f"{API}/clubs/follow/{scene.drone.id}", headers=h)
    followed = await _topics(client, h)
    assert f"club_{scene.drone.id}" in followed["topics"] and followed["version"] != first["version"]
    await client.post(f"{API}/clubs/unfollow/{scene.drone.id}", headers=h)
    assert (await _topics(client, h)) == first  # the same list is the same version
    # Asha's device subscribes: it left Drone Club's topic on the server's side too.
    assert fcm.unsubscribed == [(["tok-asha"], f"club_{scene.drone.id}")]

    await client.post(f"{API}/user/interests/select", json={"interest_ids": []}, headers=h)
    assert (await _topics(client, h))["topics"] == sorted(["all", f"club_{scene.club.id}"])
    assert fcm.unsubscribed[-1] == (["tok-asha"], "topic_robotics-push")
    assert (await client.get(f"{API}/notifications/topics")).status_code == 401


async def test_keeping_an_interest_keeps_when_it_was_picked(client, session, scene):
    from app.api.users.models import UserInterests

    before = await session.scalar(select(UserInterests.created_at).where(UserInterests.user_id == scene.asha.id))
    other = Interests(name="Rovers push", category_id=scene.robotics.id)
    session.add(other)
    await session.commit()
    r = await client.post(f"{API}/user/interests/select", json={"interest_ids": [scene.drones.id, other.id]}, headers=scene.h[scene.asha])
    assert sorted(i["id"] for i in r.json()) == sorted([scene.drones.id, other.id])
    rows = dict((await session.execute(select(UserInterests.interest_id, UserInterests.created_at).where(UserInterests.user_id == scene.asha.id).execution_options(populate_existing=True))).all())
    assert rows[scene.drones.id] == before and rows[other.id] >= before


async def test_a_muted_club_leaves_the_list_the_devices_and_the_fan_out(client, session, scene, fcm, flags):
    h = scene.h[scene.asha]
    assert (await client.get(f"{API}/notifications/mutes/clubs/{scene.club.id}", headers=h)).json()["muted"] is False
    r = await client.put(f"{API}/notifications/mutes/clubs/{scene.club.id}", headers=h)
    assert r.status_code == 200 and r.json() == {"club_id": scene.club.id, "muted": True}
    assert (await client.get(f"{API}/notifications/mutes/clubs/{scene.club.id}", headers=h)).json()["muted"] is True
    assert f"club_{scene.club.id}" not in (await _topics(client, h))["topics"]
    assert fcm.unsubscribed == [(["tok-asha"], f"club_{scene.club.id}")]
    # Device by device (the switch off), she is left out too.
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    assert fcm.device_tokens() == ["tok-ravi"]
    assert (await session.scalars(select(Notifications.user_id).where(Notifications.type == "club_thread"))).all() == [scene.ravi.id]
    # Not a club she follows: nothing to mute.
    assert (await client.put(f"{API}/notifications/mutes/clubs/{scene.drone.id}", headers=h)).status_code == 404
    r = await client.delete(f"{API}/notifications/mutes/clubs/{scene.club.id}", headers=h)
    assert r.json()["muted"] is False and f"club_{scene.club.id}" in (await _topics(client, h))["topics"]


async def test_a_switch_turned_off_drops_its_topics_and_its_pushes(client, session, scene, fcm, flags):
    h = scene.h[scene.asha]
    assert (await client.get(f"{API}/notifications/settings", headers=h)).json() == {"clubs": True, "topics": True, "platform": True}
    r = await client.patch(f"{API}/notifications/settings", json={"topics": False, "platform": False}, headers=h)
    assert r.json() == {"clubs": True, "topics": False, "platform": False}
    assert (await _topics(client, h))["topics"] == [f"club_{scene.club.id}"]
    assert sorted(t for _, t in fcm.unsubscribed) == ["all", "topic_robotics-push"]
    flags(PUSH_TOPICS=True)
    await triggers.notify_users_by_interest(session, scene.robotics.id, scene.robotics.name, scene.drone.id, scene.drone_ev.id, scene.drone_ev.name)
    await jobs.settle()
    assert fcm.device_tokens() == ["tok-meera"]
    # With the clubs switch off, the club's line and push skip her as well.
    r = await client.patch(f"{API}/notifications/settings", json={"clubs": False}, headers=h)
    assert r.json() == {"clubs": False, "topics": False, "platform": False}
    assert fcm.unsubscribed[-1] == (["tok-asha"], f"club_{scene.club.id}")
    assert (await _topics(client, h))["topics"] == []
    flags(PUSH_TOPICS=False)
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    assert "tok-asha" not in fcm.device_tokens()


# ─── 4. What a device reports ────────────────────────────────────────────


async def test_a_device_says_whether_it_subscribes_and_the_admins_see_how_many(client, session, scene, make_admin, mint_token):
    rows = dict((await session.execute(select(UserDeviceTokens.fcm_token, UserDeviceTokens.supports_topics).execution_options(populate_existing=True))).all())
    assert rows == {"tok-asha": True, "tok-ravi": False, "tok-meera": False}
    # The same install with an app from before topics: it says no again.
    await client.post(f"{API}/user/fcm-token", json={"fcm_token": "tok-asha", "platform": "android"}, headers=scene.h[scene.asha])
    assert await session.scalar(select(UserDeviceTokens.supports_topics).where(UserDeviceTokens.fcm_token == "tok-asha").execution_options(populate_existing=True)) is False
    await client.post(f"{API}/user/fcm-token", json={"fcm_token": "tok-asha", "platform": "android", "topics": 1}, headers=scene.h[scene.asha])
    bad = await client.post(f"{API}/user/fcm-token", json={"fcm_token": "tok-x", "platform": "android", "topics": 2}, headers=scene.h[scene.asha])
    assert bad.status_code == 422

    admin = await make_admin(email="admin-adoption@example.com")
    await session.commit()
    assert (await client.get(f"{API}/notifications/topics/adoption", headers=scene.h[scene.asha])).status_code in (401, 403)
    r = await client.get(f"{API}/notifications/topics/adoption", headers=auth_header(await mint_token(admin)))
    assert r.status_code == 200
    body = r.json()
    assert body["devices"] == 3 and body["topic_devices"] == 1 and body["share"] == round(1 / 3, 4)
    assert body["by_platform"] == {"android": {"devices": 3, "topics": 1}} and body["push_topics"] is False


# ─── 5. The shared inbox: the same answers as the rows it replaces ───────


def _shape(body: dict) -> dict:
    """A page with what differs by nature taken out: the line's id (a shared
    row's is the broadcast's) and updated_at (a row's moves when it is read)."""
    return {**body, "items": [{k: v for k, v in item.items() if k not in ("id", "updated_at")} for item in body["items"]]}


async def _inbox(client, headers, **params) -> dict:
    r = await client.get(f"{API}/notifications/fetch-list", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _story(client, session, scene, clock: Clock, base: datetime) -> dict:
    """The same things happen: a personal line, Circuit Society's new event
    and post, Drone Club's event by topic; then each person reads one line
    and Asha reads everything. Returns what each person's inbox said."""
    clock.at = base + timedelta(minutes=1)
    await triggers.notify_followers_of_new_event(session, scene.club.id, scene.club.name, scene.ev.id, scene.ev.name)
    await jobs.settle()
    clock.at = base + timedelta(minutes=2)
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    clock.at = base + timedelta(minutes=3)
    await triggers.notify_users_by_interest(session, scene.robotics.id, scene.robotics.name, scene.drone.id, scene.drone_ev.id, scene.drone_ev.name)
    await jobs.settle()

    seen: dict = {}
    for name, who in (("asha", scene.asha), ("ravi", scene.ravi), ("meera", scene.meera)):
        h = scene.h[who]
        full = await _inbox(client, h, limit=10)
        pages, offset = [], 0
        while True:
            page = await _inbox(client, h, limit=2, offset=offset)
            pages.append(_shape({**page, "next": bool(page["next"])}))
            if not page["next"]:
                break
            offset += 2
        unread = (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["count"]
        # Reading the second line.
        second = full["items"][1]["id"] if len(full["items"]) > 1 else full["items"][0]["id"]
        read = await client.post(f"{API}/notifications/read/{second}", headers=h)
        assert read.status_code == 200, read.text
        read_row = {k: v for k, v in read.json().items() if k not in ("id", "updated_at")}
        after_one = (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["count"]
        seen[name] = dict(full=_shape(full), pages=pages, unread=unread, read_row=read_row, after_one=after_one,
                          statuses=[i["status"] for i in (await _inbox(client, h, limit=10))["items"]])
    h = scene.h[scene.asha]
    seen["read_all"] = (await client.post(f"{API}/notifications/read-all", headers=h)).json()
    seen["after_all"] = (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["count"]
    seen["statuses_after_all"] = [i["status"] for i in (await _inbox(client, h, limit=10))["items"]]
    return seen


async def test_the_shared_inbox_answers_exactly_what_the_rows_did(client, session, scene, fcm, flags, monkeypatch):
    clock = Clock(datetime.now(timezone.utc))
    monkeypatch.setattr(fanout, "datetime", clock)
    base = datetime.now(timezone.utc) + timedelta(minutes=5)
    session.add(Notifications(user_id=scene.asha.id, title="You're in", description="Hack Night push", type="event_registered",
                              event_id=scene.ev.id, from_club_id=scene.club.id, created_at=base + timedelta(seconds=30)))
    await session.commit()

    old = await _story(client, session, scene, clock, base)

    # The same again, with the broadcasts shared (and sent by topic): the
    # per-recipient lines go, the personal line and its state go back to new.
    await session.execute(delete(Notifications).where(Notifications.type.in_(["new_event", "club_thread", "nearby_event"])))
    await session.execute(update(Notifications).values(status="unread"))
    await session.commit()
    flags(INBOX_SHARED=True, PUSH_TOPICS=True)
    new = await _story(client, session, scene, clock, base)

    assert new == old
    # And this time the broadcasts were one row each, not one per recipient.
    assert await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.type.in_(["new_event", "club_thread", "nearby_event"]))) == 0
    assert await session.scalar(select(func.count()).select_from(NotificationBroadcasts).where(NotificationBroadcasts.shared == True)) == 3  # noqa: E712
    # What the story saw, so the comparison is not of two empty inboxes.
    assert [i["type"] for i in old["asha"]["full"]["items"]] == ["nearby_event", "club_thread", "new_event", "event_registered"]
    assert [i["type"] for i in old["ravi"]["full"]["items"]] == ["club_thread", "new_event"]
    assert [i["type"] for i in old["meera"]["full"]["items"]] == ["nearby_event"]
    assert old["asha"]["unread"] == 4 and old["asha"]["after_one"] == 3 and old["after_all"] == 0
    assert old["read_all"]["updated_count"] == 3 and set(old["statuses_after_all"]) == {"read"}
    assert old["asha"]["full"]["items"][0]["user_id"] == scene.asha.id
    assert old["asha"]["full"]["items"][1]["from_club"]["id"] == scene.club.id


async def test_the_shared_inbox_pages_by_keyset(client, session, scene, fcm, flags, monkeypatch):
    flags(INBOX_SHARED=True)
    clock = Clock(datetime.now(timezone.utc))
    monkeypatch.setattr(fanout, "datetime", clock)
    base = datetime.now(timezone.utc) + timedelta(minutes=5)
    for i in range(5):
        t = ClubThreads(club_id=scene.club.id, body=f"Post {i}", kind="post", notify_followers=False, created_by=scene.owner.id)
        session.add(t)
        session.add(Notifications(user_id=scene.ravi.id, title=f"Mine {i}", description="d", type="event_reminder", created_at=base + timedelta(minutes=i, seconds=30)))
        await session.commit()
        clock.at = base + timedelta(minutes=i)
        await threads_service.tell_followers(scene.club.id, t.id)
        await jobs.settle()
    h = scene.h[scene.ravi]
    everything = [i["title"] for i in (await _inbox(client, h, limit=20))["items"]]
    assert everything == [t for i in reversed(range(5)) for t in (f"Mine {i}", "Circuit Society posted")]

    first = (await client.get(f"{API}/notifications/fetch-list", params={"limit": 3}, headers=h)).json()
    assert "cursor=" in first["next"] and "offset=3" in first["next"]
    # A newer line arrives between pages: by offset it would push one line
    # onto the next page twice; by the cursor nothing moves.
    t = ClubThreads(club_id=scene.club.id, body="Late post", kind="post", notify_followers=False, created_by=scene.owner.id)
    session.add(t)
    await session.commit()
    clock.at = base + timedelta(minutes=30)
    await threads_service.tell_followers(scene.club.id, t.id)
    await jobs.settle()
    walked, url = [i["title"] for i in first["items"]], first["next"]
    while url:
        page = (await client.get(url, headers=h)).json()
        walked += [i["title"] for i in page["items"]]
        url = page["next"]
    assert walked == everything
    # A cursor that is not ours reads as the first page.
    assert len((await _inbox(client, h, limit=3, cursor="not-a-cursor"))["items"]) == 3


async def test_a_shared_line_can_be_dismissed_and_only_by_who_can_see_it(client, session, scene, fcm, flags):
    flags(INBOX_SHARED=True)
    await threads_service.tell_followers(scene.club.id, scene.thread.id)
    await jobs.settle()
    [line] = (await _inbox(client, scene.h[scene.ravi]))["items"]
    # Meera does not follow the club: not hers to read or dismiss.
    assert (await client.post(f"{API}/notifications/read/{line['id']}", headers=scene.h[scene.meera])).status_code == 404
    assert (await client.delete(f"{API}/notifications/{line['id']}", headers=scene.h[scene.meera])).status_code == 404
    assert (await client.delete(f"{API}/notifications/{line['id']}", headers=scene.h[scene.ravi])).status_code == 204
    assert (await _inbox(client, scene.h[scene.ravi]))["items"] == []
    assert len((await _inbox(client, scene.h[scene.asha]))["items"]) == 1  # hers is untouched
    assert (await client.get(f"{API}/notifications/unread-count", headers=scene.h[scene.ravi])).json()["count"] == 0
    # Someone who follows later does not get the club's older lines.
    await client.post(f"{API}/clubs/follow/{scene.club.id}", headers=scene.h[scene.meera])
    assert (await _inbox(client, scene.h[scene.meera]))["items"] == []
    assert (await client.post(f"{API}/notifications/read/not-an-id", headers=scene.h[scene.ravi])).status_code == 404


async def test_the_shared_unread_count_stops_at_its_cap_and_counts_a_line_once(client, session, scene, fcm, flags, monkeypatch):
    flags(INBOX_SHARED=True)
    monkeypatch.setattr(inbox, "UNREAD_CAP", 3)
    # A second topic Asha picked an interest under; an event under both of hers.
    rovers = InterestCategory(name="Rovers push", slug="rovers-push")
    session.add(rovers)
    await session.flush()
    session.add(Interests(name="Wheels push", category_id=rovers.id))
    await session.commit()
    wheels = await session.scalar(select(Interests.id).where(Interests.category_id == rovers.id))
    await client.post(f"{API}/user/interests/select", json={"interest_ids": [scene.drones.id, wheels]}, headers=scene.h[scene.asha])
    await triggers._announce_by_topic(
        session, [scene.robotics.id, rovers.id], club_id=scene.drone.id, event_id=scene.drone_ev.id,
        title="Event", body="Both topics", data={}, push_data={"type": "interest_match"},
    )
    h = scene.h[scene.asha]
    count = lambda: client.get(f"{API}/notifications/unread-count", headers=h)  # noqa: E731
    assert (await count()).json()["count"] == 1
    assert [i["description"] for i in (await _inbox(client, h))["items"]] == ["Both topics"]
    for n in range(4):
        t = ClubThreads(club_id=scene.club.id, body=f"Post {n}", kind="post", notify_followers=False, created_by=scene.owner.id)
        session.add(t)
        await session.commit()
        await threads_service.tell_followers(scene.club.id, t.id)
    assert (await count()).json()["count"] == 3  # 5, at most UNREAD_CAP
    monkeypatch.setattr(inbox, "UNREAD_CAP", 999)
    assert (await count()).json()["count"] == 5
    # The clubs switch off: the club's lines leave her inbox and her count.
    await client.patch(f"{API}/notifications/settings", json={"clubs": False}, headers=h)
    assert (await count()).json()["count"] == 1


async def test_the_shared_inbox_reads_one_index_range_per_target(session, scene, flags, monkeypatch):
    """The read's plan, with sequential scans priced out: each target is a
    range of the feed index, the person's own rows a range of theirs."""
    flags(INBOX_SHARED=True)
    captured: list = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if "notification_broadcast_targets" in statement:
            captured.append((statement, parameters))

    engine = session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        await inbox.page(session, scene.asha.id, limit=20)
        await inbox.page(session, scene.asha.id, limit=20, cursor=(datetime.now(timezone.utc), scene.thread.id))
        await inbox.shared_unread(session, scene.asha.id)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    raw = await session.connection()
    for statement, parameters in captured:
        plan = "\n".join(r[0] for r in (await raw.exec_driver_sql("EXPLAIN " + statement, parameters)).all())
        assert "ix_notification_broadcast_targets_feed" in plan, plan
    assert len(captured) == 3


# ─── 7. Which pushes are broadcasts ─────────────────────────────────────────
# (Last: a first test that opens no session leaves the suite's engine on
# another event loop when this file is run alone.)


async def test_every_kind_is_decided_and_only_broadcasts_go_by_topic():
    kinds = {k.value for k in NotificationType} | {"event_live_update", "event_live_join", "platform"}
    decided = set(delivery.BROADCASTS) | set(delivery.PERSONAL)
    assert kinds <= decided, kinds - decided
    assert not set(delivery.BROADCASTS) & set(delivery.PERSONAL)
    assert {k for k in kinds if delivery.delivery_for(k) is delivery.Delivery.TOPIC} == {"club_thread", "new_event", "nearby_event", "platform"}
    # Tickets, payments, reminders, check-ins, guests, and anything new: device by device.
    for personal in ("event_registered", "event_reminder", "event_checkin", "certificate_generated", "club_announcement", "event_live_update", "event_live_join", "payment_captured", "guest_ticket", None):
        assert delivery.delivery_for(personal) is delivery.Delivery.DEVICE


async def test_topic_names_ids_and_conditions():
    assert delivery.club_topic(12) == "club_12"
    assert delivery.topic_topic("ui-ux") == "topic_ui-ux"
    assert delivery.topic_topic("a b/c") == "topic_abc"  # nothing FCM refuses
    assert delivery.push_id("club_thread", "x" * 100) == ("club_thread:" + "x" * 100)[:64]
    assert delivery.condition(["topic_a"], unless="club_1") == "('topic_a' in topics) && !('club_1' in topics)"
    assert delivery.condition(["topic_a", "topic_b"]) == "'topic_a' in topics || 'topic_b' in topics"
    # FCM takes five topics in a condition: four and the club.
    many = delivery.condition([f"topic_{i}" for i in range(7)], unless="club_1")
    assert many.count(" in topics") == 5 and "topic_4" not in many
    with pytest.raises(ValueError):
        delivery.condition([])
