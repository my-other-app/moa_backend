"""Telling the room (app/api/live/notify.py): starting the quiz, or launching a
poll when the host asks, pushes to every device of everyone checked in and
writes their inbox line -- not to the registered who are not there, not to
whoever muted the club or turned clubs' pushes off, not to guests without the
app -- at most once per event every three minutes and ten times a day in
India, settled by Postgres when two hosts press at once. The quiz or the poll
starts whatever happens to the notification. FCM is never called: the
messaging module is faked."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from firebase_admin import messaging
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.audit.models import AdminAuditLogs
from app.api.checkins.models import CheckInVia, CheckIns
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.live import notify, service
from app.api.live.models import LivePolls, LiveQuizPlayers, LiveQuizQuestions, LiveQuizzes
from app.api.notifications.models import NotificationSettings, Notifications
from app.api.users.models import UserDeviceTokens, Users, UserTypes
from app.config import settings
from app.core import jobs
from app.core.notifications import service as push
from app.core.utils.keys import generate_ticket_id
from tests.conftest import _make_user, auth_header

START = datetime.now(timezone.utc) + timedelta(hours=1)
QUIZ_BODY = "The quiz is starting — join now"
POLL_BODY = "A poll is live — vote now"
HOST_KEYS = {"status", "join_code", "title", "players", "current_index", "total", "question", "leaderboard"}
POLL_KEYS = {"id", "kind", "prompt", "options", "status", "launched_at", "closed_at", "sort_order", "results", "my_response"}
#: Starting the quiz again while it runs (QUIZ_RUNNING otherwise).
AGAIN = {"restart": True}


class FakeMessaging:
    """firebase_admin.messaging's sends, recorded: multicasts (device by
    device) and single sends (a topic's, or one device's)."""

    def __init__(self):
        self.multicasts: list = []
        self.sent: list = []

    def send(self, message):
        self.sent.append(message)
        return f"projects/test/messages/{len(self.sent)}"

    def send_each_for_multicast(self, message):
        self.multicasts.append(message)
        return SimpleNamespace(success_count=len(message.tokens), failure_count=0, responses=[])

    def send_each(self, messages):
        """A batch whose devices have different badges (each its person's
        unread count): a message per device, in one call. Recorded as the
        batch it is -- its tokens, and what every message shares."""
        first = messages[0]
        self.multicasts.append(SimpleNamespace(
            tokens=[m.token for m in messages], data=first.data, notification=first.notification,
            android=first.android, apns=first.apns, badges=[m.apns.payload.aps.badge for m in messages],
        ))
        return SimpleNamespace(success_count=len(messages), failure_count=0, responses=[])


@pytest.fixture
def fcm(monkeypatch):
    fake = FakeMessaging()
    monkeypatch.setattr(push, "initialize_firebase", lambda: True)
    monkeypatch.setattr(messaging, "send", fake.send)
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake.send_each_for_multicast)
    monkeypatch.setattr(messaging, "send_each", fake.send_each)
    return fake


@pytest.fixture
async def room(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch, fcm):
    """Hack Night 04 of Circuit Society, its quiz with a question and a draft
    poll. Checked in: Sneha (two phones) and Rohan (one, follows the club),
    Meera (follows it, muted), Sam (turned clubs' pushes off) and a guest
    from the door with no app. Registered, not there: Kiran (a phone)."""
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    for k, v in dict(LIVE_NOTIFY=True, LIVE_NOTIFY_GAP_SECONDS=180, LIVE_NOTIFY_PER_DAY=10, PUSH_TOPICS=False, INBOX_SHARED=False).items():
        monkeypatch.setattr(settings, k, v)
    owner, club = await make_club(name="Circuit Society", email="notify-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=START, duration=3.0)
    people = {}
    for key, email in (("sneha", "sneha-n@example.com"), ("rohan", "rohan-n@example.com"), ("meera", "meera-n@example.com"),
                       ("sam", "sam-n@example.com"), ("kiran", "kiran-n@example.com")):
        people[key] = await make_user(email=email)
    people["guest"] = await make_user(email="door-guest-n@example.com", user_type=UserTypes.guest)
    regs = {k: await make_registration(event=event, user=u, full_name=k.title()) for k, u in people.items()}
    for k in ("sneha", "rohan", "meera", "sam", "guest"):
        session.add(CheckIns(event_id=event.id, registration_id=regs[k].id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate A"))
    for token, k in (("tok-sneha-1", "sneha"), ("tok-sneha-2", "sneha"), ("tok-rohan", "rohan"), ("tok-meera", "meera"), ("tok-sam", "sam"), ("tok-kiran", "kiran")):
        session.add(UserDeviceTokens(user_id=people[k].id, fcm_token=token, platform="android"))
    session.add(ClubUsersLink(club_id=club.id, user_id=people["rohan"].id, is_following=True))
    session.add(ClubUsersLink(club_id=club.id, user_id=people["meera"].id, is_following=True, is_muted=True))
    session.add(NotificationSettings(user_id=people["sam"].id, clubs=False))
    quiz = LiveQuizzes(event_id=event.id, title="Hack Night 04 quiz")
    session.add(quiz)
    await session.flush()
    session.add(LiveQuizQuestions(quiz_id=quiz.id, prompt="RAM on an ESP32-S3?", options=[{"key": "a", "label": "512 KB"}, {"key": "b", "label": "8 MB"}], correct_key="a", seconds=20, sort_order=0))
    poll = LivePolls(event_id=event.id, kind=service.PollKind.choice, prompt="Which track?", options=["Hardware", "Software"], sort_order=0)
    session.add(poll)
    await session.commit()
    base = f"/api/v1/events/{event.id}/live"
    # Plain values only: a notification that fails rolls the session back,
    # and the rows it held are expired then.
    return SimpleNamespace(
        event=event, event_id=event.id, club_id=club.id, owner=owner, owner_id=owner.id, owner_email=owner.email,
        ids={k: u.id for k, u in people.items()}, h=auth_header(await mint_token(owner)),
        sneha_h=auth_header(await mint_token(people["sneha"])),
        start=f"{base}/quiz/host/start", quiz=f"{base}/quiz", poll_status=f"{base}/polls/{poll.id}/status", polls=f"{base}/polls",
        base=base, poll_id=str(poll.id), quiz_id=str(quiz.id),
    )


async def _audits(session, event_id):
    return list((await session.scalars(
        select(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION, AdminAuditLogs.entity_id == str(event_id)).order_by(AdminAuditLogs.created_at)
    )).all())


async def _inbox(session, event_id):
    return list((await session.scalars(
        select(Notifications).where(Notifications.event_id == event_id, Notifications.type == notify.TYPE).order_by(Notifications.user_id)
    )).all())


async def _push_jobs(session):
    return list((await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == notify.PUSH_JOB).order_by(jobs.Jobs.id))).all())


def _sent(n):
    return {"sent": n, "skipped_reason": None, "last_sent_at": None, "retry_after": None}


# ─── Starting the quiz tells the room ──────────────────────────────────────


async def test_starting_the_quiz_with_no_body_tells_everyone_checked_in_and_nobody_else(client, session, room, fcm):
    started = await client.post(room.start, headers=room.h)  # a console from before: no body at all
    assert started.status_code == 200, started.text
    out = started.json()
    assert set(out) == HOST_KEYS | {"notified"}
    assert out["status"] == "live" and len(out["join_code"]) == 6 and out["current_index"] == -1 and out["total"] == 1
    # Sneha and Rohan; not Meera (muted the club), Sam (clubs' pushes off), the guest (no app), Kiran (not there).
    assert out["notified"] == _sent(2)

    # The push: device by device (every phone of each), never a topic, exactly the contract's.
    assert fcm.sent == []
    [message] = fcm.multicasts
    assert sorted(message.tokens) == ["tok-rohan", "tok-sneha-1", "tok-sneha-2"]
    push_id = message.data["push_id"]
    assert str(uuid.UUID(push_id)) == push_id
    # The push's id is also its lines' key (fanout.line_id): a tap marks the reader's.
    assert message.data == {"type": "event_live_join", "event_id": str(room.event_id), "what": "quiz", "push_id": push_id, "notification_id": push_id}
    assert message.notification.title == "Hack Night 04" and message.notification.body == QUIZ_BODY
    # Wave 2's dedupe: the push_id is the tray's tag and APNs' collapse id.
    assert message.android.notification.tag == push_id and message.apns.headers == {"apns-collapse-id": push_id}

    # Sent as a job, ahead of the queue.
    [job] = await _push_jobs(session)
    assert job.priority == jobs.HIGH and job.finished_at is not None and sorted(job.payload["user_ids"]) == sorted([room.ids["sneha"], room.ids["rohan"]])

    # An inbox line each, that opens as the push does; the push names it
    # (its id derived from the push's id and the person: a tap marks it read).
    from app.core.notifications import fanout

    opens = {k: v for k, v in message.data.items() if k != "notification_id"}
    lines = await _inbox(session, room.event_id)
    assert sorted(n.user_id for n in lines) == sorted([room.ids["sneha"], room.ids["rohan"]])
    for n in lines:
        assert (n.title, n.description, n.from_club_id, n.data) == ("Hack Night 04", QUIZ_BODY, room.club_id, opens)
        assert n.id == fanout.line_id(message.data["notification_id"], n.user_id)
    page = (await client.get("/api/v1/notifications/fetch-list", headers=room.sneha_h)).json()
    mine = next(i for i in page["items"] if i["type"] == "event_live_join")
    assert mine["event"]["id"] == room.event_id and mine["data"] == opens and mine["description"] == QUIZ_BODY
    assert mine["id"] == str(fanout.line_id(push_id, room.ids["sneha"]))

    # Who told the room, in the audit log.
    [entry] = await _audits(session, room.event_id)
    assert (entry.actor_id, entry.actor_email, entry.entity_type) == (room.owner_id, room.owner_email, "event")
    assert entry.details == {"what": "quiz", "ref": room.quiz_id, "push_id": push_id, "recipients": 2}
    assert entry.summary == f"Told 2 checked-in people: {QUIZ_BODY}"


async def test_the_console_body_sets_who_is_told_and_launching_a_poll_tells_unless_asked_not_to(client, session, room, fcm):
    # The quiz, started without telling anyone.
    quiet = await client.post(room.start, json={"notify": False}, headers=room.h)
    assert quiet.status_code == 200 and quiet.json()["status"] == "live" and quiet.json()["notified"] is None
    # Next, end and reset take no notification.
    for action in ("next", "end", "reset"):
        r = await client.post(room.start.replace("/start", f"/{action}"), json={"notify": True}, headers=room.h)
        assert r.status_code == 200 and r.json()["notified"] is None, (action, r.text)

    # A poll launched with the switch off: nobody told.
    launched = await client.post(room.poll_status, json={"status": "live", "notify": False}, headers=room.h)
    assert launched.status_code == 200, launched.text
    assert set(launched.json()) == POLL_KEYS | {"notified"}
    assert launched.json()["status"] == "live" and launched.json()["notified"] is None
    assert fcm.multicasts == [] and await _audits(session, room.event_id) == [] and await _inbox(session, room.event_id) == []

    # Launched again as a console from before sends it (no `notify`): the
    # room is told a poll is live -- a poll notifies by default now.
    told = await client.post(room.poll_status, json={"status": "live"}, headers=room.h)
    assert told.json()["status"] == "live" and told.json()["notified"] == _sent(2)
    [message] = fcm.multicasts
    assert message.data["what"] == "poll" and message.notification.body == POLL_BODY and message.notification.title == "Hack Night 04"
    assert (await _audits(session, room.event_id))[0].details["ref"] == room.poll_id
    # Closing one never tells anybody.
    closed = await client.post(room.poll_status, json={"status": "closed", "notify": True}, headers=room.h)
    assert closed.json()["status"] == "closed" and closed.json()["notified"] is None
    assert len(fcm.multicasts) == 1


async def test_the_console_of_today_sends_an_empty_body_and_the_quiz_tells_the_room(client, room, fcm):
    started = await client.post(room.start, json={}, headers=room.h)
    assert started.json()["notified"] == _sent(2)
    assert (await client.post(room.start, json={"notify": None, "restart": True}, headers=room.h)).json()["notified"]["skipped_reason"] == "rate_limited"


# ─── The limit ───────────────────────────────────────────────────────────


async def test_one_notification_per_event_every_three_minutes_and_the_action_still_happens(client, session, room, fcm):
    first = await client.post(room.start, headers=room.h)
    assert first.json()["notified"] == _sent(2)
    sent_at = (await _audits(session, room.event_id))[0].created_at

    soon = await client.post(room.poll_status, json={"status": "live", "notify": True}, headers=room.h)
    assert soon.status_code == 200 and soon.json()["status"] == "live"  # launched all the same
    n = soon.json()["notified"]
    assert n["sent"] == 0 and n["skipped_reason"] == "rate_limited"
    assert datetime.fromisoformat(n["last_sent_at"]) == sent_at and 170 <= n["retry_after"] <= 180
    assert len(fcm.multicasts) == 1 and len(await _audits(session, room.event_id)) == 1  # a refusal does not count

    # Three minutes on, it may.
    await session.execute(update(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION).values(created_at=sent_at - timedelta(seconds=181)))
    await session.commit()
    later = await client.post(room.poll_status, json={"status": "live", "notify": True}, headers=room.h)
    assert later.json()["notified"] == _sent(2) and len(fcm.multicasts) == 2


async def test_ten_a_day_in_india_and_the_gap_holds_across_midnight(session, room, monkeypatch):
    ist = timezone(timedelta(hours=5, minutes=30))
    assert notify.day_start(datetime(2026, 10, 18, 18, 29, tzinfo=timezone.utc)) == datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc)
    assert notify.day_start(datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc)) == datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc)

    event = await session.get(Events, room.event_id)
    owner = await session.get(Users, room.owner_id)

    async def tell(at):
        return await notify.tell_room(session, event, "quiz", actor=owner, now=at)

    day = datetime(2026, 10, 19, 0, 5, tzinfo=ist)
    for i in range(10):
        assert (await tell(day + timedelta(minutes=5 * i)))["sent"] == 2, i
    eleventh = await tell(day + timedelta(hours=2))
    assert eleventh["skipped_reason"] == "rate_limited" and eleventh["sent"] == 0
    # Until India's midnight: 21 h 55 min from 02:05.
    assert eleventh["retry_after"] == int(timedelta(hours=21, minutes=55).total_seconds())
    assert eleventh["last_sent_at"] == day + timedelta(minutes=45)
    # Gone midnight in India -- not in UTC -- the count starts again.
    assert (await tell(datetime(2026, 10, 19, 23, 58, tzinfo=ist)))["skipped_reason"] == "rate_limited"
    assert (await tell(datetime(2026, 10, 20, 0, 0, 30, tzinfo=ist)))["sent"] == 2

    # The day's last at 23:59 holds the next day's first at 00:01.
    assert (await tell(datetime(2026, 10, 20, 23, 59, tzinfo=ist)))["sent"] == 2
    held = await tell(datetime(2026, 10, 21, 0, 1, tzinfo=ist))
    assert held["skipped_reason"] == "rate_limited" and held["retry_after"] == 60
    assert (await tell(datetime(2026, 10, 21, 0, 2, tzinfo=ist)))["sent"] == 2
    assert len(await _audits(session, room.event_id)) == 13

    # The numbers are settings.
    monkeypatch.setattr(settings, "LIVE_NOTIFY_PER_DAY", 0)
    assert (await tell(datetime(2026, 10, 22, 12, 0, tzinfo=ist)))["skipped_reason"] == "rate_limited"
    monkeypatch.setattr(settings, "LIVE_NOTIFY_PER_DAY", 10)
    monkeypatch.setattr(settings, "LIVE_NOTIFY_GAP_SECONDS", 30)
    assert (await tell(datetime(2026, 10, 22, 12, 0, 40, tzinfo=ist)))["sent"] == 2


# ─── Nobody to tell, switched off, failing ───────────────────────────────


async def test_nobody_checked_in_is_said_and_does_not_count(client, session, room, fcm):
    await session.execute(delete(CheckIns).where(CheckIns.event_id == room.event_id))
    await session.commit()
    r = await client.post(room.start, headers=room.h)
    assert r.json()["status"] == "live" and r.json()["notified"] == {"sent": 0, "skipped_reason": "nobody_checked_in", "last_sent_at": None, "retry_after": None}
    assert fcm.multicasts == [] and await _audits(session, room.event_id) == [] and await _inbox(session, room.event_id) == []
    regs = {r_.user_id: r_.id for r_ in (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == room.event_id))).all()}

    async def arrive(*keys):
        for k in keys:
            session.add(CheckIns(event_id=room.event_id, registration_id=regs[room.ids[k]], via=CheckInVia.gate, checked_in_at=START))
        await session.commit()

    # A guest from the door, with no app: nobody who can be told.
    await arrive("guest")
    assert (await client.post(room.start, json=AGAIN, headers=room.h)).json()["notified"]["skipped_reason"] == "nobody_checked_in"
    # Meera (muted the club) and Sam (clubs' pushes off): there, and all muted.
    await arrive("meera", "sam")
    muted = (await client.post(room.start, json=AGAIN, headers=room.h)).json()
    assert muted["status"] == "live" and muted["notified"] == {"sent": 0, "skipped_reason": "all_muted", "last_sent_at": None, "retry_after": None}
    launched = (await client.post(room.poll_status, json={"status": "live", "notify": True}, headers=room.h)).json()
    assert launched["notified"]["skipped_reason"] == "all_muted"
    assert fcm.multicasts == [] and await _audits(session, room.event_id) == [] and await _inbox(session, room.event_id) == []
    # Sneha arrives: she is told at once, nothing held it back.
    await arrive("sneha")
    assert (await client.post(room.start, json=AGAIN, headers=room.h)).json()["notified"] == _sent(1)


async def test_switched_off_the_quiz_starts_and_the_console_is_told_why(client, session, room, fcm, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_NOTIFY", False)
    r = await client.post(room.start, headers=room.h)
    assert r.status_code == 200 and r.json()["status"] == "live"
    assert r.json()["notified"] == {"sent": 0, "skipped_reason": "disabled", "last_sent_at": None, "retry_after": None}
    p = await client.post(room.poll_status, json={"status": "live", "notify": True}, headers=room.h)
    assert p.json()["status"] == "live" and p.json()["notified"]["skipped_reason"] == "disabled"
    assert fcm.multicasts == [] and await _audits(session, room.event_id) == []


async def test_a_notification_that_fails_never_fails_the_quiz_and_does_not_count(client, session, room, fcm, monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("the inbox is away")

    real = notify.fanout.insert_for_audience
    monkeypatch.setattr(notify.fanout, "insert_for_audience", broken)
    r = await client.post(room.start, headers=room.h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == HOST_KEYS | {"notified"} and r.json()["status"] == "live" and r.json()["join_code"]
    assert r.json()["notified"] == {"sent": 0, "skipped_reason": "failed", "last_sent_at": None, "retry_after": None}
    # The quiz did start; nothing of the notification is left behind.
    assert (await client.get(room.quiz, headers=room.h)).json()["status"] == "live"
    assert fcm.multicasts == [] and await _audits(session, room.event_id) == [] and await _push_jobs(session) == []

    # The poll's launch the same way.
    p = await client.post(room.poll_status, json={"status": "live", "notify": True}, headers=room.h)
    assert p.status_code == 200 and p.json()["status"] == "live" and p.json()["notified"]["skipped_reason"] == "failed"

    # Mended, the next one goes: the failures were not counted.
    monkeypatch.setattr(notify.fanout, "insert_for_audience", real)
    again = await client.post(room.start, json=AGAIN, headers=room.h)
    assert again.json()["notified"] == _sent(2) and len(fcm.multicasts) == 1


# ─── Two hosts at once ──────────────────────────────────────────────────


class Gate:
    """Lets `n` tasks through together, or each after `timeout`."""

    def __init__(self, n: int, timeout: float):
        self.n, self.timeout, self.arrived, self.go = n, timeout, 0, asyncio.Event()

    async def wait(self):
        self.arrived += 1
        if self.arrived >= self.n:
            self.go.set()
        try:
            await asyncio.wait_for(self.go.wait(), self.timeout)
        except asyncio.TimeoutError:
            pass


@pytest.fixture
def make(_engine):
    from app.db.core import SESSION_KWARGS
    from app.db.listeners import add_loader_criteria

    def factory():
        s = async_sessionmaker(bind=_engine, **SESSION_KWARGS)()
        add_loader_criteria(s)
        return s

    return factory


async def _committed_room(make, tag):
    """A room committed for real, so two sessions on two connections race over it."""
    async with make() as s:
        owner = await _make_user(s, email=f"{tag}-owner@example.com", user_type=UserTypes.club, username=f"{tag}owner")
        club = Clubs(slug=f"{tag}-club", name=f"{tag} Club", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name=tag, created_by_id=owner.id)
        s.add(category)
        await s.flush()
        ev = Events(slug=f"{tag}-night", name=f"{tag} Night", images=[], duration=3.0, event_datetime=START, club_id=club.id, category_id=category.id)
        s.add(ev)
        await s.flush()
        users = []
        for i in range(2):
            u = await _make_user(s, email=f"{tag}-g{i}@example.com", username=f"{tag}g{i}")
            r = EventRegistrationsLink(event_id=ev.id, user_id=u.id, full_name=f"Guest {i}", email=u.email, ticket_id=generate_ticket_id())
            s.add(r)
            await s.flush()
            s.add(CheckIns(event_id=ev.id, registration_id=r.id, via=CheckInVia.gate, checked_in_at=START))
            users.append(u.id)
        quiz = LiveQuizzes(event_id=ev.id, title="Race quiz")
        s.add(quiz)
        await s.flush()
        s.add(LiveQuizQuestions(quiz_id=quiz.id, prompt="Who wins?", options=[{"key": "a", "label": "One"}, {"key": "b", "label": "Both"}], correct_key="a", seconds=20, sort_order=0))
        await s.commit()
        return dict(owner=owner.id, club=club.id, category=category.id, event=ev.id, users=users, quiz=quiz.id)


async def _drop(make, w):
    from app.api.subscriptions.models import ClubSubscriptions

    async with make() as s:
        await s.execute(delete(jobs.Jobs).where(jobs.Jobs.name == notify.PUSH_JOB, jobs.Jobs.payload["data"]["event_id"].astext == str(w["event"])))
        await s.execute(delete(AdminAuditLogs).where(AdminAuditLogs.entity_type == "event", AdminAuditLogs.entity_id == str(w["event"])))
        await s.execute(delete(Notifications).where(Notifications.event_id == w["event"]))
        await s.execute(delete(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == w["quiz"]))
        await s.execute(delete(LiveQuizQuestions).where(LiveQuizQuestions.quiz_id == w["quiz"]))
        await s.execute(delete(LiveQuizzes).where(LiveQuizzes.id == w["quiz"]))
        await s.execute(delete(CheckIns).where(CheckIns.event_id == w["event"]))
        await s.execute(delete(EventRegistrationsLink).where(EventRegistrationsLink.event_id == w["event"]))
        await s.execute(delete(Events).where(Events.id == w["event"]))
        await s.execute(delete(EventCategories).where(EventCategories.id == w["category"]))
        await s.execute(delete(ClubSubscriptions).where(ClubSubscriptions.club_id == w["club"]))
        await s.execute(delete(Clubs).where(Clubs.id == w["club"]))
        await s.execute(delete(Users).where(Users.id.in_([w["owner"], *w["users"]])))
        await s.commit()


async def _two_hosts_start_at_once(make, w, monkeypatch) -> list:
    """Two consoles press Start at once, each on its own connection, as the
    router runs it: the quiz starts, then the room is told. Both have read
    what was sent lately before either goes on."""
    gate = Gate(2, timeout=1.0)
    real = notify._sent_lately

    async def sent_lately(*args, **kwargs):
        out = await real(*args, **kwargs)
        await gate.wait()
        return out

    monkeypatch.setattr(notify, "_sent_lately", sent_lately)

    async def host():
        async with make() as s:
            event = await s.get(Events, w["event"])
            owner = await s.get(Users, w["owner"])
            quiz = await service.quiz_of(s, event, create=False)
            await service.start_quiz(s, quiz, restart=True)
            return await notify.tell_room(s, event, "quiz", actor=owner, ref=w["quiz"])

    return await asyncio.gather(host(), host())


async def test_two_hosts_starting_the_quiz_at_once_send_one_notification(make, monkeypatch):
    w = await _committed_room(make, "notifyrace")
    try:
        got = await _two_hosts_start_at_once(make, w, monkeypatch)
        assert sorted((g["sent"], g["skipped_reason"] or "") for g in got) == [(0, "rate_limited"), (2, "")]
        async with make() as s:
            assert await s.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION, AdminAuditLogs.entity_id == str(w["event"]))) == 1
            assert await s.scalar(select(func.count()).select_from(Notifications).where(Notifications.event_id == w["event"])) == 2
            assert await s.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == notify.PUSH_JOB, jobs.Jobs.payload["data"]["event_id"].astext == str(w["event"]))) == 1
            assert (await s.get(LiveQuizzes, w["quiz"])).status.value == "live"
    finally:
        await _drop(make, w)


async def test_without_the_lock_the_same_race_would_send_twice(make, monkeypatch):
    """The race above is real: with the lock taken away, both hosts read that
    nothing went lately and both send. (What the lock is there for.)"""
    w = await _committed_room(make, "notifynolock")
    try:
        async def no_lock(session, event_id):
            return None

        monkeypatch.setattr(notify, "_lock", no_lock)
        got = await _two_hosts_start_at_once(make, w, monkeypatch)
        assert [g["sent"] for g in got] == [2, 2]
    finally:
        await _drop(make, w)
