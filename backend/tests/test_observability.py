"""Error tracking, JSON logs and request ids (app/core/observability.py).

The scrubbing is tested on hand-made Sentry events; the wiring through the
real application (doth.py) runs in a child process, so Sentry's patching of
Starlette and FastAPI never touches the rest of the suite."""
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.config import settings
from app.core import observability as obs

BACKEND = Path(__file__).resolve().parents[1]
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_everything_is_off_by_default():
    assert settings.SENTRY_DSN == "" and settings.LOG_JSON is False
    assert not obs.sentry_enabled() and not obs.request_ids_enabled()
    assert obs.init_sentry() is False
    assert obs.configure_logging() is False
    import doth

    assert not isinstance(doth.application.build_middleware_stack(), obs.RequestIdMiddleware)


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("failed for priya.k+test@example.co.in", "priya.k+test@example.co.in"),
        ("call +91 9876543210 now", "9876543210"),
        ("call 09876543210", "9876543210"),
        ("whatsapp 9876543210.", "9876543210"),
        (f"Authorization: Bearer {JWT}", JWT),
        (f"token {JWT} expired", JWT),
        ("GET /x?token=abc123secretvalue&page=2", "abc123secretvalue"),
        ("GET /x?email=a@b.co", "a@b.co"),
        ("postgresql://moa:hunter2-pass@db:5432/moa", "hunter2-pass"),
        ("key rzp_live_AbCdEf123456789 failed", "rzp_live_AbCdEf123456789"),
        ("whsec_abcdefghijklmnop is wrong", "whsec_abcdefghijklmnop"),
        ("AKIAIOSFODNN7EXAMPLE denied", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_scrub_text(text, leaked):
    assert leaked not in obs.scrub_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "event 3f2c1a9e-8b7d-4c6e-9f10-123456789012 not found",  # a uuid is kept
        "took 1760000000 ms",                                   # a timestamp is not a phone
        "KeyError: 'club_id'",
        "/api/v1/events/42/registrations",
    ],
)
def test_scrub_text_keeps_what_debugging_needs(text):
    assert obs.scrub_text(text) == text


def test_own_secrets_never_leave():
    obs._own_secrets.cache_clear()
    try:
        out = obs.scrub_text(f"bad key {settings.RAZORPAY_KEY_SECRET} and {settings.SECRET_KEY}")
    finally:
        obs._own_secrets.cache_clear()
    assert settings.RAZORPAY_KEY_SECRET not in out and settings.SECRET_KEY not in out


def _event() -> dict:
    return {
        "request": {
            "method": "POST",
            "url": "https://api.myotherapp.in/api/v1/payments/webhook/club/AbCdEfGhIjKlMnOpQrSt",
            "query_string": "email=riya@example.com&page=2",
            "headers": {
                "Authorization": f"Bearer {JWT}",
                "Cookie": "access_token=abc; refresh_token=def",
                "X-Site-Token": "a" * 40,
                "X-Forwarded-For": "203.0.113.9",
                "User-Agent": "MyOtherApp/2.1 (riya@example.com)",
                "Content-Type": "application/json",
            },
            "cookies": {"access_token": "abc"},
            "data": {"email": "riya@example.com", "password": "pw"},
            "env": {"REMOTE_ADDR": "203.0.113.9"},
        },
        "user": {"id": 7, "email": "riya@example.com", "ip_address": "203.0.113.9"},
        "exception": {"values": [{
            "type": "ValueError",
            "value": "no account for riya@example.com (+91 9876543210)",
            "stacktrace": {"frames": [{"function": "login", "vars": {"password": "pw", "email": "riya@example.com"}}]},
        }]},
        "logentry": {"message": "sign-in failed for %s", "params": ["riya@example.com"]},
        "breadcrumbs": {"values": [{"category": "httplib", "data": {"url": "https://nominatim.example/search?q=12+MG+Road&email=ops@myotherapp.in"}, "message": "otp 9876543210"}]},
        "extra": {"password": "pw", "note": "for riya@example.com"},
        "tags": {"request_id": "abc12345"},
    }


def test_scrub_event_removes_personal_data_and_credentials():
    event = obs.scrub_event(_event())
    flat = json.dumps(event)
    for leaked in ("riya@example.com", "9876543210", JWT, "203.0.113.9", "abc; refresh", "a" * 40,
                   "AbCdEfGhIjKlMnOpQrSt", "ops@myotherapp.in", '"pw"'):
        assert leaked not in flat, leaked
    request = event["request"]
    assert "cookies" not in request and "data" not in request and "env" not in request
    assert request["headers"]["Authorization"] == obs.FILTERED
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["method"] == "POST"
    assert request["url"].startswith("https://api.myotherapp.in/api/v1/payments/webhook/club/AbCdEf")
    assert "page=2" in request["query_string"]
    assert event["user"] == {"id": 7}
    value = event["exception"]["values"][0]
    assert value["type"] == "ValueError" and "no account for" in value["value"]
    assert "vars" not in value["stacktrace"]["frames"][0]
    assert event["tags"]["request_id"] == "abc12345"


# ---------------------------------------------------------------------------
# Request ids and JSON lines
# ---------------------------------------------------------------------------


def _app(seen: list):
    log = logging.getLogger("tests.observability")

    async def endpoint(request):
        seen.append(obs.current_request_id())
        log.warning("inside the request")
        return PlainTextResponse("ok")

    return obs.RequestIdMiddleware(Starlette(routes=[Route("/", endpoint)]))


async def test_request_id_made_kept_or_replaced(caplog):
    seen: list = []
    async with AsyncClient(transport=ASGITransport(app=_app(seen)), base_url="http://t") as client:
        made = await client.get("/")
        kept = await client.get("/", headers={"X-Request-ID": "deploy-smoke-0001"})
        replaced = await client.get("/", headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert len(made.headers["x-request-id"]) == 32 and seen[0] == made.headers["x-request-id"]
    assert kept.headers["x-request-id"] == "deploy-smoke-0001" == seen[1]
    assert replaced.headers["x-request-id"] != "<script>alert(1)</script>"
    assert obs.current_request_id() is None  # reset after the request


def test_json_lines():
    formatter = obs.JsonFormatter()
    stamp = obs.RequestIdFilter()
    record = logging.LogRecord("app.x", logging.ERROR, __file__, 1, "failed %s", ("once",), None)
    token = obs._request_id.set("req-00000001")
    try:
        stamp.filter(record)
    finally:
        obs._request_id.reset(token)
    line = json.loads(formatter.format(record))
    assert line["level"] == "ERROR" and line["logger"] == "app.x" and line["msg"] == "failed once"
    assert line["request_id"] == "req-00000001" and line["ts"].endswith("+00:00")

    access = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", "/api/v1/plans", "1.1", 200), None,
    )
    line = json.loads(formatter.format(access))
    assert (line["method"], line["path"], line["status"]) == ("GET", "/api/v1/plans", 200)


def test_logging_config_is_valid():
    import logging.config

    config = obs.logging_config()
    logging.config.dictConfig({**config, "root": {"handlers": [], "level": "WARNING"}, "loggers": {}})


# ---------------------------------------------------------------------------
# Through the real application, in a child process
# ---------------------------------------------------------------------------

CHILD = textwrap.dedent('''
    import asyncio, json, logging, sys
    import sentry_sdk
    from sentry_sdk.transport import Transport

    captured = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type in ("event", "transaction"):
                    captured.append(json.loads(item.get_bytes()))

    real_init = sentry_sdk.init
    sentry_sdk.init = lambda **kw: real_init(transport=Capture, **kw)

    from app.core import observability
    observability.configure_logging()
    import doth
    from httpx import ASGITransport, AsyncClient

    JWT = sys.argv[2]

    @doth.application.get("/__boom/{who}")
    async def boom(who: str):
        raise ValueError(f"no account for {who} (+91 9876543210), token {JWT}")

    @doth.application.get("/__logged")
    async def logged():
        logging.getLogger("app.test").error("refund failed for %s", "riya@example.com")
        return {"ok": True}

    async def main():
        async with AsyncClient(transport=ASGITransport(app=doth.application, raise_app_exceptions=False), base_url="http://t") as c:
            boom = await c.get("/__boom/riya@example.com?email=riya@example.com",
                               headers={"Authorization": "Bearer " + JWT, "Cookie": "access_token=abc"})
            logged = await c.get("/__logged", headers={"X-Request-ID": "smoke-0000-0001"})
        sentry_sdk.flush()
        return {"boom": [boom.status_code, dict(boom.headers), boom.json()],
                "logged": [logged.status_code, dict(logged.headers)], "events": captured}

    result = asyncio.run(main())
    with open(sys.argv[1], "w") as out:
        json.dump(result, out)
''')


def test_sentry_and_json_logs_through_the_application(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    result_file = tmp_path / "result.json"
    env = {**os.environ, "APP_SENTRY_DSN": "https://public@o0.ingest.sentry.io/0", "APP_LOG_JSON": "true",
           "APP_SENTRY_ENVIRONMENT": "test", "PYTHONPATH": str(BACKEND)}
    run = subprocess.run(
        [sys.executable, str(script), str(result_file), JWT], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr[-3000:]
    result = json.loads(result_file.read_text())

    status, headers, body = result["boom"]
    assert status == 500
    request_id = headers["x-request-id"]
    assert body["track_id"] == request_id  # the id the user sees is the one in the logs

    errors = [e for e in result["events"] if e.get("exception")]
    assert errors, result["events"]
    event = next(e for e in errors if e["exception"]["values"][-1]["type"] == "ValueError")
    flat = json.dumps(event)
    for leaked in ("riya@example.com", "9876543210", JWT, "access_token=abc"):
        assert leaked not in flat, leaked
    assert event["tags"]["request_id"] == request_id
    assert event["tags"]["component"] == "api"
    assert event["environment"] == "test"
    assert event.get("release", "").startswith("myotherapp-backend@")
    assert event["request"]["headers"].get("Authorization", obs.FILTERED) == obs.FILTERED
    assert event["transaction"] == "/__boom/{who}"

    logged_events = [e for e in result["events"] if "refund failed" in json.dumps(e)]
    assert logged_events and "riya@example.com" not in json.dumps(logged_events)
    assert result["logged"][1]["x-request-id"] == "smoke-0000-0001"

    lines = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    refund = [line for line in lines if line["msg"].startswith("refund failed")]
    assert refund and refund[0]["request_id"] == "smoke-0000-0001" and refund[0]["level"] == "ERROR"
