"""The live room made the organiser's (CONTRACT.md): rounds, question kinds,
the round's settings, teams, winners, poll kinds and settings, the theme
and its pictures, the big screen's controls -- through the API, for each
role -- and what a client from before this release still sees."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.events.models import EventRegistrationsLink
from app.api.live import looks, room, service
from app.api.live.models import LiveQuizPlayers, LiveQuizzes
from tests.club_ops_helpers import member_header
from tests.conftest import auth_header
from tests.racing import beaten_to

START = datetime.now(timezone.utc) + timedelta(hours=1)
V2 = {"X-Live-Version": "2"}
#: Today's PlayerState and its question's fields: what a client from before reads.
PLAY_KEYS = {"status", "title", "name", "score", "current_index", "total", "question", "leaderboard", "rank"}
PLAY_QUESTION_KEYS = {"id", "prompt", "options", "seconds", "sort_order", "time_left", "my_answer", "my_points", "my_correct", "correct_key"}
FEED_KEYS = {"event", "banner", "updates", "poll", "questions", "qa", "quiz"}
POLL_KEYS = {"id", "kind", "prompt", "options", "status", "launched_at", "closed_at", "sort_order", "results", "my_response"}


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    looks.forget_for_tests()
    owner, club = await make_club(name="Quiz Society", email="quiz-custom@example.com")
    event = await make_event(club=club, created_by=owner, name="Quiz Night", event_datetime=START, duration=3.0, location_name="Hall")
    users, regs = [], []
    for i in range(3):
        u = await make_user(email=f"quizzer-{i}@example.com")
        r = await make_registration(event=event, user=u, full_name=f"Registered {i}")
        session.add(CheckIns(event_id=event.id, registration_id=r.id, via=CheckInVia.gate, checked_in_at=START, door_label="A"))
        users.append(u)
        regs.append(r)
    await session.commit()
    other_owner, _ = await make_club(name="Elsewhere", email="elsewhere-custom@example.com")
    h = auth_header(await mint_token(owner))
    return {
        "event": event,
        "eid": event.id,
        "club": club,
        "owner": owner,
        "h": h,
        "hv": {**h, **V2},
        "other": auth_header(await mint_token(other_owner)),
        "users": users,
        "regs": regs,
        "tokens": [auth_header(await mint_token(u)) for u in users],
    }


def _u(world, path=""):
    return f"/api/v1/events/{world['eid']}/live{path}"


def _event(world):
    """The event as the service needs it, without touching the row (a
    rollback in a request expires the test's objects)."""
    from types import SimpleNamespace

    return SimpleNamespace(id=world["eid"], name="Quiz Night")


def _key(i: int) -> dict:
    return {"X-Live-Key": f"browser-{i:08d}"}


def _opts(n=4):
    return [{"key": k, "label": f"Answer {k.upper()}"} for k in "abcdef"[:n]]


async def _add(client, world, question: dict, path="/quiz/questions"):
    r = await client.post(_u(world, path), json={"seconds": 20, **question}, headers=world["hv"])
    assert r.status_code == 201, r.text
    return r.json()


async def _settings(client, world, change: dict):
    r = await client.patch(_u(world, "/quiz"), json={"settings": change}, headers=world["hv"])
    assert r.status_code == 200, r.text
    return r.json()


async def _start(client, world, path="/quiz/host/start"):
    r = await client.post(_u(world, path), json={"notify": False}, headers=world["hv"])
    assert r.status_code == 200, r.text
    return r.json()


async def _join(client, code, i, *, team=None, token=None, v2=True):
    body = {"code": code, "name": f"Player {i}", "player_key": f"browser-{i:08d}"}
    if team:
        body["team"] = team
    headers = {**(token or {}), **(V2 if v2 else {})}
    r = await client.post("/api/v1/live/quiz/join", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return {**r.json(), "h": {"X-Live-Player": r.json()["player_key"], **(V2 if v2 else {})}}


async def _next(client, world, path="/quiz/host/next"):
    r = await client.post(_u(world, path), headers=world["hv"])
    assert r.status_code == 200, r.text
    return r.json()


async def _play(client, world, p, v2=True):
    headers = dict(p["h"]) if v2 else {"X-Live-Player": p["player_key"]}
    r = await client.get(_u(world, "/quiz/play"), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _answer(client, world, p, body):
    return await client.post(_u(world, "/quiz/answer"), json=body, headers=p["h"])


async def _close(session, world, seconds=60):
    """The open question's clock (and its grace) run out."""
    quiz = await service.quiz_of(session, _event(world))
    quiz.question_opened_at = quiz.question_opened_at - timedelta(seconds=seconds)
    await session.commit()
    room.quizzes.forget(("quiz", world["eid"]))


# ─── What a client from before sees ──────────────────────────────────────


async def test_an_unedited_quiz_gives_every_client_todays_bodies(client, session, world):
    await client.post(
        _u(world, "/quiz/questions"), json={"prompt": "Two plus two?", "options": _opts(3), "correct_key": "b", "seconds": 20}, headers=world["h"]
    )
    started = (await client.post(_u(world, "/quiz/host/start"), headers=world["h"])).json()
    assert set(started) == {"status", "join_code", "title", "players", "current_index", "total", "question", "leaderboard", "notified"}
    old = await _join(client, started["join_code"], 1, v2=False)
    assert set(old) - {"h"} == {"event_id", "quiz_id", "title", "player_id", "player_key", "name"}
    new = await _join(client, started["join_code"], 2)
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    body = await _play(client, world, old, v2=False)
    assert set(body) == PLAY_KEYS and set(body["question"]) == PLAY_QUESTION_KEYS
    assert (await _answer(client, world, {"h": {"X-Live-Player": old["player_key"]}}, {"option_key": "b"})).json() == {
        "correct": None,
        "points": None,
        "score": 0,
        "answered": True,
    }
    v2 = await _play(client, world, new)
    assert v2["phase"] == "question" and v2["question"]["kind"] == "single" and v2["question"]["effective_points"] == 1000 and v2["theme_version"]
    await _close(session, world)
    told = await _play(client, world, old, v2=False)
    assert told["question"]["my_correct"] is True and 500 <= told["question"]["my_points"] <= 1000 and told["rank"] == 1
    assert told["leaderboard"][0] == {"rank": 1, "name": "Player 1", "score": told["question"]["my_points"]}
    # The console's own bodies, without the header, are today's too.
    host = (await client.get(_u(world, "/quiz/host"), headers=world["h"])).json()
    assert set(host) == {"status", "join_code", "title", "players", "current_index", "total", "question", "leaderboard"}
    assert set(host["question"]) == {"id", "prompt", "options", "correct_key", "seconds", "sort_order", "time_left", "opened_at", "answered", "counts"}
    quiz = (await client.get(_u(world, "/quiz"), headers=world["h"])).json()
    assert set(quiz["questions"][0]) == {"id", "prompt", "options", "correct_key", "seconds", "sort_order"}
    screen = (await client.get(_u(world, "/screen"), headers=world["h"])).json()
    assert set(screen) == {"event", "pinned", "board", "poll", "quiz", "update"}
    feed = (await client.get(_u(world))).json()
    assert set(feed) == FEED_KEYS
    assert "theme" in (await client.get(_u(world), headers=V2)).json()


async def test_new_kinds_degrade_for_an_old_client_and_true_false_and_polls_still_play(client, session, world):
    for q in (
        {"kind": "multi", "prompt": "Which are prime?", "options": _opts(4), "correct_keys": ["a", "c"]},
        {"kind": "type", "prompt": "Capital of India?", "accepted": ["New Delhi"]},
        {"kind": "order", "prompt": "Oldest first", "options": _opts(3)},
        {"kind": "true_false", "prompt": "The sky is blue", "correct_key": "a"},
        {"kind": "poll", "prompt": "Tea or coffee?", "options": _opts(2)},
    ):
        await _add(client, world, q)
    code = (await _start(client, world))["join_code"]
    old = await _join(client, code, 1, v2=False)
    old_h = {"h": {"X-Live-Player": old["player_key"]}}
    for i in range(3):
        await _next(client, world)
        body = await _play(client, world, old, v2=False)
        q = body["question"]
        assert set(body) == PLAY_KEYS and set(q) == PLAY_QUESTION_KEYS
        assert q["options"] == [] and q["prompt"].endswith(service.NEEDS_UPDATE) and q["correct_key"] is None
        assert (await _answer(client, world, old_h, {"option_key": "a"})).status_code == 422
        await _close(session, world)
        assert (await _play(client, world, old, v2=False))["question"]["my_points"] is None
    await _next(client, world)  # true or false: today's question
    tf = (await _play(client, world, old, v2=False))["question"]
    assert [o["label"] for o in tf["options"]] == ["True", "False"]
    assert (await _answer(client, world, old_h, {"option_key": "a"})).status_code == 200
    await _close(session, world)
    tf = (await _play(client, world, old, v2=False))["question"]
    assert tf["my_correct"] is True and tf["correct_key"] == "a" and tf["my_points"] >= 500
    await _next(client, world)  # a poll question: answered, never right or wrong
    assert (await _answer(client, world, old_h, {"option_key": "b"})).status_code == 200
    await _close(session, world)
    pq = (await _play(client, world, old, v2=False))["question"]
    assert pq["my_answer"] == "b" and pq["correct_key"] is None and pq["my_correct"] is None and pq["my_points"] == 0


# ─── Kinds, scored ───────────────────────────────────────────────────────


async def test_every_kind_is_answered_scored_and_revealed(client, session, world):
    await _settings(client, world, {"scoring": {"speed": "off"}})
    await _add(client, world, {"kind": "multi", "prompt": "Which are prime?", "options": _opts(4), "correct_keys": ["a", "c"], "multi_rule": "proportional"})
    await _add(client, world, {"kind": "type", "prompt": "Capital of Brazil's biggest city?", "accepted": ["São Paulo"], "match": "close"})
    await _add(client, world, {"kind": "order", "prompt": "Oldest first", "options": _opts(4), "order_rule": "partial"})
    await _add(client, world, {"kind": "single", "prompt": "Double?", "options": _opts(2), "correct_key": "a", "double": True, "points": 600})
    code = (await _start(client, world))["join_code"]
    a, b = await _join(client, code, 1), await _join(client, code, 2)

    await _next(client, world)
    assert (await _answer(client, world, a, {"option_keys": ["a"]})).status_code == 200
    assert (await _answer(client, world, b, {"option_keys": ["c", "a"]})).status_code == 200
    assert (await _answer(client, world, b, {"option_keys": ["a"]})).status_code == 409
    open_ = await _play(client, world, a)
    assert open_["question"]["my_response"] == {"option_keys": ["a"]} and open_["question"]["correct_keys"] is None and open_["question"]["my_credit"] is None
    host = (await client.get(_u(world, "/quiz/host"), headers=world["hv"])).json()["question"]
    assert (
        {c["key"]: c["count"] for c in host["counts"]} == {"a": 2, "b": 0, "c": 1, "d": 0} and host["fully_correct"] == 1 and host["correct_keys"] == ["a", "c"]
    )
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["quiz"]["question"]
    assert screen["correct_keys"] is None and not any(c["correct"] for c in screen["counts"])
    await _close(session, world)
    shown = (await _play(client, world, a))["question"]
    assert shown["my_points"] == 500 and shown["my_credit"] == 0.5 and shown["my_correct"] is False and shown["correct_keys"] == ["a", "c"]
    assert (await _play(client, world, b))["question"]["my_points"] == 1000

    await _next(client, world)
    assert (await _answer(client, world, a, {"text": "sao polo"})).status_code == 200
    assert (await _answer(client, world, b, {"text": "Rio"})).status_code == 200
    host = (await client.get(_u(world, "/quiz/host"), headers=world["hv"])).json()["question"]
    assert host["counts"] == [] and {t["text"]: t["correct"] for t in host["texts"]} == {"sao polo": True, "rio": False}
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["quiz"]["question"]["texts"] is None
    await _close(session, world)
    shown = (await _play(client, world, a))["question"]
    assert shown["my_points"] == 1000 and shown["accepted"] == ["São Paulo"] and shown["my_response"] == {"text": "sao polo"}

    await _next(client, world)
    seen = (await _play(client, world, a))["question"]["options"]
    assert [o["key"] for o in seen] != ["a", "b", "c", "d"] and sorted(o["key"] for o in seen) == ["a", "b", "c", "d"]
    assert (await _answer(client, world, a, {"order": ["a", "c", "b", "d"]})).status_code == 200
    assert (await _answer(client, world, b, {"order": ["a", "b"]})).status_code == 422
    await _close(session, world)
    shown = (await _play(client, world, a))["question"]
    assert shown["my_points"] == 500 and shown["correct_order"] == ["a", "b", "c", "d"]

    await _next(client, world)
    assert (await _play(client, world, a))["question"]["effective_points"] == 1200
    await _answer(client, world, a, {"option_key": "a"})
    await _close(session, world)
    assert (await _play(client, world, a))["question"]["my_points"] == 1200
    assert (await _play(client, world, a))["score"] == 500 + 1000 + 500 + 1200


async def test_questions_are_validated_per_kind_through_the_api(client, world):
    for bad in (
        {"kind": "multi", "prompt": "Which?", "options": _opts(3), "correct_keys": ["z"]},
        {"kind": "type", "prompt": "Which?", "accepted": []},
        {"kind": "single", "prompt": "Which?", "options": _opts(3), "correct_key": "z"},
        {"kind": "single", "prompt": "Which?", "options": _opts(2), "correct_key": "a", "seconds": 301},
    ):
        r = await client.post(_u(world, "/quiz/questions"), json=bad, headers=world["hv"])
        assert r.status_code == 422, (bad, r.text)
    q = await _add(client, world, {"kind": "true_false", "prompt": "Water is wet", "correct_key": "b"})
    assert q["kind"] == "true_false" and q["correct_key"] == "b" and [o["label"] for o in q["options"]] == ["True", "False"]
    changed = (await client.patch(_u(world, f"/quiz/questions/{q['id']}"), json={"kind": "type", "accepted": ["wet"]}, headers=world["hv"])).json()
    assert changed["kind"] == "type" and changed["options"] == [] and changed["accepted"] == ["wet"] and changed["correct_key"] is None


# ─── The round's settings ────────────────────────────────────────────────


async def test_negative_marking_streaks_and_the_floor(client, session, world):
    await _settings(client, world, {"scoring": {"speed": "off", "wrong_penalty": 0.25, "streak_from": 2, "streak_bonus": 100}})
    for _ in range(4):
        await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    a, b = await _join(client, code, 1), await _join(client, code, 2)
    plan = {"a": ["a", "a", "b", "a"], "b": ["b", None, "a", "a"]}
    for i in range(4):
        await _next(client, world)
        for who, p in (("a", a), ("b", b)):
            if plan[who][i]:
                assert (await _answer(client, world, p, {"option_key": plan[who][i]})).status_code == 200
        await _close(session, world)
    # a: 1000, 1000+100 (streak), -250, 1000 (streak broken) = 2850
    assert (await _play(client, world, a))["score"] == 2850
    # b: wrong at 0 floors to 0 (recorded 0), missed, 1000, 1000+100 = 2100
    assert (await _play(client, world, b))["score"] == 2100
    rows = (
        await session.execute(
            text("SELECT p.name, sum(a.points), p.score FROM live_quiz_answers a JOIN live_quiz_players p ON p.id = a.player_id GROUP BY p.id ORDER BY p.name")
        )
    ).all()
    assert [(n, int(s), int(t)) for n, s, t in rows] == [("Player 1", 2850, 2850), ("Player 2", 2100, 2100)]


async def test_answers_together_score_once_on_the_careful_path(client, session, world):
    await _settings(client, world, {"scoring": {"wrong_penalty": 0.5, "streak_from": 2}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    qid = (await client.get(_u(world, "/quiz/host"), headers=world["hv"])).json()["question"]["id"]
    with beaten_to(
        session,
        "live_quiz_answers",
        "INSERT INTO live_quiz_answers (id, question_id, player_id, option_key, correct, points, answered_at, created_at, updated_at) "
        "VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 'a', true, 900, now(), now(), now())",
        qid,
        p["player_id"],
    ) as fired:
        r = await _answer(client, world, p, {"option_key": "b"})
    assert fired and r.status_code == 409
    score = await session.scalar(select(LiveQuizPlayers.score).where(LiveQuizPlayers.id == p["player_id"]))
    assert score == 0  # the losing answer changed nothing


async def test_ties_by_time_rank_the_faster_first(client, session, world):
    await _settings(client, world, {"scoring": {"speed": "off"}, "ties": "time"})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    slow, fast = await _join(client, code, 1), await _join(client, code, 2)
    await _next(client, world)
    quiz = await service.quiz_of(session, _event(world))
    quiz.question_opened_at = quiz.question_opened_at - timedelta(seconds=10)
    await session.commit()
    room.quizzes.forget(("quiz", world["eid"]))
    await _answer(client, world, slow, {"option_key": "a"})
    await session.execute(text("UPDATE live_quiz_players SET saved_ms = saved_ms + 5000 WHERE id = :p"), {"p": fast["player_id"]})
    await _answer(client, world, fast, {"option_key": "a"})
    await _close(session, world)
    board = (await _play(client, world, slow))["leaderboard"]
    assert [r["name"] for r in board] == ["Player 2", "Player 1"] and [r["rank"] for r in board] == [1, 2]
    assert (await _play(client, world, slow))["rank"] == 2 and (await _play(client, world, fast))["rank"] == 1
    winners = (await client.get(_u(world, f"/quizzes/{quiz.id}/winners"), headers=world["hv"])).json()
    assert [w["rank"] for w in winners] == [1, 2] and winners[0]["name"] == "Player 2" and winners[0]["correct"] == 1


async def test_the_board_only_every_nth_question_or_at_the_end(client, session, world):
    await _settings(client, world, {"flow": {"board": "every_n", "board_every": 2}})
    for _ in range(3):
        await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    shown = []
    for _ in range(3):
        await _next(client, world)
        await _answer(client, world, p, {"option_key": "a"})
        await _close(session, world)
        body = await _play(client, world, p)
        shown.append((body["board_now"], bool(body["leaderboard"]), body["rank"]))
    assert shown == [(False, False, 1), (True, True, 1), (True, True, 1)]  # own rank still shown (the theme's default)
    await _settings(client, world, {"flow": {"board": "end"}})
    assert (await _play(client, world, p))["leaderboard"] == []
    await _next(client, world)  # past the last: ended
    assert (await _play(client, world, p))["leaderboard"]


async def test_the_host_reveals_pauses_resumes_skips_and_reopens(client, session, world):
    await _settings(client, world, {"scoring": {"speed": "off"}, "flow": {"reveal": "host"}})
    for _ in range(3):
        await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    assert (await client.post(_u(world, "/quiz/host/reveal"), headers=world["hv"])).json()["error_code"] == "QUIZ_NOT_CLOSED"
    paused = (await client.post(_u(world, "/quiz/host/pause"), headers=world["hv"])).json()
    assert paused["phase"] == "paused" and paused["paused"] is True
    r = await _answer(client, world, p, {"option_key": "a"})
    assert r.status_code == 409 and r.json()["error_code"] == "QUIZ_PAUSED"
    assert (await client.post(_u(world, "/quiz/host/pause"), headers=world["hv"])).status_code == 409
    resumed = (await client.post(_u(world, "/quiz/host/resume"), headers=world["hv"])).json()
    assert resumed["phase"] == "question" and resumed["question"]["time_left"] >= 18
    await _answer(client, world, p, {"option_key": "a"})
    await _close(session, world)
    closed = await _play(client, world, p)
    assert closed["phase"] == "closed" and closed["question"]["correct_key"] is None and closed["score"] == 0
    revealed = (await client.post(_u(world, "/quiz/host/reveal"), headers=world["hv"])).json()
    assert revealed["phase"] == "revealed"
    assert (await _play(client, world, p))["score"] == 1000
    assert (await client.post(_u(world, "/quiz/host/reveal"), headers=world["hv"])).json()["error_code"] == "QUIZ_ALREADY_REVEALED"
    # Question 2, answered then voided: the points go back and the round moves on.
    await _next(client, world)
    await _answer(client, world, p, {"option_key": "a"})
    skipped = (await client.post(_u(world, "/quiz/host/skip"), headers=world["hv"])).json()
    assert skipped["current_index"] == 2
    assert (await session.scalar(select(LiveQuizPlayers.score).where(LiveQuizPlayers.id == p["player_id"]))) == 1000
    # Reopen question 2: a fresh clock, and it can be answered again.
    reopened = (await client.post(_u(world, "/quiz/host/reopen"), json={"index": 1}, headers=world["hv"])).json()
    assert reopened["current_index"] == 1 and reopened["question"]["time_left"] >= 18
    assert (await _answer(client, world, p, {"option_key": "a"})).status_code == 200


async def test_an_auto_round_moves_on_by_itself(client, session, world):
    await _settings(client, world, {"flow": {"advance": "auto", "auto_seconds": 3}})
    for _ in range(2):
        await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    body = await _play(client, world, p)
    assert body["next_at"] is not None and body["current_index"] == 0
    await _close(session, world, seconds=40)
    moved = await _play(client, world, p)
    assert moved["current_index"] == 1 and moved["phase"] == "question"
    await _close(session, world, seconds=40)
    assert (await _play(client, world, p))["status"] == "ended"


# ─── Players and teams ───────────────────────────────────────────────────


async def test_teams_picked_or_assigned_and_their_board(client, session, world):
    teams = [{"key": "red", "name": "Red Rockets", "color": "#E11D48"}, {"key": "blue", "name": "Blue Jays", "color": "#2563EB"}]
    await _settings(client, world, {"teams": {"mode": "pick", "score": "average", "list": teams}, "scoring": {"speed": "off"}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    r1 = await _join(client, code, 1, team="red")
    assert r1["team"] == {"key": "red", "name": "Red Rockets", "color": "#E11D48"}
    old = await _join(client, code, 2, v2=False)  # sends no team: assigned, and is not told (today's body)
    assert "team" not in old
    assert await session.scalar(select(LiveQuizPlayers.team).where(LiveQuizPlayers.id == old["player_id"])) == "blue"
    r3 = await _join(client, code, 3, team="red")
    bad = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "X", "player_key": "browser-99999999", "team": "green"})
    assert bad.status_code == 422 and bad.json()["error_code"] == "QUIZ_TEAM_UNKNOWN"
    await _next(client, world)
    await _answer(client, world, r1, {"option_key": "a"})
    await _answer(client, world, {"h": {"X-Live-Player": old["player_key"]}}, {"option_key": "a"})
    await _answer(client, world, r3, {"option_key": "b"})
    await _close(session, world)
    body = await _play(client, world, r1)
    assert body["team"]["key"] == "red" and [(t["key"], t["score"], t["players"]) for t in body["teams"]] == [("blue", 1000, 1), ("red", 500, 2)]
    assert all("team" in row for row in body["leaderboard"])
    host = (await client.get(_u(world, "/quiz/host"), headers=world["hv"])).json()
    assert host["teams"][0]["key"] == "blue"


async def test_player_rules(client, session, world):
    await _settings(client, world, {"players": {"names": "registered", "one_per_ticket": True, "late_join": False, "filter_names": True}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    anon = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Ghost", "player_key": "browser-00000009"})
    assert anon.status_code == 403 and anon.json()["error_code"] == "QUIZ_REGISTERED_ONLY"
    signed = await _join(client, code, 1, token=world["tokens"][0])
    assert signed["name"] == "Registered 0"  # the registration's name, not the one typed
    await _settings(client, world, {"players": {"names": "nickname"}})
    rude = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "sh1t head", "player_key": "browser-00000008"})
    assert rude.status_code == 422 and rude.json()["error_code"] == "QUIZ_NAME_REFUSED"
    # One player a ticket: the same registration from another client key.
    session.add(LiveQuizPlayers(quiz_id=signed["quiz_id"], name="Twin", player_key="twin-key-xx", registration_id=world["regs"][1].id, score=0))
    await session.commit()
    twin = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Twin 2", "player_key": "browser-00000007"}, headers=world["tokens"][1])
    assert twin.status_code == 409 and twin.json()["error_code"] == "QUIZ_TICKET_IN_USE"
    await _next(client, world)
    late = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Late", "player_key": "browser-00000006"})
    assert late.status_code == 409 and late.json()["error_code"] == "QUIZ_LATE"
    again = await _join(client, code, 1, token=world["tokens"][0])  # already in: always back in
    assert again["player_id"] == signed["player_id"]


async def test_an_anonymous_screen_shows_tags(client, session, world):
    await _settings(client, world, {"players": {"anonymous_screen": True}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    await _answer(client, world, p, {"option_key": "a"})
    await _close(session, world)
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["quiz"]["leaderboard"][0]["name"] == f"Player {p['tag']}"
    assert (await client.get(_u(world, "/quiz/host"), headers=world["hv"])).json()["leaderboard"][0]["name"] == "Player 1"
    assert (await _play(client, world, p))["tag"] == p["tag"]


# ─── Rounds ──────────────────────────────────────────────────────────────


async def test_rounds_one_live_at_a_time_and_players_follow(client, session, world):
    first = (await client.get(_u(world, "/quiz"), headers=world["hv"])).json()
    await _add(client, world, {"prompt": "Round one?", "options": _opts(2), "correct_key": "a"})
    second = (await client.post(_u(world, "/quizzes"), json={"title": "Round 2", "settings": {"scoring": {"speed": "off"}}}, headers=world["hv"])).json()
    assert second["sort_order"] == 1 and second["settings"]["scoring"]["speed"] == "off" and second["round"] == {"index": 1, "count": 2}
    await _add(client, world, {"prompt": "Round two?", "options": _opts(2), "correct_key": "a"}, path=f"/quizzes/{second['id']}/questions")
    rounds = (await client.get(_u(world, "/quizzes"), headers=world["hv"])).json()
    assert [r["title"] for r in rounds] == [first["title"], "Round 2"] and [r["questions"] for r in rounds] == [1, 1]
    code1 = (await _start(client, world))["join_code"]
    p = await _join(client, code1, 1, token=world["tokens"][0])
    other = await client.post(_u(world, f"/quizzes/{second['id']}/host/start"), json={"notify": False}, headers=world["hv"])
    assert other.status_code == 409 and other.json()["error_code"] == "QUIZ_OTHER_LIVE" and other.json()["errors"]["quiz_id"] == first["id"]
    assert (await client.delete(_u(world, f"/quizzes/{first['id']}"), headers=world["hv"])).status_code == 409
    await _next(client, world)
    await _answer(client, world, p, {"option_key": "a"})
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    code2 = (await _start(client, world, path=f"/quizzes/{second['id']}/host/start"))["join_code"]
    # The console's /quiz is now the live round.
    assert (await client.get(_u(world, "/quiz"), headers=world["hv"])).json()["id"] == second["id"]
    gone = await client.get(_u(world, "/quiz/play"), headers=p["h"])
    assert gone.status_code == 401 and gone.json()["error_code"] == "QUIZ_NEW_ROUND" and gone.json()["errors"]["join_code"] == code2
    p2 = await _join(client, code2, 1, token=world["tokens"][0])
    await _next(client, world, path=f"/quizzes/{second['id']}/host/next")
    await _answer(client, world, p2, {"option_key": "a"})
    await client.post(_u(world, f"/quizzes/{second['id']}/host/end"), headers=world["hv"])
    overall = (await client.get(_u(world, "/leaderboard"), headers=world["hv"])).json()
    assert len(overall) == 1 and overall[0]["rounds"] == 2 and overall[0]["score"] > 1000 and overall[0]["registration"]["id"] == str(world["regs"][0].id)
    host = (await client.get(_u(world, f"/quizzes/{second['id']}/host"), headers=world["hv"])).json()
    assert host["overall"][0]["rounds"] == 2
    reordered = (await client.put(_u(world, "/quizzes/order"), json={"ids": [second["id"], first["id"]]}, headers=world["hv"])).json()
    assert [r["id"] for r in reordered] == [second["id"], first["id"]]
    assert (await client.put(_u(world, "/quizzes/order"), json={"ids": [second["id"]]}, headers=world["hv"])).status_code == 422


async def test_one_live_round_is_held_by_the_database(session, world):
    quiz = LiveQuizzes(event_id=world["eid"], title="A", status=service.QuizStatus.live, join_code="111111")
    session.add(quiz)
    await session.commit()
    session.add(LiveQuizzes(event_id=world["eid"], title="B", status=service.QuizStatus.live, join_code="222222", sort_order=1))
    with pytest.raises(Exception):
        await session.commit()
    await session.rollback()


# ─── Winners ─────────────────────────────────────────────────────────────


async def test_winners_export_and_marking_on_registrations(client, session, world):
    await _settings(client, world, {"scoring": {"speed": "off"}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    started = await _start(client, world)
    quiz_id = started["quiz_id"]
    players = [await _join(client, started["join_code"], i, token=world["tokens"][i]) for i in range(2)]
    guest = await _join(client, started["join_code"], 7)
    await _next(client, world)
    await _answer(client, world, guest, {"option_key": "a"})
    await _answer(client, world, players[0], {"option_key": "a"})
    await _answer(client, world, players[1], {"option_key": "b"})
    assert (await client.post(_u(world, f"/quizzes/{quiz_id}/winners/mark"), json={"top": 3}, headers=world["hv"])).json()["error_code"] == "QUIZ_NOT_ENDED"
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    csv_r = await client.get(_u(world, f"/quizzes/{quiz_id}/winners/export"), params={"top": 3}, headers=world["hv"])
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(csv_r.text)))
    assert rows[0] == service.WINNER_HEADERS and len(rows) == 4
    # Equal scores share a rank, in join order (the round's ties: join).
    assert [r[:3] for r in rows[1:]] == [["1", "Player 0", "1000"], ["1", "Player 7", "1000"], ["3", "Player 1", "0"]]
    assert rows[1][6] == "Registered 0" and rows[1][7] == "quizzer-0@example.com" and rows[2][6] == ""  # the guest has no registration
    xlsx = await client.get(_u(world, f"/quizzes/{quiz_id}/winners/export"), params={"format": "xlsx"}, headers=world["hv"])
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    marked = (await client.post(_u(world, f"/quizzes/{quiz_id}/winners/mark"), json={"top": 3}, headers=world["hv"])).json()
    assert [m["position"] for m in marked["marked"]] == [1, 3] and marked["skipped"] == [{"rank": 1, "name": "Player 7", "reason": "no_registration"}]
    placed = dict(
        (
            await session.execute(
                select(EventRegistrationsLink.full_name, EventRegistrationsLink.position).where(EventRegistrationsLink.event_id == world["eid"])
            )
        ).all()
    )
    assert placed == {"Registered 0": 1, "Registered 1": 3, "Registered 2": None}
    listed = (await client.get(f"/api/v1/events/registration/{world['eid']}/winners", headers=world["h"])).json()
    assert {w["position"] for w in listed} == {1, 3}
    overall = await client.get(_u(world, "/leaderboard/export"), headers=world["hv"])
    assert list(csv.reader(io.StringIO(overall.text)))[0] == service.OVERALL_HEADERS


# ─── Who may ─────────────────────────────────────────────────────────────


async def test_roles_door_reads_editors_change_others_are_refused(client, session, world):
    door = {**await member_header(client, session, world["club"], "door"), **V2}
    editor = {**await member_header(client, session, world["club"], "editor"), **V2}
    assert (await client.get(_u(world, "/theme"), headers=door)).status_code == 200
    assert (await client.put(_u(world, "/theme"), json={"preset": "meridian_light"}, headers=door)).status_code == 403
    assert (await client.get(_u(world, "/quizzes"), headers=door)).status_code == 200
    assert (await client.post(_u(world, "/quizzes"), json={"title": "x"}, headers=door)).status_code == 403
    assert (await client.put(_u(world, "/screen/control"), json={"mode": "qa"}, headers=door)).status_code == 403
    quiz = (await client.get(_u(world, "/quiz"), headers=door)).json()
    assert (await client.get(_u(world, f"/quizzes/{quiz['id']}/winners/export"), headers=door)).status_code == 403
    assert (await client.get(_u(world, f"/quizzes/{quiz['id']}/winners"), headers=door)).status_code == 200
    assert (await client.put(_u(world, "/theme"), json={"preset": "meridian_light"}, headers=editor)).status_code == 200
    assert (await client.post(_u(world, "/quizzes"), json={"title": "Editor's"}, headers=editor)).status_code == 201
    assert (await client.get(_u(world, f"/quizzes/{quiz['id']}/winners/export"), headers=editor)).status_code == 200
    for path in ("/theme", "/quizzes", "/screen/control", "/media", "/leaderboard", f"/quizzes/{quiz['id']}"):
        assert (await client.get(_u(world, path), headers=world["other"])).status_code in (403, 404), path


# ─── The theme and its pictures ──────────────────────────────────────────


async def test_the_theme_saved_checked_and_told_to_the_room(client, session, world):
    assert (await client.get(_u(world, "/theme"))).status_code == 401
    got = (await client.get(_u(world, "/theme"), headers=world["hv"])).json()
    assert got["custom"] is False and got["theme"]["preset"] == "meridian_dark" and got["theme"]["text"]["on_background"] == "#FFFFFF"
    presets = (await client.get(_u(world, "/theme/presets"), headers=world["hv"])).json()
    assert [p["key"] for p in presets] == ["meridian_light", "meridian_dark", "high_contrast", "club"]
    check = (await client.post(_u(world, "/theme/check"), json={"preset": "meridian_dark", "colors": {"accent": "#1A1F1E"}}, headers=world["hv"])).json()
    assert check["ok"] is False and check["errors"][0]["code"] == "THEME_CONTRAST"
    assert (await client.get(_u(world, "/theme"), headers=world["hv"])).json()["custom"] is False  # nothing saved
    bad = await client.put(_u(world, "/theme"), json={"preset": "meridian_dark", "colors": {"accent": "#1A1F1E"}}, headers=world["hv"])
    assert bad.status_code == 422 and bad.json()["error_code"] == "THEME_CONTRAST" and bad.json()["errors"]["field"] == "colors.accent"
    assert (await client.put(_u(world, "/theme"), json={"preset": "meridian_dark", "timer": "sand"}, headers=world["hv"])).json()[
        "error_code"
    ] == "THEME_INVALID"
    assert (await client.put(_u(world, "/theme"), json={"preset": "meridian_dark", "logo": "6f1c2a9e00004000800000000000000a"}, headers=world["hv"])).json()[
        "error_code"
    ] == "THEME_MEDIA"
    before = (await client.get(_u(world, "/theme/public"))).json()
    saved = (
        await client.put(_u(world, "/theme"), json={"preset": "high_contrast", "timer": "ring", "font": {"display": "bebas_neue"}}, headers=world["hv"])
    ).json()
    assert saved["custom"] is True and saved["theme"]["timer"] == "ring" and saved["theme"]["colors"]["background"] == "#000000"
    public = await client.get(_u(world, "/theme/public"))
    assert public.status_code == 200 and public.json()["version"] == saved["version"] != before["version"]
    assert (await client.get(_u(world), headers=V2)).json()["theme"]["version"] == saved["version"]
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["theme_custom"] is True and screen["theme"]["font"]["display"] == "bebas_neue"
    cleared = (await client.delete(_u(world, "/theme"), headers=world["hv"])).json()
    assert cleared["custom"] is False


async def test_pictures_uploaded_used_and_deleted(client, session, world, fake_s3):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1200, 800), (200, 30, 30)).save(buf, "PNG")
    up = await client.post(_u(world, "/media"), files={"file": ("logo.png", buf.getvalue(), "image/png")}, data={"kind": "logo"}, headers=world["hv"])
    assert up.status_code == 201, up.text
    media = up.json()
    assert media["kind"] == "logo" and media["width"] == 1200 and media["large"]
    assert any("events/live/" in k for k in fake_s3)
    assert (await client.post(_u(world, "/media"), files={"file": ("x.txt", b"hello", "text/plain")}, headers=world["hv"])).json()[
        "error_code"
    ] == "LIVE_MEDIA_TYPE"
    saved = (await client.put(_u(world, "/theme"), json={"preset": "meridian_dark", "logo": media["id"]}, headers=world["hv"])).json()
    assert saved["theme"]["logo"]["id"] == media["id"]
    assert [m["id"] for m in (await client.get(_u(world, "/media"), headers=world["hv"])).json()] == [media["id"]]
    assert (await client.delete(_u(world, f"/media/{media['id']}"), headers=world["hv"])).status_code == 200
    after = (await client.get(_u(world, "/theme"), headers=world["hv"])).json()
    assert after["theme"]["logo"] is None and after["version"] != saved["version"]


# ─── Polls ───────────────────────────────────────────────────────────────


async def _poll(client, world, body):
    r = await client.post(_u(world, "/polls"), json=body, headers=world["hv"])
    assert r.status_code == 201, r.text
    poll = r.json()
    await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "live", "notify": False}, headers=world["hv"])
    return poll


async def _respond(client, world, poll, i, body, v2=True):
    return await client.post(_u(world, f"/polls/{poll['id']}/respond"), json=body, headers={**_key(i), **(V2 if v2 else {})})


async def test_multiple_choice_counts_each_pick_and_an_old_client_taps_one(client, world):
    poll = await _poll(client, world, {"kind": "choice", "prompt": "Which drinks?", "options": ["Tea", "Coffee", "Juice"], "settings": {"max_choices": 2}})
    r = await _respond(client, world, poll, 1, {"values": ["Juice", "Tea"]})
    assert r.status_code == 200 and r.json()["my_response"] == "0,2" and r.json()["my_values"] == ["Tea", "Juice"]
    assert (await _respond(client, world, poll, 2, {"values": ["Tea", "Coffee", "Juice"]})).status_code == 422
    old = await _respond(client, world, poll, 3, {"value": "Tea"}, v2=False)
    assert old.status_code == 200 and old.json()["my_response"] == "Tea" and set(old.json()) == POLL_KEYS
    feed = (await client.get(_u(world), headers=_key(1))).json()["poll"]
    assert set(feed) == POLL_KEYS and feed["kind"] == "choice" and feed["my_response"] == "Tea"
    assert [(c["label"], c["count"], c["pct"]) for c in feed["results"]["choices"]] == [("Tea", 2, 100), ("Coffee", 0, 0), ("Juice", 1, 50)]
    v2 = (await client.get(_u(world), headers={**_key(1), **V2})).json()["poll"]
    assert v2["settings"]["max_choices"] == 2 and v2["my_values"] == ["Tea", "Juice"]


async def test_kinds_old_clients_cannot_answer_say_so(client, world):
    for body in (
        {"kind": "rating", "prompt": "Rate the talk", "settings": {"scale": 10}},
        {"kind": "ranking", "prompt": "Rank these", "options": ["A", "B", "C"]},
        {"kind": "text", "prompt": "One word for tonight?"},
    ):
        poll = await _poll(client, world, body)
        feed = (await client.get(_u(world))).json()["poll"]
        assert set(feed) == POLL_KEYS and feed["kind"] == "choice" and feed["options"] == [] and "needs an update" in feed["prompt"]
        assert (await client.get(_u(world), headers=V2)).json()["poll"]["kind"] == body["kind"]
        await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "closed"}, headers=world["hv"])


async def test_rating_out_of_ten_and_a_ranking(client, world):
    rating = await _poll(client, world, {"kind": "rating", "prompt": "Rate it", "settings": {"scale": 10}})
    assert (await _respond(client, world, rating, 1, {"value": "9"})).status_code == 200
    assert (await _respond(client, world, rating, 2, {"value": "11"})).status_code == 422
    res = (await client.get(_u(world, "/polls"), headers=world["hv"])).json()[0]["results"]
    assert res["scale"] == 10 and len(res["rating"]["histogram"]) == 10 and res["rating"]["average"] == 9
    ranking = await _poll(client, world, {"kind": "ranking", "prompt": "Best talk?", "options": ["Rust", "Go", "Zig"]})
    await _respond(client, world, ranking, 1, {"order": ["Zig", "Rust", "Go"]})
    await _respond(client, world, ranking, 2, {"order": ["Zig", "Go", "Rust"]})
    assert (await _respond(client, world, ranking, 3, {"order": ["Zig", "Go"]})).status_code == 422
    out = (await client.get(_u(world), headers=V2)).json()["poll"]
    assert [r["label"] for r in out["results"]["ranking"]] == ["Zig", "Rust", "Go"] and out["results"]["ranking"][0]["first"] == 2


async def test_open_text_waits_for_the_host(client, world):
    poll = await _poll(client, world, {"kind": "text", "prompt": "What should we change?", "settings": {"anonymous": False}})
    r = await _respond(client, world, poll, 1, {"value": "More pizza", "name": "Sneha"})
    assert r.status_code == 200
    assert (await client.get(_u(world), headers=V2)).json()["poll"]["results"]["texts"] == []
    pending = (await client.get(_u(world, f"/polls/{poll['id']}/responses"), params={"status": "pending"}, headers=world["hv"])).json()
    assert [p["text"] for p in pending] == ["More pizza"] and pending[0]["name"] == "Sneha"
    console = (await client.get(_u(world, "/polls"), headers=world["hv"])).json()[0]["results"]
    assert console["pending"] == 1
    ok = await client.post(_u(world, f"/polls/{poll['id']}/responses/{pending[0]['id']}"), json={"action": "approve"}, headers=world["hv"])
    assert ok.json()["status"] == "approved"
    texts = (await client.get(_u(world), headers=V2)).json()["poll"]["results"]["texts"]
    assert [(t["text"], t["name"]) for t in texts] == [("More pizza", "Sneha")]
    # Changed, it goes back to the host.
    await _respond(client, world, poll, 1, {"value": "More pizza and chai"})
    assert (await client.get(_u(world), headers=V2)).json()["poll"]["results"]["texts"] == []


async def test_hidden_results_one_answer_only_and_a_timer(client, session, world):
    poll = await _poll(
        client, world, {"kind": "choice", "prompt": "Tea?", "options": ["Yes", "No"], "settings": {"results": "closed", "change_vote": False, "seconds": 30}}
    )
    assert (await _respond(client, world, poll, 1, {"value": "Yes"})).status_code == 200
    again = await _respond(client, world, poll, 1, {"value": "No"})
    assert again.status_code == 409 and again.json()["error_code"] == "POLL_ANSWERED"
    guest = (await client.get(_u(world), headers=V2)).json()["poll"]
    assert guest["results"] == {"responses": 1} and guest["results_hidden"] is True and guest["closes_at"]
    old = (await client.get(_u(world))).json()["poll"]
    assert [c["count"] for c in old["results"]["choices"]] == [0, 0]
    assert (await client.get(_u(world, "/polls"), headers=world["hv"])).json()[0]["results"]["choices"][0]["count"] == 1
    # Display settings change while it runs; its answers do not.
    assert (await client.patch(_u(world, f"/polls/{poll['id']}"), json={"settings": {"chart": "donut"}}, headers=world["hv"])).status_code == 200
    assert (await client.patch(_u(world, f"/polls/{poll['id']}"), json={"settings": {"max_choices": 2}}, headers=world["hv"])).status_code == 409
    await session.execute(text("UPDATE live_polls SET closes_at = now() - interval '1 second' WHERE id = :p"), {"p": poll["id"]})
    await session.commit()
    room.forget_room(world["eid"])
    late = await _respond(client, world, poll, 2, {"value": "No"})
    assert late.status_code == 409
    assert (await client.get(_u(world))).json()["poll"] is None  # the timer closed it
    closed = (await client.get(_u(world, "/polls"), headers=world["hv"])).json()[0]
    assert closed["status"] == "closed" and closed["results_hidden"] is False


# ─── The big screen ──────────────────────────────────────────────────────


async def test_the_screen_carries_the_host_club_and_partner_logos_until_turned_off(client, session, world, fake_s3):
    from PIL import Image

    from app.api.event_partners.models import EventPartners
    from app.api.events.models import Events
    from app.api.live import stream

    def png():
        out = io.BytesIO()
        Image.new("RGB", (200, 200), (10, 120, 200)).save(out, "PNG")
        return {"bytes": out.getvalue(), "filename": "logo.png"}

    world["club"].logo = png()
    for kind, name, pos, logo in (
        ("partner", "Second Partner", 1, png()),
        ("partner", "First Partner", 0, png()),
        ("sponsor", "Title Sponsor", 0, png()),
        ("partner", "No Logo", 2, None),
    ):
        session.add(EventPartners(event_id=world["eid"], kind=kind, name=name, position=pos, logo=logo, label="Title" if kind == "sponsor" else None))
    await session.commit()
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["club"]["name"] == "Quiz Society" and "clubs/logos/" in screen["club"]["logo_url"]
    assert [p["name"] for p in screen["partners"]] == ["Title Sponsor", "First Partner", "Second Partner"]
    assert screen["partners"][0]["kind"] == "sponsor" and screen["partners"][0]["label"] == "Title"
    # The room's pushed screen snapshot carries the same strip.
    event = await session.get(Events, world["eid"])
    pushed = await stream._club_body(session, service, event, "screen")
    assert pushed["club"] == screen["club"] and pushed["partners"] == screen["partners"]
    # Old consoles keep today's body.
    assert "club" not in (await client.get(_u(world, "/screen"), headers=world["h"])).json()
    # The organiser can turn the strip off.
    put = await client.put(_u(world, "/screen/control"), json={"logo_footer": False}, headers=world["hv"])
    assert put.status_code == 200 and put.json()["logo_footer"] is False
    off = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert off["club"] is None and off["partners"] == []
    assert (await client.put(_u(world, "/screen/control"), json={"logo_footer": "maybe"}, headers=world["hv"])).status_code == 422



async def test_a_footer_image_stands_in_for_the_logos_and_survives_control_changes(client, session, world, fake_s3):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (3000, 300), (10, 120, 200)).save(out, "PNG")
    up = await client.post(_u(world, "/screen/footer-image"), files={"image": ("strip.png", out.getvalue(), "image/png")}, headers=world["hv"])
    assert up.status_code == 200, up.text
    url = up.json()["footer_image_url"]
    assert "events/live/footers/" in url and up.json()["logo_footer"] is True
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["footer_image_url"] == url and "_footer_image" not in screen["control"] and screen["cohosts"] == []
    # Another control change keeps it.
    assert (await client.put(_u(world, "/screen/control"), json={"ticker": True}, headers=world["hv"])).json()["footer_image_url"] == url
    assert (await client.get(_u(world, "/screen/control"), headers=world["hv"])).json()["footer_image_url"] == url
    bad = await client.post(_u(world, "/screen/footer-image"), files={"image": ("x.txt", b"hi", "text/plain")}, headers=world["hv"])
    assert bad.status_code == 422
    gone = await client.delete(_u(world, "/screen/footer-image"), headers=world["hv"])
    assert gone.status_code == 200 and gone.json()["footer_image_url"] is None
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["footer_image_url"] is None


async def test_the_screens_controls_and_what_it_shows(client, session, world):
    control = (await client.get(_u(world, "/screen/control"), headers=world["hv"])).json()
    assert control == {"mode": "auto", "results": "hidden", "ticker": False, "idle_slides": [], "slide_seconds": 12, "wall_size": 6, "logo_footer": True, "footer_image_url": None}
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "qa"
    bad = await client.put(_u(world, "/screen/control"), json={"idle_slides": ["agenda", "agenda"]}, headers=world["hv"])
    assert bad.status_code == 422
    put = (await client.put(_u(world, "/screen/control"), json={"idle_slides": ["agenda", "sponsors", "join"], "ticker": True}, headers=world["hv"])).json()
    assert put["idle_slides"] == ["agenda", "sponsors", "join"] and put["mode"] == "auto"
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["shows"] == "idle" and screen["agenda"] == [] and screen["sponsors"] == []
    poll = await _poll(client, world, {"kind": "choice", "prompt": "Tea?", "options": ["Yes", "No"]})
    await _respond(client, world, poll, 1, {"value": "Yes"})
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "poll"
    # A poll's results show on the screen as they run, unless the poll says after closing.
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["poll"]["results"]["choices"][0]["count"] == 1
    later = await _poll(client, world, {"kind": "choice", "prompt": "Coffee?", "options": ["Yes", "No"], "settings": {"results": "closed"}})
    await _respond(client, world, later, 1, {"value": "Yes"})
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["poll"]["results"] == {"responses": 1}
    await client.put(_u(world, "/screen/control"), json={"mode": "qa"}, headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "qa"


# ─── Streams ─────────────────────────────────────────────────────────────


async def test_streams_send_each_client_its_own_version(client, session, world, monkeypatch):
    from tests.test_live_stream import SSE, streams  # noqa: F401
    from app.api.live import bus, stream
    from app.config import settings
    from contextlib import asynccontextmanager

    monkeypatch.setattr(settings, "LIVE_SSE", True)
    hub = stream.Hub()
    hub.manual = True
    monkeypatch.setattr(stream, "hub", hub)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(stream, "session_scope", scope)
    monkeypatch.setattr(stream, "RESYNC_SECONDS", 3600.0)
    monkeypatch.setattr(stream, "PACE", {})
    bus.reset_for_tests()
    try:
        await _add(client, world, {"kind": "multi", "prompt": "Which?", "options": _opts(3), "correct_keys": ["a"]})
        code = (await _start(client, world))["join_code"]
        p = await _join(client, code, 1)
        await _next(client, world)
        path = _u(world, "/stream")
        old = await SSE(path, {}, {"view": "quiz", "player": p["player_key"]}).open()
        new = await SSE(path, {}, {"view": "quiz", "player": p["player_key"], "v": "2"}).open()
        await hub.step()
        o = (await old.next_of("snapshot"))["json"]
        n = (await new.next_of("snapshot"))["json"]
        assert set(o) == PLAY_KEYS and o["question"]["options"] == [] and service.NEEDS_UPDATE in o["question"]["prompt"]
        assert n["question"]["kind"] == "multi" and len(n["question"]["options"]) == 3 and n["phase"] == "question"
        room_old = await SSE(path, _key(5), {"view": "room"}).open()
        room_new = await SSE(path, _key(5), {"view": "room", "v": "2"}).open()
        await hub.step()
        assert "theme" not in (await room_old.next_of("snapshot"))["json"]
        assert "theme" in (await room_new.next_of("snapshot"))["json"]
        host_old = await SSE(path, world["h"], {"view": "host"}).open()
        host_new = await SSE(path, world["h"], {"view": "host", "v": "2"}).open()
        await hub.step()
        assert "phase" not in (await host_old.next_of("snapshot"))["json"]
        assert (await host_new.next_of("snapshot"))["json"]["phase"] == "question"
        # The host pauses: both quiz streams are told at once.
        await client.post(_u(world, "/quiz/host/pause"), headers=world["hv"])
        await hub.step()
        assert (await new.next_of("snapshot"))["json"]["phase"] == "paused"
        for s in (old, new, room_old, room_new, host_old, host_new):
            await s.close()
    finally:
        await hub.close()
        bus.reset_for_tests()


async def test_the_public_theme_is_conditional_and_the_version_rides_the_query(client, session, world):
    first = await client.get(_u(world, "/theme/public"))
    assert first.status_code == 200 and first.headers["cache-control"] == "public, max-age=15" and first.headers.get("etag")
    again = await client.get(_u(world, "/theme/public"), headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert (await client.get(f"/api/v1/events/{world['eid'] + 9999}/live/theme/public")).status_code == 404
    assert "theme" in (await client.get(_u(world), params={"v": "2"})).json()
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1, v2=False)
    by_query = await client.get(_u(world, "/quiz/play"), params={"v": "2"}, headers={"X-Live-Player": p["player_key"]})
    assert by_query.json()["phase"] == "lobby" and by_query.json()["round"] == {"index": 0, "count": 1}


async def test_the_screen_shows_the_agenda_and_the_sponsors_for_idle_slides(client, session, world):
    from app.api.agenda.models import AgendaItems
    from app.api.event_partners.models import EventPartners

    now = datetime.now(timezone.utc)
    session.add(AgendaItems(event_id=world["eid"], starts_at=now - timedelta(minutes=10), ends_at=now + timedelta(minutes=20), title="Keynote", sort_order=0))
    session.add(AgendaItems(event_id=world["eid"], starts_at=now + timedelta(minutes=30), ends_at=now + timedelta(minutes=60), title="Workshops", sort_order=1))
    session.add(AgendaItems(event_id=world["eid"], starts_at=now - timedelta(hours=2), ends_at=now - timedelta(hours=1), title="Doors", sort_order=2))
    session.add(EventPartners(event_id=world["eid"], kind="sponsor", name="Acme", position=0))
    session.add(EventPartners(event_id=world["eid"], kind="partner", name="Not a sponsor", position=1))
    await session.commit()
    await client.put(_u(world, "/screen/control"), json={"idle_slides": ["agenda", "sponsors"]}, headers=world["hv"])
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["shows"] == "idle"
    assert [(a["title"], a["now"]) for a in screen["agenda"]] == [("Keynote", True), ("Workshops", False)]
    assert [s["name"] for s in screen["sponsors"]] == ["Acme"]
    # The theme's sponsor strip asks for them too, with no idle slides.
    await client.put(_u(world, "/screen/control"), json={"idle_slides": []}, headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["sponsors"] == []
    await client.put(_u(world, "/theme"), json={"preset": "meridian_dark", "screen": {"sponsors": True}}, headers=world["hv"])
    assert [s["name"] for s in (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["sponsors"]] == ["Acme"]


async def test_a_guest_sees_the_teams_before_joining(client, world):
    teams = [{"key": "red", "name": "Red Rockets", "color": "#E11D48"}, {"key": "blue", "name": "Blue Jays", "color": "#2563EB"}]
    await _settings(client, world, {"teams": {"mode": "pick", "list": teams}, "players": {"late_join": False}})
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    got = (await client.get("/api/v1/live/quiz/lookup", params={"code": code})).json()
    assert got["teams"]["mode"] == "pick" and [t["key"] for t in got["teams"]["list"]] == ["red", "blue"]
    assert got["started"] is False and got["players"] == {"names": "nickname", "late_join": False} and got["event_id"] == world["eid"]
    assert (await client.get("/api/v1/live/quiz/lookup", params={"code": "000000"})).status_code == 404


async def test_the_play_state_carries_ties_anonymity_the_overall_board_and_a_poll_questions_counts(client, session, world):
    await _settings(client, world, {"ties": "time", "players": {"anonymous_screen": True}})
    await _add(client, world, {"kind": "poll", "prompt": "Tea or coffee?", "options": _opts(2)})
    code = (await _start(client, world))["join_code"]
    a, b = await _join(client, code, 1), await _join(client, code, 2)
    await _next(client, world)
    await _answer(client, world, a, {"option_key": "a"})
    await _answer(client, world, b, {"option_key": "a"})
    body = await _play(client, world, a)
    assert body["ties"] == "time" and body["anonymous_screen"] is True and body["question"]["counts"] is None  # not before the reveal
    await _close(session, world)
    counts = (await _play(client, world, a))["question"]["counts"]
    assert [(c["key"], c["count"]) for c in counts] == [("a", 2), ("b", 0)]
    old = await _play(client, world, a, v2=False)
    assert "counts" not in old["question"] and "ties" not in old
    # A second round, played and ended: the board across both on the phone.
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    second = (await client.post(_u(world, "/quizzes"), json={"title": "Round 2"}, headers=world["hv"])).json()
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"}, path=f"/quizzes/{second['id']}/questions")
    code2 = (await _start(client, world, path=f"/quizzes/{second['id']}/host/start"))["join_code"]
    a2 = await _join(client, code2, 1)
    await _next(client, world, path=f"/quizzes/{second['id']}/host/next")
    await _answer(client, world, a2, {"option_key": "a"})
    assert (await _play(client, world, a2))["overall"] is None
    await client.post(_u(world, f"/quizzes/{second['id']}/host/end"), headers=world["hv"])
    ended = await _play(client, world, a2)
    assert ended["phase"] == "ended" and ended["overall"][0]["name"] == "Player 1" and ended["overall"][0]["rounds"] == 2


async def test_the_rooms_quiz_card_follows_the_live_round(client, world):
    await _add(client, world, {"prompt": "Round one?", "options": _opts(2), "correct_key": "a"})
    first = (await client.get(_u(world, "/quiz"), headers=world["hv"])).json()
    second = (await client.post(_u(world, "/quizzes"), json={"title": "The Final"}, headers=world["hv"])).json()
    await _add(client, world, {"prompt": "Round two?", "options": _opts(2), "correct_key": "a"}, path=f"/quizzes/{second['id']}/questions")
    assert (await client.get(_u(world))).json()["quiz"] == {"live": False, "title": first["title"]}
    await _start(client, world, path=f"/quizzes/{second['id']}/host/start")
    assert (await client.get(_u(world))).json()["quiz"] == {"live": True, "title": "The Final"}
    assert (await client.get(_u(world, "/banner"))).json()["quiz"] == {"live": True, "title": "The Final"}
    await client.post(_u(world, f"/quizzes/{second['id']}/host/end"), headers=world["hv"])
    # Ended, the card shows the round that ended last; then round one, live again.
    assert (await client.get(_u(world))).json()["quiz"] == {"live": False, "title": "The Final"}
    await _start(client, world, path=f"/quizzes/{first['id']}/host/start")
    assert (await client.get(_u(world))).json()["quiz"] == {"live": True, "title": first["title"]}


async def test_the_poll_that_closed_last_stays_on_this_releases_feed_read_only(client, session, world):
    poll = await _poll(client, world, {"kind": "choice", "prompt": "Tea?", "options": ["Yes", "No"], "settings": {"results": "closed"}})
    await _respond(client, world, poll, 1, {"value": "Yes"})
    assert (await client.get(_u(world), headers={**_key(1), **V2})).json()["poll"]["results"] == {"responses": 1}  # hidden while it runs
    await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "closed"}, headers=world["hv"])
    assert (await client.get(_u(world), headers=_key(1))).json()["poll"] is None  # today's feed: gone, as always
    shown = (await client.get(_u(world), headers={**_key(1), **V2})).json()["poll"]
    assert shown["id"] == poll["id"] and shown["status"] == "closed" and shown["results_hidden"] is False
    assert [c["count"] for c in shown["results"]["choices"]] == [1, 0] and shown["my_response"] == "Yes" and shown["my_values"] == ["Yes"]
    assert (await _respond(client, world, poll, 2, {"value": "No"})).status_code == 409  # never answerable
    # The next launch takes its place.
    other = await _poll(client, world, {"kind": "words", "prompt": "One word?"})
    assert (await client.get(_u(world), headers=V2)).json()["poll"]["id"] == other["id"]
    await client.post(_u(world, f"/polls/{other['id']}/status"), json={"status": "closed"}, headers=world["hv"])
    assert (await client.get(_u(world), headers=V2)).json()["poll"]["id"] == other["id"]
    # Half an hour after it closed, it goes.
    await session.execute(text("UPDATE live_polls SET closed_at = now() - interval '31 minutes' WHERE event_id = :e"), {"e": world["eid"]})
    await session.commit()
    room.forget_room(world["eid"])
    assert (await client.get(_u(world), headers=V2)).json()["poll"] is None
    # A poll hidden from guests stays hidden once closed.
    hidden = await _poll(client, world, {"kind": "choice", "prompt": "Secret?", "options": ["A", "B"], "settings": {"hide_results": True}})
    await _respond(client, world, hidden, 1, {"value": "A"})
    await client.post(_u(world, f"/polls/{hidden['id']}/status"), json={"status": "closed"}, headers=world["hv"])
    assert (await client.get(_u(world), headers=V2)).json()["poll"]["results"] == {"responses": 1}



async def test_the_screen_shows_a_questions_tally_from_time_up_unless_set_live(client, session, world):
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    await _answer(client, world, p, {"option_key": "a"})
    running = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["quiz"]["question"]
    assert running["answered"] == 1 and [c["count"] for c in running["counts"]] == [0, 0]  # the default: as the screen always was
    old = (await client.get(_u(world, "/screen"), headers=world["h"])).json()["quiz"]["question"]
    assert [c["count"] for c in old["counts"]] == [1, 0]  # a console from before: today's body
    await client.put(_u(world, "/screen/control"), json={"results": "live"}, headers=world["hv"])
    assert [c["count"] for c in (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["quiz"]["question"]["counts"]] == [1, 0]
    await client.put(_u(world, "/screen/control"), json={"results": "hidden"}, headers=world["hv"])
    quiz = await service.quiz_of(session, _event(world))
    quiz.question_opened_at = quiz.question_opened_at - timedelta(seconds=19.5)  # the clock at 0, in the grace
    await session.commit()
    room.quizzes.forget(("quiz", world["eid"]))
    at_zero = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["quiz"]["question"]
    assert at_zero["time_left"] == 0 and [c["count"] for c in at_zero["counts"]] == [1, 0] and at_zero["correct_key"] is None


async def test_an_ended_round_stays_on_the_screen_until_something_else_goes_up(client, session, world):
    await _add(client, world, {"prompt": "Pick A", "options": _opts(2), "correct_key": "a"})
    code = (await _start(client, world))["join_code"]
    p = await _join(client, code, 1)
    await _next(client, world)
    await _answer(client, world, p, {"option_key": "a"})
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    screen = (await client.get(_u(world, "/screen"), headers=world["hv"])).json()
    assert screen["shows"] == "quiz" and screen["quiz"]["status"] == "ended" and screen["quiz"]["leaderboard"][0]["name"] == "Player 1"
    assert (await client.get(_u(world, "/screen"), headers=world["h"])).json()["quiz"] is None  # a console from before: as today
    # A poll launched now takes its place.
    await _poll(client, world, {"kind": "choice", "prompt": "Tea?", "options": ["Yes", "No"]})
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "poll"
    # A question pinned after the end, or the controls changed, does too.
    await client.post(_u(world, "/quiz/host/start"), json={"notify": False, "restart": True}, headers=world["hv"])
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    for poll in (await client.get(_u(world, "/polls"), headers=world["hv"])).json():
        await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "closed"}, headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "quiz"
    await client.put(_u(world, "/screen/control"), json={"idle_slides": ["join"]}, headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "idle"
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False, "notify": False}, headers=world["hv"])
    asked = (await client.post(_u(world, "/questions"), json={"text": "When is lunch?"}, headers=_key(4))).json()
    await client.post(_u(world, "/quiz/host/start"), json={"notify": False}, headers=world["hv"])
    await client.post(_u(world, "/quiz/host/end"), headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "quiz"
    await client.post(_u(world, f"/questions/{asked['id']}"), json={"action": "pin", "notify": False}, headers=world["hv"])
    assert (await client.get(_u(world, "/screen"), headers=world["hv"])).json()["shows"] == "qa"
