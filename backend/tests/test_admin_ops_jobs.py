"""The job queue's failed and stuck jobs (app/api/admin_ops/jobs_admin.py)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.core import jobs
from tests.admin_ops_support import role_headers

API = "/api/v1"


@pytest.fixture
async def queue(session):
    now = datetime.now(timezone.utc)
    rows = {
        "dead": jobs.Jobs(name="mail.send", payload={"to": "x"}, attempts=6, max_attempts=6, dead=True, last_error="SES said no"),
        "failing": jobs.Jobs(name="push.send", payload={}, attempts=2, last_error="FCM timeout", run_at=now + timedelta(minutes=5)),
        "abandoned": jobs.Jobs(name="render.doc", payload={}, attempts=1, locked_by="worker-1", locked_until=now - timedelta(minutes=3),
                               run_at=now - timedelta(minutes=3)),
        "waiting_long": jobs.Jobs(name="closeout.run", payload={}, run_at=now - timedelta(minutes=30)),
        "running": jobs.Jobs(name="render.doc", payload={}, attempts=1, locked_by="worker-2", locked_until=now + timedelta(minutes=1),
                             run_at=now + timedelta(minutes=1)),
        "fine": jobs.Jobs(name="mail.send", payload={}, run_at=now + timedelta(minutes=1)),
    }
    session.add_all(rows.values())
    await session.commit()
    return rows


async def test_dead_failing_and_stuck_jobs_are_listed(client, make_admin, mint_token, queue):
    h = await role_headers(make_admin, mint_token)
    dead = await client.get(f"{API}/admin/jobs/items", params={"state": "dead"}, headers=h["viewer"])
    assert dead.status_code == 200, dead.text
    assert [j["id"] for j in dead.json()["items"]] == [queue["dead"].id]
    assert dead.json()["items"][0]["error"] == "SES said no" and dead.json()["items"][0]["attempts"] == 6
    assert dead.json()["counts"] == {"dead": 1, "failing": 1, "stuck": 2}
    failing = (await client.get(f"{API}/admin/jobs/items", params={"state": "failing"}, headers=h["viewer"])).json()
    assert [j["id"] for j in failing["items"]] == [queue["failing"].id]
    stuck = (await client.get(f"{API}/admin/jobs/items", params={"state": "stuck"}, headers=h["viewer"])).json()
    assert {j["id"] for j in stuck["items"]} == {queue["abandoned"].id, queue["waiting_long"].id}
    assert all(j["state"] == "stuck" for j in stuck["items"])
    assert (await client.get(f"{API}/admin/jobs/items", params={"state": "bogus"}, headers=h["viewer"])).status_code == 422


async def test_cancel_settles_a_job_and_is_for_moderators(client, session, make_admin, mint_token, queue):
    h = await role_headers(make_admin, mint_token)
    assert (await client.post(f"{API}/admin/jobs/{queue['dead'].id}/cancel", headers=h["viewer"])).status_code == 403
    r = await client.post(f"{API}/admin/jobs/{queue['dead'].id}/cancel", headers=h["support"])
    assert r.status_code == 200 and r.json()["was"] == "dead"
    await session.refresh(queue["dead"])
    assert queue["dead"].dead is False and queue["dead"].finished_at is not None
    assert queue["dead"].last_error.startswith("Cancelled by")
    stuck = await client.post(f"{API}/admin/jobs/{queue['abandoned'].id}/cancel", headers=h["owner"])
    assert stuck.status_code == 200 and stuck.json()["was"] == "stuck"
    busy = await client.post(f"{API}/admin/jobs/{queue['running'].id}/cancel", headers=h["owner"])
    assert busy.status_code == 409 and busy.json()["error_code"] == "JOB_RUNNING"
    again = await client.post(f"{API}/admin/jobs/{queue['dead'].id}/cancel", headers=h["owner"])
    assert again.status_code == 409
    audit = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "job.cancel"))).all()
    assert len(audit) == 2
    counts = (await client.get(f"{API}/admin/jobs/items", headers=h["viewer"])).json()["counts"]
    assert counts["dead"] == 0


async def test_retry_still_puts_a_dead_job_back(client, session, make_admin, mint_token, queue, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    monkeypatch.setattr(jobs, "_after_commit", lambda *a, **k: None)
    assert (await client.post(f"{API}/admin/jobs/{queue['dead'].id}/retry", headers=h["viewer"])).status_code == 403
    r = await client.post(f"{API}/admin/jobs/{queue['dead'].id}/retry", headers=h["support"])
    assert r.status_code == 200 and r.json()["requeued"] is True
