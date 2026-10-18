"""The live room.

Five things happen while an event runs, and one just after:

* a quiz the host drives from the console, which the room plays on phones
  with a code off the screen;
* questions the floor asks and votes up, which the organiser lets through,
  pins to the screen and marks answered;
* polls and word clouds, one live at a time;
* updates pushed to phones and shown as a banner on the event page, some of
  them made by shifting the run of show;
* the feedback survey, sent to everyone who was there an hour after the end.

Nobody in the room needs an account. A browser makes itself a key and sends
it on every call; that key is the voter, the asker and the player.
"""
import asyncio
import hashlib
import hmac
import logging
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import joinedload

from app.api.agenda.models import AgendaItems
from app.api.checkins.models import CheckIns
from app.api.closeout.models import EventStatus
from app.api.deliveries import service as deliveries
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink, Events
from app.api.live.models import (
    Audience,
    FeedbackResponses,
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
from app.core.email import email as email_mod
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

VOTER_HEADER = "X-Live-Key"
PLAYER_HEADER = "X-Live-Player"
SWEEP_LOCK = 7_301_010
# Answers after the clock plus this grace still count, for a slow phone.
ANSWER_GRACE = 2
UPDATE_TYPE = "event_live_update"
# A banner is the latest update, as long as it is this fresh.
BANNER_FOR = timedelta(hours=3)

DEFAULT_SURVEY = [
    {"key": "overall", "kind": "rating", "label": "How was the event overall?"},
    {"key": "mentors", "kind": "rating", "label": "How useful were the mentors?"},
    {"key": "recommend", "kind": "nps", "label": "How likely are you to recommend us to a friend?"},
    {"key": "change", "kind": "text", "label": "What should we change next time?"},
]

_tz = ZoneInfo(EVENT_TIMEZONE)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def voter_key_of(request) -> str:
    key = (request.headers.get(VOTER_HEADER) or "").strip()
    if not (8 <= len(key) <= 64) or not key.replace("-", "").replace("_", "").isalnum():
        raise CustomHTTPException(422, message="This browser has no key for the room yet. Reload the page and try again.")
    return key


def _clock(dt: datetime) -> str:
    return dt.astimezone(_tz).strftime("%-I:%M %p")


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


async def quiz_of(session, event: Events, create: bool = True) -> Optional[LiveQuizzes]:
    quiz = await session.scalar(select(LiveQuizzes).where(LiveQuizzes.event_id == event.id))
    if quiz is None and create:
        quiz = LiveQuizzes(event_id=event.id, title=f"{event.name} quiz")
        session.add(quiz)
        await session.commit()
        await session.refresh(quiz)
    return quiz


def _question_out(q: LiveQuizQuestions, with_answer: bool) -> dict:
    out = {"id": q.id, "prompt": q.prompt, "options": list(q.options or []), "seconds": q.seconds, "sort_order": q.sort_order}
    if with_answer:
        out["correct_key"] = q.correct_key
    return out


async def _player_count(session, quiz_id) -> int:
    return await session.scalar(select(func.count()).select_from(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz_id)) or 0


async def quiz_out(session, quiz: LiveQuizzes) -> dict:
    return {
        "id": quiz.id, "title": quiz.title, "intro": quiz.intro, "status": quiz.status.value, "join_code": quiz.join_code,
        "current_index": quiz.current_index, "players": await _player_count(session, quiz.id),
        "questions": [_question_out(q, True) for q in quiz.questions],
    }


def _check_options(options: list, correct_key: str) -> None:
    keys = [o.get("key") for o in options]
    if not (2 <= len(options) <= 6) or len(set(keys)) != len(keys) or any(not (o.get("label") or "").strip() for o in options):
        raise CustomHTTPException(422, message="Give the question two to six answers, each with text.")
    if correct_key not in keys:
        raise CustomHTTPException(422, message="Mark one of the answers as the right one.")


async def update_quiz(session, quiz: LiveQuizzes, body) -> dict:
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"]:
        quiz.title = data["title"].strip()
    if "intro" in data:
        quiz.intro = (data["intro"] or "").strip() or None
    await session.commit()
    return await quiz_out(session, quiz)


async def add_question(session, quiz: LiveQuizzes, body) -> dict:
    options = [{"key": o.key, "label": o.label.strip()} for o in body.options]
    _check_options(options, body.correct_key)
    top = max((q.sort_order for q in quiz.questions), default=-1)
    row = LiveQuizQuestions(quiz_id=quiz.id, prompt=body.prompt.strip(), options=options, correct_key=body.correct_key, seconds=body.seconds, sort_order=top + 1)
    session.add(row)
    await session.commit()
    await session.refresh(quiz)
    return _question_out(row, True)


async def _question_of(session, quiz: LiveQuizzes, question_id: UUID) -> LiveQuizQuestions:
    row = await session.get(LiveQuizQuestions, question_id)
    if row is None or row.quiz_id != quiz.id:
        raise CustomHTTPException(404, message="That question is not in this quiz.")
    return row


async def update_question(session, quiz: LiveQuizzes, question_id: UUID, body) -> dict:
    row = await _question_of(session, quiz, question_id)
    data = body.model_dump(exclude_unset=True)
    options = [{"key": o.key, "label": o.label.strip()} for o in body.options] if body.options is not None else list(row.options)
    correct = data.get("correct_key") or row.correct_key
    _check_options(options, correct)
    if data.get("prompt"):
        row.prompt = data["prompt"].strip()
    row.options, row.correct_key = options, correct
    if data.get("seconds"):
        row.seconds = data["seconds"]
    await session.commit()
    await session.refresh(quiz)
    return _question_out(row, True)


async def delete_question(session, quiz: LiveQuizzes, question_id: UUID) -> None:
    row = await _question_of(session, quiz, question_id)
    await session.delete(row)
    await session.commit()
    await session.refresh(quiz)


async def _fresh_code(session) -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(900_000) + 100_000}"
        if await session.scalar(select(LiveQuizzes.id).where(LiveQuizzes.join_code == code)) is None:
            return code
    raise CustomHTTPException(500, message="Could not make a join code; try again.")


async def start_quiz(session, quiz: LiveQuizzes) -> dict:
    """Opens the lobby. A quiz run before starts clean: last time's players and answers go."""
    if not quiz.questions:
        raise CustomHTTPException(422, message="Add at least one question before hosting.")
    for p in list((await session.scalars(select(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))).all()):
        await session.delete(p)
    quiz.status, quiz.current_index, quiz.question_opened_at, quiz.ended_at = QuizStatus.live, -1, None, None
    quiz.join_code = await _fresh_code(session)
    await session.commit()
    return await host_state(session, quiz)


async def next_question(session, quiz: LiveQuizzes) -> dict:
    if quiz.status is not QuizStatus.live:
        raise CustomHTTPException(409, message="The quiz is not running.")
    if quiz.current_index + 1 >= len(quiz.questions):
        return await end_quiz(session, quiz)
    quiz.current_index += 1
    quiz.question_opened_at = now_utc()
    await session.commit()
    return await host_state(session, quiz)


async def end_quiz(session, quiz: LiveQuizzes) -> dict:
    quiz.status, quiz.ended_at, quiz.question_opened_at = QuizStatus.ended, now_utc(), None
    quiz.join_code = None
    await session.commit()
    return await host_state(session, quiz)


async def reset_quiz(session, quiz: LiveQuizzes) -> dict:
    """Back to a draft: questions stay, the run is forgotten."""
    for p in list((await session.scalars(select(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))).all()):
        await session.delete(p)
    quiz.status, quiz.current_index, quiz.question_opened_at, quiz.ended_at, quiz.join_code = QuizStatus.draft, -1, None, None, None
    await session.commit()
    return await host_state(session, quiz)


def _time_left(quiz: LiveQuizzes, q: LiveQuizQuestions, now: datetime) -> int:
    if quiz.question_opened_at is None:
        return q.seconds
    return max(0, int(q.seconds - (now - quiz.question_opened_at).total_seconds()))


async def _leaderboard(session, quiz: LiveQuizzes, limit: int = 10) -> list:
    rows = list((await session.scalars(select(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id).order_by(LiveQuizPlayers.score.desc(), LiveQuizPlayers.created_at).limit(limit))).all())
    return [{"rank": i + 1, "name": p.name, "score": p.score} for i, p in enumerate(rows)]


async def _tally(session, q: LiveQuizQuestions) -> dict:
    rows = (await session.execute(select(LiveQuizAnswers.option_key, func.count()).where(LiveQuizAnswers.question_id == q.id).group_by(LiveQuizAnswers.option_key))).all()
    by = dict(rows)
    return {"answered": sum(by.values()), "counts": [{"key": o["key"], "label": o["label"], "count": by.get(o["key"], 0), "correct": o["key"] == q.correct_key} for o in q.options]}


async def host_state(session, quiz: LiveQuizzes, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    current = quiz.questions[quiz.current_index] if quiz.status is QuizStatus.live and 0 <= quiz.current_index < len(quiz.questions) else None
    out = {
        "status": quiz.status.value, "join_code": quiz.join_code, "title": quiz.title, "players": await _player_count(session, quiz.id),
        "current_index": quiz.current_index, "total": len(quiz.questions), "question": None, "leaderboard": await _leaderboard(session, quiz),
    }
    if current is not None:
        out["question"] = {**_question_out(current, True), "time_left": _time_left(quiz, current, now), "opened_at": quiz.question_opened_at, **(await _tally(session, current))}
    return out


async def join_quiz(session, code: str, name: str, registration_id=None) -> dict:
    quiz = await session.scalar(select(LiveQuizzes).where(LiveQuizzes.join_code == code.strip(), LiveQuizzes.status == QuizStatus.live))
    if quiz is None:
        raise CustomHTTPException(404, message="No quiz is running with that code. Check the screen and try again.")
    name = " ".join(name.split())[:60]
    if not name:
        raise CustomHTTPException(422, message="Tell the room your name.")
    player = LiveQuizPlayers(quiz_id=quiz.id, name=name, player_key=secrets.token_urlsafe(24), registration_id=registration_id)
    session.add(player)
    await session.commit()
    return {"event_id": quiz.event_id, "quiz_id": quiz.id, "title": quiz.title, "player_id": player.id, "player_key": player.player_key, "name": player.name}


async def _player(session, quiz: LiveQuizzes, player_key: str) -> LiveQuizPlayers:
    p = await session.scalar(select(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id, LiveQuizPlayers.player_key == (player_key or "")))
    if p is None:
        raise CustomHTTPException(401, message="You are not in this quiz. Join again with the code on the screen.")
    return p


async def player_state(session, event: Events, player_key: str, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    quiz = await quiz_of(session, event, create=False)
    if quiz is None:
        raise CustomHTTPException(404, message="This event has no quiz.")
    player = await _player(session, quiz, player_key)
    out = {"status": quiz.status.value, "title": quiz.title, "name": player.name, "score": player.score, "current_index": quiz.current_index, "total": len(quiz.questions), "question": None, "leaderboard": [], "rank": None}
    if quiz.status is QuizStatus.live and 0 <= quiz.current_index < len(quiz.questions):
        q = quiz.questions[quiz.current_index]
        left = _time_left(quiz, q, now)
        mine = await session.scalar(select(LiveQuizAnswers).where(LiveQuizAnswers.question_id == q.id, LiveQuizAnswers.player_id == player.id))
        out["question"] = {
            **_question_out(q, False), "time_left": left, "my_answer": mine.option_key if mine else None, "my_points": mine.points if mine else None,
            # The right answer is shown once the clock is out, or once this player has answered.
            "correct_key": q.correct_key if (left == 0 or mine is not None) else None,
        }
    if quiz.status is QuizStatus.ended or (quiz.status is QuizStatus.live and quiz.current_index >= 0):
        board = await _leaderboard(session, quiz, 10)
        out["leaderboard"] = board
        above = await session.scalar(select(func.count()).select_from(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id, LiveQuizPlayers.score > player.score)) or 0
        out["rank"] = above + 1
    return out


async def answer(session, event: Events, player_key: str, option_key: str, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    quiz = await quiz_of(session, event, create=False)
    if quiz is None or quiz.status is not QuizStatus.live or not (0 <= quiz.current_index < len(quiz.questions)):
        raise CustomHTTPException(409, message="No question is open right now.")
    player = await _player(session, quiz, player_key)
    q = quiz.questions[quiz.current_index]
    if option_key not in [o["key"] for o in q.options]:
        raise CustomHTTPException(422, message="That is not one of the answers.")
    elapsed = (now - quiz.question_opened_at).total_seconds() if quiz.question_opened_at else 0.0
    if elapsed > q.seconds + ANSWER_GRACE:
        raise CustomHTTPException(409, message="Time is up for this one.")
    if await session.scalar(select(LiveQuizAnswers.id).where(LiveQuizAnswers.question_id == q.id, LiveQuizAnswers.player_id == player.id)) is not None:
        raise CustomHTTPException(409, message="You already answered this one.")
    correct = option_key == q.correct_key
    # Half the points for being right, the other half for being quick about it.
    points = int(500 + 500 * max(0.0, 1 - min(elapsed, q.seconds) / q.seconds)) if correct else 0
    session.add(LiveQuizAnswers(question_id=q.id, player_id=player.id, option_key=option_key, correct=correct, points=points, answered_at=now))
    player.score += points
    await session.commit()
    return {"correct": correct, "points": points, "score": player.score, "correct_key": q.correct_key}


# --- Q&A ----------------------------------------------------------------------


def _question_row_out(r: LiveQuestions, voted: bool = False, mine: bool = False) -> dict:
    return {
        "id": r.id, "text": r.text, "author_name": r.author_name, "status": r.status.value, "pinned": r.pinned, "votes": r.votes,
        "created_at": r.created_at, "answered_at": r.answered_at, "voted": voted, "mine": mine,
    }


async def ask(session, event: Events, text_: str, author_name: Optional[str], voter_key: str, registration_id=None) -> dict:
    body = " ".join(text_.split())
    if len(body) < 3:
        raise CustomHTTPException(422, message="Ask a question with a few words in it.")
    if event.lifecycle in (EventStatus.closed, EventStatus.archived):
        raise CustomHTTPException(409, message="This event is over; the floor is closed.")
    row = LiveQuestions(
        event_id=event.id, text=body[:500], author_name=(" ".join((author_name or "").split())[:60] or None), author_key=voter_key, registration_id=registration_id,
        status=QuestionStatus.pending if event.qa_approve_first else QuestionStatus.approved,
    )
    session.add(row)
    await session.commit()
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
        for other in await _questions(session, event):
            other.pinned = False
        row.pinned, row.status = True, QuestionStatus.approved
    elif action == "unpin":
        row.pinned = False
    else:
        raise CustomHTTPException(422, message="Unknown action.")
    await session.commit()
    return _question_row_out(row)


async def vote(session, event: Events, question_id: UUID, voter_key: str) -> dict:
    row = await _question_in(session, event, question_id)
    existing = await session.scalar(select(LiveQuestionVotes).where(LiveQuestionVotes.question_id == row.id, LiveQuestionVotes.voter_key == voter_key))
    if existing is not None:
        await session.delete(existing)
        row.votes = max(0, row.votes - 1)
        voted = False
    else:
        session.add(LiveQuestionVotes(question_id=row.id, voter_key=voter_key))
        row.votes += 1
        voted = True
    await session.commit()
    return {"id": row.id, "votes": row.votes, "voted": voted}


# --- polls ----------------------------------------------------------------------


async def _responses(session, poll_id) -> list:
    return list((await session.scalars(select(LivePollResponses).where(LivePollResponses.poll_id == poll_id))).all())


def results_of(poll: LivePolls, responses: list) -> dict:
    n = len(responses)
    out: dict = {"responses": n}
    values = [r.value for r in responses]
    if poll.kind is PollKind.choice:
        c = Counter(values)
        out["choices"] = [{"label": o, "count": c.get(o, 0), "pct": round(100 * c.get(o, 0) / n) if n else 0} for o in poll.options]
    elif poll.kind is PollKind.words:
        c = Counter(values)
        out["words"] = [{"word": w, "count": k} for w, k in c.most_common(40)]
    else:
        nums = [int(v) for v in values if v.isdigit()]
        c = Counter(nums)
        out["rating"] = {"average": round(sum(nums) / len(nums), 2) if nums else None, "histogram": [{"value": v, "count": c.get(v, 0), "pct": round(100 * c.get(v, 0) / n) if n else 0} for v in range(1, 6)]}
    return out


async def poll_out(session, poll: LivePolls, voter_key: Optional[str] = None) -> dict:
    responses = await _responses(session, poll.id)
    out = {
        "id": poll.id, "kind": poll.kind.value, "prompt": poll.prompt, "options": list(poll.options or []), "status": poll.status.value,
        "launched_at": poll.launched_at, "closed_at": poll.closed_at, "sort_order": poll.sort_order, "results": results_of(poll, responses),
    }
    if voter_key is not None:
        mine = next((r.value for r in responses if r.voter_key == voter_key), None)
        out["my_response"] = mine
    return out


async def list_polls(session, event: Events) -> list:
    rows = list((await session.scalars(select(LivePolls).where(LivePolls.event_id == event.id).order_by(LivePolls.sort_order, LivePolls.created_at))).all())
    return [await poll_out(session, p) for p in rows]


def _check_poll(kind: PollKind, options: list) -> list:
    opts = [" ".join(o.split())[:120] for o in options if o and o.strip()]
    if kind is PollKind.choice:
        if not (2 <= len(opts) <= 8) or len(set(opts)) != len(opts):
            raise CustomHTTPException(422, message="A multiple-choice poll needs two to eight different answers.")
        return opts
    return []


async def create_poll(session, event: Events, body) -> dict:
    kind = PollKind(body.kind)
    top = await session.scalar(select(func.max(LivePolls.sort_order)).where(LivePolls.event_id == event.id))
    row = LivePolls(event_id=event.id, kind=kind, prompt=" ".join(body.prompt.split())[:300], options=_check_poll(kind, body.options), sort_order=(top if top is not None else -1) + 1)
    if not row.prompt:
        raise CustomHTTPException(422, message="Ask the room something.")
    session.add(row)
    await session.commit()
    return await poll_out(session, row)


async def _poll_in(session, event: Events, poll_id: UUID) -> LivePolls:
    row = await session.get(LivePolls, poll_id)
    if row is None or row.event_id != event.id:
        raise CustomHTTPException(404, message="That poll could not be found.")
    return row


async def update_poll(session, event: Events, poll_id: UUID, body) -> dict:
    row = await _poll_in(session, event, poll_id)
    data = body.model_dump(exclude_unset=True)
    if row.status is not PollStatus.draft and ("options" in data or "kind" in data):
        raise CustomHTTPException(409, message="A poll that has run keeps its answers; make a new one.")
    if "kind" in data and data["kind"]:
        row.kind = PollKind(data["kind"])
    if "options" in data and data["options"] is not None:
        row.options = _check_poll(row.kind, data["options"])
    if "prompt" in data and data["prompt"]:
        row.prompt = " ".join(data["prompt"].split())[:300]
    await session.commit()
    return await poll_out(session, row)


async def set_poll_status(session, event: Events, poll_id: UUID, status: str) -> dict:
    row = await _poll_in(session, event, poll_id)
    new = PollStatus(status)
    now = now_utc()
    if new is PollStatus.live:
        # One at a time: launching this one closes whatever is up.
        for other in (await session.scalars(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live, LivePolls.id != row.id))).all():
            other.status, other.closed_at = PollStatus.closed, now
        row.status, row.closed_at = PollStatus.live, None
        row.launched_at = row.launched_at or now
    elif new is PollStatus.closed:
        row.status, row.closed_at = PollStatus.closed, now
    else:
        raise CustomHTTPException(422, message="A poll is launched or closed; it does not go back to a draft.")
    await session.commit()
    return await poll_out(session, row)


async def delete_poll(session, event: Events, poll_id: UUID) -> None:
    row = await _poll_in(session, event, poll_id)
    await session.delete(row)
    await session.commit()


async def respond(session, event: Events, poll_id: UUID, voter_key: str, value: str) -> dict:
    row = await _poll_in(session, event, poll_id)
    if row.status is not PollStatus.live:
        raise CustomHTTPException(409, message="This poll is not open.")
    v = " ".join((value or "").split())
    if row.kind is PollKind.choice:
        if v not in row.options:
            raise CustomHTTPException(422, message="Pick one of the answers.")
    elif row.kind is PollKind.rating:
        if v not in {"1", "2", "3", "4", "5"}:
            raise CustomHTTPException(422, message="A rating is 1 to 5.")
    else:
        v = v.lower()[:40]
        if len(v) < 2:
            raise CustomHTTPException(422, message="A word or two.")
    existing = await session.scalar(select(LivePollResponses).where(LivePollResponses.poll_id == row.id, LivePollResponses.voter_key == voter_key))
    if existing is not None:
        existing.value = v
    else:
        session.add(LivePollResponses(poll_id=row.id, voter_key=voter_key, value=v))
    await session.commit()
    return await poll_out(session, row, voter_key)


# --- feedback ----------------------------------------------------------------------


def _survey_token(reg_id) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"feedback:{reg_id}".encode(), hashlib.sha256).hexdigest()[:32]


def survey_link(event: Events, reg: EventRegistrationsLink) -> str:
    return f"{settings.PUBLIC_SITE_URL.rstrip('/')}/feedback/{event.id}/{reg.id}?k={_survey_token(reg.id)}"


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
    return {"auto_send": survey.auto_send, "delay_minutes": survey.delay_minutes, "questions": list(survey.questions or []), "sent_at": survey.sent_at, "sent_count": survey.sent_count}


async def update_survey(session, event: Events, body) -> dict:
    survey = await survey_of(session, event)
    data = body.model_dump(exclude_unset=True)
    if "questions" in data and body.questions is not None:
        survey.questions = _check_questions(body.questions)
    if data.get("auto_send") is not None:
        survey.auto_send = data["auto_send"]
    if data.get("delay_minutes") is not None:
        survey.delay_minutes = max(0, min(7 * 24 * 60, data["delay_minutes"]))
    await session.commit()
    return survey_out(survey)


def _survey_email(event: Events, reg: EventRegistrationsLink, org: str) -> dict:
    link = survey_link(event, reg)
    first = (reg.full_name or "").split(" ")[0] or "there"
    text_ = f"Hi {first},\n\nThanks for coming to {event.name}. Two minutes of your thoughts would help the next one:\n\n{link}\n\n{org}"
    html = (
        f"<p>Hi {first},</p><p>Thanks for coming to <strong>{event.name}</strong>. Two minutes of your thoughts would help the next one.</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:10px 18px;background:#1f8f6b;color:#fff;border-radius:8px;text-decoration:none">Share feedback</a></p>'
        f"<p style=\"color:#666;font-size:12px\">Or open {link}</p><p>{org}</p>"
    )
    return {"subject": f"How was {event.name}?", "text": text_, "html": html}


async def send_survey(session, event: Events, now: Optional[datetime] = None) -> dict:
    """To everyone who was checked in; each gets a link only they hold. Sending again reaches only people not yet sent."""
    survey = await survey_of(session, event)
    regs = await audience_regs(session, event, Audience.checked_in)
    org = (event.club.name if getattr(event, "club", None) else None) or "The organisers"
    sent = 0
    for reg in regs:
        if not reg.email:
            continue
        mail = _survey_email(event, reg, org)
        try:
            await asyncio.to_thread(email_mod.send_email, recipients=[reg.email], subject=mail["subject"], text=mail["text"], html=mail["html"], sender_name=org)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("feedback survey email failed for registration %s", reg.id)
    survey.sent_at = now or now_utc()
    survey.sent_count = (survey.sent_count or 0) + sent
    await session.commit()
    return {"sent": sent, "invited": len(regs), **survey_out(survey)}


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
    return {**survey_out(survey), "sends_at": _auto_send_at(event, survey), "results": results, "previous": previous, "checked_in": counts["checked_in"]}


def _ends_at(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=event.duration or 0)


def _auto_send_at(event: Events, survey: FeedbackSurveys) -> Optional[datetime]:
    if not survey.auto_send or survey.sent_at is not None:
        return None
    return _ends_at(event) + timedelta(minutes=survey.delay_minutes or 0)


async def public_survey(session, event: Events, reg_id: UUID, token: str) -> dict:
    reg = await session.get(EventRegistrationsLink, reg_id)
    if reg is None or reg.event_id != event.id or not hmac.compare_digest(token or "", _survey_token(reg.id)):
        raise CustomHTTPException(404, message="This feedback link is not valid.")
    survey = await survey_of(session, event)
    done = await session.scalar(select(FeedbackResponses).where(FeedbackResponses.survey_id == survey.id, FeedbackResponses.registration_id == reg.id))
    return {"event_name": event.name, "club_name": event.club.name if getattr(event, "club", None) else "", "first_name": (reg.full_name or "").split(" ")[0], "questions": list(survey.questions or []), "submitted": done is not None}


async def submit_survey(session, event: Events, reg_id: UUID, token: str, answers: dict) -> dict:
    reg = await session.get(EventRegistrationsLink, reg_id)
    if reg is None or reg.event_id != event.id or not hmac.compare_digest(token or "", _survey_token(reg.id)):
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
    row = await session.scalar(select(FeedbackResponses).where(FeedbackResponses.survey_id == survey.id, FeedbackResponses.registration_id == reg.id))
    if row is None:
        session.add(FeedbackResponses(survey_id=survey.id, registration_id=reg.id, answers=clean))
    else:
        row.answers = clean
    await session.commit()
    return {"submitted": True}


async def sweep(session, now: Optional[datetime] = None) -> int:
    """Every minute: surveys whose moment has come go out."""
    now = now or now_utc()
    due = (await session.execute(
        select(FeedbackSurveys, Events).join(Events, Events.id == FeedbackSurveys.event_id).options(joinedload(Events.club))
        .where(FeedbackSurveys.auto_send == True, FeedbackSurveys.sent_at.is_(None), Events.is_deleted == False, Events.lifecycle != EventStatus.draft)  # noqa: E712
    )).all()
    sent = 0
    for survey, event in due:
        if _ends_at(event) + timedelta(minutes=survey.delay_minutes or 0) > now:
            continue
        try:
            await send_survey(session, event, now)
            sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("feedback auto-send failed for event %s", event.id)
            await session.rollback()
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


def update_out(u: LiveUpdates) -> dict:
    return {"id": u.id, "kind": u.kind.value, "text": u.text, "audience": u.audience.value, "recipients": u.recipients, "pushed": u.pushed, "sent_at": u.sent_at}


async def push_update(session, event: Events, kind: str, text_: str, audience: str, by=None) -> dict:
    body = " ".join((text_ or "").split())
    if len(body) < 3:
        raise CustomHTTPException(422, message="Say what changed.")
    aud = Audience(audience)
    regs = await audience_regs(session, event, aud)
    user_ids = sorted({r.user_id for r in regs if r.user_id})
    row = LiveUpdates(event_id=event.id, kind=UpdateKind(kind), text=body[:500], audience=aud, recipients=len(regs), sent_by=getattr(by, "id", None), sent_at=now_utc())
    session.add(row)
    await session.commit()
    pushed = 0
    if user_ids:
        from app.api.notifications.service import create_notifications_batch
        from app.core.notifications.service import send_notification_to_users

        data = {"type": UPDATE_TYPE, "event_id": str(event.id), "kind": row.kind.value}
        try:
            await create_notifications_batch(session, user_ids, event.name, body, UPDATE_TYPE, data=data, from_club_id=event.club_id, event_id=event.id)
        except Exception:  # noqa: BLE001
            logger.exception("in-app notifications failed for live update %s", row.id)
            await session.rollback()
        try:
            pushed = await send_notification_to_users(session, user_ids, event.name, body, data=data)
        except Exception:  # noqa: BLE001
            logger.exception("push failed for live update %s", row.id)
    row.pushed = pushed or 0
    await session.commit()
    return update_out(row)


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
    update = await push_update(session, event, "schedule", text_, "checked_in", by)
    return {"update": update, "items": [{"id": r.id, "title": r.title, "starts_at": r.starts_at, "ends_at": r.ends_at} for r in rows]}


# --- what the room sees ----------------------------------------------------------------------


async def feed(session, event: Events, voter_key: Optional[str], now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    updates = (await session.scalars(select(LiveUpdates).where(LiveUpdates.event_id == event.id).order_by(LiveUpdates.sent_at.desc()).limit(10))).all()
    banner = updates[0] if updates and now - updates[0].sent_at <= BANNER_FOR else None
    live_poll = await session.scalar(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live))
    questions = [r for r in await _questions(session, event) if r.status in (QuestionStatus.approved, QuestionStatus.answered)]
    voted: set = set()
    if voter_key and questions:
        voted = set((await session.scalars(select(LiveQuestionVotes.question_id).where(LiveQuestionVotes.voter_key == voter_key, LiveQuestionVotes.question_id.in_([q.id for q in questions])))).all())
    quiz = await quiz_of(session, event, create=False)
    return {
        "event": {"id": event.id, "name": event.name, "status": (event.lifecycle or EventStatus.published).value},
        "banner": update_out(banner) if banner else None,
        "updates": [update_out(u) for u in updates],
        "poll": await poll_out(session, live_poll, voter_key) if live_poll else None,
        "questions": [_question_row_out(q, voted=q.id in voted, mine=bool(voter_key) and q.author_key == voter_key) for q in questions],
        "qa": {"approve_first": bool(event.qa_approve_first), "open": event.lifecycle not in (EventStatus.closed, EventStatus.archived)},
        "quiz": {"live": bool(quiz and quiz.status is QuizStatus.live), "title": quiz.title if quiz else None},
    }


async def screen(session, event: Events, now: Optional[datetime] = None) -> dict:
    """The big screen: the pinned question, the board, the poll up now, the quiz if it runs, the last update."""
    now = now or now_utc()
    rows = await _questions(session, event)
    pinned = next((r for r in rows if r.pinned), None)
    board = [r for r in rows if r.status is QuestionStatus.approved and not r.pinned][:5]
    live_poll = await session.scalar(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live))
    quiz = await quiz_of(session, event, create=False)
    latest = await session.scalar(select(LiveUpdates).where(LiveUpdates.event_id == event.id).order_by(LiveUpdates.sent_at.desc()).limit(1))
    return {
        "event": {"id": event.id, "name": event.name},
        "pinned": _question_row_out(pinned) if pinned else None,
        "board": [_question_row_out(r) for r in board],
        "poll": await poll_out(session, live_poll) if live_poll else None,
        "quiz": await host_state(session, quiz, now) if quiz and quiz.status is QuizStatus.live else None,
        "update": update_out(latest) if latest and now - latest.sent_at <= BANNER_FOR else None,
    }
