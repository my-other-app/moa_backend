"""The uptime monitor's two URLs (app/api/health.py):

- GET /health: liveness. The process answers; nothing else is asked.
- GET /health/ready: the database answers within a second, and Redis when
  APP_REDIS_URL is set; 503 otherwise.

Both: no sign-in, never rate limited, `Cache-Control: no-store`, HEAD as
well as GET, and nothing in the body but the status, the checks and a short
git sha -- no versions, no migration names, no settings."""
import asyncio
import time

import pytest

from app.api import health
from app.config import settings
from app.core import observability, redis
from app.core.rate_limit import limiter

PATHS = ("/health", "/health/ready")


@pytest.fixture
def sha(monkeypatch):
    monkeypatch.setattr(observability, "revision", lambda: "0123456789abcdef0123456789abcdef01234567")
    health._short_revision.cache_clear()
    yield "0123456"
    health._short_revision.cache_clear()


async def test_liveness_asks_nothing(client, monkeypatch, sha):
    async def boom(*_a, **_kw):
        raise AssertionError("liveness must not touch the database")

    monkeypatch.setattr(health, "ping_database", boom)
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "revision": sha}
    assert r.headers["cache-control"] == "no-store"


async def test_ready_with_the_real_database(client, session, sha):
    r = await client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "checks": {"database": "ok"}, "revision": sha}
    assert r.headers["cache-control"] == "no-store"


async def test_not_ready_without_the_database(client, monkeypatch):
    from app.db import core

    class Down:
        def connect(self):
            raise ConnectionRefusedError("no database at postgres:5432 for moa:secret")

    monkeypatch.setattr(core, "engine", Down())
    r = await client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "fail" and body["checks"] == {"database": "fail"}
    assert "secret" not in r.text and "postgres" not in r.text  # never the error's text
    assert r.headers["cache-control"] == "no-store"


async def test_a_slow_database_is_not_ready_within_a_second(client, monkeypatch):
    async def stuck(conn):
        await asyncio.sleep(10)

    monkeypatch.setattr(health, "_select_one", stuck)
    began = time.monotonic()
    r = await client.get("/health/ready")
    took = time.monotonic() - began
    assert r.status_code == 503 and r.json()["checks"]["database"] == "fail"
    assert took < 2.5, took
    assert health.READY_TIMEOUT == 1.0


async def test_redis_is_asked_only_when_configured(client, session, monkeypatch):
    assert "redis" not in (await client.get("/health/ready")).json()["checks"]
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens on port 1
    redis.reset_for_tests()
    try:
        r = await client.get("/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"] == {"database": "ok", "redis": "fail"}
    finally:
        await redis.close()
        redis.reset_for_tests()


@pytest.mark.parametrize("path", PATHS)
async def test_head_works_for_monitors(client, session, path):
    r = await client.head(path)
    assert r.status_code == 200 and r.content == b""
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", PATHS)
async def test_never_rate_limited_and_no_sign_in(client, session, path):
    assert limiter.enabled
    codes = {(await client.get(path)).status_code for _ in range(60)}
    assert codes == {200}
    # Exempt by name, so a default limit added later still leaves them alone.
    names = {f"{f.__module__}.{f.__name__}" for f in (health.health, health.health_ready)}
    assert names <= set(limiter._exempt_routes)


async def test_the_body_says_nothing_about_the_build(client, session, monkeypatch):
    monkeypatch.setattr(observability, "revision", lambda: None)
    health._short_revision.cache_clear()
    try:
        body = (await client.get("/health/ready")).json()
    finally:
        health._short_revision.cache_clear()
    assert body == {"status": "ok", "checks": {"database": "ok"}, "revision": None}


async def test_the_older_probes_are_not_cached_either(client, session):
    for path in ("/healthz", "/readyz"):
        r = await client.get(path)
        assert r.headers["cache-control"] == "no-store", path
