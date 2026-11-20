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

Server-sent events go further (stream.py, behind APP_LIVE_SSE): one
connection per phone, the same bodies pushed when they change instead of a
request per phone per poll. Every change here is also told on the event's
channel (bus.py): `room_changed` and `quiz_changed` at once, what the
audience does folded into a message a third of a second.

With Redis the quiz's scores also live in a sorted set per run (board.py):
a rank and the top ten come from it, and the snapshot no longer reads and
sorts every player each second -- it keeps the players it has met (a
roster, per run, in this process) and asks the set for their standing.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import orjson
from sqlalchemy import func, select, text, update

from app.api.closeout.models import EventStatus
from app.api.clubs.models import Clubs
from app.api.discover.cache import _MISSING, TTLCache
from app.api.events.models import Events
from app.api.live import board, bus, scoring
from app.api.live import settings as live_settings
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

#: How long the players of a quiz run met by this process are kept (board mode).
ROSTER_SECONDS = 3 * 3600.0

#: Answers after the clock plus this grace still count, for a slow phone.
ANSWER_GRACE = 2

rooms = TTLCache(ttl=SNAPSHOT_SECONDS, maxsize=4096)
quizzes = TTLCache(ttl=SNAPSHOT_SECONDS, maxsize=1024)
voters = TTLCache(ttl=VOTER_SECONDS, maxsize=200_000)
rosters = TTLCache(ttl=ROSTER_SECONDS, maxsize=1024)

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


#: Open-text answers a poll's results carry.
POLL_TEXTS = 50
#: What a client from before the poll kinds shows for one it cannot answer.
POLL_NEEDS_UPDATE = "This kind of poll needs an update: reload the page or update the app to answer it."


def _indexes(value: str) -> list[int]:
    try:
        return [int(x) for x in value.split(",") if x != ""]
    except ValueError:
        return []


def results_of(poll: LivePolls, total: int, counts: list[tuple[str, int]], st: Optional[dict] = None) -> dict:
    """A poll's results from its counts by value (and its settings: a
    multiple-choice poll's values are option indexes, a rating's scale)."""
    st = st if st is not None else live_settings.poll_settings(poll.kind.value, poll.settings)
    out: dict = {"responses": total}
    by = dict(counts)
    options = list(poll.options or [])
    if poll.kind is PollKind.choice:
        if int(st.get("max_choices") or 1) > 1:
            picked: dict[int, int] = {}
            for v, k in counts:
                for i in set(_indexes(v)):
                    picked[i] = picked.get(i, 0) + k
            out["choices"] = [{"label": o, "count": picked.get(i, 0), "pct": round(100 * picked.get(i, 0) / total) if total else 0} for i, o in enumerate(options)]
        else:
            out["choices"] = [{"label": o, "count": by.get(o, 0), "pct": round(100 * by.get(o, 0) / total) if total else 0} for o in options]
    elif poll.kind is PollKind.words:
        out["words"] = [{"word": w, "count": k} for w, k in counts[:CLOUD_WORDS]]
    elif poll.kind is PollKind.rating:
        scale = int(st.get("scale") or 5)
        nums = {int(v): k for v, k in counts if v.isdigit()}
        rated = sum(nums.values())
        out["rating"] = {
            "average": round(sum(v * k for v, k in nums.items()) / rated, 2) if rated else None,
            "histogram": [{"value": v, "count": nums.get(v, 0), "pct": round(100 * nums.get(v, 0) / total) if total else 0} for v in range(1, scale + 1)],
        }
    elif poll.kind is PollKind.ranking:
        n = len(options)
        pos_sum = [0] * n
        points = [0] * n
        first = [0] * n
        voters = 0
        for v, k in counts:
            order = _indexes(v)
            if sorted(order) != list(range(n)):
                continue
            voters += k
            for place, i in enumerate(order):
                pos_sum[i] += (place + 1) * k
                points[i] += (n - place - 1) * k
            first[order[0]] += k
        rows = [
            {"label": o, "average": round(pos_sum[i] / voters, 2) if voters else None, "points": points[i], "first": first[i], "_i": i}
            for i, o in enumerate(options)
        ]
        rows.sort(key=lambda r: (-r["points"], r["_i"]))
        for r in rows:
            r.pop("_i")
        out["ranking"] = rows
    return out


async def _texts(session, poll: LivePolls, st: dict, *, pending: bool = False) -> dict:
    """An open-text poll's answers the room may see: approved (or, unmoderated,
    every one), newest first."""
    q = select(LivePollResponses.id, LivePollResponses.value, LivePollResponses.author_name, LivePollResponses.created_at).where(LivePollResponses.poll_id == poll.id)
    q = q.where(LivePollResponses.status == "approved")
    rows = (await session.execute(q.order_by(LivePollResponses.created_at.desc()).limit(POLL_TEXTS))).all()
    out = {"texts": [{"id": rid, "text": v, "name": (name if not st.get("anonymous", True) else None), "at": at} for rid, v, name, at in rows]}
    if pending:
        out["pending"] = int(await session.scalar(
            select(func.count()).select_from(LivePollResponses).where(LivePollResponses.poll_id == poll.id, LivePollResponses.status == "pending")
        ) or 0)
    return out


async def _option_images(session, poll: LivePolls, st: dict) -> list:
    ids = [i for i in (st.get("option_images") or []) if i]
    if not ids:
        return []
    from app.api.live import looks

    media = await looks.media_of(session, poll.event_id, ids)
    return [({k: media[i][k] for k in ("thumbnail", "medium", "large")} if i and i in media else None) for i in st.get("option_images") or []]


async def poll_full(session, poll: LivePolls, *, console: bool = True) -> dict:
    """The poll as the console and the big screen see it (v2): today's fields,
    every result, its settings, and whether guests are shown the results."""
    st = live_settings.poll_settings(poll.kind.value, poll.settings)
    total, counts = await counts_of(session, poll.id)
    results = results_of(poll, total, counts, st)
    if poll.kind is PollKind.rating:
        results["scale"] = int(st["scale"])
    if poll.kind is PollKind.text:
        results.update(await _texts(session, poll, st, pending=console))
    return {
        "id": poll.id, "kind": poll.kind.value, "prompt": poll.prompt, "options": list(poll.options or []), "status": poll.status.value,
        "launched_at": poll.launched_at, "closed_at": poll.closed_at, "sort_order": poll.sort_order, "results": results,
        "settings": st, "closes_at": poll.closes_at, "option_images": await _option_images(session, poll, st),
        "results_hidden": live_settings.poll_results_hidden(st, poll.status.value), "my_values": None,
    }


def guest_poll(full: dict) -> dict:
    """The v2 poll as a guest sees it: the results only while they are shown."""
    out = dict(full)
    if full["results_hidden"]:
        out["results"] = {"responses": full["results"]["responses"]}
    elif "pending" in full["results"]:
        out["results"] = {k: v for k, v in full["results"].items() if k != "pending"}
    return out


def legacy_poll(full: dict) -> dict:
    """The poll as a client from before the poll kinds reads it: today's
    fields with today's meanings. A multiple-choice poll is a single choice
    (one tap is a one-option answer); a kind it cannot answer is its prompt
    with a line asking to update and nothing to answer; hidden results are
    empty."""
    st = full["settings"]
    kind = full["kind"]
    total = full["results"]["responses"]
    out = {k: full[k] for k in ("id", "kind", "prompt", "options", "status", "launched_at", "closed_at", "sort_order")}
    if live_settings.poll_needs_update(kind, st):
        out.update(kind="choice", prompt=f"{full['prompt']}\n\n{POLL_NEEDS_UPDATE}", options=[], results={"responses": total, "choices": []})
        return out
    results = full["results"]
    if full["results_hidden"]:
        if kind == "choice":
            results = {"responses": total, "choices": [{"label": o, "count": 0, "pct": 0} for o in full["options"]]}
        elif kind == "words":
            results = {"responses": total, "words": []}
        else:
            results = {"responses": total, "rating": {"average": None, "histogram": [{"value": v, "count": 0, "pct": 0} for v in range(1, 6)]}}
    else:
        results = {k: v for k, v in results.items() if k in ("responses", "choices", "words", "rating")}
    out["results"] = results
    return out


def my_fields(kind: str, options, st: dict, value: Optional[str], v2: bool) -> dict:
    """A browser's own answer on the poll, as each client reads it."""
    if value is None:
        return {"my_response": None, **({"my_values": None} if v2 else {})}
    options = list(options or [])
    if kind == "choice" and int(st.get("max_choices") or 1) > 1:
        labels = [options[i] for i in _indexes(value) if 0 <= i < len(options)]
    elif kind == "ranking":
        labels = [options[i] for i in _indexes(value) if 0 <= i < len(options)]
    else:
        labels = [value]
    if v2:
        return {"my_response": value, "my_values": labels}
    if live_settings.poll_needs_update(kind, st):
        return {"my_response": None}
    return {"my_response": labels[0] if labels else None}


async def poll_public(session, poll: LivePolls) -> dict:
    """The poll as a client from before the kinds reads it (legacy_poll)."""
    return legacy_poll(await poll_full(session, poll, console=False))


async def close_due(session, event_id: int, now: Optional[datetime] = None) -> bool:
    """Polls whose timer ran out, closed (whoever reads the room first does
    it; the others find them closed). True when one closed."""
    now = now or now_utc()
    gone = (await session.execute(
        update(LivePolls).where(LivePolls.event_id == event_id, LivePolls.status == PollStatus.live, LivePolls.closes_at.isnot(None), LivePolls.closes_at <= now)
        .values(status=PollStatus.closed, closed_at=LivePolls.closes_at).returning(LivePolls.id)
    )).all()
    if gone:
        await session.commit()
        bus.changed(event_id, "room", now=True)
        return True
    return False


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
    #: Its settings, resolved, and when its timer runs out.
    poll_settings: dict = field(default_factory=dict)
    poll_closes_at: Optional[datetime] = None
    #: The feed as a client of this release reads it (X-Live-Version: 2):
    #: the poll with its settings, and the theme.
    body_v2: bytes = b""
    data_v2: dict = field(default_factory=dict)
    #: The event page's strip (BannerFeed, view=banner), JSON-safe, and as
    #: it is sent (GET .../live/banner and the stream's frames).
    strip: Optional[dict] = None
    strip_body: bytes = b""


def update_out(u: LiveUpdates) -> dict:
    return {"id": u.id, "kind": u.kind.value, "text": u.text, "audience": u.audience.value, "recipients": u.recipients, "pushed": u.pushed, "sent_at": u.sent_at}


def question_out(r, voted: bool = False, mine: bool = False) -> dict:
    return {
        "id": r.id, "text": r.text, "author_name": r.author_name, "status": r.status.value, "pinned": r.pinned, "votes": r.votes,
        "created_at": r.created_at, "answered_at": r.answered_at, "voted": voted, "mine": mine,
    }


async def _build_room(session, event_id: int):
    from app.api.live import looks
    from app.api.live.schemas import BannerFeed, Feed, FeedV2
    from app.api.subscriptions import gates

    event = await session.scalar(select(Events).join(Clubs, Clubs.id == Events.club_id).where(Events.id == event_id, *visible()))
    if event is None:
        return _NONE
    now = now_utc()
    await close_due(session, event.id, now)
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
    full = await poll_full(session, poll, console=False) if poll else None
    public = {
        "event": {"id": event.id, "name": event.name, "status": (event.lifecycle or EventStatus.published).value},
        "banner": update_out(banner) if banner else None,
        "updates": [update_out(u) for u in updates],
        "poll": {**legacy_poll(full), "my_response": None} if poll else None,
        "questions": [question_out(q) for q, _ in rows],
        "qa": {"approve_first": bool(event.qa_approve_first), "open": qa_open},
        "quiz": {"live": bool(quiz and quiz[0] is QuizStatus.live), "title": quiz[1] if quiz else None},
    }
    data = Feed(**public).model_dump(mode="json")
    custom, resolved, version = await looks.state(session, event.id)
    data_v2 = FeedV2(**{**public, "poll": {**guest_poll(full), "my_response": None} if poll else None, "theme": {"custom": custom, "theme": resolved, "version": version}}).model_dump(mode="json")
    strip = BannerFeed(
        event={**public["event"], "starts_at": event.event_datetime, "ends_at": event.event_datetime + timedelta(hours=event.duration or 0)},
        banner=public["banner"],
        poll={"id": poll.id, "kind": poll.kind.value, "prompt": poll.prompt} if poll else None,
        quiz=public["quiz"],
        qa={"open": qa_open, "has_questions": bool(rows)},
    ).model_dump(mode="json")
    asked_by: dict[str, list[str]] = {}
    for q, author in rows:
        asked_by.setdefault(author, []).append(str(q.id))
    return Room(
        event_id=event.id, club_id=event.club_id, lifecycle=event.lifecycle, approve_first=bool(event.qa_approve_first),
        body=orjson.dumps(data), data=data, question_ids=frozenset(str(q.id) for q, _ in rows), asked_by=asked_by,
        poll_id=str(poll.id) if poll else None, poll_kind=poll.kind if poll else None, poll_options=tuple(poll.options or ()) if poll else (),
        poll_settings=full["settings"] if full else {}, poll_closes_at=poll.closes_at if poll else None,
        strip=strip, strip_body=orjson.dumps(strip), body_v2=orjson.dumps(data_v2), data_v2=data_v2,
    )


async def room_of(session, event_id: int) -> Room:
    """The event's room snapshot; 404 for an event the room may not be opened for."""
    room = await rooms.remember(("room", event_id), lambda: _build_room(session, event_id))
    if room is _NONE:
        raise CustomHTTPException(404, message="Event not found")
    return room


def forget_room(event_id: int) -> None:
    """The next read rebuilds the room (this process only; nothing is told)."""
    rooms.forget(("room", event_id))


def room_changed(event_id: int) -> None:
    """Something everyone sees changed (after its commit): the next poll
    rebuilds the room, and every stream of it is told at once."""
    forget_room(event_id)
    bus.changed(event_id, "room", now=True)


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
    """A key a client made (a browser's, an install's): 8 to 64 letters,
    digits, - and _. Never the `u:` form, which only the server makes: a
    client cannot pass itself off as an account by sending one."""
    key = (key or "").strip()
    if 8 <= len(key) <= 64 and key.replace("-", "").replace("_", "").isalnum():
        return key
    return None


def account_key(user_id) -> str:
    """The room's key for a signed-in person: one voter, asker and
    respondent across every device and browser they use."""
    return f"u:{int(user_id)}"


def person_key(user, client_key: Optional[str]) -> Optional[str]:
    """Who is voting, asking or answering a poll: the account when the
    request carried a valid token (`user`), else the client's own key
    (validated; None when there is none)."""
    if user is not None and getattr(user, "id", None) is not None:
        return account_key(user.id)
    return valid_key(client_key)


def token_user_id(request) -> Optional[int]:
    """The account a request's bearer token names, read from the token alone
    (its signature and expiry checked, no database): for what the room only
    shows a person -- their own votes, questions and poll answers on the feed
    and its stream -- where a token revoked a minute ago does no harm.
    Writes (a vote, a question, an answer) check the token in full
    (OptionalAuth). None when there is no token or it does not hold."""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        from app.core.auth.jwt import decode_jwt_token

        payload = decode_jwt_token(token.strip())
    except Exception:  # noqa: BLE001 - expired, forged, malformed: a visitor
        return None
    if payload.get("token_type") != "access_token" or not payload.get("user_id"):
        return None
    try:
        return int(payload["user_id"])
    except (TypeError, ValueError):
        return None


def shown_key(request, client_key: Optional[str]) -> Optional[str]:
    """`person_key` for the room's reads: the account from the token alone
    (token_user_id), else the client's key."""
    uid = token_user_id(request)
    return account_key(uid) if uid is not None else valid_key(client_key)


async def feed_body(session, event_id: int, key: Optional[str], v2: bool = False) -> bytes:
    """The feed for one browser (or one account: `key` is person_key's or
    shown_key's): the room's body, or a copy with its own flags. `v2`: the
    body of this release (X-Live-Version: 2)."""
    room = await room_of(session, event_id)
    if not key:
        return room.body_v2 if v2 else room.body
    return body_for(room, key, await voter_of(session, event_id, key), v2=v2)


async def banner_body(session, event_id: int) -> bytes:
    """The event page's strip (BannerFeed), as it is sent: from the room's
    snapshot, the same for everyone."""
    return (await room_of(session, event_id)).strip_body


def flags_of(room: Room, key: Optional[str], voter: Optional[Voter]) -> Optional[tuple]:
    """What sets one browser's body apart from the room's: (the questions on
    the feed it voted for, those it asked, its answer to the poll up), or
    None when nothing does. Browsers with the same flags see the same body."""
    if key is None or voter is None:
        return None
    voted = frozenset(voter.voted & room.question_ids)
    mine = frozenset(room.asked_by.get(key, ()))
    answer = voter.answers.get(room.poll_id) if room.poll_id else None
    if not voted and not mine and answer is None:
        return None
    return voted, mine, answer


def body_for(room: Room, key: Optional[str], voter: Optional[Voter], flags: Optional[tuple] = None, v2: bool = False) -> bytes:
    """The room's body as one browser sees it: as it stands, or a copy with
    its own flags (what it voted for, what it asked, its answer to the poll)."""
    flags = flags if flags is not None else flags_of(room, key, voter)
    if flags is None:
        return room.body_v2 if v2 else room.body
    voted, mine, answer = flags
    base = room.data_v2 if v2 else room.data
    data = dict(base)
    if voted or mine:
        data["questions"] = [
            dict(q, voted=q["id"] in voted, mine=q["id"] in mine) if (q["id"] in voted or q["id"] in mine) else q
            for q in base["questions"]
        ]
    if answer is not None and data.get("poll"):
        kind = room.poll_kind.value if room.poll_kind is not None else "choice"
        data["poll"] = dict(data["poll"], **my_fields(kind, room.poll_options, room.poll_settings, answer, v2))
    return orjson.dumps(data)


# ─── The quiz ─────────────────────────────────────────────────────────────


@dataclass
class Player:
    id: object
    name: str
    score: int
    created_at: Optional[datetime] = None
    team: Optional[str] = None
    saved_ms: int = 0
    streak: int = 0

    def member(self) -> str:
        return board.member(self.id, self.created_at, self.name)


class Mine(NamedTuple):
    """A player's answer to the open question: the key (one-key kinds), the
    points it came to, the answer as given (the other kinds), whether it was
    wholly right and its credit."""

    option_key: str
    points: int
    response: Optional[dict] = None
    correct: Optional[bool] = None
    credit: Optional[float] = None


@dataclass
class QuizView:
    event_id: int
    quiz_id: object
    title: str
    status: QuizStatus
    current_index: int
    opened_at: Optional[datetime]
    questions: list
    players: dict  # player key -> Player (with the board in Redis: those this process has met)
    scores: Optional[list]  # every player's score (or (score, saved) with ties by time), ascending; None when the board is in Redis
    board: list    # the top ten, as the board shows them
    answers: dict  # player id -> Mine, for the open question
    #: The run's sorted set (board.py), when ranks come from Redis.
    board_key: Optional[str] = None
    player_count: int = 0
    #: The round's settings, resolved (app/api/live/settings.py).
    settings: dict = field(default_factory=lambda: live_settings.quiz_settings(None))
    join_code: Optional[str] = None
    paused_at: Optional[datetime] = None
    revealed_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    #: (this round's place, how many rounds the event has)
    round: tuple = (0, 1)
    #: The teams' board (scoring.team_board), when the round plays in teams.
    teams: Optional[list] = None
    #: The scored question asked just before the open one (its id as text), for streaks.
    prev_scored: Optional[str] = None
    theme_version: Optional[str] = None
    #: The theme's `leaderboard.own_rank`: a player sees their rank without the board.
    own_rank: bool = True
    #: The board's players' ids (as text), row for row.
    board_ids: list = field(default_factory=list)
    #: The teams of board rows whose players this process has not met (Redis board).
    board_teams: dict = field(default_factory=dict)

    def current(self) -> Optional[dict]:
        if self.status is QuizStatus.live and 0 <= self.current_index < len(self.questions):
            return self.questions[self.current_index]
        return None

    @property
    def ties_by_time(self) -> bool:
        return self.settings.get("ties") == "time"

    def rank_of(self, score: int, saved_ms: int = 0) -> int:
        """1 + how many players are ahead (the snapshot's own scores)."""
        key = (score, saved_ms) if self.ties_by_time else score
        return 1 + len(self.scores) - bisect.bisect_right(self.scores, key)

    def clock_now(self, now: datetime) -> datetime:
        """The question's clock: stopped at the pause while paused."""
        return self.paused_at if self.paused_at is not None and self.paused_at < now else now

    def is_revealed(self, q: Optional[dict], now: datetime) -> bool:
        """Whether the open question's answer, points, scores and board may be shown now."""
        if q is None:
            return True
        if self.paused_at is not None:
            return False
        closed = revealed(self.opened_at, q["seconds"], now)
        if self.settings["flow"]["reveal"] == "host":
            return closed and self.revealed_at is not None
        return closed

    def phase(self, now: datetime) -> str:
        if self.status is QuizStatus.draft:
            return "draft"
        if self.status is QuizStatus.ended:
            return "ended"
        q = self.current()
        if q is None:
            return "lobby"
        if self.paused_at is not None:
            return "paused"
        if not revealed(self.opened_at, q["seconds"], now):
            return "question"
        return "revealed" if self.is_revealed(q, now) else "closed"

    def board_now(self, now: datetime) -> bool:
        """Whether the board is to be shown now (the round's `flow.board`)."""
        phase = self.phase(now)
        if phase == "ended":
            return True
        if phase != "revealed":
            return False
        rule = self.settings["flow"]["board"]
        if rule == "every":
            return True
        if rule == "end":
            return False
        n = int(self.settings["flow"]["board_every"])
        return (self.current_index + 1) % n == 0 or self.current_index == len(self.questions) - 1

    def next_at(self) -> Optional[datetime]:
        """When an auto-advancing round opens its next question (None: the host moves it)."""
        q = self.current()
        if q is None or self.settings["flow"]["advance"] != "auto" or self.paused_at is not None or self.opened_at is None:
            return None
        shown = self.revealed_at if self.settings["flow"]["reveal"] == "host" else reveal_at(self.opened_at, q["seconds"])
        if shown is None:
            return None
        return shown + timedelta(seconds=int(self.settings["flow"]["auto_seconds"]))


def reveal_at(opened_at: datetime, seconds: int) -> datetime:
    """When an open question's answers close and (unless the host reveals it)
    its answer may be shown: the clock and the grace after it (ANSWER_GRACE).
    Not at the clock's 0: answers still count for two seconds then, and a
    second phone shown the answer could score with it."""
    return opened_at + timedelta(seconds=seconds + ANSWER_GRACE)


def revealed(opened_at: Optional[datetime], seconds: int, now: datetime) -> bool:
    """Whether the open question's answers have closed (and, with the clock
    revealing it, everything that gives it away may be shown)."""
    return opened_at is not None and now > reveal_at(opened_at, seconds)


def _question(q: LiveQuizQuestions) -> dict:
    kind = q.kind or scoring.SINGLE
    options = list(q.options or [])
    out = {
        "id": q.id, "prompt": q.prompt, "options": options, "seconds": q.seconds, "sort_order": q.sort_order, "correct_key": q.correct_key,
        "kind": kind, "settings": dict(q.settings or {}),
    }
    st = out["settings"]
    out["points"] = st.get("points")
    out["double"] = bool(st.get("double"))
    # What players are shown: an order question's answers shuffled.
    out["shown"] = scoring.shuffled(options, str(q.id)) if kind == scoring.ORDER else options
    return out


def current_round(rows: list):
    """The event's round the room shows: the live one; else the one that ended
    last; else the first. `rows` are (id, status, ended_at, ...) in order."""
    if not rows:
        return None
    for r in rows:
        if r[1] is QuizStatus.live:
            return r
    ended = [r for r in rows if r[1] is QuizStatus.ended and r[2] is not None]
    if ended:
        return max(ended, key=lambda r: r[2])
    return rows[0]


async def rounds_of(session, event_id: int, *, visible_only: bool = True) -> list:
    """The event's rounds in their order: (id, status, ended_at, sort_order)."""
    q = select(LiveQuizzes.id, LiveQuizzes.status, LiveQuizzes.ended_at, LiveQuizzes.sort_order).where(LiveQuizzes.event_id == event_id)
    if visible_only:
        q = q.join(Events, Events.id == LiveQuizzes.event_id).join(Clubs, Clubs.id == Events.club_id).where(*visible())
    return list((await session.execute(q.order_by(LiveQuizzes.sort_order, LiveQuizzes.created_at))).all())


def _prev_scored(questions: list, asked: list, current_id) -> Optional[str]:
    kinds = {str(q["id"]): q["kind"] for q in questions}
    cur = str(current_id)
    upto = len(asked)
    if cur in asked:
        upto = len(asked) - 1 - asked[::-1].index(cur)
    for qid in reversed(asked[:upto]):
        if qid != cur and kinds.get(qid, scoring.POLL) != scoring.POLL:
            return qid
    return None


async def _build_quiz(session, event_id: int):
    rows = await rounds_of(session, event_id)
    picked = current_round(rows)
    if picked is None:
        return _NONE
    quiz = (await session.execute(select(LiveQuizzes).where(LiveQuizzes.id == picked[0]))).scalars().first()
    if quiz is None:
        return _NONE
    return await build_view(session, quiz, rows)


async def build_view(session, quiz, rows: Optional[list] = None) -> "QuizView":
    """One round's snapshot (the room keeps the current round's, quiz_view)."""
    event_id = quiz.event_id
    if rows is None:
        rows = await rounds_of(session, event_id, visible_only=False)
    st = live_settings.quiz_settings(quiz.settings)
    if quiz.status is QuizStatus.live and st["flow"]["advance"] == "auto":
        from app.api.live import service

        if await service.advance_if_due(session, quiz):
            st = live_settings.quiz_settings(quiz.settings)
    questions = [_question(q) for q in quiz.questions]
    common = dict(
        event_id=event_id, quiz_id=quiz.id, title=quiz.title, status=quiz.status, current_index=quiz.current_index,
        opened_at=quiz.question_opened_at, questions=questions, answers={},
        settings=st, join_code=quiz.join_code, paused_at=quiz.paused_at, revealed_at=quiz.revealed_at, ended_at=quiz.ended_at,
        round=(next((i for i, r in enumerate(rows) if r[0] == quiz.id), 0), len(rows)),
    )
    run = board_run(quiz, st)
    view = None
    if run is not None and await board.ensure(session, quiz.id, run):
        # The board is in Redis: no player is read or sorted here. The
        # players this process has met stay in its roster across snapshots.
        k = board.key(quiz.id, run)
        top = await board.top(k)
        if top is not None:
            count = await session.scalar(select(func.count()).select_from(LiveQuizPlayers).where(LiveQuizPlayers.quiz_id == quiz.id)) or 0
            view = QuizView(**common, players=roster_of(quiz.id, run), scores=None, board=top, board_key=k, player_count=int(count))
    if view is None:
        # Every player, best first (ix_live_quiz_players_rank): the board is
        # the first ten, the rank a search in the scores.
        timed = st["ties"] == "time"
        order = (LiveQuizPlayers.score.desc(), LiveQuizPlayers.saved_ms.desc(), LiveQuizPlayers.created_at) if timed else (LiveQuizPlayers.score.desc(), LiveQuizPlayers.created_at)
        rows_ = (await session.execute(
            select(LiveQuizPlayers.id, LiveQuizPlayers.player_key, LiveQuizPlayers.name, LiveQuizPlayers.score, LiveQuizPlayers.created_at, LiveQuizPlayers.team, LiveQuizPlayers.saved_ms, LiveQuizPlayers.streak)
            .where(LiveQuizPlayers.quiz_id == quiz.id)
            .order_by(*order)
        )).all()
        players = {key: Player(id=pid, name=name, score=score, created_at=at, team=team, saved_ms=int(saved or 0), streak=int(streak or 0)) for pid, key, name, score, at, team, saved, streak in rows_}
        top = [{"rank": i + 1, "name": name, "score": score, "_id": str(pid)} for i, (pid, _, name, score, *_rest) in enumerate(rows_[:10])]
        scores = sorted((score, int(saved or 0)) for _, _, _, score, _, _, saved, _ in rows_) if timed else sorted(score for _, _, _, score, *_rest in rows_)
        view = QuizView(**common, players=players, scores=scores, board=top, player_count=len(rows_))
    view.board_ids = [r.pop("_id", None) for r in view.board]
    current = view.current()
    if current is not None:
        view.answers = {pid: Mine(opt, pts, resp, corr, (resp or {}).get("credit")) for pid, opt, pts, resp, corr in (await session.execute(
            select(LiveQuizAnswers.player_id, LiveQuizAnswers.option_key, LiveQuizAnswers.points, LiveQuizAnswers.response, LiveQuizAnswers.correct)
            .where(LiveQuizAnswers.question_id == current["id"])
        )).all()}
        view.prev_scored = _prev_scored(questions, [str(a) for a in (quiz.asked or [])], current["id"])
    teams = st["teams"]
    if teams["mode"] != "off" and teams["list"]:
        counts = (await session.execute(
            select(LiveQuizPlayers.team, func.count(), func.coalesce(func.sum(LiveQuizPlayers.score), 0))
            .where(LiveQuizPlayers.quiz_id == quiz.id).group_by(LiveQuizPlayers.team)
        )).all()
        view.teams = scoring.team_board(teams["list"], counts, teams["score"])
        met = {str(p.id) for p in view.players.values()}
        missing = [i for i in view.board_ids if i and i not in met]
        if missing:
            found = dict((await session.execute(select(LiveQuizPlayers.id, LiveQuizPlayers.team).where(LiveQuizPlayers.id.in_(missing)))).all())
            view.board_teams = {str(k): v for k, v in found.items()}
    from app.api.live import looks

    custom, resolved, view.theme_version = await looks.state(session, event_id)
    view.own_rank = bool(resolved["leaderboard"]["own_rank"]) if custom else True
    return view


def board_run(quiz, st: dict) -> Optional[str]:
    """The run's sorted set's name (board.py): None for a draft, and for a
    round whose scores can fall (it ranks from its snapshot)."""
    run = board.token(quiz.status, quiz.join_code, quiz.ended_at)
    if run is None or live_settings.scores_can_fall(st):
        return None
    return run + ("t" if st["ties"] == "time" else "")


def board_out(view: QuizView, *, anonymous: bool = False) -> list:
    """The board as it is sent: {rank, name, score} (as always), with each
    player's team when the round plays in teams, and on an anonymous screen
    "Player <tag>" for the name."""
    teams_on = view.settings["teams"]["mode"] != "off"
    by_id = {str(p.id): p for p in view.players.values()} if teams_on else {}
    extra = view.board_teams if teams_on else {}
    out = []
    ids = view.board_ids if len(view.board_ids) == len(view.board) else [None] * len(view.board)
    for r, pid in zip(view.board, ids):
        row = {"rank": r["rank"], "name": r["name"], "score": r["score"]}
        if anonymous and pid:
            row["name"] = f"Player {scoring.tag_of(pid)}"
        if teams_on:
            p = by_id.get(pid or "")
            row["team"] = p.team if p is not None else extra.get(pid or "")
        out.append(row)
    return out


def roster_of(quiz_id, run: str) -> dict:
    """The players of a quiz run this process has met, by key: kept across
    snapshots (board mode), filled as they join or first poll."""
    k = ("roster", quiz_id, run)
    got = rosters.get(k)
    if got is _MISSING:
        got = {}
        rosters.put(k, got)
    return got


async def quiz_view(session, event_id: int) -> Optional[QuizView]:
    """The quiz as it stood at most a second ago; None when the event has none (or may not be seen)."""
    view = await quizzes.remember(("quiz", event_id), lambda: _build_quiz(session, event_id))
    return None if view is _NONE else view


def quiz_changed(event_id: int) -> None:
    """The host moved the quiz on (after the commit): the next poll reads it
    afresh, and every stream of it is told at once."""
    quizzes.forget(("quiz", event_id))
    bus.changed(event_id, "quiz", now=True)


def quiz_kept(event_id: int) -> Optional[QuizView]:
    got = quizzes.get(("quiz", event_id))
    return None if got is _MISSING or got is _NONE else got


#: For each score, how many players of the quiz scored more.
_ABOVE = text(
    "SELECT s, (SELECT count(*) FROM live_quiz_players p WHERE p.quiz_id = :quiz AND p.score > s) "
    "FROM unnest(CAST(:scores AS int[])) AS s"
)
#: How many players of the quiz are ahead of one score and saved time (ties by time).
_AHEAD = text(
    "SELECT count(*) FROM live_quiz_players p WHERE p.quiz_id = :quiz "
    "AND (p.score > :score OR (p.score = :score AND p.saved_ms > :saved))"
)


async def standings(session, view: QuizView, players: list) -> dict:
    """Each player's (score, rank), by player id: from the snapshot's own
    scores, or with the board in Redis from the set (one round trip for them
    all), or -- Redis away, or a player missing from the set -- from Postgres
    (counted on ix_live_quiz_players_rank)."""
    if view.board_key is None:
        return {p.id: (p.score, view.rank_of(p.score, p.saved_ms)) for p in players}
    out: dict = {}
    missing = list(players)
    got = await board.standings(view.board_key, [p.member() for p in players])
    if got is not None:
        missing = []
        for p in players:
            score, rank = got.get(p.member(), (None, None))
            if rank is None:
                missing.append(p)
            else:
                p.score = score
                out[p.id] = (score, rank)
    if missing:
        found = {pid: (int(score), int(saved or 0)) for pid, score, saved in (await session.execute(
            select(LiveQuizPlayers.id, LiveQuizPlayers.score, LiveQuizPlayers.saved_ms).where(LiveQuizPlayers.id.in_([p.id for p in missing]))
        )).all()}
        if view.ties_by_time:
            for p in missing:
                p.score, p.saved_ms = found.get(p.id, (p.score, p.saved_ms))
                ahead = await session.scalar(_AHEAD, {"quiz": view.quiz_id, "score": p.score, "saved": p.saved_ms})
                out[p.id] = (p.score, 1 + int(ahead or 0))
        else:
            wanted = sorted({found.get(p.id, (p.score, 0))[0] for p in missing})
            above = dict((await session.execute(_ABOVE, {"quiz": view.quiz_id, "scores": wanted})).all())
            for p in missing:
                p.score = found.get(p.id, (p.score, 0))[0]
                out[p.id] = (p.score, 1 + int(above.get(p.score, 0)))
        if got is not None:
            for p in missing:
                # Joined while Redis was away: back into the set.
                await board.scored(view.board_key, p.member(), p.score, p.saved_ms)
    return out
