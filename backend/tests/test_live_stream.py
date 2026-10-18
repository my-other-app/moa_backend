"""The live room pushed (app/api/live/stream.py, bus.py, board.py): who may
open each view, that a snapshot is the poll's body, resuming by
Last-Event-ID, pings, the audience's changes folded, a slow phone skipping
versions, the bounds on open streams, nothing left behind a disconnect, the
flag off, a change crossing processes through Redis, and a player's rank
from the board in Redis.

A stream is opened on the app itself (ASGI, with its whole middleware
stack), read frame by frame, and disconnected by the test. The feeds run in
the hub's manual mode: what a feed's pump would do happens when the test
awaits `hub.step()`, so their builds take turns with the test on its one
session. One test runs the real pump.

Redis tests use the local Redis the platform tests use (tests/test_redis.py),
skipped when none answers.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from sqlalchemy import select, text, update

from app.api.live import bus, limits, room, stream
from app.api.live.models import LiveQuizzes
from app.config import settings
from app.core import redis
from tests.live_harness import HALL, REDIS_URL, _join, _key, _no_clock, _quiz, _u, with_redis, world  # noqa: F401 (fixtures)
from tests.test_live_board import _scored_quiz
from tests.test_redis import needs_redis


# ─── Opening a stream on the app ─────────────────────────────────────────


class SSE:
    """One stream request on the app, read as frames."""

    def __init__(self, path: str, headers: dict | None = None, query: dict | None = None, address: str = HALL):
        self.path = path
        self.headers = headers or {}
        self.query = urlencode(query or {})
        self.address = address
        self.sent: asyncio.Queue = asyncio.Queue()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.buffer = b""
        self.status = None
        self.response_headers: dict = {}
        self.body = b""
        self.ended = False
        self.task = None
        #: While set, the app's writes wait (a phone that stopped reading).
        self.stalled: asyncio.Event | None = None
        self.writes = 0

    async def open(self):
        from doth import application

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": self.path, "raw_path": self.path.encode(), "query_string": self.query.encode(),
            "root_path": "", "server": ("testserver", 80), "client": (self.address, 50000),
            "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")]
            + [(k.lower().encode(), str(v).encode()) for k, v in self.headers.items()],
        }
        await self.inbox.put({"type": "http.request", "body": b"", "more_body": False})
        self.task = asyncio.get_running_loop().create_task(application(scope, self.inbox.get, self._send))
        first = await asyncio.wait_for(self.sent.get(), 10)
        assert first["type"] == "http.response.start", first
        self.status = first["status"]
        self.response_headers = {k.decode().lower(): v.decode() for k, v in first["headers"]}
        if self.status != 200:
            while not self.ended:
                await self._more(5)
            await self.task
        return self

    async def _send(self, message):
        if self.stalled is not None and message["type"] == "http.response.body":
            await self.stalled.wait()
        self.writes += 1
        await self.sent.put(message)

    async def _more(self, timeout: float) -> None:
        message = await asyncio.wait_for(self.sent.get(), timeout)
        if message["type"] == "http.response.body":
            self.buffer += message.get("body", b"")
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                self.ended = True

    def json(self):
        return json.loads(self.body)

    async def frame(self, timeout: float = 3.0) -> dict | None:
        """The next frame: {"event", "id", "data", "retry"}; None once the stream ended."""
        while b"\n\n" not in self.buffer:
            if self.ended:
                return None
            await self._more(timeout)
        raw, self.buffer = self.buffer.split(b"\n\n", 1)
        out: dict = {"event": None, "id": None, "data": None, "retry": None}
        for line in raw.decode().split("\n"):
            name, _, value = line.partition(": ")
            out[name] = value
        if out["data"] is not None and out["event"] in ("snapshot", "gone", "ping"):
            out["json"] = json.loads(out["data"])
        return out

    async def next_of(self, event: str, timeout: float = 3.0) -> dict:
        while True:
            f = await self.frame(timeout)
            assert f is not None, f"the stream ended before a {event}"
            if f["event"] == event:
                return f

    async def nothing(self, wait: float = 0.15) -> None:
        """No frame arrives meanwhile."""
        await asyncio.sleep(wait)
        assert b"\n\n" not in self.buffer and self.sent.empty(), (self.buffer, self.sent.qsize())

    async def close(self):
        await self.inbox.put({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 5)


@pytest.fixture
async def streams(monkeypatch, session):
    """Streams on, a fresh hub in manual mode, its builds on the test's session."""
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
    # Unpaced unless a test is about the pace.
    monkeypatch.setattr(stream, "PACE", {})
    bus.reset_for_tests()
    yield hub
    await hub.close()
    bus.reset_for_tests()


def _s(world):
    return _u(world, "/stream")


async def _busy_room(client, world):
    h = world["h"]
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=h)
    mine = (await client.post(_u(world, "/questions"), json={"text": "Mine, from browser one"}, headers=_key(1))).json()
    other = (await client.post(_u(world, "/questions"), json={"text": "Another, from browser two"}, headers=_key(2))).json()
    poll = (await client.post(_u(world, "/polls"), json={"kind": "choice", "prompt": "Tea or coffee?", "options": ["Tea", "Coffee"]}, headers=h)).json()
    await client.post(_u(world, f"/polls/{poll['id']}/status"), json={"status": "live"}, headers=h)
    await client.post(_u(world, f"/questions/{other['id']}/vote"), headers=_key(1))
    await client.post(_u(world, f"/polls/{poll['id']}/respond"), json={"value": "Tea"}, headers=_key(1))
    return mine, other, poll


# ─── The flag ────────────────────────────────────────────────────────────


async def test_with_the_flag_off_the_stream_is_a_404_and_the_poll_answers(client, world):
    assert settings.LIVE_SSE is False
    r = await client.get(_s(world), params={"view": "room"})
    assert r.status_code == 404 and r.json()["error_code"] == "LIVE_STREAM_OFF"
    assert (await client.get(_u(world))).status_code == 200


# ─── Each view: who may open it, and what it sends ───────────────────────


async def test_the_room_stream_sends_the_polls_body_with_this_browsers_flags(client, world, streams):
    mine, other, poll = await _busy_room(client, world)
    one = await SSE(_s(world), _key(1), {"view": "room"}).open()
    assert one.status == 200
    assert one.response_headers["content-type"].startswith("text/event-stream")
    assert one.response_headers["cache-control"] == "no-cache, no-transform"
    assert one.response_headers["x-accel-buffering"] == "no"
    assert "content-length" not in one.response_headers  # a stream has none
    assert one.response_headers["x-frame-options"] == "DENY"  # through every middleware
    assert (await one.frame())["retry"] == "3000"
    await streams.step()
    snap = await one.next_of("snapshot")
    assert int(snap["id"]) > 0
    poll_body = (await client.get(_u(world), headers=_key(1))).json()
    assert snap["json"] == poll_body
    by = {q["id"]: q for q in snap["json"]["questions"]}
    assert by[mine["id"]]["mine"] is True and by[other["id"]]["voted"] is True and snap["json"]["poll"]["my_response"] == "Tea"

    # Without a key (or a key in the query string, for EventSource), as the poll.
    anon = await SSE(_s(world), {}, {"view": "room"}).open()
    queried = await SSE(_s(world), {}, {"view": "room", "key": "browser-00000001"}).open()
    await streams.step()
    assert (await anon.next_of("snapshot"))["json"] == (await client.get(_u(world))).json()
    assert (await queried.next_of("snapshot"))["json"] == poll_body
    for s in (one, anon, queried):
        await s.close()


async def test_the_keys_in_a_streams_query_reach_no_log_line(client, world, streams, monkeypatch):
    """EventSource sends the browser's key, the player's key and the resume
    point in the query string, and a player key is enough to answer as that
    player: no log line carries them, nor a token -- not the access line
    (plain or JSON, as uvicorn writes it for this very request), not what
    the app logs while the stream runs, not Sentry's event or breadcrumb."""
    import logging

    from uvicorn.protocols.utils import get_path_with_query_string

    from app.core import observability

    code = await _quiz(client, world)
    player = (await _join(client, code, 1))["player_key"]
    secrets = {"key": "browser-00000001", "player": player, "last_event_id": "1760793812345678", "token": "tok-0123456789abcdef"}
    lines: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(record)

    keep = Keep(level=logging.DEBUG)
    root, access = logging.getLogger(), logging.getLogger("uvicorn.access")
    monkeypatch.setattr(root, "level", logging.DEBUG)
    root.addHandler(keep)
    try:
        for view in ("room", "quiz"):
            s = await SSE(_s(world), {}, {"view": view, **secrets}).open()
            assert s.status == 200
            await streams.step()
            await s.next_of("snapshot")
            await s.close()
            # The access line uvicorn writes for it, with uvicorn's own helper
            # (the path argument carries the query string).
            scope = {"path": _s(world), "query_string": s.query.encode(), "method": "GET", "http_version": "1.1"}
            assert all(v in s.query for v in ("browser-00000001", "1760793812345678"))
            access.info('%s - "%s %s HTTP/%s" %d', f"{HALL}:50000", "GET", get_path_with_query_string(scope), "1.1", 200)
    finally:
        root.removeHandler(keep)

    written = [logging.Formatter("%(name)s %(message)s").format(r) for r in lines]
    written += [observability.JsonFormatter().format(r) for r in lines]
    assert any("live/stream?" in w and "view=quiz" in w for w in written)  # the access lines are there
    for value in secrets.values():
        assert not [w for w in written if value in w], value
    assert any("player=[Filtered]" in w for w in written)

    # Sentry's request and a breadcrumb, as its integrations make them.
    url = f"http://testserver{_s(world)}?{s.query}"
    event = observability.scrub_event({"request": {"url": url, "query_string": s.query, "method": "GET"}, "breadcrumbs": {"values": [{"category": "httplib", "data": {"url": url}}]}})
    crumb = observability.scrub_breadcrumb({"category": "httplib", "message": f"GET {url}", "data": {"url": url}})
    for value in secrets.values():
        assert value not in json.dumps(event) and value not in json.dumps(crumb), value


async def test_the_room_is_built_once_per_change_however_many_listen(client, world, streams):
    mine, other, poll = await _busy_room(client, world)
    subs = [await SSE(_s(world), _key(100 + i), {"view": "room"}).open() for i in range(20)]
    subs.append(await SSE(_s(world), _key(1), {"view": "room"}).open())
    await streams.step()
    feed = streams.feeds[world["event"].id]
    assert feed.builds == 1
    frames = [await s.next_of("snapshot") for s in subs]
    assert len({f["data"] for f in frames[:20]}) == 1 and frames[20]["data"] != frames[0]["data"]
    # Twenty browsers answer the poll alike: one more body, not twenty.
    for i in range(20):
        await client.post(_u(world, f"/polls/{poll['id']}/respond"), json={"value": "Coffee"}, headers=_key(100 + i))
    await streams.step()
    assert feed.builds == 2
    frames = [await s.next_of("snapshot") for s in subs]
    assert len({f["data"] for f in frames[:20]}) == 1 and frames[0]["json"]["poll"]["my_response"] == "Coffee"
    assert len(feed._frames) == 2  # the twenty's, browser one's
    for s in subs:
        await s.close()


async def test_the_room_stream_is_a_404_where_the_poll_is(client, session, world, streams):
    from app.api.closeout.models import EventStatus

    world["event"].lifecycle = EventStatus.draft
    await session.commit()
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    assert s.status == 404 and (await client.get(_u(world))).status_code == 404
    assert streams.open == 0
    bad = await SSE(_s(world), _key(1), {"view": "everything"}).open()
    assert bad.status == 422


async def test_the_banner_view_is_the_rooms_strip_sent_only_when_it_changes(client, session, world, streams):
    """The event page's live strip: the room feed's event and banner, with
    the event's start and end, a few hundred bytes; open to whoever may see
    the room; an audience's change (it shows the strip nothing new) is not
    sent, an update pushed is."""
    from app.api.closeout.models import EventStatus

    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=world["h"])
    s = await SSE(_s(world), {}, {"view": "banner"}).open()
    assert s.status == 200 and s.response_headers["content-type"].startswith("text/event-stream")
    await streams.step()
    snap = await s.next_of("snapshot")
    feed = (await client.get(_u(world))).json()
    event = world["event"]
    assert set(snap["json"]) == {"event", "banner"} and snap["json"]["banner"] is None and feed["banner"] is None
    strip_event = snap["json"]["event"]
    assert {k: strip_event[k] for k in feed["event"]} == feed["event"] and set(strip_event) == set(feed["event"]) | {"starts_at", "ends_at"}
    assert datetime.fromisoformat(strip_event["starts_at"].replace("Z", "+00:00")) == event.event_datetime
    assert datetime.fromisoformat(strip_event["ends_at"].replace("Z", "+00:00")) == event.event_datetime + timedelta(hours=event.duration)
    assert len(snap["data"]) < 400

    # The audience asks and votes: the room moves on, the strip does not.
    asked = (await client.post(_u(world, "/questions"), json={"text": "Is there a break?"}, headers=_key(7))).json()
    await client.post(_u(world, f"/questions/{asked['id']}/vote"), headers=_key(8))
    await bus.drain()
    await streams.step()
    await s.nothing()

    # The host pushes an update: the banner shows it, the same as the room's.
    sent = await client.post(_u(world, "/updates"), json={"kind": "general", "text": "Doors open at seven", "audience": "registered"}, headers=world["h"])
    assert sent.status_code == 201
    await bus.drain()
    await streams.step()
    snap = await s.next_of("snapshot")
    feed = (await client.get(_u(world))).json()
    assert snap["json"]["banner"] == feed["banner"] and feed["banner"]["text"] == "Doors open at seven"
    assert len(snap["data"]) < 600 and len(snap["data"]) * 2 < len(json.dumps(feed))

    # Gone where the room is.
    event.lifecycle = EventStatus.draft
    await session.commit()
    await streams.step(resync=True)
    await streams.step()
    assert (await s.next_of("gone"))["json"]["status"] == 404
    await asyncio.wait_for(s.task, 2)
    assert (await SSE(_s(world), {}, {"view": "banner"}).open()).status == 404


async def test_the_quiz_stream_is_a_players_and_sends_their_poll(client, world, streams):
    code = await _quiz(client, world, questions=2)
    players = [await _join(client, code, i) for i in range(3)]
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    await client.post(_u(world, "/quiz/answer"), json={"option_key": "a"}, headers={"X-Live-Player": players[0]["player_key"]})
    await client.post(_u(world, "/quiz/answer"), json={"option_key": "b"}, headers={"X-Live-Player": players[1]["player_key"]})

    assert (await SSE(_s(world), {}, {"view": "quiz"}).open()).status == 401
    assert (await SSE(_s(world), {"X-Live-Player": "not-a-player-key-at-all"}, {"view": "quiz"}).open()).status == 401
    for p in players:
        s = await SSE(_s(world), {"X-Live-Player": p["player_key"]}, {"view": "quiz"}).open()
        assert s.status == 200
        await streams.step()
        snap = await s.next_of("snapshot")
        polled = (await client.get(_u(world, "/quiz/play"), headers={"X-Live-Player": p["player_key"]})).json()
        assert _no_clock(snap["json"]) == _no_clock(polled)
        assert snap["json"]["name"] == p["name"] and snap["json"]["rank"] == polled["rank"]
        await s.close()
    # Through the query string too (EventSource).
    s = await SSE(_s(world), {}, {"view": "quiz", "player": players[0]["player_key"]}).open()
    await streams.step()
    assert (await s.next_of("snapshot"))["json"]["question"]["my_answer"] == "a"
    await s.close()


@pytest.mark.parametrize("view,poll", [("host", "/quiz/host"), ("screen", "/screen"), ("questions", "/questions"), ("polls", "/polls")])
async def test_the_clubs_views_are_the_clubs_and_send_its_polls(client, world, streams, view, poll):
    await _busy_room(client, world)
    code = await _quiz(client, world)
    await _join(client, code, 5)
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    assert (await SSE(_s(world), {}, {"view": view}).open()).status == 401
    assert (await SSE(_s(world), world["other"], {"view": view}).open()).status == 403
    # A browser key is not a club's session.
    assert (await SSE(_s(world), _key(1), {"view": view}).open()).status == 401
    s = await SSE(_s(world), world["h"], {"view": view}).open()
    assert s.status == 200
    await streams.step()
    snap = await s.next_of("snapshot")
    assert _no_clock(snap["json"]) == _no_clock((await client.get(_u(world, poll), headers=world["h"])).json())
    await s.close()


# ─── Versions, resuming, pings ───────────────────────────────────────────


async def test_last_event_id_resumes_and_an_unchanged_version_is_not_sent_again(client, world, streams, monkeypatch):
    await _busy_room(client, world)
    first = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    v = int((await first.next_of("snapshot"))["id"])
    await first.close()

    # Back with the version it has: a ping, no snapshot.
    again = await SSE(_s(world), {**_key(1), "Last-Event-ID": str(v)}, {"view": "room"}).open()
    await streams.step()
    assert (await again.frame())["retry"] == "3000"
    assert (await again.frame())["event"] == "ping"
    await again.nothing()
    # A change: the next version, sent once.
    await client.post(_u(world, "/questions"), json={"text": "A new one from the floor"}, headers=_key(3))
    await streams.step()
    snap = await again.next_of("snapshot")
    assert int(snap["id"]) > v and "A new one from the floor" in snap["data"]
    await again.nothing()
    v2 = int(snap["id"])
    await again.close()

    # ?last_event_id= (EventSource after a reload) works the same; an old id gets the snapshot.
    same = await SSE(_s(world), _key(1), {"view": "room", "last_event_id": v2}).open()
    old = await SSE(_s(world), {**_key(1), "Last-Event-ID": str(v)}, {"view": "room"}).open()
    await streams.step()
    assert (await same.frame())["retry"] and (await same.frame())["event"] == "ping"
    assert int((await old.next_of("snapshot"))["id"]) == v2
    await same.close()
    await old.close()


async def test_a_quiet_stream_is_pinged(client, world, streams, monkeypatch):
    monkeypatch.setattr(stream, "PING_SECONDS", 0.2)
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    await s.next_of("snapshot")
    before = datetime.now(timezone.utc).timestamp() * 1000
    ping = await s.next_of("ping", timeout=2)
    assert ping["id"] is None and abs(ping["json"]["t"] - before) < 5000
    await s.next_of("ping", timeout=2)
    await s.close()


async def test_a_stream_ends_after_its_lifetime_with_a_jittered_retry(client, world, streams, monkeypatch):
    monkeypatch.setattr(stream, "LIFETIME", 0.3)
    s = await SSE(_s(world), _key(2), {"view": "room"}).open()
    await streams.step()
    await s.next_of("snapshot")
    frames = []
    while (f := await s.frame(timeout=3)) is not None:
        frames.append(f)
    assert s.ended and frames[-1]["retry"] is not None and 3000 <= int(frames[-1]["retry"]) <= 6000
    await asyncio.wait_for(s.task, 2)
    assert not streams.feeds[world["event"].id].listeners and streams.open == 0


# ─── The audience folded, the host at once ───────────────────────────────


async def test_the_audiences_changes_are_folded_and_the_hosts_told_at_once(client, world, streams, monkeypatch):
    # A long window, closed by the test: what lands in it does not depend on
    # how fast this machine runs twelve requests.
    monkeypatch.setattr(bus, "COALESCE_SECONDS", 30.0)
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=world["h"])
    asked = (await client.post(_u(world, "/questions"), json={"text": "Vote this one up please"}, headers=_key(90))).json()
    await bus.drain()
    eid = world["event"].id
    heard = redis.subscribe(bus.channel(eid))
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    v0 = int((await s.next_of("snapshot"))["id"])

    loop = asyncio.get_running_loop()
    for i in range(12):
        assert (await client.post(_u(world, f"/questions/{asked['id']}/vote"), headers=_key(i))).status_code == 200
    # Nothing is told while the window is open: one timer, set by the first vote.
    pending = bus._pending[eid]
    assert pending.timer is not None and 0 < pending.timer.when() - loop.time() <= 30.0 and len(pending.deltas) == 12
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(heard.__anext__(), 0.1)
    # The window closes: one message, every browser's vote in it.
    pending.timer.cancel()
    bus._due(eid)
    message = await asyncio.wait_for(heard.__anext__(), 1.0)
    assert message["t"] == ["room"] and len(message["d"]) == 12 and message["v"] > v0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(heard.__anext__(), 0.2)
    await streams.step()
    snap = await s.next_of("snapshot")
    assert int(snap["id"]) == message["v"]
    q = next(q for q in snap["json"]["questions"] if q["id"] == asked["id"])
    assert q["votes"] == 12 and q["voted"] is True  # browser one's own vote, from the delta
    await s.nothing()

    # The host's change goes at once, folding in what was gathered.
    await client.post(_u(world, "/questions"), json={"text": "One more from the floor"}, headers=_key(50))
    assert bus._pending[eid].timer is not None
    await client.post(_u(world, f"/questions/{asked['id']}"), json={"action": "pin"}, headers=world["h"])
    message = await asyncio.wait_for(heard.__anext__(), 1.0)
    assert message["t"] == ["room"] and eid not in bus._pending
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(heard.__anext__(), 0.4)
    await streams.step()
    snap = await s.next_of("snapshot")
    assert snap["json"]["questions"][0]["pinned"] is True and "One more from the floor" in snap["data"]
    await heard.aclose()
    await s.close()


async def test_the_room_feed_paces_the_audiences_changes_but_not_the_hosts(client, world, streams, monkeypatch):
    monkeypatch.setattr(stream, "PACE", {"room": 0.6})
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=world["h"])
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    await s.next_of("snapshot")
    loop = asyncio.get_running_loop()
    # Two of the audience's changes, moments after the last frame: one frame,
    # the latest, when the pace allows.
    t0 = loop.time()
    await client.post(_u(world, "/questions"), json={"text": "First from the floor"}, headers=_key(2))
    await streams.step()
    await client.post(_u(world, "/questions"), json={"text": "Second from the floor"}, headers=_key(3))
    await streams.step()
    paced = await s.next_of("snapshot", timeout=2)
    assert loop.time() - t0 >= 0.45 and "Second from the floor" in paced["data"]
    assert int(paced["id"]) == streams.feeds[world["event"].id].version("room")
    # The host's change right after: at once.
    asked = next(q for q in paced["json"]["questions"] if q["text"] == "First from the floor")
    t1 = loop.time()
    await client.post(_u(world, f"/questions/{asked['id']}"), json={"action": "pin"}, headers=world["h"])
    await streams.step()
    pinned = await s.next_of("snapshot", timeout=2)
    assert loop.time() - t1 < 0.4 and pinned["json"]["questions"][0]["pinned"] is True
    # And the host's change while an audience frame is held goes at once, with it.
    await client.post(_u(world, "/questions"), json={"text": "Third from the floor"}, headers=_key(4))
    await streams.step()
    await asyncio.sleep(0.05)
    t2 = loop.time()
    await client.post(_u(world, f"/questions/{asked['id']}"), json={"action": "unpin"}, headers=world["h"])
    await streams.step()
    both = await s.next_of("snapshot", timeout=2)
    assert loop.time() - t2 < 0.4 and "Third from the floor" in both["data"] and not both["json"]["questions"][0]["pinned"]
    await s.close()


async def test_a_slow_phone_skips_to_the_latest_version_and_nothing_queues(client, world, streams):
    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=world["h"])
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    await s.next_of("snapshot")
    s.stalled = asyncio.Event()  # the phone stops reading
    versions = []
    for i in range(5):
        await client.post(_u(world, "/questions"), json={"text": f"Asked while the phone slept {i}"}, headers=_key(10 + i))
        await streams.step()
        versions.append(streams.feeds[world["event"].id].version("room"))
        await asyncio.sleep(0.01)
    assert len(set(versions)) == 5
    s.stalled.set()
    frames = []
    while True:
        try:
            frames.append(await s.next_of("snapshot", timeout=0.4))
        except asyncio.TimeoutError:
            break
    ids = [int(f["id"]) for f in frames]
    # At most the one in hand when it stalled, then the latest: never the five.
    assert ids[-1] == versions[-1] and len(ids) <= 2, ids
    assert all(f"Asked while the phone slept {i}" in frames[-1]["data"] for i in range(5))
    await s.close()


# ─── Bounds ──────────────────────────────────────────────────────────────


async def test_open_streams_are_bounded_per_process_event_and_address(client, world, streams, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_SSE_MAX_STREAMS", 2)
    a = await SSE(_s(world), _key(1), {"view": "room"}).open()
    b = await SSE(_s(world), _key(2), {"view": "room"}).open()
    full = await SSE(_s(world), _key(3), {"view": "room"}).open()
    assert full.status == 503 and full.json()["error_code"] == "LIVE_STREAM_FULL" and full.response_headers["retry-after"] == "60"
    await a.close()
    c = await SSE(_s(world), _key(3), {"view": "room"}).open()
    assert c.status == 200 and streams.open == 2
    for s in (b, c):
        await s.close()
    assert streams.open == 0 and not streams.by_address

    monkeypatch.setattr(settings, "LIVE_SSE_MAX_STREAMS", 100)
    monkeypatch.setattr(stream, "PER_ADDRESS", 1)
    a = await SSE(_s(world), _key(1), {"view": "room"}, address="106.51.1.1").open()
    assert (await SSE(_s(world), _key(2), {"view": "room"}, address="106.51.1.1").open()).status == 503
    b = await SSE(_s(world), _key(2), {"view": "room"}, address="106.51.1.2").open()
    assert b.status == 200
    monkeypatch.setattr(stream, "PER_ADDRESS", 100)
    monkeypatch.setattr(stream, "PER_EVENT", 2)
    assert (await SSE(_s(world), _key(3), {"view": "room"}, address="106.51.1.3").open()).status == 503
    for s in (a, b):
        await s.close()

    # Opening counts against the room's limits, per browser key.
    monkeypatch.setitem(limits.RULES, "stream", limits.Rule("2/minute", "100/minute"))
    for _ in range(2):
        await (await SSE(_s(world), _key(7), {"view": "room"}).open()).close()
    over = await SSE(_s(world), _key(7), {"view": "room"}).open()
    assert over.status == 429 and over.json()["error_code"] == "RATE_LIMITED"
    assert streams.open == 0


async def test_a_disconnect_leaves_nothing_behind(client, world, monkeypatch, session):
    """The real pump: two phones come and go; their streams, the feed, its
    subscription and its tasks are gone after."""
    from contextlib import asynccontextmanager

    monkeypatch.setattr(settings, "LIVE_SSE", True)
    hub = stream.Hub()
    monkeypatch.setattr(stream, "hub", hub)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(stream, "session_scope", scope)
    monkeypatch.setattr(stream, "RESYNC_SECONDS", 3600.0)
    monkeypatch.setattr(stream, "LINGER_SECONDS", 0.2)
    channel = bus.channel(world["event"].id)
    # One at a time: the pump builds on the test's session, as a request does.
    a = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await a.next_of("snapshot")
    b = await SSE(_s(world), _key(2), {"view": "room"}).open()
    await b.next_of("snapshot")
    feed = hub.feeds[world["event"].id]
    assert len(feed.listeners) == 2 and hub.open == 2 and redis.subscribers(channel) == 1
    await a.close()
    assert len(feed.listeners) == 1 and hub.open == 1
    await b.close()
    assert not feed.listeners and hub.open == 0 and not hub.by_address
    await asyncio.wait_for(feed.task, 2)
    assert world["event"].id not in hub.feeds and redis.subscribers(channel) == 0
    assert feed.reader is None and feed.sub is None
    left = [t for t in asyncio.all_tasks() if (t.get_name() or "") in (f"live-feed:{feed.event_id}", f"live-feed-read:{feed.event_id}") and not t.done()]
    assert left == []


# ─── Gone, and the quiz's clock ──────────────────────────────────────────


async def test_a_room_that_closes_is_gone_and_the_stream_ends(client, session, world, streams):
    from app.api.closeout.models import EventStatus

    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    await s.next_of("snapshot")
    # Changed outside the live room: nothing told, the resync finds it.
    world["event"].lifecycle = EventStatus.draft
    await session.commit()
    await streams.step(resync=True)
    await streams.step()
    gone = await s.next_of("gone")
    assert gone["json"]["status"] == 404
    assert await s.frame() is None and s.ended
    await asyncio.wait_for(s.task, 2)


async def test_a_reset_quiz_sends_its_players_away(client, world, streams):
    code = await _quiz(client, world)
    p = await _join(client, code, 1)
    s = await SSE(_s(world), {"X-Live-Player": p["player_key"]}, {"view": "quiz"}).open()
    await streams.step()
    await s.next_of("snapshot")
    await client.post(_u(world, "/quiz/host/reset"), headers=world["h"])
    await streams.step()
    gone = await s.next_of("gone")
    assert gone["json"]["status"] == 401
    assert await s.frame() is None


async def test_when_the_clock_runs_out_the_players_are_shown_the_answer(client, session, world, streams):
    code = await _quiz(client, world, seconds=5)
    p = await _join(client, code, 1)
    await client.post(_u(world, "/quiz/host/next"), headers=world["h"])
    # The question has been open three and a half of its five seconds: the
    # poll counts 0, and shows the answer, from four.
    await session.execute(update(LiveQuizzes).where(LiveQuizzes.event_id == world["event"].id).values(
        question_opened_at=datetime.now(timezone.utc) - timedelta(seconds=3.5)))
    await session.commit()
    s = await SSE(_s(world), {"X-Live-Player": p["player_key"]}, {"view": "quiz"}).open()
    await streams.step()
    snap = await s.next_of("snapshot")
    assert snap["json"]["question"]["correct_key"] is None and snap["json"]["question"]["time_left"] == 1
    await asyncio.sleep(0.7)
    await streams.step()   # the clock is due: the quiz is told
    await streams.step()   # and rebuilt
    after = await s.next_of("snapshot")
    assert int(after["id"]) > int(snap["id"])
    assert after["json"]["question"]["correct_key"] == "a" and after["json"]["question"]["time_left"] == 0
    await s.close()


# ─── Across processes, through Redis ─────────────────────────────────────


async def _arrives(feed, timeout=2.0):
    loop = asyncio.get_running_loop()
    ends = loop.time() + timeout
    while feed.inbox.empty():
        assert loop.time() < ends, "nothing arrived from Redis"
        await asyncio.sleep(0.01)


@needs_redis
async def test_a_change_told_by_another_process_reaches_the_stream_through_redis(client, session, world, streams, with_redis):
    import redis.asyncio as aioredis

    await client.patch(_u(world, "/questions/settings"), json={"approve_first": False}, headers=world["h"])
    asked = (await client.post(_u(world, "/questions"), json={"text": "Before the other process"}, headers=_key(5))).json()
    await bus.drain()
    s = await SSE(_s(world), _key(1), {"view": "room"}).open()
    await streams.step()
    v0 = int((await s.next_of("snapshot"))["id"])
    feed = streams.feeds[world["event"].id]
    # Versions come from Redis: another process's next one is above this one's.
    assert int(await (other := aioredis.from_url(REDIS_URL)).get(f"moa:live:v:{world['event'].id}")) == v0
    # Another process changes the room and tells it; this one hears it from Redis.
    await session.execute(text("UPDATE live_questions SET votes = 41 WHERE id = :id"), {"id": asked["id"]})
    await session.commit()
    v1 = await other.incr(f"moa:live:v:{world['event'].id}")
    await other.publish(f"moa:ch:live:{world['event'].id}", json.dumps({"v": v1, "t": ["room"], "d": [["v", "browser-00000001", asked["id"], 1]], "x": False}))
    await _arrives(feed)
    await streams.step()
    snap = await s.next_of("snapshot")
    q = next(q for q in snap["json"]["questions"] if q["id"] == asked["id"])
    assert int(snap["id"]) == v1 and q["votes"] == 41 and q["voted"] is True
    # And this process's own change goes out through Redis too (and comes back).
    bus.reset_for_tests()
    await client.post(_u(world, f"/questions/{asked['id']}"), json={"action": "pin"}, headers=world["h"])
    await bus.drain()
    await _arrives(feed)
    await streams.step()
    pinned = await s.next_of("snapshot")
    assert int(pinned["id"]) > v1 and pinned["json"]["questions"][0]["pinned"] is True
    await other.aclose()
    await s.close()


@needs_redis
async def test_the_quiz_stream_ranks_from_the_board_in_redis(client, session, world, streams, with_redis):
    players = await _scored_quiz(client, session, world, [500, 500, 1000, 0])
    subs = []
    for p in players:
        subs.append(await SSE(_s(world), {"X-Live-Player": p["player_key"]}, {"view": "quiz"}).open())
    await streams.step()
    feed = streams.feeds[world["event"].id]
    assert feed.quiz.board_key is not None
    for p, s in zip(players, subs):
        snap = await s.next_of("snapshot")
        polled = (await client.get(_u(world, "/quiz/play"), headers={"X-Live-Player": p["player_key"]})).json()
        assert _no_clock(snap["json"]) == _no_clock(polled)
    assert [ (await room.standings(session, feed.quiz, [feed.quiz.players[p["player_key"]]]))[feed.quiz.players[p["player_key"]].id][1] for p in players] == [2, 2, 1, 4]
    for s in subs:
        await s.close()
