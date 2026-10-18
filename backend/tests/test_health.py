"""Liveness, readiness, and the admin's view of the job queue (app/api/health.py)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api import health
from app.api.users.models import AdminRoles
from app.config import settings
from app.core import jobs, redis
from tests.conftest import auth_header


@pytest.fixture
def heads(monkeypatch):
    """The suite's schema comes from create_all, with no alembic_version: say
    which revision the database is at, and which the code's head is."""
    state = {"db": {"w2_platform"}, "code": {"w2_platform"}}

    async def check():
        return {
            "database": {"ok": True},
            "migrations": {"ok": state["db"] <= state["code"] and bool(state["db"]), "database": sorted(state["db"]), "code": sorted(state["code"])},
            "_alive": state.get("alive", []),
        }

    monkeypatch.setattr(health, "check_database", check)
    return state


async def test_healthz_asks_nothing(client, monkeypatch):
    async def boom():
        raise AssertionError("liveness must not touch the database")

    monkeypatch.setattr(health, "check_database", boom)
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_readyz_is_ready_at_the_head(client, heads):
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["checks"]["jobs"]["mode"] == "in-process"


async def test_readyz_is_not_ready_with_a_migration_to_run(client, heads):
    heads["db"] = {"topics"}
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["migrations"]["ok"] is False


async def test_readyz_is_not_ready_without_the_database(client, monkeypatch):
    from app.db import core

    class Down:
        def connect(self):
            raise ConnectionRefusedError("no database")

    monkeypatch.setattr(core, "engine", Down())
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["database"] == {"ok": False, "error": "ConnectionRefusedError"}


async def test_readyz_asks_redis_only_when_configured(client, heads, monkeypatch):
    r = await client.get("/readyz")
    assert "redis" not in r.json()["checks"]
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens on port 1
    redis.reset_for_tests()
    try:
        r = await client.get("/readyz")
        assert r.status_code == 503 and r.json()["checks"]["redis"] == {"ok": False}
    finally:
        await redis.close()
        redis.reset_for_tests()


async def test_readyz_warns_when_the_worker_is_missing_but_stays_ready(client, heads, monkeypatch):
    monkeypatch.setattr(settings, "JOBS_WORKER", True)
    r = await client.get("/readyz")
    body = r.json()
    assert r.status_code == 200 and "no worker" in body["checks"]["jobs"]["warning"]
    heads["alive"] = [{"kind": "worker", "sweeps": False}]
    body = (await client.get("/readyz")).json()
    assert "warning" not in body["checks"]["jobs"] and body["checks"]["jobs"]["kinds"] == ["worker"]


def test_the_codes_head_is_this_branchs_migration():
    # Read from the migrations folder, as /readyz does in production.
    assert health.migration_heads() and all(isinstance(h, str) for h in health.migration_heads())


async def test_readyz_reads_the_real_database(client, session):
    """Unpatched: the database answers; the suite's schema has no alembic_version, so not at the head."""
    r = await client.get("/readyz")
    checks = r.json()["checks"]
    assert checks["database"] == {"ok": True} and checks["migrations"]["ok"] is False and checks["migrations"]["database"] == []


async def test_the_admin_sees_the_queue_and_retries_a_dead_job(client, session, make_admin, mint_token):
    admin = await make_admin(email="jobs-admin@example.com", role=AdminRoles.owner)
    token = await mint_token(admin)
    now = datetime.now(timezone.utc)
    dead = jobs.Jobs(name="mail.billing_notice", payload={"args": [], "kwargs": {}}, dead=True, attempts=6, last_error="SES is down")
    waiting = jobs.Jobs(name="mail.billing_notice", payload={"args": [], "kwargs": {}}, run_at=now - timedelta(seconds=30))
    session.add_all([dead, waiting])
    await session.flush()
    r = await client.get("/api/v1/admin/jobs", headers=auth_header(token))
    assert r.status_code == 200
    [q] = [q for q in r.json()["queues"] if q["name"] == "mail.billing_notice"]
    assert q["dead"] == 1 and q["due"] == 1 and r.json()["mode"] == "in-process"
    # Only a dead job can be retried.
    assert (await client.post(f"/api/v1/admin/jobs/{waiting.id}/retry", headers=auth_header(token))).status_code == 409
    r = await client.post(f"/api/v1/admin/jobs/{dead.id}/retry", headers=auth_header(token))
    assert r.status_code == 200 and r.json()["requeued"] is True
    row = (await session.scalars(select(jobs.Jobs).where(jobs.Jobs.id == dead.id).execution_options(populate_existing=True))).one()
    # Back in the queue and run after the request (its args are empty, so the strict send fails at once: attempt 1).
    assert not row.dead and row.attempts == 1 and row.last_error


async def test_an_app_user_cannot_see_the_queue(client, make_user, mint_token):
    user = await make_user(email="not-admin@example.com")
    r = await client.get("/api/v1/admin/jobs", headers=auth_header(await mint_token(user)))
    assert r.status_code in (401, 403)
