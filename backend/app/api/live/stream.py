"""The live room pushed: server-sent events (APP_LIVE_SSE).

Every phone in a room polls: the feed every five seconds, the quiz every two,
the club's console four endpoints every two to five. room.py made each poll
cheap, but a poll is still a request -- parsing, routing, the middleware, a
response -- and at a thousand phones that is about five hundred a second per
room. Here a phone opens one stream instead and is sent the same body the
poll would return, when it changes:

    GET /events/{id}/live/stream?view=room|quiz|host|screen|questions|polls

(the client contract: scratchpad wave2/LIVE_SSE.md; the poll endpoints stay,
and every client falls back to them).

One `Feed` per event per process holds what its streams show. It subscribes
to the event's channel (bus.py), and on a message rebuilds what the message's
topics show -- once, in a session of its own, however many streams read it --
then wakes them:

- `room`: the room's snapshot (room.Room), its body already made. A browser
  that voted for nothing, asked nothing and answered no poll is sent that
  body's frame as it stands; one that did, the body with its flags
  (room.body_for), from what it did -- read when its stream opened, kept
  current by the deltas the messages carry. Each frame is made once per
  version and set of flags: a poll's thousand "Tea"s are sent one body.
- `quiz`: the quiz's snapshot (room.QuizView), and each streaming player's
  (score, rank) asked for all of them at once (room.standings: the snapshot's
  own scores, or the sorted set in Redis); each player is sent
  service.player_body, as the poll.
- the club's views (`host`, `screen`, `questions`, `polls`): the body the
  console's poll returns, made once per version for every console watching.

Each stream waits on its own wake-up and, woken, renders the latest state and
writes it. While a write is held up by a slow reader, wake-ups fold into one:
when it returns it renders whatever is latest then, so a slow phone skips
versions and nothing queues behind it. A stream that cannot write for
SEND_TIMEOUT is let go (behind nginx, its send_timeout lets go first).

A frame's `id` is the version of the stream's topics (bus.py): the same in
every process for the same change, so a phone that reconnects anywhere with
`Last-Event-ID` is sent nothing new when nothing changed.

The feed also rebuilds every RESYNC_SECONDS, and if what it built differs from
what it had -- a message lost, a change made outside the live room (the event
closed, its club unlisted, the banner's three hours up) -- tells everyone
(bus.changed). When an open question's answers close (its clock and the
grace after it, room.reveal_at) it asks for the quiz to be told once (the
players' screens show the right answer, their points and the board then),
and when the
room or the quiz is gone (404) or a player was removed (401) its streams are
sent `gone` and end.

Bounds: streams per process (LIVE_SSE_MAX_STREAMS), per event and per address
in a process; opening one counts against the room's limits (limits.py,
"stream"). A stream lives LIFETIME seconds (less for the club's, so a session
ended elsewhere stops being served soon), then ends with a jittered `retry`;
the client reconnects with its last id. Nothing is left behind a stream: its
one helper task (the wait for the client's disconnect) ends with it, and a
feed whose last stream went stops LINGER_SECONDS later.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import orjson
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from starlette.responses import Response

from app.api.events.models import Events
from app.api.live import bus, room
from app.config import settings
from app.core import redis

logger = logging.getLogger(__name__)

PING_SECONDS = 15.0
RETRY_MS = 3000
RESYNC_SECONDS = 5.0
LINGER_SECONDS = 20.0
#: How long a stream is served before it is ended (and reconnected).
LIFETIME = 600.0
CLUB_LIFETIME = 300.0
#: A write held this long (the phone stopped reading) ends the stream. Above
#: nginx's send_timeout (60 s), which lets such a phone go first and so ends
#: the stream as a disconnect; this only bounds a stream with nothing in
#: front, whose end uvicorn then logs as an incomplete response.
SEND_TIMEOUT = 90.0
#: The least time between two frames of a stream of this view, unless the
#: newer is the host's (told at once). The room's feed is 30 KB a frame and a
#: phone polled it every 5 s: a busy hall voting would otherwise send every
#: phone a frame each third of a second -- 100 MB a second over a venue's
#: Wi-Fi at 3,000 phones -- for counts nobody reads that fast. The quiz's
#: frames are small and its players race: unpaced.
PACE = {"room": 2.0}
#: How long a new stream waits for the feed's first build before a ping.
FIRST_BUILD_WAIT = 10.0
#: Streams of one event, and from one address, one process holds at most.
#: An address is a whole hall on one Wi-Fi, so its ceiling is a hall's.
PER_EVENT = 4000
PER_ADDRESS = 3000

#: What each view shows, by the topics it is made from. `banner` is the
#: room's strip for the event page (BannerFeed): built with the room, sent
#: only when what it shows changes.
VIEWS: dict[str, tuple[str, ...]] = {
    "room": ("room",),
    "banner": ("room",),
    "quiz": ("quiz",),
    "host": ("quiz",),
    "screen": ("room", "quiz"),
    "questions": ("room",),
    "polls": ("room",),
}
CLUB_VIEWS = frozenset({"host", "screen", "questions", "polls"})


def on() -> bool:
    return bool(settings.LIVE_SSE)


@asynccontextmanager
async def _default_scope() -> AsyncIterator[Any]:
    from app.db.core import AsyncSessionLocal
    from app.db.listeners import add_loader_criteria

    async with AsyncSessionLocal() as session:
        add_loader_criteria(session)
        yield session


#: Where a feed gets the session it rebuilds in. The test suite points it at
#: the test's session.
session_scope = _default_scope


def _now() -> datetime:
    return datetime.now(timezone.utc)


def frame(event: str, data: bytes, version: Optional[int] = None) -> bytes:
    head = b"id: %d\nevent: %s\n" % (version, event.encode()) if version is not None else b"event: %s\n" % event.encode()
    return head + b"data: " + data + b"\n\n"


def ping_frame() -> bytes:
    return frame("ping", b'{"t":%d}' % (time.time_ns() // 1_000_000))


def retry_frame(ms: int) -> bytes:
    return b"retry: %d\n\n" % ms


class Full(Exception):
    """No room for another stream here."""


@dataclass(eq=False)
class Listener:
    view: str
    event_id: int
    #: The browser's key (room) or the player's (quiz).
    key: Optional[str] = None
    address: str = ""
    #: The player behind a quiz stream, as found when it opened.
    player: Optional[room.Player] = None
    #: The version last sent, and what it showed (to send a new version only
    #: when it shows this stream something new).
    sent_version: Optional[int] = None
    sent_shows: Any = None
    sent_at: float = 0.0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False


class Feed:
    """One event's streams in this process, and what they show."""

    def __init__(self, hub: "Hub", event_id: int):
        self.hub = hub
        self.event_id = event_id
        self.listeners: set[Listener] = set()
        self.versions: dict[str, int] = {}
        #: The latest version of each topic that the host's change made (sent unpaced).
        self.urgent: dict[str, int] = {}
        self.room: Optional[room.Room] = None
        self.quiz: Optional[room.QuizView] = None
        #: Each streaming player's (score, rank), by player id, for self.quiz.
        self.standing: dict = {}
        #: The club's views: view -> its body as a JSON-ready dict.
        self.club: dict[str, Any] = {}
        #: Views that are gone: view -> (status, message).
        self.gone: dict[str, tuple[int, str]] = {}
        #: Players removed since their stream opened: key -> (status, message).
        self.gone_players: dict[str, tuple[int, str]] = {}
        #: The browsers streaming here, by key: what each did (kept by the deltas).
        self.voters: dict[str, room.Voter] = {}
        self._voter_refs: Counter = Counter()
        #: The room's frames for this version: as it stands (most browsers are
        #: sent it), and per set of flags (a poll's answerers share theirs).
        self._frames: dict = {}
        self._frames_for: Optional[tuple] = None
        #: The strip's frame for this version (view=banner), made once.
        self._strip: bytes = b""
        self._strip_for: Optional[tuple] = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sub = None
        self.reader: Optional[asyncio.Task] = None
        self.task: Optional[asyncio.Task] = None
        self._idle_since: Optional[float] = time.monotonic()
        self._next_resync = time.monotonic() + RESYNC_SECONDS
        self._clock: Optional[tuple[float, str]] = None
        self.builds = 0

    # ─── Streams coming and going ───────────────────────────────────────

    def add(self, listener: Listener, voter: Optional[room.Voter] = None) -> None:
        self.listeners.add(listener)
        self._idle_since = None
        if listener.key and listener.view == "room":
            self._voter_refs[listener.key] += 1
            if voter is not None and listener.key not in self.voters:
                self.voters[listener.key] = voter
        if not self.has(listener.view):
            self.inbox.put_nowait({"attach": listener.view})

    def remove(self, listener: Listener) -> None:
        if listener not in self.listeners:
            return
        self.listeners.discard(listener)
        if listener.key and listener.view == "room":
            self._voter_refs[listener.key] -= 1
            if self._voter_refs[listener.key] <= 0:
                del self._voter_refs[listener.key]
                self.voters.pop(listener.key, None)
        if listener.view == "quiz" and listener.key:
            self.gone_players.pop(listener.key, None)
        if not self.listeners:
            self._idle_since = time.monotonic()
            # The pump may be waiting a long while: it starts counting now.
            self.inbox.put_nowait({"idle": True})

    def views(self) -> set[str]:
        return {listener.view for listener in self.listeners}

    def has(self, view: str) -> bool:
        if _made_of_room(view) in self.gone:
            return True
        if view in ("room", "banner"):
            return self.room is not None
        if view == "quiz":
            return self.quiz is not None
        return view in self.club

    def version(self, view: str) -> int:
        return max(self.versions.get(t, 0) for t in VIEWS[view])

    def gone_for(self, listener: Listener) -> Optional[tuple[int, str]]:
        """(status, message) when what the stream shows is gone, else None."""
        return self.gone.get(_made_of_room(listener.view)) or (
            self.gone_players.get(listener.key) if listener.view == "quiz" and listener.key else None
        )

    def urgent_since(self, view: str, version: Optional[int]) -> bool:
        """Whether the host changed what this view shows after `version`."""
        return max(self.urgent.get(t, 0) for t in VIEWS[view]) > (version or 0)

    def _wake(self, views: Optional[set] = None) -> None:
        for listener in self.listeners:
            if views is None or listener.view in views:
                listener.wake.set()

    # ─── The pump ───────────────────────────────────────────────────────

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self.sub is None:
            # Subscribed before the version is read: nothing told in between is missed.
            self.sub = redis.subscribe(bus.channel(self.event_id))
            self.reader = loop.create_task(self._read(self.sub), name=f"live-feed-read:{self.event_id}")
        if not self.hub.manual and (self.task is None or self.task.done()):
            self.task = loop.create_task(self._run(), name=f"live-feed:{self.event_id}")

    async def _ready(self) -> None:
        if not self.versions:
            v = await bus.current_version(self.event_id)
            self.versions = {t: v for t in bus.TOPICS}

    async def _stop(self) -> None:
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        sub, self.sub = self.sub, None
        if sub is not None:
            await sub.aclose()

    def _batch(self) -> list:
        batch: list = []
        while not self.inbox.empty():
            batch.append(self.inbox.get_nowait())
        return batch

    async def step(self, *, resync: bool = False) -> None:
        """What the pump does with what is waiting, now (tests: Hub.manual)."""
        await self._ready()
        if resync:
            self._next_resync = 0.0
        await self._step(self._batch())

    async def _run(self) -> None:
        try:
            await self._ready()
            # The streams that made this feed asked for their views (inbox).
            while True:
                timeout = max(0.0, min(self._next_resync, self._clock[0] if self._clock else float("inf")) - time.monotonic())
                if not self.listeners:
                    timeout = min(timeout, LINGER_SECONDS)
                batch: list = []
                try:
                    async with asyncio.timeout(timeout):
                        batch.append(await self.inbox.get())
                except TimeoutError:
                    pass
                batch += self._batch()
                if not self.listeners and self._idle_since is not None and time.monotonic() - self._idle_since >= LINGER_SECONDS:
                    # Checked and left without awaiting in between: a stream
                    # opening from now on makes a new feed.
                    if self.hub.feeds.get(self.event_id) is self:
                        del self.hub.feeds[self.event_id]
                    return
                try:
                    await self._step(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next message or resync tries again
                    logger.exception("live feed %s could not rebuild", self.event_id)
                    await asyncio.sleep(1.0)
        finally:
            await self._stop()

    async def _read(self, sub) -> None:
        async for message in sub:
            self.inbox.put_nowait(message)

    async def _step(self, batch: list) -> None:
        topics: set[str] = set()
        attach: set[str] = set()
        reread = False
        for m in batch:
            if "attach" in m:
                attach.add(m["attach"])
                continue
            if "idle" in m:
                continue
            v = int(m.get("v") or 0)
            bus.seen(self.event_id, v)
            for t in m.get("t") or ():
                if t in bus.TOPICS:
                    topics.add(t)
                    self.versions[t] = max(self.versions.get(t, 0), v)
                    if m.get("h"):
                        self.urgent[t] = max(self.urgent.get(t, 0), v)
            if m.get("x"):
                reread = True
            for delta in m.get("d") or ():
                self._apply(delta)
        now = time.monotonic()
        resync = now >= self._next_resync
        if resync:
            self._next_resync = now + RESYNC_SECONDS
        clock = self._clock is not None and now >= self._clock[0]
        if clock:
            tag, self._clock = self._clock[1], None
            await bus.changed_once(self.event_id, tag, "quiz")
        if not self.listeners:
            # Nobody to show it to: what changed is built again when someone comes.
            self._forget(topics | (set(bus.TOPICS) if reread else set()))
            return
        if not (topics or attach or resync or reread):
            return
        views = self.views()
        told = {v for v in views if set(VIEWS[v]) & topics} | ({"room", "banner"} & views if reread else set())
        wanted = told | (attach & views) | (views if resync else set())
        before = self._fingerprint() if resync else None
        await self._build(wanted, reread)
        if before is not None:
            after = self._fingerprint()
            moved = sorted({t for v, fp in after.items() if v not in told and before.get(v) != fp for t in VIEWS[v]})
            if moved:
                # Built differently with no message: someone changed it without
                # telling, or the message was lost. Everyone is told now (the
                # message comes back here too, with the version to send it under).
                bus.changed(self.event_id, *moved, now=True)
        self._wake(told | attach)

    def _forget(self, topics: set) -> None:
        for view, made_of in VIEWS.items():
            if set(made_of) & topics:
                self.gone.pop(view, None)
                if view in ("room", "banner"):
                    self.room = None
                elif view == "quiz":
                    self.quiz, self.standing = None, {}
                else:
                    self.club.pop(view, None)

    def _apply(self, delta: list) -> None:
        """What one browser did, onto its flags here (and this process's poll cache)."""
        try:
            kind, key, target, value = delta
        except (TypeError, ValueError):
            return
        for voter in (self.voters.get(key), room.voter_kept(self.event_id, key)):
            if voter is None:
                continue
            if kind == "v":
                (voter.voted.add if value else voter.voted.discard)(target)
            elif kind == "r":
                voter.answers[target] = value

    def _fingerprint(self) -> dict:
        """What each view shows, comparable between builds (the clocks left out)."""
        out: dict = {view: ("gone", status) for view, (status, _) in self.gone.items()}
        if self.room is not None and "room" not in self.gone:
            out["room"] = self.room.body
        if self.quiz is not None and "quiz" not in self.gone:
            q = self.quiz
            out["quiz"] = (q.status, q.title, q.current_index, q.opened_at, orjson.dumps(q.questions, default=str), orjson.dumps(q.board), q.player_count, len(q.answers))
        for view, data in self.club.items():
            if view not in self.gone:
                out[view] = orjson.dumps(_without_clock(view, data), default=str)
        return out

    async def _build(self, views: set[str], reread: bool) -> None:
        from app.api.live import service

        self.builds += 1
        async with session_scope() as s:
            if views & {"room", "banner"}:
                built = await room._build_room(s, self.event_id)
                if built is room._NONE:
                    self.gone["room"] = (404, "Event not found")
                else:
                    self.gone.pop("room", None)
                    self.room = built
                if reread:
                    for key, voter in list(self.voters.items()):
                        fresh = await room._load_voter(s, self.event_id, key)
                        voter.voted, voter.answers = fresh.voted, fresh.answers
            if views & {"quiz", "host", "screen"}:
                # The poll path's snapshot too: the club's views read it.
                room.quizzes.forget(("quiz", self.event_id))
            if "quiz" in views:
                await self._build_quiz(s, service)
            club = views & CLUB_VIEWS
            if club:
                event = await s.scalar(
                    select(Events).options(joinedload(Events.club)).where(Events.id == self.event_id, Events.is_deleted == False)  # noqa: E712
                )
                for view in club:
                    if event is None:
                        self.gone[view] = (404, "Event not found")
                        continue
                    self.club[view] = await _club_body(s, service, event, view)
            await s.commit()

    async def _build_quiz(self, s, service) -> None:
        built = await room._build_quiz(s, self.event_id)
        if built is room._NONE:
            self.gone["quiz"] = (404, "This event has no quiz.")
            self._clock = None
            return
        self.gone.pop("quiz", None)
        # Every streaming player in the snapshot: those it does not have (it
        # keeps only the players met, or they joined since) found at once.
        keys = {listener.key for listener in self.listeners if listener.view == "quiz" and listener.key}
        unknown = [k for k in keys if k not in built.players]
        if unknown:
            from app.api.live.models import LiveQuizPlayers

            rows = (await s.execute(
                select(LiveQuizPlayers.player_key, LiveQuizPlayers.id, LiveQuizPlayers.name, LiveQuizPlayers.score, LiveQuizPlayers.created_at)
                .where(LiveQuizPlayers.quiz_id == built.quiz_id, LiveQuizPlayers.player_key.in_(unknown))
            )).all()
            for key, pid, name, score, at in rows:
                built.players[key] = room.Player(id=pid, name=name, score=score, created_at=at)
            for key in set(unknown) - {r[0] for r in rows}:
                self.gone_players[key] = (401, "You are not in this quiz. Join again with the code on the screen.")
        players = [built.players[k] for k in keys if k in built.players]
        standing = await room.standings(s, built, players) if players else {}
        self.quiz, self.standing = built, standing
        current = built.current()
        if current is not None and built.opened_at is not None:
            # When answers close and the players may be shown the right
            # answer, their points and the board (room.revealed).
            ends = room.reveal_at(built.opened_at, current["seconds"])
            left = (ends - _now()).total_seconds()
            self._clock = (time.monotonic() + left + 0.05, f"clock:{current['id']}") if left > 0 else None
        else:
            self._clock = None

    # ─── What one stream is sent ────────────────────────────────────────

    def render(self, listener: Listener) -> tuple[Optional[int], Any, bytes]:
        """(version, what it shows, frame) for the stream now. Version None:
        the view is gone, and the frame says so."""
        view = listener.view
        gone = self.gone_for(listener)
        if gone is not None:
            return None, None, frame("gone", orjson.dumps({"status": gone[0], "message": gone[1]}))
        v = self.version(view)
        if view == "room":
            # One frame per version and set of flags, whoever is sent it: the
            # room as it stands for most, and the few bodies a poll's or a
            # question's flags make, each made once.
            if self._frames_for != (v, id(self.room)):
                self._frames, self._frames_for = {}, (v, id(self.room))
            flags = room.flags_of(self.room, listener.key, self.voters.get(listener.key) if listener.key else None)
            made = self._frames.get(flags)
            if made is None:
                body = room.body_for(self.room, listener.key, None, flags)
                made = self._frames[flags] = (body, frame("snapshot", body, v))
            return v, made[0], made[1]
        if view == "banner":
            data = self.room.strip
            if self._strip_for != (v, id(self.room)):
                self._strip, self._strip_for = frame("snapshot", orjson.dumps(data), v), (v, id(self.room))
            return v, data, self._strip
        if view == "quiz":
            from app.api.live import service

            q = self.quiz
            player = q.players.get(listener.key) or listener.player
            standing = self.standing.get(player.id) or (player.score, q.rank_of(player.score) if q.scores is not None else None)
            data = service.player_body(q, player, standing, _now())
            question = data.get("question")
            shows = {**data, "question": {**question, "time_left": None}} if question else data
            return v, shows, frame("snapshot", orjson.dumps(data, default=str), v)
        data = self.club[view]
        return v, data, frame("snapshot", orjson.dumps(_with_clock(view, data, _now()), default=str), v)


def _made_of_room(view: str) -> str:
    """The view whose gone a view shares: the strip is the room's."""
    return "room" if view == "banner" else view


def _question_of(view: str, data):
    if view == "host" and isinstance(data, dict):
        return data.get("question")
    if view == "screen" and isinstance(data, dict) and data.get("quiz"):
        return data["quiz"].get("question")
    return None


def _without_clock(view: str, data):
    q = _question_of(view, data)
    if q is None:
        return data
    out = dict(data)
    if view == "host":
        out["question"] = {**q, "time_left": None}
    else:
        out["quiz"] = {**data["quiz"], "question": {**q, "time_left": None}}
    return out


def _with_clock(view: str, data, now: datetime):
    """The club's view with its question's clock counted now, as the poll would."""
    from app.api.live import service

    q = _question_of(view, data)
    if q is None or not q.get("opened_at"):
        return data
    opened = datetime.fromisoformat(str(q["opened_at"]).replace("Z", "+00:00"))
    left = service._time_left(opened, int(q.get("seconds") or 0), now)
    if view == "host":
        return {**data, "question": {**q, "time_left": left}}
    return {**data, "quiz": {**data["quiz"], "question": {**q, "time_left": left}}}


async def _club_body(s, service, event: Events, view: str):
    """The body the console's poll of `view` returns, JSON-ready."""
    from app.api.live.schemas import HostState, PollOut, QABoard, Screen

    if view == "host":
        quiz = await service.quiz_of(s, event)
        return HostState(**await service.host_state(s, quiz)).model_dump(mode="json", by_alias=True)
    if view == "screen":
        return Screen(**await service.screen(s, event)).model_dump(mode="json", by_alias=True)
    if view == "questions":
        return QABoard(**await service.questions_for_club(s, event)).model_dump(mode="json", by_alias=True)
    return [PollOut(**p).model_dump(mode="json", by_alias=True) for p in await service.list_polls(s, event)]


# ─── The process's feeds and its bounds ─────────────────────────────────


class Hub:
    def __init__(self):
        self.feeds: dict[int, Feed] = {}
        self.open = 0
        self.by_address: Counter = Counter()
        #: No pump tasks: what a feed's pump would do happens when `step` is
        #: awaited (the test suite, so builds share the test's session in turn).
        self.manual = False

    def feed(self, event_id: int) -> Feed:
        feed = self.feeds.get(event_id)
        if feed is None:
            feed = self.feeds[event_id] = Feed(self, event_id)
        feed.start()
        return feed

    def admit(self, event_id: int, address: str) -> None:
        """Count a stream in, or Full when this process holds all it may."""
        feed = self.feeds.get(event_id)
        if self.open >= int(settings.LIVE_SSE_MAX_STREAMS):
            raise Full("this server holds all the streams it can")
        if feed is not None and len(feed.listeners) >= PER_EVENT:
            raise Full("this room has all the streams this server can hold")
        if self.by_address[address] >= PER_ADDRESS:
            raise Full("too many streams from one address")
        self.open += 1
        self.by_address[address] += 1

    def release(self, address: str) -> None:
        self.open = max(0, self.open - 1)
        self.by_address[address] -= 1
        if self.by_address[address] <= 0:
            del self.by_address[address]

    async def step(self, *, resync: bool = False) -> None:
        """Every feed's waiting messages handled now (Hub.manual)."""
        await bus.drain()
        await asyncio.sleep(0)  # the subscriptions' readers hand them over
        for feed in list(self.feeds.values()):
            await feed.step(resync=resync)

    async def close(self) -> None:
        """Stop every feed (shutdown, tests). Streams still open end with their requests."""
        feeds = list(self.feeds.values())
        self.feeds.clear()
        loop = asyncio.get_running_loop()
        for feed in feeds:
            if feed.task is not None:
                feed.task.cancel()
                if feed.task.get_loop() is loop:
                    with suppress(asyncio.CancelledError, Exception):
                        await feed.task
            if feed.reader is None or feed.reader.get_loop() is loop:
                await feed._stop()

    def stats(self) -> dict:
        return {"open": self.open, "feeds": len(self.feeds), "listeners": sum(len(f.listeners) for f in self.feeds.values())}


hub = Hub()


# ─── The response ───────────────────────────────────────────────────────


class EventStream(Response):
    """The stream of one listener, as an ASGI response: frames as the feed
    changes, a ping in each quiet PING_SECONDS, an end after its lifetime."""

    media_type = "text/event-stream"

    def __init__(self, feed: Feed, listener: Listener, last_event_id: Optional[int], lifetime: float):
        # As StreamingResponse sets itself up: with no body, so no
        # Content-Length (a stream has none; uvicorn would hold it to 0).
        self.status_code = 200
        self.media_type = "text/event-stream; charset=utf-8"
        self.background = None
        self.init_headers({"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        self.feed = feed
        self.listener = listener
        self.last_event_id = last_event_id
        self.lifetime = lifetime * random.uniform(0.9, 1.1)

    async def __call__(self, scope, receive, send) -> None:
        watcher = asyncio.get_running_loop().create_task(self._watch(receive))
        try:
            await self._serve(send)
        except TimeoutError:
            logger.info("a live stream of event %s stopped reading; let go", self.listener.event_id)
        finally:
            self.listener.closed = True
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            self.feed.remove(self.listener)
            self.feed.hub.release(self.listener.address)

    async def _watch(self, receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.listener.closed = True
                self.listener.wake.set()
                return

    async def _serve(self, send) -> None:
        listener, feed = self.listener, self.feed

        async def write(data: bytes, more: bool = True) -> None:
            async with asyncio.timeout(SEND_TIMEOUT):
                await send({"type": "http.response.body", "body": data, "more_body": more})

        async with asyncio.timeout(SEND_TIMEOUT):
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await write(retry_frame(RETRY_MS))
        ends = time.monotonic() + self.lifetime
        # The feed's first build of this view, if it is new to the feed.
        with suppress(TimeoutError):
            async with asyncio.timeout(FIRST_BUILD_WAIT):
                while not feed.has(listener.view) and not listener.closed:
                    listener.wake.clear()
                    await listener.wake.wait()
        if listener.closed:
            return
        listener.wake.set()
        if feed.has(listener.view) and self.last_event_id is not None and self.last_event_id == feed.version(listener.view):
            # The client has this version: nothing to send but a ping.
            version, shows, _ = feed.render(listener)
            if version is not None:
                listener.sent_version, listener.sent_shows = version, shows
        quiet_since = -PING_SECONDS
        while True:
            if listener.closed:
                return
            if listener.wake.is_set() and feed.has(listener.view):
                listener.wake.clear()
                due = listener.sent_at + PACE.get(listener.view, 0.0)
                now = time.monotonic()
                if (now < due and listener.sent_version is not None and feed.version(listener.view) != listener.sent_version
                        and feed.gone_for(listener) is None and not feed.urgent_since(listener.view, listener.sent_version)):
                    # The audience's, and the last frame was moments ago: sent
                    # when its pace allows (the latest by then), or at once if
                    # the host acts meanwhile.
                    with suppress(TimeoutError):
                        async with asyncio.timeout(due - now):
                            await listener.wake.wait()
                    listener.wake.set()
                    continue
                version, shows, data = feed.render(listener)
                if version is None:
                    await write(data)
                    await write(b"", more=False)
                    return
                if version != listener.sent_version:
                    # A new version that shows this stream nothing new (a
                    # change elsewhere in the room) is not sent.
                    if listener.sent_version is None or shows is not listener.sent_shows and shows != listener.sent_shows:
                        await write(data)
                        quiet_since = listener.sent_at = time.monotonic()
                    listener.sent_version, listener.sent_shows = version, shows
                continue
            listener.wake.clear()
            now = time.monotonic()
            if now >= ends:
                await write(retry_frame(RETRY_MS + random.randint(0, RETRY_MS)), more=False)
                return
            if now - quiet_since >= PING_SECONDS:
                await write(ping_frame())
                quiet_since = now
                continue
            with suppress(TimeoutError):
                async with asyncio.timeout(min(ends, quiet_since + PING_SECONDS) - now):
                    await listener.wake.wait()


def parse_version(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None
