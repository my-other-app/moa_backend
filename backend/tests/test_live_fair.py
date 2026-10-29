"""The live room, made fair (feat/live-fixes):

- the quiz keeps its answer while answers are open: no right answer, no
  points or verdict, no score, rank or board that moves with them, until the
  clock and its grace are out -- and the big screen waits as the phones do;
- one player per person: the same key (or the same account, on any device)
  in the same run is the same player;
- one vote, question and poll answer per account when the request is signed in;
- an update's push respects mutes and switches, and goes from a job;
- the event going live and Q&A opening are told, once per event, under the
  room's limit;
- Start on a running quiz and a second press of Next are refused (409s);
- the event page's strip is a small public GET with a weak ETag.

FCM is faked (tests/test_live_notify.py's `fcm`); the room is its `room`."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.api.audit.models import AdminAuditLogs
from app.api.closeout import service as closeout
from app.api.closeout.models import EventStatus
from app.api.events.models import Events
from app.api.live import notify, room as room_mod, service
from app.api.live.models import LiveQuestionVotes, LiveQuizPlayers, LiveQuizzes
from app.api.notifications.models import Notifications
from app.core import jobs
from tests.racing import statements
from tests.test_live_notify import FakeMessaging, _audits, _sent, fcm, room  # noqa: F401 (fixtures)
from tests.test_live_room import snapshots  # noqa: F401 (fixture)

KEY_A = "browser-aaaaaaaa"
KEY_B = "browser-bbbbbbbb"
QA_BODY = "Q&A is open — ask your questions"
LIVE_BODY = "It's on — open the live room"


def _k(key):
    return {"X-Live-Key": key}


async def _open_question(client, room, seconds=20, questions=1):
    """The quiz started (quietly) with `questions` questions, on its first."""
    for n in range(questions - 1):
        await client.post(f"{room.quiz}/questions", json={"prompt": f"Extra {n}?", "options": [{"key": "a", "label": "Yes"}, {"key": "b", "label": "No"}], "correct_key": "b", "seconds": seconds}, headers=room.h)
    started = await client.post(room.start, json={"notify": False}, headers=room.h)
    assert started.status_code == 200, started.text
    return started.json()["join_code"]


async def _opened_ago(session, room, seconds: float):
    await session.execute(update(LiveQuizzes).where(LiveQuizzes.event_id == room.event_id).values(question_opened_at=datetime.now(timezone.utc) - timedelta(seconds=seconds)))
    await session.commit()
    room_mod.quizzes.clear()


def _join(client, code, name, key=None, headers=None):
    body = {"code": code, "name": name}
    if key is not None:
        body["player_key"] = key
    return client.post("/api/v1/live/quiz/join", json=body, headers=headers or {})


# ─── The quiz keeps its answer ─────────────────────────────────────────


async def test_nothing_gives_the_answer_away_until_answers_close(client, session, room):
    code = await _open_question(client, room)
    a = (await _join(client, code, "Sneha", KEY_A)).json()
    b = (await _join(client, code, "Rohan", KEY_B)).json()
    pa, pb = {"X-Live-Player": a["player_key"]}, {"X-Live-Player": b["player_key"]}
    await client.post(room.start.replace("/start", "/next"), json={"expected_index": -1}, headers=room.h)

    # Rohan's second phone answers first; nothing it is told says if it was right.
    got = await client.post(f"{room.base}/quiz/answer", json={"option_key": "a"}, headers=pb)
    assert got.json() == {"correct": None, "points": None, "score": 0, "answered": True}
    state = (await client.get(f"{room.base}/quiz/play", headers=pb)).json()
    q = state["question"]
    assert (q["my_answer"], q["my_points"], q["my_correct"], q["correct_key"]) == ("a", None, None, None)
    assert state["score"] == 0 and state["rank"] is None and state["leaderboard"] == []
    # Nor does anyone else's view: Sneha's, the big screen's.
    other = (await client.get(f"{room.base}/quiz/play", headers=pa)).json()
    assert other["question"]["correct_key"] is None and other["leaderboard"] == [] and other["rank"] is None
    screen = (await client.get(f"{room.base}/screen", headers=room.h)).json()["quiz"]["question"]
    assert screen["correct_key"] is None and not any(c["correct"] for c in screen["counts"])
    # The host sees it all.
    host = (await client.get(f"{room.base}/quiz/host", headers=room.h)).json()["question"]
    assert host["correct_key"] == "a" and [c["correct"] for c in host["counts"]] == [True, False]

    # The clock is out (20 of 20 s) but answers count for the grace: still nothing shown, and an answer is taken.
    await _opened_ago(session, room, 20.5)
    held = (await client.get(f"{room.base}/quiz/play", headers=pa)).json()["question"]
    assert held["time_left"] == 0 and held["correct_key"] is None
    late = await client.post(f"{room.base}/quiz/answer", json={"option_key": "b"}, headers=pa)
    assert late.status_code == 200 and late.json()["correct"] is None

    # Answers close: everything is shown.
    await _opened_ago(session, room, 22.5)
    assert (await client.post(f"{room.base}/quiz/answer", json={"option_key": "a"}, headers=pa)).status_code == 409
    rohan = (await client.get(f"{room.base}/quiz/play", headers=pb)).json()
    q = rohan["question"]
    assert q["correct_key"] == "a" and q["my_correct"] is True and q["my_points"] >= 500
    assert rohan["score"] == q["my_points"] and rohan["rank"] == 1 and rohan["leaderboard"][0]["name"] == "Rohan"
    sneha = (await client.get(f"{room.base}/quiz/play", headers=pa)).json()["question"]
    assert (sneha["my_answer"], sneha["my_correct"], sneha["my_points"]) == ("b", False, 0)
    screen = (await client.get(f"{room.base}/screen", headers=room.h)).json()["quiz"]["question"]
    assert screen["correct_key"] == "a" and [c["correct"] for c in screen["counts"]] == [True, False]


async def test_the_reveal_is_at_the_clock_and_its_grace(session, room):
    opened = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    assert room_mod.reveal_at(opened, 20) == opened + timedelta(seconds=22)
    assert not room_mod.revealed(opened, 20, opened + timedelta(seconds=22))
    assert room_mod.revealed(opened, 20, opened + timedelta(seconds=22, milliseconds=1))
    assert not room_mod.revealed(None, 20, opened + timedelta(hours=1))


# ─── One player per person ─────────────────────────────────────────────


async def test_the_same_key_or_account_in_the_same_run_is_the_same_player(client, session, room):
    code = await _open_question(client, room)
    first = await _join(client, code, "Sneha J", KEY_A)
    assert first.status_code == 201, first.text
    again = (await _join(client, code, "Someone else", KEY_A)).json()
    assert (again["player_id"], again["player_key"], again["name"]) == (first.json()["player_id"], first.json()["player_key"], "Sneha J")
    # The browser's room key works as well when the body has none.
    by_header = (await _join(client, code, "Sneha", headers=_k(KEY_A))).json()
    assert by_header["player_id"] == first.json()["player_id"]
    # The secret is not the key the browser shows the room.
    assert first.json()["player_key"] != KEY_A
    other = (await _join(client, code, "Rohan", KEY_B)).json()
    assert other["player_id"] != first.json()["player_id"]
    # A client from before (no key at all): a new player each time, as it was.
    anon = [(await _join(client, code, "Anon")).json()["player_id"] for _ in range(2)]
    assert anon[0] != anon[1]
    assert (await _join(client, code, "Bad", "no spaces allowed")).status_code == 422

    # Signed in, the account is the player on every device, and bound to the registration.
    phone = (await _join(client, code, "Sneha (phone)", "phone-install-1", headers=room.sneha_h)).json()
    laptop = (await _join(client, code, "Sneha (laptop)", KEY_B, headers=room.sneha_h)).json()
    assert phone["player_id"] == laptop["player_id"] and phone["player_key"] == laptop["player_key"] and laptop["name"] == "Sneha (phone)"
    row = await session.get(LiveQuizPlayers, phone["player_id"])
    assert row.registration_id is not None

    # A new run (restarted) is a new player for the same key.
    await client.post(room.start, json={"restart": True, "notify": False}, headers=room.h)
    code2 = (await client.get(f"{room.base}/quiz/host", headers=room.h)).json()["join_code"]
    fresh = (await _join(client, code2, "Sneha J", KEY_A)).json()
    assert fresh["player_key"] != first.json()["player_key"]
    assert (await client.get(f"{room.base}/quiz/play", headers={"X-Live-Player": first.json()["player_key"]})).status_code == 401


async def test_joins_are_limited_per_key_and_per_address(client, room, monkeypatch):
    from app.api.live import limits
    from app.core.rate_limit import limiter

    code = await _open_question(client, room)
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    for _ in range(10):
        assert (await _join(client, code, "Again", "stuck-button-01")).status_code == 201
    stuck = await _join(client, code, "Again", "stuck-button-01")
    assert stuck.status_code == 429 and stuck.json()["error_code"] == "RATE_LIMITED"
    # Another key from the same address is another person.
    assert (await _join(client, code, "Next", "stuck-button-02")).status_code == 201
    # The account is the person when signed in, whatever key it sends.
    for i in range(10):
        assert (await _join(client, code, "Sneha", f"rotating-key-{i:04d}", headers=room.sneha_h)).status_code == 201
    assert (await _join(client, code, "Sneha", "rotating-key-9999", headers=room.sneha_h)).status_code == 429
    # And an address has a ceiling.
    monkeypatch.setitem(limits.RULES, "join", limits.Rule("10/minute", "2/minute"))
    limiter.reset()
    for i in range(2):
        assert (await _join(client, code, "Flood", f"flood-key-{i:04d}", headers={"x-forwarded-for": "106.51.100.77"})).status_code == 201
    assert (await _join(client, code, "Flood", "flood-key-9999", headers={"x-forwarded-for": "106.51.100.77"})).status_code == 429
    limiter.reset()


# ─── One vote per account ──────────────────────────────────────────────


async def test_a_signed_in_person_has_one_vote_one_question_flag_and_one_answer_across_devices(client, session, room):
    await client.patch(f"{room.base}/questions/settings", json={"approve_first": False, "notify": False}, headers=room.h)
    asked = await client.post(f"{room.base}/questions", json={"text": "Will the slides be shared?"}, headers={**_k(KEY_A), **room.sneha_h})
    assert asked.status_code == 201 and asked.json()["mine"] is True
    qid = asked.json()["id"]

    # Her phone (key A) votes; her laptop (key B), signed in as her, takes the same vote back.
    v1 = (await client.post(f"{room.base}/questions/{qid}/vote", headers={**_k(KEY_A), **room.sneha_h})).json()
    assert v1["voted"] is True and v1["votes"] == 1
    v2 = (await client.post(f"{room.base}/questions/{qid}/vote", headers={**_k(KEY_B), **room.sneha_h})).json()
    assert v2["voted"] is False and v2["votes"] == 0
    await client.post(f"{room.base}/questions/{qid}/vote", headers={**_k(KEY_B), **room.sneha_h})
    voters = (await session.scalars(select(LiveQuestionVotes.voter_key).where(LiveQuestionVotes.question_id == qid))).all()
    assert voters == [f"u:{room.ids['sneha']}"]
    # A guest's key still votes as itself.
    assert (await client.post(f"{room.base}/questions/{qid}/vote", headers=_k("guest-key-0001"))).json()["votes"] == 2

    # The feed shows her her own flags whichever browser asks, signed in.
    room_mod.rooms.clear()
    feed = (await client.get(room.base, headers={**_k("third-browser-1"), **room.sneha_h})).json()
    mine = next(q for q in feed["questions"] if q["id"] == qid)
    assert mine["voted"] is True and mine["mine"] is True
    plain = (await client.get(room.base, headers=_k("third-browser-1"))).json()
    assert next(q for q in plain["questions"] if q["id"] == qid)["voted"] is False

    # One poll answer per account: the second device changes it.
    await client.post(room.poll_status, json={"status": "live", "notify": False}, headers=room.h)
    await client.post(f"{room.base}/polls/{room.poll_id}/respond", json={"value": "Hardware"}, headers={**_k(KEY_A), **room.sneha_h})
    changed = (await client.post(f"{room.base}/polls/{room.poll_id}/respond", json={"value": "Software"}, headers={**_k(KEY_B), **room.sneha_h})).json()
    assert changed["results"]["responses"] == 1 and changed["my_response"] == "Software"

    # A client cannot claim an account by sending its key form.
    assert (await client.post(f"{room.base}/questions/{qid}/vote", headers=_k(f"u:{room.ids['rohan']}"))).status_code == 422
    # A dead token is a visitor: the browser's key votes.
    dead = {"Authorization": "Bearer not-a-token"}
    assert (await client.post(f"{room.base}/questions/{qid}/vote", headers={**_k("guest-key-0002"), **dead})).json()["voted"] is True


# ─── An update's push ──────────────────────────────────────────────────


async def test_an_updates_push_respects_mutes_and_goes_from_a_job(client, session, room, fcm):
    sent = await client.post(f"{room.base}/updates", json={"kind": "room", "text": "Talks move to Hall B", "audience": "registered"}, headers=room.h)
    assert sent.status_code == 201, sent.text
    # Six registered; pushed to Sneha, Rohan and Kiran -- not Meera (muted), Sam (clubs off), the guest (no app).
    assert sent.json()["recipients"] == 6 and sent.json()["pushed"] == 3
    [job] = list((await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == service.UPDATE_PUSH_JOB))).all())
    assert sorted(job.payload["user_ids"]) == sorted(room.ids[k] for k in ("sneha", "rohan", "kiran")) and job.finished_at is not None
    [message] = fcm.multicasts
    assert sorted(message.tokens) == ["tok-kiran", "tok-rohan", "tok-sneha-1", "tok-sneha-2"]
    assert message.data["type"] == "event_live_update" and message.data["event_id"] == str(room.event_id) and message.data["kind"] == "room"
    # Everyone registered with an account still has the update in their inbox, muted or not.
    lines = list((await session.scalars(select(Notifications).where(Notifications.event_id == room.event_id, Notifications.type == service.UPDATE_TYPE))).all())
    assert sorted(n.user_id for n in lines) == sorted(room.ids.values())

    # Checked in only: Kiran (not there) is left out.
    fcm.multicasts.clear()
    there = (await client.post(f"{room.base}/updates", json={"text": "Pizza is here", "audience": "checked_in"}, headers=room.h)).json()
    assert there["recipients"] == 5 and there["pushed"] == 2
    assert sorted(fcm.multicasts[0].tokens) == ["tok-rohan", "tok-sneha-1", "tok-sneha-2"]


async def test_the_update_is_saved_and_the_push_waits_for_the_job(session, room, fcm, monkeypatch):
    calls = []

    async def counted(*a, **k):
        calls.append(a)
        return 0

    import app.core.notifications.service as push_mod

    monkeypatch.setattr(push_mod, "send_notification_to_users", counted)
    event = await session.get(Events, room.event_id)
    out = await service.push_update(session, event, "general", "Doors are open", "checked_in")
    assert out["pushed"] == 2 and calls == []  # nothing sent inside the request
    await jobs.settle()  # the job, once the update committed
    assert len(calls) == 1 and sorted(calls[0][1]) == sorted([room.ids["sneha"], room.ids["rohan"]])


# ─── The event goes live; Q&A opens ────────────────────────────────────


async def test_the_event_going_live_tells_everyone_registered_once(client, session, room, fcm):
    event = await session.get(Events, room.event_id)
    assert await closeout.advance_clock(session, now=event.event_datetime + timedelta(minutes=1)) == 1
    await jobs.settle()
    [message] = fcm.multicasts
    # Registered, checked in or not, with the app, not muted: Sneha, Rohan, Kiran.
    assert sorted(message.tokens) == ["tok-kiran", "tok-rohan", "tok-sneha-1", "tok-sneha-2"]
    push_id = message.data["push_id"]
    # The push's id is also its lines' key (fanout.line_id): a tap marks the reader's.
    assert message.data == {"type": "event_live_started", "event_id": str(room.event_id), "what": "room", "push_id": push_id, "notification_id": push_id}
    assert message.notification.body == LIVE_BODY
    [entry] = await _audits(session, room.event_id)
    assert entry.actor_id is None and entry.details["what"] == "room" and entry.summary.startswith("Told 3 registered people")
    lines = list((await session.scalars(select(Notifications).where(Notifications.event_id == room.event_id, Notifications.type == "event_live_started"))).all())
    assert sorted(n.user_id for n in lines) == sorted(room.ids[k] for k in ("sneha", "rohan", "kiran"))

    # Set back and live again by hand: not told twice.
    settings_url = f"/api/v1/events/{room.event_id}/closeout/settings"
    assert (await client.patch(settings_url, json={"status": "published"}, headers=room.h)).status_code == 200
    await session.execute(update(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION).values(created_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await session.commit()
    live = await client.patch(settings_url, json={"status": "live"}, headers=room.h)
    assert live.status_code == 200 and live.json()["status"] == "live"
    await jobs.settle()
    assert len(fcm.multicasts) == 1
    assert (await notify.tell_room(session, await session.get(Events, room.event_id), "room"))["skipped_reason"] == "already_sent"


async def test_a_live_event_held_by_the_limit_is_told_when_the_gap_ends(client, session, room, fcm):
    await client.post(room.start, headers=room.h)  # the quiz push, a moment ago
    assert len(fcm.multicasts) == 1
    event = await session.get(Events, room.event_id)
    event.lifecycle = EventStatus.live
    await notify.queue_event_live(session, room.event_id)
    await session.commit()
    await jobs.settle()
    assert len(fcm.multicasts) == 1  # held by the three minutes
    [again] = list((await session.scalars(select(jobs.Jobs).where(jobs.Jobs.name == notify.STARTED_JOB, jobs.Jobs.finished_at.is_(None)))).all())
    assert again.payload == {"event_id": room.event_id, "try": 2}
    wait = (again.run_at - datetime.now(timezone.utc)).total_seconds()
    assert 170 <= wait <= 182
    # The gap over, the job runs: told.
    await session.execute(update(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=4)))
    await session.execute(update(jobs.Jobs).where(jobs.Jobs.id == again.id).values(run_at=datetime.now(timezone.utc)))
    await session.commit()
    await jobs.run_due(ids=[again.id])
    await jobs.settle()  # the push it wrote
    assert len(fcm.multicasts) == 2 and fcm.multicasts[1].data["what"] == "room"


async def test_qa_opening_tells_the_room_once_and_the_console_can_say_no(client, session, room, fcm):
    asked = []
    for i, text in enumerate(("How long is the break?", "Is there parking?")):
        asked.append((await client.post(f"{room.base}/questions", json={"text": text}, headers=_k(f"asker-key-{i:04d}"))).json()["id"])

    # Approved with the switch off: nobody told.
    quiet = await client.post(f"{room.base}/questions/{asked[0]}", json={"action": "approve", "notify": False}, headers=room.h)
    assert quiet.status_code == 200 and quiet.json()["status"] == "approved" and quiet.json()["notified"] is None
    assert fcm.multicasts == []
    # A reject never tells.
    rejected = await client.post(f"{room.base}/questions/{asked[1]}", json={"action": "reject"}, headers=room.h)
    assert rejected.json()["notified"] is None

    # The next approval as a console from before sends it: told (checked in, not muted).
    told = await client.post(f"{room.base}/questions/{asked[1]}", json={"action": "approve"}, headers=room.h)
    assert told.json()["notified"] == _sent(2)
    [message] = fcm.multicasts
    assert message.data["type"] == "event_live_qa" and message.data["what"] == "qa" and message.notification.body == QA_BODY
    assert sorted(message.tokens) == ["tok-rohan", "tok-sneha-1", "tok-sneha-2"]

    # Once per event: a pin, approval off, any later approval -- already sent.
    pinned = await client.post(f"{room.base}/questions/{asked[1]}", json={"action": "pin"}, headers=room.h)
    assert pinned.json()["notified"]["skipped_reason"] == "already_sent"
    off = await client.patch(f"{room.base}/questions/settings", json={"approve_first": False}, headers=room.h)
    assert off.json()["approve_first"] is False and off.json()["notified"]["skipped_reason"] == "already_sent"
    on = await client.patch(f"{room.base}/questions/settings", json={"approve_first": True}, headers=room.h)
    assert on.json()["notified"] is None
    assert len(fcm.multicasts) == 1


async def test_qa_and_the_quiz_share_the_rooms_limit(client, session, room, fcm):
    await client.post(room.start, headers=room.h)
    off = await client.patch(f"{room.base}/questions/settings", json={"approve_first": False}, headers=room.h)
    n = off.json()["notified"]
    assert n["skipped_reason"] == "rate_limited" and n["sent"] == 0
    assert len(fcm.multicasts) == 1  # only the quiz's
    # Held, not spent: once the gap is over the next opening goes.
    await session.execute(update(AdminAuditLogs).where(AdminAuditLogs.action == notify.ACTION).values(created_at=datetime.now(timezone.utc) - timedelta(minutes=4)))
    await session.commit()
    again = await client.patch(f"{room.base}/questions/settings", json={"approve_first": False}, headers=room.h)
    assert again.json()["notified"] == _sent(2) and fcm.multicasts[-1].data["what"] == "qa"


# ─── Start and Next ────────────────────────────────────────────────────


async def test_start_on_a_running_quiz_and_a_second_next_are_refused(client, session, room):
    code = await _open_question(client, room, questions=2)
    player = (await _join(client, code, "Sneha", KEY_A)).json()
    nxt = room.start.replace("/start", "/next")

    running = await client.post(room.start, headers=room.h)
    assert running.status_code == 409 and running.json()["error_code"] == "QUIZ_RUNNING"
    assert (await client.get(f"{room.base}/quiz/play", headers={"X-Live-Player": player["player_key"]})).status_code == 200  # nobody wiped

    first = await client.post(nxt, json={"expected_index": -1}, headers=room.h)
    assert first.status_code == 200 and first.json()["current_index"] == 0
    double = await client.post(nxt, json={"expected_index": -1}, headers=room.h)
    assert double.status_code == 409 and double.json()["error_code"] == "QUIZ_MOVED_ON"
    assert (await client.get(f"{room.base}/quiz/host", headers=room.h)).json()["current_index"] == 0
    # Without expected_index (a console from before), Next moves as it did.
    assert (await client.post(nxt, headers=room.h)).json()["current_index"] == 1
    ended = await client.post(nxt, json={"expected_index": 1}, headers=room.h)
    assert ended.json()["status"] == "ended"
    late = await client.post(nxt, json={"expected_index": 1}, headers=room.h)
    assert late.status_code == 409 and late.json()["error_code"] == "QUIZ_MOVED_ON"
    assert (await client.post(nxt, headers=room.h)).status_code == 409  # not running

    # Restarting asks for it, and clears the players.
    again = await client.post(room.start, json={"notify": False}, headers=room.h)  # ended: starts clean, no restart needed
    assert again.status_code == 200 and again.json()["players"] == 0
    restarted = await client.post(room.start, json={"restart": True, "notify": False}, headers=room.h)
    assert restarted.status_code == 200 and restarted.json()["join_code"] != again.json()["join_code"]


# ─── The strip ─────────────────────────────────────────────────────────


async def test_the_strip_is_a_small_public_get_with_a_weak_etag(client, session, room, snapshots):
    url = f"{room.base}/banner"
    got = await client.get(url)
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"event", "banner", "poll", "quiz", "qa"}
    assert body["event"]["id"] == room.event_id and body["event"]["status"] == "published" and body["event"]["starts_at"]
    assert body["banner"] is None and body["poll"] is None and body["quiz"] == {"live": False, "title": "Hack Night 04 quiz"}
    assert body["qa"] == {"open": True, "has_questions": False}
    assert len(got.content) < 500
    assert got.headers["cache-control"] == "public, max-age=15" and got.headers["etag"].startswith('W/"')
    # Asked again with its tag: 304 while nothing changed, from the snapshot with no statement.
    await client.get(url)
    with statements(session) as seen:
        same = await client.get(url, headers={"If-None-Match": got.headers["etag"]})
    assert same.status_code == 304 and same.content == b"" and seen == []

    # What changes it: an update, a poll, the quiz.
    await client.post(f"{room.base}/updates", json={"text": "Doors open at seven"}, headers=room.h)
    await client.post(room.poll_status, json={"status": "live", "notify": False}, headers=room.h)
    await client.post(room.start, json={"notify": False}, headers=room.h)
    room_mod.rooms.clear()
    now = await client.get(url, headers={"If-None-Match": got.headers["etag"]})
    assert now.status_code == 200
    body = now.json()
    assert body["banner"]["text"] == "Doors open at seven" and body["poll"]["prompt"] == "Which track?" and body["quiz"]["live"] is True
    assert set(body["poll"]) == {"id", "kind", "prompt"}

    # Where the room is not shown, neither is the strip.
    event = await session.get(Events, room.event_id)
    event.lifecycle = EventStatus.draft
    await session.commit()
    room_mod.rooms.clear()
    assert (await client.get(url)).status_code == 404
