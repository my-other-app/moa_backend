"""What the Redis paths cost (app/core/redis.py): a rate-limit hit in memory
and in Redis, a cold discover key read from a sibling process's value, and
a message published in one process and heard in another.

    REDIS_BENCH_URL=redis://localhost:6379/13 python -m scripts.bench.redis_paths

Uses only keys under moa: in that Redis database, and deletes them after.
"""
from __future__ import annotations

import asyncio
import json
import os
import pickle
import statistics
import time

from scripts.bench import _env  # noqa: F401  (settings before app)

URL = os.environ.get("REDIS_BENCH_URL", "redis://localhost:6379/13")


def _pct(xs: list[float]) -> str:
    q = statistics.quantiles(xs, n=100)
    return f"p50 {q[49]:.3f} ms, p95 {q[94]:.3f} ms"


async def main() -> None:
    import redis.asyncio as aioredis
    from limits import parse
    from slowapi.util import get_remote_address

    from app.api.discover.cache import TTLCache
    from app.config import settings
    from app.core import redis
    from app.core.rate_limit import SharedLimiter, storage_options

    item = parse("100000/minute")
    for uri in ("memory://", URL):
        lim = SharedLimiter(key_func=get_remote_address, storage_uri=uri, storage_options=storage_options(uri), in_memory_fallback_enabled=True)
        xs = []
        for i in range(3000):
            t = time.perf_counter()
            lim.limiter.hit(item, "bench", f"k{i % 50}")
            xs.append((time.perf_counter() - t) * 1000)
        print(f"a rate-limit hit, {uri.split(':')[0]}: {_pct(xs[200:])}")

    settings.REDIS_URL = URL
    value = {"events": [{"id": i, "name": f"Event {i}", "starts": "2026-11-20T18:00:00+05:30", "club": {"id": i % 90, "name": "Club"}, "topics": ["a", "b"]} for i in range(400)]}
    print(f"the value, pickled: {len(pickle.dumps(value)) / 1024:.0f} KB")

    async def build():
        return value

    await TTLCache(ttl=60.0, register_it=False, shared_as="bench").remember(("home",), build)
    xs = []
    for _ in range(500):
        sibling = TTLCache(ttl=60.0, register_it=False, shared_as="bench")  # another process: nothing kept locally
        t = time.perf_counter()
        await sibling.remember(("home",), build)
        xs.append((time.perf_counter() - t) * 1000)
    print(f"a cold key read from a sibling's value: {_pct(xs[50:])}")

    sub = redis.subscribe("bench")
    await asyncio.sleep(0.3)
    other = aioredis.from_url(URL)
    xs = []
    for i in range(300):
        t = time.perf_counter()
        await other.publish(redis.PREFIX + "ch:bench", json.dumps({"i": i}))
        await sub.__anext__()
        xs.append((time.perf_counter() - t) * 1000)
    print(f"published in one process, heard in another: {_pct(xs[30:])}")
    await sub.aclose()
    keys = [k async for k in other.scan_iter(match="moa:*")]
    if keys:
        await other.delete(*keys)
    await other.aclose()
    await redis.close()


if __name__ == "__main__":
    asyncio.run(main())
