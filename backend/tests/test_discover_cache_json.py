"""The discover cache's shared level keeps values as JSON, never pickled
(app/api/discover/cache.py): the types it holds come back as they were, a
value written by an older release is a miss, and a value JSON cannot carry
stays in the process."""

import enum
import pickle
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.api.discover import cache


class _Redis:
    """Enough of a Redis client for Shared.get/put."""

    def __init__(self):
        self.d = {}

    async def mget(self, *keys):
        return [self.d.get(k) for k in keys]

    async def set(self, k, v, px=None, nx=False):
        if nx and k in self.d:
            return None
        self.d[k] = v if isinstance(v, bytes) else str(v).encode()
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    from app.core import redis

    r = _Redis()
    monkeypatch.setattr(redis, "available", lambda: r)
    return r


VALUE = {
    "items": [
        {
            "id": 1,
            "name": "Build Night",
            "starts": datetime(2026, 11, 28, 9, 30, tzinfo=timezone.utc),
        }
    ],
    "counts": {1: 4, 2: 0},
    "cell": (12.5, 76.25),
    "day": date(2026, 11, 28),
    "price": Decimal("199.00"),
    "ids": {3, 5},
    "frozen": frozenset({"a"}),
    "uuid": uuid.UUID("3f2c1a9e-8b7d-4c6e-9f10-123456789012"),
    "~": "a key that looks like the tag",
    "nested": [[1, (2, 3)], {"x": None, "y": True, "z": 1.5}],
}


def test_the_types_the_cache_holds_come_back_as_they_were():
    entry = (3, 1.5, 2.5, VALUE)
    back = cache._loads(cache._dumps(entry))
    assert back == entry
    assert (
        type(back[3]["cell"]) is tuple
        and type(back[3]["ids"]) is set
        and type(back[3]["frozen"]) is frozenset
    )
    assert (
        list(back[3]["counts"]) == [1, 2]
        and back[3]["items"][0]["starts"].tzinfo is not None
    )


class _Kind(str, enum.Enum):
    A = "a"


@pytest.mark.parametrize(
    "value", [object(), _Kind.A, {"k": _Kind.A}, {_Kind.A: 1}, b"bytes"]
)
def test_a_value_json_cannot_carry_is_refused(value):
    with pytest.raises(TypeError):
        cache._dumps((1, 1.0, 2.0, value))


@pytest.mark.parametrize(
    "raw", [pickle.dumps((1, 1.0, 2.0, {"a": 1})), b"not json", b"j1:{broken", "text"]
)
def test_anything_else_in_redis_is_never_unpickled(raw, monkeypatch):
    monkeypatch.setattr(pickle, "loads", lambda *a, **k: pytest.fail("unpickled"))
    with pytest.raises(Exception):
        cache._loads(raw)


async def test_put_then_get_through_redis(fake_redis):
    shared = cache.Shared("json-test")
    await shared.put(("home",), 0, VALUE, ttl=60.0, stale=30.0)
    stored = next(v for k, v in fake_redis.d.items() if ":v:" in k)
    assert stored.startswith(b"j1:")
    gen, got = await shared.get(("home",))
    assert gen == 0 and got.value == VALUE and got.fresh > 0


async def test_an_older_releases_pickled_entry_is_a_miss(fake_redis, monkeypatch):
    shared = cache.Shared("json-test")
    value_key, _, _ = shared._keys(("home",))
    now = time.time()
    fake_redis.d[value_key] = pickle.dumps((0, now + 60, now + 90, {"old": True}))
    monkeypatch.setattr(pickle, "loads", lambda *a, **k: pytest.fail("unpickled"))
    gen, got = await shared.get(("home",))
    assert gen == 0 and got is None


async def test_a_value_it_cannot_carry_stays_in_this_process(fake_redis):
    shared = cache.Shared("json-test")
    await shared.put(("home",), 0, {"k": object()}, ttl=60.0, stale=30.0)
    assert not [k for k in fake_redis.d if ":v:" in k]
