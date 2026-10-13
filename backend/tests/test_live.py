"""The live room: the quiz the host drives, the floor's questions, polls, the
feedback survey after, and the updates pushed to phones."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.live import service
from tests.conftest import auth_header

START = datetime.now(timezone.utc) + timedelta(hours=1)
KEY_A = "browser-aaaaaaaa"
KEY_B = "browser-bbbbbbbb"


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Circuit Society", email="live@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=START, duration=3.0, location_name="Lab 3")
    a = await make_user(email="sneha-l@example.com")
    b = await make_user(email="rohan-l@example.com")
    c = await make_user(email="anon-l@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Sneha J")
    r_b = await make_registration(event=event, user=b, full_name="Rohan P")
    await make_registration(event=event, user=c, full_name="Not Here")
    session.add(CheckIns(event_id=event.id, registration_id=r_a.id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate A"))
    session.add(CheckIns(event_id=event.id, registration_id=r_b.id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate A"))
    await session.commit()
    other_owner, _other = await make_club(name="Other", email="other-live@example.com")
    return {"event": event, "club": club, "owner": owner, "a": r_a, "b": r_b, "h": auth_header(await mint_token(owner)), "other": auth_header(await mint_token(other_owner)), "a_user": a}


def _u(world, path=""):
    return f"/api/v1/events/{world['event'].id}/live{path}"


def _q(prompt, correct="a", seconds=20):
    return {"prompt": prompt, "options": [{"key": "a", "label": "512 KB"}, {"key": "b", "label": "8 MB"}, {"key": "c", "label": "64 KB"}], "correct_key": correct, "seconds": seconds}


# --- quiz ----------------------------------------------------------------------


async def test_the_host_runs_the_quiz_and_the_room_plays_it_by_code(client, session, world):
    h = world["h"]
    quiz = await client.get(_u(world, "/quiz"), headers=h)
    assert quiz.status_code == 200 and quiz.json()["status"] == "draft" and quiz.json()["questions"] == []
    assert (await client.post(_u(world, "/quiz/host/start"), headers=h)).status_code == 422  # nothing to ask yet

    q1 = await client.post(_u(world, "/quiz/questions"), json=_q("RAM on an ESP32-S3?"), headers=h)
    assert q1.status_code == 201, q1.text
    q2 = await client.post(_u(world, "/quiz/questions"), json=_q("int8 saves mainly…", correct="b", seconds=10), headers=h)
    assert q2.status_code == 201
    bad = await client.post(_u(world, "/quiz/questions"), json={**_q("x"), "correct_key": "z"}, headers=h)
    assert bad.status_code == 422
    assert (await client.get(_u(world, "/quiz"), headers=world["other"])).status_code in (403, 404)

    started = await client.post(_u(world, "/quiz/host/start"), headers=h)
    assert started.status_code == 200, started.text
    code = started.json()["join_code"]
    assert len(code) == 6 and started.json()["status"] == "live" and started.json()["current_index"] == -1

    assert (await client.post("/api/v1/live/quiz/join", json={"code": "000000", "name": "Nobody"})).status_code == 404
    join_a = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "  Sneha   J "})
    assert join_a.status_code == 201, join_a.text
    assert join_a.json()["name"] == "Sneha J" and join_a.json()["event_id"] == world["event"].id
    pa = {"X-Live-Player": join_a.json()["player_key"]}
    join_b = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Rohan"})
    pb = {"X-Live-Player": join_b.json()["player_key"]}
    assert (await client.get(_u(world, "/quiz/play"), headers={"X-Live-Player": "nope"})).status_code == 401

    lobby = await client.get(_u(world, "/quiz/play"), headers=pa)
    assert lobby.json()["status"] == "live" and lobby.json()["question"] is None and lobby.json()["total"] == 2
    assert (await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers=pa)).status_code == 409

    nxt = await client.post(_u(world, "/quiz/host/next"), headers=h)
    assert nxt.json()["current_index"] == 0 and nxt.json()["players"] == 2 and nxt.json()["question"]["correct_key"] == "a"
    play = await client.get(_u(world, "/quiz/play"), headers=pa)
    assert play.json()["question"]["prompt"] == "RAM on an ESP32-S3?" and play.json()["question"]["correct_key"] is None  # not revealed yet
    assert "correct_key" not in play.json()["question"]["options"][0]

    right = await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers=pa)
    assert right.status_code == 200, right.text
    assert right.json()["correct"] is True and 900 <= right.json()["points"] <= 1000
    assert (await client.post(_u(world, "/quiz/answer"), json={"option_key": "b"}, headers=pa)).status_code == 409  # once
    wrong = await client.post(_u(world, "/quiz/answer"), json={"option_key": "c"}, headers=pb)
    assert wrong.json() == {"correct": False, "points": 0, "score": 0, "correct_key": "a"}
    assert (await client.post(_u(world, "/quiz/answer"), json={"option_key": "q"}, headers=pb)).status_code == 422

    host = await client.get(_u(world, "/quiz/host"), headers=h)
    tally = host.json()["question"]
    assert tally["answered"] == 2 and [c["count"] for c in tally["counts"]] == [1, 0, 1]
    assert host.json()["leaderboard"][0]["name"] == "Sneha J" and host.json()["leaderboard"][1]["score"] == 0

    mine = await client.get(_u(world, "/quiz/play"), headers=pa)
    assert mine.json()["question"]["my_answer"] == "a" and mine.json()["question"]["correct_key"] == "a" and mine.json()["rank"] == 1

    # The clock runs out: a late answer is refused and the right answer shows for everyone.
    quiz_row = await service.quiz_of(session, world["event"])
    quiz_row.question_opened_at = datetime.now(timezone.utc) - timedelta(seconds=40)
    await session.commit()
    late = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Late"})
    assert (await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers={"X-Live-Player": late.json()["player_key"]})).status_code == 409
    assert (await client.get(_u(world, "/quiz/play"), headers={"X-Live-Player": late.json()["player_key"]})).json()["question"]["correct_key"] == "a"

    assert (await client.post(_u(world, "/quiz/host/next"), headers=h)).json()["current_index"] == 1
    ended = await client.post(_u(world, "/quiz/host/next"), headers=h)  # past the last question
    assert ended.json()["status"] == "ended" and ended.json()["join_code"] is None
    final = await client.get(_u(world, "/quiz/play"), headers=pa)
    assert final.json()["status"] == "ended" and final.json()["leaderboard"][0] == {"rank": 1, "name": "Sneha J", "score": right.json()["score"]}

    # Hosting again starts clean.
    again = await client.post(_u(world, "/quiz/host/start"), headers=h)
    assert again.json()["players"] == 0 and again.json()["join_code"] != code
    assert (await client.get(_u(world, "/quiz/play"), headers=pa)).status_code == 401


# --- Q&A ----------------------------------------------------------------------


async def test_questions_wait_for_the_organiser_then_the_room_votes_them_up(client, world):
    h = world["h"]
    assert (await client.post(_u(world, "/questions"), json={"text": "Is there a power bank station?"})).status_code == 422  # no browser key
    asked = await client.post(_u(world, "/questions"), json={"text": "Is there a power bank station? My laptop is at 12%", "author_name": "Sneha J."}, headers={"X-Live-Key": KEY_A})
    assert asked.status_code == 201, asked.text
    assert asked.json()["status"] == "pending" and asked.json()["mine"] is True
    spam = await client.post(_u(world, "/questions"), json={"text": "buy cheap followers at ..."}, headers={"X-Live-Key": KEY_B})
    tracks = await client.post(_u(world, "/questions"), json={"text": "Can teams switch tracks halfway?"}, headers={"X-Live-Key": KEY_B})

    board = await client.get(_u(world, "/questions"), headers=h)
    assert board.json()["approve_first"] is True and len(board.json()["to_review"]) == 3 and board.json()["board"] == []
    # The room sees nothing yet.
    feed = await client.get(_u(world), headers={"X-Live-Key": KEY_B})
    assert feed.json()["questions"] == [] and feed.json()["qa"] == {"approve_first": True, "open": True}

    assert (await client.post(_u(world, f"/questions/{asked.json()['id']}"), json={"action": "approve"}, headers=h)).json()["status"] == "approved"
    assert (await client.post(_u(world, f"/questions/{spam.json()['id']}"), json={"action": "reject"}, headers=h)).json()["status"] == "rejected"
    assert (await client.post(_u(world, f"/questions/{tracks.json()['id']}"), json={"action": "approve"}, headers=h)).status_code == 200

    v1 = await client.post(_u(world, f"/questions/{asked.json()['id']}/vote"), headers={"X-Live-Key": KEY_B})
    assert v1.json()["votes"] == 1 and v1.json()["voted"] is True
    v2 = await client.post(_u(world, f"/questions/{asked.json()['id']}/vote"), headers={"X-Live-Key": KEY_B})
    assert v2.json()["votes"] == 0 and v2.json()["voted"] is False  # taken back
    await client.post(_u(world, f"/questions/{asked.json()['id']}/vote"), headers={"X-Live-Key": KEY_B})
    await client.post(_u(world, f"/questions/{asked.json()['id']}/vote"), headers={"X-Live-Key": KEY_A})

    feed = await client.get(_u(world), headers={"X-Live-Key": KEY_B})
    qs = feed.json()["questions"]
    assert [q["text"][:8] for q in qs] == ["Is there", "Can team"] and qs[0]["votes"] == 2 and qs[0]["voted"] is True and qs[1]["voted"] is False
    assert qs[1]["mine"] is True and qs[0]["mine"] is False

    pinned = await client.post(_u(world, f"/questions/{tracks.json()['id']}"), json={"action": "pin"}, headers=h)
    assert pinned.json()["pinned"] is True
    pinned2 = await client.post(_u(world, f"/questions/{asked.json()['id']}"), json={"action": "pin"}, headers=h)
    assert pinned2.json()["pinned"] is True
    screen = await client.get(_u(world, "/screen"), headers=h)
    assert screen.json()["pinned"]["id"] == asked.json()["id"] and [b["id"] for b in screen.json()["board"]] == [tracks.json()["id"]]  # one pin at a time

    done = await client.post(_u(world, f"/questions/{asked.json()['id']}"), json={"action": "answered"}, headers=h)
    assert done.json()["status"] == "answered" and done.json()["pinned"] is False and done.json()["answered_at"]
    board = await client.get(_u(world, "/questions"), headers=h)
    assert len(board.json()["answered"]) == 1 and len(board.json()["board"]) == 1 and board.json()["rejected"] == 1
    assert (await client.post(_u(world, f"/questions/{asked.json()['id']}"), json={"action": "reopen"}, headers=h)).json()["status"] == "approved"

    # Switch approval off: a new question goes straight to the board.
    off = await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=h)
    assert off.json()["approve_first"] is False
    direct = await client.post(_u(world, "/questions"), json={"text": "Will boards be available to take home?"}, headers={"X-Live-Key": KEY_A})
    assert direct.json()["status"] == "approved"
    assert (await client.post(_u(world, f"/questions/{uuid.uuid4()}"), json={"action": "approve"}, headers=h)).status_code == 404


# --- polls ----------------------------------------------------------------------


async def test_one_poll_runs_at_a_time_and_each_kind_counts_its_own_way(client, world):
    h = world["h"]
    words = await client.post(_u(world, "/polls"), json={"kind": "words", "prompt": "What are you building tonight?"}, headers=h)
    assert words.status_code == 201 and words.json()["status"] == "draft" and words.json()["results"] == {"responses": 0, "words": []}
    choice = await client.post(_u(world, "/polls"), json={"kind": "choice", "prompt": "How comfortable are you with embedded C?", "options": ["Never touched it", "A little", "Comfortable", "I dream in pointers"]}, headers=h)
    assert choice.status_code == 201
    assert (await client.post(_u(world, "/polls"), json={"kind": "choice", "prompt": "One answer?", "options": ["only"]}, headers=h)).status_code == 422
    rating = await client.post(_u(world, "/polls"), json={"kind": "rating", "prompt": "Rate the keynote"}, headers=h)
    assert rating.json()["results"]["rating"]["average"] is None

    assert (await client.post(_u(world, f"/polls/{words.json()['id']}/respond"), json={"value": "wake word"}, headers={"X-Live-Key": KEY_A})).status_code == 409  # not launched
    live = await client.post(_u(world, f"/polls/{words.json()['id']}/status"), json={"status": "live"}, headers=h)
    assert live.json()["status"] == "live" and live.json()["launched_at"]
    feed = await client.get(_u(world), headers={"X-Live-Key": KEY_A})
    assert feed.json()["poll"]["id"] == words.json()["id"] and feed.json()["poll"]["my_response"] is None

    r = await client.post(_u(world, f"/polls/{words.json()['id']}/respond"), json={"value": "  Wake  Word "}, headers={"X-Live-Key": KEY_A})
    assert r.status_code == 200 and r.json()["my_response"] == "wake word"
    await client.post(_u(world, f"/polls/{words.json()['id']}/respond"), json={"value": "wake word"}, headers={"X-Live-Key": KEY_B})
    changed = await client.post(_u(world, f"/polls/{words.json()['id']}/respond"), json={"value": "gesture control"}, headers={"X-Live-Key": KEY_B})
    assert changed.json()["results"] == {"responses": 2, "words": [{"word": "wake word", "count": 1}, {"word": "gesture control", "count": 1}]}

    # Launching the choice poll closes the word cloud.
    live2 = await client.post(_u(world, f"/polls/{choice.json()['id']}/status"), json={"status": "live"}, headers=h)
    assert live2.json()["status"] == "live"
    polls = await client.get(_u(world, "/polls"), headers=h)
    by = {p["id"]: p for p in polls.json()}
    assert by[words.json()["id"]]["status"] == "closed" and by[words.json()["id"]]["closed_at"]
    assert (await client.post(_u(world, f"/polls/{words.json()['id']}/respond"), json={"value": "late"}, headers={"X-Live-Key": KEY_A})).status_code == 409
    assert (await client.patch(_u(world, f"/polls/{words.json()['id']}"), json={"options": ["x", "y"]}, headers=h)).status_code == 409

    assert (await client.post(_u(world, f"/polls/{choice.json()['id']}/respond"), json={"value": "Something else"}, headers={"X-Live-Key": KEY_A})).status_code == 422
    await client.post(_u(world, f"/polls/{choice.json()['id']}/respond"), json={"value": "A little"}, headers={"X-Live-Key": KEY_A})
    res = await client.post(_u(world, f"/polls/{choice.json()['id']}/respond"), json={"value": "A little"}, headers={"X-Live-Key": KEY_B})
    assert res.json()["results"]["choices"][1] == {"label": "A little", "count": 2, "pct": 100}

    closed = await client.post(_u(world, f"/polls/{choice.json()['id']}/status"), json={"status": "closed"}, headers=h)
    assert closed.json()["status"] == "closed"
    assert (await client.get(_u(world), headers={"X-Live-Key": KEY_A})).json()["poll"] is None

    await client.post(_u(world, f"/polls/{rating.json()['id']}/status"), json={"status": "live"}, headers=h)
    assert (await client.post(_u(world, f"/polls/{rating.json()['id']}/respond"), json={"value": "7"}, headers={"X-Live-Key": KEY_A})).status_code == 422
    await client.post(_u(world, f"/polls/{rating.json()['id']}/respond"), json={"value": "5"}, headers={"X-Live-Key": KEY_A})
    r5 = await client.post(_u(world, f"/polls/{rating.json()['id']}/respond"), json={"value": "4"}, headers={"X-Live-Key": KEY_B})
    assert r5.json()["results"]["rating"]["average"] == 4.5 and r5.json()["results"]["rating"]["histogram"][4] == {"value": 5, "count": 1, "pct": 50}

    assert (await client.delete(_u(world, f"/polls/{rating.json()['id']}"), headers=h)).status_code == 200
    assert len((await client.get(_u(world, "/polls"), headers=h)).json()) == 2


# --- feedback ----------------------------------------------------------------------


async def test_the_survey_goes_to_whoever_was_there_and_each_link_is_theirs(client, session, world, monkeypatch):
    h = world["h"]
    sent = []
    monkeypatch.setattr(service.email_mod, "send_email", lambda **kw: sent.append(kw))

    fb = await client.get(_u(world, "/feedback"), headers=h)
    assert fb.status_code == 200, fb.text
    assert fb.json()["auto_send"] is True and fb.json()["delay_minutes"] == 60 and [q["kind"] for q in fb.json()["questions"]] == ["rating", "rating", "nps", "text"]
    assert fb.json()["sends_at"] and fb.json()["results"]["responses"] == 0 and fb.json()["checked_in"] == 2 and fb.json()["previous"] is None

    put = await client.put(_u(world, "/feedback"), json={"delay_minutes": 30, "questions": [{"key": "overall", "kind": "rating", "label": "How was it?"}, {"key": "recommend", "kind": "nps", "label": "Recommend us?"}, {"key": "change", "kind": "text", "label": "What should change?"}]}, headers=h)
    assert put.status_code == 200 and put.json()["delay_minutes"] == 30 and len(put.json()["questions"]) == 3
    assert (await client.put(_u(world, "/feedback"), json={"questions": []}, headers=h)).status_code == 422

    # Not due yet: the event has not even started.
    assert await service.sweep(session) == 0

    go = await client.post(_u(world, "/feedback/send"), headers=h)
    assert go.status_code == 200, go.text
    assert go.json()["sent"] == 2 and go.json()["invited"] == 2 and go.json()["sent_at"]
    assert {m["recipients"][0] for m in sent} == {"sneha-l@example.com", "rohan-l@example.com"}
    link = next(line for line in sent[0]["text"].splitlines() if line.startswith("http"))
    assert link.startswith(f"https://events.myotherapp.in/feedback/{world['event'].id}/")
    path, _, token = link.partition("?k=")
    reg_id = path.rsplit("/", 1)[1]

    assert (await client.get(f"/api/v1/events/{world['event'].id}/feedback/{reg_id}?k=wrong")).status_code == 404
    form = await client.get(f"/api/v1/events/{world['event'].id}/feedback/{reg_id}?k={token}")
    assert form.status_code == 200 and form.json()["submitted"] is False and form.json()["event_name"] == "Hack Night 04"
    assert (await client.post(f"/api/v1/events/{world['event'].id}/feedback/{reg_id}?k={token}", json={"answers": {}})).status_code == 422
    ok = await client.post(f"/api/v1/events/{world['event'].id}/feedback/{reg_id}?k={token}", json={"answers": {"overall": 5, "recommend": 10, "change": " More time for hacking, less for talks. "}})
    assert ok.status_code == 200
    assert (await client.get(f"/api/v1/events/{world['event'].id}/feedback/{reg_id}?k={token}")).json()["submitted"] is True

    other_link = next(line for line in sent[1]["text"].splitlines() if line.startswith("http"))
    other_path, _, other_token = other_link.partition("?k=")
    other_reg = other_path.rsplit("/", 1)[1]
    await client.post(f"/api/v1/events/{world['event'].id}/feedback/{other_reg}?k={other_token}", json={"answers": {"overall": 4, "recommend": 6}})

    fb = await client.get(_u(world, "/feedback"), headers=h)
    r = fb.json()["results"]
    assert r["responses"] == 2 and r["invited"] == 2
    assert r["nps"] == {"score": 0, "promoters": 50, "passives": 0, "detractors": 50, "count": 2}
    assert r["averages"] == [{"key": "overall", "label": "How was it?", "average": 4.5, "count": 2}]
    assert r["texts"] == ["More time for hacking, less for talks."]
    assert fb.json()["sends_at"] is None  # already sent


async def test_the_survey_sends_itself_after_the_event_and_last_time_shows_beside_it(client, session, world, make_event, monkeypatch):
    sent = []
    monkeypatch.setattr(service.email_mod, "send_email", lambda **kw: sent.append(kw))
    h = world["h"]
    past = await make_event(club=world["club"], created_by=world["owner"], name="Hack Night 03", event_datetime=START - timedelta(days=30), duration=3.0)
    # Last time's survey with a reply.
    past_survey = await service.survey_of(session, past)
    past_survey.sent_at = START - timedelta(days=29)
    session.add(service.FeedbackResponses(survey_id=past_survey.id, registration_id=world["a"].id, answers={"overall": 4, "recommend": 9, "change": "Fans next time?"}))
    await session.commit()

    fb = await client.get(_u(world, "/feedback"), headers=h)
    assert fb.json()["previous"]["event_name"] == "Hack Night 03" and fb.json()["previous"]["nps"]["score"] == 100 and fb.json()["previous"]["texts"] == ["Fans next time?"]

    ends = START + timedelta(hours=3)
    assert await service.sweep(session, now=ends + timedelta(minutes=59)) == 0
    assert await service.sweep(session, now=ends + timedelta(minutes=61)) == 1
    assert len(sent) == 2
    assert await service.sweep(session, now=ends + timedelta(minutes=62)) == 0  # once

    # Switched off, nothing goes out by itself.
    late = await make_event(club=world["club"], created_by=world["owner"], name="Quiet one", event_datetime=START - timedelta(days=2), duration=1.0)
    await client.put(f"/api/v1/events/{late.id}/live/feedback", json={"auto_send": False}, headers=h)
    assert await service.sweep(session, now=START + timedelta(days=1)) == 0


# --- updates and the schedule ----------------------------------------------------------------------


async def test_an_update_reaches_the_room_and_shifting_a_block_sends_one(client, session, world, monkeypatch):
    h = world["h"]
    pushed = []

    async def fake_push(session_, user_ids, title, body, data=None, image_url=None):
        pushed.append({"user_ids": sorted(user_ids), "title": title, "body": body, "data": data})
        return len(user_ids)

    import app.core.notifications.service as push_mod

    monkeypatch.setattr(push_mod, "send_notification_to_users", fake_push)

    page = await client.get(_u(world, "/updates"), headers=h)
    assert page.json() == {"audience": {"registered": 3, "checked_in": 2}, "items": []}

    sent = await client.post(_u(world, "/updates"), json={"kind": "general", "text": "Doors are open — collect your board at the front desk", "audience": "registered"}, headers=h)
    assert sent.status_code == 201, sent.text
    assert sent.json()["recipients"] == 3 and sent.json()["pushed"] == 3
    assert pushed[0]["title"] == "Hack Night 04" and pushed[0]["data"]["type"] == "event_live_update" and len(pushed[0]["user_ids"]) == 3
    wifi = await client.post(_u(world, "/updates"), json={"kind": "general", "text": "Wi-Fi: CS-HackNight · password edge2026", "audience": "checked_in"}, headers=h)
    assert wifi.json()["recipients"] == 2
    assert (await client.post(_u(world, "/updates"), json={"text": "x"}, headers=h)).status_code == 422

    # In-app notifications were written too, one per guest.
    from sqlalchemy import select

    from app.api.notifications.models import Notifications

    notes = list((await session.scalars(select(Notifications).where(Notifications.event_id == world["event"].id))).all())
    assert len(notes) == 5 and {n.type for n in notes} == {"event_live_update"}
    assert any(n.description.startswith("Doors are open") and n.user_id == world["a_user"].id for n in notes)

    feed = await client.get(_u(world))
    assert feed.json()["banner"]["text"].startswith("Wi-Fi") and len(feed.json()["updates"]) == 2

    # A run of show to shift.
    a_url = f"/api/v1/events/{world['event'].id}/agenda"
    k = await client.post(a_url, json={"starts_at": (START).isoformat(), "ends_at": (START + timedelta(minutes=30)).isoformat(), "title": "Keynote"}, headers=h)
    hk = await client.post(a_url, json={"starts_at": (START + timedelta(minutes=30)).isoformat(), "ends_at": (START + timedelta(minutes=105)).isoformat(), "title": "Hack time"}, headers=h)
    dm = await client.post(a_url, json={"starts_at": (START + timedelta(minutes=150)).isoformat(), "title": "Demos"}, headers=h)
    assert k.status_code == 201 and hk.status_code == 201 and dm.status_code == 201

    shifted = await client.post(_u(world, "/schedule/shift"), json={"item_id": hk.json()["id"], "minutes": 10}, headers=h)
    assert shifted.status_code == 200, shifted.text
    assert shifted.json()["update"]["kind"] == "schedule" and "Hack time now starts at" in shifted.json()["update"]["text"] and "+10 min" in shifted.json()["update"]["text"]
    assert shifted.json()["update"]["recipients"] == 2  # checked-in only
    items = {i["title"]: i for i in shifted.json()["items"]}
    assert datetime.fromisoformat(items["Keynote"]["starts_at"]) == START
    assert datetime.fromisoformat(items["Hack time"]["starts_at"]) == START + timedelta(minutes=40)
    assert datetime.fromisoformat(items["Hack time"]["ends_at"]) == START + timedelta(minutes=115)
    assert datetime.fromisoformat(items["Demos"]["starts_at"]) == START + timedelta(minutes=160)
    assert (await client.post(_u(world, "/schedule/shift"), json={"item_id": str(uuid.uuid4()), "minutes": 10}, headers=h)).status_code == 404
    assert (await client.post(_u(world, "/schedule/shift"), json={"item_id": hk.json()["id"], "minutes": 0}, headers=h)).status_code == 422

    screen = await client.get(_u(world, "/screen"), headers=h)
    assert screen.json()["update"]["kind"] == "schedule" and screen.json()["quiz"] is None and screen.json()["poll"] is None
    assert (await client.get(_u(world, "/updates"), headers=world["other"])).status_code in (403, 404)
