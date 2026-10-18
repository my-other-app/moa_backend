"""What the room reads, kept a second at a time.

Every phone in the room polls: the feed every five seconds (and every event
page every thirty), the quiz every two. Rebuilt per poll, the feed was a
dozen statements and every response of the poll that is up read as rows,
and each quiz poll ordered every player by score and counted the players
above this one, so the work of a poll grew with the room and the room's work
with its square: about 500 people in a room, or 150 in a quiz, filled one
worker.

Now, per event and per quiz, one snapshot of what is the same for everyone
is made at most once a second (concurrent misses wait for the one build;
discover.cache's TTLCache), and a poll reads it:

- the room (`Room`): the event's state, the banner and updates, the poll that
  is up with its results counted by GROUP BY, the questions on the board,
  whether a quiz runs, whether the floor is open. Kept with its JSON already
  made, so a poll from someone who has voted for nothing, asked nothing and
  answered no poll is that body as it stands;
- what one browser did (`Voter`: the questions it voted for, its answers to
  polls), read once and kept thirty seconds, and kept current by that
  browser's own votes and answers in this process. Only a browser that did
  something gets a body of its own, its flags written into a copy;
- the quiz (`QuizView`): its state and questions, every player by key with
  their score, the scores in order, the top ten, and the open question's
  answers. A player's poll is then dictionary lookups and a binary search
  for their rank, with no statement; an answer or a join updates the view in
  place, so the phone that answered sees it on its next poll.

What changes for everyone (the host's next question, a moderated question, a
poll launched) forgets the snapshot at once; what the audience does (votes,
answers) shows within the second. With more than one worker, another
worker's snapshot is at most a second old.

Server-sent events would go further: one connection per phone, the snapshot
pushed when it changes instead of a request per phone per poll. Each poll
still costs a request's parsing, routing and response here (a few hundred
microseconds of Python at best), which is what bounds one worker now.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import orjson
from sqlalchemy import func, select

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.discover.cache import _MISSING, TTLCache
from app.api.events.models import Events
from app.api.live.models import (
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
)
from app.response import CustomHTTPException

#: How long a snapshot is kept.
SNAPSHOT_SECONDS = 1.0
#: How long what one browser did is kept without a read.
VOTER_SECONDS = 30.0
#: The most questions the room's feed carries: pinned, then the most voted.
FEED_QUESTIONS = 200
BANNER_FOR = timedelta(hours=3)
#: The words a word cloud shows.
CLOUD_WORDS = 40

rooms = TTLCache(ttl=SNAPSHOT_SECONDS, maxsize=4096)
quizzes = TTLCache(ttl=SNAPSHOT_SECONDS, maxsize=1024)
voters = TTLCache(ttl=VOTER_SECONDS, maxsize=200_000)

_NONE = "none"  # a snapshot of "there is nothing here", kept like any other


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─── Who may see the room ─────────────────────────────────────────────────


def visible() -> list:
    """An event the room may be opened for: not deleted, not a draft (shown
    or held), and its club listed (verified, not deleted, not under a
    blocked organisation). Needs Clubs joined."""
    return [
        Events.is_deleted == False,  # noqa: E712
        Events.lifecycle != EventStatus.draft,
        Clubs.listed == True,  # noqa: E712
    ]


async def public_event(session, event_id: int) -> Events:
    """The event, as the room's public side may see it; 404 otherwise."""
    from sqlalchemy.orm import contains_eager

    event = await session.scalar(
        select(Events).join(Clubs, Clubs.id == Events.club_id).options(contains_eager(Events.club))
        .where(Events.id == event_id, *visible())
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


# ─── Polls' results, counted in the database ──────────────────────────────


async def counts_of(session, poll_id) -> tuple[int, list[tuple[str, int]]]:
    """(responses, [(value, count)]): most answered first, ties in the order
    each value was first given."""
    first = func.min(LivePollResponses.created_at)
    n = func.count()
    rows = (await session.execute(
        select(LivePollResponses.value, n, first)
        .where(LivePollResponses.poll_id == poll_id)
        .group_by(LivePollResponses.value)
        .order_by(n.desc(), first, LivePollResponses.value)
    )).all()
    return sum(int(c) for _, c, _ in rows), [(v, int(c)) for v, c, _ in rows]


def results_of(poll: LivePolls, total: int, counts: list[tuple[str, int]]) -> dict:
    out: dict = {"responses": total}
    by = dict(counts)
    if poll.kind is PollKind.choice:
        out["choices"] = [{"label": o, "count": by.get(o, 0), "pct": round(100 * by.get(o, 0) / total) if total else 0} for o in poll.options]
    elif poll.kind is PollKind.words:
        out["words"] = [{"word": w, "count": k} for w, k in counts[:CLOUD_WORDS]]
    else:
        nums = {int(v): k for v, k in counts if v.isdigit()}
        rated = sum(nums.values())
        out["rating"] = {
            "average": round(sum(v * k for v, k in nums.items()) / rated, 2) if rated else None,
            "histogram": [{"value": v, "count": nums.get(v, 0), "pct": round(100 * nums.get(v, 0) / total) if total else 0} for v in range(1, 6)],
        }
    return out


async def poll_public(session, poll: LivePolls) -> dict:
    total, counts = await counts_of(session, poll.id)
    return {
        "id": poll.id, "kind": poll.kind.value, "prompt": poll.prompt, "options": list(poll.options or []), "status": poll.status.value,
        "launched_at": poll.launched_at, "closed_at": poll.closed_at, "sort_order": poll.sort_order, "results": results_of(poll, total, counts),
    }


# ─── The room ─────────────────────────────────────────────────────────────


@dataclass
class Room:
    event_id: int
    club_id: Optional[int]
    lifecycle: EventStatus
    approve_first: bool
    #: The feed as anyone sees it, ready to send, and as a JSON-safe dict to copy from.
    body: bytes
    data: dict
    #: Question ids (as text) on the feed, and who asked each (the browser's key).
    question_ids: frozenset
    asked_by: dict[str, list[str]]
    #: The poll that is up: its id as text, its kind and choices.
    poll_id: Optional[str] = None
    poll_kind: Optional[PollKind] = None
    poll_options: tuple = ()


def update_out(u: LiveUpdates) -> dict:
    return {"id": u.id, "kind": u.kind.value, "text": u.text, "audience": u.audience.value, "recipients": u.recipients, "pushed": u.pushed, "sent_at": u.sent_at}


def question_out(r, voted: bool = False, mine: bool = False) -> dict:
    return {
        "id": r.id, "text": r.text, "author_name": r.author_name, "status": r.status.value, "pinned": r.pinned, "votes": r.votes,
        "created_at": r.created_at, "answered_at": r.answered_at, "voted": voted, "mine": mine,
    }


async def _build_room(session, event_id: int):
    from app.api.live.schemas import Feed
    from app.api.subscriptions import gates

    event = await session.scalar(select(Events).join(Clubs, Clubs.id == Events.club_id).where(Events.id == event_id, *visible()))
    if event is None:
        return _NONE
    now = now_utc()
    updates = list((await session.scalars(select(LiveUpdates).where(LiveUpdates.event_id == event.id).order_by(LiveUpdates.sent_at.desc()).limit(10))).all())
    banner = updates[0] if updates and now - updates[0].sent_at <= BANNER_FOR else None
    poll = await session.scalar(select(LivePolls).where(LivePolls.event_id == event.id, LivePolls.status == PollStatus.live).order_by(LivePolls.launched_at.desc()).limit(1))
    rows = (await session.execute(
        select(LiveQuestions, LiveQuestions.author_key)
        .where(LiveQuestions.event_id == event.id, LiveQuestions.status.in_([QuestionStatus.approved, QuestionStatus.answered]))
        .order_by(LiveQuestions.pinned.desc(), LiveQuestions.votes.desc(), LiveQuestions.created_at)
        .limit(FEED_QUESTIONS)
    )).all()
    quiz = (await session.execute(select(LiveQuizzes.status, LiveQuizzes.title).where(LiveQuizzes.event_id == event.id))).first()
    qa_open = event.lifecycle not in (EventStatus.closed, EventStatus.archived)
    # The floor shows as closed when the club's plan has no Q&A (and the event
    # is not grandfathered), so the ask box never appears only to refuse.
    if qa_open and not await gates.allows_for_feed(session, event.club_id, "qa", event=event):
        qa_open = False
    public = {
        "event": {"id": event.id, "name": event.name, "status": (event.lifecycle or EventStatus.published).value},
        "banner": update_out(banner) if banner else None,
        "updates": [update_out(u) for u in updates],
        "poll": {**await poll_public(session, poll), "my_response": None} if poll else None,
        "questions": [question_out(q) for q, _ in rows],
        "qa": {"approve_first": bool(event.qa_approve_first), "open": qa_open},
        "quiz": {"live": bool(quiz and quiz[0] is QuizStatus.live), "title": quiz[1] if quiz else None},
    }
    data = Feed(**public).model_dump(mode="json")
    asked_by: dict[str, list[str]] = {}
    for q, author in rows:
        asked_by.setdefault(author, []).append(str(q.id))
    return Room(
        event_id=event.id, club_id=event.club_id, lifecycle=event.lifecycle, approve_first=bool(event.qa_approve_first),
        body=orjson.dumps(data), data=data, question_ids=frozenset(str(q.id) for q, _ in rows), asked_by=asked_by,
        poll_id=str(poll.id) if poll else None, poll_kind=poll.kind if poll else None, poll_options=tuple(poll.options or ()) if poll else (),
    )


async def room_of(session, event_id: int) -> Room:
    """The event's room snapshot; 404 for an event the room may not be opened for."""
    room = await rooms.remember(("room", event_id), lambda: _build_room(session, event_id))
    if room is _NONE:
        raise CustomHTTPException(404, message="Event not found")
    return room


def room_changed(event_id: int) -> None:
    """Something everyone sees changed: the next poll rebuilds the room."""
    rooms.forget(("room", event_id))


# ─── One browser in the room ──────────────────────────────────────────────


@dataclass
class Voter:
    voted: set = field(default_factory=set)
    #: poll id (text) -> the answer given
    answers: dict = field(default_factory=dict)


async def _load_voter(session, event_id: int, key: str) -> Voter:
    voted = (await session.scalars(
        select(LiveQuestionVotes.question_id).join(LiveQuestions, LiveQuestions.id == LiveQuestionVotes.question_id)
        .where(LiveQuestionVotes.voter_key == key, LiveQuestions.event_id == event_id)
    )).all()
    answers = (await session.execute(
        select(LivePollResponses.poll_id, LivePollResponses.value).join(LivePolls, LivePolls.id == LivePollResponses.poll_id)
        .where(LivePollResponses.voter_key == key, LivePolls.event_id == event_id)
    )).all()
    return Voter(voted={str(q) for q in voted}, answers={str(p): v for p, v in answers})


async def voter_of(session, event_id: int, key: str) -> Voter:
    return await voters.remember(("voter", event_id, key), lambda: _load_voter(session, event_id, key))


def voter_kept(event_id: int, key: str) -> Optional[Voter]:
    """What is kept of this browser in this process, if anything (to update in place)."""
    got = voters.get(("voter", event_id, key))
    return None if got is _MISSING else got


def valid_key(key: Optional[str]) -> Optional[str]:
    key = (key or "").strip()
    if 8 <= len(key) <= 64 and key.replace("-", "").replace("_", "").isalnum():
        return key
    return None


async def feed_body(session, event_id: int, key: Optional[str]) -> bytes:
    """The feed for one browser: the room's body, or a copy with its own flags."""
    room = await room_of(session, event_id)
    key = valid_key(key)
    if key is None:
        return room.body
    voter = await voter_of(session, event_id, key)
    voted = voter.voted & room.question_ids
    mine = set(room.asked_by.get(key, ()))
    answer = voter.answers.get(room.poll_id) if room.poll_id else None
    if not voted and not mine and answer is None:
        return room.body
    data = dict(room.data)
    if voted or mine:
        data["questions"] = [
            dict(q, voted=q["id"] in voted, mine=q["id"] in mine) if (q["id"] in voted or q["id"] in mine) else q
            for q in room.data["questions"]
        ]
    if answer is not None and data.get("poll"):
        data["poll"] = dict(data["poll"], my_response=answer)
    return orjson.dumps(data)


# ─── The quiz ─────────────────────────────────────────────────────────────


@dataclass
class Player:
    id: object
    name: str
    score: int


@dataclass
class QuizView:
    event_id: int
    quiz_id: object
    title: str
    status: QuizStatus
    current_index: int
    opened_at: Optional[datetime]
    questions: list
    players: dict  # player key -> Player
    scores: list   # every player's score, ascending
    board: list    # the top ten, as the board shows them
    answers: dict  # player id -> (option key, points), for the open question

    def current(self) -> Optional[dict]:
        if self.status is QuizStatus.live and 0 <= self.current_index < len(self.questions):
            return self.questions[self.current_index]
        return None

    def rank_of(self, score: int) -> int:
        """1 + how many players scored more."""
        return 1 + len(self.scores) - bisect.bisect_right(self.scores, score)


def _question(q: LiveQuizQuestions) -> dict:
    return {"id": q.id, "prompt": q.prompt, "options": list(q.options or []), "seconds": q.seconds, "sort_order": q.sort_order, "correct_key": q.correct_key}


async def _build_quiz(session, event_id: int):
    found = (await session.execute(
        select(LiveQuizzes).join(Events, Events.id == LiveQuizzes.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(LiveQuizzes.event_id == event_id, *visible())
    )).scalars().first()
    if found is None:
        return _NONE
    quiz = found
    questions = [_question(q) for q in quiz.questions]
    # Every player, best first (ix_live_quiz_players_board): the board is the
    # first ten, the rank a search in the scores.
    rows = (await session.execute(
        select(LiveQuizPlayers.id, LiveQuizPlayers.player_key, LiveQuizPlayers.name, LiveQuizPlayers.score)
        .where(LiveQuizPlayers.quiz_id == quiz.id)
        .order_by(LiveQuizPlayers.score.desc(), LiveQuizPlayers.created_at)
    )).all()
    players = {key: Player(id=pid, name=name, score=score) for pid, key, name, score in rows}
    board = [{"rank": i + 1, "name": name, "score": score} for i, (_, _, name, score) in enumerate(rows[:10])]
    view = QuizView(
        event_id=event_id, quiz_id=quiz.id, title=quiz.title, status=quiz.status, current_index=quiz.current_index,
        opened_at=quiz.question_opened_at, questions=questions, players=players,
        scores=sorted(score for *_, score in rows), board=board, answers={},
    )
    current = view.current()
    if current is not None:
        view.answers = {pid: (opt, pts) for pid, opt, pts in (await session.execute(
            select(LiveQuizAnswers.player_id, LiveQuizAnswers.option_key, LiveQuizAnswers.points).where(LiveQuizAnswers.question_id == current["id"])
        )).all()}
    return view


async def quiz_view(session, event_id: int) -> Optional[QuizView]:
    """The quiz as it stood at most a second ago; None when the event has none (or may not be seen)."""
    view = await quizzes.remember(("quiz", event_id), lambda: _build_quiz(session, event_id))
    return None if view is _NONE else view


def quiz_changed(event_id: int) -> None:
    """The host moved the quiz on: the next poll reads it afresh."""
    quizzes.forget(("quiz", event_id))


def quiz_kept(event_id: int) -> Optional[QuizView]:
    got = quizzes.get(("quiz", event_id))
    return None if got is _MISSING or got is _NONE else got
