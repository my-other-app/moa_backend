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
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from app.api.agenda.models import AgendaItems
from app.api.checkins.models import CheckIns
from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.documents import EVENT_TIMEZONE
from app.api.events.models import EventRegistrationsLink, Events
from app.api.live import board, bus, room
from app.api.live.models import (
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


def voter_key_of(request) -> str:
    key = room.valid_key(request.headers.get(VOTER_HEADER))
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


async def quiz_of(session, event: Events, create: bool = True) -> Optional[LiveQuizzes]:
    quiz = await session.scalar(select(LiveQuizzes).where(LiveQuizzes.event_id == event.id))
    if quiz is None and create:
        quiz = LiveQuizzes(event_id=event.id, title=f"{event.name} quiz")
        session.add(quiz)
        await session.commit()
        await session.refresh(quiz)
    return quiz


def _question_out(q, with_answer: bool) -> dict:
    get = q.get if isinstance(q, dict) else (lambda k: getattr(q, k))
    out = {"id": get("id"), "prompt": get("prompt"), "options": list(get("options") or []), "seconds": get("seconds"), "sort_order": get("sort_order")}
    if with_answer:
        out["correct_key"] = get("correct_key")
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
    room.quiz_changed(quiz.event_id)
    room.room_changed(quiz.event_id)
    return await quiz_out(session, quiz)


async def add_question(session, quiz: LiveQuizzes, body) -> dict:
    options = [{"key": o.key, "label": o.label.strip()} for o in body.options]
    _check_options(options, body.correct_key)
    top = max((q.sort_order for q in quiz.questions), default=-1)
    row = LiveQuizQuestions(quiz_id=quiz.id, prompt=body.prompt.strip(), options=options, correct_key=body.correct_key, seconds=body.seconds, sort_order=top + 1)
    session.add(row)
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)
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
    room.quiz_changed(quiz.event_id)
    return _question_out(row, True)


async def delete_question(session, quiz: LiveQuizzes, question_id: UUID) -> None:
    row = await _question_of(session, quiz, question_id)
    await session.delete(row)
    await session.commit()
    await session.refresh(quiz)
    room.quiz_changed(quiz.event_id)


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


async def start_quiz(session, quiz: LiveQuizzes) -> dict:
    """Opens the lobby. A quiz run before starts clean: last time's players and answers go."""
    if not quiz.questions:
        raise CustomHTTPException(422, message="Add at least one question before hosting.")
    await session.execute(delete(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))
    quiz.status, quiz.current_index, quiz.question_opened_at, quiz.ended_at = QuizStatus.live, -1, None, None
    quiz.join_code = await _fresh_code(session)
    await session.commit()
    return await _moved(session, quiz)


async def next_question(session, quiz: LiveQuizzes) -> dict:
    if quiz.status is not QuizStatus.live:
        raise CustomHTTPException(409, message="The quiz is not running.")
    if quiz.current_index + 1 >= len(quiz.questions):
        return await end_quiz(session, quiz)
    quiz.current_index += 1
    quiz.question_opened_at = now_utc()
    await session.commit()
    return await _moved(session, quiz)


async def end_quiz(session, quiz: LiveQuizzes) -> dict:
    quiz.status, quiz.ended_at, quiz.question_opened_at = QuizStatus.ended, now_utc(), None
    quiz.join_code = None
    await session.commit()
    return await _moved(session, quiz)


async def reset_quiz(session, quiz: LiveQuizzes) -> dict:
    """Back to a draft: questions stay, the run is forgotten."""
    await session.execute(delete(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id))
    quiz.status, quiz.current_index, quiz.question_opened_at, quiz.ended_at, quiz.join_code = QuizStatus.draft, -1, None, None, None
    await session.commit()
    return await _moved(session, quiz)


def _time_left(opened_at: Optional[datetime], seconds: int, now: datetime) -> int:
    if opened_at is None:
        return seconds
    return max(0, int(seconds - (now - opened_at).total_seconds()))


async def _tally(session, q: LiveQuizQuestions) -> dict:
    rows = (await session.execute(select(LiveQuizAnswers.option_key, func.count()).where(LiveQuizAnswers.question_id == q.id).group_by(LiveQuizAnswers.option_key))).all()
    by = dict(rows)
    return {"answered": sum(by.values()), "counts": [{"key": o["key"], "label": o["label"], "count": by.get(o["key"], 0), "correct": o["key"] == q.correct_key} for o in q.options]}


async def _leaderboard(session, quiz: LiveQuizzes, limit: int = 10) -> list:
    """The top of the board, read through ix_live_quiz_players_board (for a quiz the room cannot see, a draft's)."""
    rows = (await session.execute(
        select(LiveQuizPlayers.name, LiveQuizPlayers.score).where(LiveQuizPlayers.quiz_id == quiz.id)
        .order_by(LiveQuizPlayers.score.desc(), LiveQuizPlayers.created_at).limit(limit)
    )).all()
    return [{"rank": i + 1, "name": name, "score": score} for i, (name, score) in enumerate(rows)]


async def host_state(session, quiz: LiveQuizzes, now: Optional[datetime] = None) -> dict:
    """What the host (and the big screen) sees: players and the board from
    the room's quiz snapshot, the open question's tally counted afresh."""
    now = now or now_utc()
    current = quiz.questions[quiz.current_index] if quiz.status is QuizStatus.live and 0 <= quiz.current_index < len(quiz.questions) else None
    view = await room.quiz_view(session, quiz.event_id)
    out = {
        "status": quiz.status.value, "join_code": quiz.join_code, "title": quiz.title,
        "players": view.player_count if view is not None else await _player_count(session, quiz.id),
        "current_index": quiz.current_index, "total": len(quiz.questions), "question": None,
        "leaderboard": list(view.board) if view is not None else await _leaderboard(session, quiz),
    }
    if current is not None:
        out["question"] = {**_question_out(current, True), "time_left": _time_left(quiz.question_opened_at, current.seconds, now), "opened_at": quiz.question_opened_at, **(await _tally(session, current))}
    return out


async def join_quiz(session, code: str, name: str, registration_id=None) -> dict:
    quiz = await session.scalar(
        select(LiveQuizzes).join(Events, Events.id == LiveQuizzes.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(LiveQuizzes.join_code == code.strip(), LiveQuizzes.status == QuizStatus.live, *room.visible())
    )
    if quiz is None:
        raise CustomHTTPException(404, message="No quiz is running with that code. Check the screen and try again.")
    name = " ".join(name.split())[:60]
    if not name:
        raise CustomHTTPException(422, message="Tell the room your name.")
    player = LiveQuizPlayers(quiz_id=quiz.id, name=name, player_key=secrets.token_urlsafe(24), registration_id=registration_id, score=0)
    session.add(player)
    await session.commit()
    met = room.Player(id=player.id, name=player.name, score=0, created_at=player.created_at)
    # In the snapshot at once, so the new player's first poll finds them.
    view = room.quiz_kept(quiz.event_id)
    if view is not None and view.quiz_id == quiz.id:
        view.players[player.player_key] = met
    run = board.token(quiz.status, quiz.join_code, quiz.ended_at)
    if run is not None and board.on():
        await board.joined(board.key(quiz.id, run), met.member())
    bus.changed(quiz.event_id, "quiz")
    return {"event_id": quiz.event_id, "quiz_id": quiz.id, "title": quiz.title, "player_id": player.id, "player_key": player.player_key, "name": player.name}


async def _player(session, view: "room.QuizView", player_key: str) -> "room.Player":
    """The player behind a key: from the snapshot, or (joined since it was
    made, or on another worker) one probe of the unique key."""
    p = view.players.get(player_key or "")
    if p is not None:
        return p
    row = (await session.execute(
        select(LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.score, LiveQuizPlayers.created_at)
        .where(LiveQuizPlayers.quiz_id == view.quiz_id, LiveQuizPlayers.player_key == (player_key or ""))
    )).first()
    if row is None:
        raise CustomHTTPException(401, message="You are not in this quiz. Join again with the code on the screen.")
    p = room.Player(id=row[0], name=row[1], score=row[2], created_at=row[3])
    view.players[player_key] = p
    return p


async def _view_for(session, event_id: int) -> "room.QuizView":
    view = await room.quiz_view(session, event_id)
    if view is None:
        raise CustomHTTPException(404, message="This event has no quiz.")
    return view


async def player_state(session, event_id: int, player_key: str, now: Optional[datetime] = None) -> dict:
    """A player's poll: read from the quiz snapshot, no statement unless the
    player is new to it (with the board in Redis, one round trip there for
    their score and rank)."""
    view = await _view_for(session, event_id)
    player = await _player(session, view, player_key)
    standing = (await room.standings(session, view, [player]))[player.id]
    return player_body(view, player, standing, now or now_utc())


def player_body(view: "room.QuizView", player: "room.Player", standing: tuple, now: datetime) -> dict:
    """What a player sees (PlayerState), from the snapshot and their
    (score, rank): the poll's body, and the quiz stream's."""
    score, rank = standing
    out = {"status": view.status.value, "title": view.title, "name": player.name, "score": score, "current_index": view.current_index, "total": len(view.questions), "question": None, "leaderboard": [], "rank": None}
    q = view.current()
    if q is not None:
        left = _time_left(view.opened_at, q["seconds"], now)
        mine = view.answers.get(player.id)
        out["question"] = {
            **_question_out(q, False), "time_left": left, "my_answer": mine[0] if mine else None, "my_points": mine[1] if mine else None,
            # The right answer is shown once the clock is out, or once this player has answered.
            "correct_key": q["correct_key"] if (left == 0 or mine is not None) else None,
        }
    if view.status is QuizStatus.ended or (view.status is QuizStatus.live and view.current_index >= 0):
        out["leaderboard"] = list(view.board)
        out["rank"] = rank
    return out


async def answer(session, event_id: int, player_key: str, option_key: str, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    view = await room.quiz_view(session, event_id)
    q = view.current() if view is not None else None
    if q is None:
        raise CustomHTTPException(409, message="No question is open right now.")
    player = await _player(session, view, player_key)
    if option_key not in [o["key"] for o in q["options"]]:
        raise CustomHTTPException(422, message="That is not one of the answers.")
    elapsed = (now - view.opened_at).total_seconds() if view.opened_at else 0.0
    if elapsed > q["seconds"] + ANSWER_GRACE:
        raise CustomHTTPException(409, message="Time is up for this one.")
    if player.id in view.answers:
        raise CustomHTTPException(409, message="You already answered this one.")
    correct = option_key == q["correct_key"]
    # Half the points for being right, the other half for being quick about it.
    points = int(500 + 500 * max(0.0, 1 - min(elapsed, q["seconds"]) / q["seconds"])) if correct else 0
    # Once per player and question, whatever arrives at once: the second of a double tap is told so.
    took = await session.scalar(
        pg_insert(LiveQuizAnswers)
        .values(question_id=q["id"], player_id=player.id, option_key=option_key, correct=correct, points=points, answered_at=now)
        .on_conflict_do_nothing(index_elements=["question_id", "player_id"])
        .returning(LiveQuizAnswers.id)
    )
    if took is None:
        raise CustomHTTPException(409, message="You already answered this one.")
    score = await session.scalar(update(LiveQuizPlayers).where(LiveQuizPlayers.id == player.id).values(score=LiveQuizPlayers.score + points).returning(LiveQuizPlayers.score))
    await session.commit()
    view.answers[player.id] = (option_key, points)
    player.score = int(score)
    if view.board_key is not None:
        await board.scored(view.board_key, player.member(), player.score)
    bus.changed(event_id, "quiz")
    return {"correct": correct, "points": points, "score": player.score, "correct_key": q["correct_key"]}


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
    out = await room.poll_public(session, poll)
    if voter_key is not None:
        out["my_response"] = await session.scalar(select(LivePollResponses.value).where(LivePollResponses.poll_id == poll.id, LivePollResponses.voter_key == voter_key))
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
    # Not on the room's feed while a draft; on the console's list of polls.
    room.room_changed(event.id)
    return await poll_out(session, row)


async def _poll_in(session, event_id: int, poll_id: UUID) -> LivePolls:
    row = await session.get(LivePolls, poll_id)
    if row is None or row.event_id != event_id:
        raise CustomHTTPException(404, message="That poll could not be found.")
    return row


async def update_poll(session, event: Events, poll_id: UUID, body) -> dict:
    row = await _poll_in(session, event.id, poll_id)
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


def _clean_value(kind: PollKind, options, value: str) -> str:
    v = " ".join((value or "").split())
    if kind is PollKind.choice:
        if v not in options:
            raise CustomHTTPException(422, message="Pick one of the answers.")
    elif kind is PollKind.rating:
        if v not in {"1", "2", "3", "4", "5"}:
            raise CustomHTTPException(422, message="A rating is 1 to 5.")
    else:
        v = v.lower()[:40]
        if len(v) < 2:
            raise CustomHTTPException(422, message="A word or two.")
    return v


async def respond(session, event_id: int, poll_id: UUID, voter_key: str, value: str) -> dict:
    """Answer the poll that is up, or change the answer: one upsert. What
    comes back is the room's view of the poll (its counts at most a second
    old) with this browser's answer on it."""
    snap = await room.room_of(session, event_id)
    if snap.poll_id == str(poll_id):
        kind, options = snap.poll_kind, snap.poll_options
    else:  # launched since the snapshot, or not up at all
        row = await _poll_in(session, event_id, poll_id)
        if row.status is not PollStatus.live:
            raise CustomHTTPException(409, message="This poll is not open.")
        # Read afresh below; the streams are told once the answer commits.
        room.forget_room(event_id)
        kind, options = row.kind, tuple(row.options or ())
    v = _clean_value(kind, options, value)
    # created_at from the clock, not the transaction: a word cloud's ties go to the word given first.
    stmt = pg_insert(LivePollResponses).values(poll_id=poll_id, voter_key=voter_key, value=v, created_at=now_utc())
    await session.execute(stmt.on_conflict_do_update(index_elements=["poll_id", "voter_key"], set_={"value": stmt.excluded.value}))
    await session.commit()
    kept = room.voter_kept(event_id, voter_key)
    if kept is not None:
        kept.answers[str(poll_id)] = v
    bus.changed(event_id, "room", delta=["r", voter_key, str(poll_id), v])
    snap = await room.room_of(session, event_id)
    poll = snap.data.get("poll")
    if poll is None or snap.poll_id != str(poll_id):
        return {**await poll_out(session, await _poll_in(session, event_id, poll_id)), "my_response": v}
    return {**poll, "my_response": v}


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


def _survey_mail_key(survey_id, registration_id) -> str:
    return f"live:survey:{survey_id}:{registration_id}"


async def send_survey(session, event: Events, now: Optional[datetime] = None) -> dict:
    """To everyone who was checked in and has not been sent it yet; each gets
    a link only they hold. The recipients are claimed (feedback_sends) in the
    same transaction that marks the survey sent, so sending again, or the
    sweep and a send at once, reaches only the people not sent yet. Each
    claimed guest's mail is a job of its own (SURVEY_MAIL), written in that
    transaction: it survives a restart, is retried when the provider does not
    take it, and marks the guest mailed (feedback_sends.mailed_at) once it
    went, so a job run twice mails nobody twice."""
    survey = await survey_of(session, event)
    everyone = await audience_regs(session, event, Audience.checked_in)
    regs = [r for r in everyone if r.email]
    claimed: list = []
    if regs:
        claimed = list((await session.scalars(
            pg_insert(FeedbackSends)
            .values([{"survey_id": survey.id, "registration_id": r.id} for r in regs])
            .on_conflict_do_nothing(index_elements=["survey_id", "registration_id"])
            .returning(FeedbackSends.registration_id)
        )).all())
    survey.sent_at = now or now_utc()
    survey.sent_count = (survey.sent_count or 0) + len(claimed)
    await jobs.enqueue_many(session, SURVEY_MAIL, [
        ({"survey_id": survey.id, "registration_id": reg_id}, _survey_mail_key(survey.id, reg_id)) for reg_id in claimed
    ])
    await session.commit()
    return {"sent": len(claimed), "invited": len(everyone), **survey_out(survey)}


@jobs.handler(SURVEY_MAIL, max_attempts=6, timeout=60.0)
async def _survey_mail_job(payload: dict) -> None:
    await mail_survey(UUID(str(payload["survey_id"])), UUID(str(payload["registration_id"])))


async def mail_survey(survey_id: UUID, registration_id: UUID) -> bool:
    """One guest's survey mail, unless it went already. True when it went now.

    Read, send, then mark: a run cut off between the send and the mark sends
    it again on the retry -- rather that than a mail claimed and never sent --
    and the provider is given the same idempotency key both times, so one
    that keeps them (Resend) delivers it once. A provider that does not take
    it raises here, and the job is tried again later."""
    async with jobs.scope() as s:
        row = (await s.execute(
            select(FeedbackSends.mailed_at, EventRegistrationsLink, Events)
            .join(EventRegistrationsLink, EventRegistrationsLink.id == FeedbackSends.registration_id)
            .join(FeedbackSurveys, FeedbackSurveys.id == FeedbackSends.survey_id)
            .join(Events, Events.id == FeedbackSurveys.event_id)
            .options(joinedload(Events.club))
            .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id)
        )).first()
        # End the read before the provider's call: no pooled connection held meanwhile.
        await s.commit()
    if row is None or row[0] is not None or not row[1].email:
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
            .where(FeedbackSends.survey_id == survey_id, FeedbackSends.registration_id == registration_id, FeedbackSends.mailed_at.is_(None))
            .values(mailed_at=now_utc())
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
    """Every minute: surveys whose moment has come go out."""
    now = now or now_utc()
    due = (await session.execute(due_query(now))).all()
    sent = 0
    for _survey, event in due:
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


update_out = room.update_out


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
    room.room_changed(event.id)
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
    update_ = await push_update(session, event, "schedule", text_, "checked_in", by)
    return {"update": update_, "items": [{"id": r.id, "title": r.title, "starts_at": r.starts_at, "ends_at": r.ends_at} for r in rows]}


# --- what the room sees ----------------------------------------------------------------------


async def screen(session, event: Events, now: Optional[datetime] = None) -> dict:
    """The big screen: the pinned question, the board, the poll up now, the quiz if it runs, the last update."""
    now = now or now_utc()
    rows = list((await session.scalars(
        select(LiveQuestions).where(LiveQuestions.event_id == event.id, LiveQuestions.status == QuestionStatus.approved)
        .order_by(LiveQuestions.pinned.desc(), LiveQuestions.votes.desc(), LiveQuestions.created_at).limit(6)
    )).all())
    pinned = next((r for r in rows if r.pinned), None)
    board = [r for r in rows if not r.pinned][:5]
    live_poll = await session.scalar(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live).limit(1))
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
