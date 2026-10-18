"""The live room at the size of a hall: who it lets in and how often, what it
lets the public see, the second-long snapshots every phone reads, double
taps, and a survey that goes to each guest once."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.discover import cache
from app.api.live import service
from app.api.live.models import FeedbackSends, FeedbackSurveys, LiveQuizPlayers
from app.api.orgs.models import Organizations, OrgTypes
from app.core import after_commit, jobs
from tests.conftest import auth_header
from tests.racing import beaten_to, statements

START = datetime.now(timezone.utc) + timedelta(hours=1)
VENUE = {"x-forwarded-for": "49.36.113.7"}  # one hall, one address


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Hall Society", email="hall@example.com")
    event = await make_event(club=club, created_by=owner, name="Hall Night", event_datetime=START, duration=3.0, location_name="Main Hall")
    guests = []
    for i in range(3):
        u = await make_user(email=f"hall-guest-{i}@example.com")
        guests.append(await make_registration(event=event, user=u, full_name=f"Guest {i}"))
    await session.commit()
    return {"event": event, "club": club, "owner": owner, "guests": guests, "h": auth_header(await mint_token(owner))}


def _u(world, path=""):
    return f"/api/v1/events/{world['event'].id}/live{path}"


def _key(i: int) -> dict:
    return {**VENUE, "X-Live-Key": f"browser-{i:08d}"}


async def _quiz(client, world, questions=1) -> str:
    h = world["h"]
    for n in range(questions):
        q = {"prompt": f"Question {n}?", "options": [{"key": "a", "label": "Yes"}, {"key": "b", "label": "No"}], "correct_key": "a", "seconds": 30}
        assert (await client.post(_u(world, "/quiz/questions"), json=q, headers=h)).status_code == 201
    started = await client.post(_u(world, "/quiz/host/start"), headers=h)
    assert started.status_code == 200, started.text
    return started.json()["join_code"]


# --- rate limits: per person, with a ceiling per address ------------------------------


async def test_a_hall_on_one_address_is_many_people_not_one(client, world, monkeypatch):
    code = await _quiz(client, world)
    # Forty phones on the venue's Wi-Fi join in the same minute (the limit
    # was 30 a minute per address: the 31st was refused).
    for i in range(40):
        r = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": f"Player {i}"}, headers=_key(i))
        assert r.status_code == 201, (i, r.text)
    # Twenty-five ask a question (the limit was 20 an hour per address).
    for i in range(25):
        r = await client.post(_u(world, "/questions"), json={"text": f"Question number {i} from the floor"}, headers=_key(i))
        assert r.status_code == 201, (i, r.text)
    # One phone is still one person: its eleventh join in a minute is refused.
    for _ in range(9):
        assert (await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Again"}, headers=_key(0))).status_code == 201
    stuck = await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Again"}, headers=_key(0))
    assert stuck.status_code == 429 and stuck.json()["error_code"] == "RATE_LIMITED" and stuck.json()["errors"]["retry_after"] >= 1
    # And one address has a ceiling, whatever keys it makes up.
    from app.api.live import limits

    monkeypatch.setitem(limits.RULES, "vote", limits.Rule("120/minute", "5/minute"))
    asked = (await client.post(_u(world, "/questions"), json={"text": "Ceiling check, please"}, headers=_key(900))).json()
    for i in range(5):
        assert (await client.post(_u(world, f"/questions/{asked['id']}/vote"), headers={"x-forwarded-for": "106.51.100.9", "X-Live-Key": f"flood-{i:06d}"})).status_code == 200
    flood = await client.post(_u(world, f"/questions/{asked['id']}/vote"), headers={"x-forwarded-for": "106.51.100.9", "X-Live-Key": "flood-999999"})
    assert flood.status_code == 429
    # Another address is not touched by it.
    assert (await client.post(_u(world, f"/questions/{asked['id']}/vote"), headers=_key(901))).status_code == 200


# --- what the public may see ----------------------------------------------------------


async def test_the_room_is_closed_for_what_the_public_cannot_see(client, session, world, make_club, make_event):
    from app.api.closeout.models import EventStatus

    code = await _quiz(client, world)
    player = (await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Before"}, headers=_key(1))).json()
    play = {"X-Live-Player": player["player_key"]}
    assert (await client.get(_u(world))).status_code == 200
    assert (await client.get(_u(world, "/quiz/play"), headers=play)).status_code == 200

    async def shut():
        assert (await client.get(_u(world))).status_code == 404
        assert (await client.get(_u(world, "/quiz/play"), headers=play)).status_code == 404
        assert (await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Late"}, headers=_key(2))).status_code == 404
        assert (await client.post(_u(world, "/questions"), json={"text": "Anyone there?"}, headers=_key(3))).status_code == 404

    # A draft (the room served it by id before).
    world["event"].lifecycle = EventStatus.draft
    await session.commit()
    await shut()
    world["event"].lifecycle = EventStatus.published
    await session.commit()
    assert (await client.get(_u(world))).status_code == 200

    # A club that is not verified.
    world["club"].is_verified = False
    await session.commit()
    await shut()
    world["club"].is_verified = True
    await session.commit()
    assert (await client.get(_u(world))).status_code == 200

    # A club under a blocked organisation.
    org = Organizations(name="Blocked College", type=list(OrgTypes)[0])
    session.add(org)
    await session.flush()
    world["club"].org_id = org.id
    await session.commit()
    assert (await client.get(_u(world))).status_code == 200
    org.is_blocked = True
    await session.commit()
    await shut()
    org.is_blocked = False
    await session.commit()
    assert (await client.get(_u(world))).status_code == 200


# --- double taps ------------------------------------------------------------------------


async def test_a_double_tap_is_answered_not_a_500(client, session, world):
    asked = (await client.post(_u(world, "/questions"), json={"text": "Two taps at once?"}, headers=_key(1))).json()
    qid = asked["id"]
    await client.post(_u(world, f"/questions/{qid}"), json={"action": "approve"}, headers=world["h"])
    # The other tap's vote lands between this one's check and its insert.
    with beaten_to(session, "live_question_votes", "INSERT INTO live_question_votes (id, question_id, voter_key) VALUES (gen_random_uuid(), $1::uuid, $2)", qid, "browser-00000002") as fired:
        r = await client.post(_u(world, f"/questions/{qid}/vote"), headers=_key(2))
    assert fired and r.status_code == 200, r.text
    # The toggle saw the vote that was there and took it back: no vote, and the count agrees with the rows.
    votes, rows = (await session.execute(text(
        "SELECT q.votes, (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id) FROM live_questions q WHERE q.id = :q"
    ), {"q": qid})).one()
    assert r.json()["voted"] is False and rows == 0 and votes == 0

    # A quiz answer, twice at once: one counts, the other is told so.
    code = await _quiz(client, world)
    player = (await client.post("/api/v1/live/quiz/join", json={"code": code, "name": "Quick"}, headers=_key(3))).json()
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    question_id = (await client.get(_u(world, "/quiz/host"), headers=world["h"])).json()["question"]["id"]
    with beaten_to(
        session, "live_quiz_answers",
        "INSERT INTO live_quiz_answers (id, question_id, player_id, option_key, correct, points, answered_at, created_at, updated_at) "
        "VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 'a', true, 900, now(), now(), now())",
        question_id, player["player_id"],
    ) as fired:
        r = await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers={"X-Live-Player": player["player_key"]})
    assert fired and r.status_code == 409, r.text

    # A poll answer, twice at once: the later one is the answer.
    poll = (await client.post(_u(world, "/polls"), json={"kind": "choice", "prompt": "Tea or coffee?", "options": ["Tea", "Coffee"]}, headers=world["h"])).json()
    await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "live"}, headers=world["h"])
    with beaten_to(session, "live_poll_responses", "INSERT INTO live_poll_responses (id, poll_id, voter_key, value) VALUES (gen_random_uuid(), $1::uuid, $2, 'Tea')", poll["id"], "browser-00000004") as fired:
        r = await client.post(_u(world, f"/polls/{poll['id']}/respond"), json={"value": "Coffee"}, headers=_key(4))
    assert fired and r.status_code == 200, r.text
    assert r.json()["my_response"] == "Coffee" and r.json()["results"]["responses"] == 1
    assert [c["count"] for c in r.json()["results"]["choices"]] == [0, 1]


# --- the snapshots ------------------------------------------------------------------------


@pytest.fixture
def snapshots():
    cache.reset(enabled=True)
    yield
    cache.reset(enabled=False)


async def test_the_room_is_read_from_a_snapshot_with_each_browsers_own_flags(client, session, world, snapshots):
    h = world["h"]
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=h)
    mine = (await client.post(_u(world, "/questions"), json={"text": "Mine, from browser one"}, headers=_key(1))).json()
    other = (await client.post(_u(world, "/questions"), json={"text": "Another, from browser two"}, headers=_key(2))).json()
    poll = (await client.post(_u(world, "/polls"), json={"kind": "choice", "prompt": "Tea or coffee?", "options": ["Tea", "Coffee"]}, headers=h)).json()
    await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "live"}, headers=h)
    await client.post(_u(world, f"/questions/{other['id']}/vote"), headers=_key(1))
    await client.post(_u(world, f"/polls/{poll['id']}/respond"), json={"value": "Tea"}, headers=_key(1))

    first = await client.get(_u(world))  # builds the room
    assert first.status_code == 200
    with statements(session) as seen:
        for _ in range(20):
            anon = await client.get(_u(world))
            assert anon.content == first.content
    assert seen == [], seen  # twenty phones, no statement: the snapshot answers

    one = (await client.get(_u(world), headers=_key(1))).json()  # reads what browser one did, once
    by = {q["id"]: q for q in one["questions"]}
    assert by[mine["id"]]["mine"] is True and by[mine["id"]]["voted"] is False
    assert by[other["id"]]["voted"] is True and by[other["id"]]["mine"] is False
    assert one["poll"]["my_response"] == "Tea"
    with statements(session) as seen:
        again = (await client.get(_u(world), headers=_key(1))).json()
        stranger = (await client.get(_u(world), headers=_key(99))).json()
    assert again == one
    assert all(not q["voted"] and not q["mine"] for q in stranger["questions"]) and stranger["poll"]["my_response"] is None
    assert len(seen) == 2  # the stranger's votes and answers, once

    # Browser one takes its vote back: its own flags follow at once.
    await client.post(_u(world, f"/questions/{other['id']}/vote"), headers=_key(1))
    after = {q["id"]: q for q in (await client.get(_u(world), headers=_key(1))).json()["questions"]}
    assert after[other["id"]]["voted"] is False

    # What the organiser changes reaches everyone at once: the snapshot is forgotten.
    await client.post(_u(world, f"/questions/{other['id']}"), json={"action": "reject"}, headers=h)
    assert other["id"] not in {q["id"] for q in (await client.get(_u(world))).json()["questions"]}
    # And otherwise the snapshot lasts its second.
    from app.api.live import room

    room.rooms.clear()
    await client.get(_u(world))
    with statements(session) as seen:
        await client.get(_u(world))
    assert seen == []


async def test_a_players_poll_is_read_from_the_quiz_snapshot(client, session, world, snapshots):
    code = await _quiz(client, world, questions=2)
    players = [(await client.post("/api/v1/live/quiz/join", json={"code": code, "name": f"P{i}"}, headers=_key(i))).json() for i in range(6)]
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    keys = [{"X-Live-Player": p["player_key"]} for p in players]
    # Four answer right, quickest first; one wrong; one not at all.
    for i in range(5):
        r = await client.post(_u(world, "/quiz/answer"), json={"option_key": "a" if i < 4 else "b"}, headers=keys[i])
        assert r.status_code == 200, r.text
    # The answer shows on the player's very next poll (the snapshot is updated in place).
    assert (await client.get(_u(world, "/quiz/play"), headers=keys[0])).json()["question"]["my_answer"] == "a"

    from app.api.live import room

    room.quizzes.clear()
    await client.get(_u(world, "/quiz/play"), headers=keys[0])  # builds the quiz snapshot
    with statements(session) as seen:
        states = [(await client.get(_u(world, "/quiz/play"), headers=k)).json() for k in keys]
    assert seen == [], seen  # six polls, no statement

    # Each player's rank is what counting the players above them says.
    scores = dict((await session.execute(select(LiveQuizPlayers.player_key, LiveQuizPlayers.score).where(LiveQuizPlayers.quiz_id == (await room.quiz_view(session, world["event"].id)).quiz_id))).all())
    for p, s in zip(players, states):
        mine = scores[p["player_key"]]
        assert s["score"] == mine and s["rank"] == 1 + sum(1 for v in scores.values() if v > mine)
    assert [b["score"] for b in states[0]["leaderboard"]] == sorted(scores.values(), reverse=True)

    # The host moves on: every phone sees the next question at once.
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    nxt = (await client.get(_u(world, "/quiz/play"), headers=keys[0])).json()
    assert nxt["current_index"] == 1 and nxt["question"]["my_answer"] is None


# --- the survey ------------------------------------------------------------------------


async def test_send_now_reaches_only_those_not_sent_yet(client, session, world, monkeypatch):
    sent = []
    monkeypatch.setattr(service.email_mod, "send_email", lambda **kw: sent.append(kw["recipients"][0]))
    g = world["guests"]
    for reg in g[:2]:
        session.add(CheckIns(event_id=world["event"].id, registration_id=reg.id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate"))
    await session.commit()

    first = (await client.post(_u(world, "/feedback/send"), headers=world["h"])).json()
    await after_commit.drain()
    assert first["sent"] == 2 and sorted(sent) == sorted([g[0].email, g[1].email])
    # The docstring always said so: sending again reaches only people not sent it yet.
    session.add(CheckIns(event_id=world["event"].id, registration_id=g[2].id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate"))
    await session.commit()
    second = (await client.post(_u(world, "/feedback/send"), headers=world["h"])).json()
    await after_commit.drain()
    assert second["sent"] == 1 and second["invited"] == 3 and sent[2:] == [g[2].email]
    third = (await client.post(_u(world, "/feedback/send"), headers=world["h"])).json()
    await after_commit.drain()
    assert third["sent"] == 0 and len(sent) == 3 and third["sent_count"] == 3


async def test_the_sweep_finds_due_surveys_by_their_due_time(client, session, world, make_event, monkeypatch):
    monkeypatch.setattr(service.email_mod, "send_email", lambda **kw: None)
    event = world["event"]
    survey = await service.survey_of(session, event)
    ends = START + timedelta(hours=3)
    await session.refresh(survey)
    assert survey.due_at == ends + timedelta(minutes=60)
    # Kept with the event and the delay.
    event.event_datetime = START + timedelta(days=1)
    await session.commit()
    await session.refresh(survey)
    assert survey.due_at == ends + timedelta(days=1, minutes=60)
    await client.put(_u(world, "/feedback"), json={"delay_minutes": 10}, headers=world["h"])
    await session.refresh(survey)
    assert survey.due_at == ends + timedelta(days=1, minutes=10)

    # Many surveys never sent; the sweep reads the due ones only, by the index.
    for i in range(5):
        later = await make_event(club=world["club"], created_by=world["owner"], name=f"Later {i}", event_datetime=START + timedelta(days=30 + i), duration=1.0)
        await service.survey_of(session, later)
    assert await service.sweep(session, now=ends + timedelta(days=1, minutes=9)) == 0
    assert await service.sweep(session, now=ends + timedelta(days=1, minutes=11)) == 1
    assert (await session.scalar(select(FeedbackSurveys.sent_at).where(FeedbackSurveys.id == survey.id))) is not None
    # What the sweep asks for is a range of the due index, not every survey never sent.
    sql = str(service.due_query(START).compile())
    assert "feedback_surveys.due_at <=" in sql and " LIMIT " in sql


async def test_survey_mails_are_jobs_and_a_rerun_mails_nobody_twice(client, session, world, monkeypatch):
    sent = []

    def send_email(**kw):
        sent.append((kw["recipients"][0], kw["idempotency_key"]))

    monkeypatch.setattr(service.email_mod, "send_email", send_email)
    g = world["guests"]
    for reg in g:
        session.add(CheckIns(event_id=world["event"].id, registration_id=reg.id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate"))
    await session.commit()

    out = (await client.post(_u(world, "/feedback/send"), headers=world["h"])).json()
    assert out["sent"] == 3 and sorted(a for a, _ in sent) == sorted(r.email for r in g)
    survey = await service.survey_of(session, world["event"])
    rows = (await session.execute(select(FeedbackSends.registration_id, FeedbackSends.mailed_at).where(FeedbackSends.survey_id == survey.id))).all()
    assert len(rows) == 3 and all(at is not None for _, at in rows)
    # One job per guest, keyed by the guest.
    keys = (await session.scalars(text("SELECT dedupe_key FROM jobs WHERE name = 'live.survey_mail' ORDER BY dedupe_key"))).all()
    assert keys == sorted(f"live:survey:{survey.id}:{r.id}" for r in g)

    # The queue is at least once: every job run again mails nobody.
    for reg in g:
        await service._survey_mail_job({"survey_id": str(survey.id), "registration_id": str(reg.id)})
    assert len(sent) == 3
    # Sending the survey again writes no job and mails nobody.
    again = (await client.post(_u(world, "/feedback/send"), headers=world["h"])).json()
    await jobs.settle()
    assert again["sent"] == 0 and len(sent) == 3

    # A mail the provider does not take is tried again, and only that one.
    later = await service.survey_of(session, world["event"])
    await session.execute(update(FeedbackSends).where(FeedbackSends.survey_id == later.id, FeedbackSends.registration_id == g[1].id).values(mailed_at=None))
    await session.commit()
    calls = []

    def refuse(**kw):
        calls.append(kw["recipients"][0])
        raise RuntimeError("the provider is down")

    monkeypatch.setattr(service.email_mod, "send_email", refuse)
    with pytest.raises(RuntimeError):
        await service._survey_mail_job({"survey_id": str(later.id), "registration_id": str(g[1].id)})
    assert (await session.scalar(select(FeedbackSends.mailed_at).where(FeedbackSends.registration_id == g[1].id))) is None
    monkeypatch.setattr(service.email_mod, "send_email", send_email)
    for reg in g:
        await service._survey_mail_job({"survey_id": str(later.id), "registration_id": str(reg.id)})
    assert [a for a, _ in sent[3:]] == [g[1].email] and calls == [g[1].email]
    # The provider is given the same key each time, so a retry after a cut-off run is one mail.
    assert sent[3][1] == f"live:survey:{later.id}:{g[1].id}"


async def test_one_unfinished_job_per_guest_however_often_it_is_enqueued(session, world):
    survey = await service.survey_of(session, world["event"])
    items = [({"survey_id": str(survey.id), "registration_id": str(r.id)}, service._survey_mail_key(survey.id, r.id)) for r in world["guests"]]
    assert await jobs.enqueue_many(session, service.SURVEY_MAIL, items) == 3
    assert await jobs.enqueue_many(session, service.SURVEY_MAIL, items) == 0
    await session.commit()
    assert (await session.scalar(text("SELECT count(*) FROM jobs WHERE name = 'live.survey_mail' AND finished_at IS NULL"))) == 3
