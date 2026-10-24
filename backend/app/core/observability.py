"""Error tracking (Sentry), JSON logs and request ids. All off by default.

- APP_SENTRY_DSN set: errors (the 500s, and anything logged at ERROR) go to
  Sentry, tagged with the release (the git sha the deploy wrote to
  backend/REVISION), the environment and the request id; a share of requests
  is traced when APP_SENTRY_TRACES_SAMPLE_RATE is above 0. Unset: sentry_sdk
  is never imported.
- APP_LOG_JSON=true: every log line is one JSON object (time, level, logger,
  message, request id; method, path and status for uvicorn's access line),
  for a log shipper to index. Unset: uvicorn's own format, as before.
- Either one on: each request carries an id. A caller's X-Request-ID is kept
  when it looks like one (8-128 of [A-Za-z0-9._:-]); otherwise one is made.
  It is sent back in X-Request-ID, stamped on every log line the request
  causes, tagged on its Sentry event, and is the track_id of a 500 -- so a
  user's screenshot of an error leads to its log lines and its event.

Personal data never leaves for Sentry (scrub_event): no request bodies, no
cookies, no local variables; the Authorization, Cookie, token and forwarded
address headers are filtered; and every string in an event -- exception
messages, log messages, breadcrumbs, URLs -- has emails, phone numbers,
bearer tokens, JWTs, known key shapes and this process's own secrets
replaced. Sentry's own scrubber runs too (send_default_pii=False).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
#: Whether init_sentry started Sentry in this process.
_sentry_started = False

BACKEND = Path(__file__).resolve().parents[2]


def current_request_id() -> Optional[str]:
    return _request_id.get()


def sentry_enabled() -> bool:
    return bool((settings.SENTRY_DSN or "").strip())


def request_ids_enabled() -> bool:
    return bool(settings.LOG_JSON) or sentry_enabled()


# ---------------------------------------------------------------------------
# Request ids
# ---------------------------------------------------------------------------


class RequestIdMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware task hop): sets the request id for
    everything downstream and adds it to the response's headers. doth.py
    puts it outside the whole middleware stack (Application.
    build_middleware_stack), so a 500 or a rate-limited answer carries it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for name, value in scope.get("headers") or ():
            if name == b"x-request-id":
                incoming = value.decode("latin-1").strip()
                break
        request_id = incoming if incoming and _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = _request_id.set(request_id)
        if _sentry_started:
            import sentry_sdk

            sentry_sdk.set_tag("request_id", request_id)

        async def send_with_id(message):
            if message.get("type") == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


# ---------------------------------------------------------------------------
# JSON logs
# ---------------------------------------------------------------------------


class RequestIdFilter(logging.Filter):
    """Stamps the request id on the record when it is made, in the request's
    own context (a handler writing later could be in another)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or current_request_id()
        if request_id:
            out["request_id"] = request_id
        # uvicorn's access line: (client, method, path, http version, status).
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, version, status = record.args
            out.update(client=client, method=method, path=path, http=version, status=status)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, default=str, ensure_ascii=False)


def logging_config() -> dict:
    """A dictConfig for uvicorn.run(log_config=...) and for processes that are
    not uvicorn (configure_logging). The root logger stays at WARNING: what
    Python printed before (its last-resort handler), now as JSON."""
    handler = {
        "class": "logging.StreamHandler",
        "formatter": "json",
        "filters": ["request_id"],
        "stream": "ext://sys.stdout",
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_id": {"()": "app.core.observability.RequestIdFilter"}},
        "formatters": {"json": {"()": "app.core.observability.JsonFormatter"}},
        "handlers": {"json": handler},
        "loggers": {
            "uvicorn": {"handlers": ["json"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["json"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["json"], "level": "WARNING"},
    }


def configure_logging() -> bool:
    """JSON logs for a process uvicorn does not start (a worker, a script).
    Does nothing, and returns False, when APP_LOG_JSON is off."""
    if not settings.LOG_JSON:
        return False
    import logging.config

    logging.config.dictConfig(logging_config())
    return True


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

FILTERED = "[Filtered]"
_SENSITIVE_KEY = re.compile(
    r"auth|cookie|token|secret|passw|api[_-]?key|signature|session|otp|x-site|forwarded-for|real-ip|^key$|^code$",
    re.I,
)
_TEXT_RULES = (
    (re.compile(r"(\w+://)([^/@\s:]*):([^/@\s]+)@"), r"\1\2:" + FILTERED + "@"),
    (re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}"), "[jwt]"),
    (re.compile(r"(?i)\b(bearer)\s+[\w.~+/=-]{8,}"), r"\1 [token]"),
    (re.compile(r"\b(?:rzp_(?:live|test)_|whsec_|sk_live_|sk_test_)[A-Za-z0-9_]{8,}"), "[key]"),
    # Resend's re_...: only with a digit and a capital in it, so a function
    # called re_send_tickets in a traceback is left alone.
    (re.compile(r"\bre_(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Z])[A-Za-z0-9_]{16,}"), "[key]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[key]"),
    # Query values: the live stream's keys (key, player, last_event_id; see
    # app.core.redact), any ...token..., and the usual credentials.
    (re.compile(r"(?i)([?&;\s](?:\w*token\w*|code|key|player|last_event_id|signature|password|secret|otp|email|phone)=)[^&\s#]+"), r"\1" + FILTERED),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?<![\w+])(?:\+91[\s-]?|0)?[6-9]\d{9}(?!\d)"), "[phone]"),
    (re.compile(r"(?<![\w+])\+\d{1,3}[\s-]?\d{6,12}(?!\d)"), "[phone]"),
)


@lru_cache(maxsize=1)
def _own_secrets() -> tuple[str, ...]:
    """This process's own credentials, longest first: never sent anywhere."""
    from urllib.parse import unquote, urlsplit

    values = [
        settings.SECRET_KEY, settings.S3_ACCESS_KEY, settings.S3_SECRET_KEY, settings.SES_ACCESS_KEY,
        settings.SES_SECRET_KEY, settings.RAZORPAY_KEY_SECRET, settings.RAZORPAY_WEBHOOK_SECRET,
        settings.RESEND_API_KEY, settings.RESEND_WEBHOOK_SECRET, settings.WHATSAPP_ACCESS_TOKEN,
        settings.WHATSAPP_APP_SECRET, settings.CLOUDFLARE_API_TOKEN, settings.SITE_REVALIDATE_SECRET,
        settings.PAYMENT_KEYS_SECRET, settings.SES_WEBHOOK_TOKEN,
        settings.BACKUP_S3_ACCESS_KEY, settings.BACKUP_S3_SECRET_KEY, *settings.site_tokens,
    ]
    for url in (settings.DATABASE_URL, settings.DATABASE_URL_SYNC):
        try:
            password = urlsplit(url).password
        except ValueError:
            password = None
        if password:
            values += [password, unquote(password)]
    return tuple(sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True))


def scrub_text(text: str) -> str:
    for secret in _own_secrets():
        if secret in text:
            text = text.replace(secret, FILTERED)
    for pattern, replacement in _TEXT_RULES:
        text = pattern.sub(replacement, text)
    return text


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: (FILTERED if isinstance(k, str) and _SENSITIVE_KEY.search(k) and v not in (None, "", [], {}) else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v, depth + 1) for v in value)
    return value


def _headers(headers: Any) -> Any:
    def one(name, value):
        return FILTERED if _SENSITIVE_KEY.search(str(name)) else scrub_text(str(value))

    if isinstance(headers, dict):
        return {k: one(k, v) for k, v in headers.items()}
    if isinstance(headers, list):
        return [
            [pair[0], one(pair[0], pair[1])] if isinstance(pair, (list, tuple)) and len(pair) == 2 else pair
            for pair in headers
        ]
    return headers


def scrub_event(event: dict, hint: Optional[dict] = None) -> dict:
    """Sentry's before_send (and before_send_transaction): the event with no
    personal data and no credentials in it."""
    from app.core import redact

    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        request.pop("data", None)
        request.pop("env", None)
        if "headers" in request:
            request["headers"] = _headers(request["headers"])
        if isinstance(request.get("url"), str):
            request["url"] = scrub_text(redact.path(request["url"]))
        if isinstance(request.get("query_string"), str):
            request["query_string"] = scrub_text("?" + request["query_string"])[1:]
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": user["id"]} if user.get("id") is not None else {}
    for key in ("exception", "threads", "message", "logentry", "extra", "contexts", "tags", "breadcrumbs", "transaction"):
        if key in event:
            event[key] = _scrub(event[key])
    # Frame variables are off (include_local_variables=False); if anything
    # brought them back, drop them rather than trust the scrub.
    for exception in ((event.get("exception") or {}).get("values") or []):
        for frame in ((exception.get("stacktrace") or {}).get("frames") or []):
            frame.pop("vars", None)
    return event


def scrub_breadcrumb(crumb: dict, hint: Optional[dict] = None) -> dict:
    return _scrub(crumb)


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------


def release() -> Optional[str]:
    """APP_SENTRY_RELEASE, else the git sha: backend/REVISION (the deploy
    writes it; the server has no .git), else `git rev-parse` in a checkout."""
    if settings.SENTRY_RELEASE:
        return settings.SENTRY_RELEASE
    sha = None
    try:
        sha = (BACKEND / "REVISION").read_text().strip() or None
    except OSError:
        pass
    if sha is None:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True, text=True, timeout=2,
            ).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            sha = None
    if sha and re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return f"myotherapp-backend@{sha[:12]}"
    return None


def _rate(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def init_sentry(component: Optional[str] = None) -> bool:
    """Start Sentry when APP_SENTRY_DSN is set; False (and nothing imported)
    when not. Call before the app's routes are built (doth.py does, at the
    top), so the integrations see every route. Events are tagged with the
    component: this process's APP_PROCESS (api, or worker for the job
    worker, which imports doth too) unless one is given."""
    if not sentry_enabled():
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        # A DSN set on a container whose image predates sentry-sdk: say so,
        # and serve without error tracking rather than not at all. Rebuild
        # the image (install_requirements) to turn it on.
        logger.error("APP_SENTRY_DSN is set but sentry-sdk is not installed in this image; error tracking is off")
        return False

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN.strip(),
        environment=settings.SENTRY_ENVIRONMENT or None,
        release=release(),
        sample_rate=_rate(settings.SENTRY_SAMPLE_RATE),
        traces_sample_rate=_rate(settings.SENTRY_TRACES_SAMPLE_RATE),
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=scrub_event,
        before_send_transaction=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
        integrations=[
            # The route's template (/api/v1/events/{event_id}), never the
            # path with somebody's id or slug in it.
            StarletteIntegration(transaction_style="url"),
            FastApiIntegration(transaction_style="url"),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
    sentry_sdk.set_tag("component", component or settings.PROCESS)
    global _sentry_started
    _sentry_started = True
    logger.info("Sentry is on (%s, release %s)", settings.SENTRY_ENVIRONMENT, release())
    return True
