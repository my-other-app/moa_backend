"""Sentry, end to end (app/core/observability.py): what is sent, what is not.

- the release is the deployed git sha (backend/REVISION, else git), the
  environment APP_SENTRY_ENVIRONMENT or else APP_ENV;
- nothing personal leaves: no request bodies, no Authorization, cookies or
  forwarded addresses, no emails or phone numbers or tokens in breadcrumbs
  (log lines, outgoing HTTP calls) or messages;
- a 4xx -- the API's own refusals, FastAPI's, validation, the rate limiter,
  an unknown route -- is never an error event; a 500 is;
- the job worker reports too: a job that dies sends its exception, tagged
  component=worker.

Sentry patches Starlette, FastAPI and httpx when it starts, so everything
with a live client runs in a child process (as tests/test_observability.py
does), never in the suite's own."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.config import settings
from app.core import observability as obs

BACKEND = Path(__file__).resolve().parents[1]
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJlLXZhbHVlLWhlcmU"

CAPTURE = '''
import json, sys
import sentry_sdk
from sentry_sdk.transport import Transport

captured, inits = [], []

class Capture(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type in ("event", "transaction"):
                captured.append(json.loads(item.get_bytes()))

real_init = sentry_sdk.init

def init(**kw):
    inits.append({k: v for k, v in kw.items() if isinstance(v, (str, int, float, bool, type(None)))})
    return real_init(transport=Capture, **kw)

sentry_sdk.init = init
'''

API_CHILD = CAPTURE + textwrap.dedent('''
    import asyncio, logging
    import httpx
    from fastapi import Body, HTTPException, Request

    import doth
    from app.core.rate_limit import limiter
    from app.response import CustomHTTPException
    from httpx import ASGITransport, AsyncClient

    JWT = sys.argv[2]
    app = doth.application

    @app.get("/__t/missing")
    async def missing():
        raise CustomHTTPException(404, message="No such event")

    @app.get("/__t/forbidden")
    async def forbidden():
        raise HTTPException(status_code=403, detail="Not yours")

    @app.post("/__t/invalid")
    async def invalid(n: int = Body(..., embed=True)):
        return {"n": n}

    @app.get("/__t/limited")
    @limiter.limit("1/minute")
    async def limited(request: Request):
        return {"ok": True}

    @app.get("/__t/conflict")
    async def conflict():
        raise CustomHTTPException(409, message="Already registered: riya@example.com")

    @app.post("/__t/boom")
    async def boom(body: dict = Body(...)):
        logging.getLogger("app.payments").warning("looked up riya@example.com at +91 9876543210")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://partner.test/lookup?email=riya@example.com&token=s3cr3t-Token-Value")
        raise RuntimeError("the payment step failed")

    async def main():
        out = {}
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t") as c:
            out["missing"] = (await c.get("/__t/missing")).status_code
            out["forbidden"] = (await c.get("/__t/forbidden")).status_code
            out["invalid"] = (await c.post("/__t/invalid", json={"n": "not a number"})).status_code
            out["limited"] = [(await c.get("/__t/limited")).status_code for _ in range(2)]
            out["conflict"] = (await c.get("/__t/conflict")).status_code
            out["unknown"] = (await c.get("/__t/no-such-route")).status_code
            out["boom"] = (await c.post(
                "/__t/boom",
                json={"email": "riya@example.com", "password": "hunter2-Secret", "phone": "9876543210"},
                headers={"Authorization": "Bearer " + JWT, "Cookie": "access_token=abc123; refresh_token=def456",
                         "X-Forwarded-For": "203.0.113.7", "X-Real-IP": "203.0.113.7"},
            )).status_code
        sentry_sdk.flush()
        return out

    result = asyncio.run(main())
    with open(sys.argv[1], "w") as handle:
        json.dump({"statuses": result, "events": captured, "inits": inits}, handle)
''')

WORKER_CHILD = 'import os\nos.environ["APP_PROCESS"] = "worker"\n' + CAPTURE + textwrap.dedent('''
    import asyncio
    import sqlalchemy as sa

    from app.core import observability
    import doth  # noqa: F401 -- as app/worker.py does: Sentry starts here, tagged worker
    from app.core import jobs
    from app.db.core import AsyncSessionLocal

    @jobs.handler("test.sentry_dead_job", max_attempts=1, alert_dead=False)
    async def dead_job(payload):
        raise ValueError(f"mail to {payload['email']} bounced (+91 9876543210)")

    async def main():
        async with AsyncSessionLocal() as s:
            await jobs.enqueue(s, "test.sentry_dead_job", {"email": "riya@example.com"})
            await s.commit()
            job_id = await s.scalar(sa.select(sa.func.max(jobs.Jobs.id)).where(jobs.Jobs.name == "test.sentry_dead_job"))
        try:
            ran = await jobs.run_due(ids=[job_id])
        finally:
            async with AsyncSessionLocal() as s:
                await s.execute(sa.delete(jobs.Jobs).where(jobs.Jobs.name == "test.sentry_dead_job"))
                await s.commit()
        sentry_sdk.flush()
        return ran

    ran = asyncio.run(main())
    with open(sys.argv[1], "w") as handle:
        json.dump({"ran": ran, "events": captured, "inits": inits}, handle)
''')


def _run(script: str, tmp_path, **env_extra) -> dict:
    path = tmp_path / "child.py"
    path.write_text(script)
    result = tmp_path / "result.json"
    env = {**os.environ, "APP_SENTRY_DSN": "https://public@o0.ingest.sentry.io/0", "PYTHONPATH": str(BACKEND)}
    env.pop("APP_SENTRY_ENVIRONMENT", None)
    env.update(env_extra)
    run = subprocess.run(
        [sys.executable, str(path), str(result), JWT], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    return json.loads(result.read_text())


@pytest.fixture(scope="module")
def api_result(tmp_path_factory):
    return _run(API_CHILD, tmp_path_factory.mktemp("sentry-api"), APP_ENV="staging", APP_SENTRY_TRACES_SAMPLE_RATE="0")


def _errors(events: list) -> list:
    return [e for e in events if e.get("exception")]


# ---------------------------------------------------------------------------
# Release and environment
# ---------------------------------------------------------------------------


def test_the_release_is_the_deployed_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "BACKEND", tmp_path)
    monkeypatch.setattr(settings, "SENTRY_RELEASE", "")
    (tmp_path / "REVISION").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert obs.release() == "myotherapp-backend@0123456789ab"
    assert obs.revision() == "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "REVISION").write_text("not a sha\n")
    assert obs.release() is None  # and tmp_path is no git checkout to ask
    monkeypatch.setattr(settings, "SENTRY_RELEASE", "myotherapp-backend@hotfix-1")
    assert obs.release() == "myotherapp-backend@hotfix-1"


@pytest.mark.parametrize(
    "sentry_env, app_env, expected",
    [("", "", "production"), ("", "staging", "staging"), ("qa", "staging", "qa"), ("", "test", "test")],
)
def test_the_environment(monkeypatch, sentry_env, app_env, expected):
    monkeypatch.setattr(settings, "SENTRY_ENVIRONMENT", sentry_env)
    monkeypatch.setattr(settings, "ENV", app_env)
    assert obs.environment() == expected


def test_init_options(api_result):
    [options] = api_result["inits"]
    assert options["environment"] == "staging"  # APP_ENV, with no APP_SENTRY_ENVIRONMENT
    assert options["release"].startswith("myotherapp-backend@")
    assert options["send_default_pii"] is False
    assert options["max_request_body_size"] == "never"
    assert options["include_local_variables"] is False


# ---------------------------------------------------------------------------
# 4xx are not errors; a 500 is
# ---------------------------------------------------------------------------


def test_refusals_are_not_reported(api_result):
    statuses = api_result["statuses"]
    assert statuses["missing"] == 404 and statuses["forbidden"] == 403 and statuses["invalid"] == 422
    assert statuses["limited"] == [200, 429] and statuses["conflict"] == 409 and statuses["unknown"] == 404
    assert statuses["boom"] == 500
    errors = _errors(api_result["events"])
    types = [e["exception"]["values"][-1]["type"] for e in errors]
    assert types == ["RuntimeError"], types
    # No other event at all -- not a logged error either -- from the refusals.
    others = [e for e in api_result["events"] if "/__t/boom" not in json.dumps(e.get("request") or {})]
    assert others == [], others


def test_scrub_drops_a_4xx_that_reaches_it():
    """Belt and braces: a 4xx exception logged by hand still is not sent."""
    from app.response import CustomHTTPException

    try:
        raise CustomHTTPException(404, message="No such event")
    except CustomHTTPException as exc:
        hint = {"exc_info": (type(exc), exc, exc.__traceback__)}
    assert obs.scrub_event({"exception": {"values": [{"type": "CustomHTTPException"}]}}, hint) is None
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        hint = {"exc_info": (type(exc), exc, exc.__traceback__)}
    assert obs.scrub_event({"exception": {"values": [{"type": "RuntimeError"}]}}, hint) is not None


# ---------------------------------------------------------------------------
# Personal data
# ---------------------------------------------------------------------------


def test_nothing_personal_leaves(api_result):
    [event] = _errors(api_result["events"])
    flat = json.dumps(event)
    for leaked in ("riya@example.com", "9876543210", "hunter2-Secret", "s3cr3t-Token-Value", JWT, "abc123",
                   "def456", "203.0.113.7"):
        assert leaked not in flat, leaked
    request = event["request"]
    assert "data" not in request and "cookies" not in request
    headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
    for name in ("authorization", "cookie", "x-forwarded-for", "x-real-ip"):
        assert headers.get(name, obs.FILTERED) == obs.FILTERED, name
    assert set((event.get("user") or {})) <= {"id"}
    assert event["tags"]["component"] == "api"


def test_breadcrumbs_keep_their_shape_without_the_data(api_result):
    [event] = _errors(api_result["events"])
    crumbs = (event.get("breadcrumbs") or {}).get("values") or []
    log = [c for c in crumbs if "looked up" in (c.get("message") or "")]
    assert log and log[0]["message"] == "looked up [email] at [phone]"
    http = [c for c in crumbs if c.get("type") == "http"]
    assert http, crumbs
    text = json.dumps(http)
    assert "partner.test/lookup" in text
    assert "riya@example.com" not in text and "s3cr3t-Token-Value" not in text


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


async def test_a_dead_job_in_the_worker_is_reported_with_its_exception(_engine, tmp_path):
    result = _run(WORKER_CHILD, tmp_path, APP_ENV="production", APP_SENTRY_ENVIRONMENT="production")
    assert result["ran"] == 1
    [options] = result["inits"]
    assert options["environment"] == "production"
    errors = _errors(result["events"])
    assert errors, result["events"]
    event = errors[-1]
    assert event["exception"]["values"][-1]["type"] == "ValueError"
    assert event["tags"]["component"] == "worker"
    assert "test.sentry_dead_job" in json.dumps(event.get("logentry") or event.get("message") or "")
    flat = json.dumps(event)
    assert "riya@example.com" not in flat and "9876543210" not in flat
