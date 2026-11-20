"""The live room.

Five things happen while an event runs, and one just after:

* a quiz the host drives from the console, which the room plays on phones
  with a code off the screen;
* questions the floor asks and votes up, which the organiser lets through,
  pins to the screen and marks answered;
* polls and word clouds, one live at a time;
* updates pushed to phones and shown as a banner on the event page, some of
  them made by shifting the run of show;
* the feedback survey, sent to everyone who was there an hour after the end,
  each on the cheapest channel that reaches them -- a free push to whoever
  has the app, and the club's own choice of WhatsApp or email for the rest
  (send_survey, pick_channel).

Nobody in the room needs an account. A browser makes itself a key and sends
it on every call; that key is the voter, the asker and the player. A request
that carries a valid sign-in is the account instead (`u:<user id>`,
room.person_key): one vote, one poll answer and one quiz player per person,
whichever devices they use.

The quiz keeps its answer to itself while answers are open: the right
answer, a player's points and whether they were right, and every score, rank
and board that would give those away, are shown once the clock and its grace
are out (room.revealed). Joining again with the same key -- or as the same
account -- in the same run is the same player.

What the room polls is read from second-long snapshots (room.py). Counters
on a row (a question's votes, a player's score) move by one atomic UPDATE;
a vote, an answer and a poll response are an INSERT ... ON CONFLICT, so a
double tap is answered rather than a 500.

Every change is told on the event's channel once committed (bus.py), for the
room's streams (stream.py): the host's at once (room.room_changed and
quiz_changed), the audience's folded a third of a second.
"""
import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import delete, func, inspect, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.agenda.models import AgendaItems
from app.api.checkins.models import CheckIns
from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink, Events
from app.api.live import board, bus, names, notify, room, scoring
from app.api.live import settings as live_settings
from app.api.message_quota import service as quota
from app.api.live.models import (
    DEFAULT_CHANNELS,
    EMAIL,
    FEEDBACK_CHANNELS,
    PUSH,
    WHATSAPP,
    Audience,
    FeedbackResponses,
    FeedbackSends,
    FeedbackSurveys,
    LivePollResponses,
    LivePolls,
    LiveQuestions,
    LiveQuestionVotes,
    LiveQuizAnswers,
    LiveQuizPlayers,
    LiveQuizQuestions,
    LiveQuizzes,
    LiveUpdates,
    PollKind,
    PollStatus,
    QuestionStatus,
    QuizStatus,
    UpdateKind,
)
from app.config import settings
from app.core import jobs
from app.core.mail import links
from app.core.email import email as email_mod
from app.response import CustomHTTPException
from app.db import lock_keys

logger = logging.getLogger(__name__)

VOTER_HEADER = "X-Live-Key"
PLAYER_HEADER = "X-Live-Player"
SWEEP_LOCK = lock_keys.LIVE_SWEEP
# Answers after the clock plus this grace still count, for a slow phone.
ANSWER_GRACE = room.ANSWER_GRACE
UPDATE_TYPE = "event_live_update"
# A banner is the latest update, as long as it is this fresh.
BANNER_FOR = room.BANNER_FOR
#: Surveys one sweep sends at most; the rest wait a minute.
SWEEP_BATCH = 50

DEFAULT_SURVEY = [
    {"key": "overall", "kind": "rating", "label": "How was the event overall?"},
    {"key": "mentors", "kind": "rating", "label": "How useful were the mentors?"},
    {"key": "recommend", "kind": "nps", "label": "How likely are you to recommend us to a friend?"},
    {"key": "change", "kind": "text", "label": "What should we change next time?"},
]

_tz = ZoneInfo(EVENT_TIMEZONE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def voter_key_of(request, user=None) -> str:
    """Who votes, asks or answers: the account when the request carried a
    valid sign-in (`user`, OptionalAuth's), else the browser's key."""
    key = room.person_key(user, request.headers.get(VOTER_HEADER))
    if key is None:
        raise CustomHTTPException(422, message="This browser has no key for the room yet. Reload the page and try again.")
    return key


def _clock(dt: datetime) -> str:
    return dt.astimezone(_tz).strftime("%-I:%M %p")


public_event = room.public_event


# --- the audience ----------------------------------------------------------------------


async def audience_regs(session, event: Events, audience: Audience) -> list:
    q = select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)  # noqa: E712
    if audience is Audience.checked_in:
        q = q.join(CheckIns, CheckIns.registration_id == EventRegistrationsLink.id)
    return list((await session.scalars(q)).all())


async def audience_counts(session, event: Events) -> dict:
    registered = await session.scalar(select(func.count()).select_from(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.is_deleted == False)) or 0  # noqa: E712
    checked_in = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == event.id)) or 0
    return {"registered": registered, "checked_in": checked_in}


async def _registration_of(session, event: Events, user) -> Optional[EventRegistrationsLink]:
    if user is None:
        return None
    return await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == event.id, EventRegistrationsLink.user_id == user.id, EventRegistrationsLink.is_deleted == False))  # noqa: E712


# --- the quiz ----------------------------------------------------------------------
#
# An event holds rounds (live_quizzes, in sort_order), at most one live. The
# room shows its current round (room.current_round: the live one, else the
# one that ended last, else the first), and the console's `/quiz` endpoints
# act on the same one, so a console from before rounds still drives the
# event's only quiz. Each round's settings (app/api/live/settings.py) say how
# it scores and runs; with none set it is the quiz as it always was.

#: Rounds an event may hold, and questions a round.
ROUNDS_MAX = 20
QUESTIONS_MAX = 200
#: The line a client from before the question kinds shows for one it cannot answer.
NEEDS_UPDATE = "This kind of question needs an update: reload the page or update the app to answer it."


async def quiz_of(session, event: Events, create: bool = True) -> Optional[LiveQuizzes]:
    """The event's current round (room.current_round), made when it has none."""
    rows = await room.rounds_of(session, event.id, visible_only=False)
    picked = room.current_round(rows)
    quiz = await session.get(LiveQuizzes, picked[0]) if picked is not None else None
    if quiz is None and create:
        quiz = LiveQuizzes(event_id=event.id, title=f"{event.name} quiz", sort_order=0)
        session.add(quiz)
        await session.commit()
        await session.refresh(quiz)
    return quiz


async def round_of(session, event: Events, quiz_id: UUID) -> LiveQuizzes:
    quiz = await session.get(LiveQuizzes, quiz_id)
    if quiz is None or quiz.event_id != event.id:
        raise CustomHTTPException(404, message="That round is not in this event.", error_code="QUIZ_NOT_FOUND")
    return quiz


def _question_out(q, with_answer: bool) -> dict:
    get = q.get if isinstance(q, dict) else (lambda k: getattr(q, k))
    out = {"id": get("id"), "prompt": get("prompt"), "options": list(get("options") or []), "seconds": get("seconds"), "sort_order": get("sort_order")}
    if with_answer:
        out["correct_key"] = get("correct_key")
    return out


def question_admin(q) -> dict:
    """A question as the console sees it: today's fields, then its kind's."""
    f = live_settings.question_fields(q)
    out = _question_out(q, True)
    out["correct_key"] = f["correct_key"]
    st_points = f["points"]
    out.update(
        kind=f["kind"], correct_keys=f["correct_keys"], accepted=f["accepted"], match=f["match"], multi_rule=f["multi_rule"],
        order_rule=f["order_rule"], points=st_points, double=f["double"],
    )
    return out


def _with_effective(out: dict, settings_: dict) -> dict:
    out["effective_points"] = scoring.question_points(out.get("points"), int(settings_["scoring"]["points"]), bool(out.get("double")))
    return out


async def _player_count(session, quiz_id) -> int:
    return await session.scalar(select(func.count()).select_from(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz_id)) or 0


async def quiz_out(session, quiz: LiveQuizzes) -> dict:
    st = live_settings.quiz_settings(quiz.settings)
    rows = await room.rounds_of(session, quiz.event_id, visible_only=False)
    return {
        "id": quiz.id, "title": quiz.title, "intro": quiz.intro, "status": quiz.status.value, "join_code": quiz.join_code,
        "current_index": quiz.current_index, "players": await _player_count(session, quiz.id),
        "questions": [_with_effective(question_admin(q), st) for q in quiz.questions],
        "sort_order": quiz.sort_order, "settings": st,
        "round": {"index": next((i for i, r in enumerate(rows) if r[0] == quiz.id), 0), "count": len(rows)},
    }


def _changed_quiz(event_id: int) -> None:
    room.quiz_changed(event_id)
    room.room_changed(event_id)


async def update_quiz(session, quiz: LiveQuizzes, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"]:
        quiz.title = data["title"].strip()
    if "intro" in data:
        quiz.intro = (data["intro"] or "").strip() or None
    if "settings" in data and data["settings"] is not None:
        quiz.settings = live_settings.saved_change(quiz.settings, data["settings"])
    await session.commit()
    _changed_quiz(quiz.event_id)
    return await quiz_out(session, quiz)


# --- rounds ---------------------------------------------------------------------


def round_summary(quiz: LiveQuizzes, players: int) -> dict:
    return {
        "id": quiz.id, "title": quiz.title, "intro": quiz.intro, "status": quiz.status.value, "sort_order": quiz.sort_order,
        "questions": len(quiz.questions), "players": players, "join_code": quiz.join_code, "settings": live_settings.quiz_settings(quiz.settings),
    }


async def list_rounds(session, event: Events) -> list:
    quizzes = list((await session.scalars(select(LiveQuizzes).where(LiveQuizzes.event_id == event.id).order_by(LiveQuizzes.sort_order, LiveQuizzes.created_at))).all())
    counts = dict((await session.execute(
        select(LiveQuizPlayers.quiz_id, func.count()).where(LiveQuizPlayers.quiz_id.in_([q.id for q in quizzes])).group_by(LiveQuizPlayers.quiz_id)
    )).all()) if quizzes else {}
    return [round_summary(q, int(counts.get(q.id, 0))) for q in quizzes]


async def create_round(session, event: Events, body) -> dict:
    n = await session.scalar(select(func.count()).select_from(LiveQuizzes).where(LiveQuizzes.event_id == event.id)) or 0
    if n >= ROUNDS_MAX:
        raise CustomHTTPException(409, message=f"An event holds up to {ROUNDS_MAX} quiz rounds.", error_code="QUIZ_ROUNDS_FULL")
    top = await session.scalar(select(func.max(LiveQuizzes.sort_order)).where(LiveQuizzes.event_id == event.id))
    title = " ".join((body.title or "").split())[:160] or f"Round {n + 1}"
    quiz = LiveQuizzes(
        event_id=event.id, title=title, intro=(" ".join((body.intro or "").split()) or None), sort_order=(top if top is not None else -1) + 1,
        settings=live_settings.saved_change(None, body.settings) if body.settings else None,
    )
    session.add(quiz)
    await session.commit()
    await session.refresh(quiz)
    _changed_quiz(event.id)
    return await quiz_out(session, quiz)


async def reorder_rounds(session, event: Events, ids: list) -> list:
    quizzes = {q.id: q for q in (await session.scalars(select(LiveQuizzes).where(LiveQuizzes.event_id == event.id))).all()}
    if len(set(ids)) != len(ids) or set(ids) != set(quizzes):
        raise CustomHTTPException(422, message="List every round of this event once, in the new order.", error_code="QUIZ_ORDER")
    for i, qid in enumerate(ids):
        quizzes[qid].sort_order = i
    await session.commit()
    _changed_quiz(event.id)
    return await list_rounds(session, event)


async def delete_round(session, event: Events, quiz: LiveQuizzes) -> None:
    if quiz.status is QuizStatus.live:
        raise CustomHTTPException(409, message="End the round before deleting it.", error_code="QUIZ_RUNNING")
    await session.delete(quiz)
    await session.commit()
    _changed_quiz(event.id)


# --- questions ----------------------------------------------------------------------


def _question_body(body) -> dict:
    data = body.model_dump(exclude_unset=False) if hasattr(body, "model_dump") else dict(body)
    data["options"] = [o if isinstance(o, dict) else {"key": o.key, "label": o.label} for o in (data.get("options") or [])]
    return data


async def add_question(session, quiz: LiveQuizzes, body) -> dict:
    if len(quiz.questions) >= QUESTIONS_MAX:
        raise CustomHTTPException(409, message=f"A round holds up to {QUESTIONS_MAX} questions.", error_code="QUIZ_FULL")
    clean = live_settings.clean_question(_question_body(body))
    top = max((q.sort_order for q in quiz.questions), default=-1)
    row = LiveQuizQuestions(quiz_id=quiz.id, sort_order=top + 1, **clean)
    session.add(row)
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)
    return _with_effective(question_admin(row), live_settings.quiz_settings(quiz.settings))


async def _question_of(session, quiz: LiveQuizzes, question_id: UUID) -> LiveQuizQuestions:
    row = await session.get(LiveQuizQuestions, question_id)
    if row is None or row.quiz_id != quiz.id:
        raise CustomHTTPException(404, message="That question is not in this quiz.")
    return row


def _current_form(row: LiveQuizQuestions) -> dict:
    f = live_settings.question_fields(row)
    return {
        "kind": f["kind"], "prompt": row.prompt, "options": list(row.options or []), "correct_key": row.correct_key or None,
        "correct_keys": f["correct_keys"], "accepted": f["accepted"], "match": f["match"], "multi_rule": f["multi_rule"],
        "order_rule": f["order_rule"], "seconds": row.seconds, "points": f["points"], "double": f["double"],
    }


async def update_question(session, quiz: LiveQuizzes, question_id: UUID, body) -> dict:
    row = await _question_of(session, quiz, question_id)
    change = body.model_dump(exclude_unset=True)
    if "options" in change and change["options"] is not None:
        change["options"] = [o if isinstance(o, dict) else {"key": o["key"], "label": o["label"]} for o in change["options"]]
    data = {**_current_form(row), **{k: v for k, v in change.items() if v is not None or k in ("points",)}}
    if change.get("kind") and change["kind"] != (row.kind or scoring.SINGLE) and "options" not in change and change["kind"] == scoring.TYPE:
        data["options"] = []
    if (data.get("kind") or scoring.SINGLE) == scoring.TRUE_FALSE and "options" not in change and len(data.get("options") or []) != 2:
        data["options"] = None
    clean = live_settings.clean_question(data)
    for k, v in clean.items():
        setattr(row, k, v)
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)
    return _with_effective(question_admin(row), live_settings.quiz_settings(quiz.settings))


async def delete_question(session, quiz: LiveQuizzes, question_id: UUID) -> None:
    row = await _question_of(session, quiz, question_id)
    await session.delete(row)
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)


async def reorder_questions(session, quiz: LiveQuizzes, ids: list) -> dict:
    if quiz.status is QuizStatus.live:
        raise CustomHTTPException(409, message="The round is running; reorder it once it ends.", error_code="QUIZ_RUNNING")
    rows = {q.id: q for q in quiz.questions}
    if len(set(ids)) != len(ids) or set(ids) != set(rows):
        raise CustomHTTPException(422, message="List every question of the round once, in the new order.", error_code="QUESTION_ORDER")
    for i, qid in enumerate(ids):
        rows[qid].sort_order = i
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)
    return await quiz_out(session, quiz)


# --- running a round ----------------------------------------------------------------------


async def _fresh_code(session) -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(900_000) + 100_000}"
        if await session.scalar(select(LiveQuizzes.id).where(LiveQuizzes.join_code == code)) is None:
            return code
    raise CustomHTTPException(500, message="Could not make a join code; try again.")


async def _moved(session, quiz: LiveQuizzes) -> dict:
    """After the host moved the quiz: the room's snapshots are forgotten, and the host sees it as it is now."""
    room.quiz_changed(quiz.event_id)
    room.room_changed(quiz.event_id)
    return await host_state(session, quiz)


_RUN = ["status", "current_index", "question_opened_at", "ended_at", "join_code", "paused_at", "revealed_at", "asked", "settings"]


async def _hold(session, quiz: LiveQuizzes) -> None:
    """The quiz's row read again and held until this transaction ends: a
    second press (or a second console) waits here, then sees what the first
    one did."""
    await session.refresh(quiz, attribute_names=_RUN, with_for_update=True)


def _clear_run(quiz: LiveQuizzes) -> None:
    quiz.question_opened_at, quiz.paused_at, quiz.revealed_at = None, None, None


async def start_quiz(session, quiz: LiveQuizzes, restart: bool = False) -> dict:
    """Opens the lobby. A quiz run before starts clean: last time's players
    and answers go. A quiz that is running is started again only when asked
    to (`restart`): it clears every player's score, so a stray press of
    Start mid-game is 409 QUIZ_RUNNING instead. Another round of the event
    live is 409 QUIZ_OTHER_LIVE: one at a time."""
    if not quiz.questions:
        raise CustomHTTPException(422, message="Add at least one question before hosting.")
    await _hold(session, quiz)
    if quiz.status is QuizStatus.live and not restart:
        await session.rollback()
        raise CustomHTTPException(
            409, message="The quiz is running. Restarting it clears every player's score.", error_code="QUIZ_RUNNING",
        )
    other = await session.scalar(select(LiveQuizzes.id).where(LiveQuizzes.event_id == quiz.event_id, LiveQuizzes.status == QuizStatus.live, LiveQuizzes.id != quiz.id))
    if other is not None:
        await session.rollback()
        raise _other_live(other)
    await session.execute(delete(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))
    quiz.status, quiz.current_index, quiz.ended_at, quiz.asked = QuizStatus.live, -1, None, []
    _clear_run(quiz)
    quiz.join_code = await _fresh_code(session)
    try:
        await session.commit()
    except IntegrityError:
        # Two consoles started two rounds at once: the database kept one.
        await session.rollback()
        other = await session.scalar(select(LiveQuizzes.id).where(LiveQuizzes.event_id == quiz.event_id, LiveQuizzes.status == QuizStatus.live))
        raise _other_live(other)
    return await _moved(session, quiz)


def _other_live(other) -> CustomHTTPException:
    return CustomHTTPException(409, message="Another round of this event is running. End it first.", error_code="QUIZ_OTHER_LIVE", errors={"quiz_id": str(other) if other else None})


def _moved_on() -> CustomHTTPException:
    return CustomHTTPException(409, message="The quiz has already moved on.", error_code="QUIZ_MOVED_ON")


def _asked_with(quiz: LiveQuizzes, question_id) -> list:
    asked = [str(a) for a in (quiz.asked or [])]
    qid = str(question_id)
    return asked if qid in asked else [*asked, qid]


async def _open(session, quiz: LiveQuizzes, index: int) -> dict:
    quiz.current_index = index
    quiz.question_opened_at = now_utc()
    quiz.paused_at, quiz.revealed_at = None, None
    quiz.asked = _asked_with(quiz, quiz.questions[index].id)
    await session.commit()
    return await _moved(session, quiz)


async def next_question(session, quiz: LiveQuizzes, expected_index: Optional[int] = None) -> dict:
    """The next question (past the last one, the end). `expected_index`, the
    question the console shows: when the quiz is no longer there -- a double
    press, a second console -- nothing moves and it is 409 QUIZ_MOVED_ON."""
    await _hold(session, quiz)
    if quiz.status is not QuizStatus.live:
        await session.rollback()
        if expected_index is not None:
            raise _moved_on()
        raise CustomHTTPException(409, message="The quiz is not running.")
    if expected_index is not None and quiz.current_index != expected_index:
        await session.rollback()
        raise _moved_on()
    if quiz.current_index + 1 >= len(quiz.questions):
        return await end_quiz(session, quiz)
    return await _open(session, quiz, quiz.current_index + 1)


async def end_quiz(session, quiz: LiveQuizzes) -> dict:
    quiz.status, quiz.ended_at = QuizStatus.ended, now_utc()
    _clear_run(quiz)
    quiz.join_code = None
    await session.commit()
    return await _moved(session, quiz)


async def reset_quiz(session, quiz: LiveQuizzes) -> dict:
    """Back to a draft: questions stay, the run is forgotten."""
    await session.execute(delete(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))
    quiz.status, quiz.current_index, quiz.ended_at, quiz.join_code, quiz.asked = QuizStatus.draft, -1, None, None, None
    _clear_run(quiz)
    await session.commit()
    return await _moved(session, quiz)


def _open_question(quiz: LiveQuizzes):
    if quiz.status is QuizStatus.live and 0 <= quiz.current_index < len(quiz.questions):
        return quiz.questions[quiz.current_index]
    return None


async def _held_open(session, quiz: LiveQuizzes):
    await _hold(session, quiz)
    q = _open_question(quiz)
    if q is None or quiz.question_opened_at is None:
        await session.rollback()
        raise CustomHTTPException(409, message="No question is open right now.", error_code="QUIZ_NOT_OPEN")
    return q


async def reveal_question(session, quiz: LiveQuizzes) -> dict:
    """The host shows the open question's answer (a round revealed by the host), once its answers closed."""
    q = await _held_open(session, quiz)
    now = now_utc()
    if quiz.paused_at is not None or not room.revealed(quiz.question_opened_at, q.seconds, now):
        await session.rollback()
        raise CustomHTTPException(409, message="Answers are still open; reveal once the time is up.", error_code="QUIZ_NOT_CLOSED")
    if quiz.revealed_at is not None:
        await session.rollback()
        raise CustomHTTPException(409, message="This question is already revealed.", error_code="QUIZ_ALREADY_REVEALED")
    quiz.revealed_at = now
    await session.commit()
    return await _moved(session, quiz)


async def pause_question(session, quiz: LiveQuizzes) -> dict:
    q = await _held_open(session, quiz)
    now = now_utc()
    if quiz.paused_at is not None or room.revealed(quiz.question_opened_at, q.seconds, now):
        await session.rollback()
        raise CustomHTTPException(409, message="Only a question whose time is running can be paused.", error_code="QUIZ_NOT_OPEN")
    quiz.paused_at = now
    await session.commit()
    return await _moved(session, quiz)


async def resume_question(session, quiz: LiveQuizzes) -> dict:
    await _held_open(session, quiz)
    if quiz.paused_at is None:
        await session.rollback()
        raise CustomHTTPException(409, message="The question is not paused.", error_code="QUIZ_NOT_PAUSED")
    # The clock carries on from where it stopped.
    quiz.question_opened_at = quiz.question_opened_at + (now_utc() - quiz.paused_at)
    quiz.paused_at = None
    await session.commit()
    return await _moved(session, quiz)


async def skip_question(session, quiz: LiveQuizzes) -> dict:
    """The open question voided: its answers removed and every point they
    gave (streak bonuses too) taken back, each player's saved time and
    streak as they were before it; then on to the next one."""
    q = await _held_open(session, quiz)
    qid = q.id
    rows = (await session.execute(
        select(LiveQuizAnswers.player_id, LiveQuizAnswers.points, LiveQuizAnswers.response).where(LiveQuizAnswers.question_id == qid)
    )).all()
    for pid, points, resp in rows:
        resp = resp or {}
        values = {"score": LiveQuizPlayers.score - int(points or 0), "saved_ms": LiveQuizPlayers.saved_ms - int(resp.get("saved_ms") or 0)}
        if "streak_before" in resp:
            values["streak"] = int(resp.get("streak_before") or 0)
            values["streak_at"] = UUID(resp["streak_at_before"]) if resp.get("streak_at_before") else None
        await session.execute(update(LiveQuizPlayers).where(LiveQuizPlayers.id == pid).values(**values))
    await session.execute(delete(LiveQuizAnswers).where(LiveQuizAnswers.question_id == qid))
    quiz.asked = [a for a in (quiz.asked or []) if str(a) != str(qid)]
    k = room.board_run(quiz, live_settings.quiz_settings(quiz.settings))
    if quiz.current_index + 1 >= len(quiz.questions):
        out = await end_quiz(session, quiz)
    else:
        out = await _open(session, quiz, quiz.current_index + 1)
    if k is not None and board.on():
        # Scores went down: the run's set is built again from the database.
        await board.drop(board.key(quiz.id, k))
    return out


async def reopen_question(session, quiz: LiveQuizzes, index: Optional[int] = None) -> dict:
    """Question `index` (the current one by default) open again with a fresh
    clock; whoever answered it keeps their answer."""
    await _hold(session, quiz)
    if quiz.status is not QuizStatus.live:
        await session.rollback()
        raise CustomHTTPException(409, message="The quiz is not running.")
    i = quiz.current_index if index is None else index
    if not (0 <= i < len(quiz.questions)):
        await session.rollback()
        raise CustomHTTPException(422, message="There is no question there.", error_code="QUESTION_INVALID")
    return await _open(session, quiz, i)


async def advance_if_due(session, quiz: LiveQuizzes, now: Optional[datetime] = None) -> bool:
    """An auto-advancing round's next question, once its reveal has been up
    `auto_seconds`: whoever reads the round then moves it on (a second reader
    finds it moved). True when it moved."""
    st = live_settings.quiz_settings(quiz.settings)
    q = _open_question(quiz)
    if q is None or st["flow"]["advance"] != "auto" or quiz.paused_at is not None or quiz.question_opened_at is None:
        return False
    now = now or now_utc()
    shown = quiz.revealed_at if st["flow"]["reveal"] == "host" else room.reveal_at(quiz.question_opened_at, q.seconds)
    if shown is None or now < shown + timedelta(seconds=int(st["flow"]["auto_seconds"])):
        return False
    index = quiz.current_index
    await _hold(session, quiz)
    if quiz.status is not QuizStatus.live or quiz.current_index != index:
        await session.rollback()
        return True
    if index + 1 >= len(quiz.questions):
        quiz.status, quiz.ended_at, quiz.join_code = QuizStatus.ended, now, None
        _clear_run(quiz)
    else:
        quiz.current_index = index + 1
        quiz.question_opened_at, quiz.paused_at, quiz.revealed_at = now, None, None
        quiz.asked = _asked_with(quiz, quiz.questions[index + 1].id)
    await session.commit()
    room.quizzes.forget(("quiz", quiz.event_id))
    bus.changed(quiz.event_id, "quiz", now=True)
    bus.changed(quiz.event_id, "room", now=True)
    room.forget_room(quiz.event_id)
    return True


def _time_left(opened_at: Optional[datetime], seconds: int, now: datetime) -> int:
    if opened_at is None:
        return seconds
    return max(0, int(seconds - (now - opened_at).total_seconds()))


async def _tally(session, q: dict) -> dict:
    """The open question's answers, counted for the host by its kind."""
    kind = q.get("kind") or scoring.SINGLE
    if kind in scoring.ONE_KEY:
        by = dict((await session.execute(select(LiveQuizAnswers.option_key, func.count()).where(LiveQuizAnswers.question_id == q["id"]).group_by(LiveQuizAnswers.option_key))).all())
        out: dict = {"answered": sum(by.values())}
        out["counts"] = [{"key": o["key"], "label": o["label"], "count": by.get(o["key"], 0), "correct": kind != scoring.POLL and o["key"] == q["correct_key"]} for o in q["options"]]
        return out
    answered, right = (await session.execute(
        select(func.count(), func.count().filter(LiveQuizAnswers.correct == True)).where(LiveQuizAnswers.question_id == q["id"])  # noqa: E712
    )).one()
    out = {"answered": int(answered), "fully_correct": int(right)}
    st = q.get("settings") or {}
    if kind == scoring.TYPE:
        out["counts"] = []
        rows = (await session.execute(_TEXT_TALLY, {"q": q["id"]})).all()
        out["texts"] = [{"text": t, "count": int(n), "correct": bool(ok)} for t, n, ok in rows]
        return out
    field_ = "option_keys" if kind == scoring.MULTI else "placed"
    by = dict((await session.execute(_KEY_TALLY.bindparams(field=field_), {"q": q["id"]})).all())
    correct = set(st.get("correct_keys") or []) if kind == scoring.MULTI else {o["key"] for o in q["options"]}
    out["counts"] = [{"key": o["key"], "label": o["label"], "count": int(by.get(o["key"], 0)), "correct": o["key"] in correct} for o in q["options"]]
    return out


#: How many players picked each key (multi), or placed it right (order), from the answers' JSON.
_KEY_TALLY = sa.text(
    "SELECT k, count(*) FROM live_quiz_answers a, json_array_elements_text(a.response -> :field) AS k "
    "WHERE a.question_id = :q GROUP BY k"
)
#: A typed question's ten most given answers (normalised), and whether each was right.
_TEXT_TALLY = sa.text(
    "SELECT a.response ->> 'norm' AS t, count(*) AS n, bool_or(a.correct) FROM live_quiz_answers a "
    "WHERE a.question_id = :q AND a.response ->> 'norm' IS NOT NULL GROUP BY t ORDER BY n DESC, t LIMIT 10"
)


def _order(st: dict) -> tuple:
    if st["ties"] == "time":
        return (LiveQuizPlayers.score.desc(), LiveQuizPlayers.saved_ms.desc(), LiveQuizPlayers.created_at)
    return (LiveQuizPlayers.score.desc(), LiveQuizPlayers.created_at)


async def _leaderboard(session, quiz: LiveQuizzes, limit: int = 10) -> list:
    """The top of the board, read through ix_live_quiz_players_rank (for a quiz the room cannot see, a draft's)."""
    rows = (await session.execute(
        select(LiveQuizPlayers.name, LiveQuizPlayers.score).where(LiveQuizPlayers.quiz_id == quiz.id)
        .order_by(*_order(live_settings.quiz_settings(quiz.settings))).limit(limit)
    )).all()
    return [{"rank": i + 1, "name": name, "score": score} for i, (name, score) in enumerate(rows)]


async def view_of(session, quiz: LiveQuizzes) -> "room.QuizView":
    """The round's snapshot: the room's when it is the event's current round, else made now."""
    view = await room.quiz_view(session, quiz.event_id)
    if view is None or view.quiz_id != quiz.id:
        view = await room.build_view(session, quiz)
    return view


def _hide_answer(qd: dict) -> None:
    """The big screen before the players are told: nothing gives the answer away."""
    qd["correct_key"] = None
    for k in ("correct_keys", "accepted", "correct_order", "texts"):
        if k in qd:
            qd[k] = None
    if qd.get("kind") == scoring.ORDER:
        qd["counts"] = [{"key": o["key"], "label": o["label"], "count": 0, "correct": False} for o in qd["options"]]
    else:
        qd["counts"] = [{**c, "correct": False} for c in qd.get("counts") or []]


async def host_state(session, quiz: LiveQuizzes, now: Optional[datetime] = None, *, for_screen: bool = False, hide_results: bool = False) -> dict:
    """What the host (and the big screen) sees: players and the board from
    the round's snapshot, the open question's tally counted afresh. The big
    screen (`for_screen`) is in front of the room: it is not told the right
    answer until the players are (QuizView.is_revealed), and shows names as
    tags when the round says so."""
    now = now or now_utc()
    view = await view_of(session, quiz)
    st = view.settings
    current = view.current()
    anonymous = for_screen and st["players"]["anonymous_screen"]
    out = {
        "status": view.status.value, "join_code": view.join_code, "title": view.title,
        "players": view.player_count,
        "current_index": view.current_index, "total": len(view.questions), "question": None,
        "leaderboard": room.board_out(view, anonymous=anonymous),
        "quiz_id": view.quiz_id, "round": {"index": view.round[0], "count": view.round[1]}, "phase": view.phase(now),
        "paused": view.paused_at is not None, "board_now": view.board_now(now), "next_at": view.next_at(), "settings": st,
        "teams": view.teams, "overall": None,
    }
    if current is not None:
        clock = view.clock_now(now)
        f = live_settings.question_fields(current)
        shown = view.is_revealed(current, now)
        tally = await _tally(session, current)
        qd = {
            **_question_out(current, True), "time_left": _time_left(view.opened_at, current["seconds"], clock), "opened_at": view.opened_at,
            **tally,
            "kind": f["kind"], "correct_keys": f["correct_keys"], "accepted": f["accepted"],
            "correct_order": [o["key"] for o in current["options"]] if f["kind"] == scoring.ORDER else None,
            "double": f["double"], "points": f["points"], "texts": None, "fully_correct": None,
            "reveal_at": room.reveal_at(view.opened_at, current["seconds"]) if view.opened_at else None, "revealed": shown,
        }
        qd.update({k: v for k, v in tally.items() if k in ("texts", "fully_correct")})
        qd["correct_key"] = f["correct_key"]
        _with_effective(qd, st)
        if for_screen:
            qd["options"] = list(current["shown"])
            if not shown:
                _hide_answer(qd)
            if hide_results and not shown:
                qd["counts"] = [{**c, "count": 0} for c in qd["counts"]]
                qd["texts"] = None
        out["question"] = qd
    if view.status is QuizStatus.ended and view.round[1] >= 2:
        out["overall"] = await overall(session, quiz.event_id, top=10, anonymous=anonymous)
    return out


# --- joining ----------------------------------------------------------------------


def player_key_for(quiz: LiveQuizzes, person: str) -> str:
    """The player's secret for one person (`u:<id>`, or a client's key) in
    one run of a quiz: the same every time they join it, and nobody's to work
    out without the server's key. The run is its join code (a fresh one each
    start)."""
    message = f"live-player:{quiz.id}:{quiz.join_code}:{person}"
    return hmac.new(settings.SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()[:48]


def person_for(event_id: int, person: str) -> str:
    """Who joined, the same across an event's rounds (the overall board), and
    not the key itself."""
    return hmac.new(settings.SECRET_KEY.encode(), f"live-person:{event_id}:{person}".encode(), hashlib.sha256).hexdigest()[:40]


def _team_out(st: dict, key: Optional[str]) -> Optional[dict]:
    for t in st["teams"]["list"]:
        if t["key"] == key:
            return dict(t)
    return None


def _joined(quiz: LiveQuizzes, player_id, player_key: str, name: str, team: Optional[str] = None) -> dict:
    st = live_settings.quiz_settings(quiz.settings)
    return {
        "event_id": quiz.event_id, "quiz_id": quiz.id, "title": quiz.title, "player_id": player_id, "player_key": player_key, "name": name,
        "team": _team_out(st, team) if st["teams"]["mode"] != "off" else None, "tag": scoring.tag_of(player_id),
    }


async def lookup_quiz(session, code: str) -> dict:
    """The round running with `code`, as a guest sees it before joining: its
    title, its teams (for a team picker) and the rules a join meets. The code
    off the screen is the proof of being in the room, as for joining."""
    quiz = await session.scalar(
        select(LiveQuizzes).join(Events, Events.id == LiveQuizzes.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(LiveQuizzes.join_code == code.strip(), LiveQuizzes.status == QuizStatus.live, *room.visible())
    )
    if quiz is None:
        raise CustomHTTPException(404, message="No quiz is running with that code. Check the screen and try again.")
    st = live_settings.quiz_settings(quiz.settings)
    teams = st["teams"]
    return {
        "event_id": quiz.event_id, "quiz_id": quiz.id, "title": quiz.title, "started": quiz.current_index >= 0,
        "teams": {"mode": teams["mode"], "score": teams["score"], "list": [dict(t) for t in teams["list"]] if teams["mode"] != "off" else []},
        "players": {"names": st["players"]["names"], "late_join": st["players"]["late_join"]},
    }


async def join_quiz(session, code: str, name: str, registration_id=None, person: Optional[str] = None, team: Optional[str] = None, user=None) -> dict:
    """Into the quiz running with `code`. `person` -- the account
    (`u:<id>`) or the client's own key -- makes it the same player however
    many times they join this run, from whichever device: the first join's
    name and score. Without one (an older client) each join is a new player,
    as before; the limits bound how many. The round's player rules (its
    names, late joining, one player per ticket, teams) apply to a new player;
    one already in always gets back in."""
    quiz = await session.scalar(
        select(LiveQuizzes).join(Events, Events.id == LiveQuizzes.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(LiveQuizzes.join_code == code.strip(), LiveQuizzes.status == QuizStatus.live, *room.visible())
    )
    if quiz is None:
        raise CustomHTTPException(404, message="No quiz is running with that code. Check the screen and try again.")
    st = live_settings.quiz_settings(quiz.settings)
    rules = st["players"]
    reg = None
    if user is not None:
        reg = await session.scalar(select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id == quiz.event_id, EventRegistrationsLink.user_id == user.id, EventRegistrationsLink.is_deleted == False,  # noqa: E712
        ))
    if reg is not None and registration_id is None:
        registration_id = reg.id
    key = player_key_for(quiz, person) if person else secrets.token_urlsafe(24)
    if person:
        found = (await session.execute(
            select(LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.registration_id, LiveQuizPlayers.team)
            .where(LiveQuizPlayers.quiz_id == quiz.id, LiveQuizPlayers.player_key == key)
        )).first()
        if found is not None:
            if registration_id is not None and found[2] is None:
                await session.execute(update(LiveQuizPlayers).where(LiveQuizPlayers.id == found[0]).values(registration_id=registration_id))
            await session.commit()
            return _joined(quiz, found[0], key, found[1], found[3])
    if rules["names"] == "registered":
        if reg is None:
            raise CustomHTTPException(403, message="This quiz is for registered guests: sign in with the account you registered with.", error_code="QUIZ_REGISTERED_ONLY")
        name = reg.full_name or name
    name = " ".join(name.split())[:60]
    if not name:
        raise CustomHTTPException(422, message="Tell the room your name.")
    if rules["filter_names"] and names.refused(name):
        raise CustomHTTPException(422, message="Pick another name.", error_code="QUIZ_NAME_REFUSED")
    if not rules["late_join"] and quiz.current_index >= 0:
        raise CustomHTTPException(409, message="This quiz has started; new players can't join now.", error_code="QUIZ_LATE")
    if rules["one_per_ticket"] and registration_id is not None:
        taken = await session.scalar(select(LiveQuizPlayers.id).where(LiveQuizPlayers.quiz_id == quiz.id, LiveQuizPlayers.registration_id == registration_id))
        if taken is not None:
            raise CustomHTTPException(409, message="Your ticket is already playing on another device.", error_code="QUIZ_TICKET_IN_USE")
    team_key = None
    teams = st["teams"]
    if teams["mode"] != "off" and teams["list"]:
        keys = [t["key"] for t in teams["list"]]
        if teams["mode"] == "pick" and team:
            if team not in keys:
                raise CustomHTTPException(422, message="Pick one of the quiz's teams.", error_code="QUIZ_TEAM_UNKNOWN")
            team_key = team
        else:
            counts = dict((await session.execute(
                select(LiveQuizPlayers.team, func.count()).where(LiveQuizPlayers.quiz_id == quiz.id).group_by(LiveQuizPlayers.team)
            )).all())
            team_key = scoring.pick_team(teams["list"], counts)
    # Two taps at once with the same key are one player: the second insert
    # takes nothing and reads the first's row.
    took = (await session.execute(
        pg_insert(LiveQuizPlayers)
        .values(
            id=uuid4(), quiz_id=quiz.id, name=name, player_key=key, registration_id=registration_id, score=0, created_at=now_utc(), updated_at=now_utc(),
            person=person_for(quiz.event_id, person) if person else None, team=team_key, saved_ms=0, streak=0,
        )
        .on_conflict_do_nothing(index_elements=["quiz_id", "player_key"])
        .returning(LiveQuizPlayers.id, LiveQuizPlayers.created_at)
    )).first()
    if took is None:
        row = (await session.execute(
            select(LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.team).where(LiveQuizPlayers.quiz_id == quiz.id, LiveQuizPlayers.player_key == key)
        )).one()
        await session.commit()
        return _joined(quiz, row[0], key, row[1], row[2])
    await session.commit()
    met = room.Player(id=took[0], name=name, score=0, created_at=took[1], team=team_key)
    # In the snapshot at once, so the new player's first poll finds them.
    view = room.quiz_kept(quiz.event_id)
    if view is not None and view.quiz_id == quiz.id:
        view.players[key] = met
    run = room.board_run(quiz, st)
    if run is not None and board.on():
        await board.joined(board.key(quiz.id, run), met.member())
    bus.changed(quiz.event_id, "quiz")
    return _joined(quiz, met.id, key, name, team_key)


async def _player(session, view: "room.QuizView", player_key: str) -> "room.Player":
    """The player behind a key: from the snapshot, or (joined since it was
    made, or on another worker) one probe of the unique key. A key of an
    earlier round of the event while another runs: 401 QUIZ_NEW_ROUND, with
    the new round's code to join again with."""
    p = view.players.get(player_key or "")
    if p is not None:
        return p
    row = (await session.execute(
        select(LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.score, LiveQuizPlayers.created_at, LiveQuizPlayers.team, LiveQuizPlayers.saved_ms, LiveQuizPlayers.streak)
        .where(LiveQuizPlayers.quiz_id == view.quiz_id, LiveQuizPlayers.player_key == (player_key or ""))
    )).first()
    if row is None:
        if player_key and view.status is QuizStatus.live and view.join_code:
            earlier = await session.scalar(
                select(LiveQuizPlayers.id).join(LiveQuizzes, LiveQuizzes.id == LiveQuizPlayers.quiz_id)
                .where(LiveQuizPlayers.player_key == player_key, LiveQuizzes.event_id == view.event_id, LiveQuizzes.id != view.quiz_id)
            )
            if earlier is not None:
                raise CustomHTTPException(
                    401, message="A new round has started. Join again with the code on the screen.", error_code="QUIZ_NEW_ROUND",
                    errors={"join_code": view.join_code, "quiz_id": str(view.quiz_id), "title": view.title},
                )
        raise CustomHTTPException(401, message="You are not in this quiz. Join again with the code on the screen.")
    p = room.Player(id=row[0], name=row[1], score=row[2], created_at=row[3], team=row[4], saved_ms=int(row[5] or 0), streak=int(row[6] or 0))
    view.players[player_key] = p
    return p


async def _view_for(session, event_id: int) -> "room.QuizView":
    view = await room.quiz_view(session, event_id)
    if view is None:
        raise CustomHTTPException(404, message="This event has no quiz.")
    return view


async def player_state(session, event_id: int, player_key: str, now: Optional[datetime] = None, v2: bool = False) -> dict:
    """A player's poll: read from the quiz snapshot, no statement unless the
    player is new to it (with the board in Redis, one round trip there for
    their score and rank)."""
    view = await _view_for(session, event_id)
    player = await _player(session, view, player_key)
    standing = (await room.standings(session, view, [player]))[player.id]
    return player_body(view, player, standing, now or now_utc(), v2=v2)


def _payload(response: Optional[dict]) -> Optional[dict]:
    if not response:
        return None
    return {k: response[k] for k in ("option_key", "option_keys", "text", "order") if k in response} or None


def player_body(view: "room.QuizView", player: "room.Player", standing: tuple, now: datetime, v2: bool = False) -> dict:
    """What a player sees (PlayerState), from the snapshot and their
    (score, rank): the poll's body, and the quiz stream's.

    While the open question's answers are open (and, with the host revealing,
    until the host does), nothing gives its answer away: no right answer,
    the player's own answer without its points or verdict, their score as it
    stood before it, and no rank or board -- a second phone that answered
    first would read the answer off any of them. Once revealed, all of it;
    the board and the rank as the round's `flow.board` and the theme's
    `own_rank` say.

    `v2`: a client that knows the question kinds and the round's settings
    (X-Live-Version: 2). Without it the body is exactly the one there always
    was: a kind it cannot answer comes as its prompt with a line asking to
    update, and no answers to tap."""
    score, rank = standing
    st = view.settings
    out = {"status": view.status.value, "title": view.title, "name": player.name, "score": score, "current_index": view.current_index, "total": len(view.questions), "question": None, "leaderboard": [], "rank": None}
    q = view.current()
    shown = True
    mine = None
    if q is not None:
        kind = q.get("kind") or scoring.SINGLE
        left = _time_left(view.opened_at, q["seconds"], view.clock_now(now))
        shown = view.is_revealed(q, now)
        mine = view.answers.get(player.id)
        right_key = q["correct_key"] if kind in (scoring.SINGLE, scoring.TRUE_FALSE) else None
        one_key = kind in scoring.ONE_KEY
        question = {
            "id": q["id"], "prompt": q["prompt"], "options": list(q["shown"]), "seconds": q["seconds"], "sort_order": q["sort_order"],
            "time_left": left,
            "my_answer": mine.option_key if (mine and one_key) else None,
            "my_points": mine.points if (mine and shown) else None,
            "my_correct": (bool(mine.correct) if kind != scoring.POLL else None) if (mine and shown) else None,
            "correct_key": right_key if shown else None,
        }
        if not v2 and kind in scoring.NEEDS_UPDATE:
            question.update(prompt=f"{q['prompt']}\n\n{NEEDS_UPDATE}", options=[], my_answer=None, my_points=None, my_correct=None, correct_key=None)
        if v2:
            f = live_settings.question_fields(q)
            question.update(
                kind=kind, double=f["double"], effective_points=scoring.question_points(f["points"], int(st["scoring"]["points"]), f["double"]),
                my_response=_payload(mine.response) if mine and mine.response else ({"option_key": mine.option_key} if mine and one_key else None),
                my_credit=(mine.credit if mine.credit is not None else (1.0 if mine.correct else 0.0)) if (mine and shown and kind != scoring.POLL) else None,
                correct_keys=f["correct_keys"] if shown else None,
                accepted=f["accepted"] if shown else None,
                correct_order=[o["key"] for o in q["options"]] if (shown and kind == scoring.ORDER) else None,
                counts=view.counts() if (shown and kind == scoring.POLL) else None,
            )
        out["question"] = question
        if mine and not shown:
            before = int(score) - int(mine.points or 0)
            out["score"] = before if st["scoring"]["allow_negative"] else max(0, before)
    board_now = view.board_now(now)
    phase = view.phase(now)
    if board_now:
        out["leaderboard"] = room.board_out(view)
    if phase in ("revealed", "ended") and shown and (board_now or view.own_rank):
        out["rank"] = rank
    if v2:
        out.update(
            quiz_id=view.quiz_id, round={"index": view.round[0], "count": view.round[1]}, phase=phase, paused=view.paused_at is not None,
            board_now=board_now, next_at=view.next_at(), team=_team_out(st, player.team) if st["teams"]["mode"] != "off" else None,
            teams=view.teams if board_now else None, tag=scoring.tag_of(player.id), streak=player.streak, theme_version=view.theme_version,
            ties=st["ties"], anonymous_screen=bool(st["players"]["anonymous_screen"]),
            overall=view.overall if phase == "ended" else None,
        )
    return out


# --- answering ----------------------------------------------------------------------


async def answer(session, event_id: int, player_key: str, body, now: Optional[datetime] = None) -> dict:
    """One answer to the open question, scored once (CONTRACT section 4).
    `body`: {"option_key"} as always, or the kind's own field."""
    now = now or now_utc()
    if isinstance(body, str):
        body = {"option_key": body}
    view = await room.quiz_view(session, event_id)
    q = view.current() if view is not None else None
    if q is None:
        raise CustomHTTPException(409, message="No question is open right now.")
    player = await _player(session, view, player_key)
    if view.paused_at is not None:
        raise CustomHTTPException(409, message="The host has paused this question. Wait a moment.", error_code="QUIZ_PAUSED")
    response = live_settings.check_response(q, body)
    elapsed = (now - view.opened_at).total_seconds() if view.opened_at else 0.0
    if elapsed > q["seconds"] + ANSWER_GRACE:
        raise CustomHTTPException(409, message="Time is up for this one.")
    if player.id in view.answers:
        raise CustomHTTPException(409, message="You already answered this one.")
    st = view.settings
    sc = st["scoring"]
    kind = q.get("kind") or scoring.SINGLE
    careful = float(sc.get("wrong_penalty") or 0) > 0 or int(sc.get("streak_from") or 0) >= 2
    streak_before, streak_at_before, total_before = int(player.streak or 0), None, int(player.score)
    if careful:
        # The player's row held while this answer is scored: their streak and
        # their total (for the floor) as they are now.
        row = (await session.execute(
            select(LiveQuizPlayers.score, LiveQuizPlayers.streak, LiveQuizPlayers.streak_at).where(LiveQuizPlayers.id == player.id).with_for_update()
        )).first()
        total_before, streak_before, streak_at_before = int(row[0]), int(row[1] or 0), row[2]
    continues = streak_at_before is not None and view.prev_scored is not None and str(streak_at_before) == view.prev_scored
    scored = scoring.score_answer(
        question=q, response=response, scoring=sc, elapsed=elapsed, streak_before=streak_before, streak_continues=continues, total_before=total_before,
    )
    record = {
        **(response if kind not in scoring.ONE_KEY else {}), "credit": scored.credit, "elapsed_ms": scored.elapsed_ms, "saved_ms": scored.saved_ms,
    }
    if careful:
        record["streak_before"] = streak_before
        record["streak_at_before"] = str(streak_at_before) if streak_at_before else None
    if kind == scoring.TYPE:
        record["norm"] = scoring.normalise(response["text"])[:120]
    if kind == scoring.ORDER:
        record["placed"] = [g for g, c in zip(response["order"], [o["key"] for o in q["options"]]) if g == c]
    option_key = response.get("option_key") or (response.get("option_keys") or [""])[0]
    # Once per player and question, whatever arrives at once: the second of a double tap is told so.
    took = await session.scalar(
        pg_insert(LiveQuizAnswers)
        .values(question_id=q["id"], player_id=player.id, option_key=option_key[:2], correct=scored.correct, points=scored.points, answered_at=now, response=record)
        .on_conflict_do_nothing(index_elements=["question_id", "player_id"])
        .returning(LiveQuizAnswers.id)
    )
    if took is None:
        raise CustomHTTPException(409, message="You already answered this one.")
    values = {"score": LiveQuizPlayers.score + scored.points, "saved_ms": LiveQuizPlayers.saved_ms + scored.saved_ms}
    if careful and kind != scoring.POLL:
        values["streak"] = scored.streak
        values["streak_at"] = q["id"] if scored.correct else None
    score, saved = (await session.execute(
        update(LiveQuizPlayers).where(LiveQuizPlayers.id == player.id).values(**values).returning(LiveQuizPlayers.score, LiveQuizPlayers.saved_ms)
    )).one()
    await session.commit()
    view.answers[player.id] = room.Mine(option_key, scored.points, record, scored.correct, scored.credit)
    player.score, player.saved_ms = int(score), int(saved or 0)
    if careful and kind != scoring.POLL:
        player.streak = scored.streak
    if view.board_key is not None:
        await board.scored(view.board_key, player.member(), player.score, player.saved_ms)
    bus.changed(event_id, "quiz")
    # Answers close before the answer is shown (room.revealed), so an answer
    # taken is never told whether it was right: that comes with the state.
    if view.is_revealed(q, now):
        return {"correct": scored.correct, "points": scored.points, "score": player.score, "answered": True}
    return {"correct": None, "points": None, "score": player.score - scored.points, "answered": True}


# --- winners and the overall board -----------------------------------------------------


async def _ranked(session, quiz_ids: list, st: dict, top: int, *, overall_: bool = False) -> list:
    """Players best first with their rank (ties as the round says)."""
    if overall_:
        who = func.coalesce(LiveQuizPlayers.person, sa.cast(LiveQuizPlayers.id, sa.String))
        rows = (await session.execute(
            select(
                who.label("who"), func.sum(LiveQuizPlayers.score).label("score"), func.sum(LiveQuizPlayers.saved_ms).label("saved"),
                func.min(LiveQuizPlayers.created_at).label("first"), func.count().label("rounds"),
                func.max(LiveQuizPlayers.created_at).label("last"),
            ).where(LiveQuizPlayers.quiz_id.in_(quiz_ids)).group_by(who)
            .order_by(sa.desc("score"), sa.desc("saved"), sa.asc("first")).limit(top)
        )).all()
        whos = [r.who for r in rows]
        latest = {}
        if whos:
            for w, name, reg in (await session.execute(
                select(who, LiveQuizPlayers.name, LiveQuizPlayers.registration_id).where(LiveQuizPlayers.quiz_id.in_(quiz_ids), who.in_(whos))
                .order_by(LiveQuizPlayers.created_at)
            )).all():
                prev = latest.get(w, (None, None))
                latest[w] = (name, reg or prev[1])
        out = []
        for i, r in enumerate(rows):
            name, reg = latest.get(r.who, ("", None))
            out.append({"rank": i + 1, "name": name, "score": int(r.score or 0), "rounds": int(r.rounds), "time_ms": None, "saved_ms": int(r.saved or 0), "registration_id": reg})
        return out
    rows = (await session.execute(
        select(LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.score, LiveQuizPlayers.saved_ms, LiveQuizPlayers.team, LiveQuizPlayers.registration_id)
        .where(LiveQuizPlayers.quiz_id.in_(quiz_ids)).order_by(*_order(st)).limit(top)
    )).all()
    out, last = [], None
    for i, (pid, name, score, saved, team, reg) in enumerate(rows):
        if st["ties"] == "time" or score != (last[0] if last else None):
            rank = i + 1
        else:
            rank = last[1]
        last = (score, rank)
        out.append({"rank": rank, "name": name, "score": int(score), "player_id": pid, "saved_ms": int(saved or 0), "team": team, "registration_id": reg})
    return out


async def _registrations(session, ids: list) -> dict:
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    rows = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.id.in_(ids)))).all()
    return {r.id: r for r in rows}


def _reg_out(reg) -> Optional[dict]:
    if reg is None:
        return None
    return {"id": reg.id, "full_name": reg.full_name, "ticket_id": reg.ticket_id, "position": reg.position}


async def winners(session, quiz: LiveQuizzes, top: int = 10) -> list:
    st = live_settings.quiz_settings(quiz.settings)
    rows = await _ranked(session, [quiz.id], st, top)
    ids = [r["player_id"] for r in rows]
    stats = {}
    if ids:
        stats = {pid: (int(n or 0), t) for pid, n, t in (await session.execute(
            select(
                LiveQuizAnswers.player_id, func.count().filter(LiveQuizAnswers.correct == True),  # noqa: E712
                func.sum(sa.cast(sa.func.json_extract_path_text(LiveQuizAnswers.response, "elapsed_ms"), sa.Integer)),
            ).where(LiveQuizAnswers.player_id.in_(ids)).group_by(LiveQuizAnswers.player_id)
        )).all()}
    regs = await _registrations(session, [r["registration_id"] for r in rows])
    out = []
    for r in rows:
        correct, t = stats.get(r["player_id"], (0, None))
        out.append({
            "rank": r["rank"], "name": r["name"], "score": r["score"], "correct": correct, "time_ms": int(t) if t is not None else None,
            "team": r["team"], "player_id": r["player_id"], "registration": _reg_out(regs.get(r["registration_id"])),
        })
    return out


async def _overall_rounds(session, event_id: int) -> list:
    rows = (await session.execute(select(LiveQuizzes.id, LiveQuizzes.settings).where(LiveQuizzes.event_id == event_id))).all()
    return [qid for qid, s in rows if live_settings.quiz_settings(s)["overall"]]


async def overall(session, event_id: int, top: int = 10, *, anonymous: bool = False, with_registration: bool = False) -> list:
    """The event's board across its rounds (those counting toward it)."""
    ids = await _overall_rounds(session, event_id)
    if not ids:
        return []
    rows = await _ranked(session, ids, live_settings.quiz_settings(None), top, overall_=True)
    regs = await _registrations(session, [r["registration_id"] for r in rows]) if with_registration else {}
    out = []
    for r in rows:
        row = {"rank": r["rank"], "name": r["name"], "score": r["score"], "rounds": r["rounds"], "saved_ms": r["saved_ms"]}
        if anonymous:
            row["name"] = f"Player {r['rank']}"
        if with_registration:
            row["registration"] = _reg_out(regs.get(r["registration_id"]))
        out.append(row)
    return out


WINNER_HEADERS = ["Rank", "Name", "Score", "Correct answers", "Answer time (s)", "Team", "Registered name", "Email", "Phone", "Ticket ID", "Registration ID"]
OVERALL_HEADERS = ["Rank", "Name", "Score", "Rounds", "Registered name", "Email", "Phone", "Ticket ID", "Registration ID"]


async def winner_rows(session, quiz: LiveQuizzes, top: int) -> list:
    rows = await winners(session, quiz, top)
    regs = await _registrations(session, [(r["registration"] or {}).get("id") for r in rows])
    out = []
    for r in rows:
        reg = regs.get((r["registration"] or {}).get("id"))
        out.append([
            r["rank"], r["name"], r["score"], r["correct"], round(r["time_ms"] / 1000, 1) if r["time_ms"] is not None else None, r["team"] or "",
            reg.full_name if reg else "", reg.email if reg else "", reg.phone if reg else "", reg.ticket_id if reg else "", str(reg.id) if reg else "",
        ])
    return out


async def overall_rows(session, event_id: int, top: int) -> list:
    rows = await overall(session, event_id, top, with_registration=True)
    regs = await _registrations(session, [(r["registration"] or {}).get("id") for r in rows])
    out = []
    for r in rows:
        reg = regs.get((r["registration"] or {}).get("id"))
        out.append([
            r["rank"], r["name"], r["score"], r["rounds"],
            reg.full_name if reg else "", reg.email if reg else "", reg.phone if reg else "", reg.ticket_id if reg else "", str(reg.id) if reg else "",
        ])
    return out


async def mark_winners(session, event: Events, user, rows: list, top: int) -> dict:
    """The top `top` marked as the event's winners on their registrations
    (the existing winners feature: position and is_won), so winner
    certificates and badges follow. A player with no registration is skipped."""
    from app.api.events.registration import service as registration_service
    from app.api.events.access import is_admin_user

    marked, skipped = [], []
    for r in rows[:top]:
        reg = r.get("registration")
        if not reg:
            skipped.append({"rank": r["rank"], "name": r["name"], "reason": "no_registration"})
            continue
        await registration_service.set_winner(
            session, user_id=user.id, event_id=event.id, registration_id=str(reg["id"]), position=r["rank"], as_admin=is_admin_user(user),
        )
        marked.append({"rank": r["rank"], "name": r["name"], "registration_id": reg["id"], "position": r["rank"]})
    return {"marked": marked, "skipped": skipped}


# --- Q&A ----------------------------------------------------------------------


_question_row_out = room.question_out


async def ask(session, event: Events, text_: str, author_name: Optional[str], voter_key: str, registration_id=None) -> dict:
    body = " ".join(text_.split())
    if len(body) < 3:
        raise CustomHTTPException(422, message="Ask a question with a few words in it.")
    if event.lifecycle in (EventStatus.closed, EventStatus.archived):
        raise CustomHTTPException(409, message="This event is over; the floor is closed.")
    row = LiveQuestions(
        event_id=event.id, text=body[:500], author_name=(" ".join((author_name or "").split())[:60] or None), author_key=voter_key, registration_id=registration_id,
        status=QuestionStatus.pending if event.qa_approve_first else QuestionStatus.approved, votes=0, pinned=False,
    )
    session.add(row)
    await session.commit()
    bus.changed(event.id, "room")
    return _question_row_out(row, mine=True)


async def _questions(session, event: Events) -> list:
    return list((await session.scalars(select(LiveQuestions).where(LiveQuestions.event_id == event.id).order_by(LiveQuestions.pinned.desc(), LiveQuestions.votes.desc(), LiveQuestions.created_at))).all())


async def questions_for_club(session, event: Events) -> dict:
    rows = await _questions(session, event)
    by = {s: [_question_row_out(r) for r in rows if r.status is s] for s in QuestionStatus}
    return {
        "approve_first": bool(event.qa_approve_first),
        "to_review": by[QuestionStatus.pending], "board": by[QuestionStatus.approved], "answered": by[QuestionStatus.answered],
        "rejected": len(by[QuestionStatus.rejected]),
    }


async def set_qa(session, event: Events, approve_first: bool) -> dict:
    event.qa_approve_first = approve_first
    await session.commit()
    room.room_changed(event.id)
    return await questions_for_club(session, event)


async def _question_in(session, event: Events, question_id: UUID) -> LiveQuestions:
    row = await session.get(LiveQuestions, question_id)
    if row is None or row.event_id != event.id:
        raise CustomHTTPException(404, message="That question could not be found.")
    return row


async def moderate(session, event: Events, question_id: UUID, action: str) -> dict:
    row = await _question_in(session, event, question_id)
    if action == "approve":
        row.status = QuestionStatus.approved
    elif action == "reject":
        row.status, row.pinned = QuestionStatus.rejected, False
    elif action == "answered":
        row.status, row.pinned, row.answered_at = QuestionStatus.answered, False, now_utc()
    elif action == "reopen":
        row.status, row.answered_at = QuestionStatus.approved, None
    elif action == "pin":
        # One on the screen at a time.
        await session.execute(update(LiveQuestions).where(LiveQuestions.event_id == event.id, LiveQuestions.pinned == True, LiveQuestions.id != row.id).values(pinned=False))  # noqa: E712
        row.pinned, row.status = True, QuestionStatus.approved
    elif action == "unpin":
        row.pinned = False
    else:
        raise CustomHTTPException(422, message="Unknown action.")
    await session.commit()
    room.room_changed(event.id)
    return _question_row_out(row)


async def vote(session, event_id: int, question_id: UUID, voter_key: str) -> dict:
    """Vote a question up, or take the vote back: a toggle. The vote row is
    claimed or released by one statement and the count moves by one in the
    same transaction, so two taps at once are a vote and its withdrawal,
    never a 500 or a count off by one."""
    await room.room_of(session, event_id)
    owned = select(LiveQuestions.id).where(LiveQuestions.id == question_id, LiveQuestions.event_id == event_id)
    added = await session.scalar(
        pg_insert(LiveQuestionVotes)
        .from_select(
            ["question_id", "voter_key"],
            select(LiveQuestions.id, literal(voter_key)).where(LiveQuestions.id == question_id, LiveQuestions.event_id == event_id),
            include_defaults=False,
        )
        .on_conflict_do_nothing(index_elements=["question_id", "voter_key"])
        .returning(LiveQuestionVotes.question_id)
    )
    if added is not None:
        votes = await session.scalar(update(LiveQuestions).where(LiveQuestions.id == question_id).values(votes=LiveQuestions.votes + 1).returning(LiveQuestions.votes))
        voted = True
    else:
        if await session.scalar(owned) is None:
            raise CustomHTTPException(404, message="That question could not be found.")
        gone = await session.scalar(delete(LiveQuestionVotes).where(LiveQuestionVotes.question_id == question_id, LiveQuestionVotes.voter_key == voter_key).returning(LiveQuestionVotes.id))
        if gone is not None:
            votes = await session.scalar(update(LiveQuestions).where(LiveQuestions.id == question_id).values(votes=func.greatest(LiveQuestions.votes - 1, 0)).returning(LiveQuestions.votes))
        else:
            votes = await session.scalar(select(LiveQuestions.votes).where(LiveQuestions.id == question_id))
        voted = False
    await session.commit()
    kept = room.voter_kept(event_id, voter_key)
    if kept is not None:
        (kept.voted.add if voted else kept.voted.discard)(str(question_id))
    bus.changed(event_id, "room", delta=["v", voter_key, str(question_id), 1 if voted else 0])
    return {"id": question_id, "votes": int(votes or 0), "voted": voted}


# --- polls ----------------------------------------------------------------------


async def poll_out(session, poll: LivePolls, voter_key: Optional[str] = None) -> dict:
    """The poll as the console sees it (every result, its settings)."""
    out = await room.poll_full(session, poll)
    if voter_key is not None:
        value = await session.scalar(select(LivePollResponses.value).where(LivePollResponses.poll_id == poll.id, LivePollResponses.voter_key == voter_key))
        out.update(room.my_fields(poll.kind.value, poll.options, out["settings"], value, True))
    return out


async def list_polls(session, event: Events) -> list:
    await room.close_due(session, event.id)
    rows = list((await session.scalars(select(LivePolls).where(LivePolls.event_id == event.id).order_by(LivePolls.sort_order, LivePolls.created_at))).all())
    return [await poll_out(session, p) for p in rows]


def _check_poll(kind: PollKind, options: list) -> list:
    opts = [" ".join(o.split())[:120] for o in options if o and o.strip()]
    if kind in (PollKind.choice, PollKind.ranking):
        if not (2 <= len(opts) <= 8) or len(set(opts)) != len(opts):
            noun = "A multiple-choice poll" if kind is PollKind.choice else "A ranking"
            raise CustomHTTPException(422, message=f"{noun} needs two to eight different answers.")
        return opts
    return []


async def create_poll(session, event: Events, body) -> dict:
    kind = PollKind(body.kind)
    top = await session.scalar(select(func.max(LivePolls.sort_order)).where(LivePolls.event_id == event.id))
    options = _check_poll(kind, body.options)
    row = LivePolls(
        event_id=event.id, kind=kind, prompt=" ".join(body.prompt.split())[:300], options=options, sort_order=(top if top is not None else -1) + 1,
        settings=live_settings.poll_settings_saved(kind.value, options, None, body.settings) if body.settings else None,
    )
    if not row.prompt:
        raise CustomHTTPException(422, message="Ask the room something.")
    session.add(row)
    await session.commit()
    # Not on the room's feed while a draft; on the console's list of polls.
    room.room_changed(event.id)
    return await poll_out(session, row)


async def _poll_in(session, event_id: int, poll_id: UUID) -> LivePolls:
    row = await session.get(LivePolls, poll_id)
    if row is None or row.event_id != event_id:
        raise CustomHTTPException(404, message="That poll could not be found.")
    return row


#: What only a draft may change: the poll's answers and how they are counted.
_DRAFT_ONLY = ("max_choices", "scale")


async def update_poll(session, event: Events, poll_id: UUID, body) -> dict:
    row = await _poll_in(session, event.id, poll_id)
    data = body.model_dump(exclude_unset=True)
    change = data.get("settings") or {}
    if row.status is not PollStatus.draft and ("options" in data or "kind" in data or any(k in change for k in _DRAFT_ONLY)):
        raise CustomHTTPException(409, message="A poll that has run keeps its answers; make a new one.")
    if "kind" in data and data["kind"]:
        row.kind = PollKind(data["kind"])
    if "options" in data and data["options"] is not None:
        row.options = _check_poll(row.kind, data["options"])
    if "prompt" in data and data["prompt"]:
        row.prompt = " ".join(data["prompt"].split())[:300]
    if "settings" in data and data["settings"] is not None or "kind" in data or "options" in data:
        row.settings = live_settings.poll_settings_saved(row.kind.value, list(row.options or []), row.settings, change)
    await session.commit()
    room.room_changed(event.id)
    return await poll_out(session, row)


async def set_poll_status(session, event: Events, poll_id: UUID, status: str) -> dict:
    row = await _poll_in(session, event.id, poll_id)
    new = PollStatus(status)
    now = now_utc()
    if new is PollStatus.live:
        # One at a time: launching this one closes whatever is up.
        for other in (await session.scalars(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live, LivePolls.id != row.id))).all():
            other.status, other.closed_at = PollStatus.closed, now
        row.status, row.closed_at = PollStatus.live, None
        row.launched_at = row.launched_at or now
        seconds = live_settings.poll_settings(row.kind.value, row.settings).get("seconds")
        row.closes_at = now + timedelta(seconds=int(seconds)) if seconds else None
    elif new is PollStatus.closed:
        row.status, row.closed_at = PollStatus.closed, now
    else:
        raise CustomHTTPException(422, message="A poll is launched or closed; it does not go back to a draft.")
    await session.commit()
    room.room_changed(event.id)
    return await poll_out(session, row)


async def delete_poll(session, event: Events, poll_id: UUID) -> None:
    row = await _poll_in(session, event.id, poll_id)
    await session.delete(row)
    await session.commit()
    room.room_changed(event.id)


def _clean_value(kind: PollKind, options, value, st: Optional[dict] = None, values=None, order=None) -> str:
    """The stored form of an answer (CONTRACT 7.2): a single choice's label
    (as always), a word, a rating, a text; a multiple choice's or a ranking's
    option indexes joined by commas."""
    st = st or {}
    options = list(options or [])
    v = " ".join((value or "").split())
    if kind is PollKind.choice:
        if int(st.get("max_choices") or 1) > 1:
            picked = values if values is not None else ([v] if v else [])
            if not picked or len(set(picked)) != len(picked) or any(p not in options for p in picked):
                raise CustomHTTPException(422, message="Pick from the answers, each once.")
            if len(picked) > int(st["max_choices"]):
                raise CustomHTTPException(422, message=f"Pick up to {int(st['max_choices'])}.")
            return ",".join(str(i) for i in sorted(options.index(p) for p in picked))
        if v not in options:
            raise CustomHTTPException(422, message="Pick one of the answers.")
    elif kind is PollKind.rating:
        scale = int(st.get("scale") or 5)
        if v not in {str(i) for i in range(1, scale + 1)}:
            raise CustomHTTPException(422, message=f"A rating is 1 to {scale}.")
    elif kind is PollKind.ranking:
        if order is None or sorted(order) != sorted(options) or len(set(order)) != len(order):
            raise CustomHTTPException(422, message="Put every answer in order, each once.")
        return ",".join(str(options.index(o)) for o in order)
    elif kind is PollKind.text:
        if not (1 <= len(v) <= 120):
            raise CustomHTTPException(422, message="Write an answer of up to 120 characters.")
    else:
        v = v.lower()[:40]
        if len(v) < 2:
            raise CustomHTTPException(422, message="A word or two.")
    return v


async def respond(session, event_id: int, poll_id: UUID, voter_key: str, value, now: Optional[datetime] = None, *, values=None, order=None, name=None, v2: bool = False) -> dict:
    """Answer the poll that is up, or change the answer: one upsert. What
    comes back is the room's view of the poll (its counts at most a second
    old) with this browser's answer on it. A poll whose settings say no
    changing is answered once (409 POLL_ANSWERED); one whose timer ran out
    is closed (409 POLL_CLOSED)."""
    now = now or now_utc()
    snap = await room.room_of(session, event_id)
    if snap.poll_id == str(poll_id):
        kind, options, st, closes_at = snap.poll_kind, snap.poll_options, snap.poll_settings, snap.poll_closes_at
    else:  # launched since the snapshot, or not up at all
        row = await _poll_in(session, event_id, poll_id)
        if row.status is not PollStatus.live:
            raise CustomHTTPException(409, message="This poll is not open.")
        # Read afresh below; the streams are told once the answer commits.
        room.forget_room(event_id)
        kind, options, st, closes_at = row.kind, tuple(row.options or ()), live_settings.poll_settings(row.kind.value, row.settings), row.closes_at
    if closes_at is not None and now >= closes_at:
        raise CustomHTTPException(409, message="This poll has closed.", error_code="POLL_CLOSED")
    v = _clean_value(kind, options, value, st, values, order)
    extra: dict = {}
    if kind is PollKind.text:
        extra["status"] = "pending" if st.get("moderate", True) else "approved"
        extra["author_name"] = None if st.get("anonymous", True) else ((" ".join((name or "").split())[:60]) or None)
    # created_at from the clock, not the transaction: a word cloud's ties go to the word given first.
    stmt = pg_insert(LivePollResponses).values(poll_id=poll_id, voter_key=voter_key, value=v, created_at=now_utc(), **extra)
    if st.get("change_vote", True):
        await session.execute(stmt.on_conflict_do_update(index_elements=["poll_id", "voter_key"], set_={"value": stmt.excluded.value, **{k: getattr(stmt.excluded, k) for k in extra}}))
    else:
        took = await session.scalar(stmt.on_conflict_do_nothing(index_elements=["poll_id", "voter_key"]).returning(LivePollResponses.id))
        if took is None:
            raise CustomHTTPException(409, message="You've answered this poll.", error_code="POLL_ANSWERED")
    await session.commit()
    kept = room.voter_kept(event_id, voter_key)
    if kept is not None:
        kept.answers[str(poll_id)] = v
    bus.changed(event_id, "room", delta=["r", voter_key, str(poll_id), v])
    snap = await room.room_of(session, event_id)
    mine = room.my_fields(kind.value, options, st, v, v2)
    if snap.poll_id == str(poll_id):
        poll = (snap.data_v2 if v2 else snap.data).get("poll")
        if poll is not None:
            return {**poll, **mine}
    full = await room.poll_full(session, await _poll_in(session, event_id, poll_id), console=False)
    return {**(room.guest_poll(full) if v2 else room.legacy_poll(full)), **mine}


async def poll_responses(session, event: Events, poll_id: UUID, status: Optional[str], limit: int = 100) -> list:
    """An open-text poll's answers for the console to moderate, newest first."""
    row = await _poll_in(session, event.id, poll_id)
    q = select(LivePollResponses).where(LivePollResponses.poll_id == row.id)
    if status:
        q = q.where(LivePollResponses.status == status)
    rows = (await session.scalars(q.order_by(LivePollResponses.created_at.desc()).limit(limit))).all()
    return [{"id": r.id, "text": r.value, "name": r.author_name, "status": r.status, "at": r.created_at} for r in rows]


async def moderate_response(session, event: Events, poll_id: UUID, response_id: UUID, action: str) -> dict:
    row = await _poll_in(session, event.id, poll_id)
    resp = await session.get(LivePollResponses, response_id)
    if resp is None or resp.poll_id != row.id or row.kind is not PollKind.text:
        raise CustomHTTPException(404, message="That answer could not be found.")
    resp.status = "approved" if action == "approve" else "rejected"
    await session.commit()
    room.room_changed(event.id)
    return {"id": resp.id, "text": resp.value, "name": resp.author_name, "status": resp.status, "at": resp.created_at}


# --- feedback ----------------------------------------------------------------------


def _survey_token(reg_id) -> str:
    """The survey link's proof: signed like every emailed link (app.core.mail.links)."""
    return links.sign(f"feedback:{reg_id}")


def _survey_token_ok(reg_id, token: str) -> bool:
    return links.verify(f"feedback:{reg_id}", token or "")


def survey_link(event: Events, reg: EventRegistrationsLink) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/feedback/{event.id}/{reg.id}?k={_survey_token(reg.id)}"


#: What feedback_url reads of the survey, selected beside the guest's own
#: registration through an outer join on feedback_surveys.event_id (its
#: unique index), so the event page reads both in the statement it already
#: makes: no query more, survey or none. All null when there is no survey.
SURVEY_STATE = (FeedbackSurveys.id.label("survey_id"), FeedbackSurveys.auto_send, FeedbackSurveys.sent_at, FeedbackSurveys.delay_minutes)


def survey_open(event: Events, survey, now: Optional[datetime] = None) -> bool:
    """Whether the survey has opened, by the emails' rules: it went out
    (the club's Send now, or the sweep), or it is switched on and the event's
    end plus its delay has passed, which is when the sweep sends it (within
    the minute). It does not close: the links mailed never expire
    (docs/OPERATIONS.md, the links in emails), and the survey takes answers
    as long as they do. `survey` is a FeedbackSurveys or a SURVEY_STATE row."""
    if survey.sent_at is not None:
        return True
    if not survey.auto_send:
        return False
    return (now or now_utc()) >= _ends_at(event) + timedelta(minutes=survey.delay_minutes or 0)


def feedback_url(event: Events, reg: Optional[EventRegistrationsLink], survey, now: Optional[datetime] = None) -> Optional[str]:
    """The guest's own survey link, the one the email sends (survey_link),
    while it is theirs to use: they were checked in, the event has a survey
    and it is open (survey_open), and the survey's page would show it (as
    room.visible: not a draft, its club listed). None otherwise.

    The link is all it takes to answer as the guest, so `reg` must be the
    caller's own registration; `survey` is None when the event has none."""
    if reg is None or survey is None or not reg.is_attended:
        return None
    if event.is_deleted or event.lifecycle is EventStatus.draft or event.club is None or not event.club.listed:
        return None
    if not survey_open(event, survey, now):
        return None
    return survey_link(event, reg)


async def survey_of(session, event: Events) -> FeedbackSurveys:
    row = await session.scalar(select(FeedbackSurveys).where(FeedbackSurveys.event_id == event.id))
    if row is None:
        row = FeedbackSurveys(event_id=event.id, questions=[dict(q) for q in DEFAULT_SURVEY])
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _check_questions(questions: list) -> list:
    out, seen = [], set()
    for q in questions:
        key, kind, label = (q.key or "").strip(), q.kind, " ".join((q.label or "").split())[:200]
        if not key or key in seen or kind not in ("rating", "nps", "text") or not label:
            raise CustomHTTPException(422, message="Every question needs a kind and some wording, and keys must differ.")
        seen.add(key)
        out.append({"key": key, "kind": kind, "label": label})
    if not out:
        raise CustomHTTPException(422, message="Keep at least one question.")
    return out


def survey_out(survey: FeedbackSurveys) -> dict:
    return {
        "auto_send": survey.auto_send, "delay_minutes": survey.delay_minutes, "channels": clean_channels(survey.channels),
        "questions": list(survey.questions or []), "sent_at": survey.sent_at, "sent_count": survey.sent_count,
    }


# --- which channel each guest is asked on -------------------------------------
#
# The club says which channels the survey may go on; the server decides, for
# each guest, which one it actually goes on: the cheapest allowed channel
# that can reach them, in CHANNEL_ORDER. A push to a phone that has the app
# costs nothing, a WhatsApp and an email each cost the club one of the
# month's messages, so this order is cost order -- and allowing a channel
# can only widen the reach, never raise the bill. Nothing is ever sent twice
# to one guest: one channel each, recorded on their feedback_sends row.
#
# The fallback is written down rather than implied: with ["push", "email"]
# (the default), whoever has the app gets the free push and nobody else's
# address is touched; the people the push cannot reach -- no app, the club
# muted, clubs' pushes switched off -- get the email. With ["push"] alone
# the rest get nothing, and their row says so. With ["email"] alone it
# behaves exactly as it did before this existed.

CHANNEL_ORDER = (PUSH, WHATSAPP, EMAIL)

#: Why a channel could not carry it, per guest: what the row's
#: `skipped_reason` is built from when no allowed channel can.
WHY = {
    PUSH: "the app is not on their phone, or they muted the club",
    WHATSAPP: "they did not ask for WhatsApp, or their number cannot take it",
    EMAIL: "no email address",
}


def clean_channels(raw) -> list[str]:
    """A survey's channels as the rest of the code reads them: the known
    ones, each once, in CHANNEL_ORDER. Anything unrecognised -- a null
    column on a row from before, an empty list -- is the default."""
    picked = {str(c).strip().lower() for c in (raw or [])}
    out = [c for c in CHANNEL_ORDER if c in picked]
    return out or list(DEFAULT_CHANNELS)


def _check_channels(raw) -> list[str]:
    """What the club asked for, refusing what cannot be honoured (the
    dashboard's PUT)."""
    picked = [str(c).strip().lower() for c in (raw or [])]
    bad = [c for c in picked if c not in FEEDBACK_CHANNELS]
    if bad:
        raise CustomHTTPException(422, message=f"The survey goes by push, WhatsApp or email, not {bad[0]!r}.")
    out = [c for c in CHANNEL_ORDER if c in set(picked)]
    if not out:
        raise CustomHTTPException(422, message="Keep at least one channel for the survey.")
    return out


async def push_reachable(session, event: Events, user_ids) -> set[int]:
    """Of these accounts, the ones a survey push would reach: checked in to
    the event, with the club not muted and clubs' pushes on
    (notify.recipients), and with a live device to push to. Exactly the set
    notify.tell_room would tell, so what it says it sent is what these
    people got."""
    from app.api.users.models import UserDeviceTokens
    from app.core.notifications.service import int_array, reachable

    ids = sorted({int(u) for u in user_ids if u})
    if not ids:
        return set()
    with_devices = list((await session.scalars(
        select(UserDeviceTokens.user_id).where(
            UserDeviceTokens.user_id == sa.any_(int_array(ids)),
            UserDeviceTokens.is_deleted == False,  # noqa: E712
            reachable(UserDeviceTokens.user_id),
        ).distinct()
    )).all())
    if not with_devices:
        return set()
    return {int(u) for u in (await session.scalars(
        notify.recipients(event.id, event.club_id, checked_in=True, only=with_devices)
    )).all()}


def whatsapp_possible() -> bool:
    """Whether a WhatsApp survey could go at all: the server holds the
    platform's keys. A club that sends from its own number is decided per
    send (app/api/club_whatsapp.service.route_for), which is why a club
    without the platform's keys is still offered the channel."""
    from app.core import whatsapp

    return bool(whatsapp.configured())


def pick_channel(channels: list[str], guest, pushed: set[int], *, whatsapp_on: bool) -> tuple[Optional[str], Optional[str]]:
    """The channel this guest's survey goes on, and -- when none can -- why
    not, in words a club can read. `guest` is a `_Guest`."""
    from app.core import whatsapp

    tried: list[str] = []
    for channel in channels:
        if channel == PUSH:
            if guest.user_id is not None and int(guest.user_id) in pushed:
                return PUSH, None
        elif channel == WHATSAPP:
            number, _ = whatsapp.normalise(guest.phone)
            if whatsapp_on and guest.whatsapp_opt_in and number is not None and guest.wa_bounced_at is None:
                return WHATSAPP, None
        elif channel == EMAIL:
            if guest.email:
                return EMAIL, None
        tried.append(f"{channel}: {WHY[channel]}")
    return None, ("Nothing could reach this guest — " + "; ".join(tried))[:300]


async def update_survey(session, event: Events, body) -> dict:
    survey = await survey_of(session, event)
    data = body.model_dump(exclude_unset=True)
    if "questions" in data and body.questions is not None:
        survey.questions = _check_questions(body.questions)
    if data.get("auto_send") is not None:
        survey.auto_send = data["auto_send"]
    if data.get("delay_minutes") is not None:
        survey.delay_minutes = max(0, min(7 * 24 * 60, data["delay_minutes"]))
    if data.get("channels") is not None:
        survey.channels = _check_channels(body.channels)
    await session.commit()
    return survey_out(survey)


def _survey_email(event: Events, reg, org: str) -> dict:
    link = survey_link(event, reg)
    first = (reg.full_name or "").split(" ")[0] or "there"
    text_ = f"Hi {first},\n\nThanks for coming to {event.name}. Two minutes of your thoughts would help the next one:\n\n{link}\n\n{org}"
    html = (
        f"<p>Hi {first},</p><p>Thanks for coming to <strong>{event.name}</strong>. Two minutes of your thoughts would help the next one.</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:10px 18px;background:#1f8f6b;color:#fff;border-radius:8px;text-decoration:none">Share feedback</a></p>'
        f"<p style=\"color:#666;font-size:12px\">Or open {link}</p><p>{org}</p>"
    )
    return {"subject": f"How was {event.name}?", "text": text_, "html": html}


SURVEY_MAIL = "live.survey_mail"
SURVEY_WHATSAPP = "live.survey_whatsapp"

#: What the club's monthly messages count a survey as (app/api/message_quota).
SURVEY_KIND = "survey"


def _survey_mail_key(survey_id, registration_id) -> str:
    return f"live:survey:{survey_id}:{registration_id}"


def _survey_whatsapp_key(survey_id, registration_id) -> str:
    return f"live:survey:wa:{survey_id}:{registration_id}"


#: The job that carries one guest's survey on each paid channel.
CHANNEL_JOB = {EMAIL: SURVEY_MAIL, WHATSAPP: SURVEY_WHATSAPP}
CHANNEL_KEY = {EMAIL: _survey_mail_key, WHATSAPP: _survey_whatsapp_key}


@dataclass(frozen=True)
class _Guest:
    """One checked-in guest as the send reads them, read before anything
    commits: the push below ends this session's transaction, which would
    expire the rows themselves."""

    id: object
    user_id: Optional[int]
    email: Optional[str]
    phone: Optional[str]
    whatsapp_opt_in: bool
    wa_bounced_at: Optional[datetime]


def _guests(regs) -> list[_Guest]:
    return [
        _Guest(id=r.id, user_id=r.user_id, email=r.email, phone=r.phone,
               whatsapp_opt_in=bool(r.whatsapp_opt_in), wa_bounced_at=r.wa_bounced_at)
        for r in regs
    ]


async def sends_by_channel(session, survey_id) -> dict:
    """What the survey's sends became, by channel: `{"push": 12,
    "whatsapp": 0, "email": 3, "none": 1}` -- what the club's report shows
    as the split. `none` is the guests no allowed channel could reach
    (their row says why). A row from before the channels existed, which was
    always a mail, counts as an email."""
    out = {c: 0 for c in CHANNEL_ORDER}
    out["none"] = 0
    rows = (await session.execute(
        select(FeedbackSends.channel, FeedbackSends.mailed_at.isnot(None), func.count())
        .where(FeedbackSends.survey_id == survey_id)
        .group_by(FeedbackSends.channel, FeedbackSends.mailed_at.isnot(None))
    )).all()
    for channel, mailed, count in rows:
        key = channel if channel in out else (EMAIL if mailed else "none")
        out[key] += int(count)
    return out


async def send_survey(session, event: Events, now: Optional[datetime] = None) -> dict:
    """To everyone who was checked in and has not been sent it yet, each on
    one channel and each with a link only they hold.

    The free channel goes first and as one send: the guests whose phones
    have the app are worked out (push_reachable), and all of them are told
    in a single fan-out through the live room's own machinery
    (notify.tell_room with `what="feedback"`), which honours their mutes,
    the per-event rate limits and the audit log, and writes their inbox
    lines. Only then is anything paid for: every other guest is claimed for
    the cheapest remaining channel the club allows that can reach them
    (pick_channel), so a club never spends an email on somebody who could
    have had the push. A guest no allowed channel can reach is claimed with
    no channel at all and the reason on their row.

    The claim (feedback_sends) is in the same transaction that marks the
    survey sent, so sending again, or the sweep and a send at once, reaches
    only the people not sent yet. Each paid guest's message is a job of its
    own (SURVEY_MAIL, SURVEY_WHATSAPP), written in that transaction: it
    survives a restart, is retried when the provider does not take it, and
    marks the guest delivered once it went, so a job run twice sends nobody
    twice.

    A guest claimed with no channel is claimed again by a later send (the
    claim re-opens a row that carries neither a channel nor a delivery), so
    a club that adds email after a push-only send does reach them."""
    survey = await survey_of(session, event)
    survey_id, club_id = survey.id, event.club_id
    channels = clean_channels(survey.channels)
    everyone = _guests(await audience_regs(session, event, Audience.checked_in))
    now = now or now_utc()

    # The free channel, as one fan-out. It commits (and on a failure rolls
    # back) this session's transaction, which is why `everyone` is already
    # read out of the rows above.
    pushed: set[int] = set()
    push_outcome = None
    if PUSH in channels and everyone:
        candidates = await push_reachable(session, event, [g.user_id for g in everyone])
        told = await notify.tell_room(session, event, "feedback", only=candidates, now=now)
        push_outcome = told["skipped_reason"]
        if told["sent"]:
            pushed = candidates

    # Asked once a send, not once a guest -- and not at all when the club
    # did not allow the channel.
    whatsapp_on = WHATSAPP in channels and (whatsapp_possible() or await _club_sends_whatsapp(session, club_id))
    rows, waiting = [], []
    for guest in everyone:
        channel, why = pick_channel(channels, guest, pushed, whatsapp_on=whatsapp_on)
        rows.append({
            "survey_id": survey_id, "registration_id": guest.id, "sent_at": now, "channel": channel,
            # The push is already out; the rest wait on their job.
            "delivered_at": now if channel == PUSH else None, "skipped_reason": why,
        })
        if channel in CHANNEL_JOB:
            waiting.append((channel, guest.id))

    claimed: list = []
    if rows:
        stmt = pg_insert(FeedbackSends).values(rows)
        claimed = list((await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["survey_id", "registration_id"],
                set_={
                    "sent_at": stmt.excluded.sent_at, "channel": stmt.excluded.channel,
                    "delivered_at": stmt.excluded.delivered_at, "skipped_reason": stmt.excluded.skipped_reason,
                },
                # Only a row nothing ever went out on: a guest already sent
                # the survey -- or with a message still waiting on its job --
                # is left exactly as they are.
                where=sa.and_(
                    FeedbackSends.channel.is_(None), FeedbackSends.delivered_at.is_(None), FeedbackSends.mailed_at.is_(None),
                ),
            ).returning(FeedbackSends.registration_id, FeedbackSends.channel)
        )).all())
    mine = {r[0] for r in claimed}
    sent = sum(1 for _reg_id, channel in claimed if channel is not None)
    await session.execute(
        update(FeedbackSurveys)
        .where(FeedbackSurveys.id == survey_id)
        .values(sent_at=now, sent_count=FeedbackSurveys.sent_count + sent)
    )
    for channel, job in CHANNEL_JOB.items():
        await jobs.enqueue_many(session, job, [
            ({"survey_id": survey_id, "registration_id": reg_id}, CHANNEL_KEY[channel](survey_id, reg_id))
            for c, reg_id in waiting if c == channel and reg_id in mine
        ])
    await session.commit()
    survey = await session.scalar(select(FeedbackSurveys).where(FeedbackSurveys.id == survey_id))
    return {
        "sent": sent, "invited": len(everyone), "push_skipped_reason": push_outcome,
        "by_channel": await sends_by_channel(session, survey_id), **survey_out(survey),
    }


async def _club_sends_whatsapp(session, club_id: Optional[int]) -> bool:
    """Whether this club could send the survey from its own WhatsApp number
    (app/api/club_whatsapp), which costs it nothing of its MyOtherApp
    allowance. Never raises: without an answer the channel is simply not
    offered to this send."""
    if club_id is None:
        return False
    try:
        from app.api.club_whatsapp import service as club_whatsapp

        return await club_whatsapp.route_for(session, int(club_id), "event_update") is not None
    except Exception:  # noqa: BLE001 - the other channels still carry it
        logger.warning("club %s: could not tell whether it sends WhatsApp itself", club_id, exc_info=True)
        return False


@jobs.handler(SURVEY_MAIL, max_attempts=6, timeout=60.0)
async def _survey_mail_job(payload: dict) -> None:
    await mail_survey(UUID(str(payload["survey_id"])), UUID(str(payload["registration_id"])))


async def mail_survey(survey_id: UUID, registration_id: UUID) -> bool:
    """One guest's survey mail, unless it went already. True when it went now.

    Read, send, then mark: a run cut off between the send and the mark sends
    it again on the retry -- rather that than a mail claimed and never sent --
    and the provider is given the same idempotency key both times, so one
    that keeps them (Resend) delivers it once. A provider that does not take
    it raises here, and the job is tried again later.

    It counts against the club's monthly emails (app/api/message_quota):
    past Free's limit it is held back, the reason kept on its row.

    `delivered_at` and `mailed_at` are both written: the first is what the
    channels read, the second is what it has always been for an email."""
    terms = held = None
    async with jobs.scope() as s:
        row = (await s.execute(
            # Already gone, on either column: a row claimed before
            # `delivered_at` existed has only `mailed_at`.
            select(func.coalesce(FeedbackSends.delivered_at, FeedbackSends.mailed_at), EventRegistrationsLink, Events)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == FeedbackSends.registration_id)
            .join(FeedbackSurveys, FeedbackSurveys.id == FeedbackSends.survey_id)
            .join(Events, Events.id == FeedbackSurveys.event_id)
            .options(joinedload(Events.club))
            .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
        )).first()
        if row is not None and row[0] is None and row[1].email:
            terms, held = await quota.check(s, row[2].club_id, quota.EMAIL, SURVEY_KIND)
            if held:
                await s.execute(
                    update(FeedbackSends)
                    .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
                    .values(skipped_reason=held[:300])
                )
        # End the read before the provider's call: no pooled connection held meanwhile.
        await s.commit()
    if row is None or row[0] is not None or not row[1].email or held:
        return False
    reg, event = row[1], row[2]
    org = (event.club.name if event.club is not None else None) or "The organisers"
    mail = _survey_email(event, reg, org)
    await asyncio.to_thread(
        email_mod.send_email, recipients=[reg.email], subject=mail["subject"], text=mail["text"], html=mail["html"],
        sender_name=org, idempotency_key=_survey_mail_key(survey_id, registration_id),
    )
    async with jobs.scope() as s:
        await s.execute(
            update(FeedbackSends)
            .where(
                FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id,
                FeedbackSends.delivered_at.is_(None), FeedbackSends.mailed_at.is_(None),
            )
            .values(channel=EMAIL, delivered_at=now_utc(), mailed_at=now_utc(), skipped_reason=None)
        )
        await quota.count(
            s, club_id=event.club_id, channel=quota.EMAIL, kind=SURVEY_KIND, terms=terms,
            ref=f"survey:{survey_id}:{registration_id}",
        )
        await s.commit()
    return True


# --- the survey on WhatsApp ----------------------------------------------------


def _survey_whatsapp(event: Events, reg, org: str):
    """The survey as the approved `event_update` template carries it. Our
    own words cannot go by WhatsApp outside a conversation the guest
    started, so this is the UTILITY template Meta already approved for
    event updates, with the ask and the guest's own link as the update. A
    `feedback_survey` template of its own would read better and is the
    follow-up this leaves for Meta's review."""
    from app.core import whatsapp

    return whatsapp.event_update(
        name=reg.full_name or "", club=org, event=event.name,
        update=f"how was it? Two minutes of your thoughts would help the next one: {survey_link(event, reg)}",
    )


@jobs.handler(SURVEY_WHATSAPP, max_attempts=6, timeout=60.0)
async def _survey_whatsapp_job(payload: dict) -> None:
    await whatsapp_survey(UUID(str(payload["survey_id"])), UUID(str(payload["registration_id"])))


async def whatsapp_survey(survey_id: UUID, registration_id: UUID) -> bool:
    """One guest's survey on WhatsApp, unless it went already. True when it
    went now. Read, send, then mark, as the mail does: a run cut off between
    the two sends it again on the retry rather than claim a message nobody
    got. Meta has no idempotency key, so a guest could see it twice; the
    alternative is a guest who never sees it.

    It goes from the club's own number when the club has one routed
    (app/api/club_whatsapp), which it pays Meta for and which is never
    counted here; otherwise from the platform's, counted against the club's
    monthly WhatsApp (app/api/message_quota) and held back past the
    allowance with the reason kept on its row."""
    from app.api.club_whatsapp import service as club_whatsapp
    from app.core import whatsapp

    terms = held = route = None
    async with jobs.scope() as s:
        row = (await s.execute(
            select(FeedbackSends.delivered_at, EventRegistrationsLink, Events)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == FeedbackSends.registration_id)
            .join(FeedbackSurveys, FeedbackSurveys.id == FeedbackSends.survey_id)
            .join(Events, Events.id == FeedbackSurveys.event_id)
            .options(joinedload(Events.club))
            .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
        )).first()
        if row is not None and row[0] is None and row[1].phone:
            route = await club_whatsapp.route_for(s, row[2].club_id, "event_update")
            if route is None:
                terms, held = await quota.check(s, row[2].club_id, quota.WHATSAPP, SURVEY_KIND)
            if held:
                await s.execute(
                    update(FeedbackSends)
                    .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
                    .values(skipped_reason=held[:300])
                )
        await s.commit()
    if row is None or row[0] is not None or not row[1].phone or held:
        return False
    reg, event = row[1], row[2]
    org = (event.club.name if event.club is not None else None) or "The organisers"
    try:
        await whatsapp.send(reg.phone, _survey_whatsapp(event, reg, org), sender=route.sender if route is not None else None)
    except whatsapp.Undeliverable as e:
        # A fact about the number, not a failure: the row says so and the
        # job is done with it.
        async with jobs.scope() as s:
            await s.execute(
                update(FeedbackSends)
                .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
                .values(skipped_reason=str(e.reason)[:300])
            )
            await s.commit()
        return False
    async with jobs.scope() as s:
        await s.execute(
            update(FeedbackSends)
            .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id, FeedbackSends.delivered_at.is_(None))
            .values(channel=WHATSAPP, delivered_at=now_utc(), skipped_reason=None)
        )
        if route is None:
            await quota.count(
                s, club_id=event.club_id, channel=quota.WHATSAPP, kind=SURVEY_KIND, terms=terms,
                ref=f"survey:wa:{survey_id}:{registration_id}",
            )
        await s.commit()
    return True


async def _results(session, survey: FeedbackSurveys, invited: int) -> dict:
    rows = list((await session.scalars(select(FeedbackResponses).where(FeedbackResponses.survey_id == survey.id))).all())
    answers = [r.answers or {} for r in rows]
    out: dict = {"responses": len(rows), "invited": invited, "nps": None, "averages": [], "texts": []}
    for q in survey.questions or []:
        vals = [a.get(q["key"]) for a in answers if a.get(q["key"]) not in (None, "")]
        if q["kind"] == "rating":
            nums = [float(v) for v in vals if str(v).replace(".", "", 1).isdigit()]
            out["averages"].append({"key": q["key"], "label": q["label"], "average": round(sum(nums) / len(nums), 1) if nums else None, "count": len(nums)})
        elif q["kind"] == "nps" and out["nps"] is None:
            nums = [int(v) for v in vals if str(v).isdigit()]
            n = len(nums)
            if n:
                pro, det = sum(1 for v in nums if v >= 9), sum(1 for v in nums if v <= 6)
                out["nps"] = {"score": round(100 * (pro - det) / n), "promoters": round(100 * pro / n), "passives": round(100 * (n - pro - det) / n), "detractors": round(100 * det / n), "count": n}
        elif q["kind"] == "text":
            out["texts"].extend(str(v)[:300] for v in vals[-12:])
    return out


async def feedback_for_club(session, event: Events) -> dict:
    survey = await survey_of(session, event)
    counts = await audience_counts(session, event)
    results = await _results(session, survey, counts["checked_in"])
    # Last time: the club's most recent other event that got replies.
    previous = None
    others = (await session.execute(
        select(FeedbackSurveys, Events).join(Events, Events.id == FeedbackSurveys.event_id)
        .where(Events.club_id == event.club_id, Events.id != event.id, Events.is_deleted == False, FeedbackSurveys.sent_at.isnot(None))  # noqa: E712
        .order_by(Events.event_datetime.desc()).limit(5)
    )).all()
    for s, e in others:
        inv = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.event_id == e.id)) or 0
        r = await _results(session, s, inv)
        if r["responses"]:
            previous = {"event_id": e.id, "event_name": e.name, **r}
            break
    return {
        **survey_out(survey), "sends_at": _auto_send_at(event, survey), "results": results, "previous": previous,
        "checked_in": counts["checked_in"], "by_channel": await sends_by_channel(session, survey.id),
    }


def _ends_at(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=event.duration or 0)


def _auto_send_at(event: Events, survey: FeedbackSurveys) -> Optional[datetime]:
    if not survey.auto_send or survey.sent_at is not None:
        return None
    return _ends_at(event) + timedelta(minutes=survey.delay_minutes or 0)


async def public_survey(session, event: Events, reg_id: UUID, token: str) -> dict:
    reg = await session.get(EventRegistrationsLink, reg_id)
    if reg is None or reg.event_id != event.id or not _survey_token_ok(reg.id, token):
        raise CustomHTTPException(404, message="This feedback link is not valid.")
    survey = await survey_of(session, event)
    done = await session.scalar(select(FeedbackResponses).where(FeedbackResponses.survey_id == survey.id, FeedbackResponses.registration_id == reg.id))
    return {"event_name": event.name, "club_name": event.club.name if getattr(event, "club", None) else "", "first_name": (reg.full_name or "").split(" ")[0], "questions": list(survey.questions or []), "submitted": done is not None}


async def submit_survey(session, event: Events, reg_id: UUID, token: str, answers: dict) -> dict:
    reg = await session.get(EventRegistrationsLink, reg_id)
    if reg is None or reg.event_id != event.id or not _survey_token_ok(reg.id, token):
        raise CustomHTTPException(404, message="This feedback link is not valid.")
    survey = await survey_of(session, event)
    clean: dict = {}
    for q in survey.questions or []:
        v = answers.get(q["key"])
        if v in (None, ""):
            continue
        if q["kind"] == "rating" and str(v) in {"1", "2", "3", "4", "5"}:
            clean[q["key"]] = int(v)
        elif q["kind"] == "nps" and str(v).isdigit() and 0 <= int(v) <= 10:
            clean[q["key"]] = int(v)
        elif q["kind"] == "text":
            clean[q["key"]] = " ".join(str(v).split())[:1000]
    if not clean:
        raise CustomHTTPException(422, message="Answer at least one question.")
    stmt = pg_insert(FeedbackResponses).values(survey_id=survey.id, registration_id=reg.id, answers=clean)
    await session.execute(stmt.on_conflict_do_update(index_elements=["survey_id", "registration_id"], set_={"answers": stmt.excluded.answers}))
    await session.commit()
    return {"submitted": True}


def due_query(now: datetime, limit: int = SWEEP_BATCH):
    """The surveys whose moment has come and that have not gone out: a range
    of ix_feedback_surveys_due, oldest first."""
    return (
        select(FeedbackSurveys, Events).join(Events, Events.id == FeedbackSurveys.event_id).options(joinedload(Events.club))
        .where(
            FeedbackSurveys.auto_send == True, FeedbackSurveys.sent_at.is_(None),  # noqa: E712
            FeedbackSurveys.due_at.isnot(None), FeedbackSurveys.due_at <= now,
            Events.is_deleted == False, Events.lifecycle != EventStatus.draft,  # noqa: E712
        )
        .order_by(FeedbackSurveys.due_at).limit(limit)
    )


async def sweep(session, now: Optional[datetime] = None) -> int:
    """Every minute: surveys whose moment has come go out.

    One that fails is rolled back, logged by its event's id, and tried again
    next minute; the ones after it still go. A rollback expires every row the
    session holds (send_survey rolls back itself on some failures), so the
    loop carries ids and reads an event again whenever it was expired. It
    used to read the expired rows: one failing survey held back every survey
    due after it, each minute."""
    now = now or now_utc()
    due = [(survey.id, event.id, event) for survey, event in (await session.execute(due_query(now))).all()]
    sent = 0
    failed: list[int] = []
    for survey_id, event_id, event in due:
        try:
            if inspect(event).expired_attributes:
                event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id))
                if event is None:
                    continue
            await send_survey(session, event, now)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("feedback auto-send failed for survey %s on event %s", survey_id, event_id)
            failed.append(event_id)
            await session.rollback()
    if failed:
        logger.warning("feedback sweep: %d sent, %d failed (events %s)", sent, len(failed), failed)
    return sent


async def sweep_loop(interval: int = 60) -> None:
    await asyncio.sleep(11)
    while True:
        try:
            async with deliveries.session_scope() as session:
                # The lock on a connection of its own (app.db.locks): taken
                # through the session, it stayed with whichever pooled
                # connection had it once the sweep committed.
                from app.db.locks import singleton

                async with singleton(session, SWEEP_LOCK) as mine:
                    if mine:
                        n = await sweep(session)
                        if n:
                            logger.info("feedback sweep sent %d survey(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("feedback sweep failed")
        await asyncio.sleep(interval)


# --- live updates ----------------------------------------------------------------------


update_out = room.update_out


UPDATE_PUSH_JOB = "live.update_push"


async def push_update(session, event: Events, kind: str, text_: str, audience: str, by=None) -> dict:
    """An update: the banner on the event page, a line in the inbox of
    everyone in the audience with an account, and a push to those of them
    who have not muted the club or turned clubs' pushes off (notify.py's
    rule). The inbox lines, the push's job and the update are one commit;
    the push goes from the job (`live.update_push`), never inside the
    request. `recipients` counts the audience; `pushed`, the people the push
    was written for."""
    from app.api.live import notify
    from app.core.notifications import fanout

    body = " ".join((text_ or "").split())
    if len(body) < 3:
        raise CustomHTTPException(422, message="Say what changed.")
    aud = Audience(audience)
    regs = await audience_regs(session, event, aud)
    user_ids = sorted({r.user_id for r in regs if r.user_id})
    row = LiveUpdates(id=uuid4(), event_id=event.id, kind=UpdateKind(kind), text=body[:500], audience=aud, recipients=len(regs), sent_by=getattr(by, "id", None), sent_at=now_utc(), pushed=0)
    session.add(row)
    push_id = f"live-update:{row.id}"
    data = {"type": UPDATE_TYPE, "event_id": str(event.id), "kind": row.kind.value, "push_id": push_id}
    if user_ids:
        await fanout.insert_for_audience(
            session, fanout.users_in(user_ids), title=event.name, description=body, type=UPDATE_TYPE, data=data,
            from_club_id=event.club_id, event_id=event.id, line_key=row.id,
        )
        told = sorted(int(u) for u in (await session.scalars(
            notify.recipients(event.id, event.club_id, checked_in=aud is Audience.checked_in)
        )).all())
        if told:
            # The update is the fan-out: its id names each person's line.
            push = {"user_ids": told, "title": event.name, "body": body, "data": fanout.with_line(data, row.id)}
            await jobs.enqueue(session, UPDATE_PUSH_JOB, push, dedupe_key=f"live:update:{row.id}")
        row.pushed = len(told)
    await session.commit()
    room.room_changed(event.id)
    if user_ids:
        from app.api.clubs import announcements_page

        announcements_page.forget(event.club_id)
    return update_out(row)


@jobs.handler(UPDATE_PUSH_JOB, max_attempts=4, timeout=300.0, concurrency=2, priority=jobs.HIGH)
async def _update_push_job(p: dict) -> None:
    """The update's push, to every device of the people it was written for.
    Run again, it pushes again with the same push_id: the app shows a push
    with a given id once."""
    from app.core.notifications.service import send_notification_to_users

    async with jobs.scope() as s:
        await send_notification_to_users(s, [int(u) for u in p.get("user_ids") or ()], p["title"], p["body"], data=dict(p.get("data") or {}))


async def list_updates(session, event: Events, limit: int = 30) -> list:
    rows = (await session.scalars(select(LiveUpdates).where(LiveUpdates.event_id == event.id).order_by(LiveUpdates.sent_at.desc()).limit(limit))).all()
    return [update_out(u) for u in rows]


async def shift_schedule(session, event: Events, item_id: UUID, minutes: int, by=None) -> dict:
    """Moves a block and every block after it, and tells the room."""
    if minutes == 0 or abs(minutes) > 24 * 60:
        raise CustomHTTPException(422, message="Shift by a few minutes, either way.")
    rows = list((await session.scalars(select(AgendaItems).where(AgendaItems.event_id == event.id).order_by(AgendaItems.starts_at, AgendaItems.sort_order, AgendaItems.created_at))).all())
    idx = next((i for i, r in enumerate(rows) if r.id == item_id), None)
    if idx is None:
        raise CustomHTTPException(404, message="That block could not be found.")
    delta = timedelta(minutes=minutes)
    for r in rows[idx:]:
        r.starts_at = r.starts_at + delta
        if r.ends_at is not None:
            r.ends_at = r.ends_at + delta
    await session.commit()
    first = rows[idx]
    later = len(rows) - idx - 1
    text_ = f"{first.title} now starts at {_clock(first.starts_at)}" + (f" ({'+' if minutes > 0 else ''}{minutes} min); everything after moves with it." if later else f" ({'+' if minutes > 0 else ''}{minutes} min).")
    update_ = await push_update(session, event, "schedule", text_, "checked_in", by)
    return {"update": update_, "items": [{"id": r.id, "title": r.title, "starts_at": r.starts_at, "ends_at": r.ends_at} for r in rows]}


# --- what the room sees ----------------------------------------------------------------------


async def screen(session, event: Events, now: Optional[datetime] = None) -> dict:
    """The big screen: the pinned question, the board, the poll up now, the
    quiz if it runs, the last update -- and, with this release, the theme,
    the screen's controls and what it shows (`shows`), the Q&A wall, the
    updates for a ticker, the agenda and sponsors for idle slides, and the
    board across rounds."""
    from app.api.live import looks

    now = now or now_utc()
    control = await looks.get_control(session, event.id)
    wall_size = int(control["wall_size"])
    rows = list((await session.scalars(
        select(LiveQuestions).where(LiveQuestions.event_id == event.id, LiveQuestions.status == QuestionStatus.approved)
        .order_by(LiveQuestions.pinned.desc(), LiveQuestions.votes.desc(), LiveQuestions.created_at).limit(max(6, wall_size))
    )).all())
    pinned = next((r for r in rows if r.pinned), None)
    board = [r for r in rows if not r.pinned][:5]
    await room.close_due(session, event.id, now)
    live_poll = await session.scalar(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live).limit(1))
    shown_poll = live_poll
    if control["mode"] == "poll" and live_poll is None:
        shown_poll = await session.scalar(
            select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.closed).order_by(LivePolls.closed_at.desc().nullslast()).limit(1)
        )
    quiz = await quiz_of(session, event, create=False)
    latest = list((await session.scalars(select(LiveUpdates).where(LiveUpdates.event_id == event.id).order_by(LiveUpdates.sent_at.desc()).limit(5))).all())
    fresh = [u for u in latest if now - u.sent_at <= BANNER_FOR]
    custom, resolved, version = await looks.state(session, event.id)
    quiz_live = quiz is not None and quiz.status is QuizStatus.live
    if control["mode"] == "auto":
        shows = "quiz" if quiz_live else "poll" if live_poll is not None else "qa" if pinned is not None else "idle" if control["idle_slides"] else "qa"
    else:
        shows = control["mode"]
    show_quiz = quiz is not None and (quiz_live or shows in ("quiz", "leaderboard"))
    hide = control["results"] == "hidden"
    poll = await poll_out(session, shown_poll) if shown_poll else None
    if poll is not None and hide and poll["status"] == "live":
        poll["results"] = {"responses": poll["results"]["responses"]}
    quiz_state = await host_state(session, quiz, now, for_screen=True, hide_results=hide) if show_quiz else None
    overall_rows = None
    if shows == "leaderboard" and quiz is not None:
        overall_rows = await overall(session, event.id, top=10, anonymous=bool(quiz_state and quiz_state["settings"]["players"]["anonymous_screen"]))
    want_sponsors = bool(resolved["screen"]["sponsors"]) if custom else False
    want_sponsors = want_sponsors or "sponsors" in control["idle_slides"]
    return {
        "event": {"id": event.id, "name": event.name},
        "pinned": _question_row_out(pinned) if pinned else None,
        "board": [_question_row_out(r) for r in board],
        "poll": poll if (live_poll is not None or shows == "poll") else None,
        "quiz": quiz_state if (quiz_live or shows in ("quiz", "leaderboard")) else None,
        "update": update_out(fresh[0]) if fresh else None,
        "theme": resolved, "theme_custom": custom, "theme_version": version,
        "control": control, "shows": shows,
        "wall": [_question_row_out(r) for r in rows[:wall_size]],
        "updates": [update_out(u) for u in fresh],
        "agenda": await looks.agenda(session, event.id, now) if "agenda" in control["idle_slides"] else [],
        "sponsors": await looks.sponsors(session, event.id) if want_sponsors else [],
        "overall": overall_rows,
    }
