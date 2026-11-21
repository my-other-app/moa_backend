"""Event quests (feature-register/quests/CONTRACT.md): every kind of task,
milestones and their rewards' stock, stations and the swag desk at the door
(offline and from consoles from before), codes, grants and revokes, the
leaderboard and the export -- and that an event without a quest, and every
entrance, behaves exactly as before."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.checkins.models import CheckIns, Doors
from app.api.merch.models import MerchCollections, MerchItems, MerchVariants
from app.api.quests import engine, signals, switch
from app.api.quests.models import QuestAudit, QuestClaims, QuestEntries, QuestProgress, QuestStationVisits, QuestUnlocks
from app.core import jobs
from app.core.jobs import Jobs
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)
NEW = {"X-Gate-Features": "quests"}


@pytest.fixture(autouse=True)
def _at_once(monkeypatch):
    """Evaluations run when the request's jobs settle (nothing waits for a bucket here)."""
    from app.api.quests import guest

    monkeypatch.setattr(signals, "BUCKET_SECONDS", 0)
    switch.forget()
    guest._looked.clear()
    yield
    switch.forget()
    guest._looked.clear()


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Quest Club", email="quest-host@example.com")
    event = await make_event(club=club, created_by=owner, name="Quest Night", event_datetime=NOW - timedelta(hours=1), duration=4.0)
    guests = {}
    for key, name in (("a", "Asha Kumar"), ("b", "Ben Thomas"), ("c", "Chen Li"), ("d", "Dev Rao")):
        u = await make_user(email=f"q-{key}@example.com", username=f"q{key}")
        u.full_name = name
        r = await make_registration(event=event, user=u, full_name=name)
        guests[key] = (u, r, auth_header(await mint_token(u)))
    shirt = MerchItems(id=uuid.uuid4(), event_id=event.id, name="T-shirt", stock=None, variant_field="T-shirt size")
    shirt.variants = [MerchVariants(id=uuid.uuid4(), label="M", stock=1), MerchVariants(id=uuid.uuid4(), label="L", stock=1)]
    sticker = MerchItems(id=uuid.uuid4(), event_id=event.id, name="Sticker", stock=1)
    session.add_all([shirt, sticker])
    await session.commit()
    return {"event": event, "club": auth_header(await mint_token(owner)), "owner": owner, "g": guests, "shirt": shirt, "sticker": sticker}


def url(w, path=""):
    return f"{API}/events/{w['event'].id}{path}"


async def ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


async def setup(client, w, *, tasks, milestones=((1, "sticker", "first_come"),), mode="checkmarks", **settings):
    """The quest switched on with these tasks and milestones (threshold, item, sold-out mode)."""
    h = w["club"]
    made = []
    for t in tasks:
        made.append(await ok(await client.post(url(w, "/quests/tasks"), json=t, headers=h), 201))
    for threshold, item, mode_ in milestones:
        m = await ok(await client.post(url(w, "/quests/milestones"), json={"title": f"Tier {threshold}", "threshold": threshold}, headers=h), 201)
        if item:
            await ok(
                await client.post(url(w, f"/quests/milestones/{m['id']}/rewards"), json={"merch_item_id": str(w[item].id), "sold_out_mode": mode_}, headers=h),
                201,
            )
    await ok(await client.patch(url(w, "/quests"), json={"enabled": True, "mode": mode, **settings}, headers=h))
    return made


def scan(code, door=None, ref=None, at=None):
    body = {"code": code, "client_ref": ref or uuid.uuid4().hex, "scanned_at": (at or datetime.now(timezone.utc)).isoformat()}
    if door:
        body["door"] = door
    return body


async def card(client, w, key):
    return await ok(await client.get(url(w, "/quest"), headers=w["g"][key][2]))


def task_me(c, task_id):
    return next(t["me"] for t in c["tasks"] if t["id"] == task_id)


async def check_in(client, w, key):
    return await ok(await client.post(url(w, "/gate/scan"), json=scan(w["g"][key][1].ticket_id, door="Gate A"), headers=w["club"]))


# ─── Off means off ──────────────────────────────────────────────────────


async def test_an_event_without_a_quest_or_with_it_off_is_untouched(client, session, world):
    w = world
    assert await card(client, w, "a") == {"enabled": False}
    r = await check_in(client, w, "a")
    assert r["outcome"] == "CHECKED_IN" and r["station"] is None and r["swag"] is None
    # The quest made but off: still nothing.
    await ok(await client.post(url(w, "/quests/tasks"), json={"kind": "checked_in"}, headers=w["club"]), 201)
    await check_in(client, w, "b")
    assert await card(client, w, "b") == {"enabled": False}
    assert not await session.scalar(select(func.count()).select_from(Jobs).where(Jobs.name.like("quests.%")))
    assert not await session.scalar(select(func.count()).select_from(QuestProgress))
    ctx = await ok(await client.get(url(w, "/gate/context"), headers=w["club"]))
    assert ctx["quests"] is None and all("purpose" not in d for d in ctx["doors"])


# ─── Check-in, milestone, reservation, notification ─────────────────────


async def test_a_check_in_ticks_unlocks_reserves_and_tells_the_guest(client, session, world):
    w = world
    (t,) = await setup(client, w, tasks=[{"kind": "checked_in", "title": "Check in"}])
    await check_in(client, w, "a")
    c = await card(client, w, "a")
    assert task_me(c, t["id"])["done"] is True and c["me"]["tasks_done"] == 1
    reward = c["milestones"][0]["rewards"][0]
    assert c["milestones"][0]["unlocked"] and reward["claim"]["status"] == "reserved"
    item = await session.scalar(select(MerchItems).where(MerchItems.id == w["sticker"].id).execution_options(populate_existing=True))
    assert item.reserved_count == 1
    # A sticker has no sizes: its held unit is never counted as waiting for one.
    live = await ok(await client.get(url(w, "/quests/dashboard"), headers=w["club"]))
    assert [(x["reserved"], x["needs_size"]) for x in live["rewards"]] == [(1, 0)]
    admin = await ok(await client.get(url(w, "/quests"), headers=w["club"]))
    assert admin["milestones"][0]["rewards"][0]["claims"]["needs_size"] == 0
    from app.api.notifications.models import Notifications

    note = await session.scalar(select(Notifications).where(Notifications.user_id == w["g"]["a"][0].id, Notifications.type == "quest_unlocked"))
    assert note is not None and "Sticker" in note.description
    # Evaluating again changes nothing (idempotent): one unlock, one claim, one note.
    await engine.evaluate(session, w["event"].id, everyone=True)
    await jobs.settle()
    assert await session.scalar(select(func.count()).select_from(QuestUnlocks)) == 1
    assert await session.scalar(select(func.count()).select_from(QuestClaims)) == 1
    assert await session.scalar(select(func.count()).select_from(Notifications).where(Notifications.type == "quest_unlocked")) == 1
    # The second guest: the last sticker is gone (first come, first served).
    await check_in(client, w, "b")
    cb = await card(client, w, "b")
    assert cb["milestones"][0]["rewards"][0]["claim"]["status"] == "sold_out"


async def test_switching_on_credits_what_guests_already_did(client, session, world):
    w = world
    await check_in(client, w, "a")
    (t,) = await setup(client, w, tasks=[{"kind": "checked_in"}], milestones=())
    c = await card(client, w, "a")
    assert task_me(c, t["id"])["done"] is True


# ─── The Network ────────────────────────────────────────────────────────


async def _met(session, owner, person, event, at, method="qr"):
    from app.api.profiles.models import NetworkEncounters, NetworkPeople

    np = await session.scalar(select(NetworkPeople).where(NetworkPeople.owner_id == owner.id, NetworkPeople.person_id == person.id))
    if np is None:
        np = NetworkPeople(owner_id=owner.id, person_id=person.id, tags=[], mutual=False)
        session.add(np)
        await session.flush()
    session.add(NetworkEncounters(network_person_id=np.id, event_id=event.id if event else None, method=method, met_at=at))
    await session.flush()


async def test_the_network_task_counts_distinct_checked_in_people_by_qr_with_a_minute_cap(client, session, world, make_user, make_registration):
    w = world
    for k in "abcd":
        w["g"][k][1].is_attended = True
    await session.commit()
    either, saved, saved_by = await setup(
        client,
        w,
        milestones=(),
        tasks=[
            {"kind": "network", "config": {"count": 3, "direction": "either", "per_minute": 2}},
            {"kind": "network", "title": "Scan 2", "config": {"count": 2, "direction": "saved", "per_minute": 5}},
            {"kind": "network", "title": "Be scanned 1", "config": {"count": 1, "direction": "saved_by", "per_minute": 5}},
        ],
    )
    a, b, c, d = (w["g"][k][0] for k in "abcd")
    minute = (NOW - timedelta(minutes=30)).replace(second=0, microsecond=0)
    await _met(session, a, b, w["event"], minute + timedelta(seconds=5))
    await _met(session, a, c, w["event"], minute + timedelta(seconds=20))
    await _met(session, a, d, w["event"], minute + timedelta(seconds=40))  # the third in that minute: not counted
    await _met(session, b, a, w["event"], minute + timedelta(minutes=3))  # Ben again, the other way: the same person
    stranger = await make_user(email="q-x@example.com", username="qx")
    await make_registration(event=w["event"], user=stranger, is_attended=False)
    await _met(session, a, stranger, w["event"], minute + timedelta(minutes=5))  # not checked in
    await _met(session, d, a, None, minute + timedelta(minutes=6))  # not at the event
    await session.commit()
    await engine.evaluate(session, w["event"].id, kinds=["network"], user_ids=[a.id])
    c_ = await card(client, w, "a")
    assert task_me(c_, either["id"]) == {**task_me(c_, either["id"]), "count": 2, "done": False}
    assert task_me(c_, saved["id"])["done"] is True  # Ben, Chen, Dev saved (cap 5)
    assert task_me(c_, saved_by["id"])["done"] is True  # Ben saved her
    # A different minute: Dev (at the event, by QR) now counts.
    from app.api.profiles.models import NetworkEncounters, NetworkPeople

    np_d = await session.scalar(select(NetworkPeople).where(NetworkPeople.owner_id == a.id, NetworkPeople.person_id == d.id))
    enc = await session.scalar(select(NetworkEncounters).where(NetworkEncounters.network_person_id == np_d.id))
    enc.met_at = minute + timedelta(minutes=10)
    await session.commit()
    await engine.evaluate(session, w["event"].id, kinds=["network"], user_ids=[a.id])
    assert task_me(await card(client, w, "a"), either["id"])["done"] is True


async def test_a_qr_save_at_the_event_sets_the_task_off_for_both_people(client, session, world):
    from tests.profiles_world import code_of, save
    from app.api.users.models import UserProfiles

    w = world
    for k in "ab":
        w["g"][k][1].is_attended = True
        session.add(UserProfiles(user_id=w["g"][k][0].id, full_name=w["g"][k][0].full_name))
    await session.commit()
    (t,) = await setup(client, w, milestones=(), tasks=[{"kind": "network", "config": {"count": 2, "direction": "either"}}])
    code = await code_of(client, w["g"]["b"][2])
    r = await save(client, w["g"]["a"][2], code=code, method="qr", event_id=w["event"].id)
    assert r.status_code in (200, 201), r.text
    # The save answers with the saver's count at once ("1 of 2 met").
    assert r.json()["quest"] == {"event_id": w["event"].id, "tasks": [{"task_id": t["id"], "title": t["title"], "count": 1, "target": 2, "done": False}]}
    assert task_me(await card(client, w, "a"), t["id"])["count"] == 1
    assert task_me(await card(client, w, "b"), t["id"])["count"] == 1
    # The same scan again: no new meeting, the same count.
    again = await save(client, w["g"]["a"][2], code=code, method="qr", event_id=w["event"].id)
    assert again.json()["new_encounter"] is False and again.json()["quest"]["tasks"][0]["count"] == 1
    # A save with no event, or at an event without a quest: no quest block.
    plain = await save(client, w["g"]["b"][2], code=await code_of(client, w["g"]["a"][2]), method="qr")
    assert plain.json()["quest"] is None


# ─── The live room, feedback, posters, follows, the profile ─────────────


async def test_quiz_polls_questions_and_feedback(client, session, world):
    from app.api.live.models import (
        FeedbackResponses,
        FeedbackSurveys,
        LivePollResponses,
        LivePolls,
        LiveQuestions,
        LiveQuizAnswers,
        LiveQuizPlayers,
        LiveQuizQuestions,
        LiveQuizzes,
        PollStatus,
        QuestionStatus,
        QuizStatus,
    )
    from app.api.live.room import account_key

    w = world
    ev_id = w["event"].id
    played, score, top, polls, qa, fb = await setup(
        client,
        w,
        milestones=(),
        tasks=[
            {"kind": "quiz_played"},
            {"kind": "quiz_score", "config": {"min_score": 900}},
            {"kind": "quiz_top", "config": {"top": 1}},
            {"kind": "polls", "config": {"count": 2}},
            {"kind": "qa_question"},
            {"kind": "feedback"},
        ],
    )
    a, ra = w["g"]["a"][0], w["g"]["a"][1]
    rb = w["g"]["b"][1]
    quiz = LiveQuizzes(id=uuid.uuid4(), event_id=ev_id, title="Quiz", status=QuizStatus.live, current_index=0)
    session.add(quiz)
    await session.flush()
    q1 = LiveQuizQuestions(id=uuid.uuid4(), quiz_id=quiz.id, prompt="?", options=[{"key": "a", "label": "x"}, {"key": "b", "label": "y"}], correct_key="a")
    pa = LiveQuizPlayers(id=uuid.uuid4(), quiz_id=quiz.id, name="Asha", player_key="k-a-123456", registration_id=ra.id, score=950)
    pb = LiveQuizPlayers(id=uuid.uuid4(), quiz_id=quiz.id, name="Ben", player_key="k-b-123456", registration_id=rb.id, score=400)
    session.add_all([q1, pa, pb])
    await session.flush()
    session.add_all([LiveQuizAnswers(question_id=q1.id, player_id=p.id, option_key="a", correct=True, points=p.score, answered_at=NOW) for p in (pa, pb)])
    p1 = LivePolls(id=uuid.uuid4(), event_id=ev_id, prompt="One?", options=["x", "y"], status=PollStatus.live)
    p2 = LivePolls(id=uuid.uuid4(), event_id=ev_id, prompt="Two?", options=["x", "y"], status=PollStatus.live)
    session.add_all([p1, p2])
    await session.flush()
    session.add_all([LivePollResponses(poll_id=p.id, voter_key=account_key(a.id), value="x") for p in (p1, p2)])
    session.add(LiveQuestions(event_id=ev_id, text="Why?", author_key=account_key(a.id), status=QuestionStatus.approved, votes=0, pinned=False))
    survey = FeedbackSurveys(id=uuid.uuid4(), event_id=ev_id, questions=[])
    session.add(survey)
    await session.flush()
    session.add(FeedbackResponses(survey_id=survey.id, registration_id=ra.id, answers={"x": 5}))
    await session.commit()
    await engine.evaluate(session, ev_id, everyone=True)
    c = await card(client, w, "a")
    assert all(task_me(c, t["id"])["done"] for t in (played, score, polls, qa, fb))
    assert task_me(c, top["id"])["done"] is False  # not until the quiz has ended
    # The host ends the quiz: the hook evaluates every player.
    from app.api.live import service as live

    await live.end_quiz(session, quiz)
    await jobs.settle()
    assert task_me(await card(client, w, "a"), top["id"])["done"] is True
    assert task_me(await card(client, w, "b"), top["id"])["done"] is False


async def test_poster_follow_and_profile_are_looked_at_by_the_card(client, session, world):
    from app.api.clubs.models import ClubUsersLink
    from app.api.guest_posters.models import GuestPosters
    from app.api.links.models import TrackingClicks, TrackingLinks

    w = world
    made, link, follow, profile = await setup(
        client,
        w,
        milestones=(),
        tasks=[
            {"kind": "poster_made"},
            {"kind": "poster_link", "config": {"metric": "clicks", "count": 2}},
            {"kind": "follow_club"},
            {"kind": "profile_complete"},
        ],
    )
    a, ra = w["g"]["a"][0], w["g"]["a"][1]
    tl = TrackingLinks(
        id=uuid.uuid4(),
        club_id=w["event"].club_id,
        event_id=w["event"].id,
        name="Asha's poster",
        code=f"qp{uuid.uuid4().hex[:6]}",
        kind="poster_guest",
        utm_source="poster",
        utm_medium="share",
    )
    session.add(tl)
    await session.flush()
    session.add(GuestPosters(event_id=w["event"].id, registration_id=ra.id, tracking_link_id=tl.id, made_at=NOW))
    session.add_all([TrackingClicks(link_id=tl.id, visitor=v) for v in ("v1", "v1", "v2")])
    session.add(ClubUsersLink(club_id=w["event"].club_id, user_id=a.id, is_following=True))
    # The poster's first render calls the hook (guest_posters.render).
    await signals.touch(session, w["event"].id, ("poster_made",), registration_id=ra.id)
    await session.commit()
    await jobs.settle()
    c = await card(client, w, "a")
    for t in (made, link, follow):
        assert task_me(c, t["id"])["done"] is True, t["kind"]
    assert task_me(c, profile["id"])["done"] is False


# ─── Stations ───────────────────────────────────────────────────────────


async def _station(client, w, name="Cloud booth", purpose="station", task=True):
    body = {"name": name, "purpose": purpose, "link": {"days": 2}}
    if task:
        body["task"] = {"title": f"Visit {name}", "points": 10}
    return await ok(await client.post(url(w, "/quests/stations"), json=body, headers=w["club"]), 201)


async def test_a_station_records_a_visit_and_never_admits(client, session, world):
    w = world
    await setup(client, w, tasks=[], milestones=())
    st = await _station(client, w)
    door_h = {"X-Gate-Key": st["link"]["key"], **NEW}
    ra = w["g"]["a"][1]
    ref = uuid.uuid4().hex
    r = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id, ref=ref), headers=door_h))
    assert r["outcome"] == "STATION_VISIT" and r["station"]["result"] == "VISITED" and r["station"]["task"]["title"] == "Visit Cloud booth"
    assert r["attendee"]["name"] == "Asha Kumar" and r["attendee"]["ticket_id"] == "" and r["attendee"]["email"] is None
    assert await session.scalar(select(func.count()).select_from(CheckIns)) == 0
    again = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id), headers=door_h))
    assert again["station"]["result"] == "ALREADY_VISITED"
    replay = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id, ref=ref), headers=door_h))
    assert replay["replayed"] is True and replay["station"]["result"] == "VISITED"
    c = await card(client, w, "a")
    assert task_me(c, st["task"]["id"])["done"] is True and c["tasks"][0]["station"]["name"] == "Cloud booth"
    # A label naming the station, from the club's own console: a visit too, not an admission.
    rb = w["g"]["b"][1]
    by_label = await ok(await client.post(url(w, "/gate/scan"), json=scan(rb.ticket_id, door="cloud BOOTH"), headers={**w["club"], **NEW}))
    assert by_label["outcome"] == "STATION_VISIT"
    # By hand at the station.
    rc = w["g"]["c"][1]
    hand = await ok(await client.post(url(w, f"/gate/check-in/{rc.id}"), json={"door": "Cloud booth"}, headers={**w["club"], **NEW}))
    assert hand["outcome"] == "STATION_VISIT"
    assert await session.scalar(select(func.count()).select_from(CheckIns)) == 0
    assert await session.scalar(select(func.count()).select_from(QuestStationVisits)) == 3
    # A walk-up at a station is refused.
    walk = await client.post(url(w, "/gate/walkup"), json={"name": "New", "door": "Cloud booth", "client_ref": uuid.uuid4().hex}, headers={**w["club"], **NEW})
    assert walk.status_code == 409 and walk.json()["error_code"] == "QUEST_DOOR_NO_WALKUPS"


async def test_a_console_from_before_quests_gets_a_final_refusal_at_a_station(client, session, world):
    w = world
    await setup(client, w, tasks=[], milestones=())
    st = await _station(client, w)
    old = {"X-Gate-Key": st["link"]["key"]}
    ra = w["g"]["a"][1]
    r = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id), headers=old))
    assert r["outcome"] == "INVALID" and r["hint"] == "QUEST_STATION" and r["attendee"] is None and "station" in r["error"]
    assert await session.scalar(select(func.count()).select_from(CheckIns)) == 0
    # The visit is recorded all the same (staff scanned the guest there).
    assert await session.scalar(select(func.count()).select_from(QuestStationVisits)) == 1
    batch = await ok(await client.post(url(w, "/gate/scan-batch"), json={"scans": [scan(w["g"]["b"][1].ticket_id)]}, headers=old))
    assert batch["results"][0]["response"]["outcome"] == "INVALID" and not batch["results"][0]["response"].get("retry")
    # The old console's door list: entrances only.
    ctx = await ok(await client.get(url(w, "/gate/context"), headers=w["club"]))
    assert all(d["name"] != "Cloud booth" for d in ctx["doors"])
    ctx_new = await ok(await client.get(url(w, "/gate/context"), headers={**w["club"], **NEW}))
    assert any(d["name"] == "Cloud booth" and d["purpose"] == "station" for d in ctx_new["doors"])
    on_link = await ok(await client.get(url(w, "/gate/context"), headers={"X-Gate-Key": st["link"]["key"], **NEW}))
    assert on_link["quests"]["door_purpose"] == "station" and on_link["quests"]["can_hand_over"] is False
    on_link_old = await ok(await client.get(url(w, "/gate/context"), headers=old))
    assert on_link_old["expected_door_id"] is None and on_link_old["quests"] is None


async def test_offline_station_scans_keep_their_time_and_count_once(client, session, world):
    w = world
    await setup(client, w, tasks=[], milestones=())
    st = await _station(client, w)
    h = {"X-Gate-Key": st["link"]["key"], **NEW}
    ra, rb = w["g"]["a"][1], w["g"]["b"][1]
    at = datetime.now(timezone.utc) - timedelta(minutes=20)
    ref = uuid.uuid4().hex
    body = {"scans": [scan(ra.ticket_id, ref=ref, at=at), scan(ra.ticket_id, at=at + timedelta(minutes=1)), scan(rb.ticket_id, at=at), scan("MOA-NOPE-NOPE1")]}
    out = await ok(await client.post(url(w, "/gate/scan-batch"), json=body, headers=h))
    results = [x["response"] for x in out["results"]]
    assert [r["outcome"] for r in results] == ["STATION_VISIT", "STATION_VISIT", "STATION_VISIT", "INVALID"]
    assert [r["station"]["result"] for r in results[:3]] == ["VISITED", "ALREADY_VISITED", "VISITED"]
    visit = await session.scalar(select(QuestStationVisits).where(QuestStationVisits.registration_id == ra.id))
    assert abs((visit.visited_at - at).total_seconds()) < 1 and visit.via == "offline_sync"
    again = await ok(await client.post(url(w, "/gate/scan-batch"), json={"scans": [body["scans"][0]]}, headers=h))
    assert again["results"][0]["response"]["replayed"] is True
    assert await session.scalar(select(func.count()).select_from(QuestStationVisits)) == 2


async def test_a_station_while_the_quest_is_off_records_but_credits_later(client, session, world):
    w = world
    st = await _station(client, w)
    h = {"X-Gate-Key": st["link"]["key"], **NEW}
    r = await ok(await client.post(url(w, "/gate/scan"), json=scan(w["g"]["a"][1].ticket_id), headers=h))
    assert r["station"]["result"] == "NOT_COUNTED" and r["station"]["reason"] == "QUESTS_OFF"
    await ok(await client.patch(url(w, "/quests"), json={"enabled": True}, headers=w["club"]))
    assert task_me(await card(client, w, "a"), st["task"]["id"])["done"] is True


# ─── The swag desk ──────────────────────────────────────────────────────


async def test_the_swag_desk_shows_rewards_and_hands_them_over_once(client, session, world):
    w = world
    (t,) = await setup(client, w, tasks=[{"kind": "manual", "title": "Submitted"}], milestones=((1, "shirt", "waitlist"), (1, "sticker", "first_come"))[0:1])
    ra = w["g"]["a"][1]
    await ok(await client.post(url(w, f"/quests/guests/{ra.id}/tasks/{t['id']}/grant"), json={"note": "Demoed"}, headers=w["club"]))
    desk = await _station(client, w, name="Swag desk", purpose="swag_desk", task=False)
    h = {"X-Gate-Key": desk["link"]["key"], **NEW}
    r = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id), headers=h))
    assert r["outcome"] == "SWAG_DESK"
    (reward,) = r["swag"]["rewards"]
    assert reward["status"] == "reserved" and reward["needs_size"] is True and {c["label"] for c in reward["choices"]} == {"M", "L"}
    live = await ok(await client.get(url(w, "/quests/dashboard"), headers=w["club"]))
    assert [(x["reserved"], x["needs_size"]) for x in live["rewards"]] == [(1, 1)]
    need = await ok(await client.post(url(w, f"/gate/quests/claims/{reward['claim_id']}/handover"), json={}, headers=h))
    assert need["result"] == "NEEDS_SIZE"
    done = await ok(await client.post(url(w, f"/gate/quests/claims/{reward['claim_id']}/handover"), json={"variant_label": "m"}, headers=h))
    assert done["result"] == "HANDED_OVER" and done["claim"]["variant_label"] == "M" and done["claim"]["status"] == "collected"
    row = await session.scalar(select(MerchCollections).where(MerchCollections.registration_id == ra.id))
    assert row.door_label == "Swag desk"
    m = await session.scalar(
        select(MerchVariants).where(MerchVariants.label == "M", MerchVariants.item_id == w["shirt"].id).execution_options(populate_existing=True)
    )
    assert (m.collected_count, m.reserved_count) == (1, 0)
    twice = await ok(await client.post(url(w, f"/gate/quests/claims/{reward['claim_id']}/handover"), json={}, headers=h))
    assert twice["result"] == "ALREADY_COLLECTED"
    rescan = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id), headers=h))
    assert rescan["swag"]["rewards"][0]["status"] == "collected" and rescan["swag"]["rewards"][0]["door"] == "Swag desk"
    # An entrance's door link may not hand rewards over; a console from before gets a known refusal at the desk.
    entry = await ok(await client.post(url(w, "/doors"), json={"name": "Gate A"}, headers=w["club"]))
    key = (await ok(await client.post(url(w, f"/doors/{entry['id']}/link"), json={}, headers=w["club"])))["key"]
    no = await client.post(url(w, f"/gate/quests/claims/{reward['claim_id']}/handover"), json={}, headers={"X-Gate-Key": key, **NEW})
    assert no.status_code == 403 and no.json()["error_code"] == "QUEST_HANDOVER_NOT_ALLOWED"
    old = await ok(await client.post(url(w, "/gate/scan"), json=scan(ra.ticket_id), headers={"X-Gate-Key": desk["link"]["key"]}))
    assert old["outcome"] == "INVALID" and old["hint"] == "QUEST_SWAG_DESK"
    audit = await ok(await client.get(url(w, "/quests/audit"), headers=w["club"]))
    assert [a["action"] for a in audit["items"]] == ["handover", "grant"]


async def test_the_merch_tab_hands_rewards_over_by_the_claim(client, session, world):
    w = world
    (t,) = await setup(client, w, tasks=[{"kind": "manual"}], milestones=((1, "sticker", "first_come"),))
    ra, rb = w["g"]["a"][1], w["g"]["b"][1]
    await ok(await client.post(url(w, f"/quests/guests/{ra.id}/tasks/{t['id']}/grant"), json={}, headers=w["club"]))
    item_id = str(w["sticker"].id)
    rows_b = (await ok(await client.get(url(w, f"/gate/merch/{rb.id}"), headers=w["club"])))["items"]
    assert next(r for r in rows_b if r["item_id"] == item_id)["status"] == "NOT_ELIGIBLE"
    no = await ok(await client.post(url(w, f"/gate/merch/{item_id}/collect"), json={"registration_id": str(rb.id)}, headers=w["club"]))
    assert no["collected"] is False and no["status"] == "NOT_ELIGIBLE"
    rows_a = (await ok(await client.get(url(w, f"/gate/merch/{ra.id}"), headers=w["club"])))["items"]
    assert next(r for r in rows_a if r["item_id"] == item_id)["status"] == "COLLECTABLE"
    yes = await ok(await client.post(url(w, f"/gate/merch/{item_id}/collect"), json={"registration_id": str(ra.id)}, headers=w["club"]))
    assert yes["collected"] is True
    claim = await session.scalar(select(QuestClaims).where(QuestClaims.registration_id == ra.id).execution_options(populate_existing=True))
    assert claim.status == "collected" and claim.collection_id is not None
    # The merch page: no counting a reward by hand, no deleting it while it is one.
    by_hand = await client.post(url(w, f"/merch/{item_id}/handout"), headers=w["club"])
    assert by_hand.status_code == 409 and by_hand.json()["error_code"] == "MERCH_ITEM_IS_A_REWARD"
    gone = await client.delete(url(w, f"/merch/{item_id}"), headers=w["club"])
    assert gone.status_code == 409 and gone.json()["error_code"] == "MERCH_ITEM_IS_A_REWARD"
    listed = await ok(await client.get(url(w, "/merch"), headers=w["club"]))
    assert next(i for i in listed if i["id"] == item_id)["reserved"] == 0


# ─── Stock: waiting lists, releases, the race ───────────────────────────


async def test_waitlist_is_served_in_order_when_a_unit_frees(client, session, world):
    w = world
    (t,) = await setup(client, w, tasks=[{"kind": "manual"}], milestones=((1, "sticker", "waitlist"),))
    ra, rb, rc = (w["g"][k][1] for k in "abc")
    for r in (ra, rb, rc):
        await ok(await client.post(url(w, f"/quests/guests/{r.id}/tasks/{t['id']}/grant"), json={}, headers=w["club"]))
    claims = {c.registration_id: c for c in (await session.scalars(select(QuestClaims).execution_options(populate_existing=True))).all()}
    assert [claims[r.id].status for r in (ra, rb, rc)] == ["reserved", "waitlisted", "waitlisted"]
    cb = await card(client, w, "c")
    assert cb["milestones"][0]["rewards"][0]["claim"]["waitlist_position"] == 2
    # The swag desk sees the same place in line.
    desk = await ok(await client.get(url(w, f"/gate/quests/{rc.id}"), headers={**w["club"], **NEW}))
    assert [(x["status"], x["waitlist_position"]) for x in desk["swag"]["rewards"]] == [("waitlisted", 2)]
    # Asha's tick is taken back: her unit goes to Ben, the first in line.
    await ok(await client.post(url(w, f"/quests/guests/{ra.id}/tasks/{t['id']}/revoke"), json={"note": "Mistake"}, headers=w["club"]))
    claims = {c.registration_id: c for c in (await session.scalars(select(QuestClaims).execution_options(populate_existing=True))).all()}
    assert [claims[r.id].status for r in (ra, rb, rc)] == ["released", "reserved", "waitlisted"]
    unlock = await session.scalar(select(QuestUnlocks).where(QuestUnlocks.registration_id == ra.id).execution_options(populate_existing=True))
    assert unlock.revoked_at is not None
    # The log keeps the release beside the revoke that caused it.
    log = await ok(await client.get(url(w, "/quests/audit"), headers=w["club"]))
    assert [(x["action"], x["reward"] and x["reward"]["name"]) for x in log["items"][:2]] == [("release", "Sticker"), ("revoke", None)]
    from app.api.notifications.models import Notifications

    assert (
        await session.scalar(
            select(func.count()).select_from(Notifications).where(Notifications.type == "quest_reward_held", Notifications.user_id == w["g"]["b"][0].id)
        )
        == 1
    )
    # More stock: Chen is served too.
    await ok(await client.patch(url(w, f"/merch/{w['sticker'].id}"), json={"stock": 2}, headers=w["club"]))
    claim_c = await session.scalar(select(QuestClaims).where(QuestClaims.registration_id == rc.id).execution_options(populate_existing=True))
    assert claim_c.status == "reserved"
    # Stock can't go below what is held.
    low = await client.patch(url(w, f"/merch/{w['sticker'].id}"), json={"stock": 1}, headers=w["club"])
    assert low.status_code == 422
    # A revoked task isn't ticked again by the rules, and the grant, the revoke and the release it caused are in the audit.
    audit = await ok(await client.get(url(w, f"/quests/guests/{ra.id}"), headers=w["club"]))
    assert [a["action"] for a in audit["audit"]] == ["release", "revoke", "grant"] and audit["audit"][1]["note"] == "Mistake"


async def test_two_guests_racing_for_the_last_unit_one_gets_it(session, _engine, make_club, make_event, make_user, make_registration):
    """Real transactions on two connections: the item's row lock decides."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.quests import evaluators as ev
    from app.api.quests.models import QuestMilestones, QuestRewards, Quests

    maker = async_sessionmaker(bind=_engine, expire_on_commit=False)
    async with maker() as s:
        owner, club = await _committed_world(s)
        event = await make_event_in(s, club, owner)
        item = MerchItems(id=uuid.uuid4(), event_id=event.id, name="Last shirt", stock=1)
        quest = Quests(id=uuid.uuid4(), event_id=event.id, enabled=True)
        s.add_all([item, quest])
        await s.flush()
        m = QuestMilestones(id=uuid.uuid4(), quest_id=quest.id, title="T1", threshold=0)
        s.add(m)
        await s.flush()
        s.add(QuestRewards(id=uuid.uuid4(), quest_id=quest.id, event_id=event.id, milestone_id=m.id, merch_item_id=item.id, sold_out_mode="first_come"))
        regs = []
        from app.api.events.models import EventRegistrationsLink
        from app.api.users.models import Users

        for i in range(2):
            u = Users(full_name=f"Racer {i}", username=f"racer{uuid.uuid4().hex[:6]}", email=f"racer{uuid.uuid4().hex[:6]}@example.com", password="x")
            s.add(u)
            await s.flush()
            r = EventRegistrationsLink(event_id=event.id, user_id=u.id, full_name=u.full_name, email=u.email, ticket_id=f"MOA-R{uuid.uuid4().hex[:8].upper()}")
            s.add(r)
            await s.flush()
            regs.append((r.id, u.id))
        await s.commit()
        ids = dict(event=event.id, item=item.id, quest=quest.id, club=club.id, owner=owner.id, users=[u for _, u in regs])
    try:

        async def unlock(reg_id, user_id):
            async with maker() as s2:
                q = await s2.get(Quests, ids["quest"])
                from app.api.events.models import Events

                e = await s2.get(Events, ids["event"])
                d = await engine.definition(s2, q)
                await engine.refresh(s2, d, e, [ev.Guest(reg_id, user_id)])
                await s2.commit()

        await asyncio.gather(*(unlock(r, u) for r, u in regs))
        async with maker() as s3:
            statuses = sorted((await s3.scalars(select(QuestClaims.status).where(QuestClaims.merch_item_id == ids["item"]))).all())
            reserved = await s3.scalar(select(MerchItems.reserved_count).where(MerchItems.id == ids["item"]))
        assert statuses == ["reserved", "sold_out"] and reserved == 1
    finally:
        async with maker() as s4:
            from sqlalchemy import text

            await s4.execute(text("DELETE FROM jobs WHERE name LIKE 'quests.%'"))
            await s4.execute(text("DELETE FROM quests WHERE id = :q"), {"q": ids["quest"]})
            await s4.execute(text("DELETE FROM merch_items WHERE id = :i"), {"i": ids["item"]})
            await s4.execute(text("DELETE FROM event_registrations_link WHERE event_id = :e"), {"e": ids["event"]})
            await s4.execute(text("DELETE FROM events WHERE id = :e"), {"e": ids["event"]})
            await s4.execute(text("DELETE FROM event_categories WHERE created_by_id = :o"), {"o": ids["owner"]})
            await s4.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": ids["users"]})
            await s4.execute(text("DELETE FROM clubs WHERE id = :c"), {"c": ids["club"]})
            await s4.execute(text("DELETE FROM users WHERE id = :o"), {"o": ids["owner"]})
            await s4.commit()


async def _committed_world(s):
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users, UserTypes

    tag = uuid.uuid4().hex[:6]
    owner = Users(full_name="Race Club", username=f"raceclub{tag}", email=f"raceclub{tag}@example.com", password="x", user_type=UserTypes.club)
    s.add(owner)
    await s.flush()
    club = Clubs(slug=f"race-club-{tag}", name="Race Club", user_id=owner.id, is_verified=True)
    s.add(club)
    await s.flush()
    return owner, club


async def make_event_in(s, club, owner):
    from app.api.events.models import EventCategories, Events

    cat = EventCategories(name="General", created_by_id=owner.id)
    s.add(cat)
    await s.flush()
    e = Events(slug=f"race-{uuid.uuid4().hex[:6]}", name="Race", images=[], event_datetime=NOW, duration=2.0, club_id=club.id, category_id=cat.id)
    s.add(e)
    await s.flush()
    return e


# ─── Codes ──────────────────────────────────────────────────────────────


async def test_codes_ignore_case_and_spaces_count_wrong_tries_and_keep_their_window(client, session, world):
    w = world
    code, later = await setup(
        client,
        w,
        milestones=(),
        code_attempts_per_hour=3,
        tasks=[
            {"kind": "code", "config": {"code": "Cloud 24"}},
            {"kind": "code", "title": "Closing code", "config": {"code": "BYE"}, "starts_at": (NOW + timedelta(hours=2)).isoformat()},
        ],
    )
    h = w["g"]["a"][2]
    wrong = await client.post(url(w, "/quest/code"), json={"code": "nope"}, headers=h)
    assert wrong.status_code == 404 and wrong.json()["error_code"] == "QUEST_CODE_WRONG" and wrong.json()["errors"]["left"] == 2
    not_yet = await client.post(url(w, "/quest/code"), json={"code": "bye"}, headers=h)
    assert not_yet.status_code == 409 and not_yet.json()["error_code"] == "QUEST_CODE_NOT_OPEN"
    done = await ok(await client.post(url(w, "/quest/code"), json={"code": "  cloud24 "}, headers=h))
    assert done["result"] == "DONE" and done["task"]["me"]["done"] is True and done["task"]["me"]["source"] == "code"
    assert (await ok(await client.post(url(w, "/quest/code"), json={"code": "CLOUD 24"}, headers=h)))["result"] == "ALREADY_DONE"
    second = await client.post(url(w, "/quest/code"), json={"code": "nope"}, headers=h)
    assert second.json()["errors"] == {"left": 1}
    last = await client.post(url(w, "/quest/code"), json={"code": "nope"}, headers=h)
    # The last try says when codes count again, as the lock that follows does.
    assert last.json()["errors"]["left"] == 0 and 3500 < last.json()["errors"]["retry_after"] <= 3601
    locked = await client.post(url(w, "/quest/code"), json={"code": "cloud24"}, headers=h)
    assert locked.status_code == 429 and locked.json()["error_code"] == "QUEST_CODE_LOCKED" and locked.json()["errors"]["retry_after"] > 0
    # The organiser may not give two code tasks one code; a guest never sees a code.
    clash = await client.post(url(w, "/quests/tasks"), json={"kind": "code", "config": {"code": "cloud24"}}, headers=w["club"])
    assert clash.status_code == 409 and clash.json()["error_code"] == "QUEST_CODE_TAKEN"
    assert "code" not in (await card(client, w, "b"))["tasks"][0]


async def test_guest_routes_need_a_registration(client, session, world, make_user, mint_token):
    w = world
    await setup(client, w, tasks=[{"kind": "code", "config": {"code": "abc"}}], milestones=())
    outsider = await make_user(email="q-out@example.com")
    h = auth_header(await mint_token(outsider))
    r = await client.post(url(w, "/quest/code"), json={"code": "abc"}, headers=h)
    assert r.status_code == 403 and r.json()["error_code"] == "QUEST_NOT_REGISTERED"
    c = await ok(await client.get(url(w, "/quest"), headers=h))
    assert c["enabled"] is True and c["me"] is None and c["tasks"][0]["me"] is None
    anon = await ok(await client.get(url(w, "/quest")))
    assert anon["me"] is None
    # Another club's account can't open the organiser's side.
    other, _ = await world_other_club(client, session)
    assert (await client.get(url(w, "/quests"), headers=other)).status_code in (403, 404)
    assert (await client.patch(url(w, "/quests"), json={"enabled": False}, headers=h)).status_code in (401, 403)


async def world_other_club(client, session):
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users, UserTypes
    from app.core.auth.jwt import create_access_token
    from app.api.auth.schemas import AuthTokenData

    u = Users(
        full_name="Other", username=f"other{uuid.uuid4().hex[:5]}", email=f"other{uuid.uuid4().hex[:5]}@example.com", password="x", user_type=UserTypes.club
    )
    session.add(u)
    await session.flush()
    session.add(Clubs(slug=f"other-{uuid.uuid4().hex[:5]}", name="Other", user_id=u.id, is_verified=True))
    await session.commit()
    await session.refresh(u)
    token = create_access_token(data=AuthTokenData(user_id=u.id, token_type="access_token", token_version=u.token_version).model_dump())
    return auth_header(token), u


# ─── Points, required tasks, leaderboard, export ────────────────────────


async def test_points_required_tasks_leaderboard_and_export(client, session, world):
    w = world
    must, extra = await setup(
        client,
        w,
        mode="points",
        leaderboard=True,
        milestones=((15, "sticker", "first_come"),),
        tasks=[
            {"kind": "manual", "title": "Required", "points": 5, "required": True},
            {"kind": "manual", "title": "Big", "points": 20},
        ],
    )
    ra, rb = w["g"]["a"][1], w["g"]["b"][1]
    grant = lambda r, t: client.post(url(w, f"/quests/guests/{r.id}/tasks/{t['id']}/grant"), json={}, headers=w["club"])  # noqa: E731
    await ok(await grant(ra, extra))
    c = await card(client, w, "a")
    assert c["me"]["points"] == 20 and c["me"]["required_left"] == 1 and not c["milestones"][0]["unlocked"]
    await ok(await grant(ra, must))
    await ok(await grant(rb, must))
    c = await card(client, w, "a")
    assert c["me"]["points"] == 25 and c["milestones"][0]["unlocked"] and c["me"]["rank"] == 1
    board = await ok(await client.get(url(w, "/quest/leaderboard"), headers=w["g"]["b"][2]))
    assert [r["name"] for r in board["rows"]] == ["Asha K.", "Ben T."] and board["me"] == {"rank": 2, "score": 5}
    full = await ok(await client.get(url(w, "/quests/leaderboard"), headers=w["club"]))
    assert full["rows"][0]["name"] == "Asha Kumar"
    await ok(await client.patch(url(w, "/quests"), json={"leaderboard": False}, headers=w["club"]))
    assert (await client.get(url(w, "/quest/leaderboard"), headers=w["g"]["b"][2])).json()["error_code"] == "QUEST_LEADERBOARD_OFF"
    csv = await client.get(url(w, "/quests/export"), headers=w["club"])
    assert csv.status_code == 200 and "Asha Kumar" in csv.text and "Required" in csv.text and "Sticker: status" in csv.text
    xlsx = await client.get(url(w, "/quests/export?format=xlsx"), headers=w["club"])
    assert xlsx.status_code == 200 and xlsx.headers["content-type"].startswith("application/vnd.openxmlformats")
    dash = await ok(await client.get(url(w, "/quests/dashboard"), headers=w["club"]))
    assert dash["participants"] == 2 and dash["milestones"][0]["unlocked"] == 1 and dash["rewards"][0]["reserved"] == 1
    guests = await ok(await client.get(url(w, "/quests/guests"), headers=w["club"]))
    assert [g["name"] for g in guests["items"]] == ["Asha Kumar", "Ben Thomas"]


async def test_entrances_answer_exactly_as_before_with_a_quest_on(client, session, world):
    w = world
    await setup(client, w, tasks=[{"kind": "checked_in"}], milestones=())
    await _station(client, w)
    r = await check_in(client, w, "a")
    assert r["outcome"] == "CHECKED_IN" and r["attendee"]["ticket_id"] == w["g"]["a"][1].ticket_id
    again = await ok(await client.post(url(w, "/gate/scan"), json=scan(w["g"]["a"][1].ticket_id, door="Gate B"), headers={**w["club"], **NEW}))
    assert again["outcome"] == "ALREADY_CHECKED_IN"
    assert await session.scalar(select(func.count()).select_from(CheckIns)) == 1
    # The last entrance can't be retired because a station is open.
    doors = await ok(await client.get(url(w, "/doors"), headers=w["club"]))
    gate = next(d for d in doors if d["purpose"] is None)
    retire = await client.patch(url(w, f"/doors/{gate['id']}"), json={"active": False}, headers=w["club"])
    assert retire.status_code == 409


async def test_entries_and_progress_rows_stay_unique_under_a_second_full_run(client, session, world):
    w = world
    await setup(client, w, tasks=[{"kind": "checked_in"}, {"kind": "follow_club"}], milestones=((1, "sticker", "waitlist"),))
    for k in "ab":
        await check_in(client, w, k)
    before = [await session.scalar(select(func.count()).select_from(m)) for m in (QuestProgress, QuestEntries, QuestUnlocks, QuestClaims)]
    await engine.evaluate(session, w["event"].id, everyone=True)
    await engine.evaluate(session, w["event"].id, everyone=True)
    after = [await session.scalar(select(func.count()).select_from(m)) for m in (QuestProgress, QuestEntries, QuestUnlocks, QuestClaims)]
    assert before == after == [2, 2, 2, 2]
    assert await session.scalar(select(func.count()).select_from(QuestAudit)) == 0
    assert await session.scalar(select(func.count()).select_from(Doors).where(Doors.purpose.isnot(None))) == 0


async def test_a_finished_profile_and_poster_registrations_count(client, session, world):
    from sqlalchemy import text

    from app.api.events.models import EventRegistrationsLink
    from app.api.guest_posters.models import GuestPosters
    from app.api.links.models import TrackingLinks
    from app.api.profiles.models import ProfileLinks, ProfileProjects
    from app.api.users.models import UserProfiles

    w = world
    profile, regs = await setup(
        client,
        w,
        milestones=(),
        tasks=[
            {"kind": "profile_complete"},
            {"kind": "poster_link", "config": {"metric": "registrations", "count": 1}},
        ],
    )
    a, ra = w["g"]["a"][0], w["g"]["a"][1]
    session.add(UserProfiles(user_id=a.id, full_name=a.full_name, headline="Builds things"))
    session.add_all([ProfileLinks(user_id=a.id, type="github", url="https://github.com/asha"), ProfileProjects(user_id=a.id, title="Quest app")])
    await session.flush()
    # The photo column uploads on write: set as stored, by key.
    await session.execute(text("UPDATE user_profiles SET profile_pic = 'users/profile_pic/x.jpg' WHERE user_id = :u"), {"u": a.id})
    tl = TrackingLinks(
        id=uuid.uuid4(),
        club_id=w["event"].club_id,
        event_id=w["event"].id,
        name="poster",
        code=f"qr{uuid.uuid4().hex[:6]}",
        kind="poster_guest",
        utm_source="poster",
        utm_medium="share",
    )
    session.add(tl)
    await session.flush()
    session.add(GuestPosters(event_id=w["event"].id, registration_id=ra.id, tracking_link_id=tl.id))
    rb = await session.get(EventRegistrationsLink, w["g"]["b"][1].id)
    rb.tracking_link_id = tl.id
    await session.commit()
    c = await card(client, w, "a")
    assert task_me(c, profile["id"])["done"] is True and task_me(c, regs["id"])["done"] is True


async def test_kiosks_and_the_team_roles_at_quest_doors(client, session, world):
    from app.api.checkins.gate_auth import GateActor
    from app.api.checkins.models import CheckInVia
    from app.api.club_access import policy
    from app.api.quests import gate as quest_gate

    w = world
    await setup(client, w, tasks=[], milestones=())
    st = await _station(client, w)
    door = await session.get(Doors, uuid.UUID(st["door"]["id"]))
    kiosk = GateActor(kind="door", name="Kiosk", door=door, can_check_in=True, features=frozenset({"quests"}))
    out = await quest_gate.scan_at(
        session, w["event"], door, code=w["g"]["a"][1].ticket_id, registration=None, client_ref=None, scanned_at=None, actor=kiosk, via=CheckInVia.kiosk
    )
    assert out["outcome"] == "INVALID" and out["hint"] == "QUEST_STATION"
    assert await session.scalar(select(func.count()).select_from(QuestStationVisits)) == 0
    p = "/api/v1/events/1/quests"
    assert policy.allows("editor", "PATCH", p) and policy.allows("door", "GET", p)
    assert not policy.allows("door", "PATCH", p) and not policy.allows("door", "POST", p + "/guests/x/tasks/y/grant")
    assert not policy.allows("door", "GET", p + "/export") and policy.allows("editor", "GET", p + "/export")
    assert policy.allows("door", "POST", "/api/v1/events/1/gate/quests/claims/x/handover")
    assert policy.allows("door", "POST", "/api/v1/events/1/gate/scan")
    # "Desk" is where check-ins by hand go: never a station's name.
    r = await client.post(url(w, "/quests/stations"), json={"name": "DESK", "purpose": "swag_desk"}, headers=w["club"])
    assert r.status_code == 422


async def test_the_five_minute_sweep_looks_at_follows_for_events_on_now(client, session, world):
    from app.api.clubs.models import ClubUsersLink
    from app.api.quests import sweep

    w = world
    (t,) = await setup(client, w, milestones=(), tasks=[{"kind": "follow_club"}])
    session.add(ClubUsersLink(club_id=w["event"].club_id, user_id=w["g"]["b"][0].id, is_following=True))
    await session.commit()
    assert w["event"].id in await sweep.due_events(session, datetime.now(timezone.utc))
    assert w["event"].id not in await sweep.due_events(session, datetime.now(timezone.utc) + timedelta(days=3))
    assert await sweep.sweep(session) >= 1
    p = await session.scalar(select(QuestProgress).where(QuestProgress.task_id == uuid.UUID(t["id"]), QuestProgress.registration_id == w["g"]["b"][1].id))
    assert p is not None and p.done_at is not None


async def test_the_event_says_whether_it_has_a_quest_and_reading_the_card_writes_nothing(client, session, world, make_user, mint_token):
    from app.api.quests import public as quests_public

    w = world
    info = await ok(await client.get(f"{API}/events/info/{w['event'].id}", headers=w["g"]["a"][2]))
    assert info["quest"] == {"enabled": False, "can_take_part": False}
    await setup(client, w, milestones=(), tasks=[{"kind": "follow_club"}])
    info = await ok(await client.get(f"{API}/events/info/{w['event'].id}", headers=w["g"]["a"][2]))
    assert info["quest"] == {"enabled": True, "can_take_part": True}
    outsider = await make_user(email="q-info@example.com")
    info = await ok(await client.get(f"{API}/events/info/{w['event'].id}", headers=auth_header(await mint_token(outsider))))
    assert info["quest"] == {"enabled": True, "can_take_part": False}
    assert await quests_public.flag(session, w["event"].id, per_viewer=False) == {"enabled": True, "can_take_part": None}
    # Opening the card with nothing done makes no row; the first progress does.
    await card(client, w, "c")
    assert not await session.scalar(select(func.count()).select_from(QuestEntries))
