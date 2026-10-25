"""Redis when there is one, and the way back when it is gone (app/core/redis.py):
publish/subscribe across processes, the shared rate limits, and the discover
cache's second level.

Against a real Redis -- APP_TEST_REDIS_URL, else a local one on database 13 --
skipped when none answers. "Another process" is a second client, limiter or
cache store with nothing in memory shared with the first. Every key made
here is under moa: in that database and removed after each test. "Redis
down" is a port nothing listens on.
"""
import asyncio
import json
import os
import time

import pytest

from app.config import settings
from app.core import redis

URL = os.environ.get("APP_TEST_REDIS_URL", "redis://localhost:6379/13")
DOWN = "redis://127.0.0.1:1/0"


def _reachable() -> bool:
    try:
        import redis as sync_redis

        return bool(sync_redis.from_url(URL, socket_timeout=0.5, socket_connect_timeout=0.5).ping())
    except Exception:  # noqa: BLE001
        return False


needs_redis = pytest.mark.skipif(not _reachable(), reason=f"no Redis at {URL}")


async def _wipe() -> None:
    import redis.asyncio as aioredis

    c = aioredis.from_url(URL)
    try:
        keys = [k async for k in c.scan_iter(match="moa:*", count=1000)]
        if keys:
            await c.delete(*keys)
    finally:
        await c.aclose()


# The suite runs each test on a loop of its own and closes it after (its
# fixtures run on the session's): the client and the subscription task a
# test makes go with it, and teardown only forgets them.


@pytest.fixture
async def with_redis(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", URL)
    redis.reset_for_tests()
    await _wipe()
    yield URL
    redis.reset_for_tests()
    await _wipe()


@pytest.fixture
def redis_down(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", DOWN)
    redis.reset_for_tests()
    yield
    redis.reset_for_tests()


async def _next(it, timeout=2.0):
    return await asyncio.wait_for(it.__anext__(), timeout)


# First, as it uses the database: a run whose first async test does not
# leaves the later database tests on another loop (the suite's
# conftest, before this change too).


async def test_a_limited_route_answers_with_redis_down(client):
    """The real app's limiter on a dead store: the request is limited in
    memory, not failed."""
    from app.core import rate_limit

    lim = rate_limit.limiter
    dead = _limiter(DOWN)
    kept = (lim._storage, lim._limiter, lim._storage_dead)
    lim._storage, lim._limiter, lim._storage_dead = dead._storage, dead._limiter, False
    try:
        r = await client.get("/api/v1/interests/topics")
        assert r.status_code == 200
        assert lim._storage_dead is True
    finally:
        lim._storage, lim._limiter, lim._storage_dead = kept


# ─── Publish / subscribe ─────────────────────────────────────────────────


async def test_without_redis_publish_reaches_this_processs_subscribers():
    assert redis.client() is None
    a, b = redis.subscribe("room:1"), redis.subscribe("room:1")
    other = redis.subscribe("room:2")
    # Subscribed when made: published before anyone waits, still heard.
    await redis.publish("room:1", {"vote": 3})
    assert await _next(a) == {"vote": 3} and await _next(b) == {"vote": 3}
    with pytest.raises(asyncio.TimeoutError):
        await _next(other, timeout=0.05)
    for it in (a, b, other):
        await it.aclose()
    assert redis.subscribers("room:1") == 0


@needs_redis
async def test_with_redis_messages_cross_processes(with_redis):
    import redis.asyncio as aioredis

    sub = redis.subscribe("room:7")
    got = asyncio.ensure_future(_next(sub))
    # The shared connection subscribes: waited for, not slept on (0.2 s was
    # not always enough on a loaded runner).
    for _ in range(100):
        if redis._hub.connected:
            break
        await asyncio.sleep(0.05)
    assert redis._hub.connected
    # Another process publishes: this one's subscriber hears it.
    other = aioredis.from_url(URL)
    await other.publish("moa:ch:room:7", json.dumps({"from": "elsewhere"}))
    assert await got == {"from": "elsewhere"}
    # This process publishes: another process's subscriber hears it, and so
    # does its own -- once, through Redis.
    theirs = other.pubsub()
    await theirs.subscribe("moa:ch:room:7")
    # Redis has the subscription once it confirms it.
    assert (await theirs.get_message(timeout=5.0))["type"] == "subscribe"
    mine = asyncio.ensure_future(_next(sub))
    await redis.publish("room:7", {"from": "here"})
    assert await mine == {"from": "here"}
    seen = None
    for _ in range(20):
        m = await theirs.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if m:
            seen = json.loads(m["data"])
            break
    assert seen == {"from": "here"}
    with pytest.raises(asyncio.TimeoutError):
        await _next(sub, timeout=0.2)  # not twice
    await theirs.aclose()
    await other.aclose()
    await sub.aclose()


async def test_with_redis_down_publish_falls_back_and_nothing_raises(redis_down):
    sub = redis.subscribe("room:9")
    got = asyncio.ensure_future(_next(sub))
    await asyncio.sleep(0)
    started = time.monotonic()
    await redis.publish("room:9", {"still": "here"})
    assert await got == {"still": "here"}
    assert await redis.ping() is False
    # Marked down: the next calls do not wait on it at all.
    assert redis.available() is None
    assert await redis.run(lambda c: c.get("x"), "fallback") == "fallback"
    assert time.monotonic() - started < 2.0
    await sub.aclose()


# ─── Rate limits ─────────────────────────────────────────────────────────


def _limiter(uri: str):
    from slowapi.util import get_remote_address

    from app.core.rate_limit import SharedLimiter, storage_options

    return SharedLimiter(key_func=get_remote_address, storage_uri=uri, storage_options=storage_options(uri), in_memory_fallback_enabled=True)


def test_the_limits_use_redis_when_it_is_set(monkeypatch):
    from app.core import rate_limit

    monkeypatch.setattr(settings, "RATE_LIMIT_STORAGE", "memory://")
    monkeypatch.setattr(settings, "REDIS_URL", "")
    assert rate_limit.storage_uri() == "memory://"
    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis:6379/0")
    assert rate_limit.storage_uri() == "redis://redis:6379/0"
    # A store named outright wins.
    monkeypatch.setattr(settings, "RATE_LIMIT_STORAGE", "redis://other:6379/2")
    assert rate_limit.storage_uri() == "redis://other:6379/2"
    assert rate_limit.storage_options("redis://x")["key_prefix"] == "moa:limits"
    assert rate_limit.storage_options("memory://") == {}


@needs_redis
async def test_two_processes_share_one_budget(with_redis):
    from limits import parse

    item = parse("3/minute")
    a, b = _limiter(URL), _limiter(URL)
    assert a.limiter.hit(item, "login", "1.2.3.4")
    assert b.limiter.hit(item, "login", "1.2.3.4")
    assert a.limiter.hit(item, "login", "1.2.3.4")
    # Per process each would allow three; together they have had three.
    assert not b.limiter.hit(item, "login", "1.2.3.4")
    assert b.limiter.hit(item, "login", "5.6.7.8")


def test_with_redis_down_the_limits_count_in_memory():
    from limits import parse

    item = parse("2/minute")
    lim = _limiter(DOWN)
    started = time.monotonic()
    assert lim.limiter.hit(item, "join", "k") and lim._storage_dead
    assert lim.limiter.hit(item, "join", "k")
    assert not lim.limiter.hit(item, "join", "k")
    assert time.monotonic() - started < 2.0


# ─── The discover cache's second level ───────────────────────────────────


def _store():
    from app.api.discover.cache import TTLCache

    return TTLCache(ttl=60.0, register_it=False, shared_as="test")


class Builds:
    def __init__(self, value="built", delay=0.0):
        self.n, self.value, self.delay = 0, value, delay

    async def __call__(self):
        self.n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"value": self.value, "n": self.n, "ids": {1: (1.5, 2.5)}}


@needs_redis
async def test_a_value_built_by_one_process_is_read_by_another(with_redis):
    a, b = _store(), _store()
    build = Builds()
    first = await a.remember(("home",), build)
    second = await b.remember(("home",), build)
    assert build.n == 1 and second == first and second["ids"] == {1: (1.5, 2.5)}  # types kept
    assert b.shared_hits == 1
    # And it is in b's own level now: no Redis call the next time.
    assert await b.remember(("home",), build) == first and b.hits == 1


@needs_redis
async def test_a_cold_key_is_built_once_across_processes(with_redis):
    stores = [_store() for _ in range(4)]
    build = Builds(delay=0.3)
    got = await asyncio.gather(*(s.remember(("categories", True), build) for s in stores))
    assert build.n == 1 and all(g == got[0] for g in got)


@needs_redis
async def test_stale_from_another_process_is_answered_and_rebuilt_behind(with_redis):
    a, b = _store(), _store()
    old = Builds("old")
    await a.remember(("home",), old, ttl=0.2, stale=30.0, refresh=old)
    await asyncio.sleep(0.3)  # past its time, inside its stale window
    new = Builds("new")
    got = await b.remember(("home",), new, ttl=60.0, stale=30.0, refresh=new)
    assert got["value"] == "old" and new.n == 0
    await b.settle()
    assert new.n == 1
    assert (await b.remember(("home",), new))["value"] == "new"
    # The rebuild went to Redis too: a third process reads the new one.
    c = _store()
    assert (await c.remember(("home",), Builds("never")))["value"] == "new"


@needs_redis
async def test_clear_in_one_process_stops_the_shared_value_being_read(with_redis):
    a, b = _store(), _store()
    await a.remember(("home",), Builds("before"))
    a.clear()
    await a.settle()
    again = Builds("after")
    assert (await b.remember(("home",), again))["value"] == "after" and again.n == 1


async def test_the_cache_works_alone_with_redis_down(redis_down):
    a = _store()
    build = Builds()
    started = time.monotonic()
    assert (await a.remember(("home",), build))["n"] == 1
    assert (await a.remember(("home",), build))["n"] == 1 and build.n == 1
    assert time.monotonic() - started < 2.0


@needs_redis
async def test_values_of_other_code_are_not_read(with_redis, monkeypatch):
    from app.api.discover import cache

    a = _store()
    await a.remember(("home",), Builds("old code"))
    monkeypatch.setattr(cache, "_code", "another-release")
    b = _store()
    assert (await b.remember(("home",), Builds("new code")))["value"] == "new code"
